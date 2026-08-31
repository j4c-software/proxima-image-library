"""Image processing pipeline: transform → AI metadata → store.

Used by Feature 2 (upload) and any future feature that catalogs an image.
"""

import re
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from PIL import Image as PILImage
from PIL import ImageCms, ImageOps

from src.config import Config

# Approved website derivatives. Width is authoritative; height follows the
# EXIF-corrected source aspect ratio. Existing callers of transform_to_webp()
# continue to receive the standard derivative.
HERO_WIDTH = 2560
STANDARD_WIDTH = 1600
WEBP_MAX_SIDE = STANDARD_WIDTH  # backward-compatible import
WEBP_QUALITY = 82

CATEGORIES = ["Headshots", "Community", "Locations", "Situations", "Graphics", "Banners"]
SOURCES = ["ShutterStock", "AdobeStock", "Unsplash", "Pexels", "Pixabay", "Internal"]

_SOURCE_ALIASES = {
    "shutterstock": "ShutterStock",
    "adobestock": "AdobeStock",
    "adobe-stock": "AdobeStock",
    "adobe stock": "AdobeStock",
    "unsplash": "Unsplash",
    "pexels": "Pexels",
    "pixabay": "Pixabay",
    "internal": "Internal",
}


def normalize_source(source: Optional[str]) -> str:
    """Normalize free-form source values to a supported canonical source."""
    if not source:
        return "Internal"
    raw = source.strip()
    if raw in SOURCES:
        return raw
    key = re.sub(r"[^a-z0-9]+", "", raw.lower())
    return _SOURCE_ALIASES.get(key, "Internal")


# ---------------------------------------------------------------------------
# Image transformation
# ---------------------------------------------------------------------------

def classify_quality(width: int) -> str:
    """Classify an EXIF-corrected original by its usable pixel width."""
    if width >= HERO_WIDTH:
        return "hero-ready"
    if width >= STANDARD_WIDTH:
        return "standard-only"
    return "low-resolution"


def _to_srgb(img: PILImage.Image) -> PILImage.Image:
    """Convert pixels to sRGB while retaining an alpha channel when present."""
    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )
    rgba = img.convert("RGBA") if has_alpha else None
    rgb = rgba.convert("RGB") if rgba is not None else img.convert("RGB")
    icc_profile = img.info.get("icc_profile")
    if icc_profile:
        try:
            rgb = ImageCms.profileToProfile(
                rgb,
                ImageCms.ImageCmsProfile(BytesIO(icc_profile)),
                ImageCms.createProfile("sRGB"),
                outputMode="RGB",
            )
        except (ImageCms.PyCMSError, OSError, ValueError):
            # Malformed/unsupported embedded profiles should not prevent safe
            # RGB normalization through Pillow's regular conversion.
            rgb = img.convert("RGB")
    if rgba is not None:
        rgb.putalpha(rgba.getchannel("A"))
    return rgb


def inspect_original(image_bytes: bytes) -> dict:
    """Return immutable-source facts using display orientation for dimensions."""
    with PILImage.open(BytesIO(image_bytes)) as source_img:
        source_format = (source_img.format or "unknown").upper()
        oriented = ImageOps.exif_transpose(source_img)
        width, height = oriented.size
        return {
            "width": width,
            "height": height,
            "format": source_format,
            "fileSize": len(image_bytes),
            "qualityTier": classify_quality(width),
        }


def _encode_webp(img: PILImage.Image, target_width: int) -> dict:
    width, height = img.size
    output_width = min(target_width, width)  # explicit no-upscale guarantee
    output_height = round(height * output_width / width)
    if (output_width, output_height) != img.size:
        img = img.resize((output_width, output_height), PILImage.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY, method=4)
    content = buf.getvalue()
    return {
        "bytes": content,
        "width": output_width,
        "height": output_height,
        "format": "WEBP",
        "fileSize": len(content),
        "quality": WEBP_QUALITY,
    }


def generate_web_derivatives(image_bytes: bytes) -> dict:
    """Generate standard and, when eligible, hero derivatives without upscaling.

    A sub-1600 source still receives a same-width WebP in the legacy standard
    location so existing consumers continue to have a web-safe URL; its
    low-resolution classification makes clear that it is not a 1600px asset.
    """
    original = inspect_original(image_bytes)
    with PILImage.open(BytesIO(image_bytes)) as source_img:
        oriented = ImageOps.exif_transpose(source_img)
        srgb = _to_srgb(oriented)
        standard = _encode_webp(srgb, STANDARD_WIDTH)
        hero = _encode_webp(srgb, HERO_WIDTH) if original["width"] >= HERO_WIDTH else None
    return {"original": original, "standard": standard, "hero": hero}


def transform_to_webp(image_bytes: bytes) -> bytes:
    """Backward-compatible standard WebP transform (quality 82, no upscale)."""
    return generate_web_derivatives(image_bytes)["standard"]["bytes"]


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

