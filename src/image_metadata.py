"""Sidecar metadata for additive image-delivery features.

The legacy SharePoint List schema remains unchanged. New ingestion writes one
JSON document per slug under Metadata/, which keeps current MCP/API consumers
working while allowing richer derivative, rights, and focal-point metadata.
"""

import json
import re
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Optional

from src.config import Config


_SIDECAR_CACHE: dict[str, tuple[float, set[str]]] = {}
_SIDECAR_CACHE_LOCK = threading.Lock()
_SIDECAR_CACHE_SECONDS = 60


def metadata_location(slug: str) -> str:
    safe_slug = str(slug or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", safe_slug):
        raise ValueError("A valid slug is required")
    return f"Metadata/{safe_slug}.json"


def write_metadata(
    metadata: dict,
    *,
    storage_mode: str,
    image_folder: str,
    sp_client=None,
) -> str:
    slug = str(metadata.get("slug", "")).strip()
    relative = metadata_location(slug)
    content = json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8")
    if storage_mode == "sharepoint" and sp_client is not None:
        root = (Config.SHAREPOINT_IMAGE_FOLDER or "").strip().strip("/")
        folder = f"{root}/Metadata" if root else "Metadata"
        sp_client.upload_file(folder, f"{slug}.json", content)
    else:
        path = Path(image_folder) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    with _SIDECAR_CACHE_LOCK:
        _SIDECAR_CACHE.clear()
    return relative


def metadata_exists(
    slug: str,
    *,
    storage_mode: Optional[str] = None,
    image_folder: Optional[str] = None,
    sp_client=None,
) -> bool:
    """Check sidecar availability with one cached folder listing in SharePoint."""
    safe_slug = str(slug or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", safe_slug):
        return False
    mode = storage_mode or Config.STORAGE_MODE
    folder = image_folder or Config.IMAGE_FOLDER
    if mode != "sharepoint":
        return (Path(folder) / metadata_location(safe_slug)).is_file()

    root = (Config.SHAREPOINT_IMAGE_FOLDER or "").strip().strip("/")
    cache_key = f"sharepoint:{root}"
    now = time.monotonic()
    with _SIDECAR_CACHE_LOCK:
        cached = _SIDECAR_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return safe_slug in cached[1]
    try:
        if sp_client is None:
            from src.sharepoint_client import SharePointClient
            sp_client = SharePointClient()
        metadata_folder = f"{root}/Metadata" if root else "Metadata"
        slugs = {
            PurePosixPath(item.get("name", "")).stem
            for item in sp_client.list_folder(metadata_folder)
            if item.get("name", "").lower().endswith(".json")
        }
    except Exception:
        slugs = set()
    with _SIDECAR_CACHE_LOCK:
        _SIDECAR_CACHE[cache_key] = (now + _SIDECAR_CACHE_SECONDS, slugs)
    return safe_slug in slugs


def read_metadata(
    slug: str,
    *,
    storage_mode: Optional[str] = None,
    image_folder: Optional[str] = None,
    sp_client=None,
) -> dict:
    mode = storage_mode or Config.STORAGE_MODE
    folder = image_folder or Config.IMAGE_FOLDER
    relative = metadata_location(slug)
    try:
        if not metadata_exists(
            slug,
            storage_mode=mode,
            image_folder=folder,
            sp_client=sp_client,
        ):
            return {}
        if mode == "sharepoint":
            if sp_client is None:
                from src.sharepoint_client import SharePointClient
                sp_client = SharePointClient()
            root = (Config.SHAREPOINT_IMAGE_FOLDER or "").strip().strip("/")
            path = f"{root}/{relative}" if root else relative
            raw = sp_client.get_file_bytes(path)
        else:
            raw = (Path(folder) / relative).read_bytes()
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    except Exception:
        # Missing sidecars are normal for the pre-upgrade catalog. Do not let
        # them make legacy search/API responses fail.
        return {}


def update_focal_point(
    slug: str,
    x: float,
    y: float,
    *,
    storage_mode: Optional[str] = None,
    image_folder: Optional[str] = None,
    sp_client=None,
) -> dict:
    if not 0 <= x <= 1 or not 0 <= y <= 1:
        raise ValueError("focal point x and y must each be between 0 and 1")
    mode = storage_mode or Config.STORAGE_MODE
    folder = image_folder or Config.IMAGE_FOLDER
    metadata = read_metadata(
        slug,
        storage_mode=mode,
        image_folder=folder,
        sp_client=sp_client,
    )
    if not metadata:
        raise FileNotFoundError(f"No metadata exists for slug '{slug}'")
    metadata["focalPoint"] = {"x": float(x), "y": float(y)}
    write_metadata(
        metadata,
        storage_mode=mode,
        image_folder=folder,
        sp_client=sp_client,
    )
    return metadata
