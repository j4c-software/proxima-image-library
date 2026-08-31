from io import BytesIO

from PIL import Image

from src.hero_backfill import backfill_record
from src.image_metadata import read_metadata, write_metadata


def _image_bytes(width, height, image_format="JPEG"):
    image = Image.new("RGB", (width, height), (30, 90, 150))
    buf = BytesIO()
    image.save(buf, format=image_format, quality=90)
    return buf.getvalue()


def _record(record_id, slug, location, original_location):
    return {
        "id": record_id,
        "fields": {
            "Slug": slug,
            "Filename": f"{slug}.webp",
            "Location": location,
            "High-Res Location": original_location,
            "Source": "Internal",
            "Status": "approved",
        },
    }


def test_selected_hero_backfill_preserves_original_and_standard(tmp_path):
    original_location = "Internal/wide-original.jpg"
    standard_location = "Banners/wide.webp"
    original = _image_bytes(3000, 1500)
    standard = _image_bytes(1600, 800, "WEBP")
    original_path = tmp_path / "High-Res" / original_location
    standard_path = tmp_path / "WebP" / standard_location
    original_path.parent.mkdir(parents=True)
    standard_path.parent.mkdir(parents=True)
    original_path.write_bytes(original)
    standard_path.write_bytes(standard)
    write_metadata(
        {
            "slug": "wide",
            "focalPoint": {"x": 0.2, "y": 0.7},
            "original": {"attribution": "Existing credit", "license": "Existing license"},
        },
        storage_mode="local",
        image_folder=str(tmp_path),
    )

    result = backfill_record(
        _record("1", "wide", standard_location, original_location),
        storage_mode="local",
        image_folder=str(tmp_path),
    )

    assert result["heroCreated"] is True
    assert original_path.read_bytes() == original
    assert standard_path.read_bytes() == standard
    hero_path = tmp_path / "Hero" / standard_location
    with Image.open(hero_path) as hero:
        assert hero.size == (2560, 1280)
        assert hero.format == "WEBP"
    metadata = read_metadata("wide", storage_mode="local", image_folder=str(tmp_path))
    assert metadata["focalPoint"] == {"x": 0.2, "y": 0.7}
    assert metadata["original"]["attribution"] == "Existing credit"
    assert metadata["original"]["license"] == "Existing license"
    assert metadata["qualityTier"] == "hero-ready"


def test_selected_standard_only_backfill_writes_metadata_without_hero(tmp_path):
    original_location = "Internal/medium-original.png"
    standard_location = "Community/medium.webp"
    original_path = tmp_path / "High-Res" / original_location
    standard_path = tmp_path / "WebP" / standard_location
    original_path.parent.mkdir(parents=True)
    standard_path.parent.mkdir(parents=True)
    original_path.write_bytes(_image_bytes(2000, 1000, "PNG"))
    standard = _image_bytes(1600, 800, "WEBP")
    standard_path.write_bytes(standard)

    result = backfill_record(
        _record("2", "medium", standard_location, original_location),
        storage_mode="local",
        image_folder=str(tmp_path),
    )

    assert result["heroCreated"] is False
    assert not (tmp_path / "Hero").exists()
    assert standard_path.read_bytes() == standard
    metadata = read_metadata("medium", storage_mode="local", image_folder=str(tmp_path))
    assert metadata["qualityTier"] == "standard-only"
    assert metadata["hero"] is None
