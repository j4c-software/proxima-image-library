#!/usr/bin/env python3
"""Read-only health check across the maintenance surface. Never writes anything.

Mirrors the diagnostic /api/maintenance/* routes (health-snapshot,
integrity-scorecard, aging-drift, category-normalization/preview, duplicates,
orphans, broken-thumbnails) via the same underlying logic in
src/maintenance_helpers.py and src/app.py, but runs standalone so it can scan
the whole catalog without a browser/admin session.

Deliberately does NOT import src.app — that module starts the background
SharePoint ingest poller as an import-time side effect (STORAGE_MODE=sharepoint
+ SHAREPOINT_INGEST_FOLDER set), which a diagnostic script must not trigger.
Orphan/broken-thumbnail scanning logic is reimplemented here to match
src/app.py's _collect_orphan_snapshot / _check_location_health.

Usage:
    .venv/bin/python3 -m scripts.library_health_check                    # everything
    .venv/bin/python3 -m scripts.library_health_check --skip-thumbnails  # skip the
        broken-thumbnail scan (downloads + decodes every image; slow)
"""

import sys
from io import BytesIO
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from PIL import Image as PILImage

from src.config import Config
from src.maintenance_helpers import (
    build_integrity_scorecard,
    collect_drift_candidates,
    build_category_normalization_preview,
    collect_duplicate_snapshot,
    sanitize_relative_path,
)


def check_location_health(location: str, sp_client) -> tuple[str, str]:
    """Reimplements src/app.py:_check_location_health for storage_mode='sharepoint'."""
    rel = sanitize_relative_path(location)
    if not rel:
        return "invalid", "Location is empty or invalid"

    root = (Config.SHAREPOINT_IMAGE_FOLDER or "").strip("/")
    candidates = [
        f"WebP/{rel}" if not root else f"{root}/WebP/{rel}",
        f"{rel}" if not root else f"{root}/{rel}",
    ]
    for sp_path in candidates:
        try:
            blob = sp_client.get_file_bytes(sp_path)
        except Exception as exc:
            if "404" in str(exc):
                continue
            return "error", f"SharePoint read failed: {exc}"
        try:
            with PILImage.open(BytesIO(blob)) as img:
                img.verify()
            return "ok", ""
        except Exception as exc:
            return "corrupt", f"Image decode failed: {exc}"
    return "missing", "No matching WebP/legacy file found in SharePoint"


def collect_orphan_snapshot(records: list, sp_client) -> dict:
    root = Config.SHAREPOINT_IMAGE_FOLDER
    webp_paths = {str(PurePosixPath(rel)) for _, rel in sp_client.list_all_images(f"{root}/WebP")}
    high_res_paths = {str(PurePosixPath(rel)) for _, rel in sp_client.list_all_images(f"{root}/High-Res")}

    referenced_webp: set[str] = set()
    referenced_high_res: set[str] = set()
    missing_file_records = []

    for rec in records:
        fields = rec.get("fields", {})
        loc = sanitize_relative_path(fields.get("Location", ""))
        high_res_loc = sanitize_relative_path(fields.get("High-Res Location", ""))
        if loc:
            referenced_webp.add(loc)
        if high_res_loc:
            referenced_high_res.add(high_res_loc)

        missing_webp = bool(loc) and loc not in webp_paths
        missing_high_res = bool(high_res_loc) and high_res_loc not in high_res_paths
        missing_both = not loc and not high_res_loc
        if missing_webp or missing_high_res or missing_both:
            missing_file_records.append({
                "id": str(rec.get("id", "")).strip(),
                "filename": fields.get("Filename", ""),
                "status": fields.get("Status", ""),
                "missing_webp": missing_webp or missing_both,
                "missing_high_res": missing_high_res or missing_both,
            })

    return {
        "webp_file_count": len(webp_paths),
        "high_res_file_count": len(high_res_paths),
        "missing_file_records": sorted(missing_file_records, key=lambda r: r["filename"].lower()),
        "orphaned_webp_files": sorted(p for p in webp_paths if p not in referenced_webp),
        "orphaned_high_res_files": sorted(p for p in high_res_paths if p not in referenced_high_res),
    }


