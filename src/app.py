"""Flask web app for Proxima Image Library browser."""

import json
import os
import queue
import re
import hashlib
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import csv
from datetime import date, datetime
from difflib import SequenceMatcher
from io import BytesIO, StringIO
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from typing import Dict, List, Optional
from werkzeug.utils import secure_filename

import functools

import msal
import requests
from flask import Flask, Response, jsonify, redirect, render_template, request, session, stream_with_context, url_for
from flask_cors import CORS
from PIL import Image as PILImage
from werkzeug.exceptions import RequestEntityTooLarge

from src.local_client import LocalClient
from src import __version__
from src.config import Config
from src import ingest_poller
from src.maintenance_helpers import (
    MAINTENANCE_PURGE_STATUSES as _MAINTENANCE_PURGE_STATUSES,
    MAINTENANCE_CANONICAL_CATEGORIES as _MAINTENANCE_CANONICAL_CATEGORIES,
    MAINTENANCE_CATEGORY_ALIASES as _MAINTENANCE_CATEGORY_ALIASES,
    MAINTENANCE_STATE_LOCK as _MAINTENANCE_STATE_LOCK,
    MAINTENANCE_STATE_PATH as _MAINTENANCE_STATE_PATH,
    MAINTENANCE_CHECKPOINT_DIR as _MAINTENANCE_CHECKPOINT_DIR,
    now_utc_iso as _now_utc_iso,
    atomic_json_write as _atomic_json_write,
    sanitize_relative_path as _sanitize_relative_path,
    normalize_filter_key as _normalize_filter_key,
    normalize_compare_text as _normalize_compare_text,
    parse_iso_date as _parse_iso_date,
    record_date_value as _record_date_value,
    category_from_location as _category_from_location,
    canonical_category_name as _canonical_category_name,
    record_for_duplicate_scan as _record_for_duplicate_scan,
    maintenance_default_guardrails as _maintenance_default_guardrails,
    maintenance_default_state as _maintenance_default_state,
    maintenance_load_state as _maintenance_load_state,
    maintenance_save_state as _maintenance_save_state,
    maintenance_record_hash as _maintenance_record_hash,
    maintenance_guardrails as _maintenance_guardrails,
    build_integrity_scorecard as _build_integrity_scorecard,
    collect_drift_candidates as _collect_drift_candidates,
    build_category_normalization_preview as _build_category_normalization_preview,
    run_named_maintenance_job as _run_named_maintenance_job,
    build_exact_duplicate_groups as _build_exact_duplicate_groups,
    build_near_alt_duplicate_groups as _build_near_alt_duplicate_groups,
    collect_duplicate_snapshot as _collect_duplicate_snapshot_helper,
)

load_dotenv()
Config.validate_runtime()

app = Flask(__name__, template_folder="../templates", static_folder="../static")
app.secret_key = Config.FLASK_SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=Config.SESSION_COOKIE_SECURE,
    SESSION_COOKIE_SAMESITE=Config.SESSION_COOKIE_SAMESITE,
    MAX_CONTENT_LENGTH=Config.ADMIN_MAX_UPLOAD_BYTES,  # Per-file limits enforced in route handler
)

# Allow configured origins to call the API (Webflow frontend + localhost dev)
CORS(app, resources={r"/api/*": {"origins": Config.CORS_ORIGINS}},
     supports_credentials=False)

_CSRF_QUERY_REQUIRED_PATHS = {
    "/api/catalog-stock",
    "/api/upload/process",
    "/api/maintenance/retag-run",
    "/api/maintenance/sync-highres",
    "/api/maintenance/folder-ingest",
}
_RATE_LIMIT_RULES = {
    "/api/parse-suggestions": (Config.RATE_LIMIT_AUTH_REQUESTS, Config.RATE_LIMIT_WINDOW_SECONDS),
    "/api/stock-search": (Config.RATE_LIMIT_AUTH_REQUESTS, Config.RATE_LIMIT_WINDOW_SECONDS),
    "/api/download-image": (Config.RATE_LIMIT_AUTH_REQUESTS * 2, Config.RATE_LIMIT_WINDOW_SECONDS),
    "/api/upload/stage": (Config.RATE_LIMIT_AUTH_REQUESTS, Config.RATE_LIMIT_WINDOW_SECONDS),
    "/api/upload/process": (Config.RATE_LIMIT_STREAM_REQUESTS, Config.RATE_LIMIT_WINDOW_SECONDS),
    "/api/catalog-stock": (Config.RATE_LIMIT_STREAM_REQUESTS, Config.RATE_LIMIT_WINDOW_SECONDS),
    "/api/mcp/catalog-stock": (Config.RATE_LIMIT_STREAM_REQUESTS * 2, Config.RATE_LIMIT_WINDOW_SECONDS),
    "/api/mcp/catalog-from-file": (Config.RATE_LIMIT_STREAM_REQUESTS * 2, Config.RATE_LIMIT_WINDOW_SECONDS),
    "/api/mcp/claude-article": (Config.RATE_LIMIT_STREAM_REQUESTS * 2, Config.RATE_LIMIT_WINDOW_SECONDS),
    "/api/mcp/claude-article-auto": (Config.RATE_LIMIT_STREAM_REQUESTS * 2, Config.RATE_LIMIT_WINDOW_SECONDS),
    "/api/mcp/stock-search": (Config.RATE_LIMIT_AUTH_REQUESTS, Config.RATE_LIMIT_WINDOW_SECONDS),
}
_RATE_LIMIT_LOCK = threading.Lock()
_rate_limit_state: dict[tuple[str, str], list[float]] = {}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _auth_bypass_enabled() -> bool:
    # Local-only bypass for faster TEST_MODE development.
    return Config.TEST_MODE and Config.DEV_AUTH_BYPASS


def _get_csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def _same_origin_request() -> bool:
    for header_name in ("Origin", "Referer"):
        value = (request.headers.get(header_name) or "").strip()
        if not value:
            continue
        parsed = urlparse(value)
        if (parsed.netloc or "").lower() == (request.host or "").lower():
            return True
        return False
    return False


def _valid_csrf_token(candidate: str) -> bool:
    session_token = str(session.get("_csrf_token", "") or "")
    if not session_token or not candidate:
        return False
    return secrets.compare_digest(session_token, candidate)


def _csrf_error(message: str) -> Response:
    if request.path.startswith("/api/") or request.is_json:
        return jsonify({"error": message}), 403
    return Response(message, status=403)


def _request_identity_key() -> str:
    if request.path.startswith("/api/mcp/"):
        return f"mcp:{request.remote_addr or 'unknown'}"

    claims = session.get("user", {}) if isinstance(session.get("user", {}), dict) else {}
    identity_values = _user_identity_values(claims)
    if identity_values:
        return f"user:{sorted(identity_values)[0]}"
    return f"ip:{request.remote_addr or 'unknown'}"


def _consume_rate_limit(path: str) -> Optional[int]:
    rule = _RATE_LIMIT_RULES.get(path)
    if rule is None:
        return None

    limit, window_seconds = rule
    now = time.time()
    key = (path, _request_identity_key())

    with _RATE_LIMIT_LOCK:
        timestamps = _rate_limit_state.get(key, [])
        timestamps = [ts for ts in timestamps if now - ts < window_seconds]
        if len(timestamps) >= limit:
            _rate_limit_state[key] = timestamps
            retry_after = max(1, int(window_seconds - (now - timestamps[0])))
            return retry_after

        timestamps.append(now)
        _rate_limit_state[key] = timestamps

        if len(_rate_limit_state) > 1024:
            stale_before = now - (window_seconds * 2)
            stale_keys = [
                state_key
                for state_key, state_timestamps in _rate_limit_state.items()
                if not state_timestamps or state_timestamps[-1] < stale_before
            ]
            for state_key in stale_keys[:256]:
                _rate_limit_state.pop(state_key, None)

    return None


@app.context_processor
def inject_template_security_context():
    return {"csrf_token": _get_csrf_token()}


@app.after_request
def apply_security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )
    if request.is_secure or (request.headers.get("X-Forwarded-Proto", "") or "").lower() == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.errorhandler(RequestEntityTooLarge)
def handle_request_too_large(_exc):
    message = f"Upload exceeds request size limit ({Config.MAX_REQUEST_BYTES // (1024 * 1024)} MB max)"
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), 413
    return Response(message, status=413)

_msal_app_instance: Optional[msal.ConfidentialClientApplication] = None


def _msal_app() -> msal.ConfidentialClientApplication:
    global _msal_app_instance
    if _msal_app_instance is None:
        _msal_app_instance = msal.ConfidentialClientApplication(
            Config.MSAL_CLIENT_ID,
            authority=Config.MSAL_AUTHORITY,
            client_credential=Config.MSAL_CLIENT_SECRET,
        )
    return _msal_app_instance


def _is_bypass_session_user(user_claims: Optional[Dict]) -> bool:
    return isinstance(user_claims, dict) and user_claims.get("auth") == "bypass"


def _is_user_session_expired(user_claims: Optional[Dict]) -> bool:
    """Return True when MSAL `exp` claim is present and in the past."""
    if not isinstance(user_claims, dict):
        return False
    exp = user_claims.get("exp")
    if exp in (None, ""):
        return False
    try:
        return int(exp) <= int(time.time())
    except Exception:
        return False


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if _auth_bypass_enabled():
            session.setdefault("user", {"name": "Local Dev User", "auth": "bypass"})
            return f(*args, **kwargs)

        user_claims = session.get("user")
        if _is_user_session_expired(user_claims):
            session.pop("user", None)
            session.pop("auth_flow", None)
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "Session expired"}), 401
            session["next"] = request.url
            return redirect(url_for("login"))

        if not user_claims:
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "Authentication required"}), 401
            session["next"] = request.url
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def _user_identity_values(user_claims: Dict) -> set[str]:
    values = set()
    for key in ("email", "preferred_username", "upn", "unique_name"):
        val = str(user_claims.get(key, "") or "").strip().lower()
        if val:
            values.add(val)
    return values


def _is_maintenance_admin_user(user_claims: Dict) -> bool:
    # In local bypass mode, keep maintenance available for development workflows.
    if _auth_bypass_enabled():
        return True

    allowed = set(Config.MAINTENANCE_ADMIN_USERS)
    if not allowed:
        return False

    return bool(_user_identity_values(user_claims) & allowed)


@app.before_request
def clear_stale_bypass_session():
    # If bypass was used in a previous run, do not treat that session as
    # authenticated when bypass is now disabled.
    if _auth_bypass_enabled():
        return None
    if _is_bypass_session_user(session.get("user")):
        session.pop("user", None)
        session.pop("auth_flow", None)
    return None


@app.before_request
def enforce_csrf_protection():
    path = request.path or ""
    if path.startswith("/api/mcp/"):
        return None

    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _same_origin_request():
        return _csrf_error("Invalid request origin")

    if path in _CSRF_QUERY_REQUIRED_PATHS:
        token = (request.args.get("csrf_token", "") or "").strip()
        if not _valid_csrf_token(token):
            return _csrf_error("Missing or invalid CSRF token")

    return None


@app.before_request
def enforce_rate_limits():
    # Admin users are exempt from rate limits on batch upload processing.
    if _is_maintenance_admin_user(session.get("user", {})):
        return None

    retry_after = _consume_rate_limit(request.path or "")
    if retry_after is None:
        return None

    response = jsonify({
        "error": "Rate limit exceeded",
        "retry_after_seconds": retry_after,
        "path": request.path,
    })
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


@app.before_request
def enforce_maintenance_admin_access():
    path = request.path or ""
    if (
        path != "/maintenance"
        and path != "/tag-manager"
        and path != "/review"
        and not path.startswith("/api/maintenance/")
        and not path.startswith("/api/tag-library/")
        and not path.startswith("/api/image-status")
        and not path.startswith("/api/image/delete")
        and not path.startswith("/run/")
    ):
        return None

    if _auth_bypass_enabled():
        return None

    user_claims = session.get("user")

    if _is_user_session_expired(user_claims):
        session.pop("user", None)
        session.pop("auth_flow", None)
        if path.startswith("/api/") or request.is_json:
            return jsonify({"error": "Session expired"}), 401
        session["next"] = request.url
        return redirect(url_for("login"))

    if not user_claims:
        if path.startswith("/api/") or request.is_json:
            return jsonify({"error": "Authentication required"}), 401
        session["next"] = request.url
        return redirect(url_for("login"))

    if _is_maintenance_admin_user(user_claims):
        return None

    if path.startswith("/api/") or request.is_json:
        return jsonify({"error": "Admin access required"}), 403
    return render_template("login_error.html", error="Admin access required"), 403


@app.route("/login")
def login():
    if _auth_bypass_enabled():
        session.setdefault("user", {"name": "Local Dev User", "auth": "bypass"})
        return redirect(url_for("index"))

    # Keep login + callback on the same host as MSAL_REDIRECT_URI so the
    # session cookie containing auth_flow is preserved across the redirect.
    redirect_parts = urlparse(Config.MSAL_REDIRECT_URI)
    expected_host = (redirect_parts.netloc or "").lower()
    current_host = (request.host or "").lower()
    if expected_host and current_host and current_host != expected_host:
        canonical_login = f"{redirect_parts.scheme}://{redirect_parts.netloc}{url_for('login')}"
        return redirect(canonical_login)

    flow_kwargs = {
        "redirect_uri": Config.MSAL_REDIRECT_URI,
    }
    if session.pop("force_prompt_login", False):
        flow_kwargs["prompt"] = "login"

    flow = _msal_app().initiate_auth_code_flow(
        Config.MSAL_SCOPES,
        **flow_kwargs,
    )
    session["auth_flow"] = flow
    return redirect(flow["auth_uri"])


@app.route("/auth/callback")
def auth_callback():
    auth_flow = session.get("auth_flow")
    if not auth_flow:
        return render_template(
            "login_error.html",
            error=(
                "Missing login state. Start sign-in from the app login page using the "
                "same host as MSAL_REDIRECT_URI (localhost vs 127.0.0.1 must match)."
            ),
        ), 401

    try:
        result = _msal_app().acquire_token_by_auth_code_flow(
            auth_flow,
            request.args,
        )
        if "error" in result:
            return render_template("login_error.html", error=result.get("error_description", "Authentication failed")), 401
        session["user"] = result.get("id_token_claims", {})
    except Exception:
        return render_template("login_error.html", error="Authentication failed"), 401

    next_url = session.pop("next", url_for("index"))
    return redirect(next_url)


@app.route("/logout")
def logout():
    session.clear()
    if _auth_bypass_enabled():
        return redirect(url_for("index"))
    logout_url = (
        f"{Config.MSAL_AUTHORITY}/oauth2/v2.0/logout"
        f"?post_logout_redirect_uri={url_for('index', _external=True)}"
    )
    return redirect(logout_url)


@app.route("/auth/test-expire-session", methods=["POST"])
@login_required
def test_expire_session():
    """TEST_MODE helper: force-expire current session for auth-flow validation."""
    if not Config.TEST_MODE:
        return jsonify({"error": "Not found"}), 404

    user_claims = session.get("user")
    if isinstance(user_claims, dict):
        user_claims["exp"] = 0
        session["user"] = user_claims
        session["force_prompt_login"] = True
    else:
        session.pop("user", None)

    return jsonify({"ok": True, "message": "Session marked expired"})

_client = None
_sp_client = None
_records_cache: Optional[List] = None
_cache_time: float = 0
CACHE_TTL = 300  # 5-minute cache


def _sse_safe(s: str) -> str:
    """Strip characters that could inject fake SSE event boundaries."""
    return s.replace("\r", "").replace("\n", " ")


def _sse_poll_loop(t: threading.Thread, q: "queue.Queue", timeout_seconds: int,
                   timeout_msg: str, invalidate_cache: bool = False):
    """Yield SSE events from a worker queue until done/error/timeout.

    Replaces the repeated while-True polling pattern across SSE streaming routes.
    Call after starting the worker thread:
        yield from _sse_poll_loop(t, q, 180, "timed out after 3 minutes")
    """
    while True:
        try:
            kind, value = q.get(timeout=timeout_seconds)
        except queue.Empty:
            yield f"data: [ERROR] {_sse_safe(timeout_msg)}\n\n"
            break

        if kind == "progress":
            yield f"data: {_sse_safe(value)}\n\n"
        elif kind == "done":
            if invalidate_cache:
                global _records_cache
                _records_cache = None
            yield f"data: [RESULT] {json.dumps(value, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            break
        elif kind == "error":
            yield f"data: [ERROR] {_sse_safe(value)}\n\n"
            break

    t.join(timeout=5)


def get_client():
    global _client
    if _client is None:
        if Config.TEST_MODE:
            _client = LocalClient()
        else:
            from src.sharepoint_list_client import SharePointListClient
            _client = SharePointListClient()
    return _client


def get_sp_client():
    global _sp_client
    if _sp_client is None:
        from src.sharepoint_client import SharePointClient
        _sp_client = SharePointClient()
    return _sp_client


def get_all_records() -> list:
    global _records_cache, _cache_time
    if _records_cache is None or time.time() - _cache_time > CACHE_TTL:
        _records_cache = get_client().get_all_records()
        _cache_time = time.time()
    return _records_cache


@app.route("/health")
@app.route("/healthz")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/version")
def api_version():
    import subprocess
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        short = sha[:7]
    except Exception:
        sha, short = "unknown", "unknown"
    return jsonify({"version": __version__, "commit": sha, "short": short})


@app.route("/debug/metrics")
@login_required
def debug_metrics():
    """Admin-only endpoint for stress testing and ops monitoring.

    Returns in-process state: memory, cache size, thread count, URL cache size.
    """
    if not _is_maintenance_admin_user(session.get("user", {})):
        return jsonify({"error": "Admin access required"}), 403

    import os
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        rss_mb = proc.memory_info().rss / 1_048_576
    except ImportError:
        rss_mb = None

    return jsonify({
        "rss_mb": round(rss_mb, 1) if rss_mb is not None else "psutil not installed",
        "sp_url_cache_size": len(_sp_url_cache),
        "records_cache_live": _records_cache is not None,
        "records_cache_size": len(_records_cache) if _records_cache else 0,
        "active_threads": threading.active_count(),
    })


@app.route("/")
@app.route("/library")
@login_required
def index():
    user = session.get("user", {})
    is_admin = _is_maintenance_admin_user(user)
    return render_template("index.html", user=user, is_admin=is_admin, app_version=__version__)


# ------------------------------------------------------------------
# Launcher — SSE streaming routes
# ------------------------------------------------------------------

def _stream_command(cmd: list, env: dict = None):
    """Run a command and stream its output as SSE events."""
    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    def generate():
        yield "data: [START]\n\n"
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=run_env,
                cwd=str(Path(__file__).parent.parent),
            )
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line:
                    yield f"data: {line}\n\n"
            proc.wait()
            status = "DONE" if proc.returncode == 0 else f"ERROR (exit {proc.returncode})"
            yield f"data: [{status}]\n\n"
        except Exception as e:
            yield f"data: [ERROR] {e}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/run/scan-test", methods=["POST"])
@login_required
def run_scan_test():
    return _stream_command(
        [sys.executable, "-u", "-m", "src.main"],
        env={"TEST_MODE": "true"},
    )


@app.route("/run/scan-live", methods=["POST"])
@login_required
def run_scan_live():
    return _stream_command([sys.executable, "-u", "-m", "src.main"])


@app.route("/run/clean", methods=["POST"])
@login_required
def run_clean():
    mode = request.get_json(silent=True) or {}
    mode = str(mode.get("mode", "test")).strip() or "test"
    env = {"TEST_MODE": "true"} if mode == "test" else {}
    test_mode_value = "true" if mode == "test" else "false"
    script = (
        "import os\n"
        f"os.environ['TEST_MODE'] = '{test_mode_value}'\n"
        "from dotenv import load_dotenv; load_dotenv('.env')\n"
        "from src.config import Config\n"
        "from src.local_client import LocalClient\n"
        "from src.sharepoint_list_client import SharePointListClient\n"
        "client = LocalClient() if Config.TEST_MODE else SharePointListClient()\n"
        "client.delete_all_records()\n"
    )
    return _stream_command([sys.executable, "-u", "-c", script], env=env)


@app.route("/api/preview")
@login_required
def api_preview():
    """Return counts of total images and how many are new (not yet in the store)."""
    mode = request.args.get("mode", "test")
    try:
        from src.image_scanner import ImageScanner
        from src.local_client import LocalClient
        from src.sharepoint_list_client import SharePointListClient

        scanner = ImageScanner()
        all_images = scanner.get_all_images()
        total = len(all_images)

        client = LocalClient() if mode == "test" else SharePointListClient()
        existing = {r["fields"].get("Filename") for r in client.get_all_records()}

        new_count = sum(
            1 for _, rel in all_images
            if Path(rel).name not in existing
        )
        return jsonify({"total": total, "existing": len(existing), "new": new_count})
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@app.route("/stock-search")
@login_required
def stock_search():
    return render_template("stock_search.html")


@app.route("/api/parse-suggestions", methods=["POST"])
@login_required
def api_parse_suggestions():
    data = request.get_json(force=True)
    content = data.get("content", "")
    if not content:
        return jsonify({"error": "No content provided"}), 400
    from src.stock_client import parse_photo_suggestions
    phrases = parse_photo_suggestions(content)
    return jsonify({"phrases": phrases})


@app.route("/api/stock-search", methods=["POST"])
@login_required
def api_stock_search():
    data = request.get_json(force=True)
    phrases = data.get("phrases", [])
    limit = max(1, min(int(data.get("limit", 25)), 25))
    page = max(1, min(int(data.get("page", 1)), 100))
    library = (data.get("library") or "").strip()
    if not phrases:
        return jsonify({"error": "No phrases provided"}), 400
    phrases = phrases[:20]
    from src.stock_client import search_all_libraries
    libraries = [library] if library else None
    results = search_all_libraries(phrases, limit, page, libraries=libraries)
    return jsonify({"results": results, "page": page, "limit": limit})


_DOWNLOAD_ALLOWED_DOMAINS = {
    "images.pexels.com",
    "www.pexels.com",
    "cdn.pixabay.com",
    "pixabay.com",
    "images.unsplash.com",
}


def _stock_source_context(source: str, title: str, tags: list[str], photographer: str) -> str:
    """Build a context string from stock API metadata to pass to Claude."""
    parts = []
    if source:
        parts.append(f"Source: {source}")
    if title:
        parts.append(f"Title from source: {title}")
    if photographer:
        parts.append(f"Photographer: {photographer}")
    if tags:
        parts.append(f"Keywords from source: {', '.join(tags)}")
    if not parts:
        return ""
    return (
        "\n".join(parts) + "\n\n"
        "Use the above source metadata as a starting point. "
        "Reconcile keywords against the approved tag vocabulary — keep matches, drop anything off-vocabulary."
    )


def _normalized_formats() -> set[str]:
    formats = [fmt if fmt.startswith(".") else f".{fmt}" for fmt in Config.SUPPORTED_FORMATS]
    return {fmt.lower() for fmt in formats}


def _size_label(num_bytes: int) -> str:
    if num_bytes >= 1_048_576:
        return f"{num_bytes / 1_048_576:.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes} B"


def _validate_image_payload(data: bytes, filename: str, max_bytes: int | None = None) -> None:
    if not data:
        raise ValueError(f"{filename}: file is empty")
    limit = max_bytes or Config.MAX_UPLOAD_BYTES
    if len(data) > limit:
        raise ValueError(
            f"{filename}: file exceeds max upload size of {_size_label(limit)}"
        )
    try:
        with PILImage.open(BytesIO(data)) as image:
            image.verify()
            image_format = (image.format or "").upper()
    except Exception:
        raise ValueError(f"{filename}: invalid or corrupt image file")

    if image_format not in {"JPEG", "PNG", "GIF", "WEBP"}:
        raise ValueError(f"{filename}: unsupported image content type {image_format or 'unknown'}")


