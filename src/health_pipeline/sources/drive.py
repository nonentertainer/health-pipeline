"""Google Drive source, read with a service account.

Health Auto Export writes its scheduled exports into a Drive folder. Share that
folder (Viewer is enough) with the service account's e-mail address and pass
the folder id. Subfolders are walked too, since the app can nest exports.

Service accounts do not use expiring user refresh tokens, so the scheduled job
keeps working without anyone signing in again.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Iterator

FOLDER_MIME = "application/vnd.google-apps.folder"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


@dataclass
class RemoteFile:
    id: str
    name: str
    modified: str  # RFC 3339, sorts chronologically

    @property
    def state_key(self) -> str:
        return f"drive:{self.id}"


class DriveSource:
    def __init__(self, service_account_info: dict | str, folder_id: str, service=None):
        self.folder_id = folder_id
        if service is not None:  # injected in tests
            self.svc = service
            return
        from google.oauth2.service_account import Credentials  # imported lazily: optional dependency
        from googleapiclient.discovery import build

        info = json.loads(service_account_info) if isinstance(service_account_info, str) else service_account_info
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        self.svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    def _children(self, folder_id: str) -> Iterator[dict]:
        token = None
        while True:
            resp = self.svc.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                pageSize=1000,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            yield from resp.get("files", [])
            token = resp.get("nextPageToken")
            if not token:
                return

    def list_json(self) -> list[RemoteFile]:
        out, stack, seen = [], [self.folder_id], set()
        while stack:
            fid = stack.pop()
            if fid in seen:
                continue
            seen.add(fid)
            for f in self._children(fid):
                if f.get("mimeType") == FOLDER_MIME:
                    stack.append(f["id"])
                elif f.get("name", "").lower().endswith(".json"):
                    out.append(RemoteFile(f["id"], f["name"], f.get("modifiedTime", "")))
        return sorted(out, key=lambda r: (r.modified, r.name))

    def download(self, f: RemoteFile) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, self.svc.files().get_media(fileId=f.id, supportsAllDrives=True))
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue()
