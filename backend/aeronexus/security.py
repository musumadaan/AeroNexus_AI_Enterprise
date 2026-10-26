"""Role-based access control + tenant context.

Demo authentication reads `X-User`, `X-Role` and `X-Tenant` headers (the UI's role switcher sets
them). In production replace `principal_from_headers` with OIDC/JWT validation (Entra ID, Okta …);
everything downstream only sees a `Principal`, so nothing else changes.

Least privilege: viewers read; engineers run workflows; approvers decide; admins also run
evaluations and change rules. Agents run as a non-human principal that can never approve.
"""
from __future__ import annotations

from dataclasses import dataclass

PERMISSIONS: dict[str, set[str]] = {
    "viewer": {"read"},
    "engineer": {"read", "run", "comment"},
    "approver": {"read", "run", "comment", "approve"},
    "admin": {"read", "run", "comment", "approve", "evaluate", "admin"},
    "agent": {"read", "run"},
}


class Forbidden(PermissionError):
    pass


@dataclass(frozen=True)
class Principal:
    user: str
    role: str
    tenant: str

    def can(self, perm: str) -> bool:
        return perm in PERMISSIONS.get(self.role, set())

    def require(self, perm: str) -> None:
        if not self.can(perm):
            raise Forbidden(f"role '{self.role}' cannot '{perm}'")
        if perm == "approve" and (self.role == "agent" or not self.user or self.user == "agent"):
            raise Forbidden("approvals must come from a named human")


def principal_from_headers(headers) -> Principal:
    role = (headers.get("x-role") or "viewer").lower()
    if role not in PERMISSIONS or role == "agent":
        role = "viewer"
    return Principal(user=(headers.get("x-user") or "").strip()[:64], role=role,
                     tenant=(headers.get("x-tenant") or "demo").strip()[:32] or "demo")


AGENT = Principal("agent", "agent", "demo")