def _download_limited_bytes(download_url: str, timeout: int = 30, max_bytes: int | None = None) -> bytes:
    limit = max_bytes or Config.MAX_UPLOAD_BYTES
    with requests.get(download_url, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()

        content_length = resp.headers.get("Content-Length", "").strip()
        if content_length:
            try:
                if int(content_length) > limit:
                    raise ValueError(
                        f"Remote file exceeds max upload size of {_size_label(limit)}"
                    )
            except ValueError as exc:
                if "Remote file exceeds" in str(exc):
                    raise

        chunks = bytearray()
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            chunks.extend(chunk)
            if len(chunks) > limit:
                raise ValueError(
                    f"Remote file exceeds max upload size of {_size_label(limit)}"
                )
        return bytes(chunks)


@app.route("/api/catalog-stock")
@login_required
def api_catalog_stock():
    """SSE stream — download a stock image and run the full pipeline with source metadata as context."""
    download_url = request.args.get("download_url", "").strip()
    filename     = request.args.get("filename", "image.jpg").strip() or "image.jpg"
    dl_location  = request.args.get("dl", "").strip()   # Unsplash attribution ping
    source       = request.args.get("source", "").strip()
    title        = request.args.get("title", "").strip()
    tags_raw     = request.args.get("tags", "").strip()
    photographer = request.args.get("photographer", "").strip()
    category     = request.args.get("category", "").strip() or None

    from src.image_processor import CATEGORIES, process_image

    if not download_url:
        return jsonify({"error": "download_url required"}), 400

    domain = urlparse(download_url).netloc.lower()
    if not any(domain == d or domain.endswith("." + d) for d in _DOWNLOAD_ALLOWED_DOMAINS):
        return jsonify({"error": "URL not permitted"}), 403

    if category is not None and category not in CATEGORIES:
        return jsonify({"error": f"Invalid category. Must be one of: {CATEGORIES}"}), 400

    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
    source_context = _stock_source_context(source, title, tags, photographer)

    def generate():
        yield "data: [START]\n\n"
        q: queue.Queue = queue.Queue()

        def worker():
            try:
                # Unsplash attribution ping
                if dl_location:
                    dl_domain = urlparse(dl_location).netloc.lower()
                    if "unsplash.com" in dl_domain:
                        access_key = Config.UNSPLASH_ACCESS_KEY
                        if access_key:
                            try:
                                requests.get(dl_location, headers={"Authorization": f"Client-ID {access_key}"}, timeout=5)
                            except Exception:
                                pass

                q.put(("progress", f"Downloading from {source or 'source'}…"))
                file_bytes = _download_limited_bytes(download_url, timeout=30)
                _validate_image_payload(file_bytes, filename)

                from src.ai_generator import AltTextGenerator
                gen = AltTextGenerator()

                if Config.TEST_MODE:
                    list_client = LocalClient()
                    sp_client = None
                    storage_mode = "local"
                else:
                    from src.sharepoint_list_client import SharePointListClient
                    from src.sharepoint_client import SharePointClient
                    list_client = SharePointListClient()
                    sp_client = SharePointClient()
                    storage_mode = "sharepoint"

                result = process_image(
                    file_bytes=file_bytes,
                    original_filename=filename,
                    generator=gen,
                    list_client=list_client,
                    sp_client=sp_client,
                    image_folder=Config.IMAGE_FOLDER,
                    storage_mode=storage_mode,
                    on_progress=lambda msg: q.put(("progress", msg)),
                    category=category,
                    source_context=source_context or None,
                    source=source or None,
                    attribution=photographer,
                    source_url=download_url,
                )
                q.put(("done", result))
            except Exception:
                q.put(("error", "Processing failed"))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        yield from _sse_poll_loop(t, q, 180, "Processing timed out after 3 minutes")

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/mcp/catalog-stock", methods=["POST"])
def api_mcp_catalog_stock():
    """Internal endpoint for Pete MCP server to catalog a stock image.
    Protected by X-MCP-Secret header instead of MSAL session auth.
    Returns JSON directly (no SSE streaming).
    """
    secret = Config.MCP_INTERNAL_SECRET
    if not secret or request.headers.get("X-MCP-Secret") != secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True) or {}
    download_url = data.get("download_url", "").strip()
    filename = data.get("filename", "image.jpg").strip() or "image.jpg"
    category = data.get("category", "").strip() or None
    source = data.get("source", "").strip()
    title = data.get("title", "").strip()
    tags_raw = data.get("tags", "")
    photographer = data.get("photographer", "").strip()
    license_info = data.get("license", "").strip()
    source_url = data.get("source_url", "").strip() or download_url
    source_asset_id = data.get("source_asset_id", "").strip()

    from src.image_processor import CATEGORIES, process_image

    if not download_url:
        return jsonify({"error": "download_url required"}), 400

    domain = urlparse(download_url).netloc.lower()
    _allowed = {"images.pexels.com", "www.pexels.com", "cdn.pixabay.com", "pixabay.com", "images.unsplash.com"}
    if not any(domain == d or domain.endswith("." + d) for d in _allowed):
        return jsonify({"error": "URL not permitted"}), 403

    if category is not None and category not in CATEGORIES:
        return jsonify({"error": f"Invalid category. Must be one of: {CATEGORIES}"}), 400

    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
    source_context = _stock_source_context(source, title, tags, photographer)

    try:
        file_bytes = _download_limited_bytes(download_url, timeout=30)
        _validate_image_payload(file_bytes, filename)
    except Exception as e:
        return jsonify({"error": f"Download failed: {e}"}), 502

    try:
        from src.ai_generator import AltTextGenerator
        gen = AltTextGenerator()

        if Config.TEST_MODE:
            list_client = LocalClient()
            sp_client = None
            storage_mode = "local"
        else:
            from src.sharepoint_list_client import SharePointListClient
            from src.sharepoint_client import SharePointClient
            list_client = SharePointListClient()
            sp_client = SharePointClient()
            storage_mode = "sharepoint"

        result = process_image(
            file_bytes=file_bytes,
            original_filename=filename,
            generator=gen,
            list_client=list_client,
            sp_client=sp_client,
            image_folder=Config.IMAGE_FOLDER,
            storage_mode=storage_mode,
            category=category,
            source_context=source_context or None,
            source=source or None,
            attribution=photographer,
            license_info=license_info,
            source_url=source_url,
            source_asset_id=source_asset_id,
        )
        return jsonify(result)
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@app.route("/api/mcp/catalog-from-file", methods=["POST"])
def api_mcp_catalog_from_file():
    """Internal endpoint for Pete MCP server to catalog a base64-encoded image file.
    Protected by X-MCP-Secret header instead of MSAL session auth.
    Returns JSON directly.
    """
    import base64 as _base64

    secret = Config.MCP_INTERNAL_SECRET
    if not secret or request.headers.get("X-MCP-Secret") != secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True) or {}
    image_data = data.get("image_data", "").strip()
    filename = data.get("filename", "image.jpg").strip() or "image.jpg"
    category = data.get("category", "").strip() or None

    from src.image_processor import CATEGORIES, process_image

    if not image_data:
        return jsonify({"error": "image_data required"}), 400

    if category is not None and category not in CATEGORIES:
        return jsonify({"error": f"Invalid category. Must be one of: {CATEGORIES}"}), 400

    try:
        file_bytes = _base64.b64decode(image_data, validate=True)
        _validate_image_payload(file_bytes, filename)
    except Exception:
        return jsonify({"error": "Invalid image data"}), 400

    try:
        from src.ai_generator import AltTextGenerator
        gen = AltTextGenerator()

        if Config.TEST_MODE:
            list_client = LocalClient()
            sp_client = None
            storage_mode = "local"
        else:
            from src.sharepoint_list_client import SharePointListClient
            from src.sharepoint_client import SharePointClient
            list_client = SharePointListClient()
            sp_client = SharePointClient()
            storage_mode = "sharepoint"

        result = process_image(
            file_bytes=file_bytes,
            original_filename=filename,
            generator=gen,
            list_client=list_client,
            sp_client=sp_client,
            image_folder=Config.IMAGE_FOLDER,
            storage_mode=storage_mode,
            category=category,
            source="Internal",
        )
        return jsonify(result)
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@app.route("/api/mcp/claude-article", methods=["POST"])
def api_mcp_claude_article():
    """Internal endpoint for Claude article automation.

    Accepts Claude output, extracts photo phrases, and optionally runs stock search.
    Protected by X-MCP-Secret header instead of MSAL session auth.
    """
    secret = Config.MCP_INTERNAL_SECRET
    if not secret or request.headers.get("X-MCP-Secret") != secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True) or {}

    article_title = str(data.get("article_title", "") or "").strip()
    article_body = str(data.get("article_body", "") or "").strip()
    raw_suggestions = data.get("photo_suggestions", [])
    include_search = bool(data.get("include_search", True))
    try:
        search_limit = max(1, min(int(data.get("search_limit", 8)), 25))
    except (TypeError, ValueError):
        return jsonify({"error": "search_limit must be an integer"}), 400

    if raw_suggestions and not isinstance(raw_suggestions, list):
        return jsonify({"error": "photo_suggestions must be an array of strings"}), 400

    phrases: list[str] = []
    if isinstance(raw_suggestions, list):
        phrases = [str(p).strip() for p in raw_suggestions if str(p).strip()]

    if not phrases:
        from src.stock_client import parse_photo_suggestions

        parse_source = article_body or str(data.get("content", "") or "").strip()
        if not parse_source:
            return jsonify({"error": "Provide article_body/content or photo_suggestions"}), 400
        phrases = parse_photo_suggestions(parse_source)

    phrases = phrases[:20]
    if not phrases:
        return jsonify({"error": "No usable photo phrases found"}), 400

    response_payload = {
        "article_title": article_title,
        "phrase_count": len(phrases),
        "phrases": phrases,
        "search_included": include_search,
        "search_limit": search_limit,
    }

    if include_search:
        from src.stock_client import search_all_libraries

        search_results = search_all_libraries(phrases, limit=search_limit, page=1)
        response_payload["results"] = search_results

    response_payload["next_actions"] = {
        "catalog_stock_endpoint": "/api/mcp/catalog-stock",
        "catalog_file_endpoint": "/api/mcp/catalog-from-file",
    }

    return jsonify(response_payload)


@app.route("/api/mcp/claude-article-auto", methods=["POST"])
def api_mcp_claude_article_auto():
    """Internal one-call automation endpoint for Claude article workflows.

    approval_mode:
      - manual: extract/search and return shortlisted candidates only
      - auto: extract/search + catalog candidates directly
    """
    secret = Config.MCP_INTERNAL_SECRET
    if not secret or request.headers.get("X-MCP-Secret") != secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True) or {}

    article_title = str(data.get("article_title", "") or "").strip()
    article_body = str(data.get("article_body", "") or "").strip()
    raw_suggestions = data.get("photo_suggestions", [])
    approval_mode = str(data.get("approval_mode", "manual") or "manual").strip().lower()
    preferred_libraries = data.get("preferred_libraries", ["pexels", "unsplash", "pixabay", "shutterstock"])

    if approval_mode not in {"manual", "auto"}:
        return jsonify({"error": "approval_mode must be manual or auto"}), 400
    if preferred_libraries and not isinstance(preferred_libraries, list):
        return jsonify({"error": "preferred_libraries must be an array"}), 400

    try:
        search_limit = max(1, min(int(data.get("search_limit", 8)), 25))
        max_catalog_items = max(1, min(int(data.get("max_catalog_items", 5)), 20))
    except (TypeError, ValueError):
        return jsonify({"error": "search_limit and max_catalog_items must be integers"}), 400

    category = str(data.get("category", "") or "").strip() or None
    from src.image_processor import CATEGORIES
    if category is not None and category not in CATEGORIES:
        return jsonify({"error": f"Invalid category. Must be one of: {CATEGORIES}"}), 400

    if raw_suggestions and not isinstance(raw_suggestions, list):
        return jsonify({"error": "photo_suggestions must be an array of strings"}), 400

    phrases: list[str] = []
    if isinstance(raw_suggestions, list):
        phrases = [str(p).strip() for p in raw_suggestions if str(p).strip()]

    if not phrases:
        from src.stock_client import parse_photo_suggestions

        parse_source = article_body or str(data.get("content", "") or "").strip()
        if not parse_source:
            return jsonify({"error": "Provide article_body/content or photo_suggestions"}), 400
        phrases = parse_photo_suggestions(parse_source)

    phrases = phrases[:20]
    if not phrases:
        return jsonify({"error": "No usable photo phrases found"}), 400

    from src.stock_client import search_all_libraries

    search_results = search_all_libraries(phrases, limit=search_limit, page=1)
    library_order = [str(x).strip().lower() for x in preferred_libraries if str(x).strip()]
    if not library_order:
        library_order = ["pexels", "unsplash", "pixabay", "shutterstock"]

    def _safe_filename_from(download_url: str, title: str, phrase: str) -> str:
        title_base = re.sub(r"[^a-zA-Z0-9\s-]+", "", title or "").strip()
        if not title_base:
            title_base = phrase or "image"
        title_base = re.sub(r"\s+", "-", title_base)[:60].strip("-") or "image"
        ext = ".jpg"
        match = re.search(r"\.(jpe?g|png|gif|webp)(?:\?|$)", download_url or "", re.IGNORECASE)
        if match:
            ext = "." + match.group(1).lower().replace("jpeg", "jpg")
        return f"{title_base}{ext}"

    shortlisted: list[dict] = []
    for group in search_results:
        phrase = str(group.get("phrase", "") or "").strip()
        if not phrase:
            continue

        # Collect the best image from each available library for this phrase
        phrase_images = []
        for lib in library_order:
            lib_data = group.get(lib) if isinstance(group, dict) else None
            if not isinstance(lib_data, dict):
                continue
            items = lib_data.get("results", [])
            if not isinstance(items, list) or not items:
                continue

            # Get the first valid image from this library
            for item in items:
                download_url = str(item.get("download_url", "") or "").strip()
                if not download_url:
                    # Shutterstock entries generally do not provide a direct download URL.
                    continue
                title = str(item.get("title", "") or "").strip()
                tags_val = item.get("tags", [])
                tags = [str(t).strip() for t in tags_val if str(t).strip()] if isinstance(tags_val, list) else []
                photographer = str(item.get("photographer", "") or "").strip()
                picked = {
                    "phrase": phrase,
                    "library": lib,
                    "download_url": download_url,
                    "title": title,
                    "tags": tags,
                    "photographer": photographer,
                    "filename": _safe_filename_from(download_url, title, phrase),
                }
                phrase_images.append(picked)
                break  # Only take first valid image from this library

        # Add all collected images to shortlist (provides library diversity)
        shortlisted.extend(phrase_images)

    payload = {
        "article_title": article_title,
        "approval_mode": approval_mode,
        "phrase_count": len(phrases),
        "phrases": phrases,
        "search_limit": search_limit,
        "shortlisted_count": len(shortlisted),
        "shortlisted": shortlisted,
    }

    if approval_mode == "manual":
        # Build a full preview URL so Claude can give the user a clickable link
        base_url = request.host_url.rstrip("/")
        preview_endpoint = "/api/mcp/preview"
        preview_url = f"{base_url}{preview_endpoint}"

        payload["next_actions"] = {
            "preview_endpoint": preview_endpoint,
            "preview_url": preview_url,
            "hint": (
                "DO NOT try to render the images inline — stock photo CDN URLs are blocked "
                "by Claude's sandbox CSP. Instead, give the user a clickable markdown link "
                f"to open the gallery in their browser: [Open Image Gallery]({preview_url}) "
                "and instruct them to POST preview_data to that URL, or call the endpoint "
                "yourself using the fetch/HTTP tool and return the resulting HTML URL."
            ),
        }
        # Include the full payload needed for preview rendering
        payload["preview_data"] = {
            "article_title": article_title,
            "phrases": phrases,
            "shortlisted": shortlisted,
        }
        return jsonify(payload)

    # approval_mode == auto: process shortlisted candidates immediately
    from src.image_processor import process_image
    from src.ai_generator import AltTextGenerator

    if Config.TEST_MODE:
        list_client = LocalClient()
        sp_client = None
        storage_mode = "local"
    else:
        from src.sharepoint_list_client import SharePointListClient
        from src.sharepoint_client import SharePointClient

        list_client = SharePointListClient()
        sp_client = SharePointClient()
        storage_mode = "sharepoint"

    generator = AltTextGenerator()
    cataloged = []
    failures = []

    for candidate in shortlisted[:max_catalog_items]:
        try:
            download_url = candidate["download_url"]
            domain = urlparse(download_url).netloc.lower()
            if not any(domain == d or domain.endswith("." + d) for d in _DOWNLOAD_ALLOWED_DOMAINS):
                raise ValueError("URL not permitted")

            file_bytes = _download_limited_bytes(download_url, timeout=30)
            _validate_image_payload(file_bytes, candidate["filename"])

            source_context = _stock_source_context(
                candidate.get("library", ""),
                candidate.get("title", ""),
                candidate.get("tags", []),
                candidate.get("photographer", ""),
            )

            result = process_image(
                file_bytes=file_bytes,
                original_filename=candidate["filename"],
                generator=generator,
                list_client=list_client,
                sp_client=sp_client,
                image_folder=Config.IMAGE_FOLDER,
                storage_mode=storage_mode,
                category=category,
                source_context=source_context or None,
                source=candidate.get("library", ""),
                attribution=candidate.get("photographer", ""),
                source_url=download_url,
            )
            cataloged.append({
                "phrase": candidate.get("phrase", ""),
                "library": candidate.get("library", ""),
                "download_url": download_url,
                "result": result,
            })
        except Exception as exc:
            failures.append({
                "phrase": candidate.get("phrase", ""),
                "library": candidate.get("library", ""),
                "download_url": candidate.get("download_url", ""),
                "error": str(exc),
            })

    payload["auto_catalog"] = {
        "requested_max_catalog_items": max_catalog_items,
        "processed_count": min(len(shortlisted), max_catalog_items),
        "cataloged_count": len(cataloged),
        "failure_count": len(failures),
        "cataloged": cataloged,
        "failures": failures,
    }
    return jsonify(payload)


@app.route("/api/mcp/stock-search", methods=["POST"])
def api_mcp_stock_search():
    """MCP endpoint: search stock libraries by phrase list.

    Same results format as /api/stock-search but authenticated via X-MCP-Secret
    so Claude.ai skills can call it directly without a browser session.
    """
    secret = Config.MCP_INTERNAL_SECRET
    if not secret or request.headers.get("X-MCP-Secret") != secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True) or {}
    phrases = data.get("phrases", [])
    if not isinstance(phrases, list):
        return jsonify({"error": "phrases must be an array of strings"}), 400
    phrases = [str(p).strip() for p in phrases if str(p).strip()][:20]
    if not phrases:
        return jsonify({"error": "At least one phrase required"}), 400

    try:
        limit = max(1, min(int(data.get("limit", 8)), 25))
        page = max(1, min(int(data.get("page", 1)), 100))
    except (TypeError, ValueError):
        return jsonify({"error": "limit and page must be integers"}), 400

    from src.stock_client import search_all_libraries
    results = search_all_libraries(phrases, limit=limit, page=page)
    return jsonify({"results": results, "page": page, "limit": limit})


@app.route("/api/mcp/preview", methods=["POST", "GET"])
def api_mcp_preview():
    """Interactive preview endpoint for Claude article workflows.
    
    Takes shortlisted results and renders an interactive gallery for
    selection/approval before cataloging to SharePoint.
    
    This endpoint serves HTML (not JSON) to display the preview UI
    in a browser or Claude.ai rendering context.
    
    Accepts both POST (JSON body) and GET (query params) for flexibility.
    """
    import base64
    
    secret = Config.MCP_INTERNAL_SECRET
    provided = (
        request.headers.get("X-MCP-Secret")
        or request.args.get("secret")
        or request.form.get("secret")
    )
    if not secret or provided != secret:
        return jsonify({"error": "Unauthorized"}), 401
    
    # Support both POST (JSON) and GET (query params or form data)
    if request.method == "POST":
        data = request.get_json(force=True) or {}
    else:
        # GET request: try to decode from query param or form data
        encoded_data = request.args.get("data") or request.form.get("data")
        if encoded_data:
            try:
                data = json.loads(base64.b64decode(encoded_data).decode())
            except Exception:
                data = {}
        else:
            # Fall back to individual query parameters
            data = {
                "article_title": request.args.get("article_title", "Preview Gallery"),
                "shortlisted": json.loads(request.args.get("shortlisted", "[]")),
                "phrases": json.loads(request.args.get("phrases", "[]")),
            }
    
    # Extract preview parameters from request
    article_title = str(data.get("article_title", "Preview Gallery") or "Preview Gallery").strip()
    shortlisted = data.get("shortlisted", [])
    phrases = data.get("phrases", [])
    
    if not isinstance(shortlisted, list):
        return jsonify({"error": "shortlisted must be an array"}), 400
    
    if not shortlisted:
        return render_template("mcp_preview.html", 
                             article_title=article_title,
                             images=[],
                             phrases=[],
                             empty=True)
    
    # Prepare images for gallery display
    images = []
    for idx, item in enumerate(shortlisted):
        if not isinstance(item, dict):
            continue
        
        download_url = str(item.get("download_url", "")).strip()
        if not download_url:
            continue
        
        images.append({
            "id": f"img_{idx}",
            "index": idx,
            "download_url": download_url,
            "title": str(item.get("title", "") or "").strip() or "Untitled",
            "filename": str(item.get("filename", "") or "").strip() or "image.jpg",
            "library": str(item.get("library", "") or "").strip() or "stock",
            "photographer": str(item.get("photographer", "") or "").strip(),
            "phrase": str(item.get("phrase", "") or "").strip(),
            "tags": item.get("tags", []) if isinstance(item.get("tags", []), list) else [],
        })
    
    return render_template("mcp_preview.html",
                         article_title=article_title,
                         images=images,
                         phrases=phrases,
                         empty=len(images) == 0)


# In-memory session store: token → {shortlist, article_title, phrases, selection, created_at}
_preview_sessions: dict = {}
_preview_sessions_lock = threading.Lock()
_PREVIEW_SESSION_TTL = 3600  # 1 hour


def _preview_session_cleanup():
    now = time.time()
    with _preview_sessions_lock:
        expired = [k for k, v in _preview_sessions.items() if now - v["created_at"] > _PREVIEW_SESSION_TTL]
        for k in expired:
            del _preview_sessions[k]


@app.route("/api/mcp/preview/session", methods=["POST"])
def api_mcp_preview_session():
    """Create a preview session. Returns a short token for the gallery URL."""
    secret = Config.MCP_INTERNAL_SECRET
    provided = request.headers.get("X-MCP-Secret") or request.args.get("secret")
    if not secret or provided != secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True) or {}
    shortlisted = data.get("shortlisted", [])
    if not isinstance(shortlisted, list) or not shortlisted:
        return jsonify({"error": "shortlisted required"}), 400

    _preview_session_cleanup()
    token = secrets.token_urlsafe(16)
    with _preview_sessions_lock:
        _preview_sessions[token] = {
            "article_title": str(data.get("article_title", "Stock Photo Selection") or ""),
            "phrases": data.get("phrases", []),
            "shortlisted": shortlisted,
            "selection": None,
            "cataloged": [],
            "failures": [],
            "cataloged_at": None,
            "created_at": time.time(),
        }
    return jsonify({"token": token})


