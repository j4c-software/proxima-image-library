"""Stress test suite for the Proxima Image Library.

Requirements:
    pip install locust sseclient-py psutil

Server setup (TEST_MODE — no real SharePoint or Claude calls):
    TEST_MODE=true DEV_AUTH_BYPASS=true \\
        python3 -m gunicorn -w 1 -b 0.0.0.0:5000 "src.app:app"

Run:
    locust -f tests/stress/locustfile.py --host http://localhost:5000
    # then open http://localhost:8089 — pick a user class, set count + ramp

Auth note:
    With DEV_AUTH_BYPASS=true, any request to a @login_required route sets
    the session automatically (no password). Locust sessions are per-user-object
    (each HttpUser has its own requests.Session with its own cookie jar).
    on_start() calls / to warm the session before any timed requests.

Rate-limit note:
    Rate limits are on API paths (/api/stock-search, /api/parse-suggestions, etc.),
    NOT on /auth/login. RateLimitUser targets /api/stock-search (threshold: 20/60s).

Scenarios:
    LibraryReadUser  — Scenario 1: baseline read throughput (public + auth routes)
    CacheStormUser   — Scenario 3: concurrent cache-invalidating writes
    SSEProbeUser     — Scenario 2: probe non-streaming routes while SSE stream is live
    RateLimitUser    — Scenario 5: rate limiter fires at threshold, resets after window
    UploadUser       — Scenario 4: upload pipeline saturation (TEST_MODE only)
"""

import io
import random
import string
import threading
import time

import requests
from locust import HttpUser, between, task, events
from locust.env import Environment

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _random_slug(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


def _generate_jpeg(width: int = 256, height: int = 256) -> bytes:
    """Return a minimal valid JPEG. Pillow required; falls back to a static 1×1."""
    try:
        from PIL import Image as PILImage
        img = PILImage.new("RGB", (width, height), color=(
            random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)
        ))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        return buf.getvalue()
    except ImportError:
        # Minimal valid 1×1 JPEG
        return (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
            b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
            b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\x1eB"
            b"\xedb\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
            b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08"
            b"\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd2\x8a("
            b"\xff\xd9"
        )


def _warm_session(client) -> None:
    """Hit / to get a session cookie under DEV_AUTH_BYPASS. Idempotent."""
    client.get("/", name="[session warmup]", allow_redirects=True)


# ---------------------------------------------------------------------------
# Scenario 1 — Baseline read throughput
# ---------------------------------------------------------------------------

class LibraryReadUser(HttpUser):
    """Hit the main read endpoints under sustained load.

    Target: 50 concurrent users, 5-minute ramp.
    Pass criteria: p95 < 300ms, zero 5xx, RSS growth < 50MB over full run.
    """
    wait_time = between(0.5, 2)

    def on_start(self):
        _warm_session(self.client)

    @task(5)
    def list_images(self):
        self.client.get("/api/images", name="/api/images")

    @task(3)
    def get_tag_library(self):
        self.client.get("/api/tag-library", name="/api/tag-library")

    @task(2)
    def get_version(self):
        self.client.get("/api/version", name="/api/version")

    @task(1)
    def get_metrics(self):
        self.client.get("/debug/metrics", name="/debug/metrics")


# ---------------------------------------------------------------------------
# Scenario 3 — Cache invalidation storm
# ---------------------------------------------------------------------------

_known_record_ids: list[str] = []
_ids_lock = threading.Lock()


def _load_record_ids(client) -> None:
    global _known_record_ids
    with _ids_lock:
        if _known_record_ids:
            return
        try:
            resp = client.get("/api/images", name="[id prefetch]")
            data = resp.json()
            records = data if isinstance(data, list) else data.get("records", [])
            _known_record_ids = [r["id"] for r in records if r.get("id")][:50]
        except Exception:
            pass


def _random_record_id() -> str:
    with _ids_lock:
        if _known_record_ids:
            return random.choice(_known_record_ids)
    return "test-record-id"


