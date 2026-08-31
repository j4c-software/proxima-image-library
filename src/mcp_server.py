"""MCP server — exposes Proxima Image Library tools to Claude.

Tools:
  search_image_library   — full-text search against the internal SharePoint List
  search_stock_photos    — search Pexels, Shutterstock, Unsplash, Pixabay concurrently
  catalog_stock_image    — download a stock image URL, run the full pipeline, store it

Run as a stdio MCP server:
  python -m src.mcp_server

Register in Claude Desktop (~/Library/Application Support/Claude/claude_desktop_config.json):
  {
    "mcpServers": {
      "proxima-image-library": {
        "command": "python",
        "args": ["-m", "src.mcp_server"],
        "cwd": "/path/to/proxima-image-library"
      }
    }
  }
"""

import base64
import concurrent.futures
import json
import re
from pathlib import Path
from typing import Any

import requests as _requests

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from src.config import Config

server = Server("proxima-image-library")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _score_record(fields: dict, phrases: list[str]) -> int:
    """Return a match score: how many phrases have at least one token hit in alt_text or tags."""
    text = " ".join([
        fields.get("Alt Text", ""),
        fields.get("Tags", ""),
        fields.get("Filename", ""),
    ]).lower()
    score = 0
    for phrase in phrases:
        tokens = re.findall(r"[a-z0-9]+", phrase.lower())
        if any(tok in text for tok in tokens):
            score += 1
    return score


def _get_list_client():
    if Config.TEST_MODE:
        from src.local_client import LocalClient
        return LocalClient()
    from src.sharepoint_list_client import SharePointListClient
    return SharePointListClient()


def _webp_url(location: str) -> str:
    """Build a full URL for an image location.  In production this would be the
    SharePoint CDN URL; for now return the /image Flask route path for local mode."""
    if not location:
        return ""
    if Config.STORAGE_MODE == "sharepoint":
        return f"served-via-sharepoint:{location}"
    return f"http://localhost:5000/image?path={location}"


def _delivery_url(area: str, location: str) -> str:
    """Build a delivery URL for an exact stored asset tier."""
    if not location:
        return ""
    from urllib.parse import quote
    base_url = (Config.APP_BASE_URL or "http://localhost:5000").rstrip("/")
    secret = Config.MCP_INTERNAL_SECRET
    query = f"area={quote(area)}&path={quote(location)}"
    if secret:
        query += f"&key={quote(secret)}"
    return f"{base_url}/api/mcp/asset?{query}"


def _add_delivery_metadata(item: dict) -> dict:
    """Add sidecar fields without changing legacy search-result fields."""
    if "qualityTier" in item and "standard" in item:
        metadata = {
            key: item.get(key)
            for key in ("original", "hero", "standard", "width", "height", "qualityTier", "focalPoint")
        }
    else:
        from src.image_metadata import read_metadata
        metadata = read_metadata(item.get("slug", "")) if item.get("slug") else {}
    if not metadata:
        enriched = dict(item)
        enriched["backfillAvailable"] = True
        enriched["backfillReason"] = "missing derivative metadata"
        enriched["backfillTool"] = "backfill_image_derivatives"
        return enriched
    enriched = dict(item)
    for key in ("original", "hero", "standard"):
        value = metadata.get(key)
        enriched[key] = dict(value) if isinstance(value, dict) else value
    if isinstance(enriched.get("original"), dict):
        location = enriched["original"].get("location", "")
        enriched["original"]["url"] = _delivery_url("High-Res", location)
    if isinstance(enriched.get("standard"), dict):
        location = enriched["standard"].get("location", "")
        enriched["standard"]["url"] = _delivery_url("WebP", location)
    if isinstance(enriched.get("hero"), dict):
        location = enriched["hero"].get("location", "")
        enriched["hero"]["url"] = _delivery_url("Hero", location)
    enriched["width"] = metadata.get("width")
    enriched["height"] = metadata.get("height")
    enriched["qualityTier"] = metadata.get("qualityTier")
    enriched["focalPoint"] = metadata.get("focalPoint")
    enriched["preferredHeroUrl"] = (
        (enriched.get("hero") or {}).get("url")
        or (enriched.get("standard") or {}).get("url")
        or enriched.get("webp_url", "")
    )
    enriched["standardWebUrl"] = (
        (enriched.get("standard") or {}).get("url") or enriched.get("webp_url", "")
    )
    enriched["backfillAvailable"] = bool(
        enriched.get("qualityTier") == "hero-ready" and not enriched.get("hero")
    )
    if enriched["backfillAvailable"]:
        enriched["backfillReason"] = "hero-ready original has no hero derivative"
        enriched["backfillTool"] = "backfill_image_derivatives"
    return enriched