@app.route("/api/mcp/preview/<token>", methods=["GET"])
def api_mcp_preview_token(token: str):
    """Render the selection gallery for a session token."""
    with _preview_sessions_lock:
        sess = _preview_sessions.get(token)
    if not sess:
        return "Preview session not found or expired.", 404

    shortlisted = sess["shortlisted"]
    images = []
    for idx, item in enumerate(shortlisted):
        if not isinstance(item, dict) or not item.get("download_url"):
            continue
        images.append({
            "id": f"img_{idx}",
            "index": idx,
            "download_url": item.get("download_url", ""),
            "title": str(item.get("title", "") or "").strip() or "Untitled",
            "filename": str(item.get("filename", "") or "").strip() or "image.jpg",
            "library": str(item.get("library", "") or "").strip() or "stock",
            "photographer": str(item.get("photographer", "") or "").strip(),
            "phrase": str(item.get("phrase", "") or "").strip(),
            "tags": item.get("tags", []) if isinstance(item.get("tags", []), list) else [],
            "type": str(item.get("type", "") or "").strip(),
            "slug": str(item.get("slug", "") or "").strip(),
            "location": str(item.get("location", "") or "").strip(),
        })

    return render_template(
        "mcp_preview.html",
        article_title=sess["article_title"],
        images=images,
        phrases=sess["phrases"],
        empty=len(images) == 0,
        session_token=token,
    )


@app.route("/api/mcp/preview/<token>/select", methods=["POST"])
def api_mcp_preview_select(token: str):
    """Store a selection and immediately catalog selected stock images."""
    with _preview_sessions_lock:
        sess = _preview_sessions.get(token)
    if not sess:
        return jsonify({"error": "Session not found or expired"}), 404

    data = request.get_json(force=True) or {}
    selected = data.get("selected", [])
    if not isinstance(selected, list):
        return jsonify({"error": "selected must be an array"}), 400

    internal_items = [
        item for item in selected
        if isinstance(item, dict) and (item.get("type") == "internal" or item.get("library") == "internal")
    ]
    stock_items = [
        item for item in selected
        if isinstance(item, dict) and not (item.get("type") == "internal" or item.get("library") == "internal")
    ]

    from src.image_processor import CATEGORIES, process_image

    cataloged: list[dict] = []
    failures: list[dict] = []

    for item in stock_items:
        download_url = str(item.get("download_url", "") or "").strip()
        filename = str(item.get("filename", "image.jpg") or "image.jpg").strip() or "image.jpg"
        category = str(item.get("category", "") or "").strip() or None
        source = str(item.get("library", "") or "").strip()
        title = str(item.get("title", "") or "").strip()
        photographer = str(item.get("photographer", "") or "").strip()
        tags_in = item.get("tags", [])

        if not download_url:
            failures.append({"title": title or "Untitled", "error": "download_url required"})
            continue

        domain = urlparse(download_url).netloc.lower()
        _allowed = {
            "images.pexels.com",
            "www.pexels.com",
            "cdn.pixabay.com",
            "pixabay.com",
            "images.unsplash.com",
        }
        if not any(domain == d or domain.endswith("." + d) for d in _allowed):
            failures.append({"title": title or "Untitled", "error": "URL not permitted"})
            continue

        if category is not None and category not in CATEGORIES:
            failures.append({
                "title": title or "Untitled",
                "error": f"Invalid category. Must be one of: {CATEGORIES}",
            })
            continue

        if isinstance(tags_in, list):
            tags = [str(t).strip() for t in tags_in if str(t).strip()]
        else:
            tags = [t.strip() for t in str(tags_in or "").split(",") if t.strip()]
        source_context = _stock_source_context(source, title, tags, photographer)

        try:
            file_bytes = _download_limited_bytes(download_url, timeout=30)
            _validate_image_payload(file_bytes, filename)

            from src.ai_generator import AltTextGenerator

            gen = AltTextGenerator()
            if Config.TEST_MODE:
                list_client = LocalClient()
                sp_client = None
                storage_mode = "local"
            else:
                from src.sharepoint_list_client import SharePointListClient
                from src.sharepoint_client import SharePointClient

                list_client = SharePointListClient()
                sp_client = SharePointClient()
                storage_mode = "sharepoint"

            result = process_image(
                file_bytes=file_bytes,
                original_filename=filename,
                generator=gen,
                list_client=list_client,
                sp_client=sp_client,
                image_folder=Config.IMAGE_FOLDER,
                storage_mode=storage_mode,
                category=category,
                source_context=source_context or None,
                source=source or None,
                attribution=photographer,
                source_url=download_url,
            )
            cataloged.append({
                "title": title or "Untitled",
                "slug": result.get("slug", ""),
                "location": result.get("location", ""),
                "status": result.get("status", ""),
            })
        except Exception as e:
            failures.append({"title": title or "Untitled", "error": str(e) or "Processing failed"})

    with _preview_sessions_lock:
        if token in _preview_sessions:
            _preview_sessions[token]["selection"] = selected
            _preview_sessions[token]["selected_at"] = time.time()
            _preview_sessions[token]["cataloged"] = cataloged
            _preview_sessions[token]["failures"] = failures
            _preview_sessions[token]["cataloged_at"] = time.time()

    return jsonify({
        "ok": True,
        "count": len(selected),
        "internal_count": len(internal_items),
        "stock_count": len(stock_items),
        "cataloged_count": len(cataloged),
        "failure_count": len(failures),
        "cataloged": cataloged,
        "failures": failures,
    })


@app.route("/api/mcp/preview/<token>/selection", methods=["GET"])
def api_mcp_preview_get_selection(token: str):
    """Retrieve the stored selection for a session token (called by MCP tool)."""
    secret = Config.MCP_INTERNAL_SECRET
    provided = request.headers.get("X-MCP-Secret") or request.args.get("secret")
    if not secret or provided != secret:
        return jsonify({"error": "Unauthorized"}), 401

    with _preview_sessions_lock:
        sess = _preview_sessions.get(token)
    if not sess:
        return jsonify({"error": "Session not found or expired"}), 404

    return jsonify({
        "article_title": sess["article_title"],
        "selected": sess.get("selection"),
        "ready": sess.get("selection") is not None,
        "cataloged": sess.get("cataloged", []),
        "failures": sess.get("failures", []),
        "cataloged_at": sess.get("cataloged_at"),
    })


@app.route("/api/mcp/preview/<token>/image")
def api_mcp_preview_image(token: str):
    """Serve an internal library image for a valid preview session (no MSAL required).

    Used by mcp_preview.html so the gallery can display internal images without
    requiring the viewer to have an MSAL session cookie.
    """
    with _preview_sessions_lock:
        sess = _preview_sessions.get(token)
    if not sess:
        return Response("Session not found or expired", status=404)

    location = unquote(request.args.get("path", "")).strip()
    if not location:
        return Response("Missing path", status=400)

    # Validate the requested location is actually in this session's shortlist
    allowed = {
        str(item.get("location", "")).strip()
        for item in sess.get("shortlisted", [])
        if item.get("type") == "internal"
    }
    if location not in allowed:
        return Response("Not authorized for this path", status=403)

    if Config.STORAGE_MODE == "sharepoint":
        try:
            root = Config.SHAREPOINT_IMAGE_FOLDER
            sp_path = f"{root}/WebP/{location}" if root else f"WebP/{location}"
            url = _get_sp_url(sp_path, thumb=True)
            return redirect(url)
        except Exception:
            return Response("Internal server error", status=500)

    # Local mode — serve directly
    image_folder = Path(Config.IMAGE_FOLDER).resolve()
    full_path = None
    for candidate in [image_folder / "WebP" / location, image_folder / location]:
        resolved = candidate.resolve()
        if str(resolved).startswith(str(image_folder)) and resolved.exists():
            full_path = resolved
            break

    if full_path is None:
        return Response("Image not found", status=404)

    try:
        img = PILImage.open(full_path)
        img.thumbnail((240, 240))
        buf = BytesIO()
        img.save(buf, format="WEBP")
        return Response(buf.getvalue(), mimetype="image/webp",
                        headers={"Cache-Control": "private, max-age=3600"})
    except Exception:
        return Response("Internal server error", status=500)


@app.route("/api/download-image")
@login_required
def api_download_image():
    """Proxy-download a stock image so the browser gets a file attachment."""
    url = request.args.get("url", "").strip()
    filename = request.args.get("filename", "image.jpg").strip() or "image.jpg"
    download_location = request.args.get("dl", "").strip()  # Unsplash attribution ping

    if not url:
        return jsonify({"error": "url required"}), 400

    # Restrict to known stock-photo CDN domains only
    domain = urlparse(url).netloc.lower()
    if not any(domain == d or domain.endswith("." + d) for d in _DOWNLOAD_ALLOWED_DOMAINS):
        return jsonify({"error": "URL not permitted"}), 403

    # Unsplash attribution: fire-and-forget ping to their download endpoint
    if download_location:
        dl_domain = urlparse(download_location).netloc.lower()
        if "unsplash.com" in dl_domain:
            access_key = Config.UNSPLASH_ACCESS_KEY
            if access_key:
                try:
                    requests.get(
                        download_location,
                        headers={"Authorization": f"Client-ID {access_key}"},
                        timeout=5,
                    )
                except Exception:
                    pass

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    return Response(
        resp.content,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": content_type,
        },
    )


@app.route("/run/start-server")
@login_required
def run_start_server():
    """Return a simple redirect instruction — server is already running."""
    return jsonify({"url": "/library"})


@app.route("/run/stop", methods=["POST"])
@login_required
def run_stop():
    """Shut the server down gracefully after sending the response."""
    import threading
    import os
    import signal
    def _shutdown():
        import time
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_shutdown, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/folders")
@login_required
def api_folders():
    records = get_all_records()
    folder_counts: dict = {}
    for rec in records:
        location = rec.get("fields", {}).get("Location", "")
        folder = str(PurePosixPath(location).parent) if location else "."
        folder_counts[folder] = folder_counts.get(folder, 0) + 1
    folders = [
        {"name": f, "count": c}
        for f, c in sorted(folder_counts.items())
    ]
    return jsonify({"folders": folders})


@app.route("/api/tags")
@login_required
def api_tags():
    folder = request.args.get("folder", "")
    records = get_all_records()
    tag_counts: dict = {}
    for rec in records:
        fields = rec.get("fields", {})
        if folder:
            location = fields.get("Location", "")
            rec_folder = str(PurePosixPath(location).parent) if location else "."
            if rec_folder != folder:
                continue
        tags_str = fields.get("Tags", "")
        for tag in tags_str.split(","):
            tag = tag.strip()
            if tag:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
    tags = [{"name": t, "count": tag_counts[t]} for t in sorted(tag_counts, key=str.lower)]
    return jsonify({"tags": tags})


@app.route("/api/images")
@login_required
def api_images():
    folder = request.args.get("folder", "")
    selected = request.args.get("tags", "")
    selected_tags = {t.strip() for t in selected.split(",") if t.strip()}
    records = get_all_records()
    results = []
    for rec in records:
        fields = rec.get("fields", {})
        location = fields.get("Location", "")
        if folder:
            rec_folder = str(PurePosixPath(location).parent) if location else "."
            if rec_folder != folder:
                continue
        rec_tags = {t.strip() for t in fields.get("Tags", "").split(",") if t.strip()}
        status = fields.get("Status", "")
        status_filter = request.args.get("status", "")
        if status_filter and status != status_filter:
            continue
        if not selected_tags or (rec_tags & selected_tags):
            results.append(
                {
                    "id": rec["id"],
                    "filename": fields.get("Filename", ""),
                    "location": location,
                    "alt_text": fields.get("Alt Text", ""),
                    "tags": fields.get("Tags", ""),
                    "status": status,
                    "source": fields.get("Source", ""),
                }
            )
    return jsonify({"images": results})


@app.route("/api/pending-count")
@login_required
def api_pending_count():
    records = get_all_records()
    count = sum(1 for r in records if r.get("fields", {}).get("Status") == "pending-review")
    return jsonify({"count": count})


@app.route("/api/image-status", methods=["PATCH"])
@login_required
def api_image_status():
    data = request.get_json(force=True) or {}
    record_id = data.get("id", "").strip()
    status = data.get("status", "").strip()
    alt_text = data.get("alt_text")  # optional

    valid_statuses = {"pending-review", "approved", "rejected", "archived"}
    if not record_id:
        return jsonify({"error": "id required"}), 400
    if status not in valid_statuses:
        return jsonify({"error": f"status must be one of {sorted(valid_statuses)}"}), 400

    fields: dict = {"Status": status}
    if alt_text is not None:
        fields["Alt Text"] = alt_text.strip()

    if Config.TEST_MODE:
        list_client = LocalClient()
    else:
        from src.sharepoint_list_client import SharePointListClient
        list_client = SharePointListClient()

    # Strip ?-suggested tags from records being rejected — they'll never be cataloged
    if status == "rejected":
        records = list_client.get_all_records()
        for rec in records:
            if rec.get("id") == record_id or rec.get("fields", {}).get("id") == record_id:
                existing_tags = rec.get("fields", {}).get("Tags", "")
                cleaned = ", ".join(
                    t.strip() for t in existing_tags.split(",")
                    if t.strip() and not t.strip().startswith("?")
                )
                if cleaned != existing_tags:
                    fields["Tags"] = cleaned
                break

    ok = list_client.patch_fields(record_id, fields)

    if ok:
        global _records_cache
        _records_cache = None  # invalidate so pending-count reflects the change
        return jsonify({"ok": True})
    return jsonify({"error": "Record not found or update failed"}), 404


@app.route("/api/image/focal-point", methods=["PATCH"])
@login_required
def api_image_focal_point():
    """Let an authenticated editor revise normalized crop focal coordinates."""
    data = request.get_json(force=True) or {}
    record_id = str(data.get("id", "")).strip()
    try:
        x = float(data.get("x"))
        y = float(data.get("y"))
    except (TypeError, ValueError):
        return jsonify({"error": "x and y must be numbers between 0 and 1"}), 400
    if not record_id:
        return jsonify({"error": "id required"}), 400
    if not 0 <= x <= 1 or not 0 <= y <= 1:
        return jsonify({"error": "x and y must each be between 0 and 1"}), 400

    record = next(
        (item for item in get_all_records() if str(item.get("id", "")) == record_id),
        None,
    )
    if not record:
        return jsonify({"error": "Record not found"}), 404
    slug = str(record.get("fields", {}).get("Slug", "")).strip()
    if not slug:
        return jsonify({"error": "Record has no slug"}), 409

    from src.image_metadata import update_focal_point
    try:
        metadata = update_focal_point(
            slug,
            x,
            y,
            storage_mode=Config.STORAGE_MODE,
            image_folder=Config.IMAGE_FOLDER,
            sp_client=get_sp_client() if Config.STORAGE_MODE == "sharepoint" else None,
        )
    except FileNotFoundError:
        return jsonify({"error": "Asset metadata has not been created; backfill requires approval"}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "slug": slug, "focalPoint": metadata["focalPoint"]})


@app.route("/review")
@login_required
def review():
    return render_template("review.html", user=session.get("user", {}))


@app.route("/api/image-info")
@login_required
def api_image_info():
    location = unquote(request.args.get("path", ""))
    if not location:
        return jsonify({"error": "Missing path"}), 400

    if Config.STORAGE_MODE == "sharepoint":
        try:
            root = Config.SHAREPOINT_IMAGE_FOLDER
            webp_path = f"{root}/WebP/{location}" if root else f"WebP/{location}"
            meta = get_sp_client().get_file_metadata(webp_path)
            file_bytes = meta.get("size", 0)
            if file_bytes >= 1_048_576:
                file_size = f"{file_bytes / 1_048_576:.1f} MB"
            elif file_bytes >= 1024:
                file_size = f"{file_bytes / 1024:.1f} KB"
            else:
                file_size = f"{file_bytes} B"
            image_facet = meta.get("image", {})
            width = image_facet.get("width", 0)
            height = image_facet.get("height", 0)
            return jsonify({"width": width, "height": height, "file_size": file_size})
        except Exception:
            return jsonify({"error": "Internal server error"}), 500

    image_folder = Path(Config.IMAGE_FOLDER).resolve()
    full_path = (image_folder / location).resolve()

    if not str(full_path).startswith(str(image_folder)):
        return jsonify({"error": "Forbidden"}), 403

    if not full_path.exists():
        return jsonify({"error": "Not found"}), 404

    try:
        file_bytes = full_path.stat().st_size
        if file_bytes >= 1_048_576:
            file_size = f"{file_bytes / 1_048_576:.1f} MB"
        elif file_bytes >= 1024:
            file_size = f"{file_bytes / 1024:.1f} KB"
        else:
            file_size = f"{file_bytes} B"

        with PILImage.open(full_path) as img:
            width, height = img.size

        return jsonify({"width": width, "height": height, "file_size": file_size})
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@app.route("/api/image/delete", methods=["POST"])
@login_required
def api_image_delete():
    """Delete a single image record and its associated files."""
    if not _is_maintenance_admin_user(session.get("user", {})):
        return jsonify({"error": "Admin access required"}), 403

    data = request.get_json(force=True) or {}
    record_id = str(data.get("record_id", "")).strip()
    delete_files = _as_bool(data.get("delete_files", True))

    if not record_id:
        return jsonify({"error": "record_id is required"}), 400

    records = _records_snapshot(use_cache=False)
    target = next((r for r in records if str(r.get("id", "")).strip() == record_id), None)
    if not target:
        return jsonify({"error": "Record not found"}), 404

    fields = target.get("fields", {})

    if delete_files:
        if Config.STORAGE_MODE == "sharepoint":
            sp_client = get_sp_client()
            for area, rel in [
                ("WebP", fields.get("Location", "")),
                ("High-Res", fields.get("High-Res Location", "")),
            ]:
                rel = str(rel or "").strip()
                if rel:
                    _delete_sharepoint_target(sp_client, area, rel)
        else:
            image_root = Path(Config.IMAGE_FOLDER).resolve()
            for area, rel in [
                ("WebP", fields.get("Location", "")),
                ("High-Res", fields.get("High-Res Location", "")),
            ]:
                rel = str(rel or "").strip()
                if rel:
                    _delete_local_target(image_root, area, rel)

    deleted = _bulk_delete_records(get_client(), [record_id])

    global _records_cache
    _records_cache = None

    if not deleted:
        return jsonify({"error": "Failed to delete record"}), 500

    return jsonify({"deleted": True, "record_id": record_id})


@app.route("/thumbnail")
@login_required
def thumbnail():
    return _serve_image(thumb=True)


@app.route("/image")
@login_required
def image():
    return _serve_image(thumb=False)


_sp_url_cache: dict[str, tuple[str, float]] = {}  # key → (url, expires_at)
_SP_URL_TTL = 2700  # 45 minutes (SharePoint CDN URLs expire ~1 hour)


def _get_sp_url(sp_path: str, thumb: bool) -> str:
    """Return a SharePoint CDN URL, using a short-lived cache to avoid repeated Graph API calls."""
    cache_key = f"{'thumb' if thumb else 'full'}:{sp_path}"
    cached = _sp_url_cache.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0]
    url = get_sp_client().get_thumbnail_url(sp_path) if thumb else get_sp_client().get_file_url(sp_path)
    if len(_sp_url_cache) >= 500:
        # Evict expired entries; if still full, drop the oldest 125
        now = time.time()
        expired = [k for k, (_, exp) in _sp_url_cache.items() if exp <= now]
        for k in expired:
            del _sp_url_cache[k]
        if len(_sp_url_cache) >= 500:
            oldest = sorted(_sp_url_cache, key=lambda k: _sp_url_cache[k][1])[:125]
            for k in oldest:
                del _sp_url_cache[k]
    _sp_url_cache[cache_key] = (url, time.time() + _SP_URL_TTL)
    return url


def _serve_image(thumb: bool) -> Response:
    location = unquote(request.args.get("path", ""))
    if not location:
        return Response("Missing path parameter", status=400)

    if Config.STORAGE_MODE == "sharepoint":
        try:
            root = Config.SHAREPOINT_IMAGE_FOLDER
            sp_path = f"{root}/WebP/{location}" if root else f"WebP/{location}"
            url = _get_sp_url(sp_path, thumb)
            return redirect(url)
        except Exception:
            return Response("Internal server error", status=500)

    image_folder = Path(Config.IMAGE_FOLDER).resolve()

    # Prefer WebP/ subdirectory (aligned with SharePoint convention); fall back to
    # direct path for legacy records that pre-date this convention.
    full_path = None
    for candidate in [image_folder / "WebP" / location, image_folder / location]:
        resolved = candidate.resolve()
        if str(resolved).startswith(str(image_folder)) and resolved.exists():
            full_path = resolved
            break

    if full_path is None:
        return Response("Image not found", status=404)

    try:
        img = PILImage.open(full_path)
        if thumb:
            img.thumbnail((240, 240))
        buf = BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        buf.seek(0)
        return Response(buf.read(), mimetype="image/jpeg")
    except Exception as e:
        return Response(f"Error reading image: {e}", status=500)


@app.route("/api/mcp/thumbnail")
def api_mcp_thumbnail():
    """Public thumbnail endpoint for MCP skill use.

    Authenticated by ?key=<MCP_INTERNAL_SECRET> so the skill can embed
    images as markdown ![](url) in Claude.ai chat without MSAL.
    """
    secret = Config.MCP_INTERNAL_SECRET
    key = request.args.get("key", "")
    if not secret or key != secret:
        return Response("Unauthorized", status=401)
    return _serve_image(thumb=True)


@app.route("/api/mcp/asset")
def api_mcp_asset():
    """Deliver an exact original, hero, or standard asset to MCP consumers."""
    secret = Config.MCP_INTERNAL_SECRET
    key = request.args.get("key", "")
    if not secret or key != secret:
        return Response("Unauthorized", status=401)

    area = request.args.get("area", "")
    if area not in {"High-Res", "Hero", "WebP"}:
        return Response("Invalid asset area", status=400)
    location = unquote(request.args.get("path", "")).strip()
    if not location or PurePosixPath(location).is_absolute() or ".." in PurePosixPath(location).parts:
        return Response("Invalid path", status=400)

    if Config.STORAGE_MODE == "sharepoint":
        try:
            root = (Config.SHAREPOINT_IMAGE_FOLDER or "").strip().strip("/")
            sp_path = f"{root}/{area}/{location}" if root else f"{area}/{location}"
            return redirect(_get_sp_url(sp_path, thumb=False))
        except Exception:
            return Response("Internal server error", status=500)

    image_root = Path(Config.IMAGE_FOLDER).resolve()
    full_path = (image_root / area / location).resolve()
    if not str(full_path).startswith(str(image_root)):
        return Response("Forbidden", status=403)
    if not full_path.is_file():
        return Response("Image not found", status=404)
    suffix = full_path.suffix.lower()
    mimetype = {
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
    }.get(suffix, "application/octet-stream")
    return Response(
        full_path.read_bytes(),
        mimetype=mimetype,
        headers={"Cache-Control": "private, max-age=3600"},
    )


# ------------------------------------------------------------------
# Tag library
# ------------------------------------------------------------------

@app.route("/tags")
@login_required
def tags_page():
    user = session.get("user", {})
    is_admin = _is_maintenance_admin_user(user)
    return render_template("tags.html", user=user, is_admin=is_admin)


@app.route("/tag-manager")
@login_required
def tag_manager():
    """Legacy route kept for backwards compatibility; Tag Manager now lives in Maintenance."""
    return redirect("/maintenance#tag-manager")


@app.route("/maintenance")
@login_required
def maintenance():
    user = session.get("user", {})
    is_admin = _is_maintenance_admin_user(user)
    return render_template("maintenance.html", user=user, is_admin=is_admin)


