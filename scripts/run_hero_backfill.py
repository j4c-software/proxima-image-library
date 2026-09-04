#!/usr/bin/env python3
"""One-off script: backfill hero derivatives + metadata sidecars for the whole library.

Wraps src.catalog_inventory.build_inventory (read-only scan) and
src.hero_backfill.backfill_record (selective, non-destructive write — it only
fills gaps, never replaces an existing Hero image or metadata field) so the
same logic behind the /maintenance hero-backfill tool can run over every
record in one pass instead of one admin-selected batch at a time.

Dry-run by default; pass --apply to actually write Hero derivatives and
metadata sidecars. Optionally filter by Status (the /maintenance UI defaults
to "approved" only; omit --status to scan every record regardless of status).

Usage:
    .venv/bin/python3 -m scripts.run_hero_backfill                     # preview only, all statuses
    .venv/bin/python3 -m scripts.run_hero_backfill --status approved   # preview only, approved records
    .venv/bin/python3 -m scripts.run_hero_backfill --apply             # write backfills, all statuses
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from src.config import Config
from src.catalog_inventory import build_inventory
from src.hero_backfill import backfill_record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", default="", help="Only scan records with this Status (default: all statuses)")
    parser.add_argument("--apply", action="store_true", help="Write backfills (default: dry run / report only)")
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
    status_filter = args.status.strip().lower()
    if status_filter:
        records = [
            r for r in records
            if str(r.get("fields", {}).get("Status", "") or "").strip().lower() == status_filter
        ]

    print(f"Scanning {len(records)} record(s)"
          f"{f' with Status={status_filter!r}' if status_filter else ' (all statuses)'}...")

    report = build_inventory(
        records,
        storage_mode=storage_mode,
        image_folder=Config.IMAGE_FOLDER,
        sp_client=sp_client,
    )

    candidates = []
    for key in ("heroEligible", "standardOnly1601To2559", "storedOriginalOnly1600", "below1600"):
        candidates.extend(item for item in report[key] if item.get("backfillNeeded"))
    candidates.sort(key=lambda item: (0 if item.get("width", 0) >= 2560 else 1, item.get("filename", "").lower()))

    summary = report["summary"]
    print(
        f"Records: {summary['records']} | hero-eligible (>=2560px): {summary['heroEligible']} | "
        f"1601-2559px: {summary['standardOnly1601To2559']} | exactly 1600px: {summary['storedOriginalOnly1600']} | "
        f"below 1600px: {summary['below1600']} | missing original: {summary['missingOriginal']} | "
        f"metadata gaps (rights/focal point): {summary['metadataGaps']}"
    )
    print(f"Backfill needed (missing metadata sidecar, or hero-eligible without a Hero image yet): {len(candidates)}\n")

    for item in candidates:
        print(f"  {item['filename']} (width={item['width']}, metadataPresent={item['metadataPresent']}, heroPresent={item['heroPresent']})")

    if report["missingOriginal"]:
        print(f"\n{len(report['missingOriginal'])} record(s) have an unreadable/missing High-Res original — skipped, not counted above:")
        for item in report["missingOriginal"]:
            print(f"  {item['filename']}: {item['reason']}")

    if not args.apply:
        print("\nDry run only — pass --apply to write Hero derivatives and metadata sidecars.")
        return

    if not candidates:
        print("\nNothing to backfill.")
        return

    record_map = {str(r.get("id", "")).strip(): r for r in records}
    print(f"\nApplying backfill to {len(candidates)} record(s)...")
    succeeded = failed = hero_created = 0
    for index, item in enumerate(candidates, 1):
        record = record_map.get(item["id"])
        filename = item.get("filename") or item["id"]
        if not record:
            print(f"[{index}/{len(candidates)}] SKIP {filename}: record no longer exists")
            failed += 1
            continue
        try:
            result = backfill_record(
                record,
                storage_mode=storage_mode,
                image_folder=Config.IMAGE_FOLDER,
                sp_client=sp_client,
            )
            succeeded += 1
            hero_created += int(bool(result.get("heroCreated")))
            print(f"[{index}/{len(candidates)}] OK {filename} (heroCreated={result['heroCreated']}, metadataWritten={result['metadataWritten']})")
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(candidates)}] FAILED {filename}: {exc}")

    print(f"\nDone. {succeeded} succeeded, {failed} failed, {hero_created} hero derivative(s) created.")


if __name__ == "__main__":
    main()
