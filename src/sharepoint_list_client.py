"""SharePoint List client using Microsoft Graph API."""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import requests

from src.sharepoint_client import SharePointClient


class SharePointListClient(SharePointClient):
    """Stores image metadata in a SharePoint List via Microsoft Graph API.

    Implements the metadata-store interface used across the app.

    Expected SharePoint List columns (internal names):
        Title           — Filename  (built-in required field, repurposed)
        AltText         — Alt Text
        Tags            — Tags
        Status          — Status
        Slug            — Slug
        Location        — Location  (WebP path, e.g. Headshots/proxima-mike7.webp)
        HighResLocation — High-Res Location
        Source          — Source folder / provenance
    """

    # App field name → SharePoint internal column name
    _TO_SP = {
        "Filename": "Title",
        "Alt Text": "AltText",
        "Tags": "Tags",
        "Status": "Status",
        "Slug": "Slug",
        "Location": "Location",
        "High-Res Location": "HighResLocation",
        "Source": "Source",
        "Ingest Source": "IngestSource",
        "Image Hash": "ImageHash",
    }

    # SharePoint internal column name → app field name
    _TO_APP = {v: k for k, v in _TO_SP.items()}

    def __init__(self):
        super().__init__()
        self.site_id = os.getenv("SHAREPOINT_SITE_ID", "")
        self.list_name = os.getenv("SHAREPOINT_LIST_NAME", "Assets")

    @property
    def _list_url(self) -> str:
        return f"{self.GRAPH_BASE}/sites/{self.site_id}/lists/{self.list_name}"

    def _to_app_record(self, item: dict) -> dict:
        """Normalise a Graph API list item into the app's {id, fields} format."""
        sp_fields = item.get("fields", {})
        fields = {
            app_key: sp_fields.get(sp_key, "") or ""
            for sp_key, app_key in self._TO_APP.items()
        }
        return {"id": str(item["id"]), "fields": fields}

    # ------------------------------------------------------------------
    # Public interface (matches LocalClient)
    # ------------------------------------------------------------------

    def get_records(self, limit: int = 100) -> List[Dict]:
        url = f"{self._list_url}/items?expand=fields&$top={limit}"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return [self._to_app_record(i) for i in resp.json().get("value", [])]

    def get_all_records(self) -> List[Dict]:
        url = f"{self._list_url}/items?expand=fields&$top=999"
        records = []
        while url:
            resp = requests.get(url, headers=self._headers(), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            records.extend(self._to_app_record(i) for i in data.get("value", []))
            url = data.get("@odata.nextLink")
        return records

    def get_all_record_ids(self) -> List[str]:
        url = f"{self._list_url}/items?$select=id&$top=999"
        ids: List[str] = []
        while url:
            resp = requests.get(url, headers=self._headers(), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            ids.extend(str(i["id"]) for i in data.get("value", []))
            url = data.get("@odata.nextLink")
        return ids

    def record_exists(self, filename: str) -> bool:
        # SharePoint $filter on list fields requires indexed columns.
        # Fetch all item titles and check locally to avoid that requirement.
        url = f"{self._list_url}/items?$expand=fields($select=Title)&$top=999"
        while url:
            resp = requests.get(url, headers=self._headers(), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("value", []):
                if item.get("fields", {}).get("Title", "") == filename:
                    return True
            url = data.get("@odata.nextLink")
        return False

    def create_record(
        self,
        filename: str,
        image_url: Optional[str] = None,
        alt_text: str = "",
        tags: str = "",
        status: str = "pending-review",
        slug: str = "",
        location: str = "",
        high_res_location: str = "",
        source: str = "Internal",
        ingest_source: str = "",
        image_hash: str = "",
    ) -> Optional[Dict]:
        fields = {
            "Title": filename,
            "AltText": alt_text,
            "Tags": tags,
            "Status": status,
            "Slug": slug,
            "Location": location,
            "HighResLocation": high_res_location,
            "Source": source,
        }
        if ingest_source:
            fields["IngestSource"] = ingest_source
        if image_hash:
            fields["ImageHash"] = image_hash
        try:
            resp = requests.post(
                f"{self._list_url}/items",
                headers=self._headers(),
                json={"fields": fields},
                timeout=15,
            )
            resp.raise_for_status()
            return self._to_app_record(resp.json())
        except requests.exceptions.RequestException as e:
            response_text = ""
            if getattr(e, "response", None) is not None:
                try:
                    response_text = e.response.text.strip()
                except Exception:
                    response_text = ""
            detail = f": {response_text}" if response_text else ""
            print(f"Error creating SharePoint list item: {e}{detail}")
            return None

    def update_record(self, record_id: str, alt_text: str, status: str = "reviewed") -> bool:
        try:
            resp = requests.patch(
                f"{self._list_url}/items/{record_id}/fields",
                headers=self._headers(),
                json={"AltText": alt_text, "Status": status},
                timeout=15,
            )
            resp.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            print(f"Error updating SharePoint list item {record_id}: {e}")
            return False

    def patch_fields(self, record_id: str, fields: dict) -> bool:
        """Update arbitrary fields on a record. Keys are app field names."""
        sp_fields = {self._TO_SP[k]: v for k, v in fields.items() if k in self._TO_SP}
        if not sp_fields:
            return False
        try:
            resp = requests.patch(
                f"{self._list_url}/items/{record_id}/fields",
                headers=self._headers(),
                json=sp_fields,
                timeout=15,
            )
            resp.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            print(f"Error patching SharePoint list item {record_id}: {e}")
            return False

    def bulk_patch_fields(self, patches: List[tuple[str, Dict]], max_workers: int = 4) -> Dict:
        """Update many records with bounded parallelism."""
        if not patches:
            return {"updated": 0, "failed_ids": [], "missing_ids": []}

        deduped: Dict[str, Dict] = {}
        for record_id, fields in patches:
            rid = str(record_id or "").strip()
            if not rid or not isinstance(fields, dict) or not fields:
                continue
            deduped.setdefault(rid, {}).update(fields)

        if not deduped:
            return {"updated": 0, "failed_ids": [], "missing_ids": []}

        max_workers = max(1, min(int(max_workers), 8))
        updated = 0
        failed_ids: List[str] = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self.patch_fields, rid, fields): rid
                for rid, fields in deduped.items()
            }
            for future in as_completed(future_map):
                rid = future_map[future]
                try:
                    ok = bool(future.result())
                except Exception:
                    ok = False
                if ok:
                    updated += 1
                else:
                    failed_ids.append(rid)

        return {"updated": updated, "failed_ids": failed_ids, "missing_ids": []}

    def bulk_delete_records(self, record_ids: List[str], max_workers: int = 4) -> int:
        """Delete many records with bounded parallelism."""
        ids = [str(rid or "").strip() for rid in record_ids if str(rid or "").strip()]
        ids = list(dict.fromkeys(ids))
        if not ids:
            return 0

        max_workers = max(1, min(int(max_workers), 8))

        def _delete_one(rid: str) -> bool:
            try:
                resp = requests.delete(
                    f"{self._list_url}/items/{rid}",
                    headers=self._headers(),
                    timeout=15,
                )
                return resp.status_code in (200, 204)
            except requests.exceptions.RequestException as e:
                print(f"Error deleting SharePoint list item {rid}: {e}")
                return False

        deleted = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_delete_one, rid) for rid in ids]
            for future in as_completed(futures):
                try:
                    if future.result():
                        deleted += 1
                except Exception:
                    continue
        return deleted

    def delete_records(self, record_ids: List[str]) -> int:
        deleted = 0
        for rid in record_ids:
            try:
                resp = requests.delete(
                    f"{self._list_url}/items/{rid}",
                    headers=self._headers(),
                    timeout=15,
                )
                if resp.status_code in (200, 204):
                    deleted += 1
            except requests.exceptions.RequestException as e:
                print(f"Error deleting SharePoint list item {rid}: {e}")
        return deleted

    def delete_all_records(self) -> int:
        print("Fetching all record IDs...")
        ids = self.get_all_record_ids()
        print(f"Found {len(ids)} records. Deleting...")
        deleted = self.delete_records(ids)
        print(f"Deleted {deleted} records.")
        return deleted
