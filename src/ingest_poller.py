"""Background poller — watches SHAREPOINT_INGEST_FOLDER and auto-processes new images."""

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Callable, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOG_MAX = 500
_SP_LOG_PATH = "Config/ingest_log.json"

# Rolling in-memory log — last 500 entries, thread-safe via _log_lock
_log: Deque[Dict] = deque(maxlen=_LOG_MAX)
_log_lock = threading.Lock()
_log_loaded = False  # load from SP once on first use

# Prevent concurrent runs
_run_lock = threading.Lock()

_poller_thread: Optional[threading.Thread] = None


def _sp_client():
    from src.config import Config
    if Config.STORAGE_MODE != "sharepoint":
        return None
    from src.sharepoint_client import SharePointClient
    return SharePointClient()


def _load_log_from_sp() -> None:
    global _log_loaded
    sp = _sp_client()
    if not sp:
        _log_loaded = True
        return
    try:
        raw = sp.get_file_bytes(_SP_LOG_PATH)
        entries = json.loads(raw)
        if isinstance(entries, list):
            with _log_lock:
                _log.clear()
                for e in entries[-_LOG_MAX:]:
                    _log.append(e)
    except Exception:
        pass  # no log file yet — start fresh
    _log_loaded = True


def _flush_log_to_sp() -> None:
    sp = _sp_client()
    if not sp:
        return
    try:
        with _log_lock:
            entries = list(_log)
        sp.upload_file("Config", "ingest_log.json",
                       json.dumps(entries, indent=2).encode())
    except Exception as exc:
        logger.warning("[ingest-poller] Could not save log to SharePoint: %s", exc)


def get_log() -> List[Dict]:
    if not _log_loaded:
        _load_log_from_sp()
    with _log_lock:
        return list(_log)


def _emit(level: str, msg: str) -> None:
    if not _log_loaded:
        _load_log_from_sp()
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "level": level, "msg": msg}
    with _log_lock:
        _log.append(entry)
    log_fn = getattr(logger, level if level != "warning" else "warning", logger.info)
    log_fn("[ingest-poller] %s", msg)