def slug_from_text(text: str, max_len: int = 60) -> str:
    """Convert arbitrary text to a lowercase hyphenated slug (max 60 chars)."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_len].rstrip("-") or "image"


def compute_perceptual_hash(image_bytes: bytes, hash_size: int = 8) -> str:
    """Difference hash (dHash) of the image content.

    Unlike filename/slug/alt-text, this is derived purely from pixels, so it
    stays stable across re-ingestion even when AI-generated alt text wording
    (and therefore the slug) differs between runs. Hamming distance between
    two hashes measures visual similarity — 0 means identical, small values
    (~<=6 bits out of 64) mean near-duplicate/re-compressed versions.
    """
    with PILImage.open(BytesIO(image_bytes)) as img:
        img = img.convert("L").resize((hash_size + 1, hash_size), PILImage.LANCZOS)
        pixels = list(img.getdata())

    bits = []
    for row in range(hash_size):
        offset = row * (hash_size + 1)
        row_pixels = pixels[offset:offset + hash_size + 1]
        for col in range(hash_size):
            bits.append(1 if row_pixels[col] > row_pixels[col + 1] else 0)

    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return f"{value:0{hash_size * hash_size // 4}x}"


def _unique_slug(base_slug: str, list_client, existing_filenames: Optional[set] = None) -> str:
    """Return base_slug, appending -2, -3, … until no matching record exists.

    Pass existing_filenames (a set of known filenames) to avoid per-slug
    Graph API fetches during batch operations.
    """
    slug = base_slug
    counter = 2
    if existing_filenames is not None:
        while f"{slug}.webp" in existing_filenames:
            slug = f"{base_slug}-{counter}"
            counter += 1
        existing_filenames.add(f"{slug}.webp")
    else:
        while list_client.record_exists(f"{slug}.webp"):
            slug = f"{base_slug}-{counter}"
            counter += 1
    return slug


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def process_image(
    file_bytes: bytes,
    original_filename: str,
    generator,
    list_client,
    sp_client,
    image_folder: str,
    storage_mode: str,
    on_progress: Optional[Callable[[str], None]] = None,
    category: Optional[str] = None,
    source_context: Optional[str] = None,
    source: Optional[str] = None,
    write_high_res: bool = True,
    high_res_location_override: Optional[str] = None,
    initial_status: str = "pending-review",
    ingest_source: str = "",
    existing_filenames: Optional[set] = None,
    attribution: str = "",
    license_info: str = "",
    source_url: str = "",
    source_asset_id: str = "",
    focal_point: Optional[dict] = None,
) -> dict:
    """Full pipeline for a single image.

    Steps:
      1. Inspect the untouched original and generate approved WebP derivatives
      2. Generate alt text via Claude vision (max 125 chars)
      3. Generate tags via Claude vision (2–5 from approved vocabulary)
      4. Build unique slug from alt text
      5. Upload/save High-Res original, standard WebP, and optional hero WebP
      6. Create the legacy metadata record and additive JSON sidecar

    Args:
        file_bytes:        Raw bytes of the source image.
        original_filename: Original filename (used to derive extension).
        category:          Storage category folder (e.g. "Headshots").
        generator:         AltTextGenerator instance.
        list_client:       LocalClient or SharePointListClient.
        sp_client:         SharePointClient or None (local mode).
        image_folder:      Local IMAGE_FOLDER path (used in local/test mode).
        storage_mode:      "sharepoint" or "local".
        on_progress:       Optional callback(msg) for SSE-style progress reporting.

    Returns:
        dict with keys: slug, filename, alt_text, tags, location,
                        high_res_location, status.

    Raises:
        Exception on any unrecoverable error.
    """
    if category is not None and category not in CATEGORIES:
        raise ValueError(f"Invalid category '{category}'. Must be one of: {CATEGORIES}")

    ext = Path(original_filename).suffix.lower() or ".jpg"
    source_name = normalize_source(source)
    focal = focal_point or {"x": 0.5, "y": 0.5}
    try:
        focal = {"x": float(focal["x"]), "y": float(focal["y"])}
    except (KeyError, TypeError, ValueError):
        raise ValueError("focal_point must contain numeric x and y values")
    if not 0 <= focal["x"] <= 1 or not 0 <= focal["y"] <= 1:
        raise ValueError("focal_point x and y must each be between 0 and 1")

    def _log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    # ── Step 1: Inspect + transform ────────────────────────────────────────
    _log("Inspecting original and generating approved WebP derivatives…")
    derivatives = generate_web_derivatives(file_bytes)
    webp_bytes = derivatives["standard"]["bytes"]
    hero_bytes = derivatives["hero"]["bytes"] if derivatives["hero"] else None
    image_hash = compute_perceptual_hash(webp_bytes)

    # ── Steps 1b–3: Category + alt text + tags in one Claude call ─────────
    if category is None:
        _log("Generating category, alt text, and tags via Claude…")
        ai_category, alt_text, tags = generator.generate_all(
            webp_bytes, CATEGORIES, filename="image.webp", context=source_context
        )
        category = ai_category or "Situations"
        alt_text = alt_text or ""
        tags = tags or ""
    else:
        _log("Generating alt text and tags via Claude…")
        alt_text, tags = generator.generate_alt_and_tags(
            webp_bytes, filename="image.webp", context=source_context
        )
        alt_text = alt_text or ""
        tags = tags or ""
    _log(f"Category: {category} | Alt text: {alt_text} | Tags: {tags}")

    # ── Step 4: Slug ───────────────────────────────────────────────────────
    base_slug = slug_from_text(alt_text[:80])
    slug = _unique_slug(base_slug, list_client, existing_filenames)
    webp_filename = f"{slug}.webp"
    highres_filename = f"{slug}-original{ext}"
    location = f"{category}/{webp_filename}"
    hero_location = f"{category}/{webp_filename}" if hero_bytes is not None else ""
    high_res_location = high_res_location_override or f"{source_name}/{highres_filename}"

    # ── Steps 5a–5b: Store files ───────────────────────────────────────────
    if storage_mode == "sharepoint" and sp_client is not None:
        root = Config.SHAREPOINT_IMAGE_FOLDER
        hr_rel = PurePosixPath(high_res_location)
        if write_high_res:
            _log("Uploading High-Res to SharePoint…")
            hr_folder = f"{root}/High-Res/{hr_rel.parent}" if str(hr_rel.parent) != "." else f"{root}/High-Res"
            sp_client.upload_file(hr_folder, hr_rel.name, file_bytes)
        _log("Uploading WebP to SharePoint…")
        sp_client.upload_file(f"{root}/WebP/{category}", webp_filename, webp_bytes)
        if hero_bytes is not None:
            _log("Uploading hero WebP to SharePoint…")
            sp_client.upload_file(f"{root}/Hero/{category}", webp_filename, hero_bytes)
    else:
        base = Path(image_folder)
        _log("Saving files locally…")
        # Store under WebP/ to match the SharePoint folder convention
        webp_path = base / "WebP" / category / webp_filename
        webp_path.parent.mkdir(parents=True, exist_ok=True)
        webp_path.write_bytes(webp_bytes)

        if hero_bytes is not None:
            hero_path = base / "Hero" / category / webp_filename
            hero_path.parent.mkdir(parents=True, exist_ok=True)
            hero_path.write_bytes(hero_bytes)

        if write_high_res:
            hr_path = base / "High-Res" / Path(high_res_location)
            hr_path.parent.mkdir(parents=True, exist_ok=True)
            hr_path.write_bytes(file_bytes)

    # ── Step 6: Metadata record ────────────────────────────────────────────
    _log("Writing metadata record…")
    record = list_client.create_record(
        filename=webp_filename,
        alt_text=alt_text,
        tags=tags,
        status=initial_status,
        slug=slug,
        location=location,
        high_res_location=high_res_location,
        source=source_name,
        ingest_source=ingest_source,
        image_hash=image_hash,
    )
    if not record:
        raise RuntimeError("Metadata record creation failed")

    original_meta = {
        **derivatives["original"],
        "filename": original_filename,
        "location": high_res_location,
        "provider": source_name,
        "attribution": attribution,
        "license": license_info,
        "sourceUrl": source_url,
        "assetId": source_asset_id,
    }
    standard_meta = {
        key: value for key, value in derivatives["standard"].items() if key != "bytes"
    }
    standard_meta["location"] = location
    hero_meta = None
    if derivatives["hero"]:
        hero_meta = {
            key: value for key, value in derivatives["hero"].items() if key != "bytes"
        }
        hero_meta["location"] = hero_location

    metadata = {
        "schemaVersion": 1,
        "slug": slug,
        "original": original_meta,
        "hero": hero_meta,
        "standard": standard_meta,
        "width": original_meta["width"],
        "height": original_meta["height"],
        "qualityTier": original_meta["qualityTier"],
        "focalPoint": focal,
    }
    from src.image_metadata import write_metadata
    metadata_location = write_metadata(
        metadata,
        storage_mode=storage_mode,
        image_folder=image_folder,
        sp_client=sp_client,
    )

    _log("Done.")
    return {
        "slug": slug,
        "filename": webp_filename,
        "alt_text": alt_text,
        "tags": tags,
        "location": location,
        "high_res_location": high_res_location,
        "source": source_name,
        "status": initial_status,
        "image_hash": image_hash,
        "original": original_meta,
        "hero": hero_meta,
        "standard": standard_meta,
        "width": original_meta["width"],
        "height": original_meta["height"],
        "qualityTier": original_meta["qualityTier"],
        "focalPoint": focal,
        "metadata_location": metadata_location,
    }
