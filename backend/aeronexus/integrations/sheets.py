"""Google Sheets tracking (free): every approval decision and executed integration action becomes a row.

Modes (see flags.sheets_mode):
  google  appends rows with the Sheets API v4 using a Google Cloud service account (free; no billing account needed for
          the Sheets API). Share the spreadsheet with the service account's email as Editor.
  local   the same rows go to data/integrations/tracking.csv, so the demo works with no Google account at all.

Credentials come from the environment only (never from code or logs):
  GOOGLE_SHEETS_SPREADSHEET_ID      the id in the sheet URL
  GOOGLE_SERVICE_ACCOUNT_JSON_B64   base64 of the key JSON (best for Railway / GitHub secrets), or
  GOOGLE_APPLICATION_CREDENTIALS    path to the key JSON file (local development)
  GOOGLE_SHEETS_TAB                 tab name, default "Tracking"

Writes happen on a background thread with retries, so a slow or failing Sheets call never blocks an approval.
"""
from __future__ import annotations

import base64
import csv
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

from ..config import DATA
from .flags import sheets_mode

HEADER = ["timestamp", "event", "tool", "approval_id", "actor", "status", "module", "reference", "summary", "amount_cad"]
SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def _local_path() -> Path:
    return Path(os.environ["AERONEXUS_TRACKING_CSV"]) if os.environ.get("AERONEXUS_TRACKING_CSV") else DATA / "integrations" / "tracking.csv"


class SheetsTracker:
    def __init__(self) -> None:
        self.q: queue.Queue[list[Any]] = queue.Queue(maxsize=5000)
        self.stats = {"queued": 0, "written": 0, "failed": 0, "last_error": None, "last_write": None}
        self._creds = None
        self._header_checked = False
        self._thread: threading.Thread | None = None
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- public
    @property
    def mode(self) -> str:
        return sheets_mode()

    def track(self, event: str, tool: str = "", approval_id: str = "", actor: str = "", status: str = "", module: str = "",
              reference: str = "", summary: str = "", amount: float | None = None) -> None:
        if self.mode == "off":
            return
        row = [time.strftime("%Y-%m-%d %H:%M:%S"), event, tool, approval_id, actor, status, module, reference, summary[:300],
               "" if amount is None else round(float(amount), 2)]
        try:
            self.q.put_nowait(row)
            self.stats["queued"] += 1
        except queue.Full:
            self.stats["failed"] += 1
        self._ensure_worker()

    def flush(self, timeout: float = 10.0) -> None:
        """Drain the queue synchronously (tests, shutdown)."""
        end = time.time() + timeout
        while not self.q.empty() and time.time() < end:
            self._drain_once()

    def status(self) -> dict[str, Any]:
        sid = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID", "")
        return {"mode": self.mode, **self.stats, "pending": self.q.qsize(),
                "spreadsheet_url": f"https://docs.google.com/spreadsheets/d/{sid}" if self.mode == "google" and sid else None,
                "local_file": str(_local_path()) if self.mode == "local" else None, "tab": os.environ.get("GOOGLE_SHEETS_TAB", "Tracking")}

    def recent_local(self, limit: int = 30) -> list[dict[str, Any]]:
        p = _local_path()
        if not p.exists():
            return []
        with p.open() as f:
            rows = list(csv.DictReader(f))
        return rows[-limit:][::-1]

    # ---------------------------------------------------------------- worker
    def _ensure_worker(self) -> None:
        with self.lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True, name="sheets-tracker")
                self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                row = self.q.get(timeout=30)
            except queue.Empty:
                return
            self._write_with_retry([row])

    def _drain_once(self) -> None:
        rows = []
        while not self.q.empty() and len(rows) < 50:
            rows.append(self.q.get_nowait())
        if rows:
            self._write_with_retry(rows)

    def _write_with_retry(self, rows: list[list[Any]]) -> None:
        for attempt in range(3):
            try:
                (self._write_google if self.mode == "google" else self._write_local)(rows)
                self.stats["written"] += len(rows)
                self.stats["last_write"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.stats["last_error"] = None
                return
            except Exception as exc:  # noqa: BLE001 - tracking must never break the app
                self.stats["last_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                time.sleep(0.5 * 2 ** attempt)
        self.stats["failed"] += len(rows)

    def _write_local(self, rows: list[list[Any]]) -> None:
        p = _local_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with p.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(HEADER)
            w.writerows(rows)

    
    def _token(self) -> str:
        from google.auth.transport.requests import Request

        if self._creds is None:
            from .secrets import get_secret

            b64 = get_secret("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
            credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")

            if b64:
                from google.oauth2 import service_account

                info = json.loads(base64.b64decode(b64))
                self._creds = service_account.Credentials.from_service_account_info(
                    info, scopes=[SCOPE]
                )
            elif credentials_path:
                from google.oauth2 import service_account

                self._creds = service_account.Credentials.from_service_account_file(
                    credentials_path, scopes=[SCOPE]
                )
            else:
                import google.auth

                self._creds, _ = google.auth.default(scopes=[SCOPE])

        if not self._creds.valid:
            self._creds.refresh(Request())

        return self._creds.token


    def _write_google(self, rows: list[list[Any]]) -> None:
        import httpx

        sid = os.environ["GOOGLE_SHEETS_SPREADSHEET_ID"]
        tab = os.environ.get("GOOGLE_SHEETS_TAB", "Tracking")
        h = {"Authorization": f"Bearer {self._token()}"}
        base = f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values"
        if not self._header_checked:
            r = httpx.get(f"{base}/{tab}!A1:J1", headers=h, timeout=15)
            r.raise_for_status()
            if not r.json().get("values"):
                rows = [HEADER] + rows
            self._header_checked = True
        r = httpx.post(f"{base}/{tab}!A:J:append", params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                       headers=h, json={"values": rows}, timeout=15)
        r.raise_for_status()


tracker = SheetsTracker()
