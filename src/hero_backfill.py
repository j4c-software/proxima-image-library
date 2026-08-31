"""Selective, non-destructive backfill of hero derivatives and metadata sidecars."""

from io import BytesIO
from pathlib import Path, PurePosixPath

from PIL import Image as PILImage
from PIL import ImageOps

from src.config import Config
from src.image_metadata import read_metadata, write_metadata
from src.image_processor import generate_web_derivatives


def _safe_relative(value: str) -> str:
    path = PurePosixPath(str(value or "").strip())
    if not str(path) or path.is_absolute() or ".." in path.parts:
        raise ValueError("Invalid asset location")
    return str(path)


def _read_asset(area: str, location: str, *, storage_mode: str, image_folder: str, sp_client) -> bytes:
    location = _safe_relative(location)
    if storage_mode == "sharepoint":
        root = (Config.SHAREPOINT_IMAGE_FOLDER or "").strip().strip("/")
        path = f"{root}/{area}/{location}" if root else f"{area}/{location}"
        return sp_client.get_file_bytes(path)
    path = (Path(image_folder).resolve() / area / location).resolve()
    base = Path(image_folder).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise FileNotFoundError(f"Missing {area} asset: {location}")
    return path.read_bytes()


def _asset_exists(area: str, location: str, *, storage_mode: str, image_folder: str, sp_client) -> bool:
    location = _safe_relative(location)
    if storage_mode == "sharepoint":
        root = (Config.SHAREPOINT_IMAGE_FOLDER or "").strip().strip("/")
        path = f"{root}/{area}/{location}" if root else f"{area}/{location}"
        try:
            sp_client.get_file_metadata(path)
            return True
        except Exception:
            return False
    path = (Path(image_folder).resolve() / area / location).resolve()
    return path.is_relative_to(Path(image_folder).resolve()) and path.is_file()


def _write_hero(location: str, content: bytes, *, storage_mode: str, image_folder: str, sp_client) -> None:
    location = _safe_relative(location)
    rel = PurePosixPath(location)
    if storage_mode == "sharepoint":
        root = (Config.SHAREPOINT_IMAGE_FOLDER or "").strip().strip("/")
        folder = f"{root}/Hero/{rel.parent}" if root else f"Hero/{rel.parent}"
        sp_client.upload_file(folder, rel.name, content)
        return
    path = Path(image_folder) / "Hero" / Path(location)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _stored_derivative_metadata(content: bytes, location: str) -> dict:
    with PILImage.open(BytesIO(content)) as image:
        oriented = ImageOps.exif_transpose(image)
        width, height = oriented.size
        image_format = (image.format or "unknown").upper()
    return {
        "location": location,
        "width": width,
        "height": height,
        "format": image_format,
        "fileSize": len(content),
    }


def backfill_record(
    record: dict,
    *,
    storage_mode: str,
    image_folder: str,
    sp_client=None,
) -> dict:
    """Backfill one explicitly selected record without replacing existing files."""
    fields = record.get("fields", {})
    record_id = str(record.get("id", "")).strip()
    slug = str(fields.get("Slug", "") or "").strip()
    standard_location = _safe_relative(fields.get("Location", ""))
    original_location = _safe_relative(fields.get("High-Res Location", ""))
    if not record_id or not slug:
        raise ValueError("Record must have an id and slug")

    original_bytes = _read_asset(
        "High-Res", original_location,
        storage_mode=storage_mode, image_folder=image_folder, sp_client=sp_client,
    )
    standard_bytes = _read_asset(
        "WebP", standard_location,
        storage_mode=storage_mode, image_folder=image_folder, sp_client=sp_client,
    )
    derivatives = generate_web_derivatives(original_bytes)
    existing = read_metadata(
        slug,
        storage_mode=storage_mode,
        image_folder=image_folder,
        sp_client=sp_client,
    )

    hero_meta = None
    hero_created = False
    if derivatives["hero"]:
        hero_exists = _asset_exists(
            "Hero", standard_location,
            storage_mode=storage_mode, image_folder=image_folder, sp_client=sp_client,
        )
        if not hero_exists:
            _write_hero(
                standard_location,
                derivatives["hero"]["bytes"],
                storage_mode=storage_mode,
                image_folder=image_folder,
                sp_client=sp_client,
            )
            hero_created = True
        hero_meta = {
            key: value for key, value in derivatives["hero"].items() if key != "bytes"
        }
        hero_meta["location"] = standard_location

    prior_original = existing.get("original", {}) if isinstance(existing, dict) else {}
    original_meta = {
        **derivatives["original"],
        "filename": PurePosixPath(original_location).name,
        "location": original_location,
        "provider": str(fields.get("Source", "") or prior_original.get("provider", "")),
        "attribution": prior_original.get("attribution", ""),
        "license": prior_original.get("license", ""),
        "sourceUrl": prior_original.get("sourceUrl", ""),
        "assetId": prior_original.get("assetId", ""),
    }
    focal = existing.get("focalPoint") if isinstance(existing, dict) else None
    if not isinstance(focal, dict) or not all(key in focal for key in ("x", "y")):
        focal = {"x": 0.5, "y": 0.5}
    metadata = {
        **(existing if isinstance(existing, dict) else {}),
        "schemaVersion": 1,
        "slug": slug,
        "original": original_meta,
        "hero": hero_meta,
        "standard": _stored_derivative_metadata(standard_bytes, standard_location),
        "width": original_meta["width"],
        "height": original_meta["height"],
        "qualityTier": original_meta["qualityTier"],
        "focalPoint": focal,
    }
    write_metadata(
        metadata,
        storage_mode=storage_mode,
        image_folder=image_folder,
        sp_client=sp_client,
    )
    return {
        "id": record_id,
        "slug": slug,
        "filename": fields.get("Filename", ""),
        "qualityTier": original_meta["qualityTier"],
        "heroCreated": hero_created,
        "heroPreserved": bool(hero_meta) and not hero_created,
        "metadataWritten": True,
    }
