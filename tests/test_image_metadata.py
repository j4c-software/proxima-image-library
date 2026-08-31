import pytest

from src.image_metadata import read_metadata, update_focal_point, write_metadata


def test_editor_can_revise_normalized_focal_point(tmp_path):
    metadata = {
        "slug": "sample-image",
        "focalPoint": {"x": 0.5, "y": 0.5},
        "qualityTier": "hero-ready",
    }
    write_metadata(metadata, storage_mode="local", image_folder=str(tmp_path))

    updated = update_focal_point(
        "sample-image",
        0.25,
        0.8,
        storage_mode="local",
        image_folder=str(tmp_path),
    )

    assert updated["focalPoint"] == {"x": 0.25, "y": 0.8}
    assert read_metadata(
        "sample-image", storage_mode="local", image_folder=str(tmp_path)
    )["qualityTier"] == "hero-ready"


def test_focal_point_rejects_out_of_range_values(tmp_path):
    write_metadata(
        {"slug": "sample-image", "focalPoint": {"x": 0.5, "y": 0.5}},
        storage_mode="local",
        image_folder=str(tmp_path),
    )
    with pytest.raises(ValueError):
        update_focal_point(
            "sample-image", 1.1, 0.5,
            storage_mode="local", image_folder=str(tmp_path),
        )
