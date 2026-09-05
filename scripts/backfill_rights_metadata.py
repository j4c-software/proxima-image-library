#!/usr/bin/env python3
"""One-off script: close "rights" metadata gaps (missing attribution AND license)
by recording an honest license note per source — never a fabricated photographer
name. Selective and non-destructive: only touches records where both attribution
and license are currently empty; never overwrites either field if either is set.

Source -> license text:
    Internal            "Internal — Proxima Partners"
    Pexels/Pixabay/Unsplash
                         "<Source> License — free for commercial use,
                         attribution not required."

Any other source (e.g. ShutterStock, AdobeStock — paid/licensed libraries) is
skipped and reported, since a free-use note would not be accurate for them.

Dry-run by default; pass --apply to actually write the license field.

Usage:
    .venv/bin/python3 -m scripts.backfill_rights_metadata            # preview only
    .venv/bin/python3 -m scripts.backfill_rights_metadata --apply    # write license field
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from src.config import Config
from src.image_metadata import read_metadata, write_metadata

FREE_USE_SOURCES = {"Pexels", "Pixabay", "Unsplash"}

LICENSE_TEXT = {
    "Internal": "Internal — Proxima Partners",
    **{
        source: f"{source} License — free for commercial use, attribution not required."
        for source in FREE_USE_SOURCES
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the license field (default: dry run / report only)")
    args = parser.parse_args()

    if Config.TEST_MODE:
        from src.local_client import LocalClient
        client = LocalClient()
        storage_mode = "local"
        sp_client = None
    else:
        from src.sharepoint_client import SharePointClient
        from src.sharepoint_list_client import SharePointListClient
        client = SharePointListClient()
        sp_client = SharePointClient()
        storage_mode = "sharepoint"

    records = client.get_all_records()
    print(f"Scanning {len(records)} record(s)...")

    candidates = []
    skipped_unknown_source = []
    no_sidecar = []

    for record in records:
        fields = record.get("fields", {})
        slug = str(fields.get("Slug", "") or "").strip()
        source = str(fields.get("Source", "") or "").strip()
        if not slug:
            continue

        metadata = read_metadata(slug, storage_mode=storage_mode, image_folder=Config.IMAGE_FOLDER, sp_client=sp_client)
        if not isinstance(metadata, dict) or not metadata:
            continue  # no sidecar — outside this script's scope (see run_hero_backfill)

        original = metadata.get("original", {}) if isinstance(metadata.get("original"), dict) else {}
        if original.get("attribution") or original.get("license"):
            continue  # rights already present — never overwrite

        if source not in LICENSE_TEXT:
            no_sidecar_entry = {"slug": slug, "filename": fields.get("Filename", ""), "source": source}
            skipped_unknown_source.append(no_sidecar_entry)
            continue

        candidates.append({
            "slug": slug,
            "filename": fields.get("Filename", ""),
            "source": source,
            "license_text": LICENSE_TEXT[source],
            "metadata": metadata,
        })

    from collections import Counter
    by_source = Counter(c["source"] for c in candidates)
    print(f"Rights gaps to fix: {len(candidates)} ({dict(by_source)})")
    if skipped_unknown_source:
        print(f"Skipped (no free-use license rule for source — needs manual review): {len(skipped_unknown_source)}")
        for item in skipped_unknown_source:
            print(f"  {item['filename']} (source={item['source']!r})")

    print()
    for item in candidates:
        print(f"  {item['filename']}  [{item['source']}] -> {item['license_text']!r}")

    if not args.apply:
        print("\nDry run only — pass --apply to write the license field.")
        return

    if not candidates:
        print("\nNothing to apply.")
        return

    print(f"\nApplying license text to {len(candidates)} record(s)...")
    succeeded = failed = 0
    for index, item in enumerate(candidates, 1):
        try:
            metadata = item["metadata"]
            original = dict(metadata.get("original", {}))
            original["license"] = item["license_text"]
            metadata["original"] = original
            write_metadata(metadata, storage_mode=storage_mode, image_folder=Config.IMAGE_FOLDER, sp_client=sp_client)
            succeeded += 1
            print(f"[{index}/{len(candidates)}] OK {item['filename']}")
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(candidates)}] FAILED {item['filename']}: {exc}")

    print(f"\nDone. {succeeded} succeeded, {failed} failed.")


if __name__ == "__main__":
    main()
