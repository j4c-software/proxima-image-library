"""Pure computation helpers for the maintenance subsystem.

All functions here are stateless with respect to Flask — they take data as
parameters and return data. No imports from app.py (no circular dependency).
Route handlers in app.py call these helpers and handle Flask I/O themselves.
"""

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAINTENANCE_PURGE_STATUSES = {"rejected", "archived", "ingested"}

MAINTENANCE_CANONICAL_CATEGORIES = [
    "Headshots",
    "Community",
    "Locations",
    "Situations",
    "Graphics",
    "Banners",
]

MAINTENANCE_CATEGORY_ALIASES = {
    "headshot": "Headshots",
    "headshots": "Headshots",
    "community": "Community",
    "communities": "Community",
    "location": "Locations",
    "locations": "Locations",
    "situation": "Situations",
    "situations": "Situations",
    "graphic": "Graphics",
    "graphics": "Graphics",
    "banner": "Banners",
    "banners": "Banners",
}

MAINTENANCE_STATE_LOCK = threading.Lock()
MAINTENANCE_STATE_PATH = Path(
    "/home/proxima_maintenance_state.json"
    if Path("/home").exists() and os.getenv("WEBSITE_INSTANCE_ID")
    else "maintenance_state.json"
)
MAINTENANCE_CHECKPOINT_DIR = Path(
    "/home/proxima_maintenance_checkpoints"
    if Path("/home").exists() and os.getenv("WEBSITE_INSTANCE_ID")
    else "test_data/maintenance_checkpoints"
)


# ---------------------------------------------------------------------------
# General utilities
# ---------------------------------------------------------------------------

def now_utc_iso() -> str:
    return f"{datetime.utcnow().replace(microsecond=0).isoformat()}Z"


def atomic_json_write(path: Path, payload) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp_path.replace(path)


def sanitize_relative_path(relative_path: str) -> str:
    rel = str(relative_path or "").strip()
    if not rel:
        return ""
    rel_posix = PurePosixPath(rel)
    if rel_posix.is_absolute() or ".." in rel_posix.parts:
        return ""
    return str(rel_posix)