@app.route("/api/maintenance/hero-backfill/candidates")
@login_required
def api_maintenance_hero_backfill_candidates():
    """Return itemized, read-only candidates for selective derivative backfill."""
    status_filter = str(request.args.get("status", "approved") or "").strip().lower()
    records = _records_snapshot(use_cache=False)
    if status_filter:
        records = [
            record for record in records
            if str(record.get("fields", {}).get("Status", "") or "").strip().lower()
            == status_filter
        ]

    from src.catalog_inventory import build_inventory
    if Config.STORAGE_MODE == "sharepoint":
        storage_mode = "sharepoint"
        sp_client = get_sp_client()
    else:
        storage_mode = "local"
        sp_client = None
    report = build_inventory(
        records,
        storage_mode=storage_mode,
        image_folder=Config.IMAGE_FOLDER,
        sp_client=sp_client,
    )
    candidates = []
    for key in ("heroEligible", "standardOnly1601To2559", "storedOriginalOnly1600", "below1600"):
        for item in report[key]:
            if item.get("backfillNeeded"):
                candidates.append(item)
    candidates.sort(
        key=lambda item: (
            0 if int(item.get("width", 0) or 0) >= 2560 else 1,
            str(item.get("filename", "") or "").lower(),
        )
    )
    return jsonify({
        "dryRun": True,
        "statusFilter": status_filter,
        "scannedCount": len(records),
        "candidateCount": len(candidates),
        "heroEligibleCount": sum(1 for item in candidates if item.get("width", 0) >= 2560),
        "candidates": candidates,
    })


@app.route("/api/maintenance/hero-backfill/run", methods=["POST"])
@login_required
def api_maintenance_hero_backfill_run():
    """Stream progress while backfilling only explicitly selected record IDs."""
    data = request.get_json(force=True) or {}
    raw_ids = data.get("record_ids", [])
    if not isinstance(raw_ids, list):
        return jsonify({"error": "record_ids must be an array"}), 400
    record_ids = list(dict.fromkeys(str(value or "").strip() for value in raw_ids if str(value or "").strip()))
    try:
        expected_count = int(data.get("expected_count"))
    except (TypeError, ValueError):
        return jsonify({"error": "expected_count is required"}), 400
    if not record_ids:
        return jsonify({"error": "Select at least one record"}), 400
    if expected_count != len(record_ids):
        return jsonify({"error": "expected_count does not match selected records"}), 409
    max_batch = int(_maintenance_guardrails().get("max_batch_size", 500) or 500)
    if len(record_ids) > max_batch:
        return jsonify({"error": f"Selection exceeds max batch size of {max_batch}"}), 400

    record_map = {
        str(record.get("id", "")).strip(): record
        for record in _records_snapshot(use_cache=False)
    }
    missing_ids = [record_id for record_id in record_ids if record_id not in record_map]
    if missing_ids:
        return jsonify({"error": "One or more selected records no longer exist", "missing_ids": missing_ids}), 409
    targets = [record_map[record_id] for record_id in record_ids]

    def generate():
        from src.hero_backfill import backfill_record

        if Config.STORAGE_MODE == "sharepoint":
            storage_mode = "sharepoint"
            sp_client = get_sp_client()
        else:
            storage_mode = "local"
            sp_client = None
        total = len(targets)
        succeeded = 0
        failed = 0
        hero_created = 0
        yield f"data: {json.dumps({'type': 'start', 'total': total, 'processed': 0, 'succeeded': 0, 'failed': 0})}\n\n"

        for index, record in enumerate(targets, 1):
            fields = record.get("fields", {})
            filename = str(fields.get("Filename", "") or record.get("id", ""))
            try:
                result = backfill_record(
                    record,
                    storage_mode=storage_mode,
                    image_folder=Config.IMAGE_FOLDER,
                    sp_client=sp_client,
                )
                succeeded += 1
                hero_created += int(bool(result.get("heroCreated")))
                event = {
                    "type": "progress",
                    "status": "succeeded",
                    "processed": index,
                    "total": total,
                    "succeeded": succeeded,
                    "failed": failed,
                    "heroCreated": hero_created,
                    "filename": filename,
                    "result": result,
                }
            except Exception as exc:
                failed += 1
                event = {
                    "type": "progress",
                    "status": "failed",
                    "processed": index,
                    "total": total,
                    "succeeded": succeeded,
                    "failed": failed,
                    "heroCreated": hero_created,
                    "filename": filename,
                    "error": str(exc) or "Backfill failed",
                }
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        result = {
            "type": "done",
            "processed": total,
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "heroCreated": hero_created,
        }
        _maintenance_append_audit("selective-hero-backfill", "ok" if failed == 0 else "partial", result)
        yield f"data: {json.dumps(result)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )




def _maintenance_actor() -> str:
    claims = session.get("user", {}) if isinstance(session.get("user", {}), dict) else {}
    identity_values = _user_identity_values(claims)
    if identity_values:
        return sorted(identity_values)[0]
    return "local-dev" if _auth_bypass_enabled() else "unknown"


def _maintenance_append_audit(action: str, outcome: str, details: Dict) -> None:
    with _MAINTENANCE_STATE_LOCK:
        state = _maintenance_load_state()
        audit_trail = state.get("audit_trail", [])
        audit_trail.append({
            "id": f"aud_{uuid.uuid4().hex[:10]}",
            "timestamp": _now_utc_iso(),
            "actor": _maintenance_actor(),
            "action": str(action or "").strip() or "unknown",
            "outcome": str(outcome or "").strip() or "ok",
            "details": details or {},
        })
        state["audit_trail"] = audit_trail[-500:]
        _maintenance_save_state(state)


def _create_maintenance_checkpoint(name: str, note: str = "", records: Optional[List[Dict]] = None) -> Dict:
    checkpoint_id = f"ckpt_{uuid.uuid4().hex[:10]}"
    created_at = _now_utc_iso()
    actor = _maintenance_actor()
    record_pool = list(records) if records is not None else _records_snapshot(use_cache=False)

    _MAINTENANCE_CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint_path = _MAINTENANCE_CHECKPOINT_DIR / f"{checkpoint_id}.json"
    payload = {
        "checkpoint_id": checkpoint_id,
        "created_at": created_at,
        "created_by": actor,
        "name": name,
        "note": note,
        "record_count": len(record_pool),
        "records": record_pool,
    }
    _atomic_json_write(checkpoint_path, payload)

    with _MAINTENANCE_STATE_LOCK:
        state = _maintenance_load_state()
        checkpoints = state.get("checkpoints", [])
        checkpoints.append({
            "checkpoint_id": checkpoint_id,
            "name": name,
            "note": note,
            "created_at": created_at,
            "created_by": actor,
            "record_count": len(record_pool),
            "path": str(checkpoint_path),
        })
        state["checkpoints"] = checkpoints[-100:]
        _maintenance_save_state(state)

    return {
        "checkpoint_id": checkpoint_id,
        "name": name,
        "note": note,
        "created_at": created_at,
        "created_by": actor,
        "record_count": len(record_pool),
    }


def _approval_error(action: str, message: str, status_code: int = 403):
    return jsonify({"error": message, "action": action}), status_code


def _consume_approved_token(action: str, record_ids: List[str], approval_token: str):
    now_epoch = time.time()
    rec_hash = _maintenance_record_hash(record_ids)

    with _MAINTENANCE_STATE_LOCK:
        state = _maintenance_load_state()
        approvals = state.get("approvals", [])
        target = None
        for item in approvals:
            if str(item.get("token", "")) != approval_token:
                continue
            target = item
            break

        if target is None:
            return _approval_error(action, "Invalid approval token", 404)
        if str(target.get("action", "")) != action:
            return _approval_error(action, "Approval token action mismatch", 409)
        if str(target.get("status", "")) != "approved":
            return _approval_error(action, "Approval token is not approved", 409)
        if float(target.get("expires_at_epoch", 0) or 0) <= now_epoch:
            return _approval_error(action, "Approval token has expired", 409)
        if str(target.get("record_hash", "")) != rec_hash:
            return _approval_error(action, "Approval token does not match selected records", 409)

        target["status"] = "consumed"
        target["consumed_at"] = _now_utc_iso()
        target["consumed_by"] = _maintenance_actor()
        state["approvals"] = approvals[-300:]
        _maintenance_save_state(state)

    return None


def _validate_destructive_guardrails(
    action: str,
    record_ids: List[str],
    expected_count: Optional[int],
    approval_token: str,
):
    guardrails = _maintenance_guardrails()
    count = len(record_ids)

    max_batch = int(guardrails.get("max_batch_size", 500) or 0)
    if max_batch > 0 and count > max_batch:
        return jsonify({
            "error": f"Batch exceeds guardrail max_batch_size={max_batch}",
            "action": action,
            "record_count": count,
        }), 400

    if guardrails.get("require_preview_for_destructive", True) and expected_count is None:
        return jsonify({
            "error": "expected_count is required by guardrails for destructive actions",
            "action": action,
        }), 400

    if expected_count is not None and expected_count != count:
        return jsonify({
            "error": "expected_count does not match target selection",
            "action": action,
            "expected_count": expected_count,
            "record_count": count,
        }), 409

    if guardrails.get("two_step_approval_required", False):
        token = str(approval_token or "").strip()
        if not token:
            return jsonify({
                "error": "approval_token is required by guardrails",
                "action": action,
            }), 403
        approval_error = _consume_approved_token(action, record_ids, token)
        if approval_error:
            return approval_error

    if guardrails.get("checkpoint_before_destructive", False):
        try:
            _create_maintenance_checkpoint(
                name=f"auto-{action}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}",
                note="Auto-checkpoint from guardrail before destructive run",
                records=_records_snapshot(use_cache=False),
            )
        except Exception:
            # Guardrail checkpoints are best-effort to avoid blocking critical cleanup.
            pass

    return None


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _records_snapshot(use_cache: bool = True) -> List[Dict]:
    """Return a single record snapshot for a request scope."""
    return get_all_records() if use_cache else get_client().get_all_records()


def _parse_record_ids(data: Dict, require_confirm_token: bool = False) -> tuple[Optional[List[str]], Optional[tuple]]:
    if require_confirm_token:
        confirm_token = str(data.get("confirm_token", "")).strip().upper()
        if confirm_token != "PURGE":
            return None, (jsonify({"error": "confirm_token must be PURGE"}), 400)

    raw_ids = data.get("record_ids", [])
    if not isinstance(raw_ids, list):
        return None, (jsonify({"error": "record_ids must be a list"}), 400)

    record_ids = [str(rid).strip() for rid in raw_ids if str(rid).strip()]
    record_ids = list(dict.fromkeys(record_ids))
    if not record_ids:
        return None, (jsonify({"error": "record_ids cannot be empty"}), 400)
    return record_ids, None


def _bulk_delete_records(client, record_ids: List[str]) -> int:
    if hasattr(client, "bulk_delete_records"):
        return int(client.bulk_delete_records(record_ids))
    return int(client.delete_records(record_ids))


def _bulk_patch_fields(client, patches: List[tuple[str, Dict]]) -> Dict:
    """Apply field patches in bulk and return a normalized result payload."""
    if not patches:
        return {"updated": 0, "failed_ids": [], "missing_ids": []}

    if hasattr(client, "bulk_patch_fields"):
        result = client.bulk_patch_fields(patches)
        if isinstance(result, dict):
            return {
                "updated": int(result.get("updated", 0)),
                "failed_ids": [str(v) for v in result.get("failed_ids", [])],
                "missing_ids": [str(v) for v in result.get("missing_ids", [])],
            }

    updated = 0
    failed_ids: list[str] = []
    for record_id, fields in patches:
        if client.patch_fields(record_id, fields):
            updated += 1
        else:
            failed_ids.append(record_id)
    return {"updated": updated, "failed_ids": failed_ids, "missing_ids": []}


def _safe_local_target(image_root: Path, area: str, relative_path: str) -> Optional[Path]:
    rel = _sanitize_relative_path(relative_path)
    if not rel:
        return None

    root = (image_root / area).resolve()
    target = (root / Path(*PurePosixPath(rel).parts)).resolve()
    try:
        if os.path.commonpath([str(root), str(target)]) != str(root):
            return None
    except ValueError:
        return None
    return target


def _delete_local_target(image_root: Path, area: str, relative_path: str) -> str:
    target = _safe_local_target(image_root, area, relative_path)
    if target is None:
        return "invalid"
    if not target.exists():
        return "missing"
    try:
        target.unlink()
        return "deleted"
    except OSError as exc:
        return f"error: {exc}"


def _delete_sharepoint_target(sp_client, area: str, relative_path: str) -> str:
    rel = _sanitize_relative_path(relative_path)
    if not rel:
        return "invalid"

    root = Config.SHAREPOINT_IMAGE_FOLDER.strip("/")
    sp_path = f"{area}/{rel}" if not root else f"{root}/{area}/{rel}"
    try:
        return "deleted" if sp_client.delete_file(sp_path) else "missing"
    except Exception as exc:
        return f"error: {exc}"


def _get_records_for_status(status: str, records: Optional[List[Dict]] = None) -> List[Dict]:
    record_pool = records if records is not None else _records_snapshot(use_cache=False)
    return [
        r
        for r in record_pool
        if str(r.get("fields", {}).get("Status", "")).strip() == status
    ]


def _list_local_images(folder: Path) -> set[str]:
    if not folder.exists():
        return set()
    formats = _normalized_formats()
    results: set[str] = set()
    for file_path in folder.rglob("*"):
        if file_path.is_file() and file_path.suffix.lower() in formats:
            results.add(file_path.relative_to(folder).as_posix())
    return results


def _collect_orphan_snapshot(limit: int = 200, records: Optional[List[Dict]] = None) -> Dict:
    record_pool = records if records is not None else _records_snapshot(use_cache=True)
    limit = max(10, min(limit, 1000))

    if Config.TEST_MODE:
        base = Path(Config.IMAGE_FOLDER)
        webp_paths = _list_local_images(base / "WebP")
        high_res_paths = _list_local_images(base / "High-Res")
        storage_mode = "local"
    else:
        root = Config.SHAREPOINT_IMAGE_FOLDER
        sp_client = get_sp_client()
        webp_paths = {
            str(PurePosixPath(rel))
            for _, rel in sp_client.list_all_images(f"{root}/WebP")
        }
        high_res_paths = {
            str(PurePosixPath(rel))
            for _, rel in sp_client.list_all_images(f"{root}/High-Res")
        }
        storage_mode = "sharepoint"

    referenced_webp: set[str] = set()
    referenced_high_res: set[str] = set()
    missing_file_records: list[Dict] = []
    missing_file_record_ids: list[str] = []

    for rec in record_pool:
        fields = rec.get("fields", {})
        loc = _sanitize_relative_path(fields.get("Location", ""))
        high_res_loc = _sanitize_relative_path(fields.get("High-Res Location", ""))

        if loc:
            referenced_webp.add(loc)
        if high_res_loc:
            referenced_high_res.add(high_res_loc)

        missing_webp = bool(loc) and loc not in webp_paths
        missing_high_res = bool(high_res_loc) and high_res_loc not in high_res_paths
        missing_both_refs = not loc and not high_res_loc
        if missing_webp or missing_high_res or missing_both_refs:
            rec_id = str(rec.get("id", "")).strip()
            if rec_id:
                missing_file_record_ids.append(rec_id)
            missing_file_records.append({
                "id": rec_id,
                "filename": fields.get("Filename", ""),
                "status": fields.get("Status", ""),
                "source": fields.get("Source", ""),
                "location": loc,
                "high_res_location": high_res_loc,
                "missing_webp": missing_webp or missing_both_refs,
                "missing_high_res": missing_high_res or missing_both_refs,
            })

    orphaned_webp_files = sorted(p for p in webp_paths if p not in referenced_webp)
    orphaned_high_res_files = sorted(p for p in high_res_paths if p not in referenced_high_res)
    missing_file_records_sorted = sorted(
        missing_file_records,
        key=lambda r: ((r.get("filename") or "").lower(), r.get("id") or ""),
    )
    missing_file_record_ids = [r.get("id", "") for r in missing_file_records_sorted if r.get("id", "")]

    return {
        "storage_mode": storage_mode,
        "record_count": len(record_pool),
        "webp_file_count": len(webp_paths),
        "high_res_file_count": len(high_res_paths),
        "missing_file_records_count": len(missing_file_records_sorted),
        "orphaned_webp_files_count": len(orphaned_webp_files),
        "orphaned_high_res_files_count": len(orphaned_high_res_files),
        "missing_file_records": missing_file_records_sorted[:limit],
        "missing_file_record_ids": missing_file_record_ids,
        "orphaned_webp_files": orphaned_webp_files[:limit],
        "orphaned_high_res_files": orphaned_high_res_files[:limit],
        "display_limit": limit,
        "truncated": {
            "missing_file_records": len(missing_file_records_sorted) > limit,
            "orphaned_webp_files": len(orphaned_webp_files) > limit,
            "orphaned_high_res_files": len(orphaned_high_res_files) > limit,
        },
    }


@app.route("/api/maintenance/purge-preview")
@login_required
def api_maintenance_purge_preview():
    status = request.args.get("status", "").strip()
    delete_files = _as_bool(request.args.get("delete_files", "false"))

    if status not in _MAINTENANCE_PURGE_STATUSES:
        return jsonify({
            "error": (
                "status must be one of "
                f"{sorted(_MAINTENANCE_PURGE_STATUSES)}"
            )
        }), 400

    records = _records_snapshot(use_cache=True)
    matches = _get_records_for_status(status, records=records)
    sample = []
    file_refs = 0

    for rec in matches:
        fields = rec.get("fields", {})
        if str(fields.get("Location", "")).strip():
            file_refs += 1
        if str(fields.get("High-Res Location", "")).strip():
            file_refs += 1

    for rec in matches[:20]:
        fields = rec.get("fields", {})
        sample.append({
            "id": rec.get("id", ""),
            "filename": fields.get("Filename", ""),
            "location": fields.get("Location", ""),
            "high_res_location": fields.get("High-Res Location", ""),
            "source": fields.get("Source", ""),
        })

    return jsonify({
        "status": status,
        "count": len(matches),
        "delete_files": delete_files,
        "estimated_file_refs": file_refs,
        "sample": sample,
    })


@app.route("/api/maintenance/purge-status", methods=["POST"])
@login_required
def api_maintenance_purge_status():
    data = request.get_json(force=True) or {}
    status = str(data.get("status", "")).strip()
    delete_files = _as_bool(data.get("delete_files", False))
    confirm_token = str(data.get("confirm_token", "")).strip().upper()
    approval_token = str(data.get("approval_token", "")).strip()
    expected_count_raw = data.get("expected_count")

    if status not in _MAINTENANCE_PURGE_STATUSES:
        return jsonify({
            "error": (
                "status must be one of "
                f"{sorted(_MAINTENANCE_PURGE_STATUSES)}"
            )
        }), 400
    if confirm_token != "PURGE":
        return jsonify({"error": "confirm_token must be PURGE"}), 400

    expected_count: Optional[int] = None
    if expected_count_raw is not None:
        try:
            expected_count = int(expected_count_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "expected_count must be an integer"}), 400

    records = _records_snapshot(use_cache=False)
    matches = _get_records_for_status(status, records=records)
    current_count = len(matches)
    if expected_count is not None and expected_count != current_count:
        return jsonify({
            "error": "Record count changed since preview. Refresh preview and retry.",
            "current_count": current_count,
            "expected_count": expected_count,
        }), 409

    files_deleted = 0
    files_missing = 0
    invalid_file_paths = 0
    file_delete_errors = 0
    file_delete_error_details: list[str] = []

    if delete_files and matches:
        if Config.TEST_MODE:
            image_root = Path(Config.IMAGE_FOLDER).resolve()
            for rec in matches:
                fields = rec.get("fields", {})
                for area, rel in [
                    ("WebP", fields.get("Location", "")),
                    ("High-Res", fields.get("High-Res Location", "")),
                ]:
                    rel = str(rel).strip()
                    if not rel:
                        continue
                    outcome = _delete_local_target(image_root, area, rel)
                    if outcome == "deleted":
                        files_deleted += 1
                    elif outcome == "missing":
                        files_missing += 1
                    elif outcome == "invalid":
                        invalid_file_paths += 1
                    else:
                        file_delete_errors += 1
                        if len(file_delete_error_details) < 20:
                            file_delete_error_details.append(
                                f"{area}/{rel} -> {outcome}"
                            )
        else:
            sp_client = get_sp_client()
            for rec in matches:
                fields = rec.get("fields", {})
                for area, rel in [
                    ("WebP", fields.get("Location", "")),
                    ("High-Res", fields.get("High-Res Location", "")),
                ]:
                    rel = str(rel).strip()
                    if not rel:
                        continue
                    outcome = _delete_sharepoint_target(sp_client, area, rel)
                    if outcome == "deleted":
                        files_deleted += 1
                    elif outcome == "missing":
                        files_missing += 1
                    elif outcome == "invalid":
                        invalid_file_paths += 1
                    else:
                        file_delete_errors += 1
                        if len(file_delete_error_details) < 20:
                            file_delete_error_details.append(
                                f"{area}/{rel} -> {outcome}"
                            )

    # For ingested records, also delete the original file from the ingest folder
    if delete_files and status == "ingested" and matches:
        if Config.STORAGE_MODE == "sharepoint":
            sp_ingest_folder = Config.SHAREPOINT_INGEST_FOLDER.strip() if Config.SHAREPOINT_INGEST_FOLDER else ""
            if sp_ingest_folder:
                from src.sharepoint_client import SharePointClient as _SPClient
                _sp = _SPClient()
                for rec in matches:
                    src_name = str(rec.get("fields", {}).get("Ingest Source", "") or "").strip()
                    if not src_name:
                        continue
                    sp_path = f"{sp_ingest_folder}/{src_name}"
                    try:
                        deleted = _sp.delete_file(sp_path)
                        if deleted:
                            files_deleted += 1
                        else:
                            files_missing += 1
                    except Exception as exc:
                        file_delete_errors += 1
                        if len(file_delete_error_details) < 20:
                            file_delete_error_details.append(f"ingest/{src_name} -> {exc}")
        else:
            ingest_folder = Config.LOCAL_INGEST_FOLDER.strip() if Config.LOCAL_INGEST_FOLDER else ""
            if ingest_folder:
                ingest_path = Path(ingest_folder)
                for rec in matches:
                    src_name = str(rec.get("fields", {}).get("Ingest Source", "") or "").strip()
                    if not src_name:
                        continue
                    candidate = ingest_path / src_name
                    try:
                        resolved = candidate.resolve()
                        if str(resolved).startswith(str(ingest_path.resolve())) and resolved.exists():
                            resolved.unlink()
                            files_deleted += 1
                        else:
                            files_missing += 1
                    except Exception as exc:
                        file_delete_errors += 1
                        if len(file_delete_error_details) < 20:
                            file_delete_error_details.append(f"ingest/{src_name} -> {exc}")

    record_ids = [str(r.get("id", "")).strip() for r in matches if str(r.get("id", "")).strip()]

    guardrail_error = _validate_destructive_guardrails(
        action="purge-status",
        record_ids=record_ids,
        expected_count=expected_count,
        approval_token=approval_token,
    )
    if guardrail_error:
        return guardrail_error

    deleted_records = _bulk_delete_records(get_client(), record_ids)

    global _records_cache
    _records_cache = None

    result = {
        "status": status,
        "matched_records": current_count,
        "deleted_records": deleted_records,
        "record_delete_failures": max(0, current_count - deleted_records),
        "delete_files": delete_files,
        "files_deleted": files_deleted,
        "files_missing": files_missing,
        "invalid_file_paths": invalid_file_paths,
        "file_delete_errors": file_delete_errors,
        "file_delete_error_details": file_delete_error_details,
    }

    _maintenance_append_audit(
        action="purge-status",
        outcome="ok",
        details={
            "status": status,
            "matched_records": current_count,
            "deleted_records": deleted_records,
            "delete_files": delete_files,
        },
    )

    return jsonify(result)


