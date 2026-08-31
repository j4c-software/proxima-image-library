# Proxima Image Library

AI-powered image asset management for Proxima. Scans a local image folder, generates alt text and tags via Claude vision, syncs metadata to a SharePoint List, and provides a web UI for browsing, stock photo search, image review, and download.

**Tech stack:** Python 3 · Flask · Claude `claude-sonnet-4-6` · SharePoint List · Microsoft Graph API · MSAL · Pillow · Vanilla HTML/CSS/JS

---

## Quick Start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in keys and IMAGE_FOLDER
```

**Development (local data, no SharePoint required):**

```bash
source .venv/bin/activate
TEST_MODE=true STORAGE_MODE=local DEV_AUTH_BYPASS=true .venv/bin/python3 -m flask --app src.app run --port 5000 --debug
```

`.env` should have `TEST_MODE=true` and `STORAGE_MODE=local`.
Set `DEV_AUTH_BYPASS=true` to skip Microsoft login in local TEST_MODE.

To test real Microsoft auth locally, run with `DEV_AUTH_BYPASS=false` and use
`http://localhost:5000` consistently for login/callback. Do not mix
`localhost` and `127.0.0.1` during the same auth flow.

**Live (SharePoint backend):**

```bash
source .venv/bin/activate
flask --app src.app run --port 5000
```

