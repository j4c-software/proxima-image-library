# Production Deployment Checklist

Steps required before and after each production deployment to Azure App Service.

---

## Environment

- [ ] Azure App Service -> `PP-App-Serv` -> Settings -> Environment variables / Application settings:
  - Add all runtime values from the production config set here rather than in a checked-in file
  - Restart the app after saving changes
- [ ] Set `TEST_MODE=false`
- [ ] Set `STORAGE_MODE=sharepoint`
- [ ] Set `DEV_AUTH_BYPASS=false` (required: runtime validation blocks bypass outside TEST_MODE)
- [ ] Set `MAINTENANCE_ADMIN_USERS` with one or more allowed admin identities (email/UPN)
- [ ] Confirm required env vars are set in Azure App Settings:
  - `ANTHROPIC_API_KEY`
  - `SHAREPOINT_TENANT_ID`, `SHAREPOINT_CLIENT_ID`, `SHAREPOINT_CLIENT_SECRET`
  - `SHAREPOINT_SITE_ID`, `SHAREPOINT_DRIVE_ID`
  - `FLASK_SECRET_KEY`
  - `MSAL_CLIENT_ID`, `MSAL_CLIENT_SECRET`, `MSAL_TENANT_ID`, `MSAL_REDIRECT_URI`
  - `CORS_ORIGINS`
- [ ] Confirm security settings are explicit in Azure App Settings:
  - `SESSION_COOKIE_SECURE=true`
  - `SESSION_COOKIE_SAMESITE=Lax`
  - `MAX_UPLOAD_BYTES=20971520`
  - `MAX_REQUEST_BYTES=83886080`
  - `RATE_LIMIT_WINDOW_SECONDS=60`
  - `RATE_LIMIT_AUTH_REQUESTS=20`
  - `RATE_LIMIT_STREAM_REQUESTS=8`
- [ ] Confirm recommended SharePoint settings are explicit:
  - `SHAREPOINT_LIST_NAME` (default `Assets`)
  - `SHAREPOINT_IMAGE_FOLDER` (default `Images`)
- [ ] If stock search is required in production, also set:
  - `PEXELS_API_KEY`, `SHUTTERSTOCK_CLIENT_ID`, `SHUTTERSTOCK_CLIENT_SECRET`
  - `UNSPLASH_ACCESS_KEY`, `PIXABAY_API_KEY`

---

## MCP Server (Claude Desktop)

- [ ] Update `cwd` in `claude_desktop_config.json` if project path changes
- [ ] Remove `TEST_MODE` override from MCP server env if set
- [ ] Point MCP server at production app or keep as local stdio — decide before go-live
- [ ] If using production-hosted image previews from Claude Desktop, update all hardcoded localhost URLs in `src/mcp_server.py` (`/thumbnail` and `/image` references)
- [ ] Restart Claude Desktop after any config change

---

## Auth

- [ ] Microsoft Entra ID -> App registrations -> Authentication:
  - Add production redirect URI:
  `https://<production-hostname>/auth/callback`
- [ ] Microsoft Entra ID -> App registrations -> Certificates & secrets:
  - Ensure the app registration has an active client secret
  - Copy that secret value into Azure App Service Application Settings as `MSAL_CLIENT_SECRET` unless switching to Key Vault references
- [ ] Confirm `MSAL_REDIRECT_URI` exactly matches the Azure app registration redirect URI
- [ ] Verify MSAL login flow end-to-end with a liveproxima.org account
- [ ] Confirm sign-out clears session correctly
- [ ] Verify expired-session behavior forces re-login (session expiry enforced from token claims)
- [ ] Verify maintenance access control:
  - Allowlisted user can access `/maintenance` and `/api/maintenance/*`
  - Non-allowlisted authenticated user receives `403`
- [ ] Confirm TEST_MODE-only auth helper endpoint is unavailable in production:
  - `POST /auth/test-expire-session` should return `404` when `TEST_MODE=false`

---

## Data & Storage

- [ ] Clear any test records from local JSON store (`test_data/local_table.json`) before packaging/releasing data snapshots
- [ ] Verify SharePoint Assets list columns match schema in `sharepoint_list_client.py`
- [ ] Confirm `SHAREPOINT_IMAGE_FOLDER` path exists in the target drive
- [ ] Run a test upload end-to-end and verify image appears in library
- [ ] Run one stock "Add to Library" flow and verify:
  - High-Res file lands under `High-Res/{source}/...`
  - WebP file lands under `WebP/{category}/...`
  - Metadata record includes `Source`, `Location`, and `High-Res Location`

---

## Edit List

- [ ] Review `EDIT_LIST.md` and confirm there are no unresolved go-live blockers
- [ ] Keep `EDIT_LIST.md` as a living change log; do not delete it as part of deployment

---

## Deployment Pipeline

- [ ] GitHub -> Repository settings -> Secrets and variables -> Actions:
  - Confirm `AZURE_CREDENTIALS` exists for `.github/workflows/azure-deploy.yml`
  - If missing, recreate it from an Azure service principal with permission to deploy to `PP-App-Serv`
- [ ] Trigger deployment from GitHub Actions or push to `main`
- [ ] Watch the GitHub Actions run created from the latest push and wait for a clean success before testing production
- [ ] In Azure App Service -> Deployment Center / Log stream, confirm the app boots with `bash startup.sh`
- The workflow sets `WEBSITE_RUN_FROM_PACKAGE=1` on `PP-App-Serv` before every deploy so Kudu mounts the zip
  read-only instead of extracting its ~3,700 vendored dependency files one by one (this cut deploy time from
  2–7 minutes to well under a minute). Because of this, `/home/site/wwwroot` is read-only at runtime — any
  feature that needs to persist state must write under `/home/...` (see `maintenance_helpers.py` and the
  `_DOT_ENV_PATH` / `_SS_COUNTER_PATH` handling in `app.py` for the existing pattern), never back into the app directory.

### Current live deploy note

- Latest upload reliability fix pushed: commit `767ac1d` (`Use shared staging dir for uploads`)
- The corresponding deploy run is `24216649585`
- This change moves staged uploads off instance-local temp storage and onto shared Azure `/home` storage to prevent `Unknown or expired file ID` during `/api/upload/process`

---

## Post-Deploy Checks

- [ ] Health endpoints return 200: `GET /health` and `GET /healthz`
- [ ] Login flow on `https://library.liveproxima.org` redirects back to `https://library.liveproxima.org/auth/callback`
- [ ] Library loads and images display
- [ ] Upload pipeline completes without connection errors or `Unknown or expired file ID`
- [ ] Uploaded item appears in the library and its SharePoint List metadata record is created
- [ ] Review queue loads and status changes persist
- [ ] Maintenance page loads for admin accounts and is blocked for non-admin accounts
- [ ] Stock search returns results from configured sources
- [ ] Shutterstock quota modal fires on card click
- [ ] Download modal shows metadata and triggers file download
- [ ] MCP tools (`search_image_library`, `search_stock_photos`, `catalog_stock_image`, `catalog_image_from_file`) respond correctly in Claude Desktop