@app.route("/api/maintenance/orphans")
@login_required
def api_maintenance_orphans():
    limit_raw = request.args.get("limit", "200").strip()
    try:
        limit = int(limit_raw)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

    try:
        records = _records_snapshot(use_cache=True)
        return jsonify(_collect_orphan_snapshot(limit=limit, records=records))
    except Exception as exc:
        return jsonify({"error": f"Failed to scan for orphans: {exc}"}), 500


@app.route("/api/maintenance/orphans/delete-records", methods=["POST"])
@login_required
def api_maintenance_orphans_delete_records():
    data = request.get_json(force=True) or {}
    record_ids, error_response = _parse_record_ids(data, require_confirm_token=True)
    if error_response:
        return error_response

    expected_count_raw = data.get("expected_count")
    approval_token = str(data.get("approval_token", "")).strip()
    expected_count: Optional[int] = None
    if expected_count_raw is not None:
        try:
            expected_count = int(expected_count_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "expected_count must be an integer"}), 400

    guardrail_error = _validate_destructive_guardrails(
        action="orphans-delete-records",
        record_ids=record_ids,
        expected_count=expected_count,
        approval_token=approval_token,
    )
    if guardrail_error:
        return guardrail_error

    deleted = _bulk_delete_records(get_client(), record_ids)

    global _records_cache
    _records_cache = None

    result = {
        "requested": len(record_ids),
        "deleted": deleted,
        "failed": max(0, len(record_ids) - deleted),
    }

    _maintenance_append_audit(
        action="orphans-delete-records",
        outcome="ok",
        details=result,
    )

    return jsonify(result)


@app.route("/api/maintenance/orphans/flag-missing", methods=["POST"])
@login_required
def api_maintenance_orphans_flag_missing():
    data = request.get_json(force=True) or {}
    record_ids, error_response = _parse_record_ids(data, require_confirm_token=False)
    if error_response:
        return error_response

    marker = str(data.get("marker", "?missing-file")).strip() or "?missing-file"
    if not marker.startswith("?"):
        marker = f"?{marker}"
    set_pending_review = _as_bool(data.get("set_pending_review", True))

    client = get_client()
    records = _records_snapshot(use_cache=False)
    by_id = {str(r.get("id", "")).strip(): r for r in records if str(r.get("id", "")).strip()}

    patches: list[tuple[str, Dict]] = []
    unchanged = 0
    missing_ids: list[str] = []

    for record_id in record_ids:
        rec = by_id.get(record_id)
        if rec is None:
            missing_ids.append(record_id)
            continue

        fields = rec.get("fields", {})
        current_tags = str(fields.get("Tags", "") or "").strip()
        tag_parts = [t.strip() for t in current_tags.split(",") if t.strip()]
        patch: Dict = {}

        if marker not in tag_parts:
            tag_parts.append(marker)
        new_tags = ", ".join(tag_parts)
        if new_tags != current_tags:
            patch["Tags"] = new_tags

        if set_pending_review and str(fields.get("Status", "")).strip() != "pending-review":
            patch["Status"] = "pending-review"

        if not patch:
            unchanged += 1
            continue

        patches.append((record_id, patch))

    patch_result = _bulk_patch_fields(client, patches)
    updated = patch_result["updated"]
    failed_ids = patch_result["failed_ids"]

    if updated > 0:
        global _records_cache
        _records_cache = None

    return jsonify({
        "requested": len(record_ids),
        "updated": updated,
        "unchanged": unchanged,
        "missing": len(missing_ids),
        "failed": len(failed_ids),
        "missing_ids": missing_ids,
        "failed_ids": failed_ids,
        "marker": marker,
        "set_pending_review": set_pending_review,
    })


@app.route("/api/maintenance/orphans/delete-files", methods=["POST"])
@login_required
def api_maintenance_orphans_delete_files():
    data = request.get_json(force=True) or {}
    file_paths = data.get("file_paths", [])
    file_type = str(data.get("type", "")).strip().lower()

    if file_type not in ("webp", "highres"):
        return jsonify({"error": "type must be 'webp' or 'highres'"}), 400
    if not isinstance(file_paths, list) or not file_paths:
        return jsonify({"error": "file_paths must be a non-empty list"}), 400

    # Validate paths — reject any containing traversal sequences
    for p in file_paths:
        if not isinstance(p, str) or ".." in p or p.startswith("/"):
            return jsonify({"error": f"Invalid file path: {p!r}"}), 400

    subfolder = "WebP" if file_type == "webp" else "High-Res"
    deleted = 0
    failed = []

    if Config.TEST_MODE:
        base = Path(Config.IMAGE_FOLDER)
        for rel in file_paths:
            target = (base / subfolder / rel).resolve()
            # Safety: confirm resolved path is inside the expected folder
            allowed_base = (base / subfolder).resolve()
            try:
                target.relative_to(allowed_base)
            except ValueError:
                failed.append({"path": rel, "error": "path outside allowed folder"})
                continue
            if target.is_file():
                try:
                    target.unlink()
                    deleted += 1
                except Exception as exc:
                    failed.append({"path": rel, "error": str(exc)})
            else:
                failed.append({"path": rel, "error": "file not found"})
    else:
        sp_client = get_sp_client()
        root = Config.SHAREPOINT_IMAGE_FOLDER.strip("/")
        for rel in file_paths:
            sp_path = f"{root}/{subfolder}/{rel}" if root else f"{subfolder}/{rel}"
            try:
                ok = sp_client.delete_file(sp_path)
                if ok:
                    deleted += 1
                else:
                    failed.append({"path": rel, "error": "not found"})
            except Exception as exc:
                failed.append({"path": rel, "error": str(exc)})

    _maintenance_append_audit(
        action="orphans-delete-files",
        outcome="ok",
        details={"type": file_type, "deleted": deleted, "failed": len(failed)},
    )

    return jsonify({"deleted": deleted, "failed": len(failed), "failures": failed})


@app.route("/api/maintenance/orphans/register-files", methods=["POST"])
@login_required
def api_maintenance_orphans_register_files():
    data = request.get_json(force=True) or {}
    file_type = str(data.get("type", "")).strip().lower()
    file_paths = data.get("file_paths", [])

    if file_type not in ("webp", "highres"):
        return jsonify({"error": "type must be 'webp' or 'highres'"}), 400
    if not isinstance(file_paths, list) or not file_paths:
        return jsonify({"error": "file_paths must be a non-empty list"}), 400

    for p in file_paths:
        if not isinstance(p, str) or ".." in p or p.startswith("/"):
            return jsonify({"error": f"Invalid file path: {p!r}"}), 400

    client = get_client()
    registered = 0
    skipped = 0
    failed = []

    for rel_path in file_paths:
        filename = Path(rel_path).name
        try:
            if client.record_exists(filename):
                skipped += 1
                continue
            if file_type == "webp":
                client.create_record(filename=filename, location=rel_path, status="pending-review")
            else:
                client.create_record(filename=filename, high_res_location=rel_path, status="pending-review")
            registered += 1
        except Exception as exc:
            failed.append({"path": rel_path, "error": str(exc)})

    global _records_cache
    _records_cache = None

    _maintenance_append_audit(
        action="orphans-register-files",
        outcome="ok",
        details={"type": file_type, "registered": registered, "skipped": skipped, "failed": len(failed)},
    )

    return jsonify({"registered": registered, "skipped": skipped, "failed": len(failed), "failures": failed})


@app.route("/api/maintenance/duplicates")
@login_required
def api_maintenance_duplicates():
    limit_raw = request.args.get("limit", "200").strip()
    near_threshold_raw = request.args.get("near_threshold", "0.92").strip()
    include_near_alt = _as_bool(request.args.get("include_near_alt", "true"))

    try:
        limit = int(limit_raw)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

    try:
        near_threshold = float(near_threshold_raw)
    except ValueError:
        return jsonify({"error": "near_threshold must be a float"}), 400

    try:
        records = _records_snapshot(use_cache=True)
        return jsonify(
            _collect_duplicate_snapshot_helper(
                records,
                limit=limit,
                include_near_alt=include_near_alt,
                near_threshold=near_threshold,
            )
        )
    except Exception as exc:
        return jsonify({"error": f"Failed to scan duplicates: {exc}"}), 500


@app.route("/api/maintenance/duplicates/resolve", methods=["POST"])
@login_required
def api_maintenance_duplicates_resolve():
    data = request.get_json(force=True) or {}
    action = str(data.get("action", "")).strip().lower()
    confirm_token = str(data.get("confirm_token", "")).strip().upper()
    approval_token = str(data.get("approval_token", "")).strip()
    keep_id = str(data.get("keep_id", "")).strip()
    expected_count_raw = data.get("expected_count")

    if action not in {"delete", "merge"}:
        return jsonify({"error": "action must be one of ['delete', 'merge']"}), 400
    if confirm_token != "PURGE":
        return jsonify({"error": "confirm_token must be PURGE"}), 400

    raw_ids = data.get("record_ids", [])
    if not isinstance(raw_ids, list):
        return jsonify({"error": "record_ids must be a list"}), 400
    record_ids = [str(rid).strip() for rid in raw_ids if str(rid).strip()]
    record_ids = list(dict.fromkeys(record_ids))
    if len(record_ids) < 2:
        return jsonify({"error": "record_ids must contain at least 2 items"}), 400

    if not keep_id:
        keep_id = record_ids[0]
    if keep_id not in record_ids:
        return jsonify({"error": "keep_id must be included in record_ids"}), 400

    expected_count: Optional[int] = None
    if expected_count_raw is not None:
        try:
            expected_count = int(expected_count_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "expected_count must be an integer"}), 400

    client = get_client()
    records = _records_snapshot(use_cache=False)
    by_id = {str(r.get("id", "")).strip(): r for r in records if str(r.get("id", "")).strip()}

    missing_ids = [rid for rid in record_ids if rid not in by_id]
    if missing_ids:
        return jsonify({
            "error": "Some records no longer exist. Refresh duplicates and retry.",
            "missing_ids": missing_ids,
        }), 409

    resolved_records = [by_id[rid] for rid in record_ids]
    if expected_count is not None and expected_count != len(resolved_records):
        return jsonify({
            "error": "Record count changed since duplicate scan. Refresh and retry.",
            "expected_count": expected_count,
            "current_count": len(resolved_records),
        }), 409

    delete_ids = [rid for rid in record_ids if rid != keep_id]
    if not delete_ids:
        return jsonify({"error": "Nothing to resolve. Need at least one duplicate to remove."}), 400

    guardrail_error = _validate_destructive_guardrails(
        action=f"duplicates-resolve:{action}",
        record_ids=record_ids,
        expected_count=expected_count,
        approval_token=approval_token,
    )
    if guardrail_error:
        return guardrail_error

    merged_patch: Dict = {}
    if action == "merge":
        keep_fields = by_id[keep_id].get("fields", {})
        all_fields = [r.get("fields", {}) for r in resolved_records]

        # Merge tags while preserving first-seen order.
        merged_tags = []
        merged_tag_keys = set()
        for fields in all_fields:
            for tag in str(fields.get("Tags", "") or "").split(","):
                cleaned = tag.strip()
                if not cleaned:
                    continue
                key = cleaned.lower()
                if key in merged_tag_keys:
                    continue
                merged_tag_keys.add(key)
                merged_tags.append(cleaned)
        new_tags = ", ".join(merged_tags)
        if new_tags and new_tags != str(keep_fields.get("Tags", "") or "").strip():
            merged_patch["Tags"] = new_tags

        # Keep the most descriptive alt text from the duplicate set.
        alt_candidates = [str(fields.get("Alt Text", "") or "").strip() for fields in all_fields]
        alt_candidates = [a for a in alt_candidates if a]
        best_alt = max(alt_candidates, key=len) if alt_candidates else ""
        if best_alt and best_alt != str(keep_fields.get("Alt Text", "") or "").strip():
            merged_patch["Alt Text"] = best_alt

        # Fill missing fields on the keeper from other records.
        for field_name in ["Slug", "Location", "High-Res Location", "Source"]:
            keep_val = str(keep_fields.get(field_name, "") or "").strip()
            if keep_val:
                continue
            for fields in all_fields:
                candidate = str(fields.get(field_name, "") or "").strip()
                if candidate:
                    merged_patch[field_name] = candidate
                    break

        if merged_patch and not client.patch_fields(keep_id, merged_patch):
            return jsonify({"error": f"Failed to update keeper record {keep_id}"}), 500

    deleted = _bulk_delete_records(client, delete_ids)

    global _records_cache
    _records_cache = None

    result = {
        "action": action,
        "keep_id": keep_id,
        "requested_delete": len(delete_ids),
        "deleted": deleted,
        "failed": max(0, len(delete_ids) - deleted),
        "merged_patch": merged_patch,
    }

    _maintenance_append_audit(
        action=f"duplicates-resolve:{action}",
        outcome="ok",
        details={
            "group_size": len(record_ids),
            "requested_delete": len(delete_ids),
            "deleted": deleted,
            "keep_id": keep_id,
        },
    )

    return jsonify(result)


@app.route("/api/maintenance/duplicates/resolve-all", methods=["POST"])
@login_required
def api_maintenance_duplicates_resolve_all():
    """Bulk resolve all current duplicate groups in one operation.

    For each group the keeper is chosen automatically: the record with a non-empty
    Location (WebP record) is preferred; otherwise the first record in the group.
    """
    data = request.get_json(force=True) or {}
    action = str(data.get("action", "merge")).strip().lower()
    confirm_token = str(data.get("confirm_token", "")).strip().upper()
    approval_token = str(data.get("approval_token", "")).strip()
    limit_raw = data.get("limit", 1000)

    if action not in {"delete", "merge"}:
        return jsonify({"error": "action must be one of ['delete', 'merge']"}), 400
    if confirm_token != "PURGE":
        return jsonify({"error": "confirm_token must be PURGE"}), 400

    try:
        limit = max(1, min(int(limit_raw), 2000))
    except (TypeError, ValueError):
        limit = 1000

    snapshot = _collect_duplicate_snapshot_helper(_records_snapshot(use_cache=True), limit=limit)
    groups = snapshot.get("groups", [])
    if not groups:
        return jsonify({"error": "No duplicate groups found. Run a scan first."}), 400

    all_record_ids = [r.get("id", "") for g in groups for r in g.get("records", [])]
    guardrail_error = _validate_destructive_guardrails(
        action="duplicates-resolve-all",
        record_ids=all_record_ids,
        expected_count=len(all_record_ids),
        approval_token=approval_token,
    )
    if guardrail_error:
        return guardrail_error

    client = get_client()
    records_snapshot = _records_snapshot(use_cache=False)
    by_id = {str(r.get("id", "")).strip(): r for r in records_snapshot if str(r.get("id", "")).strip()}

    groups_processed = 0
    groups_skipped = 0
    total_deleted = 0
    total_failed = 0

    for group in groups:
        members = group.get("records", [])
        if len(members) < 2:
            groups_skipped += 1
            continue

        keep = next((m for m in members if str(m.get("location") or "").strip()), members[0])
        keep_id = keep.get("id", "")
        delete_ids = [m.get("id", "") for m in members if m.get("id", "") != keep_id and m.get("id", "") in by_id]

        if not delete_ids:
            groups_skipped += 1
            continue

        if action == "merge":
            keep_fields = by_id.get(keep_id, {}).get("fields", {})
            all_fields = [by_id[mid].get("fields", {}) for mid in [keep_id] + delete_ids if mid in by_id]

            merged_patch: Dict = {}
            merged_tags: list = []
            merged_tag_keys: set = set()
            for fields in all_fields:
                for tag in str(fields.get("Tags", "") or "").split(","):
                    cleaned = tag.strip()
                    if not cleaned or cleaned.lower() in merged_tag_keys:
                        continue
                    merged_tag_keys.add(cleaned.lower())
                    merged_tags.append(cleaned)
            new_tags = ", ".join(merged_tags)
            if new_tags and new_tags != str(keep_fields.get("Tags", "") or "").strip():
                merged_patch["Tags"] = new_tags

            alt_candidates = [str(f.get("Alt Text", "") or "").strip() for f in all_fields]
            best_alt = max((a for a in alt_candidates if a), key=len, default="")
            if best_alt and best_alt != str(keep_fields.get("Alt Text", "") or "").strip():
                merged_patch["Alt Text"] = best_alt

            for field_name in ["Slug", "Location", "High-Res Location", "Source"]:
                if str(keep_fields.get(field_name, "") or "").strip():
                    continue
                for fields in all_fields:
                    candidate = str(fields.get(field_name, "") or "").strip()
                    if candidate:
                        merged_patch[field_name] = candidate
                        break

            if merged_patch:
                client.patch_fields(keep_id, merged_patch)

        deleted = _bulk_delete_records(client, delete_ids)
        total_deleted += deleted
        total_failed += max(0, len(delete_ids) - deleted)
        groups_processed += 1

    global _records_cache
    _records_cache = None

    _maintenance_append_audit(
        action="duplicates-resolve-all",
        outcome="ok",
        details={
            "action": action,
            "groups_processed": groups_processed,
            "groups_skipped": groups_skipped,
            "total_deleted": total_deleted,
            "total_failed": total_failed,
        },
    )

    return jsonify({
        "action": action,
        "groups_processed": groups_processed,
        "groups_skipped": groups_skipped,
        "total_deleted": total_deleted,
        "total_failed": total_failed,
    })


def _record_matches_retag_filters(rec: Dict, category_filter: str, tag_filters: List[str], status_filter: str) -> bool:
    fields = rec.get("fields", {})

    if status_filter:
        rec_status = str(fields.get("Status", "")).strip().lower()
        if rec_status != status_filter.lower():
            return False

    location = _sanitize_relative_path(fields.get("Location", ""))
    parts = list(PurePosixPath(location).parts) if location else []
    if category_filter:
        desired = _normalize_filter_key(category_filter)
        part_keys = {_normalize_filter_key(p) for p in parts if p}
        if desired and desired not in part_keys:
            return False

    if tag_filters:
        rec_tags = {
            t.strip().lower()
            for t in str(fields.get("Tags", "") or "").split(",")
            if t.strip()
        }
        if not (rec_tags & set(tag_filters)):
            return False

    return True


def _get_retag_target_records(
    category_filter: str,
    tag_filter_raw: str,
    status_filter: str,
    max_records: int,
    records: Optional[List[Dict]] = None,
    missing_alt_only: bool = False,
    missing_tags_only: bool = False,
) -> List[Dict]:
    tag_filters = [
        t.strip().lower()
        for t in str(tag_filter_raw or "").split(",")
        if t.strip()
    ]

    record_pool = records if records is not None else _records_snapshot(use_cache=True)

    matches = [
        rec
        for rec in record_pool
        if _record_matches_retag_filters(rec, category_filter, tag_filters, status_filter)
    ]

    if missing_alt_only:
        matches = [r for r in matches if not str(r.get("fields", {}).get("Alt Text", "") or "").strip()]
    if missing_tags_only:
        matches = [r for r in matches if not str(r.get("fields", {}).get("Tags", "") or "").strip()]
    matches.sort(
        key=lambda r: (
            str(r.get("fields", {}).get("Filename", "") or "").lower(),
            str(r.get("id", "") or ""),
        )
    )
    return matches[:max_records]


def _status_reset_targets(
    category_filter: str,
    tag_filter_raw: str,
    current_status_filter: str,
    start_date_value: Optional[date],
    end_date_value: Optional[date],
    max_records: int,
    records: Optional[List[Dict]] = None,
) -> Dict:
    tag_filters = [
        t.strip().lower()
        for t in str(tag_filter_raw or "").split(",")
        if t.strip()
    ]

    matched: list[Dict] = []
    date_filtered_out = 0
    missing_date_for_filter = 0
    already_pending = 0

    record_pool = list(records) if records is not None else _records_snapshot(use_cache=True)
    record_pool.sort(
        key=lambda r: (
            str(r.get("fields", {}).get("Filename", "") or "").lower(),
            str(r.get("id", "") or ""),
        )
    )

    for rec in record_pool:
        if not _record_matches_retag_filters(rec, category_filter, tag_filters, current_status_filter):
            continue

        if start_date_value or end_date_value:
            rec_date = _record_date_value(rec)
            if rec_date is None:
                missing_date_for_filter += 1
                continue
            if start_date_value and rec_date < start_date_value:
                date_filtered_out += 1
                continue
            if end_date_value and rec_date > end_date_value:
                date_filtered_out += 1
                continue

        fields = rec.get("fields", {})
        if str(fields.get("Status", "") or "").strip() == "pending-review":
            already_pending += 1

        matched.append(rec)
        if len(matched) >= max_records:
            break

    return {
        "records": matched,
        "already_pending": already_pending,
        "date_filtered_out": date_filtered_out,
        "missing_date_for_filter": missing_date_for_filter,
    }


def _load_record_image_bytes(rec: Dict, storage_mode: str, sp_client, prefer_webp: bool = False) -> tuple[bytes, str, str]:
    """Load image bytes for a record. Returns (bytes, filename, source_label).

    prefer_webp=True tries the WebP version first — use for vision/AI calls
    where high-res quality is unnecessary and file size matters.
    """
    fields = rec.get("fields", {})
    filename_fallback = str(fields.get("Filename", "") or "image.jpg").strip() or "image.jpg"
    location = _sanitize_relative_path(fields.get("Location", ""))
    high_res_location = _sanitize_relative_path(fields.get("High-Res Location", ""))

    if storage_mode == "sharepoint" and sp_client is not None:
        root = Config.SHAREPOINT_IMAGE_FOLDER.strip("/")

        webp_entries: list[tuple[str, str, str]] = []
        hr_entries: list[tuple[str, str, str]] = []

        if location:
            webp_path = f"WebP/{location}" if not root else f"{root}/WebP/{location}"
            webp_entries.append((webp_path, PurePosixPath(location).name or filename_fallback, "webp"))
            legacy_path = f"{location}" if not root else f"{root}/{location}"
            webp_entries.append((legacy_path, PurePosixPath(location).name or filename_fallback, "legacy"))
        if high_res_location:
            high_path = f"High-Res/{high_res_location}" if not root else f"{root}/High-Res/{high_res_location}"
            hr_entries.append((high_path, PurePosixPath(high_res_location).name or filename_fallback, "high-res"))

        candidates = (webp_entries + hr_entries) if prefer_webp else (hr_entries + webp_entries)

        errors = []
        for sp_path, filename, source_label in candidates:
            try:
                return sp_client.get_file_bytes(sp_path), filename, source_label
            except Exception as exc:
                errors.append(f"{source_label}:{exc}")

        raise FileNotFoundError(
            f"Could not load record image in SharePoint for {rec.get('id', '')}. "
            f"Tried {len(candidates)} paths. Last errors: {'; '.join(errors[:3])}"
        )

    base = Path(Config.IMAGE_FOLDER).resolve()

    webp_local: list[tuple[Optional[Path], str, str]] = []
    hr_local: list[tuple[Optional[Path], str, str]] = []

    if location:
        webp_local.append((
            _safe_local_target(base, "WebP", location),
            PurePosixPath(location).name or filename_fallback,
            "webp",
        ))
        webp_local.append((
            _safe_local_target(base, "", location),
            PurePosixPath(location).name or filename_fallback,
            "legacy",
        ))
    if high_res_location:
        hr_local.append((
            _safe_local_target(base, "High-Res", high_res_location),
            PurePosixPath(high_res_location).name or filename_fallback,
            "high-res",
        ))

    candidates_local = (webp_local + hr_local) if prefer_webp else (hr_local + webp_local)

    for path, filename, source_label in candidates_local:
        if path is not None and path.exists() and path.is_file():
            return path.read_bytes(), filename, source_label

    raise FileNotFoundError(
        f"Could not load local record image for {rec.get('id', '')}. "
        f"Checked {len(candidates_local)} paths"
    )


