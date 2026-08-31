import asyncio

from src import mcp_server
from src.image_metadata import write_metadata


def test_mcp_delivery_metadata_is_additive_and_prefers_hero(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server.Config, "STORAGE_MODE", "local")
    monkeypatch.setattr(mcp_server.Config, "IMAGE_FOLDER", str(tmp_path))
    monkeypatch.setattr(mcp_server.Config, "APP_BASE_URL", "https://library.example")
    monkeypatch.setattr(mcp_server.Config, "MCP_INTERNAL_SECRET", "test-secret")
    write_metadata(
        {
            "slug": "wide-image",
            "original": {"location": "Internal/wide-image-original.png", "width": 3000, "height": 1500},
            "hero": {"location": "Banners/wide-image.webp", "width": 2560, "height": 1280},
            "standard": {"location": "Banners/wide-image.webp", "width": 1600, "height": 800},
            "width": 3000,
            "height": 1500,
            "qualityTier": "hero-ready",
            "focalPoint": {"x": 0.4, "y": 0.6},
        },
        storage_mode="local",
        image_folder=str(tmp_path),
    )
    legacy = {
        "slug": "wide-image",
        "filename": "wide-image.webp",
        "location": "Banners/wide-image.webp",
        "webp_url": "legacy-standard-url",
        "status": "approved",
    }

    enriched = mcp_server._add_delivery_metadata(legacy)

    assert all(enriched[key] == value for key, value in legacy.items())
    assert enriched["preferredHeroUrl"] == enriched["hero"]["url"]
    assert enriched["standardWebUrl"] == enriched["standard"]["url"]
    assert enriched["qualityTier"] == "hero-ready"
    assert enriched["focalPoint"] == {"x": 0.4, "y": 0.6}


def test_mcp_legacy_record_without_sidecar_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server.Config, "STORAGE_MODE", "local")
    monkeypatch.setattr(mcp_server.Config, "IMAGE_FOLDER", str(tmp_path))
    legacy = {"slug": "old-image", "location": "Community/old-image.webp"}

    enriched = mcp_server._add_delivery_metadata(legacy)

    assert all(enriched[key] == value for key, value in legacy.items())
    assert enriched["backfillAvailable"] is True
    assert enriched["backfillTool"] == "backfill_image_derivatives"


def test_pete_exposes_explicit_selective_backfill_tool():
    tools = asyncio.run(mcp_server.list_tools())
    tool = next(item for item in tools if item.name == "backfill_image_derivatives")

    assert tool.inputSchema["required"] == ["slug"]
    assert "Never call automatically" in tool.description
