from io import BytesIO

from PIL import Image

from src.image_processor import (
    HERO_WIDTH,
    STANDARD_WIDTH,
    generate_web_derivatives,
    process_image,
)


class FakeGenerator:
    def generate_all(self, _webp_bytes, _categories, filename="", context=None):
        return "Headshots", "Person smiling outdoors", "portrait, outdoor"

    def generate_alt_and_tags(self, _webp_bytes, filename="", context=None):
        return "Person smiling outdoors", "portrait, outdoor"

    def generate_category(self, _webp_bytes, _categories, filename=""):
        return "Headshots"

    def generate_alt_text(self, _webp_bytes, context=None, filename=""):
        return "Person smiling outdoors"

    def generate_tags(self, _webp_bytes, context=None, filename=""):
        return "portrait, outdoor"


class RecordingListClient:
    def __init__(self, record=None):
        self.record = record
        self.calls = []

    def record_exists(self, _filename):
        return False

    def create_record(self, **kwargs):
        self.calls.append(kwargs)
        return self.record


class RecordingSpClient:
    def __init__(self):
        self.uploads = []

    def upload_file(self, folder_path, filename, content_bytes):
        self.uploads.append((folder_path, filename, len(content_bytes)))
        return {"name": filename}


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc``\xf8\xcf\x00\x00\x02\x01\x01\x00"
    b"\x18\xdd\x8d\xb1"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _image_bytes(width, height, *, image_format="PNG", mode="RGB", exif=None):
    color = (20, 80, 140, 96) if mode == "RGBA" else (20, 80, 140)
    image = Image.new(mode, (width, height), color)
    buf = BytesIO()
    save_kwargs = {"format": image_format}
    if exif is not None:
        save_kwargs["exif"] = exif
    image.save(buf, **save_kwargs)
    return buf.getvalue()


def _opened_size(content):
    with Image.open(BytesIO(content)) as image:
        return image.size


def test_landscape_original_generates_hero_and_standard_derivatives():
    result = generate_web_derivatives(_image_bytes(3200, 1800))

    assert result["original"]["qualityTier"] == "hero-ready"
    assert _opened_size(result["hero"]["bytes"]) == (HERO_WIDTH, 1440)
    assert _opened_size(result["standard"]["bytes"]) == (STANDARD_WIDTH, 900)
    assert result["hero"]["format"] == "WEBP"
    assert result["standard"]["quality"] == 82


def test_2000px_original_only_generates_standard_and_is_standard_only():
    result = generate_web_derivatives(_image_bytes(2000, 1000))

    assert result["hero"] is None
    assert _opened_size(result["standard"]["bytes"]) == (1600, 800)
    assert result["original"]["qualityTier"] == "standard-only"


def test_1600px_original_is_not_upscaled():
    result = generate_web_derivatives(_image_bytes(1600, 900))

    assert result["hero"] is None
    assert _opened_size(result["standard"]["bytes"]) == (1600, 900)


def test_below_1600_is_low_resolution_and_legacy_webp_is_not_upscaled():
    result = generate_web_derivatives(_image_bytes(1200, 800))

    assert result["original"]["qualityTier"] == "low-resolution"
    assert result["hero"] is None
    assert _opened_size(result["standard"]["bytes"]) == (1200, 800)


def test_exif_orientation_and_aspect_ratio_are_honored():
    exif = Image.Exif()
    exif[274] = 6  # rotate 90 degrees clockwise for display
    raw = _image_bytes(1200, 2000, image_format="JPEG", exif=exif)

    result = generate_web_derivatives(raw)

    assert (result["original"]["width"], result["original"]["height"]) == (2000, 1200)
    assert _opened_size(result["standard"]["bytes"]) == (1600, 960)


def test_transparency_is_preserved():
    result = generate_web_derivatives(_image_bytes(1800, 900, mode="RGBA"))

    with Image.open(BytesIO(result["standard"]["bytes"])) as image:
        assert image.mode == "RGBA"
        assert image.getchannel("A").getextrema()[1] < 255


def test_process_image_raises_when_record_creation_fails(tmp_path):
    generator = FakeGenerator()
    list_client = RecordingListClient(record=None)
    sp_client = RecordingSpClient()

    try:
        process_image(
            file_bytes=PNG_BYTES,
            original_filename="headshot.png",
            generator=generator,
            list_client=list_client,
            sp_client=sp_client,
            image_folder=str(tmp_path),
            storage_mode="sharepoint",
            source="Internal",
        )
        assert False, "Expected metadata record creation to fail"
    except RuntimeError as exc:
        assert str(exc) == "Metadata record creation failed"


def test_process_image_returns_result_when_record_creation_succeeds(tmp_path):
    generator = FakeGenerator()
    list_client = RecordingListClient(record={"id": "sp_123"})
    sp_client = RecordingSpClient()

    result = process_image(
        file_bytes=PNG_BYTES,
        original_filename="headshot.png",
        generator=generator,
        list_client=list_client,
        sp_client=sp_client,
        image_folder=str(tmp_path),
        storage_mode="sharepoint",
        source="Internal",
    )

    assert result["status"] == "pending-review"
    assert result["filename"].endswith(".webp")
    assert len(sp_client.uploads) == 3
    assert list_client.calls[0]["location"].startswith("Headshots/")
    assert result["qualityTier"] == "low-resolution"
    assert result["focalPoint"] == {"x": 0.5, "y": 0.5}
    # Existing response keys remain available alongside additive delivery data.
    for legacy_key in ("slug", "filename", "alt_text", "tags", "location", "high_res_location", "status"):
        assert legacy_key in result
    for additive_key in ("original", "hero", "standard", "width", "height", "qualityTier", "focalPoint"):
        assert additive_key in result
