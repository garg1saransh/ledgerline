"""Share one SQLite workspace across Vercel instances."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path

import httpx

from app.db import Database

API = "https://vercel.com/api/blob"
DB_PATHNAME = "ledgerline/workbench.db"
LOCK_PATHNAME = "ledgerline/writer.lock"
LOCK_STALE_SECONDS = 75


class WorkspaceError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class Workspace:
    def __init__(self, path: Path, database: Database, token: str, client: httpx.Client | None = None):
        parts = token.split("_")
        if len(parts) < 5 or not parts[3]:
            raise WorkspaceError("Blob storage token is not usable.")
        self.path = path
        self.database = database
        self.token = token
        self.store_id = parts[3]
        self.client = client or httpx.Client(timeout=30)
        self.gate = asyncio.Lock()
        self.etag: str | None = None
        self._digest: str | None = None
        self._holding = False
        self._lock_url: str | None = None

    @classmethod
    def from_env(cls, path: Path, database: Database) -> Workspace | None:
        if not os.environ.get("VERCEL"):
            return None
        token = os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip()
        if not token:
            return None
        return cls(path, database, token)

    def pull(self) -> None:
        response = self.client.get(self._private_url(DB_PATHNAME), headers=self._auth())
        if response.status_code == 404:
            self.etag = None
            self._reset_local()
            return
        if response.status_code >= 400:
            raise WorkspaceError(self._failure("load", response))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".db.part")
        temporary.write_bytes(response.content)
        os.replace(temporary, self.path)
        self.etag = response.headers.get("etag")
        self._digest = self._hash()

    def push_if_changed(self) -> None:
        digest = self._hash()
        if digest == self._digest:
            return
        headers = self._write_headers(allow_overwrite=True)
        response = self.client.put(
            f"{API}/",
            params={"pathname": DB_PATHNAME},
            headers=headers,
            content=self.path.read_bytes(),
        )
        if response.status_code == 412:
            raise WorkspaceError("The workspace changed while it was being saved. Try again.")
        if response.status_code >= 400:
            raise WorkspaceError(self._failure("save", response))
        payload = response.json()
        self.etag = payload.get("etag") or response.headers.get("etag")
        self._digest = digest

    def acquire_writer(self) -> None:
        self._holding = False
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            response = self.client.put(
                f"{API}/",
                params={"pathname": LOCK_PATHNAME},
                headers=self._write_headers(allow_overwrite=False),
                content=str(time.time()).encode(),
            )
            if response.status_code < 400:
                payload = response.json()
                self._lock_url = payload.get("url")
                self._holding = True
                return
            if response.status_code in {401, 403}:
                raise WorkspaceError(self._failure("lock", response))
            age = self._lock_age()
            if age is None or age > LOCK_STALE_SECONDS:
                self._delete(self._lock_url_for(LOCK_PATHNAME))
                continue
            time.sleep(0.3)
        raise WorkspaceError("Another change is being saved. Try again.")

    def release_writer(self) -> None:
        if not self._holding:
            return
        url = self._lock_url or self._lock_url_for(LOCK_PATHNAME)
        try:
            self._delete(url)
        finally:
            self._holding = False
            self._lock_url = None

    def _reset_local(self) -> None:
        if self.path.exists():
            self.path.unlink()
        self.database.ensure_schema()
        self._digest = self._hash()

    def _hash(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def _auth(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}"}

    def _api_headers(self) -> dict[str, str]:
        return {
            **self._auth(),
            "x-api-version": "12",
            "x-vercel-blob-store-id": self.store_id,
        }

    def _write_headers(self, allow_overwrite: bool) -> dict[str, str]:
        return {
            **self._api_headers(),
            "x-vercel-blob-access": "private",
            "x-content-type": "application/octet-stream",
            "x-add-random-suffix": "0",
            "x-allow-overwrite": "1" if allow_overwrite else "0",
        }

    def _private_url(self, pathname: str) -> str:
        return f"{self._lock_url_for(pathname)}?cache=0"

    def _lock_url_for(self, pathname: str) -> str:
        return f"https://{self.store_id}.private.blob.vercel-storage.com/{pathname}"

    def _lock_age(self) -> float | None:
        response = self.client.get(self._private_url(LOCK_PATHNAME), headers=self._auth())
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            return 0
        try:
            return time.time() - float(response.text.strip())
        except ValueError:
            return LOCK_STALE_SECONDS + 1

    def _delete(self, url: str) -> None:
        response = self.client.post(
            f"{API}/delete",
            headers={**self._api_headers(), "content-type": "application/json"},
            json={"urls": [url]},
        )
        if response.status_code >= 400 and response.status_code != 404:
            raise WorkspaceError(self._failure("unlock", response))

    def _failure(self, action: str, response: httpx.Response) -> str:
        detail = ""
        try:
            payload = response.json()
            detail = str((payload.get("error") or {}).get("message") or "")
        except Exception:
            detail = ""
        if detail:
            return f"Could not {action} the workspace ({response.status_code}: {detail})."
        return f"Could not {action} the workspace ({response.status_code})."