Open **[http://localhost:5000](http://localhost:5000)** — login via Microsoft/MSAL on first visit.

When `DEV_AUTH_BYPASS=true` in TEST_MODE, the app auto-authenticates a local session and opens directly.

Minimum `.env` for dev: `ANTHROPIC_API_KEY`, `IMAGE_FOLDER`, `TEST_MODE=true`, `STORAGE_MODE=local`, `FLASK_SECRET_KEY`.
Optional for local login bypass: `DEV_AUTH_BYPASS=true`.
MSAL vars are required when testing real auth flow or running live mode.
Full setup: see [development.md](development.md#environment-variables-reference)

## Production Notes

- Production host: `https://library.liveproxima.org`
- Azure App Service: `PP-App-Serv`
- Store production secrets and runtime configuration in Azure App Service Application Settings, not in a checked-in file
- Microsoft Entra redirect URI must exactly match the production host callback: `https://library.liveproxima.org/auth/callback`
- On Azure App Service, staged uploads are stored under shared `/home` storage so the upload stage and process requests can resolve the same file across requests
- After each production deploy, verify one complete upload and confirm the item appears in the library with metadata persisted

---

## Project Structure

```text
src/
├── app.py                      Flask routes, thumbnail serving, SSE streaming
├── main.py                     CLI scan → generate → upload pipeline
├── ai_generator.py             Claude vision — alt text + tags
├── image_processor.py          Upload/stock image processing pipeline
├── sharepoint_list_client.py   SharePoint List CRUD (live mode)
├── sharepoint_client.py        SharePoint file operations via Graph API
├── local_client.py             Local JSON store, same interface (TEST_MODE)
├── image_scanner.py            Recursive image discovery
├── tag_library.py              Tag vocabulary management
├── config.py                   Env var loading and Config object
├── mcp_server.py               MCP server — search_image_library, search_stock_photos, catalog_image_from_file
├── rename_assets.py            Batch rename to {prefix}-{slug}.{ext}
└── stock_client.py             Pexels / Shutterstock / Unsplash / Pixabay search + full metadata

templates/
├── index.html                  Search-first library browser — hero, category tiles, browse grid
├── maintenance.html            Admin maintenance console — M1–M8 + M10–M20 operations
├── stock_search.html           Stock photo search — phrase chips → results grid → download modal
├── review.html                 Review queue — approve/reject/archive pending images
├── upload.html                 Image upload and catalog pipeline with SSE progress
└── login_error.html            Access denied/error template

Tag Manager is embedded in the Maintenance Console. Legacy route `/tag-manager` redirects to `/maintenance#tag-manager`.

tests/                          pytest — rename_assets, ImageScanner
test_data/                      local_table.json — 253 records for local dev/testing
```

`LocalClient` and `SharePointListClient` share identical interfaces. `TEST_MODE=true` swaps between them with zero code changes.

---

## Key Commands

| Task | Command |
| ---- | ------- |
| Start server (dev) | `TEST_MODE=true STORAGE_MODE=local DEV_AUTH_BYPASS=true .venv/bin/python3 -m flask --app src.app run --port 5000 --debug` |
| Start server (dev auth required) | `TEST_MODE=true STORAGE_MODE=local DEV_AUTH_BYPASS=false .venv/bin/python3 -m flask --app src.app run --port 5000` |
| Start server (live) | `.venv/bin/python3 -m flask --app src.app run --port 5000` |
| Run tests | `.venv/bin/python3 -m pytest -v` |
| Run automated T1 suite | `.venv/bin/python3 scripts/run_t1_suite.py` |
| Sync images to SharePoint | `.venv/bin/python3 -u -m src.main` |
| Rename images (preview) | `.venv/bin/python3 -m src.rename_assets --prefix proxima` |
| Rename images (apply) | `.venv/bin/python3 -m src.rename_assets --prefix proxima --apply` |

### Maintenance Access

- `/maintenance` and `/api/maintenance/*` are admin-gated in normal auth mode.
- Set `MAINTENANCE_ADMIN_USERS` to a comma-separated allowlist of emails/UPNs.
- In local TEST mode only, `DEV_AUTH_BYPASS=true` allows bypass login for faster iteration.

### Auth Session Validation (TEST_MODE only)

- Session expiry is enforced from the MSAL `exp` claim.
- Local auth testing helper endpoint: `POST /auth/test-expire-session`
  - available only when `TEST_MODE=true`
  - marks current session expired for deterministic manual testing
  - returns redirect to `/login` when not authenticated

---

## MCP Server (Claude Desktop integration)

Register in `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "proxima-image-library": {
      "command": "/Users/mike-j4c/Projects/proxima-image-library/.venv/bin/python3",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/Users/mike-j4c/Projects/proxima-image-library",
      "env": {
        "PYTHONPATH": "/Users/mike-j4c/Projects/proxima-image-library",
        "TEST_MODE": "true",
        "STORAGE_MODE": "local"
      }
    }
  }
}
```

`TEST_MODE` and `STORAGE_MODE` are set explicitly here because Claude Desktop may not load `.env` from `cwd` reliably.

## Production deployment safety

Authoritative production mapping:

| Public domain | Azure App Service | Normal deployment path |
| --- | --- | --- |
| `library.liveproxima.org` | `PP-MCP-Server` | Push to `main` |

Before calling a deployment successful, verify the public hostname resolves to the intended App Service. Do not infer the target from an App Service name, a previous workflow run, or an Azure "Running" status.

`PP-App-Serv` is **not** the normal target for this image library. It can only be selected through a manual workflow dispatch for a corrective restore, and that restore must specify a full 40-character Git commit SHA. Never select it for `library.liveproxima.org` without separate authorization and hostname verification.

For every deployment, record the commit SHA, target App Service, GitHub Actions run URL, deployment outcome, and a live authenticated route check. If authenticated checking is unavailable, record the exact authorization blocker rather than claiming application-level verification.

### MCP Tools

| Tool | Description |
| ---- | ----------- |
| `search_image_library` | Search the image library by keyword; returns ranked results with thumbnails and any available backfill action |
| `search_stock_photos` | Search Pexels, Shutterstock, Unsplash, Pixabay concurrently |
| `catalog_image_from_file` | Accept a base64 image, run the full processing pipeline, add to library |
| `backfill_image_derivatives` | Explicitly backfill a selected result after the user confirms; never runs automatically |

Newly ingested assets retain the legacy `location` and `webp_url` fields while adding `original`, `hero`, `standard`, `width`, `height`, `qualityTier`, `focalPoint`, `preferredHeroUrl`, and `standardWebUrl` when sidecar metadata is available. Existing catalog records without sidecars continue to return their legacy fields unchanged.

### Hero-readiness dry run

Run the read-only inventory before any separately approved backfill:

```bash
.venv/bin/python scripts/inventory_hero_readiness.py > hero-readiness.json
```

The report groups originals that are at least 2560px wide, exactly 1600px wide, 1601–2559px wide, below 1600px, unavailable, and missing source/rights/focal-point metadata. It does not write derivatives or modify records.

### Claude Article Automation API

For no-copy/paste integration from a Claude article-writing project, use:

- `POST /api/mcp/claude-article`
- Header: `X-MCP-Secret: <MCP_INTERNAL_SECRET>`

Request body (example):

```json
{
  "article_title": "Urban Lessons",
  "article_body": "What happens when the approach that worked somewhere else stops working here?...",
  "photo_suggestions": ["teacher with students", "students in city"],
  "include_search": true,
  "search_limit": 8
}
```

Behavior:

- If `photo_suggestions` is omitted or empty, phrases are auto-extracted from `article_body`.
- Returns normalized phrase list and, when `include_search=true`, grouped stock results (Pexels/Shutterstock/Unsplash/Pixabay).
- Use returned phrases/results to drive follow-up calls to:
  - `POST /api/mcp/catalog-stock`
  - `POST /api/mcp/catalog-from-file`

### One-Call Full Automation API

For full automation without copy/paste or step-by-step orchestration, use:

- `POST /api/mcp/claude-article-auto`
- Header: `X-MCP-Secret: <MCP_INTERNAL_SECRET>`

Key request fields:

- `approval_mode`: `manual` or `auto`
  - `manual`: extract + search + shortlist only (no catalog writes)
  - `auto`: extract + search + shortlist + catalog selected candidates
- `photo_suggestions`: optional array; if absent, phrases are extracted from `article_body`
- `search_limit`: results per library per phrase (max 25)
- `max_catalog_items`: max shortlisted items to catalog in `auto` mode
- `preferred_libraries`: library priority order, e.g. `["pexels","unsplash","pixabay","shutterstock"]`
- `category`: optional forced category (`Headshots`, `Community`, `Locations`, `Situations`, `Graphics`, `Banners`)

Response includes:

- normalized phrase list
- shortlisted candidates
- in `auto` mode: `cataloged` items and `failures`

### Claude Action Templates (Copy/Paste)

Set these once in your Claude project Action/Webhook config:

- Method: `POST`
- URL: `https://library.liveproxima.org/api/mcp/claude-article-auto` (or local `http://127.0.0.1:5000/api/mcp/claude-article-auto`)
- Header: `Content-Type: application/json`
- Header: `X-MCP-Secret: <MCP_INTERNAL_SECRET>`

Manual approval mode payload (extract + search + shortlist only):

```json
{
  "article_title": "{{title}}",
  "article_body": "{{article_text}}",
  "approval_mode": "manual",
  "search_limit": 8,
  "max_catalog_items": 5,
  "preferred_libraries": ["pexels", "unsplash", "pixabay", "shutterstock"],
  "category": "Situations"
}
```

Full auto mode payload (extract + search + catalog):

```json
{
  "article_title": "{{title}}",
  "article_body": "{{article_text}}",
  "approval_mode": "auto",
  "search_limit": 6,
  "max_catalog_items": 3,
  "preferred_libraries": ["pexels", "unsplash", "pixabay"],
  "category": "Situations"
}
```

Optional explicit suggestions payload (skip extraction step):

```json
{
  "article_title": "{{title}}",
  "photo_suggestions": [
    "teacher with students",
    "students in city",
    "urban classroom scene"
  ],
  "approval_mode": "auto",
  "search_limit": 4,
  "max_catalog_items": 2,
  "preferred_libraries": ["pexels", "unsplash"]
}
```

Quick local test command:

```bash
SECRET=$(grep '^MCP_INTERNAL_SECRET=' .env | cut -d= -f2-)
curl -s -X POST http://127.0.0.1:5000/api/mcp/claude-article-auto \
  -H "Content-Type: application/json" \
  -H "X-MCP-Secret: $SECRET" \
  -d '{
    "article_title":"Urban Lessons",
    "article_body":"What happens when the approach that worked somewhere else stops working here? Sometimes the city itself is the teacher if we are willing to be students.",
    "approval_mode":"manual",
    "search_limit":4,
    "max_catalog_items":2
  }'
```

---

## Documentation

| Document | Contents |
| -------- | -------- |
| [development.md](development.md) | Setup, architecture, how-to guides, code conventions, gotchas |
| [specification.md](specification.md) | Image output targets, naming, SharePoint List schema, AI metadata spec |
| [search-parameter.md](search-parameter.md) | Stock photo API parameters, auth, attribution requirements |
| [project-scope.md](project-scope.md) | Feature definitions, application parameters, and developer onboarding (including guidance for developers using OpenAI-based coding tools) |
| [EDIT_LIST.md](EDIT_LIST.md) | Applied changes and active future development queue |
| [PRODUCTION_DEPLOY.md](PRODUCTION_DEPLOY.md) | Deployment checklist for Azure App Service |
| [NEXT_TIME_START.md](NEXT_TIME_START.md) | Quick resume checklist for next session (env, run modes, tests, and current checkpoint) |