class CacheStormUser(HttpUser):
    """Concurrent admin writes that each invalidate _records_cache.

    10 concurrent users, 2 minutes.
    Pass criteria: zero 500s; /debug/metrics shows cache_size stays stable
    (not oscillating to 0 every few seconds, which would indicate thrashing).
    """
    wait_time = between(0.1, 0.5)

    def on_start(self):
        _warm_session(self.client)
        _load_record_ids(self.client)

    @task(3)
    def update_status(self):
        status = random.choice(["approved", "pending-review", "rejected"])
        self.client.post(
            "/api/image/status",
            json={"record_id": _random_record_id(), "status": status},
            name="/api/image/status",
        )

    @task(2)
    def update_tags(self):
        tags = random.choice(["community, service", "outreach", "headshot, portrait"])
        self.client.post(
            "/api/image/tags",
            json={"record_id": _random_record_id(), "tags": tags},
            name="/api/image/tags",
        )

    @task(1)
    def check_cache_state(self):
        self.client.get("/debug/metrics", name="/debug/metrics")


# ---------------------------------------------------------------------------
# Scenario 2 — SSE stream non-blocking probe
# ---------------------------------------------------------------------------

_sse_holder_started = False
_sse_holder_lock = threading.Lock()


def _start_sse_holder(base_url: str, duration_seconds: int = 120) -> None:
    """Start a background thread that holds an SSE stream open.

    One holder per test run — subsequent calls are no-ops.
    """
    global _sse_holder_started
    with _sse_holder_lock:
        if _sse_holder_started:
            return
        _sse_holder_started = True

    def _stream():
        try:
            import sseclient  # pip install sseclient-py
            with requests.get(
                f"{base_url}/api/maintenance/retag-run?max_records=3",
                stream=True,
                timeout=(10, duration_seconds + 10),
            ) as resp:
                client = sseclient.SSEClient(resp)
                deadline = time.time() + duration_seconds
                for event in client:
                    if time.time() > deadline:
                        break
                    if event.data and ("[DONE]" in event.data or "[ERROR]" in event.data):
                        break
        except Exception:
            pass

    threading.Thread(target=_stream, daemon=True, name="sse-holder").start()


class SSEProbeUser(HttpUser):
    """Verify non-streaming routes respond normally while an SSE stream is held open.

    The first user to start kicks off a background SSE stream. All users then
    probe the read endpoints concurrently for the duration of the stream.

    Pass criteria: /api/images p95 stays < 500ms even with SSE stream live.
    Failure mode: if the single Gunicorn worker blocks on the SSE stream,
    all these requests will queue — visible as p95 climbing toward the timeout.
    """
    wait_time = between(0.2, 1)

    def on_start(self):
        _warm_session(self.client)
        _start_sse_holder(self.host)

    @task(5)
    def list_images(self):
        self.client.get("/api/images", name="/api/images [during SSE]")

    @task(3)
    def get_tag_library(self):
        self.client.get("/api/tag-library", name="/api/tag-library [during SSE]")

    @task(2)
    def check_metrics(self):
        self.client.get("/debug/metrics", name="/debug/metrics [during SSE]")


# ---------------------------------------------------------------------------
# Scenario 5 — Rate limit enforcement
# ---------------------------------------------------------------------------

_saw_429 = False
_saw_429_lock = threading.Lock()


class RateLimitUser(HttpUser):
    """Hammer /api/stock-search to trigger the rate limiter (threshold: 20/60s).

    Pass criteria:
      - At least one 429 seen across all users during burst
      - After a 61s wait, requests return non-429

    Note: rate limits are keyed by (path, client_ip). All Locust users share
    the same loopback IP in a local run, so 20 total requests across all users
    will trigger it — not per-user.
    """
    wait_time = between(0.05, 0.15)

    def on_start(self):
        _warm_session(self.client)

    @task
    def probe_rate_limited_path(self):
        global _saw_429
        with self.client.get(
            "/api/stock-search?q=stress-test&source=pexels",
            name="/api/stock-search [rate-limit test]",
            catch_response=True,
        ) as resp:
            if resp.status_code == 429:
                with _saw_429_lock:
                    _saw_429 = True
                resp.success()  # 429 is the expected outcome
            elif resp.status_code in (200, 400):
                resp.success()
            else:
                resp.failure(f"Unexpected {resp.status_code}")


