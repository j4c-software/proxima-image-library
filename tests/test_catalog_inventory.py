from io import BytesIO

from PIL import Image

from src.catalog_inventory import build_inventory


def _write_original(root, location, width):
    path = root / "High-Res" / location
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, 500), "navy")
    buf = BytesIO()
    image.save(buf, format="JPEG")
    path.write_bytes(buf.getvalue())


def test_inventory_is_read_only_and_groups_resolution_and_metadata_gaps(tmp_path):
    records = []
    for index, width in enumerate((3000, 1600, 1200, 2000), 1):
        location = f"Internal/image-{index}.jpg"
        _write_original(tmp_path, location, width)
        records.append({
            "id": str(index),
            "fields": {
                "Slug": f"image-{index}",
                "Filename": f"image-{index}.webp",
                "High-Res Location": location,
                "Source": "Internal",
            },
        })

    report = build_inventory(records, storage_mode="local", image_folder=str(tmp_path))

    assert report["dryRun"] is True
    assert report["summary"]["heroEligible"] == 1
    assert report["summary"]["storedOriginalOnly1600"] == 1
    assert report["summary"]["below1600"] == 1
    assert report["summary"]["standardOnly1601To2559"] == 1
    assert report["summary"]["metadataGaps"] == 4
    assert not (tmp_path / "Hero").exists()
