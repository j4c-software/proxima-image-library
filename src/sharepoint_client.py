"""Microsoft Graph API client for SharePoint image storage."""

import concurrent.futures
import os
import time
from pathlib import Path, PurePosixPath
from typing import List, Tuple

import requests


class SharePointClient:
    """Access SharePoint via Microsoft Graph API using client credentials."""

    GRAPH_BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self):
        self.tenant_id = os.getenv("SHAREPOINT_TENANT_ID", "")
        self.client_id = os.getenv("SHAREPOINT_CLIENT_ID", "")
        self.client_secret = os.getenv("SHAREPOINT_CLIENT_SECRET", "")
        self.drive_id = os.getenv("SHAREPOINT_DRIVE_ID", "")
        self._token: str = ""
        self._token_expiry: float = 0.0
        _local = os.getenv("ONEDRIVE_LOCAL_PATH", "").strip()
        self._local_base: Path | None = Path(_local) if _local and Path(_local).is_dir() else None

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        resp = requests.post(
            url,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + data["expires_in"]
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    def list_folder(self, folder_path: str = "") -> List[dict]:
        """List immediate children of a folder. Returns Graph API item dicts."""
        if folder_path:
            url = f"{self.GRAPH_BASE}/drives/{self.drive_id}/root:/{folder_path}:/children"
        else:
            url = f"{self.GRAPH_BASE}/drives/{self.drive_id}/root/children"
        items = []
        while url:
            resp = requests.get(url, headers=self._headers(), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return items

    def list_all_images(self, folder_path: str = "") -> List[Tuple[str, str]]:
        """Recursively list all image files under folder_path.

        Returns list of (sharepoint_path, relative_path) tuples where:
          sharepoint_path — full path from drive root (e.g. "Images/High-Res/photo.jpg")
          relative_path   — relative to folder_path (e.g. "High-Res/photo.jpg")

        Uses local OneDrive sync when ONEDRIVE_LOCAL_PATH is set (much faster).
        """
        supported = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        if self._local_base is not None:
            local_dir = self._local_base / folder_path if folder_path else self._local_base
            if local_dir.is_dir():
                results: List[Tuple[str, str]] = []
                for dirpath, _, filenames in os.walk(local_dir):
                    for name in filenames:
                        if name.startswith(".") or name == "desktop.ini":
                            continue
                        if Path(name).suffix.lower() in supported:
                            full = Path(dirpath) / name
                            sp_path = str(full.relative_to(self._local_base)).replace(os.sep, "/")
                            rel = str(full.relative_to(local_dir)).replace(os.sep, "/")
                            results.append((sp_path, rel))
                return results
        results = []
        self._walk(folder_path, folder_path, supported, results)
        return results

    def _walk(self, root: str, current: str, supported: set, results: list):
        items = self.list_folder(current)
        folders = [item for item in items if "folder" in item]
        files = [item for item in items if "file" in item]

        for item in files:
            name = item["name"]
            path = f"{current}/{name}" if current else name
            if PurePosixPath(name).suffix.lower() in supported:
                rel = path[len(root):].lstrip("/") if root else path
                results.append((path, rel))

        if folders:
            sub_results: List[List[Tuple[str, str]]] = [[] for _ in folders]
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(folders))) as pool:
                futs = {
                    pool.submit(self._walk_collect, root, f"{current}/{f['name']}" if current else f["name"], supported): i
                    for i, f in enumerate(folders)
                }
                for fut in concurrent.futures.as_completed(futs):
                    sub_results[futs[fut]] = fut.result()
            for sub in sub_results:
                results.extend(sub)

    def _walk_collect(self, root: str, current: str, supported: set) -> List[Tuple[str, str]]:
        """Thread-safe wrapper around _walk that returns results instead of appending."""
        collected: List[Tuple[str, str]] = []
        self._walk(root, current, supported, collected)
        return collected

    def get_file_bytes(self, sharepoint_path: str) -> bytes:
        """Download file content by its full path from the drive root.

        Reads from local OneDrive sync when available to avoid an API round-trip.
        """
        if self._local_base is not None:
            local_file = (self._local_base / sharepoint_path).resolve()
            if not local_file.is_relative_to(self._local_base.resolve()):
                raise ValueError(f"Path escapes local base: {sharepoint_path}")
            if local_file.is_file():
                return local_file.read_bytes()
        url = f"{self.GRAPH_BASE}/drives/{self.drive_id}/root:/{sharepoint_path}:/content"
        resp = requests.get(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.content

    def get_file_metadata(self, sharepoint_path: str) -> dict:
        """Get file metadata including size, image dimensions, download URL, and thumbnails.

        The response includes:
          size                          — file size in bytes
          image.width / image.height   — pixel dimensions (image files only)
          @microsoft.graph.downloadUrl — pre-authenticated CDN URL (valid ~1 hour)
          thumbnails[0].medium.url     — SharePoint-generated 176px thumbnail URL
        """
        url = (
            f"{self.GRAPH_BASE}/drives/{self.drive_id}/root:/{sharepoint_path}"
            "?$expand=thumbnails"
        )
        resp = requests.get(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_file_url(self, sharepoint_path: str) -> str:
        """Return a pre-authenticated CDN URL for direct browser download (valid ~1 hour)."""
        meta = self.get_file_metadata(sharepoint_path)
        url = meta.get("@microsoft.graph.downloadUrl", "")
        if not url:
            raise ValueError(f"No download URL returned for {sharepoint_path}")
        return url

    def get_thumbnail_url(self, sharepoint_path: str, size: str = "medium") -> str:
        """Return SharePoint's generated thumbnail URL for the given size.

        Sizes: small (48px), medium (176px), large (1500px).
        Falls back to the full download URL if no thumbnail is available.
        """
        meta = self.get_file_metadata(sharepoint_path)
        thumbnails = meta.get("thumbnails", [])
        if thumbnails:
            thumb = thumbnails[0].get(size, {})
            if thumb.get("url"):
                return thumb["url"]
        # Fallback: direct download URL
        return meta.get("@microsoft.graph.downloadUrl", "")

    def upload_file(self, folder_path: str, filename: str, content_bytes: bytes) -> dict:
        """Upload a file to SharePoint, creating intermediate folders as needed.

        For files > 4 MB use the upload session API; for smaller files use PUT.
        """
        path = f"{folder_path}/{filename}" if folder_path else filename
        if len(content_bytes) > 4 * 1024 * 1024:
            return self._upload_session(path, content_bytes)
        url = f"{self.GRAPH_BASE}/drives/{self.drive_id}/root:/{path}:/content"
        resp = requests.put(
            url,
            headers={**self._headers(), "Content-Type": "application/octet-stream"},
            data=content_bytes,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_file(self, sharepoint_path: str) -> bool:
        """Delete a file by its full path from the drive root.

        Returns True when deleted and False when the file is missing.
        Raises for other non-success responses.
        """
        path = str(sharepoint_path or "").strip().strip("/")
        if not path:
            return False

        url = f"{self.GRAPH_BASE}/drives/{self.drive_id}/root:/{path}"
        resp = requests.delete(url, headers=self._headers(), timeout=30)
        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return False

    def _upload_session(self, path: str, content_bytes: bytes) -> dict:
        """Upload large files using the Graph API resumable upload session."""
        url = f"{self.GRAPH_BASE}/drives/{self.drive_id}/root:/{path}:/createUploadSession"
        session_resp = requests.post(
            url,
            headers=self._headers(),
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
            timeout=15,
        )
        session_resp.raise_for_status()
        upload_url = session_resp.json()["uploadUrl"]

        chunk_size = 4 * 1024 * 1024
        total = len(content_bytes)
        result = {}
        for start in range(0, total, chunk_size):
            chunk = content_bytes[start: start + chunk_size]
            end = start + len(chunk) - 1
            resp = requests.put(
                upload_url,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Content-Length": str(len(chunk)),
                },
                data=chunk,
                timeout=60,
            )
            resp.raise_for_status()
            if resp.status_code in (200, 201):
                result = resp.json()
        return result


class SharePointScanner(SharePointClient):
    """Scans SharePoint for images - same interface as ImageScanner.

    Returns (sharepoint_path, relative_path) tuples where:
        sharepoint_path - full path from drive root used for API calls
        relative_path - path relative to the image folder root (used in the Location field)
    """

    def get_all_images(self) -> List[Tuple[str, str]]:
        """List all images under SHAREPOINT_IMAGE_FOLDER/High-Res/."""
        root = os.getenv("SHAREPOINT_IMAGE_FOLDER", "Images")
        high_res_root = f"{root}/High-Res"
        return self.list_all_images(high_res_root)

    def get_new_images(self, processed_files: List[str]) -> List[Tuple[str, str]]:
        """Return images whose filename is not in processed_files."""
        all_images = self.get_all_images()
        processed = {PurePosixPath(f).name for f in processed_files}
        return [(sp, rel) for sp, rel in all_images if PurePosixPath(rel).name not in processed]

    def get_image_count(self) -> int:
        return len(self.get_all_images())

    @staticmethod
    def get_filename(path: str) -> str:
        return PurePosixPath(path).name