# ---------------------------------------------------------------------------
# Scenario 4 — Upload pipeline saturation (TEST_MODE only)
# ---------------------------------------------------------------------------

class UploadUser(HttpUser):
    """Concurrent image uploads that exercise the full processing pipeline.

    Run against TEST_MODE=true only (Claude calls mocked, local file storage).
    5 concurrent users, 3 minutes.

    Watch via /debug/metrics: active_threads climbing = uploads in-flight.
    Pass criteria: no 5xx, no worker hangs, all uploads either succeed or
    return a clean 400/413 (not a timeout or connection drop).
    """
    wait_time = between(1, 3)

    def on_start(self):
        _warm_session(self.client)

    @task
    def upload_image(self):
        img_bytes = _generate_jpeg(512, 512)
        fname = f"stress-{_random_slug()}.jpg"
        category = random.choice(["Community", "Situations", "Locations"])

        with self.client.post(
            "/api/upload/process",
            files={"file": (fname, img_bytes, "image/jpeg")},
            data={"category": category},
            name="/api/upload/process",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 202, 400, 413, 429):
                resp.success()
            else:
                resp.failure(f"Unexpected {resp.status_code}")


# ---------------------------------------------------------------------------
# Standalone rate-limit verification (no Locust needed)
# ---------------------------------------------------------------------------

def verify_rate_limit(base_url: str = "http://localhost:5000") -> None:
    """Standalone rate-limit check — run directly without Locust.

    Usage:
        TEST_MODE=true DEV_AUTH_BYPASS=true python3 -m gunicorn ... &
        python3 -c "
        from tests.stress.locustfile import verify_rate_limit
        verify_rate_limit()
        "
    """
    session = requests.Session()
    session.get(f"{base_url}/")  # warm session cookie

    print("Sending 25 requests to /api/stock-search (threshold: 20/60s)...")
    statuses = []
    for i in range(25):
        r = session.get(f"{base_url}/api/stock-search?q=test&source=pexels")
        statuses.append(r.status_code)
        print(f"  [{i+1:2d}] {r.status_code}")

    if 429 in statuses:
        print(f"PASS: Rate limit fired at request {statuses.index(429) + 1}.")
    else:
        print("FAIL: Rate limit never fired in 25 requests.")
        return

    print("Waiting 61s for window to reset...")
    time.sleep(61)

    r = session.get(f"{base_url}/api/stock-search?q=test&source=pexels")
    if r.status_code != 429:
        print(f"PASS: Post-reset request returned {r.status_code}.")
    else:
        print("FAIL: Rate limit did not reset after 61s.")


# ---------------------------------------------------------------------------
# Locust event hooks
# ---------------------------------------------------------------------------

@events.quitting.add_listener
def on_quitting(environment: Environment, **kwargs):
    stats = environment.stats
    total = stats.total
    print("\n=== Stress Test Summary ===")
    print(f"  Requests : {total.num_requests}")
    print(f"  Failures : {total.num_failures} ({100 * total.num_failures / max(total.num_requests, 1):.1f}%)")
    print(f"  p50 (ms) : {total.get_response_time_percentile(0.50):.0f}")
    print(f"  p95 (ms) : {total.get_response_time_percentile(0.95):.0f}")
    print(f"  p99 (ms) : {total.get_response_time_percentile(0.99):.0f}")
    print(f"  Rate-limit 429 seen: {'YES' if _saw_429 else 'no'}")
    print("===========================\n")
