"""Salesforce connector: local mock by default, or a free Salesforce Developer Edition org.

    AERONEXUS_FF_SALESFORCE=off    (default) not registered
    AERONEXUS_FF_SALESFORCE=mock   in-process mock seeded from the CRM seed (Accounts, Cases, Tasks); no account needed
    AERONEXUS_FF_SALESFORCE=live   real REST API of a Developer Edition org (free, no licence cost). Create a Connected App
                                   with the OAuth client-credentials flow, then set
                                   SALESFORCE_DOMAIN_URL      https://<your-domain>.my.salesforce.com
                                   SALESFORCE_CLIENT_ID / SALESFORCE_CLIENT_SECRET
                                   (or SALESFORCE_ACCESS_TOKEN for a short-lived session token)
                                   SALESFORCE_API_VERSION     default v62.0

Tools follow the agent runtime's risk tiers: finding accounts is READ; opening a Case is WRITE and waits for a named
approver; logging a Task is DRAFT (an internal note). Field names match Salesforce's standard objects.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from typing import Any

import httpx

from ..config import DATA
from .secrets import get_secret


def mode() -> str:
    from .flags import integrations_enabled

    if not integrations_enabled():
        return "off"
    v = os.environ.get("AERONEXUS_FF_SALESFORCE", "off").strip().lower()
    return v if v in ("mock", "live") else "off"


class MockSalesforce:
    def __init__(self) -> None:
        seed = json.loads((DATA / "crm_seed.json").read_text())
        self.lock = threading.Lock()
        self.accounts = [{"Id": f"001{i:015d}", "Name": a["name"], "AccountNumber": a["id"], "Rating": {"A": "Hot", "B": "Warm"}.get(a.get("tier"), "Cold"),
                          "Email__c": a["email"], "Parts__c": ";".join(a.get("parts", []))} for i, a in enumerate(seed["accounts"], 1)]
        self.cases: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []

    def find_accounts(self, query: str = "", part_number: str = "") -> list[dict[str, Any]]:
        q = query.lower()
        return [a for a in self.accounts if (not q or q in a["Name"].lower()) and (not part_number or part_number in a["Parts__c"])][:20]

    def find_opportunities(self, account_id: str) -> list[dict[str, Any]]:
        if not any(a["Id"] == account_id for a in self.accounts):
            raise ValueError("Unknown Salesforce account ID")
        return []

    def create_case(self, account_id: str, subject: str, description: str, priority: str = "Medium") -> dict[str, Any]:
        if not any(a["Id"] == account_id or a["AccountNumber"] == account_id for a in self.accounts):
            raise ValueError(f"unknown account {account_id}")
        with self.lock:
            c = {"Id": f"500{len(self.cases) + 1:015d}", "CaseNumber": f"{1000 + len(self.cases) + 1:08d}", "AccountId": account_id,
                 "Subject": subject[:255], "Description": description[:4000], "Priority": priority, "Status": "New",
                 "Origin": "AeroNexus", "CreatedDate": dt.datetime.now().isoformat(timespec="seconds")}
            self.cases.append(c)
        return c

    def log_task(self, account_id: str, subject: str) -> dict[str, Any]:
        with self.lock:
            t = {"Id": f"00T{len(self.tasks) + 1:015d}", "WhatId": account_id, "Subject": subject[:255], "Status": "Completed",
                 "ActivityDate": dt.date.today().isoformat()}
            self.tasks.append(t)
        return t

    def summary(self) -> dict[str, Any]:
        return {"mode": "mock", "accounts": len(self.accounts), "cases": len(self.cases), "tasks": len(self.tasks)}


class LiveSalesforce:
    """Minimal REST client for a Developer Edition org (SOQL query + sObject create)."""

    def __init__(self) -> None:
        self.base = (get_secret("SALESFORCE_DOMAIN_URL") or "").rstrip("/")
        self.version = os.environ.get("SALESFORCE_API_VERSION", "v62.0")
        self._token: str | None = get_secret("SALESFORCE_ACCESS_TOKEN")

    def _auth(self) -> dict[str, str]:
        if not self._token:
            cid, sec = get_secret("SALESFORCE_CLIENT_ID"), get_secret("SALESFORCE_CLIENT_SECRET")
            if not (self.base and cid and sec):
                raise RuntimeError("Salesforce live mode needs SALESFORCE_DOMAIN_URL plus client id/secret or an access token")
            r = httpx.post(f"{self.base}/services/oauth2/token", data={"grant_type": "client_credentials", "client_id": cid,
                                                                       "client_secret": sec}, timeout=20)
            r.raise_for_status()
            self._token = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._token}"}

    def find_accounts(self, query: str = "", part_number: str = "") -> list[dict[str, Any]]:
        safe = re.sub(r"[^\w .&-]", "", query)[:80]  # SOQL literal: strip quotes and wildcards
        soql = "SELECT Id, Name, AccountNumber, Rating FROM Account" + (f" WHERE Name LIKE '%{safe}%'" if safe else "") + " LIMIT 20"
        r = httpx.get(f"{self.base}/services/data/{self.version}/query", params={"q": soql}, headers=self._auth(), timeout=20)
        r.raise_for_status()
        return [{k: v for k, v in rec.items() if k != "attributes"} for rec in r.json().get("records", [])]

    def find_opportunities(self, account_id: str) -> list[dict[str, Any]]:
        if not re.fullmatch(r"[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?", account_id):
            raise ValueError("Invalid Salesforce account ID")

        soql = (
            "SELECT Id, Name, AccountId, StageName, Amount, CloseDate, Probability "
            "FROM Opportunity "
            f"WHERE AccountId = '{account_id}' "
            "ORDER BY CloseDate ASC NULLS LAST "
            "LIMIT 100"
        )
        r = httpx.get(
            f"{self.base}/services/data/{self.version}/query",
            params={"q": soql},
            headers=self._auth(),
            timeout=20,
        )
        r.raise_for_status()
        return [
            {k: v for k, v in record.items() if k != "attributes"}
            for record in r.json().get("records", [])
        ]

    def _create(self, obj: str, fields: dict[str, Any]) -> dict[str, Any]:
        r = httpx.post(f"{self.base}/services/data/{self.version}/sobjects/{obj}", json=fields, headers=self._auth(), timeout=20)
        r.raise_for_status()
        return {"Id": r.json().get("id"), **fields}

    def create_case(self, account_id: str, subject: str, description: str, priority: str = "Medium") -> dict[str, Any]:
        return self._create("Case", {"AccountId": account_id, "Subject": subject[:255], "Description": description[:4000],
                                     "Priority": priority, "Origin": "Web"})

    def log_task(self, account_id: str, subject: str) -> dict[str, Any]:
        return self._create("Task", {"WhatId": account_id, "Subject": subject[:255], "Status": "Completed"})

    def summary(self) -> dict[str, Any]:
        return {"mode": "live", "instance": self.base, "api_version": self.version, "authenticated": bool(self._token)}


def register(runtime: Any) -> None:
    from ..agents import Risk, Tool

    m = mode()
    if m == "off":
        return
    sf = MockSalesforce() if m == "mock" else LiveSalesforce()
    runtime.salesforce = sf
    runtime.register(Tool("sf.find_accounts", Risk.READ, {"query": "str?", "part_number": "str?"}, sf.find_accounts,
                          f"Salesforce accounts ({m})"))
    runtime.register(Tool("sf.find_opportunities", Risk.READ, {"account_id": "str"}, sf.find_opportunities,
                          f"Salesforce opportunities for an account ({m})"))
    runtime.register(Tool("sf.create_case", Risk.WRITE, {"account_id": "str", "subject": "str", "description": "str", "priority": "str?"},
                          sf.create_case, f"Open a Salesforce Case ({m}); approval required"))
    runtime.register(Tool("sf.log_task", Risk.DRAFT, {"account_id": "str", "subject": "str"}, sf.log_task,
                          f"Log a completed Task on a Salesforce account ({m})"))