def main() -> None:
    skip_thumbnails = "--skip-thumbnails" in sys.argv

    if Config.TEST_MODE:
        print("This script is designed for the live SharePoint catalog (STORAGE_MODE=sharepoint).")
        print(f"Current TEST_MODE={Config.TEST_MODE!r} — aborting to avoid a misleading report.")
        sys.exit(1)

    from src.sharepoint_client import SharePointClient
    from src.sharepoint_list_client import SharePointListClient

    sp_client = SharePointClient()
    records = SharePointListClient().get_all_records()
    print(f"Loaded {len(records)} records.\n")

    # --- Health snapshot ----------------------------------------------------
    print("=" * 70)
    print("HEALTH SNAPSHOT")
    print("=" * 70)
    statuses = ("pending-review", "approved", "rejected", "archived")
    counts = {s: sum(1 for r in records if str(r.get("fields", {}).get("Status", "")).strip() == s) for s in statuses}
    other = len(records) - sum(counts.values())
    for s in statuses:
        print(f"  {s}: {counts[s]}")
    if other:
        print(f"  (other/unknown status): {other}")

    # --- Integrity scorecard -------------------------------------------------
    print("\n" + "=" * 70)
    print("INTEGRITY SCORECARD (by category)")
    print("=" * 70)
    scorecard = build_integrity_scorecard(records)
    for row in scorecard["categories"]:
        print(
            f"  {row['category']:<14} score={row['integrity_score']:>5.1f}  total={row['total']:<4} "
            f"missing: alt={row['missing_alt']} tags={row['missing_tags']} slug={row['missing_slug']} "
            f"location={row['missing_location']} highres={row['missing_high_res_location']} source={row['missing_source']}"
        )
    if scorecard["unknown_status_count"]:
        print(f"  Records with unrecognized Status value: {scorecard['unknown_status_count']}")

    # --- Aging / drift ---------------------------------------------------------
    print("\n" + "=" * 70)
    print("AGING / DRIFT SCAN")
    print("=" * 70)
    drift = collect_drift_candidates(records, limit=2000)
    print(f"  Candidates: {drift['candidate_count']} of {drift['record_count']}")
    for reason, count in sorted(drift["reason_counts"].items(), key=lambda kv: -kv[1]):
        print(f"    {reason}: {count}")

    # --- Category normalization -------------------------------------------------
    print("\n" + "=" * 70)
    print("CATEGORY NORMALIZATION PREVIEW")
    print("=" * 70)
    cat_norm = build_category_normalization_preview(records, limit=2000)
    print(f"  Records with a non-canonical top-level category folder: {cat_norm['candidate_count']}")
    for c in cat_norm["candidates"][:20]:
        print(f"    {c['filename']}: {c['current_category']!r} -> {c['normalized_category']!r}")
    if cat_norm["candidate_count"] > 20:
        print(f"    ... and {cat_norm['candidate_count'] - 20} more")

    # --- Duplicates --------------------------------------------------------
    print("\n" + "=" * 70)
    print("DUPLICATE SCAN")
    print("=" * 70)
    dupes = collect_duplicate_snapshot(records, limit=2000)
    print(f"  Duplicate groups: {dupes['duplicate_group_count']} "
          f"(filename={dupes['filename_group_count']}, slug={dupes['slug_group_count']}, "
          f"alt_exact={dupes['alt_exact_group_count']}, alt_near={dupes['alt_near_group_count']}, "
          f"image_hash={dupes['image_hash_group_count']}, image_stem={dupes['image_stem_group_count']}, "
          f"alt+tags={dupes['alt_tags_group_count']}, near_alt+tags={dupes['near_alt_tags_group_count']})")
    for g in dupes["groups"][:20]:
        names = ", ".join(r.get("filename", "") for r in g["records"])
        print(f"    [{g['group_type']}] {names}")
    if dupes["duplicate_group_count"] > 20:
        print(f"    ... and {dupes['duplicate_group_count'] - 20} more groups")

    # --- Orphans -------------------------------------------------------------
    print("\n" + "=" * 70)
    print("ORPHAN FILE SCAN")
    print("=" * 70)
    orphans = collect_orphan_snapshot(records, sp_client)
    print(f"  WebP files on disk: {orphans['webp_file_count']} | High-Res files on disk: {orphans['high_res_file_count']}")
    print(f"  Records pointing at a missing file: {len(orphans['missing_file_records'])}")
    for r in orphans["missing_file_records"][:20]:
        flags = []
        if r["missing_webp"]:
            flags.append("missing-webp")
        if r["missing_high_res"]:
            flags.append("missing-high-res")
        print(f"    {r['filename']} [{r['status']}]: {', '.join(flags)}")
    print(f"  Orphaned WebP files (no record references them): {len(orphans['orphaned_webp_files'])}")
    for p in orphans["orphaned_webp_files"][:20]:
        print(f"    {p}")
    print(f"  Orphaned High-Res files (no record references them): {len(orphans['orphaned_high_res_files'])}")
    for p in orphans["orphaned_high_res_files"][:20]:
        print(f"    {p}")

    # --- Broken thumbnails -----------------------------------------------------
    if skip_thumbnails:
        print("\n" + "=" * 70)
        print("BROKEN THUMBNAIL SCAN — skipped (--skip-thumbnails)")
        print("=" * 70)
    else:
        print("\n" + "=" * 70)
        print("BROKEN THUMBNAIL SCAN (downloads + decodes every image — this takes a while)")
        print("=" * 70)
        broken = []
        healthy = 0
        for i, rec in enumerate(records, 1):
            fields = rec.get("fields", {})
            location = str(fields.get("Location", "") or "").strip()
            filename = str(fields.get("Filename", "") or "").strip()
            if not location:
                broken.append((filename, "missing-location", "Record has no Location value"))
                continue
            health, detail = check_location_health(location, sp_client)
            if health == "ok":
                healthy += 1
            else:
                broken.append((filename, health, detail))
            if i % 50 == 0:
                print(f"  ...scanned {i}/{len(records)}")
        print(f"  Healthy: {healthy} | Broken: {len(broken)}")
        for filename, reason, detail in sorted(broken):
            print(f"    {filename}: {reason} — {detail}")

    print("\nDone. This was a read-only scan — nothing was changed.")


if __name__ == "__main__":
    main()
