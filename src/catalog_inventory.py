"""Read-only inventory for hero-derivative eligibility and metadata gaps."""

import concurrent.futures
from io import BytesIO
from pathlib import Path, PurePosixPath

from PIL import Image as PILImage
from PIL import ImageOps

from src.config import Config
from src.image_metadata import read_metadata


def _dimensions(raw: bytes) -> tuple[int, int]:
    with PILImage.open(BytesIO(raw)) as image:
        return ImageOps.exif_transpose(image).size


def build_inventory(records: list[dict], *, storage_mode: str, image_folder: str, sp_client=None) -> dict:
    """Inspect catalog originals without creating or modifying any asset."""
    report = {
        "dryRun": True,
        "summary": {
            "records": len(records),
            "heroEligible": 0,
            "storedOriginalOnly1600": 0,
            "standardOnly1601To2559": 0,
            "below1600": 0,
            "missingOriginal": 0,
            "metadataGaps": 0,
        },
        "heroEligible": [],
        "storedOriginalOnly1600": [],
        "standardOnly1601To2559": [],
        "below1600": [],
        "missingOriginal": [],
        "metadataGaps": [],
    }
    root = (Config.SHAREPOINT_IMAGE_FOLDER or "").strip().strip("/")
    known_sidecars: set[str] = set()
    if storage_mode == "sharepoint":
        try:
            folder = f"{root}/Metadata" if root else "Metadata"
            known_sidecars = {
                PurePosixPath(item.get("name", "")).stem
                for item in sp_client.list_folder(folder)
                if item.get("name", "").lower().endswith(".json")
            }
        except Exception:
            known_sidecars = set()
    else:
        metadata_dir = Path(image_folder) / "Metadata"
        if metadata_dir.is_dir():
            known_sidecars = {path.stem for path in metadata_dir.glob("*.json")}

    sharepoint_measurements: dict[str, tuple[int, int, int] | Exception] = {}
    if storage_mode == "sharepoint":
        locations = {
            str(record.get("fields", {}).get("High-Res Location", "") or "").strip()
            for record in records
        }
        locations.discard("")

        def measure_sharepoint(location: str) -> tuple[int, int, int]:
            path = f"{root}/High-Res/{location}" if root else f"High-Res/{location}"
            file_meta = sp_client.get_file_metadata(path)
            image_meta = file_meta.get("image", {})
            width = int(image_meta.get("width", 0) or 0)
            height = int(image_meta.get("height", 0) or 0)
            file_size = int(file_meta.get("size", 0) or 0)
            if width <= 0 or height <= 0:
                raw = sp_client.get_file_bytes(path)
                width, height = _dimensions(raw)
                file_size = len(raw)
            return width, height, file_size

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            future_map = {
                executor.submit(measure_sharepoint, location): location
                for location in locations
            }
            for future in concurrent.futures.as_completed(future_map):
                location = future_map[future]
                try:
                    sharepoint_measurements[location] = future.result()
                except Exception as exc:
                    sharepoint_measurements[location] = exc

    for record in records:
        fields = record.get("fields", {})
        item = {
            "id": str(record.get("id", "")),
            "slug": str(fields.get("Slug", "") or ""),
            "filename": str(fields.get("Filename", "") or ""),
            "location": str(fields.get("Location", "") or ""),
            "highResLocation": str(fields.get("High-Res Location", "") or ""),
            "status": str(fields.get("Status", "") or ""),
            "source": str(fields.get("Source", "") or ""),
        }
        metadata = read_metadata(
            item["slug"],
            storage_mode=storage_mode,
            image_folder=image_folder,
            sp_client=sp_client,
        ) if item["slug"] in known_sidecars else {}
        original_meta = metadata.get("original", {}) if isinstance(metadata, dict) else {}
        gaps = []
        if not (fields.get("Source") or original_meta.get("provider")):
            gaps.append("source")
        if not (original_meta.get("attribution") or original_meta.get("license")):
            gaps.append("rights")
        focal = metadata.get("focalPoint") if isinstance(metadata, dict) else None
        if not isinstance(focal, dict) or not all(key in focal for key in ("x", "y")):
            gaps.append("focalPoint")
        if gaps:
            report["metadataGaps"].append({**item, "missing": gaps})

        location = item["highResLocation"]
        if not location:
            report["missingOriginal"].append({**item, "reason": "no High-Res Location"})
            continue
        try:
            if storage_mode == "sharepoint":
                measurement = sharepoint_measurements[location]
                if isinstance(measurement, Exception):
                    raise measurement
                width, height, file_size = measurement
            else:
                raw = (Path(image_folder) / "High-Res" / PurePosixPath(location)).read_bytes()
                width, height = _dimensions(raw)
                file_size = len(raw)
        except Exception as exc:
            report["missingOriginal"].append({**item, "reason": str(exc) or "unreadable original"})
            continue

        measured = {
            **item,
            "width": width,
            "height": height,
            "fileSize": file_size,
            "metadataPresent": bool(metadata),
            "heroPresent": bool(metadata.get("hero")) if isinstance(metadata, dict) else False,
            "qualityTier": (
                "hero-ready" if width >= 2560
                else "standard-only" if width >= 1600
                else "low-resolution"
            ),
        }
        measured["backfillNeeded"] = (
            not measured["metadataPresent"]
            or (width >= 2560 and not measured["heroPresent"])
        )
        if width >= 2560:
            report["heroEligible"].append(measured)
        elif width == 1600:
            report["storedOriginalOnly1600"].append(measured)
        elif width < 1600:
            report["below1600"].append(measured)
        else:
            report["standardOnly1601To2559"].append(measured)

    for key in (
        "heroEligible", "storedOriginalOnly1600", "standardOnly1601To2559",
        "below1600", "missingOriginal", "metadataGaps",
    ):
        report["summary"][key] = len(report[key])
    return report


def inventory_current_catalog() -> dict:
    """Load the configured catalog and run the read-only inventory."""
    if Config.TEST_MODE:
        from src.local_client import LocalClient
        client = LocalClient()
        return build_inventory(
            client.get_all_records(),
            storage_mode="local",
            image_folder=Config.IMAGE_FOLDER,
        )

    from src.sharepoint_client import SharePointClient
    from src.sharepoint_list_client import SharePointListClient
    sp_client = SharePointClient()
    return build_inventory(
        SharePointListClient().get_all_records(),
        storage_mode="sharepoint",
        image_folder=Config.IMAGE_FOLDER,
        sp_client=sp_client,
    )
