#!/usr/bin/env python3
"""One-off script: derive and patch the Slug field for records that lack one.

Follows the same naming convention used everywhere else in the app (Filename
== "{slug}.webp", see src/image_processor.py:slug_from_text and
src/rename_assets.py:slugify): the Slug is just the WebP filename stem,
lowercased and hyphenated. Since Filenames in this library are already
unique, lowercase, hyphenated slugs with the extension attached, this is a
straight derivation — no collision handling needed.

These are exactly the records src.hero_backfill.backfill_record rejects with
"Record must have an id and slug", since it can't build a Metadata/Hero path
without a slug.

Dry-run by default; pass --apply to actually patch records.

Usage:
    .venv/bin/python3 -m scripts.backfill_missing_slugs            # preview only
    .venv/bin/python3 -m scripts.backfill_missing_slugs --apply    # patch records
"""

import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from src.config import Config
from src.image_processor import slug_from_text


def main() -> None:
    apply = "--apply" in sys.argv

    if Config.TEST_MODE:
        from src.local_client import LocalClient
        client = LocalClient()
    else:
        from src.sharepoint_list_client import SharePointListClient
        client = SharePointListClient()

    records = client.get_all_records()
    targets = [r for r in records if not str(r.get("fields", {}).get("Slug", "") or "").strip()]

    print(f"Records missing Slug: {len(targets)} of {len(records)}")
    print("=" * 60)

    existing_slugs = {
        str(r.get("fields", {}).get("Slug", "") or "").strip()
        for r in records
    }
    existing_slugs.discard("")

    patches = []
    skipped = []
    for rec in targets:
        fields = rec.get("fields", {})
        rec_id = str(rec.get("id", "")).strip()
        filename = str(fields.get("Filename", "") or "").strip()
        if not filename:
            skipped.append((rec_id, "no Filename to derive a slug from"))
            continue

        stem = PurePosixPath(filename).stem
        slug = slug_from_text(stem)
        if slug in existing_slugs:
            skipped.append((rec_id, f"derived slug {slug!r} collides with an existing record"))
            continue
        existing_slugs.add(slug)

        print(f"[{rec_id}] {filename} -> Slug={slug!r}")
        patches.append((rec_id, {"Slug": slug}))

    if skipped:
        print(f"\nSkipped {len(skipped)} record(s):")
        for rec_id, reason in skipped:
            print(f"  [{rec_id}] {reason}")

    print(f"\nRecords to patch: {len(patches)}")
    if not apply:
        print("Dry run only — pass --apply to patch these records.")
        return

    if patches:
        result = client.bulk_patch_fields(patches)
        print(f"Patched {result.get('updated', 0)} records.")
        if result.get("failed_ids"):
            print(f"Failed to patch: {result['failed_ids']}")


if __name__ == "__main__":
    main()