def normalize_filter_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def normalize_compare_text(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def parse_iso_date(raw: str) -> Optional[date]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def record_date_value(rec: Dict) -> Optional[date]:
    fields = rec.get("fields", {})
    for key in ["Date", "Created", "Created At", "Updated", "Updated At", "Modified", "Modified At"]:
        parsed = parse_iso_date(fields.get(key, ""))
        if parsed is not None:
            return parsed
    return None


def category_from_location(location: str) -> str:
    rel = sanitize_relative_path(location)
    if not rel:
        return ""
    parts = list(PurePosixPath(rel).parts)
    return parts[0] if parts else ""


def canonical_category_name(raw: str) -> str:
    key = normalize_filter_key(raw)
    return MAINTENANCE_CATEGORY_ALIASES.get(key, "") if key else ""


def record_for_duplicate_scan(rec: Dict) -> Dict:
    fields = rec.get("fields", {})
    return {
        "id": str(rec.get("id", "")).strip(),
        "filename": str(fields.get("Filename", "")).strip(),
        "slug": str(fields.get("Slug", "")).strip(),
        "status": str(fields.get("Status", "")).strip(),
        "alt_text": str(fields.get("Alt Text", "")).strip(),
        "tags": str(fields.get("Tags", "")).strip(),
        "location": str(fields.get("Location", "")).strip(),
        "high_res_location": str(fields.get("High-Res Location", "")).strip(),
        "source": str(fields.get("Source", "")).strip(),
        "image_hash": str(fields.get("Image Hash", "")).strip().lower(),
    }


# ---------------------------------------------------------------------------
# Maintenance state management
# ---------------------------------------------------------------------------

def maintenance_default_guardrails() -> Dict:
    return {
        "max_batch_size": 500,
        "require_preview_for_destructive": True,
        "two_step_approval_required": False,
        "checkpoint_before_destructive": False,
    }


def maintenance_default_state() -> Dict:
    return {
        "guardrails": maintenance_default_guardrails(),
        "jobs": {
            "enabled": False,
            "interval_minutes": 1440,
            "job_names": ["health_snapshot", "integrity_scorecard", "aging_drift_scan"],
            "last_runs": {},
        },
        "audit_trail": [],
        "checkpoints": [],
        "approvals": [],
    }


def maintenance_load_state() -> Dict:
    state = maintenance_default_state()
    try:
        if MAINTENANCE_STATE_PATH.exists():
            loaded = json.loads(MAINTENANCE_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state.update(loaded)
    except Exception:
        pass

    guardrails = maintenance_default_guardrails()
    guardrails.update(state.get("guardrails", {}))
    state["guardrails"] = guardrails

    jobs = {
        "enabled": False,
        "interval_minutes": 1440,
        "job_names": ["health_snapshot", "integrity_scorecard", "aging_drift_scan"],
        "last_runs": {},
    }
    jobs.update(state.get("jobs", {}))
    if not isinstance(jobs.get("last_runs", {}), dict):
        jobs["last_runs"] = {}
    if not isinstance(jobs.get("job_names", []), list):
        jobs["job_names"] = ["health_snapshot", "integrity_scorecard", "aging_drift_scan"]
    state["jobs"] = jobs

    if not isinstance(state.get("audit_trail", []), list):
        state["audit_trail"] = []
    if not isinstance(state.get("checkpoints", []), list):
        state["checkpoints"] = []
    if not isinstance(state.get("approvals", []), list):
        state["approvals"] = []
    return state


def maintenance_save_state(state: Dict) -> None:
    atomic_json_write(MAINTENANCE_STATE_PATH, state)


def maintenance_record_hash(record_ids: List[str]) -> str:
    stable = "\n".join(sorted(str(rid).strip() for rid in record_ids if str(rid).strip()))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def maintenance_guardrails() -> Dict:
    with MAINTENANCE_STATE_LOCK:
        state = maintenance_load_state()
        guardrails = maintenance_default_guardrails()
        guardrails.update(state.get("guardrails", {}))
        return guardrails


# ---------------------------------------------------------------------------
# Analysis / computation
# ---------------------------------------------------------------------------

def build_integrity_scorecard(records: List[Dict]) -> Dict:
    categories: Dict[str, Dict] = {}
    unknown_status_count = 0
    valid_statuses = {"pending-review", "approved", "rejected", "archived"}

    for rec in records:
        fields = rec.get("fields", {})
        category = category_from_location(fields.get("Location", "")) or "Uncategorized"
        bucket = categories.setdefault(category, {
            "category": category,
            "total": 0,
            "missing_alt": 0,
            "missing_tags": 0,
            "missing_slug": 0,
            "missing_location": 0,
            "missing_high_res_location": 0,
            "missing_source": 0,
            "statuses": {"pending-review": 0, "approved": 0, "rejected": 0, "archived": 0},
        })
        bucket["total"] += 1

        alt_text = str(fields.get("Alt Text", "") or "").strip()
        tags = [t.strip() for t in str(fields.get("Tags", "") or "").split(",") if t.strip()]
        slug = str(fields.get("Slug", "") or "").strip()
        location = str(fields.get("Location", "") or "").strip()
        high_res_location = str(fields.get("High-Res Location", "") or "").strip()
        source = str(fields.get("Source", "") or "").strip()

        if not alt_text:
            bucket["missing_alt"] += 1
        if not tags:
            bucket["missing_tags"] += 1
        if not slug:
            bucket["missing_slug"] += 1
        if not location:
            bucket["missing_location"] += 1
        if not high_res_location:
            bucket["missing_high_res_location"] += 1
        if not source:
            bucket["missing_source"] += 1

        status = str(fields.get("Status", "") or "").strip()
        if status in bucket["statuses"]:
            bucket["statuses"][status] += 1
        if status not in valid_statuses:
            unknown_status_count += 1

    rows = []
    for row in categories.values():
        total = max(1, row["total"])
        missing_total = (
            row["missing_alt"] + row["missing_tags"] + row["missing_slug"]
            + row["missing_location"] + row["missing_high_res_location"] + row["missing_source"]
        )
        row["integrity_score"] = round(max(0.0, 100.0 - ((missing_total / (total * 6)) * 100.0)), 1)
        rows.append(row)

    rows.sort(key=lambda r: (r["integrity_score"], r["category"].lower()))

    return {
        "generated_at": now_utc_iso(),
        "record_count": len(records),
        "unknown_status_count": unknown_status_count,
        "categories": rows,
    }


def collect_drift_candidates(
    records: List[Dict],
    stale_pending_days: int = 14,
    stale_approved_days: int = 180,
    min_alt_chars: int = 40,
    min_tag_count: int = 2,
    limit: int = 200,
) -> Dict:
    today = date.today()
    candidates = []
    reason_counts: Dict[str, int] = {}

    for rec in records:
        fields = rec.get("fields", {})
        rec_id = str(rec.get("id", "") or "").strip()
        if not rec_id:
            continue

        filename = str(fields.get("Filename", "") or "").strip()
        status = str(fields.get("Status", "") or "").strip()
        alt_text = str(fields.get("Alt Text", "") or "").strip()
        tags = [t.strip() for t in str(fields.get("Tags", "") or "").split(",") if t.strip()]
        slug = str(fields.get("Slug", "") or "").strip()
        source = str(fields.get("Source", "") or "").strip()
        location = str(fields.get("Location", "") or "").strip()

        reasons = []
        rec_date = record_date_value(rec)
        days_old = (today - rec_date).days if rec_date is not None else None

        if status == "pending-review" and days_old is not None and days_old >= stale_pending_days:
            reasons.append("pending-review-stale")
        if status == "approved" and days_old is not None and days_old >= stale_approved_days:
            reasons.append("approved-stale")
        if len(alt_text) < max(1, min_alt_chars):
            reasons.append("short-alt")
        if len(tags) < max(0, min_tag_count):
            reasons.append("sparse-tags")
        if any(t.lower() == "?missing-file" for t in tags):
            reasons.append("missing-file-marker")
        if not slug:
            reasons.append("missing-slug")
        if not source:
            reasons.append("missing-source")
        if not location:
            reasons.append("missing-location")

        if not reasons:
            continue

        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

        candidates.append({
            "id": rec_id,
            "filename": filename,
            "status": status,
            "location": location,
            "category": category_from_location(location),
            "date": rec_date.isoformat() if rec_date is not None else "",
            "days_old": days_old,
            "reasons": reasons,
        })

    candidates.sort(key=lambda c: (
        -len(c.get("reasons", [])),
        -(c.get("days_old") or 0),
        str(c.get("filename", "")).lower(),
    ))

    limit = max(10, min(int(limit), 2000))
    return {
        "generated_at": now_utc_iso(),
        "record_count": len(records),
        "candidate_count": len(candidates),
        "reason_counts": reason_counts,
        "display_limit": limit,
        "truncated": len(candidates) > limit,
        "candidates": candidates[:limit],
    }


def build_category_normalization_preview(records: List[Dict], limit: int = 200) -> Dict:
    candidates = []
    for rec in records:
        fields = rec.get("fields", {})
        rec_id = str(rec.get("id", "") or "").strip()
        location = sanitize_relative_path(fields.get("Location", ""))
        if not rec_id or not location:
            continue

        parts = list(PurePosixPath(location).parts)
        if not parts:
            continue
        current_category = parts[0]
        canonical = canonical_category_name(current_category)
        if not canonical or current_category == canonical:
            continue

        proposed_location = str(PurePosixPath(canonical, *parts[1:]))
        candidates.append({
            "id": rec_id,
            "filename": str(fields.get("Filename", "") or "").strip(),
            "current_category": current_category,
            "normalized_category": canonical,
            "current_location": location,
            "proposed_location": proposed_location,
            "status": str(fields.get("Status", "") or "").strip(),
        })

    candidates.sort(key=lambda c: (str(c.get("current_category", "")).lower(), str(c.get("filename", "")).lower()))
    limit = max(10, min(int(limit), 2000))
    return {
        "generated_at": now_utc_iso(),
        "candidate_count": len(candidates),
        "display_limit": limit,
        "truncated": len(candidates) > limit,
        "candidates": candidates[:limit],
    }


def run_named_maintenance_job(job_name: str, records: List[Dict]) -> Dict:
    if job_name == "health_snapshot":
        health = {
            "record_count": len(records),
            "status_counts": {
                s: sum(1 for r in records if str(r.get("fields", {}).get("Status", "")).strip() == s)
                for s in ("pending-review", "approved", "rejected", "archived")
            },
        }
        return {"job": job_name, "summary": health}

    if job_name == "integrity_scorecard":
        scorecard = build_integrity_scorecard(records)
        return {
            "job": job_name,
            "summary": {
                "record_count": scorecard.get("record_count", 0),
                "category_count": len(scorecard.get("categories", [])),
                "lowest_score": (scorecard.get("categories", [{}])[0] or {}).get("integrity_score", 100.0)
                if scorecard.get("categories") else 100.0,
            },
        }

    if job_name == "aging_drift_scan":
        drift = collect_drift_candidates(records, limit=200)
        return {
            "job": job_name,
            "summary": {
                "candidate_count": drift.get("candidate_count", 0),
                "reason_counts": drift.get("reason_counts", {}),
            },
        }

    raise ValueError(f"Unknown job_name: {job_name}")


def build_exact_duplicate_groups(scanned_records: List[Dict], field_name: str, group_type: str) -> List[Dict]:
    buckets: Dict[str, Dict] = {}
    for rec in scanned_records:
        value = str(rec.get(field_name, "")).strip()
        if not value:
            continue
        key = value.lower()
        if key not in buckets:
            buckets[key] = {"display": value, "records": []}
        buckets[key]["records"].append(rec)

    groups = []
    for bucket in buckets.values():
        if len(bucket["records"]) < 2:
            continue
        groups.append({
            "group_type": group_type,
            "key": bucket["display"],
            "records": sorted(
                bucket["records"],
                key=lambda r: ((r.get("filename") or "").lower(), r.get("id") or ""),
            ),
        })

    groups.sort(key=lambda g: (str(g.get("key", "")).lower(), len(g.get("records", [])) * -1))
    return groups


def build_near_alt_duplicate_groups(scanned_records: List[Dict], threshold: float, window_size: int = 150) -> List[Dict]:
    entries = []
    for rec in scanned_records:
        alt_norm = normalize_compare_text(rec.get("alt_text", ""))
        if alt_norm:
            entries.append((rec.get("id", ""), alt_norm))

    entries = [(rid, alt) for rid, alt in entries if rid]
    if len(entries) < 2:
        return []

    entries.sort(key=lambda item: item[1])
    window_size = max(10, min(int(window_size), 200))

    parent = {rid: rid for rid, _ in entries}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Bounded by length (not by leading character) so differently-worded
    # AI-generated alt text for the same image can still be compared.
    n = len(entries)
    for i in range(n):
        ida, alta = entries[i]
        for j in range(i + 1, min(n, i + 1 + window_size)):
            idb, altb = entries[j]
            if alta == altb or abs(len(alta) - len(altb)) > 40:
                continue
            if SequenceMatcher(None, alta, altb).ratio() >= threshold:
                union(ida, idb)

    scanned_by_id = {rec.get("id", ""): rec for rec in scanned_records if rec.get("id", "")}
    clusters: Dict[str, List[str]] = {}
    for rid, _ in entries:
        clusters.setdefault(find(rid), []).append(rid)

    groups = []
    for cluster_ids in clusters.values():
        unique_ids = sorted(set(cluster_ids))
        if len(unique_ids) < 2:
            continue
        members = [scanned_by_id[rid] for rid in unique_ids if rid in scanned_by_id]
        if len(members) < 2:
            continue
        members.sort(key=lambda r: ((r.get("filename") or "").lower(), r.get("id") or ""))
        groups.append({
            "group_type": "alt_near",
            "key": f"similarity >= {threshold:.2f}",
            "records": members,
        })

    groups.sort(key=lambda g: ((g.get("records", [{}])[0].get("filename") or "").lower(), len(g.get("records", [])) * -1))
    return groups


def _hamming_distance_hex(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return max(len(a), len(b)) * 4
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def build_image_hash_duplicate_groups(scanned_records: List[Dict], max_distance: int = 6) -> List[Dict]:
    """Group records whose image content hash matches or nearly matches.

    Unlike filename/slug/alt-text, the hash is derived from pixel data, so it
    still catches re-ingested duplicate photos even when the AI regenerates
    different alt text (and therefore a different slug/filename) each time.
    """
    entries = [(rec.get("id", ""), str(rec.get("image_hash", "")).strip().lower()) for rec in scanned_records]
    entries = [(rid, h) for rid, h in entries if rid and h]
    if len(entries) < 2:
        return []

    parent = {rid: rid for rid, _ in entries}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    n = len(entries)
    for i in range(n):
        ida, ha = entries[i]
        for j in range(i + 1, n):
            idb, hb = entries[j]
            if _hamming_distance_hex(ha, hb) <= max_distance:
                union(ida, idb)

    scanned_by_id = {rec.get("id", ""): rec for rec in scanned_records if rec.get("id", "")}
    clusters: Dict[str, List[str]] = {}
    for rid, _ in entries:
        clusters.setdefault(find(rid), []).append(rid)

    groups = []
    for cluster_ids in clusters.values():
        unique_ids = sorted(set(cluster_ids))
        if len(unique_ids) < 2:
            continue
        members = [scanned_by_id[rid] for rid in unique_ids if rid in scanned_by_id]
        if len(members) < 2:
            continue
        members.sort(key=lambda r: ((r.get("filename") or "").lower(), r.get("id") or ""))
        groups.append({
            "group_type": "image_hash",
            "key": f"hash~{members[0].get('image_hash', '')}",
            "records": members,
        })

    groups.sort(key=lambda g: ((g.get("records", [{}])[0].get("filename") or "").lower(), len(g.get("records", [])) * -1))
    return groups


def collect_duplicate_snapshot(
    records: List[Dict],
    limit: int = 200,
    include_near_alt: bool = True,
    near_threshold: float = 0.92,
) -> Dict:
    limit = max(10, min(limit, 1000))
    near_threshold = max(0.80, min(near_threshold, 0.99))
    scanned_records = [record_for_duplicate_scan(r) for r in records]

    filename_groups = build_exact_duplicate_groups(scanned_records, "filename", "filename")
    slug_groups = build_exact_duplicate_groups(scanned_records, "slug", "slug")

    alt_exact_candidates = []
    for rec in scanned_records:
        alt_norm = normalize_compare_text(rec.get("alt_text", ""))
        if alt_norm:
            clone = dict(rec)
            clone["_alt_norm"] = alt_norm
            alt_exact_candidates.append(clone)
    alt_exact_groups = build_exact_duplicate_groups(alt_exact_candidates, "_alt_norm", "alt_exact")
    for group in alt_exact_groups:
        for rec in group.get("records", []):
            rec.pop("_alt_norm", None)

    near_alt_groups = build_near_alt_duplicate_groups(scanned_records, near_threshold) if include_near_alt else []
    image_hash_groups = build_image_hash_duplicate_groups(scanned_records)

    image_stem_candidates = []
    for rec in scanned_records:
        slug = re.sub(r"\.[^.]+$", "", str(rec.get("slug") or "").strip())
        if not slug:
            fn_stem = re.sub(r"\.[^.]+$", "", str(rec.get("filename") or "").strip())
            slug = re.sub(r"-original$", "", fn_stem)
        if not slug:
            hr = str(rec.get("high_res_location") or "").strip()
            if hr:
                hr_stem = re.sub(r"\.[^.]+$", "", PurePosixPath(hr).name)
                slug = re.sub(r"-original$", "", hr_stem)
        if slug:
            clone = dict(rec)
            clone["_image_stem"] = slug.lower()
            image_stem_candidates.append(clone)
    image_stem_groups = build_exact_duplicate_groups(image_stem_candidates, "_image_stem", "image_stem")
    for group in image_stem_groups:
        for rec in group.get("records", []):
            rec.pop("_image_stem", None)

    covered_ids: set = set()
    for g in filename_groups + slug_groups + image_hash_groups:
        for rec in g.get("records", []):
            covered_ids.add(rec.get("id", ""))
    image_stem_groups = [
        g for g in image_stem_groups
        if not all(rec.get("id", "") in covered_ids for rec in g.get("records", []))
    ]

    alt_tags_buckets: Dict[str, Dict] = {}
    for rec in scanned_records:
        alt_norm = normalize_compare_text(rec.get("alt_text", ""))
        tags_norm = normalize_compare_text(rec.get("tags", ""))
        if not alt_norm or not tags_norm:
            continue
        key = f"{alt_norm}||{tags_norm}"
        if key not in alt_tags_buckets:
            alt_tags_buckets[key] = {"display": key[:80], "records": []}
        alt_tags_buckets[key]["records"].append(rec)
    alt_tags_groups = [
        {"group_type": "alt_and_tags", "key": b["display"], "records": sorted(
            b["records"], key=lambda r: ((r.get("filename") or "").lower(), r.get("id") or "")
        )}
        for b in alt_tags_buckets.values() if len(b["records"]) >= 2
    ]

    def _sorted_tags(tags_str: str) -> str:
        return ",".join(sorted(t.strip().lower() for t in tags_str.split(",") if t.strip()))

    _near_alt_tags_candidates = [
        rec for rec in scanned_records
        if normalize_compare_text(rec.get("alt_text", "")) and rec.get("tags", "").strip()
    ]
    near_alt_tags_groups: list = []
    _paired_near: set = set()
    for i, ra in enumerate(_near_alt_tags_candidates):
        for rb in _near_alt_tags_candidates[i + 1:]:
            pair_key = tuple(sorted([ra.get("id", ""), rb.get("id", "")]))
            if pair_key in _paired_near:
                continue
            if _sorted_tags(ra.get("tags", "")) != _sorted_tags(rb.get("tags", "")):
                continue
            alt_a = normalize_compare_text(ra.get("alt_text", ""))
            alt_b = normalize_compare_text(rb.get("alt_text", ""))
            if SequenceMatcher(None, alt_a, alt_b).ratio() >= 0.85:
                _paired_near.add(pair_key)
                near_alt_tags_groups.append({
                    "group_type": "near_alt_and_tags",
                    "key": f"near-alt+tags: {alt_a[:60]}",
                    "records": sorted([ra, rb], key=lambda r: ((r.get("filename") or "").lower(), r.get("id") or "")),
                })

    all_covered_ids: set = set()
    for g in filename_groups + slug_groups + alt_exact_groups + image_stem_groups + image_hash_groups:
        for rec in g.get("records", []):
            all_covered_ids.add(rec.get("id", ""))
    alt_tags_groups = [
        g for g in alt_tags_groups
        if not all(rec.get("id", "") in all_covered_ids for rec in g.get("records", []))
    ]
    near_alt_tags_groups = [
        g for g in near_alt_tags_groups
        if not all(rec.get("id", "") in all_covered_ids for rec in g.get("records", []))
    ]

    all_groups = (filename_groups + slug_groups + alt_exact_groups + near_alt_groups
                  + image_stem_groups + image_hash_groups + alt_tags_groups + near_alt_tags_groups)
    for idx, group in enumerate(all_groups, 1):
        group["group_id"] = f"dup-{idx}"
        group["count"] = len(group.get("records", []))

    return {
        "record_count": len(scanned_records),
        "duplicate_group_count": len(all_groups),
        "filename_group_count": len(filename_groups),
        "slug_group_count": len(slug_groups),
        "alt_exact_group_count": len(alt_exact_groups),
        "alt_near_group_count": len(near_alt_groups),
        "image_stem_group_count": len(image_stem_groups),
        "image_hash_group_count": len(image_hash_groups),
        "alt_tags_group_count": len(alt_tags_groups),
        "near_alt_tags_group_count": len(near_alt_tags_groups),
        "include_near_alt": include_near_alt,
        "near_threshold": near_threshold,
        "groups": all_groups[:limit],
        "display_limit": limit,
        "truncated": len(all_groups) > limit,
    }
