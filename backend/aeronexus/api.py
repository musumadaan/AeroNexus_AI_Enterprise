"""HTTP layer: GraphQL (Strawberry) for the app, plus REST for health, metrics and file upload.

Production concerns handled here:
  * per-client token-bucket rate limiting (protects the LLM budget),
  * request timing → `Server-Timing` header and the latency report,
  * strict CORS allow-list from env,
  * PII is redacted before chat questions are logged,
  * GraphQL query depth limit (no unbounded nested queries).
Run: uvicorn aeronexus.api:app --reload --port 8000   → http://localhost:8000/graphql
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict
from typing import Any, Optional

import strawberry
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from strawberry.extensions import QueryDepthLimiter
from strawberry.fastapi import GraphQLRouter
from strawberry.scalars import JSON

from .config import DATA, settings
from .extraction.document import Sheet
from .extraction.regex_rules import redact
from .observability import tracer
from .calc import RULES, verify_receipt
from .cases import to_dict
from .public_data import catalog
from .research import verified_claims
from .security import PERMISSIONS, Forbidden, Principal, principal_from_headers
from .services import reset_services, services

# ---------------------------------------------------------------------------------------------
# GraphQL types
# ---------------------------------------------------------------------------------------------


@strawberry.type
class SheetSummary:
    id: str
    title: Optional[str]
    tags: list[str]
    customer: Optional[str]


@strawberry.type
class Approval:
    id: str
    tool: str
    risk: str
    reason: str
    status: str
    module: str
    args: JSON
    decided_by: Optional[str]
    result: Optional[JSON]


@strawberry.type
class ChatResponse:
    answer: str
    decision: str
    groundedness: float
    model: str
    sources: JSON
    claims: JSON
    instances: JSON
    chart: Optional[JSON]
    followups: list[str]


def _approval(a: Any) -> Approval:
    return Approval(id=a.id, tool=a.tool, risk=a.risk, reason=a.reason, status=a.status, module=a.module, args=a.args,
                    decided_by=a.decided_by, result=json.loads(json.dumps(a.result, default=str)) if a.result is not None else None)


def _report() -> dict[str, Any]:
    path = DATA / "reports" / "latest.json"
    if not path.exists():
        from .evals.runner import run_all

        return run_all()
    return json.loads(path.read_text())


ARCHITECTURE = {
    "layers": [
        {"id": "ingest", "label": "Ingest", "items": ["Drawings, logbooks, invoices, sensor data", "PDF text layer · OCR fallback",
                                                      "PII redaction · injection screen", "Dataset provenance + SHA-256"]},
        {"id": "perceive", "label": "Perceive", "items": ["Layout detector (YOLO / heuristic)", "Title block · BOM · notes · views",
                                                          "Token boxes kept for provenance"]},
        {"id": "extract", "label": "Extract", "items": ["Regex → NER → small → large model", "Verbatim hallucination guard",
                                                        "Cross-checks (title block vs BOM, label without value)"]},
        {"id": "model", "label": "Predict", "items": ["RUL + 90% interval (C-MAPSS)", "Engine health anomaly score",
                                                      "Safety triage with abstention (NTSB)", "Price model + conformal interval"]},
        {"id": "reason", "label": "Ground & calculate", "items": ["Hybrid RAG, revision-aware", "Contradiction detection",
                                                                  "Versioned rules + calculation receipts", "Knowledge graph impact"]},
        {"id": "act", "label": "Act (agents)", "items": ["Durable cases, resumable steps", "ERP · CRM · CMMS tools by risk tier",
                                                         "Idempotent approvals", "Email / voice with AI disclosure"]},
        {"id": "human", "label": "Human decision", "items": ["Explicit statuses: verified · needs review · blocked",
                                                             "Approved → executed", "Role-based access, tenant isolation"]},
        {"id": "assure", "label": "Assure", "items": ["Golden + public benchmarks", "Regression gate in CI", "Calibrated LLM judge",
                                                      "Latency · drift · audit log"]},
    ],
    "modules": [
        {"id": "change", "label": "Change order (flagship)", "path": "two revisions → diff → validate → impact → cost & lead time → ECR/ECO",
         "layers": ["ingest", "perceive", "extract", "reason", "act", "human", "assure"]},
        {"id": "pdm", "label": "Predictive maintenance", "path": "sensor history → RUL interval + health → rule → work order for review",
         "layers": ["ingest", "model", "reason", "act", "human", "assure"]},
        {"id": "safety", "label": "Safety analytics", "path": "narrative → triage with abstention → similar NTSB events → trends",
         "layers": ["ingest", "model", "reason", "human", "assure"]},
        {"id": "quote", "label": "Drawing to quote", "path": "drawing → cascade → policy checks → price range → drafts",
         "layers": ["ingest", "perceive", "extract", "model", "reason", "act", "human"]},
        {"id": "mro", "label": "Directive tracker", "path": "logbook → evidence match → due calculation → review",
         "layers": ["ingest", "extract", "reason", "human"]},
        {"id": "ap", "label": "Invoice matching", "path": "invoice → PO + receipt → effective-dated tolerance → exceptions",
         "layers": ["ingest", "extract", "reason", "act", "human"]},
    ],
}


def who(info: strawberry.Info) -> Principal:
    return principal_from_headers(info.context["request"].headers)


def need(info: strawberry.Info, perm: str) -> Principal:
    p = who(info)
    try:
        p.require(perm)
    except Forbidden as exc:
        raise PermissionError(str(exc))
    return p


@strawberry.type
class Query:
    @strawberry.field
    def health(self) -> JSON:
        r = services()
        from .llm import get_router

        router = get_router()
        from .llm import GroqProvider, get_judge_router

        judge = get_judge_router()
        return {"status": "ok", "llm_provider": router.provider_name, "circuit": router.breaker.state,
                "models": {"small": router.small, "large": router.large, "judge": f"{judge.provider_name}:{judge.small}"},
                "groq_limits": GroqProvider.last_limits if router.provider_name == "groq" else {},
                "kb_chunks": len(r.chunks), "golden_sheets": len(r.golden_sheets)}

    @strawberry.field
    def me(self, info: strawberry.Info) -> JSON:
        p = who(info)
        return {"user": p.user, "role": p.role, "tenant": p.tenant, "permissions": sorted(PERMISSIONS[p.role])}

    @strawberry.field
    def sheets(self, limit: int = 60) -> list[SheetSummary]:
        out = []
        for s in services().golden_sheets[:limit]:
            d = s.meta.get("difficulty", {})
            tags = [k for k, v in d.items() if v and k not in {"dropped", "noised_fields"}] + (["dropped_field"] if d.get("dropped") else [])
            out.append(SheetSummary(id=s.id, title=s.meta.get("truth", {}).get("title"), tags=tags, customer=s.meta.get("customer")))
        return out

    @strawberry.field
    def sheet(self, id: str) -> JSON:
        return services().sheet(id).to_dict()

    @strawberry.field
    def change_pairs(self) -> JSON:
        return [{"id": p["id"], "part_number": p["part_number"], "kind": p["truth"]["changed"]} for p in services().change_pairs]

    @strawberry.field
    def cases(self, info: strawberry.Info, status: Optional[str] = None, kind: Optional[str] = None) -> JSON:
        p = need(info, "read")
        return [{k: v for k, v in to_dict(c).items() if k != "data"} | {"severity": c.data.get("classify", {}).get("severity")}
                for c in services().cases.list(p.tenant, status, kind)]

    @strawberry.field
    def case(self, info: strawberry.Info, id: str) -> JSON:
        from .modules.change_order import finalize

        p = need(info, "read")
        return finalize(services().cases.get(id, p.tenant))

    @strawberry.field
    def replay_case(self, info: strawberry.Info, id: str) -> JSON:
        """Rebuild the case from its event log and compare with the live projection."""
        p = need(info, "read")
        store = services().cases
        live, rebuilt = to_dict(store.get(id, p.tenant)), to_dict(store.replay(id))
        receipts = [r for step in ("cost", "classify") for r in (rebuilt["data"].get(step, {}).get("receipts", [])
                    + ([rebuilt["data"][step]["receipt"]] if rebuilt["data"].get(step, {}).get("receipt") else []))]
        return {"identical": live == rebuilt, "events": len(store.events(id)),
                "receipts_reproduced": all(verify_receipt(r) for r in receipts), "receipts_checked": len(receipts)}

    @strawberry.field
    def mro(self, tail: Optional[str] = None) -> JSON:
        from .modules.mro import run_mro

        return json.loads(json.dumps(run_mro(tail), default=str))

    @strawberry.field
    def ap(self) -> JSON:
        from .modules.ap_match import run_ap

        return run_ap()

    @strawberry.field
    def pdm_fleet(self, subset: str = "FD001") -> JSON:
        from .modules.pdm import fleet

        return fleet(subset)

    @strawberry.field
    def engine_trend(self, subset: str, unit: int) -> JSON:
        from .modules.pdm import engine_trend

        return engine_trend(subset, unit)

    @strawberry.field
    def safety_overview(self) -> JSON:
        from .modules.safety import overview

        return overview()

    @strawberry.field
    def similar_events(self, text: str, k: int = 5) -> JSON:
        from .modules.safety import similar

        return similar(text[:2000], k=min(k, 10))

    @strawberry.field
    def datasets(self) -> JSON:
        return catalog()

    @strawberry.field
    def rules(self) -> JSON:
        return [{"id": r.id, "version": r.version, "effective_from": r.effective_from.isoformat(), "params": r.params,
                 "rationale": r.rationale} for r in RULES]

    @strawberry.field
    def eval_report(self) -> JSON:
        return _report()

    @strawberry.field
    def latency(self) -> JSON:
        return {"stages": tracer.latency_report(), "counters": dict(tracer.counters)}

    @strawberry.field
    def approvals(self, status: Optional[str] = "pending") -> list[Approval]:
        q = services().runtime.queue.values()
        return [_approval(a) for a in sorted(q, key=lambda a: -a.created) if status is None or a.status == status]

    @strawberry.field
    def audit(self, limit: int = 100) -> JSON:
        return [asdict(e) for e in services().runtime.audit[-limit:]][::-1]

    @strawberry.field
    def outbox(self) -> JSON:
        return services().runtime.outbox[::-1]

    @strawberry.field
    def research(self, module: Optional[str] = None) -> JSON:
        return verified_claims(module)

    @strawberry.field
    def architecture(self) -> JSON:
        return ARCHITECTURE


@strawberry.type
class Mutation:
    @strawberry.mutation
    def quote(self, info: strawberry.Info, sheet_id: str, customer_id: Optional[str] = None) -> JSON:
        from .modules.quote import run_quote

        need(info, "run")
        return json.loads(json.dumps(run_quote(services().sheet(sheet_id), customer_id), default=str))

    @strawberry.mutation
    def open_change_order(self, info: strawberry.Info, pair_id: str) -> JSON:
        from .modules.change_order import open_change_order

        p = need(info, "run")
        pair = next(x for x in services().change_pairs if x["id"] == pair_id)
        return open_change_order(Sheet.from_dict(pair["before"]), Sheet.from_dict(pair["after"]), pair["part_number"], p)

    @strawberry.mutation
    def decide_case(self, info: strawberry.Info, id: str, approve: bool, note: Optional[str] = "") -> JSON:
        from .modules.change_order import approve_change_order

        p = need(info, "approve")
        return approve_change_order(id, p, approve, note or "")

    @strawberry.mutation
    def assign_case(self, info: strawberry.Info, id: str, to: str) -> JSON:
        p = need(info, "run")
        return to_dict(services().cases.assign(id, p.tenant, to[:64], p.user or "unknown"))

    @strawberry.mutation
    def propose_work_order(self, info: strawberry.Info, subset: str, unit: int) -> JSON:
        from .modules.pdm import propose_work_order

        p = need(info, "run")
        return propose_work_order(subset, unit, p)

    @strawberry.mutation
    def safety_triage(self, info: strawberry.Info, text: str) -> JSON:
        from .modules.safety import similar, triage

        need(info, "run")
        return {"triage": triage(text[:4000]), "similar": similar(text[:2000], k=5)}

    @strawberry.mutation
    def decide_approval(self, info: strawberry.Info, id: str, approve: bool, approver: Optional[str] = None,
                        edits: Optional[JSON] = None) -> Approval:
        p = need(info, "approve")
        return _approval(services().runtime.decide(id, p.user or (approver or ""), approve, edits))

    @strawberry.mutation
    def chat(self, question: str) -> ChatResponse:
        from .chat import get_chat

        with tracer.span("api.chat", q=redact(question)[:120]):
            r = get_chat().ask(question[:1000])
        return ChatResponse(answer=r["answer"], decision=r["decision"], groundedness=r["groundedness"], model=r["model"],
                            sources=r["sources"], claims=r["claims"], instances=r["instances"], chart=r["chart"], followups=r["followups"])

    @strawberry.mutation
    def run_evals(self, info: strawberry.Info) -> JSON:
        from .chat import get_chat
        from .evals.runner import run_all

        p = need(info, "evaluate")
        rep = run_all()
        get_chat().reload()
        from .mlops import record_eval

        rep["tracking"] = record_eval(rep, p.user or "admin")
        return rep

    @strawberry.mutation
    def reset_demo(self, info: strawberry.Info) -> bool:
        need(info, "admin")
        reset_services()
        return True


schema = strawberry.Schema(query=Query, mutation=Mutation, extensions=[lambda: QueryDepthLimiter(max_depth=8)])

# ---------------------------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------------------------
app = FastAPI(title="AeroNexus AI", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("AERONEXUS_CORS", "http://localhost:3000").split(","),
                   allow_methods=["*"], allow_headers=["*"])


class TokenBucket:
    def __init__(self, rate: float = 10.0, burst: int = 40) -> None:
        self.rate, self.burst = rate, burst
        self.state: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            tokens, last = self.state.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1:
                self.state[key] = (tokens, now)
                return False
            self.state[key] = (tokens - 1, now)
            return True


bucket = TokenBucket(rate=float(os.environ.get("AERONEXUS_RPS", "10")), burst=int(os.environ.get("AERONEXUS_BURST", "40")))


@app.middleware("http")
async def timing_and_limits(request: Request, call_next):
    client = request.client.host if request.client else "anon"
    if request.url.path.startswith(("/graphql", "/api/")) and request.method in ("POST", "PUT", "DELETE") and not bucket.allow(client):
        return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
    t0 = time.perf_counter()
    resp = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    resp.headers["Server-Timing"] = f"app;dur={ms:.1f}"
    tracer.incr("http.requests")
    if ms > settings.p95_budget_ms:
        tracer.incr("http.over_budget")
    return resp


app.include_router(GraphQLRouter(schema), prefix="/graphql")
from .api_ops import router as ops_router  # noqa: E402  (twin, mission control, workflows, observability)

app.include_router(ops_router)

from .integrations.flags import integrations_enabled  # noqa: E402

if integrations_enabled():  # MCP / mock SAP / Sheets / approval queue: optional layer, off with AERONEXUS_FF_INTEGRATIONS=0
    from .integrations.api import router as integrations_router

    app.include_router(integrations_router)


@app.on_event("startup")
def _warm() -> None:
    """Build the 17k-event safety index and load trained models off the request path."""
    def run() -> None:
        try:
            from .modules import pdm, safety

            safety.index()
            for s_ in pdm.SUBSETS:
                pdm.bundle(s_)
        except Exception:  # warm-up is best effort; requests build lazily if it fails
            pass

    threading.Thread(target=run, daemon=True).start()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    """Prometheus exposition format."""
    lines = []
    for k, v in tracer.counters.items():
        lines.append(f'aeronexus_counter{{name="{k}"}} {v}')
    for stage, s in tracer.latency_report().items():
        for q in ("p50", "p95", "p99"):
            lines.append(f'aeronexus_latency_ms{{stage="{stage}",quantile="{q}"}} {s[q]:.3f}')
    return "\n".join(lines) + "\n"


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    """Real document path: PDF → text layer / OCR → quote pipeline (needs pdfplumber; pytesseract for scans)."""
    if not principal_from_headers(request.headers).can("run"):
        raise HTTPException(403, "Your role can't upload documents")
    if not file.filename or not file.filename.lower().endswith((".pdf", ".json")):
        raise HTTPException(400, "Upload a PDF drawing or a sheet JSON")
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "File too large (25 MB limit)")
    from .modules.quote import run_quote

    if file.filename.lower().endswith(".json"):
        sheet = Sheet.from_dict(json.loads(data))
    else:
        try:
            from .extraction.document import ocr_pdf
        except ImportError as exc:  # pragma: no cover
            raise HTTPException(501, f"PDF support needs pdfplumber: {exc}")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(data)
        sheet = ocr_pdf(tmp.name)[0]
    services().uploads[sheet.id] = sheet
    return json.loads(json.dumps(run_quote(sheet), default=str))


from .static_site import mount as _mount_frontend  # noqa: E402  (single-service deploys: serve frontend/dist when configured)

_mount_frontend(app)
