"""One-off script: strip AI reasoning text ('Wait, let me...') that leaked into the
Tags field on existing records (caused by src/ai_generator.py's old generate_tags()
having no output parsing — fixed 2026-08-27).

Dry-run by default; pass --apply to actually patch records.

Usage:
    .venv/bin/python3 -m scripts.cleanup_tag_reasoning            # preview only
    .venv/bin/python3 -m scripts.cleanup_tag_reasoning --apply    # apply patches
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from src.config import Config


def clean_tags_field(raw: str) -> str:
    """Return a clean comma-separated tag string, dropping any leaked reasoning text."""
    raw = raw.strip()
    if "\n" not in raw:
        return raw  # already clean

    blocks = [b.strip() for b in raw.split("\n\n") if b.strip()]
    last_block = blocks[-1] if blocks else raw
    last_parts = [p.strip() for p in last_block.split(",") if p.strip()]

    def is_clean(part: str) -> bool:
        return (
            "\n" not in part
            and ":" not in part
            and not part.lower().startswith("wait")
            and len(part) <= 40
        )

    if last_parts and all(is_clean(p) for p in last_parts):
        return ", ".join(last_parts)

    # Fallback: scan every block/segment and keep only clean-looking tags, deduped.
    seen = set()
    result = []
    for block in blocks:
        for part in block.split(","):
            part = part.strip()
            if not part or not is_clean(part):
                continue
            key = part.lower()
            if key not in seen:
                seen.add(key)
                result.append(part)
    return ", ".join(result)


def main() -> None:
    apply = "--apply" in sys.argv

    if Config.TEST_MODE:
        from src.local_client import LocalClient
        client = LocalClient()
    else:
        from src.sharepoint_list_client import SharePointListClient
        client = SharePointListClient()

    records = client.get_all_records()
    patches = []

    for rec in records:
        fields = rec.get("fields", {})
        rec_id = str(rec.get("id", "")).strip()
        raw_tags = fields.get("Tags", "") or ""
        if "\n" not in raw_tags:
            continue

        cleaned = clean_tags_field(raw_tags)
        filename = fields.get("Filename", "") or "unknown"
        print(f"[{rec_id}] {filename}")
        print(f"  BEFORE: {raw_tags!r}")
        print(f"  AFTER:  {cleaned!r}")
        print("-" * 60)
        if cleaned != raw_tags:
            patches.append((rec_id, {"Tags": cleaned}))

    print(f"\nRecords needing cleanup: {len(patches)}")
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
