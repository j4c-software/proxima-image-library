#!/usr/bin/env python3
"""One-off script: register orphaned SharePoint files (no List record points to
them) as new pending-review records, so they're visible in the library instead
of sitting invisibly in the document library.

Unlike the raw /api/maintenance/orphans/register-files endpoint (which
registers WebP and High-Res orphans as two independent, incomplete records —
one with only Location set, one with only High-Res Location set), this script
first pairs a WebP orphan with its High-Res orphan by filename stem (stripping
the "-original" suffix convention used by src/image_processor.py) so a single
photo produces one complete record with both fields set. Only truly unpaired
files fall back to a single-field record.

Dry-run by default; pass --apply to actually create records.

Usage:
    .venv/bin/python3 -m scripts.register_orphan_files            # preview only
    .venv/bin/python3 -m scripts.register_orphan_files --apply    # create records
"""

import re
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from src.config import Config
from src.image_processor import slug_from_text
from scripts.library_health_check import collect_orphan_snapshot


def _base_stem(path: str, strip_original: bool) -> str:
    stem = PurePosixPath(path).stem
    if strip_original:
        stem = re.sub(r"-original$", "", stem, flags=re.IGNORECASE)
    return stem.lower()


def main() -> None:
    apply = "--apply" in sys.argv

    if Config.TEST_MODE:
        print("This script targets the live SharePoint catalog (STORAGE_MODE=sharepoint).")
        sys.exit(1)

    from src.sharepoint_client import SharePointClient
    from src.sharepoint_list_client import SharePointListClient

    sp_client = SharePointClient()
    client = SharePointListClient()
    records = client.get_all_records()

    orphans = collect_orphan_snapshot(records, sp_client)
    webp_orphans = orphans["orphaned_webp_files"]
    highres_orphans = orphans["orphaned_high_res_files"]
    print(f"Orphaned WebP files: {len(webp_orphans)} | Orphaned High-Res files: {len(highres_orphans)}\n")

    webp_by_stem = {_base_stem(p, strip_original=False): p for p in webp_orphans}
    highres_by_stem = {_base_stem(p, strip_original=True): p for p in highres_orphans}

    paired_stems = sorted(set(webp_by_stem) & set(highres_by_stem))
    webp_only_stems = sorted(set(webp_by_stem) - set(highres_by_stem))
    highres_only_stems = sorted(set(highres_by_stem) - set(webp_by_stem))

    existing_filenames = {
        str(r.get("fields", {}).get("Filename", "") or "").strip()
        for r in records
    }

    plans = []

    for stem in paired_stems:
        webp_path = webp_by_stem[stem]
        highres_path = highres_by_stem[stem]
        filename = PurePosixPath(webp_path).name
        plans.append({
            "kind": "paired",
            "filename": filename,
            "location": webp_path,
            "high_res_location": highres_path,
            "slug": slug_from_text(stem),
        })

    for stem in webp_only_stems:
        webp_path = webp_by_stem[stem]
        filename = PurePosixPath(webp_path).name
        plans.append({
            "kind": "webp-only",
            "filename": filename,
            "location": webp_path,
            "high_res_location": "",
            "slug": slug_from_text(stem),
        })

    for stem in highres_only_stems:
        highres_path = highres_by_stem[stem]
        filename = PurePosixPath(highres_path).name
        plans.append({
            "kind": "highres-only",
            "filename": filename,
            "location": "",
            "high_res_location": highres_path,
            "slug": slug_from_text(stem),
        })

    print(f"Paired (one complete record each): {len(paired_stems)}")
    print(f"WebP-only (no matching High-Res orphan): {len(webp_only_stems)}")
    print(f"High-Res-only (no matching WebP orphan): {len(highres_only_stems)}")
    print()

    to_create = []
    for plan in plans:
        if plan["filename"] in existing_filenames:
            print(f"  SKIP [{plan['kind']}] {plan['filename']}: a record with this Filename already exists")
            continue
        print(f"  [{plan['kind']}] {plan['filename']} -> Location={plan['location']!r} HighResLocation={plan['high_res_location']!r} Slug={plan['slug']!r}")
        to_create.append(plan)

    print(f"\nRecords to create: {len(to_create)}")
    if not apply:
        print("Dry run only — pass --apply to create these records.")
        return

    created = failed = 0
    for plan in to_create:
        result = client.create_record(
            filename=plan["filename"],
            location=plan["location"],
            high_res_location=plan["high_res_location"],
            slug=plan["slug"],
            status="pending-review",
            source="Internal",
        )
        if result:
            created += 1
            print(f"  OK {plan['filename']} (id={result.get('id')})")
        else:
            failed += 1
            print(f"  FAILED {plan['filename']}")

    print(f"\nDone. {created} created, {failed} failed.")


if __name__ == "__main__":
    main()
