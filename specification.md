# Image Specification

Defines image transformation targets, storage structure, naming conventions, and metadata requirements for the Proxima Image Library. All values here are the authoritative source for implementation — any hardcoded values in the codebase should match these.

## Storage Structure

Every processed image produces two output files stored in parallel directories within SharePoint (local path in development).

```text
Images/
├── High-Res/
│   ├── ShutterStock/
│   ├── AdobeStock/
│   ├── Unsplash/
│   ├── Pexels/
│   ├── Pixabay/
│   └── Internal/
└── WebP/
    ├── Headshots/
    ├── Community/
    ├── Locations/
    ├── Situations/
    ├── Graphics/
    └── Banners/
```

**High-Res** preserves the original pixel data exactly — no resize, no re-encode. The source format is retained (JPEG, PNG, etc.). Files are stored under source folders (for example, `High-Res/Unsplash/...` or `High-Res/Internal/...`).

**WebP** is the delivery copy used in CMS, Webflow, and email. It is always `.webp` regardless of input format. Files are named `{slug}.webp`.

### Category definitions

| Folder | Contents |
| ------ | -------- |
| `Headshots/` | Staff and individual portrait photos |
| `Community/` | People in community contexts — conversations, groups, events, service |
| `Locations/` | SF landmarks, urban scenes, architecture, landscapes |
| `Situations/` | Conceptual/thematic photos — homelessness, isolation, prayer, hardship |
| `Graphics/` | Logos, icons, illustrations, vectors, diagrams |
| `Banners/` | Banner and background images for email/web |

### High-Res source folders

| Folder | Meaning |
| ------ | ------- |
| `ShutterStock/` | Shutterstock source images |
| `AdobeStock/` | Adobe Stock source images |
| `Unsplash/` | Unsplash source images |
| `Pexels/` | Pexels source images |
| `Pixabay/` | Pixabay source images |
| `Internal/` | Uploaded or manually provided internal images |

### Path resolution

The `Location` field stores the category-relative WebP path (e.g. `Headshots/proxima-mike7.webp`). The app constructs full SharePoint paths as:

- **Display/serve:** `Images/WebP/{Location}`
- **Original download:** `Images/High-Res/{High-Res Location}`

The `SHAREPOINT_IMAGE_FOLDER` env var sets the root (default: `Images`).

---

## WebP Output Specification

| Parameter | Value | Notes |
| --------- | ----- | ----- |
| Format | `.webp` | Always, regardless of input format |
| Max longest side | 1600 px | Scale down if longest side exceeds 1600 px; never upscale |
| Resize mode | Fit within bounds | Maintain aspect ratio (no crop, no distortion) |
| Quality | 80 | Balances file size and visual quality for web |
| Color space | sRGB | Convert on save if source is a different profile |
| Transparency | Preserved | PNG/WebP alpha retained; JPEG has no alpha to preserve |
| Progressive / lossless | No | Standard lossy WebP |

### Size reference

| Usage context | Typical max dimension | Notes |
| ------------- | --------------------- | ----- |
| Webflow hero image | 1920 × 1080 px | Full-bleed background |
| Article / blog photo | 1280 × 960 px | Inline content image |
| Card / thumbnail | 800 × 600 px | Grid or card component |
| Headshot / portrait | 600 × 800 px | People images |

The 1920 px max covers all use cases. Individual assets may be cropped or resized further at the CMS level.

---

## Thumbnail Specification (Browser UI only)

Thumbnails are generated on-the-fly by the Flask app for display in the internal browser. They are **not stored to disk**.

| Parameter | Value |
| --------- | ----- |
| Max dimension | 240 × 240 px |
| Format | JPEG |
| Quality | 85 |
| Color space | RGB (alpha dropped) |
| Implemented in | [src/app.py](src/app.py) — `_serve_image()` |

---

## File Naming Convention

All asset files follow the pattern defined in [src/rename_assets.py](src/rename_assets.py):

```text
{prefix}-{index:04d}-{slug}.{ext}
```

| Component | Description | Example |
| --------- | ----------- | ------- |
| `prefix` | Project identifier | `proxima` |
| `index` | Zero-padded 4-digit sequence number | `0042` |
| `slug` | Lowercase, hyphen-separated words from alt text | `volunteer-serving-meal` |
| `ext` | File extension | `.webp` or `.jpg` |

**Examples:**

