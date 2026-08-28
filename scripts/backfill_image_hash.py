"""One-off script: compute and store 'Image Hash' for existing records that lack one.

Reads each record's WebP file, computes the perceptual dHash (same algorithm
used at ingest time in src/image_processor.py), and patches the record so the
Duplicate Detector can find image-content duplicates in the existing library,
not just newly-ingested ones.

Usage:
    TEST_MODE=true STORAGE_MODE=local .venv/bin/python3 -m scripts.backfill_image_hash
    .venv/bin/python3 -m scripts.backfill_image_hash   # live SharePoint mode
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from src.config import Config
from src.image_processor import compute_perceptual_hash

if Config.TEST_MODE:
    from src.local_client import LocalClient
    client = LocalClient()
    sp = None
else:
    from src.sharepoint_client import SharePointClient
    from src.sharepoint_list_client import SharePointListClient
    client = SharePointListClient()
    sp = SharePointClient()

records = client.get_all_records()
targets = [r for r in records if not r.get("fields", {}).get("Image Hash", "").strip()]

print(f"Records missing Image Hash: {len(targets)} of {len(records)}")
print("=" * 60)

patches = []
processed = failed = skipped = 0

for idx, rec in enumerate(targets, 1):
    fields = rec.get("fields", {})
    rec_id = str(rec.get("id", "")).strip()
    filename = fields.get("Filename", "") or "unknown"
    location = fields.get("Location", "") or ""

    if not rec_id or not location:
        print(f"[{idx}/{len(targets)}] SKIP (no location): {filename}")
        skipped += 1
        continue

    print(f"[{idx}/{len(targets)}] {filename}", end=" ... ", flush=True)

    try:
        if sp is not None:
            root = (Config.SHAREPOINT_IMAGE_FOLDER or "").strip().strip("/")
            webp_path = f"{root}/WebP/{location}" if root else f"WebP/{location}"
            file_bytes = sp.get_file_bytes(webp_path)
        else:
            webp_path = Path(Config.IMAGE_FOLDER) / "WebP" / location
            file_bytes = webp_path.read_bytes()

        image_hash = compute_perceptual_hash(file_bytes)
        patches.append((rec_id, {"Image Hash": image_hash}))
        print(f"OK → {image_hash}")
        processed += 1

    except Exception as e:
        print(f"FAIL: {e}")
        failed += 1

if patches:
    result = client.bulk_patch_fields(patches)
    print("=" * 60)
    print(f"Patched {result.get('updated', 0)} records.")
    if result.get("failed_ids"):
        print(f"Failed to patch: {result['failed_ids']}")

print("=" * 60)
print(f"Done — processed: {processed}, skipped: {skipped}, failed: {failed}")