_THUMB_MAX_PX = 200  # max width or height for inline thumbnails


def _resize_to_jpeg_bytes(raw: bytes, max_px: int = _THUMB_MAX_PX) -> bytes:
    """Resize raw image bytes to fit within max_px on longest side, return JPEG bytes."""
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70, optimize=True)
    return buf.getvalue()


def _fetch_thumb(url: str) -> types.ImageContent | None:
    """Fetch a thumbnail URL and return as MCP ImageContent, or None on failure."""
    if not url:
        return None
    try:
        resp = _requests.get(url, timeout=10)
        resp.raise_for_status()
        jpeg_bytes = _resize_to_jpeg_bytes(resp.content)
        data = base64.b64encode(jpeg_bytes).decode()
        return types.ImageContent(type="image", data=data, mimeType="image/jpeg")
    except Exception:
        return None


def _thumb_local(location: str) -> types.ImageContent | None:
    """Read a local image file and return as MCP ImageContent."""
    if not location:
        return None
    try:
        base = Path(Config.IMAGE_FOLDER)
        path = base / "WebP" / location
        if not path.exists():
            path = base / location
            if not path.exists():
                return None
        jpeg_bytes = _resize_to_jpeg_bytes(path.read_bytes())
        data = base64.b64encode(jpeg_bytes).decode()
        return types.ImageContent(type="image", data=data, mimeType="image/jpeg")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Tool: search_image_library
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_image_library",
            description=(
                "Search the Proxima internal image library. "
                "Pass the Photo Suggestion phrases from a blog or article skill output. "
                "Returns ranked matches with alt text, tags, slug, and image URL. "
                "Always try this before searching stock photo libraries."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "phrases": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "1–20 search phrases. Use the Photo Suggestion phrases "
                            "from the writing skill output verbatim."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results to return (default 8, max 20).",
                        "default": 8,
                    },
                },
                "required": ["phrases"],
            },
        ),
        types.Tool(
            name="search_stock_photos",
            description=(
                "Search stock photo libraries (Pexels, Shutterstock, Unsplash, Pixabay) "
                "concurrently. Also automatically includes matching internal library images "
                "at the top of the combined gallery. Use this as the primary search tool — "
                "it returns a single gallery link with both internal and stock results."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "phrases": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "1–20 search phrases from the Photo Suggestion field.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Results per phrase per library (default 6, max 12).",
                        "default": 6,
                    },
                    "pexels_orientation": {
                        "type": "string",
                        "enum": ["landscape", "portrait", "square"],
                        "description": "Optional: filter Pexels results by orientation.",
                    },
                    "shutterstock_orientation": {
                        "type": "string",
                        "enum": ["horizontal", "vertical"],
                        "description": "Optional: filter Shutterstock results by orientation.",
                    },
                    "unsplash_orientation": {
                        "type": "string",
                        "enum": ["landscape", "portrait", "squarish"],
                        "description": "Optional: filter Unsplash results by orientation.",
                    },
                },
                "required": ["phrases"],
            },
        ),
        types.Tool(
            name="catalog_stock_image",
            description=(
                "Download a stock photo by URL, transform it to WebP, generate AI alt text "
                "and tags, store it in SharePoint, and write the metadata record. "
                "IMPORTANT: Never call this automatically. You MUST show the search results "
                "and thumbnails to the user first and wait for them to explicitly confirm "
                "which image they want before calling this tool. "
                "Returns the final slug, filename, alt_text, tags, and location."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "download_url": {
                        "type": "string",
                        "description": "Direct download URL of the highest-resolution image available.",
                    },
                    "original_filename": {
                        "type": "string",
                        "description": "Original filename including extension (used to derive type).",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["Headshots", "Community", "Locations", "Situations", "Graphics", "Banners"],
                        "description": "Storage category. Infer from image content if not specified by user.",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["ShutterStock", "AdobeStock", "Unsplash", "Pexels", "Pixabay", "Internal"],
                        "description": "Optional source/provenance for the High-Res folder.",
                    },
                    "attribution": {
                        "type": "string",
                        "description": "Photographer/creator credit supplied by the provider.",
                    },
                    "license": {
                        "type": "string",
                        "description": "License name, terms, or entitlement reference when known.",
                    },
                    "source_url": {
                        "type": "string",
                        "description": "Provider page or source URL for provenance.",
                    },
                    "source_asset_id": {
                        "type": "string",
                        "description": "Provider asset identifier when available.",
                    },
                },
                "required": ["download_url", "original_filename", "category"],
            },
        ),
        types.Tool(
            name="get_selected_images",
            description=(
                "Retrieve the images the user selected in the preview gallery. "
                "Call this after the user says they have made their selection. "
                "Returns the selected images so you can catalog them with catalog_stock_image."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "token": {
                        "type": "string",
                        "description": "The session token returned by search_stock_photos.",
                    },
                },
                "required": ["token"],
            },
        ),
        types.Tool(
            name="get_image_url",
            description=(
                "Return a public thumbnail URL for an image in the Proxima library. "
                "Use this to embed library images as markdown images in chat. "
                "Pass the location field from a search_image_library result."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The image location path from a search_image_library result.",
                    },
                },
                "required": ["location"],
            },
        ),
        types.Tool(
            name="backfill_image_derivatives",
            description=(
                "Backfill a specifically selected Proxima library image after search. "
                "Never call automatically: show the found image and ask the user to explicitly confirm first. "
                "Preserves the original and existing standard WebP, creates a missing 2560px hero when eligible, "
                "and writes or merges delivery/focal metadata."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Exact slug returned by search_image_library.",
                    },
                },
                "required": ["slug"],
            },
        ),
        types.Tool(
            name="catalog_image_from_file",
            description=(
                "Catalog an image file that the user has attached to this conversation. "
                "Accepts raw base64 image data, transforms it to WebP, generates AI alt text "
                "and tags, stores it in SharePoint, and writes the metadata record. "
                "Use this when the user shares a file directly in the chat and wants it added to the library. "
                "IMPORTANT: Confirm with the user which category the image belongs to before calling this tool. "
                "Returns the final slug, filename, alt_text, tags, and location."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "image_data": {
                        "type": "string",
                        "description": "Base64-encoded image data (from the file attachment).",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Original filename including extension (e.g. photo.jpg).",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["Headshots", "Community", "Locations", "Situations", "Graphics", "Banners"],
                        "description": "Storage category. Ask the user if not obvious from context.",
                    },
                },
                "required": ["image_data", "filename", "category"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    if name == "search_image_library":
        return await _search_image_library(arguments)
    if name == "search_stock_photos":
        return await _search_stock_photos(arguments)
    if name == "catalog_stock_image":
        return await _catalog_stock_image(arguments)
    if name == "get_selected_images":
        return await _get_selected_images(arguments)
    if name == "get_image_url":
        return await _get_image_url(arguments)
    if name == "backfill_image_derivatives":
        return await _backfill_image_derivatives(arguments)
    if name == "catalog_image_from_file":
        return await _catalog_image_from_file(arguments)
    return [types.TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _search_image_library(args: dict) -> list[types.TextContent]:
    phrases = args.get("phrases", [])
    limit = max(1, min(int(args.get("limit", 8)), 20))

    if not phrases:
        return [types.TextContent(type="text", text="[]")]

    client = _get_list_client()
    records = client.get_all_records()

    scored = []
    for rec in records:
        fields = rec.get("fields", {})
        score = _score_record(fields, phrases)
        if score > 0:
            location = fields.get("Location", "")
            scored.append({
                "score": score,
                "slug": fields.get("Slug", ""),
                "filename": fields.get("Filename", ""),
                "alt_text": fields.get("Alt Text", ""),
                "tags": fields.get("Tags", ""),
                "location": location,
                "webp_url": _webp_url(location),
                "category": location.split("/")[0] if "/" in location else "",
                "status": fields.get("Status", ""),
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    results = [_add_delivery_metadata(item) for item in scored[:limit]]

    if not results:
        return [types.TextContent(
            type="text",
            text=(
                "No matches found in the internal library for these phrases. "
                "Use search_stock_photos to search external libraries."
            ),
        )]

    base_url = (Config.APP_BASE_URL or "http://localhost:5000").rstrip("/")

    # Build thumbnail URLs — use public MCP thumbnail endpoint (no MSAL required)
    secret = Config.MCP_INTERNAL_SECRET
    from urllib.parse import quote as _quote
    def _thumb_url(location: str) -> str:
        if not location:
            return ""
        if secret:
            return f"{base_url}/api/mcp/thumbnail?path={_quote(location)}&key={_quote(secret)}"
        return f"{base_url}/thumbnail?path={_quote(location)}"

    # Fetch thumbnails in parallel (inline in chat)
    def _get_thumb(item):
        loc = item.get("location", "")
        if Config.STORAGE_MODE == "sharepoint":
            return item, _fetch_thumb(_thumb_url(loc))
        return item, _thumb_local(loc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_get_thumb, r): i for i, r in enumerate(results)}
        thumb_map = {}
        for f in concurrent.futures.as_completed(futures):
            idx = futures[f]
            try:
                item, img = f.result()
                if img:
                    thumb_map[idx] = (item, img)
            except Exception:
                pass

    # Build shortlist for the preview gallery
    shortlisted = []
    for item in results:
        loc = item.get("location", "")
        shortlisted.append({
            "type": "internal",
            "download_url": _thumb_url(loc),  # displayed in gallery (thumbnail proxy)
            "thumb": _thumb_url(loc),
            "title": item.get("alt_text", "") or item.get("filename", ""),
            "filename": item.get("filename", ""),
            "library": "internal",
            "slug": item.get("slug", ""),
            "location": loc,
            "phrase": phrases[0] if phrases else "",
        })

    # Create a preview session
    article_title = args.get("article_title", "Internal Library Results")
    preview_url = None
    token = None
    try:
        sess_resp = _requests.post(
            f"{base_url}/api/mcp/preview/session",
            json={"article_title": article_title, "shortlisted": shortlisted, "phrases": phrases},
            headers={"X-MCP-Secret": secret},
            timeout=15,
        )
        sess_resp.raise_for_status()
        token = sess_resp.json().get("token", "")
        preview_url = f"{base_url}/api/mcp/preview/{token}"
    except Exception:
        pass  # gallery link optional — inline results still shown

    contents: list = []

    header = f"Found **{len(results)} match(es)** in the internal library."
    if preview_url:
        header += (
            f" **[Open selection gallery]({preview_url})** to pick visually, "
            f"then tell me and I'll call `get_selected_images` with token `{token}`."
        )
    contents.append(types.TextContent(type="text", text=header))

    for idx, item in enumerate(results):
        label = (
            f"**#{idx + 1}** | slug: {item.get('slug','')} | "
            f"alt: {item.get('alt_text','')[:80]} | "
            f"tags: {item.get('tags','')[:60]} | "
            f"location: {item.get('location','')}"
        )
        delivery = {
            key: item.get(key)
            for key in (
                "original", "hero", "standard", "width", "height",
                "qualityTier", "focalPoint", "preferredHeroUrl", "standardWebUrl",
                "backfillAvailable", "backfillReason", "backfillTool",
            )
            if key in item
        }
        if delivery:
            label += f" | delivery: {json.dumps(delivery, ensure_ascii=False)}"
        contents.append(types.TextContent(type="text", text=label))
        if idx in thumb_map:
            contents.append(thumb_map[idx][1])

    return contents


async def _search_stock_photos(args: dict) -> list[types.TextContent]:

    phrases = args.get("phrases", [])[:20]
    limit = max(1, min(int(args.get("limit", 6)), 12))

    pexels_orientation = args.get("pexels_orientation")
    shutterstock_orientation = args.get("shutterstock_orientation")
    unsplash_orientation = args.get("unsplash_orientation")

    if not phrases:
        return [types.TextContent(type="text", text="{}")]

    # Build per-library search functions with optional orientation params
    def _pexels(phrase, lim):
        import os
        api_key = os.getenv("PEXELS_API_KEY", "")
        if not api_key:
            return {"results": [], "error": "PEXELS_API_KEY not configured"}
        try:
            import requests
            params = {"query": phrase, "per_page": lim}
            if pexels_orientation:
                params["orientation"] = pexels_orientation
            r = requests.get(
                "https://api.pexels.com/v1/search",
                params=params,
                headers={"Authorization": api_key},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            return {"results": [
                {
                    "thumb": p.get("src", {}).get("medium", ""),
                    "download_url": p.get("src", {}).get("original", ""),
                    "title": p.get("alt", "") or phrase,
                    "link": p.get("url", ""),
                    "source": "pexels",
                }
                for p in data.get("photos", [])
            ], "error": None}
        except Exception as e:
            return {"results": [], "error": str(e)}

    def _shutterstock(phrase, lim):
        import os
        import base64
        import requests
        cid = os.getenv("SHUTTERSTOCK_CLIENT_ID", "")
        csec = os.getenv("SHUTTERSTOCK_CLIENT_SECRET", "")
        if not cid or not csec:
            return {"results": [], "error": "Shutterstock credentials not configured"}
        try:
            credentials = base64.b64encode(f"{cid}:{csec}".encode()).decode()
            token_resp = requests.post(
                "https://api.shutterstock.com/v2/oauth/access_token",
                data={"grant_type": "client_credentials"},
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=10,
            )
            if token_resp.status_code == 401:
                return {
                    "results": [],
                    "error": "Shutterstock API auth failed (401). Check SHUTTERSTOCK_CLIENT_ID and SHUTTERSTOCK_CLIENT_SECRET.",
                }
            token_resp.raise_for_status()
            token = token_resp.json().get("access_token")
            if not token:
                return {
                    "results": [],
                    "error": "Shutterstock token response missing access_token",
                }

            params = {
                "query": phrase,
                "per_page": lim,
                "image_type": "photo",
                "fields": "id,description,assets",
            }
            if shutterstock_orientation:
                params["orientation"] = shutterstock_orientation
            r = requests.get(
                "https://api.shutterstock.com/v2/images/search",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if r.status_code == 401:
                return {
                    "results": [],
                    "error": "Shutterstock search unauthorized (401). Verify app permissions and API plan access.",
                }
            r.raise_for_status()
            data = r.json()
            results = []
            for item in data.get("data", []):
                item_id = item.get("id", "")
                desc = item.get("description", "")
                assets = item.get("assets", {})
                thumb = (
                    assets.get("large_thumb", {}).get("url")
                    or assets.get("preview", {}).get("url", "")
                )
                # Shutterstock requires licensing; provide link to license page
                results.append({
                    "thumb": thumb,
                    "download_url": None,  # requires licensing via Shutterstock
                    "title": desc,
                    "link": f"https://www.shutterstock.com/image-photo/{item_id}",
                    "source": "shutterstock",
                    "id": item_id,
                })
            return {"results": results, "error": None}
        except Exception as e:
            return {"results": [], "error": f"Shutterstock request failed: {e}"}

    def _unsplash(phrase, lim):
        import os
        import requests
        key = os.getenv("UNSPLASH_ACCESS_KEY", "")
        if not key:
            return {"results": [], "error": "UNSPLASH_ACCESS_KEY not configured"}
        try:
            params = {"query": phrase, "per_page": lim}
            if unsplash_orientation:
                params["orientation"] = unsplash_orientation
            r = requests.get(
                "https://api.unsplash.com/search/photos",
                params=params,
                headers={"Authorization": f"Client-ID {key}"},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            results = []
            for item in data.get("results", []):
                user = item.get("user", {})
                utm = "?utm_source=proxima_image_library&utm_medium=referral"
                results.append({
                    "thumb": item.get("urls", {}).get("small", ""),
                    "download_url": item.get("urls", {}).get("full", ""),
                    "title": item.get("alt_description") or item.get("description") or "",
                    "link": item.get("links", {}).get("html", "") + utm,
                    "photographer": user.get("name", ""),
                    "photographer_url": user.get("links", {}).get("html", "") + utm,
                    "source": "unsplash",
                })
            return {"results": results, "error": None}
        except Exception as e:
            return {"results": [], "error": str(e)}

    def _pixabay(phrase, lim):
        import os
        import requests
        key = os.getenv("PIXABAY_API_KEY", "")
        if not key:
            return {"results": [], "error": "PIXABAY_API_KEY not configured"}
        try:
            r = requests.get(
                "https://pixabay.com/api/",
                params={
                    "key": key,
                    "q": phrase,
                    "image_type": "photo",
                    "per_page": lim,
                    "safesearch": "true",
                },
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            return {"results": [
                {
                    "thumb": item.get("webformatURL", ""),
                    "download_url": item.get("largeImageURL", ""),
                    "title": item.get("tags", ""),
                    "link": item.get("pageURL", ""),
                    "source": "pixabay",
                }
                for item in data.get("hits", [])
            ], "error": None}
        except Exception as e:
            return {"results": [], "error": str(e)}

    searchers = {
        "pexels": _pexels,
        "shutterstock": _shutterstock,
        "unsplash": _unsplash,
        "pixabay": _pixabay,
    }

    output = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(phrases) * 4, 16)) as executor:
        future_map = {}
        for phrase in phrases:
            for lib, fn in searchers.items():
                future_map[executor.submit(fn, phrase, limit)] = (phrase, lib)

        for future in concurrent.futures.as_completed(future_map, timeout=25):
            phrase, lib = future_map[future]
            if phrase not in output:
                output[phrase] = {}
            try:
                output[phrase][lib] = future.result()
            except Exception as e:
                output[phrase][lib] = {"results": [], "error": str(e)}

    base_url = (Config.APP_BASE_URL or "http://localhost:5000").rstrip("/")
    secret = Config.MCP_INTERNAL_SECRET
    article_title = args.get("article_title", "Image Selection")

    # Query internal library and prepend matches so gallery is combined
    internal_shortlisted = []
    try:
        client = _get_list_client()
        records = client.get_all_records()
        scored = []
        for rec in records:
            fields = rec.get("fields", {})
            score = _score_record(fields, phrases)
            if score > 0:
                loc = fields.get("Location", "")
                scored.append((score, {
                    "type": "internal",
                    "download_url": f"{base_url}/thumbnail?path={loc}",
                    "thumb": f"{base_url}/thumbnail?path={loc}",
                    "title": fields.get("Alt Text", "") or fields.get("Filename", ""),
                    "filename": fields.get("Filename", ""),
                    "library": "internal",
                    "slug": fields.get("Slug", ""),
                    "location": loc,
                    "phrase": phrases[0] if phrases else "",
                }))
        scored.sort(key=lambda x: x[0], reverse=True)
        internal_shortlisted = [item for _, item in scored[:8]]
    except Exception:
        pass

    # Build shortlist for the preview UI
    shortlisted = list(internal_shortlisted)  # internal first
    for phrase, libs in output.items():
        for source, lib_data in libs.items():
            for img in lib_data.get("results", []):
                if not img.get("download_url"):
                    continue
                shortlisted.append({
                    "download_url": img.get("download_url", ""),
                    "thumb": img.get("thumb", ""),
                    "title": img.get("title", ""),
                    "filename": img.get("filename", f"{phrase.replace(' ', '-')}.jpg"),
                    "library": source,
                    "photographer": img.get("photographer", ""),
                    "phrase": phrase,
                })

    if not shortlisted:
        return [types.TextContent(type="text", text="No results found for the given phrases.")]

    # Create a server-side session and get a short token
    try:
        sess_resp = _requests.post(
            f"{base_url}/api/mcp/preview/session",
            json={"article_title": article_title, "shortlisted": shortlisted, "phrases": phrases},
            headers={"X-MCP-Secret": secret},
            timeout=15,
        )
        sess_resp.raise_for_status()
        token = sess_resp.json().get("token", "")
        preview_url = f"{base_url}/api/mcp/preview/{token}"
    except Exception as e:
        return [types.TextContent(type="text", text=f"Could not create preview session: {e}")]

    # Fetch thumbnails in parallel (up to 3 per phrase, max 12 total)
    to_fetch = []
    seen_phrases: dict = {}
    for item in shortlisted:
        phrase = item.get("phrase", "")
        seen_phrases[phrase] = seen_phrases.get(phrase, 0)
        if seen_phrases[phrase] < 3 and len(to_fetch) < 12:
            thumb = item.get("thumb") or item.get("download_url", "")
            if thumb:
                to_fetch.append((len(to_fetch) + 1, item, thumb))
            seen_phrases[phrase] += 1

    stock_count = len(shortlisted) - len(internal_shortlisted)
    internal_note = f" ({len(internal_shortlisted)} already in your library + {stock_count} stock)" if internal_shortlisted else ""
    contents: list = [types.TextContent(
        type="text",
        text=(
            f"Found **{len(shortlisted)} images**{internal_note} across {len(phrases)} phrase(s). "
            f"**[Open the combined gallery]({preview_url})** to pick visually — "
            f"library images are marked ✓.\n\n"
            f"Once you've selected, tell me and I'll call `get_selected_images` with token `{token}`."
        ),
    )]

    def _fetch_entry(entry):
        idx, item, thumb_url = entry
        img = _fetch_thumb(thumb_url)
        return idx, item, img

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(_fetch_entry, e): e for e in to_fetch}
        results = []
        for f in concurrent.futures.as_completed(futures):
            try:
                results.append(f.result())
            except Exception:
                pass

    results.sort(key=lambda x: x[0])

    for idx, item, img in results:
        label = f"**#{idx}** — {item.get('title', '')[:60]}"
        if item.get("photographer"):
            label += f" by {item['photographer']}"
        label += f" ({item.get('library', '').capitalize()})"
        contents.append(types.TextContent(type="text", text=label))
        if img:
            contents.append(img)

    return contents


async def _get_selected_images(args: dict) -> list[types.TextContent]:
    token = args.get("token", "").strip()
    if not token:
        return [types.TextContent(type="text", text=json.dumps({"error": "token required"}))]

    base_url = (Config.APP_BASE_URL or "http://localhost:5000").rstrip("/")
    secret = Config.MCP_INTERNAL_SECRET

    try:
        resp = _requests.get(
            f"{base_url}/api/mcp/preview/{token}/selection",
            headers={"X-MCP-Secret": secret},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]

    if not data.get("ready"):
        return [types.TextContent(type="text", text="The user hasn't made a selection yet. Ask them to open the gallery, choose images, and click 'Catalog Selected'.")]

    selected = data.get("selected", [])
    if not selected:
        return [types.TextContent(type="text", text="No images were selected in the gallery.")]

    cataloged = data.get("cataloged", []) if isinstance(data.get("cataloged", []), list) else []
    failures = data.get("failures", []) if isinstance(data.get("failures", []), list) else []

    internal = [img for img in selected if img.get("type") == "internal" or img.get("library") == "internal"]
    stock = [img for img in selected if img.get("type") != "internal" and img.get("library") != "internal"]

    lines = []
    if internal:
        lines.append(f"**{len(internal)} internal library image(s)** (already cataloged — no action needed):")
        for img in internal:
            slug = img.get("slug") or img.get("location", "")
            lines.append(f"  - {img.get('title', 'Untitled')} — slug: `{slug}`")

    if cataloged or failures:
        lines.append(f"\n**Stock cataloging results**: {len(cataloged)} succeeded, {len(failures)} failed.")
        for item in cataloged:
            title = item.get("title", "Untitled")
            slug = item.get("slug", "")
            location = item.get("location", "")
            detail = slug or location or "record created"
            lines.append(f"  - ✅ {title} — `{detail}`")
        for item in failures:
            title = item.get("title", "Untitled")
            err = item.get("error", "Cataloging failed")
            lines.append(f"  - ❌ {title} — {err}")
    elif stock:
        lines.append(f"\n**{len(stock)} stock image(s)** to catalog:")
        for i, img in enumerate(stock):
            lines.append(f"  - #{i+1} **{img.get('title','Untitled')}** ({img.get('library','').capitalize()}) — `{img.get('download_url','')}`")
        lines.append("\nCall `catalog_stock_image` for each stock image above.")
    else:
        lines.append("\nAll selected images are already in the library. No cataloging needed.")

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _catalog_stock_image(args: dict) -> list[types.TextContent]:
    import json
    import requests as req

    download_url = args.get("download_url", "").strip()
    original_filename = args.get("original_filename", "image.jpg").strip()
    category = args.get("category", "")
    source = args.get("source", "").strip()
    attribution = args.get("attribution", "").strip()
    license_info = args.get("license", "").strip()
    source_url = args.get("source_url", "").strip() or download_url
    source_asset_id = args.get("source_asset_id", "").strip()

    if not download_url:
        return [types.TextContent(type="text", text=json.dumps({"error": "download_url is required"}, ensure_ascii=False))]

    # Download the image
    try:
        resp = req.get(download_url, timeout=30)
        resp.raise_for_status()
        file_bytes = resp.content
    except Exception:
        return [types.TextContent(type="text", text=json.dumps({"error": "Download failed"}, ensure_ascii=False))]

    # Run full pipeline
    from src.ai_generator import AltTextGenerator
    from src.image_processor import process_image

    gen = AltTextGenerator()

    if Config.TEST_MODE:
        from src.local_client import LocalClient
        list_client = LocalClient()
        sp_client = None
        storage_mode = "local"
    else:
        from src.sharepoint_list_client import SharePointListClient
        from src.sharepoint_client import SharePointClient
        list_client = SharePointListClient()
        sp_client = SharePointClient()
        storage_mode = "sharepoint"

    try:
        result = process_image(
            file_bytes=file_bytes,
            original_filename=original_filename,
            category=category,
            generator=gen,
            list_client=list_client,
            sp_client=sp_client,
            image_folder=Config.IMAGE_FOLDER,
            storage_mode=storage_mode,
            source=source or None,
            attribution=attribution,
            license_info=license_info,
            source_url=source_url,
            source_asset_id=source_asset_id,
        )
        result["webp_url"] = _webp_url(result.get("location", ""))
        result = _add_delivery_metadata(result)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
    except Exception:
        return [types.TextContent(type="text", text=json.dumps({"error": "Cataloging failed"}, ensure_ascii=False))]


async def _get_image_url(args: dict) -> list[types.TextContent]:
    from urllib.parse import quote
    location = args.get("location", "").strip()
    if not location:
        return [types.TextContent(type="text", text=json.dumps({"error": "location is required"}))]
    secret = Config.MCP_INTERNAL_SECRET
    if not secret:
        return [types.TextContent(type="text", text=json.dumps({"error": "MCP_INTERNAL_SECRET not configured"}))]
    base = "https://library.liveproxima.org"
    url = f"{base}/api/mcp/thumbnail?path={quote(location)}&key={quote(secret)}"
    return [types.TextContent(type="text", text=json.dumps({"url": url, "location": location}))]


async def _backfill_image_derivatives(args: dict) -> list[types.TextContent]:
    slug = str(args.get("slug", "") or "").strip()
    if not slug:
        return [types.TextContent(type="text", text=json.dumps({"error": "slug is required"}))]

    list_client = _get_list_client()
    record = next(
        (
            item for item in list_client.get_all_records()
            if str(item.get("fields", {}).get("Slug", "") or "").strip() == slug
        ),
        None,
    )
    if not record:
        return [types.TextContent(type="text", text=json.dumps({"error": "Image not found", "slug": slug}))]

    if Config.STORAGE_MODE == "sharepoint":
        from src.sharepoint_client import SharePointClient
        sp_client = SharePointClient()
        storage_mode = "sharepoint"
    else:
        sp_client = None
        storage_mode = "local"
    try:
        from src.hero_backfill import backfill_record
        result = backfill_record(
            record,
            storage_mode=storage_mode,
            image_folder=Config.IMAGE_FOLDER,
            sp_client=sp_client,
        )
        fields = record.get("fields", {})
        delivery = _add_delivery_metadata({
            "slug": slug,
            "filename": fields.get("Filename", ""),
            "location": fields.get("Location", ""),
            "webp_url": _webp_url(fields.get("Location", "")),
            "status": fields.get("Status", ""),
        })
        return [types.TextContent(
            type="text",
            text=json.dumps({"ok": True, **result, "delivery": delivery}, indent=2, ensure_ascii=False),
        )]
    except Exception as exc:
        return [types.TextContent(type="text", text=json.dumps({
            "error": str(exc) or "Backfill failed",
            "slug": slug,
        }, ensure_ascii=False))]


async def _catalog_image_from_file(args: dict) -> list[types.TextContent]:
    import json

    image_data = args.get("image_data", "").strip()
    filename = args.get("filename", "image.jpg").strip() or "image.jpg"
    category = args.get("category", "")

    if not image_data:
        return [types.TextContent(type="text", text=json.dumps({"error": "image_data is required"}, ensure_ascii=False))]

    try:
        file_bytes = base64.b64decode(image_data)
    except Exception:
        return [types.TextContent(type="text", text=json.dumps({"error": "Invalid base64 data"}, ensure_ascii=False))]

    from src.ai_generator import AltTextGenerator
    from src.image_processor import process_image

    gen = AltTextGenerator()

    if Config.TEST_MODE:
        from src.local_client import LocalClient
        list_client = LocalClient()
        sp_client = None
        storage_mode = "local"
    else:
        from src.sharepoint_list_client import SharePointListClient
        from src.sharepoint_client import SharePointClient
        list_client = SharePointListClient()
        sp_client = SharePointClient()
        storage_mode = "sharepoint"

    try:
        result = process_image(
            file_bytes=file_bytes,
            original_filename=filename,
            category=category,
            generator=gen,
            list_client=list_client,
            sp_client=sp_client,
            image_folder=Config.IMAGE_FOLDER,
            storage_mode=storage_mode,
            source="Internal",
        )
        result["webp_url"] = _webp_url(result.get("location", ""))
        result = _add_delivery_metadata(result)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
    except Exception:
        return [types.TextContent(type="text", text=json.dumps({"error": "Cataloging failed"}, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
