"""Process-wide singletons (lazy). In prod these become injected dependencies; the API layer
only ever talks to `services()`, so swapping in-memory stores for Postgres/pgvector/ERPNext is a
change in one place."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from .agents import AgentRuntime
from .cases import CaseStore
from .config import DATA, settings
from .extraction.document import Sheet, load_sheets
from .extraction.pipeline import ExtractionPipeline
from .rag import HybridIndex, KnowledgeGraph, load_graph, load_kb


class Services:
    def __init__(self) -> None:
        self.index, self.chunks = load_kb(DATA / "kb")
        self.graph: KnowledgeGraph = load_graph(DATA / "graph.json")
        self.runtime = AgentRuntime()
        from .integrations import attach  # audit log + Sheets tracking for the live runtime (feature-flagged)

        attach(self.runtime)
        self.cases = CaseStore(settings.state_db)
        self.pipeline = ExtractionPipeline()
        self.golden_sheets: list[Sheet] = load_sheets(DATA / "golden" / "drawings.jsonl")
        self.mro: dict[str, Any] = json.loads((DATA / "golden" / "mro.json").read_text())
        self.ap: dict[str, Any] = json.loads((DATA / "golden" / "ap.json").read_text())
        self.change_pairs = [json.loads(l) for l in (DATA / "golden" / "change_pairs.jsonl").read_text().splitlines() if l]
        self.results: dict[str, Any] = {}  # last module outputs (for the chatbot)
        self.uploads: dict[str, Sheet] = {}  # user uploads are kept apart: the golden set stays frozen

    def sheet(self, sheet_id: str) -> Sheet:
        if sheet_id in self.uploads:
            return self.uploads[sheet_id]
        for s in self.golden_sheets:
            if s.id == sheet_id:
                return s
        raise KeyError(sheet_id)


@lru_cache(maxsize=1)
def services() -> Services:
    return Services()


def reset_services() -> None:
    services.cache_clear()