def _check_location_health(location: str, storage_mode: str, sp_client) -> tuple[str, str]:
    """Check whether a record location points to a loadable image.

    Returns (status, detail) where status is one of:
    - ok
    - missing
    - corrupt
    - invalid
    - error
    """
    rel = _sanitize_relative_path(location)
    if not rel:
        return "invalid", "Location is empty or invalid"

    if storage_mode == "sharepoint" and sp_client is not None:
        root = Config.SHAREPOINT_IMAGE_FOLDER.strip("/")
        candidates = [
            f"WebP/{rel}" if not root else f"{root}/WebP/{rel}",
            f"{rel}" if not root else f"{root}/{rel}",
        ]

        for sp_path in candidates:
            try:
                blob = sp_client.get_file_bytes(sp_path)
            except requests.exceptions.HTTPError as exc:
                response = getattr(exc, "response", None)
                if response is not None and response.status_code == 404:
                    continue
                return "error", f"SharePoint read failed: {exc}"
            except Exception as exc:
                return "error", f"SharePoint read failed: {exc}"

            try:
                with PILImage.open(BytesIO(blob)) as img:
                    img.verify()
                return "ok", ""
            except Exception as exc:
                return "corrupt", f"Image decode failed: {exc}"

        return "missing", "No matching WebP/legacy file found in SharePoint"

    base = Path(Config.IMAGE_FOLDER).resolve()
    candidates_local = [
        _safe_local_target(base, "WebP", rel),
        _safe_local_target(base, "", rel),
    ]

    found_path = None
    for candidate in candidates_local:
        if candidate is not None and candidate.exists() and candidate.is_file():
            found_path = candidate
            break

    if found_path is None:
        return "missing", "No matching WebP/legacy file found locally"

    try:
        with PILImage.open(found_path) as img:
            img.verify()
        return "ok", ""
    except Exception as exc:
        return "corrupt", f"Image decode failed: {exc}"


def _collect_broken_thumbnail_snapshot(limit: int = 200, status_filter: str = "", records: Optional[List[Dict]] = None) -> Dict:
    limit = max(10, min(limit, 1000))
    status_filter = str(status_filter or "").strip().lower()

    all_records = records if records is not None else _records_snapshot(use_cache=True)
    records = [
        rec
        for rec in all_records
        if not status_filter
        or str(rec.get("fields", {}).get("Status", "") or "").strip().lower() == status_filter
    ]

    if Config.TEST_MODE:
        storage_mode = "local"
        sp_client = None
    else:
        storage_mode = "sharepoint"
        sp_client = get_sp_client()

    broken_records = []
    healthy_count = 0

    for rec in records:
        fields = rec.get("fields", {})
        rec_id = str(rec.get("id", "")).strip()
        filename = str(fields.get("Filename", "") or "").strip()
        location = str(fields.get("Location", "") or "").strip()

        if not location:
            broken_records.append({
                "id": rec_id,
                "filename": filename,
                "status": str(fields.get("Status", "") or "").strip(),
                "source": str(fields.get("Source", "") or "").strip(),
                "location": location,
                "reason": "missing-location",
                "detail": "Record has no Location value",
            })
            continue

        health, detail = _check_location_health(location, storage_mode=storage_mode, sp_client=sp_client)
        if health == "ok":
            healthy_count += 1
            continue

        reason_map = {
            "missing": "missing-file",
            "corrupt": "corrupt-file",
            "invalid": "invalid-location",
            "error": "error-loading",
        }
        broken_records.append({
            "id": rec_id,
            "filename": filename,
            "status": str(fields.get("Status", "") or "").strip(),
            "source": str(fields.get("Source", "") or "").strip(),
            "location": location,
            "reason": reason_map.get(health, "error-loading"),
            "detail": detail,
        })

    broken_records.sort(
        key=lambda r: (
            str(r.get("filename", "") or "").lower(),
            str(r.get("id", "") or ""),
        )
    )
    broken_ids = [r.get("id", "") for r in broken_records if r.get("id", "")]

    return {
        "storage_mode": storage_mode,
        "status_filter": status_filter,
        "scanned_count": len(records),
        "healthy_count": healthy_count,
        "broken_count": len(broken_records),
        "broken_records": broken_records[:limit],
        "broken_record_ids": broken_ids,
        "display_limit": limit,
        "truncated": len(broken_records) > limit,
    }


@app.route("/api/maintenance/retag-preview")
@login_required
def api_maintenance_retag_preview():
    category_filter = request.args.get("category", "").strip()
    tag_filter = request.args.get("tag", "").strip()
    status_filter = request.args.get("status", "").strip()
    max_records_raw = request.args.get("max_records", "100").strip()
    missing_alt_only = _as_bool(request.args.get("missing_alt_only", "false"))
    missing_tags_only = _as_bool(request.args.get("missing_tags_only", "false"))

    try:
        max_records = max(1, min(int(max_records_raw), 1000))
    except ValueError:
        return jsonify({"error": "max_records must be an integer"}), 400

    records = _records_snapshot(use_cache=True)
    matches = _get_retag_target_records(
        category_filter=category_filter,
        tag_filter_raw=tag_filter,
        status_filter=status_filter,
        max_records=max_records,
        records=records,
        missing_alt_only=missing_alt_only,
        missing_tags_only=missing_tags_only,
    )

    sample = []
    for rec in matches[:25]:
        fields = rec.get("fields", {})
        sample.append({
            "id": rec.get("id", ""),
            "filename": fields.get("Filename", ""),
            "status": fields.get("Status", ""),
            "tags": fields.get("Tags", ""),
            "location": fields.get("Location", ""),
            "high_res_location": fields.get("High-Res Location", ""),
        })

    return jsonify({
        "filters": {
            "category": category_filter,
            "tag": tag_filter,
            "status": status_filter,
        },
        "max_records": max_records,
        "matched_count": len(matches),
        "sample": sample,
    })


@app.route("/api/maintenance/retag-run")
@login_required
def api_maintenance_retag_run():
    category_filter = request.args.get("category", "").strip()
    tag_filter = request.args.get("tag", "").strip()
    status_filter = request.args.get("status", "").strip()
    max_records_raw = request.args.get("max_records", "100").strip()
    regenerate_alt = _as_bool(request.args.get("regenerate_alt", "true"))
    regenerate_tags = _as_bool(request.args.get("regenerate_tags", "true"))
    missing_alt_only = _as_bool(request.args.get("missing_alt_only", "false"))
    missing_tags_only = _as_bool(request.args.get("missing_tags_only", "false"))

    if not regenerate_alt and not regenerate_tags:
        return jsonify({"error": "At least one of regenerate_alt or regenerate_tags must be true"}), 400

    try:
        max_records = max(1, min(int(max_records_raw), 1000))
    except ValueError:
        return jsonify({"error": "max_records must be an integer"}), 400

    def generate():
        yield "data: [START]\n\n"
        q: queue.Queue = queue.Queue()

        def worker():
            try:
                from src.ai_generator import AltTextGenerator

                gen = AltTextGenerator()
                if Config.TEST_MODE:
                    list_client = LocalClient()
                    sp_client = None
                    storage_mode = "local"
                else:
                    from src.sharepoint_list_client import SharePointListClient
                    from src.sharepoint_client import SharePointClient
                    list_client = SharePointListClient()
                    sp_client = SharePointClient()
                    storage_mode = "sharepoint"

                records = _records_snapshot(use_cache=False)
                targets = _get_retag_target_records(
                    category_filter=category_filter,
                    tag_filter_raw=tag_filter,
                    status_filter=status_filter,
                    max_records=max_records,
                    records=records,
                    missing_alt_only=missing_alt_only,
                    missing_tags_only=missing_tags_only,
                )
                total = len(targets)
                q.put(("progress", f"Matched {total} record(s) for bulk re-tag"))

                processed = 0
                failed = 0
                alt_updated = 0
                tags_updated = 0
                status_reset = 0
                failures: list[str] = []

                for idx, rec in enumerate(targets, 1):
                    rec_id = str(rec.get("id", "")).strip()
                    fields = rec.get("fields", {})
                    filename = str(fields.get("Filename", "") or "").strip() or "image.jpg"
                    q.put(("progress", f"[{idx}/{total}] Reprocessing {filename}"))

                    try:
                        file_bytes, source_filename, source_label = _load_record_image_bytes(
                            rec,
                            storage_mode=storage_mode,
                            sp_client=sp_client,
                            prefer_webp=True,
                        )
                        q.put(("progress", f"    Loaded {source_label} bytes"))

                        current_alt = str(fields.get("Alt Text", "") or "").strip()
                        current_tags = str(fields.get("Tags", "") or "").strip()

                        new_alt = current_alt
                        new_tags = current_tags

                        if regenerate_alt and regenerate_tags:
                            # Single API call for both fields
                            candidate_alt, candidate_tags = gen.generate_alt_and_tags(
                                file_bytes, filename=source_filename
                            )
                            if not candidate_alt:
                                raise ValueError("Alt text generation returned empty")
                            if not candidate_tags:
                                raise ValueError("Tag generation returned empty")
                            new_alt = candidate_alt.strip()
                            new_tags = candidate_tags.strip()
                        else:
                            if regenerate_alt:
                                try:
                                    candidate_alt = gen._vision_message(
                                        file_bytes, source_filename,
                                        "Analyze this image and generate a concise, descriptive alt text for web accessibility. "
                                        "Max 125 characters. Not starting with 'Image of'. Generate ONLY the alt text.",
                                    ).strip()
                                except Exception as exc:
                                    raise ValueError(f"Alt text generation failed: {exc}") from exc
                                if not candidate_alt:
                                    raise ValueError("Alt text generation returned empty")
                                new_alt = candidate_alt

                            if regenerate_tags:
                                try:
                                    candidate_tags = gen.generate_tags(file_bytes, filename=source_filename)
                                except Exception as exc:
                                    raise ValueError(f"Tag generation failed: {exc}") from exc
                                if not candidate_tags:
                                    raise ValueError("Tag generation returned empty")
                                new_tags = candidate_tags.strip()

                        patch: Dict = {}
                        if regenerate_alt and new_alt != current_alt:
                            patch["Alt Text"] = new_alt
                            alt_updated += 1
                        if regenerate_tags and new_tags != current_tags:
                            patch["Tags"] = new_tags
                            tags_updated += 1
                        if str(fields.get("Status", "") or "").strip() != "pending-review":
                            patch["Status"] = "pending-review"
                            status_reset += 1

                        if patch and not list_client.patch_fields(rec_id, patch):
                            raise ValueError(f"Failed to patch record {rec_id}")

                        processed += 1
                    except Exception as exc:
                        failed += 1
                        detail = f"{filename} ({rec_id}): {exc}"
                        failures.append(detail)
                        q.put(("progress", f"[ERROR] {detail}"))

                q.put(("done", {
                    "matched": total,
                    "processed": processed,
                    "failed": failed,
                    "alt_updated": alt_updated,
                    "tags_updated": tags_updated,
                    "status_reset": status_reset,
                    "failures": failures[:20],
                    "filters": {
                        "category": category_filter,
                        "tag": tag_filter,
                        "status": status_filter,
                        "max_records": max_records,
                    },
                    "options": {
                        "regenerate_alt": regenerate_alt,
                        "regenerate_tags": regenerate_tags,
                    },
                }))
            except Exception:
                q.put(("error", "Bulk operation failed"))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        yield from _sse_poll_loop(t, q, 1800, "Bulk re-tag timed out after 30 minutes",
                                  invalidate_cache=True)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/maintenance/status-reset-preview")
@login_required
def api_maintenance_status_reset_preview():
    category_filter = request.args.get("category", "").strip()
    tag_filter = request.args.get("tag", "").strip()
    current_status_filter = request.args.get("status", "").strip()
    start_date_raw = request.args.get("start_date", "").strip()
    end_date_raw = request.args.get("end_date", "").strip()
    max_records_raw = request.args.get("max_records", "100").strip()

    try:
        max_records = max(1, min(int(max_records_raw), 1000))
    except ValueError:
        return jsonify({"error": "max_records must be an integer"}), 400

    start_date_value = _parse_iso_date(start_date_raw) if start_date_raw else None
    end_date_value = _parse_iso_date(end_date_raw) if end_date_raw else None
    if start_date_raw and start_date_value is None:
        return jsonify({"error": "start_date must be a valid ISO date (YYYY-MM-DD)"}), 400
    if end_date_raw and end_date_value is None:
        return jsonify({"error": "end_date must be a valid ISO date (YYYY-MM-DD)"}), 400
    if start_date_value and end_date_value and start_date_value > end_date_value:
        return jsonify({"error": "start_date cannot be after end_date"}), 400

    records = _records_snapshot(use_cache=True)
    target_info = _status_reset_targets(
        category_filter=category_filter,
        tag_filter_raw=tag_filter,
        current_status_filter=current_status_filter,
        start_date_value=start_date_value,
        end_date_value=end_date_value,
        max_records=max_records,
        records=records,
    )
    matches = target_info["records"]

    sample = []
    for rec in matches[:25]:
        fields = rec.get("fields", {})
        sample.append({
            "id": rec.get("id", ""),
            "filename": fields.get("Filename", ""),
            "status": fields.get("Status", ""),
            "tags": fields.get("Tags", ""),
            "location": fields.get("Location", ""),
        })

    return jsonify({
        "filters": {
            "category": category_filter,
            "tag": tag_filter,
            "status": current_status_filter,
            "start_date": start_date_raw,
            "end_date": end_date_raw,
        },
        "max_records": max_records,
        "matched_count": len(matches),
        "already_pending_count": target_info["already_pending"],
        "eligible_to_reset_count": len(matches) - target_info["already_pending"],
        "date_filtered_out_count": target_info["date_filtered_out"],
        "missing_date_for_filter_count": target_info["missing_date_for_filter"],
        "sample": sample,
    })


@app.route("/api/maintenance/status-reset", methods=["POST"])
@login_required
def api_maintenance_status_reset():
    data = request.get_json(force=True) or {}

    category_filter = str(data.get("category", "")).strip()
    tag_filter = str(data.get("tag", "")).strip()
    current_status_filter = str(data.get("status", "")).strip()
    start_date_raw = str(data.get("start_date", "")).strip()
    end_date_raw = str(data.get("end_date", "")).strip()
    confirm_token = str(data.get("confirm_token", "")).strip().upper()
    expected_count_raw = data.get("expected_count")
    max_records_raw = str(data.get("max_records", "100")).strip()

    if confirm_token != "PURGE":
        return jsonify({"error": "confirm_token must be PURGE"}), 400

    try:
        max_records = max(1, min(int(max_records_raw), 1000))
    except ValueError:
        return jsonify({"error": "max_records must be an integer"}), 400

    expected_count: Optional[int] = None
    if expected_count_raw is not None:
        try:
            expected_count = int(expected_count_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "expected_count must be an integer"}), 400

    start_date_value = _parse_iso_date(start_date_raw) if start_date_raw else None
    end_date_value = _parse_iso_date(end_date_raw) if end_date_raw else None
    if start_date_raw and start_date_value is None:
        return jsonify({"error": "start_date must be a valid ISO date (YYYY-MM-DD)"}), 400
    if end_date_raw and end_date_value is None:
        return jsonify({"error": "end_date must be a valid ISO date (YYYY-MM-DD)"}), 400
    if start_date_value and end_date_value and start_date_value > end_date_value:
        return jsonify({"error": "start_date cannot be after end_date"}), 400

    records = _records_snapshot(use_cache=False)
    target_info = _status_reset_targets(
        category_filter=category_filter,
        tag_filter_raw=tag_filter,
        current_status_filter=current_status_filter,
        start_date_value=start_date_value,
        end_date_value=end_date_value,
        max_records=max_records,
        records=records,
    )
    matches = target_info["records"]

    if expected_count is not None and expected_count != len(matches):
        return jsonify({
            "error": "Record count changed since preview. Refresh preview and retry.",
            "expected_count": expected_count,
            "current_count": len(matches),
        }), 409

    client = get_client()
    patches: list[tuple[str, Dict]] = []
    unchanged = 0
    failed_ids: list[str] = []
    invalid_id_failures = 0

    for rec in matches:
        record_id = str(rec.get("id", "")).strip()
        if not record_id:
            invalid_id_failures += 1
            continue
        fields = rec.get("fields", {})
        current_status = str(fields.get("Status", "") or "").strip()
        if current_status == "pending-review":
            unchanged += 1
            continue
        patches.append((record_id, {"Status": "pending-review"}))

    patch_result = _bulk_patch_fields(client, patches)
    updated = patch_result["updated"]
    failed_ids.extend(patch_result["failed_ids"])
    failed = invalid_id_failures + len([fid for fid in failed_ids if fid])

    global _records_cache
    _records_cache = None

    return jsonify({
        "matched_count": len(matches),
        "updated_count": updated,
        "unchanged_count": unchanged,
        "failed_count": failed,
        "failed_ids": failed_ids[:20],
        "filters": {
            "category": category_filter,
            "tag": tag_filter,
            "status": current_status_filter,
            "start_date": start_date_raw,
            "end_date": end_date_raw,
            "max_records": max_records,
        },
    })


@app.route("/api/maintenance/export-csv")
@login_required
def api_maintenance_export_csv():
    category_filter = request.args.get("category", "").strip()
    status_filter = request.args.get("status", "").strip()

    desired_category = _normalize_filter_key(category_filter)
    desired_status = status_filter.lower()

    rows = []
    records = _records_snapshot(use_cache=True)
    for rec in records:
        fields = rec.get("fields", {})
        location = _sanitize_relative_path(fields.get("Location", ""))
        parts = list(PurePosixPath(location).parts) if location else []
        category = parts[0] if parts else ""

        rec_status = str(fields.get("Status", "") or "").strip()
        if desired_status and rec_status.lower() != desired_status:
            continue
        if desired_category:
            category_key = _normalize_filter_key(category)
            if category_key != desired_category:
                continue

        rows.append({
            "id": str(rec.get("id", "") or "").strip(),
            "filename": str(fields.get("Filename", "") or "").strip(),
            "category": category,
            "alt_text": str(fields.get("Alt Text", "") or "").strip(),
            "tags": str(fields.get("Tags", "") or "").strip(),
            "status": rec_status,
            "location": location,
        })

    rows.sort(key=lambda r: ((r.get("filename") or "").lower(), r.get("id") or ""))

    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["id", "filename", "category", "alt_text", "tags", "status", "location"],
    )
    writer.writeheader()
    writer.writerows(rows)

    filename_parts = ["image-library"]
    if category_filter:
        filename_parts.append(f"cat-{_normalize_filter_key(category_filter) or 'filtered'}")
    if status_filter:
        filename_parts.append(f"status-{_normalize_filter_key(status_filter) or 'filtered'}")
    filename = "-".join(filename_parts) + ".csv"

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/maintenance/broken-thumbnails")
@login_required
def api_maintenance_broken_thumbnails():
    limit_raw = request.args.get("limit", "200").strip()
    status_filter = request.args.get("status", "").strip()

    try:
        limit = int(limit_raw)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

    try:
        records = _records_snapshot(use_cache=True)
        return jsonify(_collect_broken_thumbnail_snapshot(limit=limit, status_filter=status_filter, records=records))
    except Exception as exc:
        return jsonify({"error": f"Failed to scan broken thumbnails: {exc}"}), 500


@app.route("/api/maintenance/broken-thumbnails/delete-records", methods=["POST"])
@login_required
def api_maintenance_broken_thumbnails_delete_records():
    data = request.get_json(force=True) or {}
    record_ids, error_response = _parse_record_ids(data, require_confirm_token=True)
    if error_response:
        return error_response

    expected_count_raw = data.get("expected_count")
    approval_token = str(data.get("approval_token", "")).strip()
    expected_count: Optional[int] = None
    if expected_count_raw is not None:
        try:
            expected_count = int(expected_count_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "expected_count must be an integer"}), 400

    guardrail_error = _validate_destructive_guardrails(
        action="broken-thumbnails-delete-records",
        record_ids=record_ids,
        expected_count=expected_count,
        approval_token=approval_token,
    )
    if guardrail_error:
        return guardrail_error

    deleted = _bulk_delete_records(get_client(), record_ids)

    global _records_cache
    _records_cache = None

    result = {
        "requested": len(record_ids),
        "deleted": deleted,
        "failed": max(0, len(record_ids) - deleted),
    }

    _maintenance_append_audit(
        action="broken-thumbnails-delete-records",
        outcome="ok",
        details=result,
    )

    return jsonify(result)


@app.route("/api/maintenance/broken-thumbnails/relink", methods=["POST"])
@login_required
def api_maintenance_broken_thumbnails_relink():
    data = request.get_json(force=True) or {}
    record_id = str(data.get("record_id", "")).strip()
    new_location_raw = str(data.get("new_location", "")).strip()
    check_exists = _as_bool(data.get("check_exists", True))
    set_pending_review = _as_bool(data.get("set_pending_review", True))

    if not record_id:
        return jsonify({"error": "record_id is required"}), 400

    new_location = _sanitize_relative_path(new_location_raw)
    if not new_location:
        return jsonify({"error": "new_location is empty or invalid"}), 400

    client = get_client()
    records = _records_snapshot(use_cache=False)
    target = next((r for r in records if str(r.get("id", "")).strip() == record_id), None)
    if target is None:
        return jsonify({"error": "record not found"}), 404

    if Config.TEST_MODE:
        storage_mode = "local"
        sp_client = None
    else:
        storage_mode = "sharepoint"
        sp_client = get_sp_client()

    if check_exists:
        health, detail = _check_location_health(new_location, storage_mode=storage_mode, sp_client=sp_client)
        if health != "ok":
            return jsonify({
                "error": "new_location is not loadable",
                "health": health,
                "detail": detail,
            }), 400

    fields = target.get("fields", {})
    patch: Dict = {"Location": new_location}
    if set_pending_review and str(fields.get("Status", "") or "").strip() != "pending-review":
        patch["Status"] = "pending-review"

    if not client.patch_fields(record_id, patch):
        return jsonify({"error": "Failed to update record"}), 500

    global _records_cache
    _records_cache = None

    return jsonify({
        "ok": True,
        "record_id": record_id,
        "new_location": new_location,
        "set_pending_review": set_pending_review,
    })