def _run_once(on_progress: Optional[Callable[[str], None]] = None) -> Dict:
    """Scan ingest folder, process new files, verify, delete originals.

    Returns a summary dict.
    """
    from src.ai_generator import AltTextGenerator
    from src.config import Config
    from src.image_processor import normalize_source, process_image
    from src.sharepoint_client import SharePointClient
    from src.sharepoint_list_client import SharePointListClient

    def progress(msg: str) -> None:
        _emit("info", msg)
        if on_progress:
            on_progress(msg)

    sp_ingest_folder = (Config.SHAREPOINT_INGEST_FOLDER or "").strip()
    if not sp_ingest_folder:
        _emit("error", "SHAREPOINT_INGEST_FOLDER is not configured — poller disabled")
        return {"skipped": 0, "processed": 0, "failed": 0, "deleted": 0}

    sp_client = SharePointClient()
    list_client = SharePointListClient()

    formats = set((Config.SUPPORTED_FORMATS or [".jpg", ".jpeg", ".png", ".gif", ".webp"]))

    # Scan ingest folder
    candidates = sp_client.list_all_images(sp_ingest_folder)
    candidates = [
        (sp_path, rel)
        for sp_path, rel in candidates
        if PurePosixPath(sp_path).suffix.lower() in formats
    ]

    if not candidates:
        progress("Ingest folder is empty — nothing to do")
        _flush_log_to_sp()
        return {"skipped": 0, "processed": 0, "failed": 0, "deleted": 0}

    # Build set of filenames already in the library
    records = list_client.get_all_records()
    existing: set = set()
    for rec in records:
        fields = rec.get("fields", {})
        for field in ("High-Res Location", "Location"):
            val = str(fields.get(field, "") or "").strip()
            if val:
                existing.add(PurePosixPath(val).name)
        ingest_src = str(fields.get("Ingest Source", "") or "").strip()
        if ingest_src:
            existing.add(ingest_src)

    new_files = []
    already_in_library = []
    for sp_path, rel in candidates:
        if PurePosixPath(sp_path).name in existing:
            already_in_library.append(sp_path)
        else:
            new_files.append((sp_path, rel))

    # Delete duplicates from ingest folder
    for sp_path in already_in_library:
        filename = PurePosixPath(sp_path).name
        try:
            sp_client.delete_file(sp_path)
            _emit("info", f"Deleted duplicate from ingest folder: {filename}")
        except Exception as exc:
            _emit("warning", f"Could not delete duplicate {filename}: {exc}")

    skipped = len(already_in_library)

    if not new_files:
        progress(f"Scanned {len(candidates)} file(s) — all already in library ({skipped} deleted from ingest folder)")
        _flush_log_to_sp()
        return {"skipped": skipped, "processed": 0, "failed": 0, "deleted": skipped}

    progress(f"Found {len(candidates)} file(s) — {len(new_files)} new, {skipped} duplicates removed")

    gen = AltTextGenerator()
    processed = 0
    failed = 0
    deleted = 0
    total = len(new_files)

    # Build slug filename set once to avoid O(N) record_exists calls per image
    existing_filenames: set = {
        str(r.get("fields", {}).get("Filename", ""))
        for r in records
        if r.get("fields", {}).get("Filename")
    }

    for idx, (sp_path, _rel) in enumerate(new_files, 1):
        filename = PurePosixPath(sp_path).name
        progress(f"[{idx}/{total}] Processing {filename}")
        try:
            file_bytes = sp_client.get_file_bytes(sp_path)
            result = process_image(
                file_bytes=file_bytes,
                original_filename=filename,
                generator=gen,
                list_client=list_client,
                sp_client=sp_client,
                image_folder=Config.IMAGE_FOLDER,
                storage_mode="sharepoint",
                on_progress=lambda msg: progress(f"    {msg}"),
                source="Internal",
                initial_status="ingested",
                ingest_source=filename,
                existing_filenames=existing_filenames,
            )

            # Verify: confirm the WebP file exists in SharePoint
            verified = False
            location = str(result.get("location") or result.get("high_res_location") or "").strip()
            if not location:
                _emit("error", f"[ERROR] {filename}: process_image returned no location — source kept, record may be incomplete")
                failed += 1
                continue
            if location:
                root = (Config.IMAGE_FOLDER or "").strip().strip("/")
                webp_path = f"{root}/WebP/{location}" if root else f"WebP/{location}"
                try:
                    sp_client.get_file_metadata(webp_path)
                    verified = True
                except Exception:
                    verified = False

            if verified:
                progress(f"    Verified — deleting source {filename}")
                try:
                    sp_client.delete_file(sp_path)
                    deleted += 1
                except Exception as del_exc:
                    _emit("warning", f"    Could not delete source {filename}: {del_exc}")
            else:
                _emit("warning", f"    Verification failed for {filename} — source kept for retry")
                failed += 1
                continue

            processed += 1

        except Exception as exc:
            failed += 1
            _emit("error", f"[ERROR] {filename}: {exc}")

    progress(f"Done — processed {processed}, failed/retry {failed}, deleted {deleted}")
    result = {"skipped": skipped, "processed": processed, "failed": failed, "deleted": deleted}
    _flush_log_to_sp()
    return result


def _poll_loop(interval_seconds: int) -> None:
    _emit("info", f"Poller started — interval {interval_seconds}s")
    while True:
        time.sleep(interval_seconds)
        if not _run_lock.acquire(blocking=False):
            _emit("info", "Previous run still in progress — skipping this cycle")
            continue
        try:
            _run_once()
        except Exception as exc:
            _emit("error", f"Unexpected poller error: {exc}")
        finally:
            _run_lock.release()


def start(interval_seconds: int = 300) -> None:
    """Start the background poller thread. Safe to call multiple times — only starts once."""
    global _poller_thread
    if _poller_thread is not None and _poller_thread.is_alive():
        return
    _poller_thread = threading.Thread(
        target=_poll_loop,
        args=(interval_seconds,),
        daemon=True,
        name="ingest-poller",
    )
    _poller_thread.start()


def run_now(on_progress: Optional[Callable[[str], None]] = None) -> Dict:
    """Trigger an immediate run (used by the manual SSE endpoint). Blocks until done."""
    if not _run_lock.acquire(blocking=False):
        return {"error": "A run is already in progress"}
    try:
        return _run_once(on_progress=on_progress)
    finally:
        _run_lock.release()