```text
proxima-0001-two-volunteers-kitchen-service.webp
proxima-0042-golden-gate-bridge-sunset-landscape.webp
proxima-0103-youth-group-outdoor-celebration.webp
```

**Slug rules:**

- Lowercase only
- Alphanumeric characters and hyphens only (no spaces, underscores, or special characters)
- Multiple hyphens collapsed to one
- Maximum 60 characters

---

## Input Format Support

| Format | Extension(s) | Notes |
| ------ | ------------ | ----- |
| JPEG | `.jpg`, `.jpeg` | Most common source format |
| PNG | `.png` | Preserves transparency in WebP output |
| WebP | `.webp` | Re-encoded to spec if quality or size differs |
| GIF | `.gif` | Static frame only; animation not preserved |

Controlled by the `SUPPORTED_FORMATS` environment variable (default: `.jpg,.jpeg,.png,.gif,.webp`).

---

## AI Metadata Specification

Generated by Claude vision (`claude-sonnet-4-6`) in [src/ai_generator.py](src/ai_generator.py).

### Alt Text

| Parameter | Value |
| --------- | ----- |
| Max length | 125 characters |
| Style | Descriptive, no "Image of" or "Picture of" prefix |
| Purpose | Screen reader accessibility (WCAG 2.1 AA) |
| Model | `claude-sonnet-4-6` |
| Max tokens | 150 |

### Tags

| Parameter | Value |
| --------- | ----- |
| Count | 2–5 tags per image |
| Source | Predefined vocabulary only (no invented tags) |
| Format | Comma-separated string |
| Max tokens | 80 |

**Predefined tag vocabulary:**

| Category | Tags |
| -------- | ---- |
| People | `people`, `headshot`, `group`, `individual`, `staff`, `volunteer`, `family`, `youth`, `children`, `elderly`, `unhoused`, `neighbor`, `community` |
| Location — SF | `city`, `san-francisco`, `bay-area`, `golden-gate`, `mission-district` |
| Location — type | `landscape`, `architecture`, `street`, `cafe`, `office`, `church`, `indoor`, `outdoor`, `park`, `beach`, `mountains`, `waterfront`, `urban`, `suburban`, `rural`, `forest`, `plaza`, `rooftop`, `bridge`, `neighborhood` |
| Emotion / theme | `hope`, `connection`, `service`, `prayer`, `celebration`, `hardship`, `joy`, `loneliness`, `generosity` |
| Content type | `icon`, `logo`, `illustration`, `graphic`, `vector`, `document`, `map`, `badge`, `partner` |
| Framing | `portrait`, `thumbnail`, `banner`, `background`, `photo` |

---

## Library Record Fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `Filename` | Text | Original filename with extension |
| `Alt Text` | Text | AI-generated alt text (max 125 chars) |
| `Tags` | Text | Comma-separated tag string |
| `Status` | Select | `pending-review` → `approved` / `rejected` / `archived` |
| `Slug` | Text | URL-safe slug derived from alt text |
| `Location` | Text | WebP path relative to `Images/WebP/` (e.g. `Headshots/proxima-mike7.webp`) |
| `High-Res Location` | Text | Original path relative to `Images/High-Res/` (e.g. `Unsplash/proxima-mike7-original.jpg`) |
| `Source` | Text | Canonical source folder name (`ShutterStock`, `AdobeStock`, `Unsplash`, `Pexels`, `Pixabay`, `Internal`) |
| `Image Hash` | Text | 16-char hex dHash of the WebP pixel content; used by the Duplicate Detector to catch re-ingested duplicate photos even when AI-generated alt text/slug differ |

---

## Processing Pipeline Reference

```text
Source image (any supported format)
        │
        ├──► High-Res/  ← Copy original, no transformation
        │
        ├──► WebP/      ← Resize longest side to ≤1600 px, encode to WebP q80
        │
        └──► Metadata store (SharePoint List / local JSON)
                 ← Claude generates alt text + tags
                           Slug derived from alt text
                           Record written with filename, location, status=pending-review
```

This pipeline is implemented in the current codebase for upload and stock-catalog flows.

---

## Auth and Session Notes

- Runtime auth is handled in Flask via MSAL session cookies and `@login_required` route protection.
- Session expiry is enforced from token claims (`exp`) during authenticated requests.
- TEST_MODE-only manual helper endpoint: `POST /auth/test-expire-session`
    - Purpose: deterministic local validation of expired-session behavior
    - Availability: only when `TEST_MODE=true`
    - Method: POST only
