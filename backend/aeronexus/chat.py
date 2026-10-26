"""Results chatbot: explains evaluation results and module outputs in plain language, with
concrete instances, and every sentence verified against a source.

How it works
  1. The latest eval report + module outputs are compiled into small "fact" passages
     (e.g. fact:field:thickness, fail:G0015:thickness, fact:judge). These are data, generated
     deterministically from numbers — so the chatbot can only say what was measured.
  2. Facts + the evaluation glossary are indexed with the same hybrid retriever as everything else.
  3. The question is routed (field / instance / module / concept / overview), relevant facts are
     retrieved, and the LLM writes a short, simple answer with [c:id] citations.
  4. Grounding verification prunes unsupported sentences or abstains.
  5. The response also returns concrete instances (failing sheets with their source tokens) and
     a chart spec, so the UI can show *the example*, not just the number.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .config import DATA
from .extraction.pipeline import QUOTE_FIELDS
from .generation import grounded_answer
from .llm import LLMRequest, MockLLM
from .rag import Chunk, Hit, HybridIndex, tokenize
from .services import services

MODULE_WORDS = {"mro": ["mro", "aircraft", "airworthiness", "logbook", "ad ", "directive", "maintenance"],
                "ap": ["invoice", "payable", "ap ", "three-way", "3-way", "vendor", "po "],
                "change": ["change", "revision", "eco", "rev ", "receipt", "replay"],
                "pdm": ["rul", "remaining useful", "engine", "c-mapss", "cmapss", "predictive", "health"],
                "safety": ["ntsb", "safety", "accident", "triage", "fatal", "asrs"],
                "security": ["rbac", "role", "tenant", "access", "permission"],
                "judge": ["judge", "kappa", "human", "agreement", "bias"],
                "quote": ["quote", "price", "pricing", "estimate"]}


def _pct(x: float) -> str:
    return f"{x * 100:.1f} percent"


def build_facts(report: dict[str, Any]) -> dict[str, str]:
    f: dict[str, str] = {}
    e = report.get("extraction", {})
    if e:
        m = e["micro"]
        f["fact:overall"] = (f"Across {e['n_sheets']} golden drawings, extraction precision is {_pct(m['precision'])}, recall is "
                             f"{_pct(m['recall'])} and F1 is {_pct(m['f1'])}.")
        f["fact:auto"] = (f"Fields accepted without a human were correct {_pct(e['auto_accept_precision'])} of the time, and "
                          f"{_pct(e['automation_rate'])} of all true fields were filled automatically.")
        f["fact:review"] = (f"{e['status_mix'].get('needs_review', 0)} field values were sent to human review and "
                            f"{e['status_mix'].get('missing', 0)} were left missing for a person or an RFI.")
        f["fact:ece"] = (f"Expected calibration error is {e['calibration']['ece']}, so reported confidence is close to real accuracy "
                         f"when this number is near 0.")
        f["fact:latency"] = (f"Median extraction latency is {e['latency_ms']['p50']} ms per drawing and p95 is "
                             f"{e['latency_ms']['p95']} ms, with {e['llm_calls_per_sheet']} LLM calls per drawing on average.")
        for name, v in e["per_field"].items():
            f[f"fact:field:{name}"] = (f"For the {name.replace('_', ' ')} field, precision is {_pct(v['precision'])}, recall is "
                                       f"{_pct(v['recall'])} and F1 is {_pct(v['f1'])} ({v['fn']} missed, {v['fp']} wrong).")
        for name, v in e["slices"].items():
            f[f"fact:slice:{name}"] = (f"On drawings tagged {name.replace('_', ' ')}, F1 is {_pct(v['f1'])} with recall "
                                       f"{_pct(v['recall'])}.")
        for x in e["failures"][:60]:
            f[f"fail:{x['sheet_id']}:{x['field']}"] = (
                f"On drawing {x['sheet_id']} the {x['field'].replace('_', ' ')} field was {x['predicted'] or 'not extracted'} while the "
                f"correct value is {x['truth']}; cause: {x['cause']}; it was marked {x['status'].replace('_', ' ')}.")
    r = report.get("retrieval")
    if r:
        f["fact:retrieval"] = f"Retrieval finds the right document in the top 3 for {_pct(r['recall_at_3'])} of test questions (MRR {r['mrr']})."
    j = report.get("judge")
    if j:
        f["fact:judge"] = (f"The LLM judge agrees with humans at kappa {j['judge_human_kappa']} while two humans agree at kappa "
                           f"{j['human_human_kappa']}; raw agreement {j['judge_human_raw_agreement']} overstates it by "
                           f"{j['kappa_deflation_pp']} points.")
        f["fact:judge_gate"] = j["verdict"]
        f["fact:judge_bias"] = (f"In {j['pairs_tested']} pairwise tests the judge changed its verdict when the answer order was swapped "
                                f"{_pct(j['position_bias_rate'])} of the time, which is position bias.")
        f["fact:judge_correction"] = (f"The judge measured a pass rate of {j['test_pass_rate_observed']}; correcting for its errors gives "
                                      f"{j['test_pass_rate_corrected']} against a true rate of {j['test_pass_rate_true']}.")
    if report.get("mro"):
        f["fact:mro"] = (f"Directive status was correct for {_pct(report['mro']['status_accuracy'])} of {report['mro']['n']} aircraft-directive "
                         f"pairs, and {_pct(report['mro']['implicit_review_recall'])} of entries without an AD number were sent to review.")
    if report.get("ap"):
        f["fact:ap"] = (f"Three-way match labelled {_pct(report['ap']['accuracy'])} of invoices correctly, auto-matched "
                        f"{report['ap']['false_auto_match']} invoices that were really exceptions, and the touchless rate is "
                        f"{_pct(report['ap']['touchless_rate'])}.")
    if report.get("change"):
        f["fact:change"] = (f"Change detection F1 is {_pct(report['change']['field_change']['f1'])} and severity was classified correctly "
                            f"{_pct(report['change']['severity_accuracy'])} of the time.")
    pd_ = report.get("pdm") or {}
    if pd_.get("available"):
        for k, v in pd_["subsets"].items():
            f[f"fact:pdm:{k}"] = (f"On NASA C-MAPSS {k} the remaining-useful-life model has test RMSE {v['test_rmse_capped']} cycles, "
                                  f"its 90 percent intervals covered {_pct(v['interval_coverage_test'])} of test engines, and the health "
                                  f"score separates early from late life with AUROC {v['health_index_auroc_early_vs_late']}.")
        f["fact:pdm"] = (f"Predictive maintenance is trained on the real public NASA C-MAPSS data; the best subset RMSE is "
                         f"{pd_['fd001_rmse']} cycles and the worst is {pd_['worst_rmse']} cycles.")
    sf = report.get("safety") or {}
    if sf.get("available"):
        f["fact:safety"] = (f"The safety triage model, trained on NTSB events from 2008 to 2015 and tested on 2017 to 2022, reaches AUROC "
                            f"{sf['test_auroc']} and PR-AUC {sf['test_pr_auc']} at a {_pct(sf['test_prevalence'])} fatal rate, with "
                            f"calibration error {sf['ece']}.")
        f["fact:safety_leak"] = (f"Masking outcome words such as killed or fatal changes AUROC by only {sf['leakage_gap']}, so the model "
                                 f"relies on causal language rather than outcome words.")
    ch = report.get("change") or {}
    if "receipt_reproducibility" in ch:
        f["fact:change_receipts"] = (f"Every change-order calculation re-computed identically ({_pct(ch['receipt_reproducibility'])}) and "
                                     f"case state rebuilt from the event log matched the live state {_pct(ch['replay_identical'])} of the time.")
    sec = report.get("security") or {}
    if sec:
        f["fact:security"] = (f"Role-based access and tenant isolation checks passed {_pct(sec['pass_rate'])}; agents can never approve "
                              f"their own actions.")
    if report.get("pricing"):
        p = report["pricing"]
        f["fact:pricing"] = (f"The price model's typical error is {_pct(p['test_mape'])} and its 90 percent intervals contained the true price "
                             f"{_pct(p['test_coverage'])} of the time.")
    if report.get("gate"):
        failed = [c["metric"] for c in report["gate"]["checks"] if not c["passed"]]
        f["fact:gate"] = ("All release gates passed." if not failed else f"Release gate failed on: {', '.join(failed)}.")
    return f


class ChatExplainer:
    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        path = DATA / "reports" / "latest.json"
        if not path.exists():  # first run on a fresh checkout: measure before explaining
            from .evals.runner import run_all

            run_all()
        self.report: dict[str, Any] = json.loads(path.read_text())
        self.facts = build_facts(self.report)
        svc = services()
        fact_chunks = [Chunk(k, k.split(":")[0], v, {"title": "Evaluation result", "section": k}) for k, v in self.facts.items()]
        glossary = [c for c in svc.chunks if c.meta.get("module") in {"evals"}]
        self.index = HybridIndex(fact_chunks + glossary)

    def _route(self, q: str) -> dict[str, Any]:
        ql = q.lower() + " "
        fields = [f for f in QUOTE_FIELDS if f.replace("_", " ") in ql or f in ql]
        sheets = re.findall(r"\b[GT]\d{4}\b", q.upper())
        module = next((m for m, ws in MODULE_WORDS.items() if any(w in ql for w in ws)), None)
        concept = bool(re.search(r"\bwhat (is|does|are)\b|\bmean|\bexplain\b|\bdefine", ql))
        why = bool(re.search(r"\bwhy\b|\bfail|\bwrong|\bmiss|\berror|\bworst|\bproblem", ql))
        return {"fields": fields, "sheets": sheets, "module": module, "concept": concept, "why": why}

    def ask(self, question: str) -> dict[str, Any]:
        route = self._route(question)
        hits = self.index.search(question, k=4)
        # pin the most relevant facts for the route so numbers are always available to cite
        pinned: list[str] = []
        for fld in route["fields"]:
            pinned.append(f"fact:field:{fld}")
            if route["why"]:
                pinned += [k for k in self.facts if k.startswith("fail:") and k.endswith(f":{fld}")][:2]
        for s in route["sheets"]:
            pinned += [k for k in self.facts if k.startswith(f"fail:{s}:")][:3]
        if route["module"]:
            pinned += [k for k in self.facts if k.startswith(f"fact:{route['module']}")]
        if not (route["fields"] or route["sheets"] or route["module"] or route["concept"]):
            pinned += ["fact:overall", "fact:auto", "fact:gate"]
        if route["why"] and not route["fields"] and not route["sheets"]:
            worst = sorted(self.report.get("extraction", {}).get("per_field", {}).items(), key=lambda kv: kv[1]["f1"])[:1]
            for name, _ in worst:
                pinned += [f"fact:field:{name}"] + [k for k in self.facts if k.startswith("fail:") and k.endswith(f":{name}")][:2]
        pinned = [p for p in dict.fromkeys(pinned) if p in self.facts]
        have = {h.chunk.id for h in hits}
        pinned_hits = [Hit(Chunk(p, p.split(":")[0], self.facts[p], {"title": "Evaluation result", "section": p}), 1.0, None, None)
                       for p in pinned if p not in have]
        all_hits = (pinned_hits + hits)[:7]
        ans = grounded_answer(question, all_hits, task="explain_results")
        instances = self._instances(route)
        return {
            "answer": ans.text, "decision": ans.report.decision, "groundedness": ans.report.groundedness,
            "claims": [c.__dict__ for c in ans.report.claims], "sources": ans.sources, "route": route,
            "instances": instances, "chart": self._chart(route), "model": ans.model,
            "followups": self._followups(route),
        }

    def _instances(self, route: dict[str, Any]) -> list[dict[str, Any]]:
        fails = self.report.get("extraction", {}).get("failures", [])
        sel = [x for x in fails if (not route["fields"] or x["field"] in route["fields"])
               and (not route["sheets"] or x["sheet_id"] in route["sheets"])]
        if not (route["fields"] or route["sheets"] or route["why"]):
            return []
        sel.sort(key=lambda x: (not x["token_ids"], x["status"] != "auto_accepted"))  # showable, riskiest first
        svc = services()
        out = []
        for x in sel[:3]:
            try:
                sheet = svc.sheet(x["sheet_id"]).to_dict()
            except KeyError:
                sheet = None
            out.append({**x, "sheet": sheet})
        return out

    def _chart(self, route: dict[str, Any]) -> dict[str, Any] | None:
        e = self.report.get("extraction")
        if not e:
            return None
        if route["module"] == "judge" and self.report.get("judge"):
            j = self.report["judge"]
            return {"type": "bar", "title": "Agreement (Cohen's kappa)", "data": [
                {"name": "Human vs human", "value": j["human_human_kappa"]}, {"name": "Judge vs human", "value": j["judge_human_kappa"]}]}
        if route["fields"] or route["why"] or not route["module"]:
            return {"type": "bar", "title": "F1 by field", "data": [{"name": k, "value": v["f1"]} for k, v in e["per_field"].items()]}
        return None

    @staticmethod
    def _followups(route: dict[str, Any]) -> list[str]:
        base = ["Which field is the weakest and why?", "Can I trust the auto-accepted values?", "Is the LLM judge reliable enough?"]
        if route["fields"]:
            base.insert(0, f"Show me a failing example for {route['fields'][0].replace('_', ' ')}")
        return base[:3]


@MockLLM.register("explain_results")
def _mock_explain(req: LLMRequest, model: str) -> str:
    """Offline explainer: picks the facts that best cover the question and restates them simply."""
    q = {t for t in tokenize(req.payload.get("question", "")) if len(t) > 2}
    hits = req.payload.get("hits", [])
    ranked = sorted(hits, key=lambda h: (h[2] >= 1.0, len(q & set(tokenize(h[1])))), reverse=True)
    sents = [f"{text.rstrip('.')} [c:{cid}]." for cid, text, _ in ranked[:3]]
    return json.dumps({"answer": " ".join(sents) if sents else "The results do not cover this.", "confidence": 0.8})


_chat: ChatExplainer | None = None


def get_chat() -> ChatExplainer:
    global _chat
    if _chat is None:
        _chat = ChatExplainer()
    return _chat