@app.route("/api/maintenance/sync-highres")
@login_required
def api_maintenance_sync_highres():
    """SSE stream — catalog unprocessed High-Res files and report orphaned WebP assets."""
    requested_source = request.args.get("source", "").strip()
    dry_run = request.args.get("dry_run", "").strip().lower() in {"1", "true", "yes"}

    def generate():
        yield "data: [START]\n\n"
        q: queue.Queue = queue.Queue()

        def worker():
            try:
                from src.ai_generator import AltTextGenerator
                from src.image_processor import normalize_source, process_image

                gen = AltTextGenerator()
                target_source = normalize_source(requested_source) if requested_source else None

                if Config.TEST_MODE:
                    list_client = LocalClient()
                    sp_client = None
                    storage_mode = "local"
                else:
                    from src.sharepoint_list_client import SharePointListClient
                    from src.sharepoint_client import SharePointClient
                    list_client = SharePointListClient()
                    sp_client = SharePointClient()
                    storage_mode = "sharepoint"

                records = list_client.get_all_records()
                existing_hr_locations = {
                    str(r.get("fields", {}).get("High-Res Location", "")).strip()
                    for r in records
                    if str(r.get("fields", {}).get("High-Res Location", "")).strip()
                }
                existing_hr_basenames = {PurePosixPath(p).name for p in existing_hr_locations if p}
                existing_webp_locations = {
                    str(r.get("fields", {}).get("Location", "")).strip()
                    for r in records
                    if str(r.get("fields", {}).get("Location", "")).strip()
                }

                candidates: list[dict] = []
                orphaned_webp: list[str] = []

                if storage_mode == "sharepoint" and sp_client is not None:
                    root = Config.SHAREPOINT_IMAGE_FOLDER
                    hr_items = sp_client.list_all_images(f"{root}/High-Res")
                    webp_items = sp_client.list_all_images(f"{root}/WebP")

                    for sp_path, rel in hr_items:
                        rel_posix = str(PurePosixPath(rel))
                        parts = PurePosixPath(rel_posix).parts
                        src = normalize_source(parts[0] if parts else "Internal")
                        if target_source and src != target_source:
                            continue
                        base = PurePosixPath(rel_posix).name
                        if rel_posix in existing_hr_locations or base in existing_hr_basenames:
                            continue
                        candidates.append({
                            "rel": rel_posix,
                            "source": src,
                            "filename": base,
                            "sp_path": sp_path,
                        })

                    for _sp_path, rel in webp_items:
                        rel_posix = str(PurePosixPath(rel))
                        if rel_posix not in existing_webp_locations:
                            orphaned_webp.append(rel_posix)

                    def load_bytes(item: dict) -> bytes:
                        return sp_client.get_file_bytes(item["sp_path"])

                else:
                    base = Path(Config.IMAGE_FOLDER)
                    hr_root = base / "High-Res"
                    webp_root = base / "WebP"
                    formats = _normalized_formats()

                    if hr_root.exists():
                        for file_path in hr_root.rglob("*"):
                            if not file_path.is_file() or file_path.suffix.lower() not in formats:
                                continue
                            rel_posix = file_path.relative_to(hr_root).as_posix()
                            parts = PurePosixPath(rel_posix).parts
                            src = normalize_source(parts[0] if parts else "Internal")
                            if target_source and src != target_source:
                                continue
                            if rel_posix in existing_hr_locations or file_path.name in existing_hr_basenames:
                                continue
                            candidates.append({
                                "rel": rel_posix,
                                "source": src,
                                "filename": file_path.name,
                                "path": str(file_path),
                            })

                    if webp_root.exists():
                        for file_path in webp_root.rglob("*.webp"):
                            rel_posix = file_path.relative_to(webp_root).as_posix()
                            if rel_posix not in existing_webp_locations:
                                orphaned_webp.append(rel_posix)

                    def load_bytes(item: dict) -> bytes:
                        return Path(item["path"]).read_bytes()

                q.put(("progress", f"Found {len(candidates)} unprocessed High-Res file(s)"))

                if dry_run:
                    summary = {
                        "processed": 0,
                        "failed": 0,
                        "unprocessed_high_res_found": len(candidates),
                        "orphaned_webp_count": len(orphaned_webp),
                        "orphaned_webp": sorted(orphaned_webp),
                        "source_filter": target_source or "all",
                        "dry_run": True,
                    }
                    q.put(("done", summary))
                    return

                processed = 0
                failed = 0
                total = len(candidates)
                # Pre-load filenames once to avoid O(N) record_exists calls per slug
                existing_filenames: set = {
                    str(r.get("fields", {}).get("Filename", ""))
                    for r in _records_snapshot(use_cache=True)
                    if r.get("fields", {}).get("Filename")
                }
                for idx, item in enumerate(candidates, 1):
                    q.put(("progress", f"[{idx}/{total}] Cataloging {item['rel']}"))
                    try:
                        file_bytes = load_bytes(item)
                        process_image(
                            file_bytes=file_bytes,
                            original_filename=item["filename"],
                            generator=gen,
                            list_client=list_client,
                            sp_client=sp_client,
                            image_folder=Config.IMAGE_FOLDER,
                            storage_mode=storage_mode,
                            on_progress=lambda msg: q.put(("progress", f"    {msg}")),
                            source=item["source"],
                            write_high_res=False,
                            high_res_location_override=item["rel"],
                            existing_filenames=existing_filenames,
                        )
                        processed += 1
                    except Exception as exc:
                        failed += 1
                        q.put(("progress", f"[ERROR] {item['rel']}: {exc}"))

                summary = {
                    "processed": processed,
                    "failed": failed,
                    "unprocessed_high_res_found": total,
                    "orphaned_webp_count": len(orphaned_webp),
                    "orphaned_webp": sorted(orphaned_webp),
                    "source_filter": target_source or "all",
                    "dry_run": False,
                }
                q.put(("done", summary))

            except Exception:
                q.put(("error", "Maintenance sync failed"))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        yield from _sse_poll_loop(t, q, 600, "Maintenance sync timed out after 10 minutes",
                                  invalidate_cache=True)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/maintenance/folder-ingest")
@login_required
def api_maintenance_folder_ingest():
    """SSE stream — trigger an immediate ingest run via the background poller."""

    def generate():
        yield "data: [START]\n\n"
        q: queue.Queue = queue.Queue()

        def worker():
            try:
                result = ingest_poller.run_now(on_progress=lambda msg: q.put(("progress", msg)))
                if "error" in result:
                    q.put(("error", result["error"]))
                else:
                    global _records_cache
                    _records_cache = None
                    q.put(("done", result))
            except Exception as exc:
                q.put(("error", f"Folder ingest failed: {exc}"))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        yield from _sse_poll_loop(t, q, 600, "Folder ingest timed out after 10 minutes")

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/maintenance/ingest-log")
@login_required
def api_maintenance_ingest_log():
    """Return the rolling ingest poller log."""
    return jsonify({"log": ingest_poller.get_log()})


@app.route("/api/maintenance/health-snapshot")
@login_required
def api_maintenance_health_snapshot():
    records = _records_snapshot(use_cache=True)
    status_counts = {
        "pending-review": 0,
        "approved": 0,
        "rejected": 0,
        "archived": 0,
        "other": 0,
    }

    for rec in records:
        status = str(rec.get("fields", {}).get("Status", "") or "").strip()
        if status in status_counts:
            status_counts[status] += 1
        else:
            status_counts["other"] += 1

    integrity = _build_integrity_scorecard(records)
    drift = _collect_drift_candidates(records, limit=50)
    orphans = _collect_orphan_snapshot(limit=25, records=records)
    broken = _collect_broken_thumbnail_snapshot(limit=25, records=records)
    duplicates = _collect_duplicate_snapshot_helper(records, limit=25, include_near_alt=True, near_threshold=0.92)

    return jsonify({
        "generated_at": _now_utc_iso(),
        "record_count": len(records),
        "status_counts": status_counts,
        "integrity": {
            "category_count": len(integrity.get("categories", [])),
            "lowest_category": (integrity.get("categories", [{}])[0] or {}).get("category", "")
            if integrity.get("categories") else "",
            "lowest_score": (integrity.get("categories", [{}])[0] or {}).get("integrity_score", 100.0)
            if integrity.get("categories") else 100.0,
            "unknown_status_count": integrity.get("unknown_status_count", 0),
        },
        "drift": {
            "candidate_count": drift.get("candidate_count", 0),
            "reason_counts": drift.get("reason_counts", {}),
        },
        "orphans": {
            "missing_file_records_count": orphans.get("missing_file_records_count", 0),
            "orphaned_webp_files_count": orphans.get("orphaned_webp_files_count", 0),
            "orphaned_high_res_files_count": orphans.get("orphaned_high_res_files_count", 0),
        },
        "broken": {
            "broken_count": broken.get("broken_count", 0),
            "healthy_count": broken.get("healthy_count", 0),
        },
        "duplicates": {
            "duplicate_group_count": duplicates.get("duplicate_group_count", 0),
            "filename_group_count": duplicates.get("filename_group_count", 0),
            "slug_group_count": duplicates.get("slug_group_count", 0),
            "alt_exact_group_count": duplicates.get("alt_exact_group_count", 0),
            "alt_near_group_count": duplicates.get("alt_near_group_count", 0),
        },
    })


@app.route("/api/maintenance/integrity-scorecard")
@login_required
def api_maintenance_integrity_scorecard():
    records = _records_snapshot(use_cache=True)
    return jsonify(_build_integrity_scorecard(records))


@app.route("/api/maintenance/aging-drift")
@login_required
def api_maintenance_aging_drift():
    stale_pending_days_raw = request.args.get("stale_pending_days", "14").strip()
    stale_approved_days_raw = request.args.get("stale_approved_days", "180").strip()
    min_alt_chars_raw = request.args.get("min_alt_chars", "40").strip()
    min_tag_count_raw = request.args.get("min_tag_count", "2").strip()
    limit_raw = request.args.get("limit", "200").strip()

    try:
        stale_pending_days = max(1, min(int(stale_pending_days_raw), 3650))
        stale_approved_days = max(1, min(int(stale_approved_days_raw), 3650))
        min_alt_chars = max(1, min(int(min_alt_chars_raw), 300))
        min_tag_count = max(0, min(int(min_tag_count_raw), 20))
        limit = max(10, min(int(limit_raw), 2000))
    except ValueError:
        return jsonify({"error": "All thresholds and limit must be integers"}), 400

    records = _records_snapshot(use_cache=True)
    return jsonify(
        _collect_drift_candidates(
            records,
            stale_pending_days=stale_pending_days,
            stale_approved_days=stale_approved_days,
            min_alt_chars=min_alt_chars,
            min_tag_count=min_tag_count,
            limit=limit,
        )
    )


@app.route("/api/maintenance/quality-drift-queue")
@login_required
def api_maintenance_quality_drift_queue():
    limit_raw = request.args.get("limit", "200").strip()
    try:
        limit = max(10, min(int(limit_raw), 2000))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

    records = _records_snapshot(use_cache=True)
    drift = _collect_drift_candidates(records, limit=limit)
    quality_reasons = {
        "short-alt",
        "sparse-tags",
        "approved-stale",
        "missing-source",
        "missing-slug",
    }

    queue = [
        item
        for item in drift.get("candidates", [])
        if set(item.get("reasons", [])) & quality_reasons
    ]

    return jsonify({
        "generated_at": _now_utc_iso(),
        "queue_reason_filter": sorted(quality_reasons),
        "queue_count": len(queue),
        "candidates": queue,
    })


@app.route("/api/maintenance/quality-drift-queue/mark", methods=["POST"])
@login_required
def api_maintenance_quality_drift_queue_mark():
    data = request.get_json(force=True) or {}
    record_ids, error_response = _parse_record_ids(data, require_confirm_token=False)
    if error_response:
        return error_response

    expected_count_raw = data.get("expected_count")
    if expected_count_raw is not None:
        try:
            expected_count = int(expected_count_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "expected_count must be an integer"}), 400
        if expected_count != len(record_ids):
            return jsonify({
                "error": "expected_count does not match record_ids length",
                "expected_count": expected_count,
                "record_count": len(record_ids),
            }), 409

    marker = str(data.get("marker", "?retag-queue")).strip() or "?retag-queue"
    if not marker.startswith("?"):
        marker = f"?{marker}"
    set_pending_review = _as_bool(data.get("set_pending_review", True))

    records = _records_snapshot(use_cache=False)
    by_id = {str(r.get("id", "")).strip(): r for r in records if str(r.get("id", "")).strip()}
    patches: List[tuple[str, Dict]] = []
    unchanged = 0
    missing = 0

    for record_id in record_ids:
        rec = by_id.get(record_id)
        if rec is None:
            missing += 1
            continue

        fields = rec.get("fields", {})
        current_tags = str(fields.get("Tags", "") or "").strip()
        tag_parts = [t.strip() for t in current_tags.split(",") if t.strip()]
        patch: Dict = {}

        if marker not in tag_parts:
            tag_parts.append(marker)
        next_tags = ", ".join(tag_parts)
        if next_tags != current_tags:
            patch["Tags"] = next_tags

        if set_pending_review and str(fields.get("Status", "") or "").strip() != "pending-review":
            patch["Status"] = "pending-review"

        if patch:
            patches.append((record_id, patch))
        else:
            unchanged += 1

    result = _bulk_patch_fields(get_client(), patches)
    if result.get("updated", 0) > 0:
        global _records_cache
        _records_cache = None

    payload = {
        "requested": len(record_ids),
        "updated": result.get("updated", 0),
        "unchanged": unchanged,
        "missing": missing + len(result.get("missing_ids", [])),
        "failed": len(result.get("failed_ids", [])),
        "marker": marker,
        "set_pending_review": set_pending_review,
    }

    _maintenance_append_audit("quality-drift-queue-mark", "ok", payload)
    return jsonify(payload)


@app.route("/api/maintenance/category-normalization/preview")
@login_required
def api_maintenance_category_normalization_preview():
    limit_raw = request.args.get("limit", "200").strip()
    try:
        limit = max(10, min(int(limit_raw), 2000))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

    records = _records_snapshot(use_cache=True)
    return jsonify(_build_category_normalization_preview(records, limit=limit))


@app.route("/api/maintenance/category-normalization/apply", methods=["POST"])
@login_required
def api_maintenance_category_normalization_apply():
    data = request.get_json(force=True) or {}
    confirm_token = str(data.get("confirm_token", "")).strip().upper()
    if confirm_token != "APPLY":
        return jsonify({"error": "confirm_token must be APPLY"}), 400

    set_pending_review = _as_bool(data.get("set_pending_review", True))
    expected_count_raw = data.get("expected_count")
    max_apply_raw = str(data.get("max_apply", "500")).strip()
    try:
        max_apply = max(1, min(int(max_apply_raw), 5000))
    except ValueError:
        return jsonify({"error": "max_apply must be an integer"}), 400

    expected_count: Optional[int] = None
    if expected_count_raw is not None:
        try:
            expected_count = int(expected_count_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "expected_count must be an integer"}), 400

    records = _records_snapshot(use_cache=False)
    preview = _build_category_normalization_preview(records, limit=5000)
    preview_by_id = {str(c.get("id", "")).strip(): c for c in preview.get("candidates", []) if str(c.get("id", "")).strip()}

    raw_record_ids = data.get("record_ids", [])
    selected_ids = []
    if isinstance(raw_record_ids, list) and raw_record_ids:
        selected_ids = [str(rid).strip() for rid in raw_record_ids if str(rid).strip()]
    else:
        selected_ids = list(preview_by_id.keys())[:max_apply]

    selected_ids = list(dict.fromkeys(selected_ids))
    selected_ids = [rid for rid in selected_ids if rid in preview_by_id]

    if not selected_ids:
        return jsonify({"error": "No normalization candidates selected"}), 400

    if expected_count is not None and expected_count != len(selected_ids):
        return jsonify({
            "error": "expected_count does not match selected candidate count",
            "expected_count": expected_count,
            "current_count": len(selected_ids),
        }), 409

    max_batch = int(_maintenance_guardrails().get("max_batch_size", 500) or 0)
    if max_batch > 0 and len(selected_ids) > max_batch:
        return jsonify({"error": f"Selected candidates exceed max_batch_size={max_batch}"}), 400

    patches: List[tuple[str, Dict]] = []
    for record_id in selected_ids:
        candidate = preview_by_id[record_id]
        patch = {"Location": candidate.get("proposed_location", "")}
        if set_pending_review:
            patch["Status"] = "pending-review"
        patches.append((record_id, patch))

    result = _bulk_patch_fields(get_client(), patches)
    if result.get("updated", 0) > 0:
        global _records_cache
        _records_cache = None

    payload = {
        "requested": len(selected_ids),
        "updated": result.get("updated", 0),
        "failed": len(result.get("failed_ids", [])),
        "missing": len(result.get("missing_ids", [])),
        "set_pending_review": set_pending_review,
    }

    _maintenance_append_audit("category-normalization-apply", "ok", payload)
    return jsonify(payload)


@app.route("/api/maintenance/checkpoints")
@login_required
def api_maintenance_checkpoints():
    with _MAINTENANCE_STATE_LOCK:
        state = _maintenance_load_state()
        checkpoints = state.get("checkpoints", [])

    rows = []
    for item in checkpoints:
        path = Path(str(item.get("path", "")))
        rows.append({
            "checkpoint_id": item.get("checkpoint_id", ""),
            "name": item.get("name", ""),
            "note": item.get("note", ""),
            "created_at": item.get("created_at", ""),
            "created_by": item.get("created_by", ""),
            "record_count": item.get("record_count", 0),
            "exists": path.exists(),
        })

    rows.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
    return jsonify({"checkpoints": rows})


@app.route("/api/maintenance/checkpoints/create", methods=["POST"])
@login_required
def api_maintenance_checkpoints_create():
    data = request.get_json(force=True) or {}
    name = str(data.get("name", "")).strip() or f"manual-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
    note = str(data.get("note", "")).strip()

    checkpoint = _create_maintenance_checkpoint(name=name, note=note)
    _maintenance_append_audit("checkpoint-create", "ok", checkpoint)
    return jsonify(checkpoint)


@app.route("/api/maintenance/checkpoints/restore", methods=["POST"])
@login_required
def api_maintenance_checkpoints_restore():
    data = request.get_json(force=True) or {}
    checkpoint_id = str(data.get("checkpoint_id", "")).strip()
    confirm_token = str(data.get("confirm_token", "")).strip().upper()

    if not checkpoint_id:
        return jsonify({"error": "checkpoint_id is required"}), 400
    if confirm_token != "PURGE":
        return jsonify({"error": "confirm_token must be PURGE"}), 400
    if not Config.TEST_MODE:
        return jsonify({"error": "Checkpoint restore is only available in TEST_MODE"}), 400

    with _MAINTENANCE_STATE_LOCK:
        state = _maintenance_load_state()
        checkpoints = state.get("checkpoints", [])

    match = None
    for item in checkpoints:
        if str(item.get("checkpoint_id", "")) == checkpoint_id:
            match = item
            break
    if match is None:
        return jsonify({"error": "checkpoint not found"}), 404

    path = Path(str(match.get("path", "")))
    if not path.exists():
        return jsonify({"error": "checkpoint file missing"}), 404

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return jsonify({"error": f"Invalid checkpoint payload: {exc}"}), 500

    checkpoint_records = payload.get("records", [])
    if not isinstance(checkpoint_records, list):
        return jsonify({"error": "checkpoint records payload is invalid"}), 500

    local_client = LocalClient()
    _atomic_json_write(Path(local_client.db_path), checkpoint_records)

    global _records_cache
    _records_cache = None

    restore_result = {
        "checkpoint_id": checkpoint_id,
        "restored_records": len(checkpoint_records),
    }
    _maintenance_append_audit("checkpoint-restore", "ok", restore_result)
    return jsonify(restore_result)


@app.route("/api/maintenance/scheduled-jobs")
@login_required
def api_maintenance_scheduled_jobs():
    with _MAINTENANCE_STATE_LOCK:
        state = _maintenance_load_state()
        jobs = state.get("jobs", {})

    return jsonify({
        "jobs": jobs,
        "available_jobs": [
            {"name": "health_snapshot", "label": "M10 Health Snapshot"},
            {"name": "integrity_scorecard", "label": "M11 Integrity Scorecard"},
            {"name": "aging_drift_scan", "label": "M12 Aging and Drift Scanner"},
        ],
    })


@app.route("/api/maintenance/scheduled-jobs/config", methods=["POST"])
@login_required
def api_maintenance_scheduled_jobs_config():
    data = request.get_json(force=True) or {}
    enabled = _as_bool(data.get("enabled", False))
    interval_raw = str(data.get("interval_minutes", "1440")).strip()
    job_names = data.get("job_names", ["health_snapshot", "integrity_scorecard", "aging_drift_scan"])

    try:
        interval_minutes = max(5, min(int(interval_raw), 10080))
    except ValueError:
        return jsonify({"error": "interval_minutes must be an integer"}), 400

    valid_job_names = {"health_snapshot", "integrity_scorecard", "aging_drift_scan"}
    if not isinstance(job_names, list):
        return jsonify({"error": "job_names must be a list"}), 400
    selected = [str(name).strip() for name in job_names if str(name).strip() in valid_job_names]
    if not selected:
        return jsonify({"error": "At least one valid job_name is required"}), 400

    with _MAINTENANCE_STATE_LOCK:
        state = _maintenance_load_state()
        jobs = state.get("jobs", {})
        jobs["enabled"] = enabled
        jobs["interval_minutes"] = interval_minutes
        jobs["job_names"] = selected
        state["jobs"] = jobs
        _maintenance_save_state(state)

    payload = {
        "enabled": enabled,
        "interval_minutes": interval_minutes,
        "job_names": selected,
    }
    _maintenance_append_audit("scheduled-jobs-config", "ok", payload)
    return jsonify(payload)


@app.route("/api/maintenance/scheduled-jobs/run", methods=["POST"])
@login_required
def api_maintenance_scheduled_jobs_run():
    data = request.get_json(force=True) or {}
    requested_job = str(data.get("job_name", "all")).strip()

    with _MAINTENANCE_STATE_LOCK:
        state = _maintenance_load_state()
        jobs_state = state.get("jobs", {})
        configured_jobs = jobs_state.get("job_names", ["health_snapshot", "integrity_scorecard", "aging_drift_scan"])

    valid_job_names = {"health_snapshot", "integrity_scorecard", "aging_drift_scan"}
    configured_jobs = [name for name in configured_jobs if name in valid_job_names]
    if not configured_jobs:
        configured_jobs = ["health_snapshot", "integrity_scorecard", "aging_drift_scan"]

    if requested_job and requested_job != "all":
        if requested_job not in valid_job_names:
            return jsonify({"error": "Invalid job_name"}), 400
        target_jobs = [requested_job]
    else:
        target_jobs = configured_jobs

    records = _records_snapshot(use_cache=True)
    run_results = []
    for job_name in target_jobs:
        run_results.append(_run_named_maintenance_job(job_name, records))

    with _MAINTENANCE_STATE_LOCK:
        state = _maintenance_load_state()
        jobs = state.get("jobs", {})
        last_runs = jobs.get("last_runs", {})
        now_iso = _now_utc_iso()
        for result in run_results:
            last_runs[result["job"]] = {
                "ran_at": now_iso,
                "ran_by": _maintenance_actor(),
                "summary": result.get("summary", {}),
            }
        jobs["last_runs"] = last_runs
        state["jobs"] = jobs
        _maintenance_save_state(state)

    payload = {"run_results": run_results}
    _maintenance_append_audit("scheduled-jobs-run", "ok", payload)
    return jsonify(payload)


@app.route("/api/maintenance/audit")
@login_required
def api_maintenance_audit():
    limit_raw = request.args.get("limit", "100").strip()
    action_filter = request.args.get("action", "").strip().lower()

    try:
        limit = max(1, min(int(limit_raw), 500))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

    with _MAINTENANCE_STATE_LOCK:
        state = _maintenance_load_state()
        events = list(state.get("audit_trail", []))

    events.sort(key=lambda e: str(e.get("timestamp", "")), reverse=True)
    if action_filter:
        events = [e for e in events if str(e.get("action", "")).strip().lower() == action_filter]

    return jsonify({
        "count": len(events),
        "events": events[:limit],
    })


@app.route("/api/maintenance/guardrails")
@login_required
def api_maintenance_guardrails():
    return jsonify(_maintenance_guardrails())


@app.route("/api/maintenance/guardrails", methods=["POST"])
@login_required
def api_maintenance_guardrails_update():
    data = request.get_json(force=True) or {}
    max_batch_raw = str(data.get("max_batch_size", "500")).strip()
    try:
        max_batch_size = max(1, min(int(max_batch_raw), 10000))
    except ValueError:
        return jsonify({"error": "max_batch_size must be an integer"}), 400

    guardrails = {
        "max_batch_size": max_batch_size,
        "require_preview_for_destructive": _as_bool(data.get("require_preview_for_destructive", True)),
        "two_step_approval_required": _as_bool(data.get("two_step_approval_required", False)),
        "checkpoint_before_destructive": _as_bool(data.get("checkpoint_before_destructive", False)),
    }

    with _MAINTENANCE_STATE_LOCK:
        state = _maintenance_load_state()
        state["guardrails"] = guardrails
        _maintenance_save_state(state)

    _maintenance_append_audit("guardrails-update", "ok", guardrails)
    return jsonify(guardrails)


@app.route("/api/maintenance/approvals")
@login_required
def api_maintenance_approvals_list():
    status_filter = request.args.get("status", "").strip().lower()
    limit_raw = request.args.get("limit", "100").strip()

    try:
        limit = max(1, min(int(limit_raw), 500))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

    with _MAINTENANCE_STATE_LOCK:
        state = _maintenance_load_state()
        approvals = list(state.get("approvals", []))

    approvals.sort(key=lambda a: str(a.get("requested_at", "")), reverse=True)
    if status_filter:
        approvals = [a for a in approvals if str(a.get("status", "")).strip().lower() == status_filter]

    return jsonify({
        "count": len(approvals),
        "approvals": approvals[:limit],
    })


@app.route("/api/maintenance/approvals/request", methods=["POST"])
@login_required
def api_maintenance_approvals_request():
    data = request.get_json(force=True) or {}
    action = str(data.get("action", "")).strip()
    if not action:
        return jsonify({"error": "action is required"}), 400

    record_ids, error_response = _parse_record_ids(data, require_confirm_token=False)
    if error_response:
        return error_response

    expected_count_raw = data.get("expected_count")
    expected_count = None
    if expected_count_raw is not None:
        try:
            expected_count = int(expected_count_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "expected_count must be an integer"}), 400

    if expected_count is not None and expected_count != len(record_ids):
        return jsonify({
            "error": "expected_count does not match record_ids length",
            "expected_count": expected_count,
            "record_count": len(record_ids),
        }), 409

    expires_hours_raw = str(data.get("expires_hours", "24")).strip()
    try:
        expires_hours = max(1, min(int(expires_hours_raw), 168))
    except ValueError:
        return jsonify({"error": "expires_hours must be an integer"}), 400

    token = f"apr_{uuid.uuid4().hex[:12]}"
    now_iso = _now_utc_iso()
    expires_at_epoch = time.time() + (expires_hours * 3600)

    approval = {
        "token": token,
        "action": action,
        "record_count": len(record_ids),
        "record_hash": _maintenance_record_hash(record_ids),
        "requested_by": _maintenance_actor(),
        "requested_at": now_iso,
        "expires_at": datetime.utcfromtimestamp(expires_at_epoch).replace(microsecond=0).isoformat() + "Z",
        "expires_at_epoch": expires_at_epoch,
        "status": "pending",
        "note": str(data.get("note", "")).strip(),
        "approved_by": "",
        "approved_at": "",
    }

    with _MAINTENANCE_STATE_LOCK:
        state = _maintenance_load_state()
        approvals = state.get("approvals", [])
        approvals.append(approval)
        state["approvals"] = approvals[-300:]
        _maintenance_save_state(state)

    _maintenance_append_audit("approval-request", "ok", {
        "token": token,
        "action": action,
        "record_count": len(record_ids),
    })
    return jsonify(approval)


@app.route("/api/maintenance/approvals/approve", methods=["POST"])
@login_required
def api_maintenance_approvals_approve():
    data = request.get_json(force=True) or {}
    token = str(data.get("token", "")).strip()
    decision = str(data.get("decision", "approve")).strip().lower()

    if not token:
        return jsonify({"error": "token is required"}), 400
    if decision not in {"approve", "reject"}:
        return jsonify({"error": "decision must be approve or reject"}), 400

    with _MAINTENANCE_STATE_LOCK:
        state = _maintenance_load_state()
        approvals = state.get("approvals", [])
        target = None
        for item in approvals:
            if str(item.get("token", "")) == token:
                target = item
                break

        if target is None:
            return jsonify({"error": "approval token not found"}), 404
        if str(target.get("status", "")) not in {"pending", "approved", "rejected"}:
            return jsonify({"error": "approval token cannot be updated"}), 409

        target["status"] = "approved" if decision == "approve" else "rejected"
        target["approved_at"] = _now_utc_iso()
        target["approved_by"] = _maintenance_actor()
        target["decision_note"] = str(data.get("note", "")).strip()

        state["approvals"] = approvals[-300:]
        _maintenance_save_state(state)

    _maintenance_append_audit("approval-decision", "ok", {
        "token": token,
        "decision": decision,
    })
    return jsonify(target)


@app.route("/api/tag-library", methods=["GET"])
@login_required
def api_tag_library_get():
    from src.tag_library import TagLibrary
    return jsonify(TagLibrary.instance().get_all())


@app.route("/api/tag-library/suggestions")
@login_required
def api_tag_library_suggestions():
    """Return all ?suggested tags found across existing records."""
    from src.tag_library import TagLibrary
    records = get_all_records()
    suggestions: dict = {}  # tag → count
    _SKIP_STATUSES = {"rejected", "archived"}
    for rec in records:
        fields = rec.get("fields", {})
        if fields.get("Status", "").lower() in _SKIP_STATUSES:
            continue
        for tag in fields.get("Tags", "").split(","):
            tag = tag.strip()
            if tag.startswith("?"):
                suggestions[tag] = suggestions.get(tag, 0) + 1
    lib = TagLibrary.instance()
    return jsonify({"suggestions": [
        {"tag": t, "count": c, "suggested_category": lib.suggest_category(t)}
        for t, c in sorted(suggestions.items())
    ]})


@app.route("/api/tag-library/add", methods=["POST"])
@login_required
def api_tag_library_add():
    data = request.get_json(force=True)
    tag      = data.get("tag", "").strip()
    category = data.get("category", "Custom").strip()
    if not tag:
        return jsonify({"error": "tag required"}), 400
    from src.tag_library import TagLibrary
    TagLibrary.instance().add_tag(tag, category)
    return jsonify({"ok": True})


@app.route("/api/tag-library/remove", methods=["POST"])
@login_required
def api_tag_library_remove():
    data = request.get_json(force=True)
    tag = data.get("tag", "").strip()
    if not tag:
        return jsonify({"error": "tag required"}), 400
    from src.tag_library import TagLibrary
    found = TagLibrary.instance().remove_tag(tag)
    return jsonify({"ok": found})


@app.route("/api/tag-library/discard-suggestion", methods=["POST"])
@login_required
def api_tag_library_discard_suggestion():
    """Remove a ?suggested tag from all records (without promoting it)."""
    data = request.get_json(force=True)
    tag = data.get("tag", "").strip()
    if not tag:
        return jsonify({"error": "tag required"}), 400

    # Normalise — accept with or without ?
    suggested = tag if tag.startswith("?") else f"?{tag}"

    client = get_client()
    records = client.get_all_records()
    patches: list[tuple[str, dict]] = []

    for rec in records:
        rec_id = str(rec.get("id", "")).strip()
        raw_tags = rec.get("fields", {}).get("Tags", "") or ""
        tag_list = [t.strip() for t in raw_tags.split(",") if t.strip()]
        new_tags = [t for t in tag_list if t != suggested]
        if len(new_tags) != len(tag_list) and rec_id:
            patches.append((rec_id, {"Tags": ", ".join(new_tags)}))

    if patches:
        client.bulk_patch_fields(patches)
        global _records_cache
        _records_cache = None

    return jsonify({"ok": True, "records_updated": len(patches)})


def _clear_promoted_suggestions_from_records(promoted_tags: list[str]) -> int:
    """Replace ?prefixed promoted tags in records with approved tag names."""
    clean_promoted = {
        str(tag or "").lstrip("?").strip().lower()
        for tag in promoted_tags
        if str(tag or "").strip()
    }
    if not clean_promoted:
        return 0

    client = get_client()
    records = client.get_all_records()
    patches: list[tuple[str, dict]] = []

    for rec in records:
        rec_id = str(rec.get("id", "")).strip()
        raw_tags = rec.get("fields", {}).get("Tags", "") or ""
        tag_list = [t.strip() for t in raw_tags.split(",") if t.strip()]
        new_tags = []
        changed = False

        for t in tag_list:
            clean = t.lstrip("?").strip().lower()
            if t.startswith("?") and clean in clean_promoted:
                new_tags.append(clean)
                changed = True
            else:
                new_tags.append(t)

        if changed and rec_id:
            patches.append((rec_id, {"Tags": ", ".join(new_tags)}))

    if patches:
        client.bulk_patch_fields(patches)
        global _records_cache
        _records_cache = None

    return len(patches)


@app.route("/api/tag-library/promote", methods=["POST"])
@login_required
def api_tag_library_promote():
    """Promote a ?suggested tag to an approved tag."""
    data     = request.get_json(force=True)
    tag      = data.get("tag", "").strip()
    category = data.get("category", "Custom").strip()
    if not tag:
        return jsonify({"error": "tag required"}), 400
    from src.tag_library import TagLibrary
    TagLibrary.instance().promote_suggestion(tag, category)
    TagLibrary.instance().invalidate_cache()

    _clear_promoted_suggestions_from_records([tag])

    return jsonify({"ok": True})


@app.route("/api/tag-library/promote-bulk", methods=["POST"])
@login_required
def api_tag_library_promote_bulk():
    """Promote multiple ?suggested tags to approved tags (admin bulk action)."""
    if not _is_maintenance_admin_user(session.get("user", {})):
        return jsonify({"error": "Admin access required"}), 403
    data = request.get_json(force=True)
    tags = data.get("tags", [])  # list of {tag, category}
    if not tags or not isinstance(tags, list):
        return jsonify({"error": "tags list required"}), 400
    from src.tag_library import TagLibrary
    lib = TagLibrary.instance()
    promoted_tags: list[str] = []  # clean tag names (no ?)
    for item in tags:
        tag = (item.get("tag") or "").strip()
        category = (item.get("category") or "Custom").strip()
        if tag:
            lib.promote_suggestion(tag, category)
            promoted_tags.append(tag.lstrip("?").strip().lower())
    lib.invalidate_cache()

    _clear_promoted_suggestions_from_records(promoted_tags)

    return jsonify({"ok": True, "promoted": len(promoted_tags)})


# ------------------------------------------------------------------
# Feature 2: Catalog external images via upload
# ------------------------------------------------------------------

_UPLOAD_ALLOWED = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _staged_root() -> Path:
    """Return the directory used to stage uploaded files between requests."""
    if Path("/home").exists() and os.getenv("WEBSITE_INSTANCE_ID"):
        root = Path("/home/proxima_staged_uploads")
    else:
        root = Path(tempfile.gettempdir()) / "proxima_staged_uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _staged_lookup(file_id: str) -> Optional[Dict[str, str]]:
    """Find a staged temp file by ID from the filesystem (worker-safe)."""
    matches = list(_staged_root().glob(f"proxima_{file_id}__*"))
    if not matches:
        return None
    path = matches[0]
    # Filename is encoded after the double-underscore separator
    filename = path.name[len(f"proxima_{file_id}__"):]
    return {"path": str(path), "filename": filename}


def _staged_save(file_id: str, original_name: str, data: bytes) -> str:
    """Write file bytes to a temp path encoding the original name in the filename."""
    tmp_path = _staged_root() / f"proxima_{file_id}__{original_name}"
    tmp_path.write_bytes(data)
    return str(tmp_path)


@app.route("/upload")
@login_required
def upload():
    is_admin = _is_maintenance_admin_user(session.get("user", {}))
    return render_template(
        "upload.html",
        is_admin=is_admin,
        admin_max_request_mb=Config.ADMIN_MAX_UPLOAD_BYTES // (1024 * 1024),
        max_request_mb=Config.MAX_REQUEST_BYTES // (1024 * 1024),
    )


@app.route("/debug/config", methods=["GET"])
def debug_config():
    """DEBUG ONLY: Return current config values."""
    if not (Config.TEST_MODE and Config.DEV_AUTH_BYPASS):
        return jsonify({"error": "Not in bypass mode"}), 403
    return jsonify({
        "TEST_MODE": Config.TEST_MODE,
        "DEV_AUTH_BYPASS": Config.DEV_AUTH_BYPASS,
        "_auth_bypass_enabled": _auth_bypass_enabled(),
        "MAX_UPLOAD_BYTES_MB": Config.MAX_UPLOAD_BYTES / (1024*1024),
        "ADMIN_MAX_UPLOAD_BYTES_MB": Config.ADMIN_MAX_UPLOAD_BYTES / (1024*1024),
        "session_user": session.get("user"),
        "is_admin": _is_maintenance_admin_user(session.get("user", {})),
    })


@app.route("/api/upload/stage", methods=["POST"])
@login_required
def api_upload_stage():
    """Receive multipart file upload, save to temp, return staged IDs."""
    files = request.files.getlist("files")
    if not files or all(f.filename == "" for f in files):
        return jsonify({"error": "No files provided"}), 400

    # Check if user is admin for higher upload limit
    user_claims = session.get("user", {})
    is_admin = _is_maintenance_admin_user(user_claims)
    max_file_size = Config.ADMIN_MAX_UPLOAD_BYTES if is_admin else Config.MAX_UPLOAD_BYTES

    staged = []
    for f in files:
        original_name = secure_filename(f.filename or "upload")
        ext = Path(original_name).suffix.lower()
        if ext not in _UPLOAD_ALLOWED:
            staged.append({"error": f"Unsupported format: {ext or '(none)'}", "filename": original_name})
            continue

        file_bytes = f.read()
        try:
            _validate_image_payload(file_bytes, original_name, max_bytes=max_file_size)
        except ValueError as exc:
            staged.append({"error": str(exc), "filename": original_name})
            continue

        file_id = uuid.uuid4().hex
        _staged_save(file_id, original_name, file_bytes)
        staged.append({"id": file_id, "filename": original_name})

    return jsonify({"staged": staged})


@app.route("/api/upload/process")
@login_required
def api_upload_process():
    """SSE stream — run the full pipeline for one staged file."""
    file_id = request.args.get("id", "")
    category = request.args.get("category", "") or None  # None = AI determines it

    from src.image_processor import CATEGORIES, process_image
    staged = _staged_lookup(file_id)
    if staged is None:
        def expired_stream():
            yield "data: [START]\n\n"
            yield "data: [ERROR] Unknown or expired file ID\n\n"

        return Response(
            stream_with_context(expired_stream()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    if category is not None and category not in CATEGORIES:
        def invalid_category_stream():
            yield "data: [START]\n\n"
            yield f"data: [ERROR] Invalid category. Must be one of: {CATEGORIES}\n\n"

        return Response(
            stream_with_context(invalid_category_stream()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def generate():
        yield "data: [START]\n\n"
        q: queue.Queue = queue.Queue()

        def worker():
            try:
                file_bytes = Path(staged["path"]).read_bytes()
                try:
                    os.unlink(staged["path"])
                except OSError:
                    pass

                from src.ai_generator import AltTextGenerator
                gen = AltTextGenerator()

                if Config.TEST_MODE:
                    list_client = LocalClient()
                    sp_client = None
                    storage_mode = "local"
                else:
                    from src.sharepoint_list_client import SharePointListClient
                    from src.sharepoint_client import SharePointClient
                    list_client = SharePointListClient()
                    sp_client = SharePointClient()
                    storage_mode = "sharepoint"

                result = process_image(
                    file_bytes=file_bytes,
                    original_filename=staged["filename"],
                    generator=gen,
                    list_client=list_client,
                    sp_client=sp_client,
                    image_folder=Config.IMAGE_FOLDER,
                    storage_mode=storage_mode,
                    on_progress=lambda msg: q.put(("progress", msg)),
                    category=category,
                    source="Internal",
                )
                q.put(("done", result))
            except Exception:
                q.put(("error", "Processing failed"))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        yield from _sse_poll_loop(t, q, 180, "Processing timed out after 3 minutes",
                                  invalidate_cache=True)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Shutterstock quota tracking
# ---------------------------------------------------------------------------

SS_QUOTA_LIMIT = 10
# /home persists on Azure App Service; fall back to project root locally
_SS_COUNTER_PATH = Path(
    "/home/proxima_ss_counter.json"
    if Path("/home").exists() and os.getenv("WEBSITE_INSTANCE_ID")
    else "proxima_ss_counter.json"
)
_ss_lock = threading.Lock()


def _ss_read() -> dict:
    """Return {month: 'YYYY-MM', count: int}."""
    try:
        if _SS_COUNTER_PATH.exists():
            return json.loads(_SS_COUNTER_PATH.read_text())
    except Exception:
        pass
    return {"month": "", "count": 0}


def _ss_write(data: dict) -> None:
    _SS_COUNTER_PATH.write_text(json.dumps(data, ensure_ascii=False))


@app.route("/api/shutterstock/quota")
@login_required
def api_ss_quota():
    with _ss_lock:
        data = _ss_read()
        current_month = time.strftime("%Y-%m")
        if data.get("month") != current_month:
            data = {"month": current_month, "count": 0}
    return jsonify({"used": data["count"], "limit": SS_QUOTA_LIMIT, "month": data["month"]})


@app.route("/api/shutterstock/track", methods=["POST"])
@login_required
def api_ss_track():
    with _ss_lock:
        data = _ss_read()
        current_month = time.strftime("%Y-%m")
        if data.get("month") != current_month:
            data = {"month": current_month, "count": 0}
        data["count"] += 1
        _ss_write(data)
    return jsonify({"used": data["count"], "limit": SS_QUOTA_LIMIT})


_STOCK_API_PROVIDERS = {
    "pexels": {
        "label": "Pexels",
        "keys": ["PEXELS_API_KEY"],
    },
    "shutterstock": {
        "label": "Shutterstock",
        "keys": ["SHUTTERSTOCK_CLIENT_ID", "SHUTTERSTOCK_CLIENT_SECRET"],
    },
    "unsplash": {
        "label": "Unsplash",
        "keys": ["UNSPLASH_ACCESS_KEY"],
    },
    "pixabay": {
        "label": "Pixabay",
        "keys": ["PIXABAY_API_KEY"],
    },
}

_DOT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _mask(value: str) -> str:
    """Return a masked version of a secret key for display."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def _test_stock_provider(provider: str) -> dict:
    """Ping a stock photo provider with a minimal test call. Returns {ok, status, message}."""
    import base64
    phrase = "people"
    try:
        if provider == "pexels":
            key = os.getenv("PEXELS_API_KEY", "")
            if not key:
                return {"ok": False, "status": "missing", "message": "PEXELS_API_KEY not set"}
            r = requests.get(
                "https://api.pexels.com/v1/search",
                params={"query": phrase, "per_page": 1},
                headers={"Authorization": key},
                timeout=8,
            )
            if r.status_code == 401:
                return {"ok": False, "status": "auth_failed", "message": "401 — key rejected"}
            if r.status_code == 200:
                return {"ok": True, "status": "ok", "message": "OK"}
            return {"ok": False, "status": "error", "message": f"HTTP {r.status_code}"}

        elif provider == "shutterstock":
            cid = os.getenv("SHUTTERSTOCK_CLIENT_ID", "")
            csec = os.getenv("SHUTTERSTOCK_CLIENT_SECRET", "")
            if not cid or not csec:
                return {"ok": False, "status": "missing", "message": "SHUTTERSTOCK_CLIENT_ID / SHUTTERSTOCK_CLIENT_SECRET not set"}
            credentials = base64.b64encode(f"{cid}:{csec}".encode()).decode()
            r = requests.get(
                "https://api.shutterstock.com/v2/images/search",
                params={"query": phrase, "per_page": 1},
                headers={"Authorization": f"Basic {credentials}"},
                timeout=8,
            )
            if r.status_code == 401:
                return {"ok": False, "status": "auth_failed", "message": "401 — credentials rejected"}
            if r.status_code == 200:
                return {"ok": True, "status": "ok", "message": "OK"}
            return {"ok": False, "status": "error", "message": f"HTTP {r.status_code}"}

        elif provider == "unsplash":
            key = os.getenv("UNSPLASH_ACCESS_KEY", "")
            if not key:
                return {"ok": False, "status": "missing", "message": "UNSPLASH_ACCESS_KEY not set"}
            r = requests.get(
                "https://api.unsplash.com/search/photos",
                params={"query": phrase, "per_page": 1},
                headers={"Authorization": f"Client-ID {key}"},
                timeout=8,
            )
            if r.status_code == 401:
                return {"ok": False, "status": "auth_failed", "message": "401 — key rejected"}
            if r.status_code == 200:
                return {"ok": True, "status": "ok", "message": "OK"}
            return {"ok": False, "status": "error", "message": f"HTTP {r.status_code}"}

        elif provider == "pixabay":
            key = os.getenv("PIXABAY_API_KEY", "")
            if not key:
                return {"ok": False, "status": "missing", "message": "PIXABAY_API_KEY not set"}
            r = requests.get(
                "https://pixabay.com/api/",
                params={"key": key, "q": phrase, "per_page": 3},
                timeout=8,
            )
            if r.status_code == 400 and "API key" in (r.text or ""):
                return {"ok": False, "status": "auth_failed", "message": "400 — key rejected"}
            if r.status_code == 200:
                return {"ok": True, "status": "ok", "message": "OK"}
            return {"ok": False, "status": "error", "message": f"HTTP {r.status_code}"}

        return {"ok": False, "status": "error", "message": "Unknown provider"}
    except requests.Timeout:
        return {"ok": False, "status": "error", "message": "Request timed out"}
    except Exception as exc:
        return {"ok": False, "status": "error", "message": "Request failed"}


def _update_env_key(key: str, value: str) -> None:
    """Update or append a key=value line in the .env file."""
    if not _DOT_ENV_PATH.exists():
        _DOT_ENV_PATH.write_text(f"{key}={value}\n", encoding="utf-8")
        return
    lines = _DOT_ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    pattern = re.compile(rf"^{re.escape(key)}\s*=")
    replaced = False
    new_lines = []
    for line in lines:
        if pattern.match(line):
            new_lines.append(f"{key}={value}\n")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f"{key}={value}\n")
    _DOT_ENV_PATH.write_text("".join(new_lines), encoding="utf-8")


@app.route("/api/maintenance/stock-api-status")
@login_required
def api_maintenance_stock_api_status():
    results = {}
    for provider, meta in _STOCK_API_PROVIDERS.items():
        keys_display = {k: _mask(os.getenv(k, "")) for k in meta["keys"]}
        test = _test_stock_provider(provider)
        results[provider] = {
            "label": meta["label"],
            "keys": keys_display,
            **test,
        }
    return jsonify({"providers": results})


@app.route("/api/maintenance/stock-api-update", methods=["POST"])
@login_required
def api_maintenance_stock_api_update():
    data = request.get_json(force=True) or {}
    provider = str(data.get("provider", "")).strip()
    updates = data.get("keys", {})

    if provider not in _STOCK_API_PROVIDERS:
        return jsonify({"error": "Unknown provider"}), 400
    allowed_keys = set(_STOCK_API_PROVIDERS[provider]["keys"])

    saved = []
    for key, value in updates.items():
        if key not in allowed_keys:
            return jsonify({"error": f"Key {key!r} not valid for provider {provider!r}"}), 400
        value = str(value).strip()
        if not value:
            return jsonify({"error": f"Value for {key!r} cannot be empty"}), 400
        os.environ[key] = value
        _update_env_key(key, value)
        saved.append(key)

    test = _test_stock_provider(provider)
    return jsonify({"saved": saved, "test": test})


# Start background ingest poller (SharePoint mode only)
if Config.STORAGE_MODE == "sharepoint" and Config.SHAREPOINT_INGEST_FOLDER:
    ingest_poller.start(Config.INGEST_POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    app.run(debug=True, port=5000)
