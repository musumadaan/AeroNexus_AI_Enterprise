"""Hybrid retrieval: BM25 + dense vectors fused with Reciprocal Rank Fusion, then reranked.

Techniques (each one addresses a known RAG failure):
  * Contextual chunk headers — every chunk is prefixed with "doc title > section" before indexing,
    so a chunk like "Use 3 mm minimum" still retrieves for "minimum thickness for culvert arch".
  * Hybrid lexical + dense — BM25 nails part numbers / standards codes ("ASTM A761") that dense
    embeddings blur; dense catches paraphrase. RRF fuses ranks without score calibration.
  * Metadata filters — restrict by doc type / module before scoring (cheaper + safer).
  * Rerank + MMR — a cross-scoring pass reorders the top-k, MMR removes near-duplicates so the
    context window is not wasted on five copies of the same paragraph.
  * Injection screening — retrieved text that tries to instruct the model is quarantined.
  * Tiny knowledge graph — part → material → standard edges for multi-hop questions.

Dense embeddings default to a hashing vectorizer (zero deps, fast, deterministic). Set
AERONEXUS_USE_ST=1 to use sentence-transformers (e.g. bge-small) — same interface.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .config import settings
from .extraction.regex_rules import looks_like_injection
from .observability import tracer

_TOKEN = re.compile(r"[a-z0-9]+(?:[.\-/][a-z0-9]+)*")
STOP = set("a an the of to in on for and or with by is are be as at from that this it its into per than".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOP]


@dataclass
class Chunk:
    id: str
    doc_id: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def indexed_text(self) -> str:  # contextual header
        return f"{self.meta.get('title', self.doc_id)} > {self.meta.get('section', '')}\n{self.text}"


def chunk_document(doc_id: str, text: str, meta: dict[str, Any], max_words: int = 120, overlap: int = 20) -> list[Chunk]:
    """Section-aware chunking: split on markdown headings first, then on a sliding word window."""
    chunks: list[Chunk] = []
    sections = re.split(r"\n(?=#+ )", text.strip())
    for si, sec in enumerate(sections):
        lines = sec.strip().splitlines()
        heading = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("#") else meta.get("title", doc_id)
        body = " ".join(lines[1:] if lines and lines[0].startswith("#") else lines)
        words = body.split()
        if not words:
            continue
        step = max(1, max_words - overlap)
        for wi in range(0, len(words), step):
            piece = " ".join(words[wi: wi + max_words])
            cid = f"{doc_id}#s{si}c{wi // step}"
            chunks.append(Chunk(cid, doc_id, piece, {**meta, "section": heading}))
            if wi + max_words >= len(words):
                break
    return chunks


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = docs
        self.N = len(docs)
        self.avgdl = sum(map(len, docs)) / max(1, self.N)
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}
        self.tf = [Counter(d) for d in docs]

    def scores(self, q: list[str]) -> np.ndarray:
        out = np.zeros(self.N)
        for i, (tf, d) in enumerate(zip(self.tf, self.docs)):
            dl = len(d)
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            out[i] = s
        return out


class HashingEmbedder:
    """Signed feature hashing over word unigrams + char 4-grams, L2-normalised. 512 dims."""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _feats(self, text: str) -> Iterable[str]:
        toks = tokenize(text)
        yield from toks
        for t in toks:
            p = f"#{t}#"
            for i in range(len(p) - 3):
                yield p[i: i + 4]

    def encode(self, texts: list[str]) -> np.ndarray:
        M = np.zeros((len(texts), self.dim), dtype=np.float32)
        for r, text in enumerate(texts):
            for f in self._feats(text):
                h = int(hashlib.md5(f.encode()).hexdigest(), 16)
                M[r, h % self.dim] += 1.0 if (h >> 64) & 1 else -1.0
        n = np.linalg.norm(M, axis=1, keepdims=True)
        return M / np.maximum(n, 1e-9)


class STEmbedder:  # pragma: no cover - optional
    def __init__(self, name: str = "BAAI/bge-small-en-v1.5") -> None:
        from sentence_transformers import SentenceTransformer

        self.m = SentenceTransformer(name)

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self.m.encode(texts, normalize_embeddings=True), dtype=np.float32)


@dataclass
class Hit:
    chunk: Chunk
    score: float
    bm25_rank: int | None
    dense_rank: int | None
    quarantined: bool = False


class HybridIndex:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self.embedder = STEmbedder() if settings.use_st_embeddings else HashingEmbedder()
        with tracer.span("rag.index", n=len(chunks)):
            self.bm25 = BM25([tokenize(c.indexed_text) for c in chunks])
            self.vecs = self.embedder.encode([c.indexed_text for c in chunks]) if chunks else np.zeros((0, 1))

    def in_force(self, as_of: str | None = None) -> set[str]:
        """Revision-aware: doc ids that are effective on `as_of` and not superseded by a newer revision."""
        as_of = as_of or "9999-12-31"
        docs = {c.doc_id: c.meta for c in self.chunks}
        effective = {d for d, m in docs.items() if str(m.get("effective", "0000-01-01")) <= as_of}
        superseded = {m["supersedes"] for d, m in docs.items() if d in effective and m.get("supersedes")}
        return effective - superseded

    def search(self, query: str, k: int = 5, filters: dict[str, Any] | None = None, rrf_k: int = 60,
               mmr_lambda: float = 0.75, as_of: str | None = None, include_superseded: bool = False) -> list[Hit]:
        with tracer.span("rag.search"):
            live = None if include_superseded else self.in_force(as_of)
            allowed = np.array([all(c.meta.get(kk) == vv for kk, vv in (filters or {}).items())
                                and (live is None or c.doc_id in live) for c in self.chunks])
            if not allowed.any():
                return []
            bm = self.bm25.scores(tokenize(query))
            qv = self.embedder.encode([query])[0]
            dense = self.vecs @ qv
            bm[~allowed], dense[~allowed] = -np.inf, -np.inf
            n_cand = min(int(allowed.sum()), max(20, 4 * k))
            bm_order = [i for i in np.argsort(-bm)[:n_cand] if bm[i] > 0]
            de_order = list(np.argsort(-dense)[:n_cand])
            fused: dict[int, float] = defaultdict(float)
            for r, i in enumerate(bm_order):
                fused[i] += 1 / (rrf_k + r + 1)
            for r, i in enumerate(de_order):
                fused[i] += 1 / (rrf_k + r + 1)
            cand = sorted(fused, key=fused.get, reverse=True)[: 3 * k]
            # rerank: query-term coverage (exact codes count double) blended with fused score
            qt = set(tokenize(query))
            codes = {t for t in qt if re.search(r"\d", t)}

            def rerank(i: int) -> float:
                ct = set(tokenize(self.chunks[i].indexed_text))
                cov = len(qt & ct) / max(1, len(qt))
                code_hit = len(codes & ct) / max(1, len(codes)) if codes else 0
                return 0.5 * fused[i] * 60 + 0.35 * cov + 0.15 * code_hit

            cand.sort(key=rerank, reverse=True)
            # MMR diversity
            chosen: list[int] = []
            while cand and len(chosen) < k:
                def mmr(i: int) -> float:
                    red = max((float(self.vecs[i] @ self.vecs[j]) for j in chosen), default=0.0)
                    return mmr_lambda * rerank(i) - (1 - mmr_lambda) * red

                best = max(cand, key=mmr)
                chosen.append(best)
                cand.remove(best)
            bm_rank = {i: r for r, i in enumerate(bm_order)}
            de_rank = {i: r for r, i in enumerate(de_order)}
            hits = []
            for i in chosen:
                c = self.chunks[i]
                hits.append(Hit(c, round(rerank(i), 4), bm_rank.get(i), de_rank.get(i), looks_like_injection(c.text)))
            if any(h.quarantined for h in hits):
                tracer.incr("rag.quarantined")
            return [h for h in hits if not h.quarantined]


class KnowledgeGraph:
    """Adjacency list of typed edges: (subject, relation, object)."""

    def __init__(self) -> None:
        self.edges: dict[str, list[tuple[str, str]]] = defaultdict(list)

    def add(self, s: str, rel: str, o: str) -> None:
        self.edges[s.upper()].append((rel, o.upper()))

    def neighbors(self, s: str, depth: int = 2) -> list[tuple[str, str, str]]:
        out, frontier, seen = [], [s.upper()], {s.upper()}
        for _ in range(depth):
            nxt = []
            for node in frontier:
                for rel, o in self.edges.get(node, []):
                    out.append((node, rel, o))
                    if o not in seen:
                        seen.add(o)
                        nxt.append(o)
            frontier = nxt
        return out


def load_kb(kb_dir: Path) -> tuple[HybridIndex, list[Chunk]]:
    chunks: list[Chunk] = []
    for p in sorted(kb_dir.glob("*.md")):
        raw = p.read_text()
        meta: dict[str, Any] = {"title": p.stem.replace("_", " ").title(), "source": p.name}
        m = re.match(r"---\n(.*?)\n---\n", raw, re.S)
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            raw = raw[m.end():]
        chunks.extend(chunk_document(p.stem, raw, meta))
    return HybridIndex(chunks), chunks


def load_graph(path: Path) -> KnowledgeGraph:
    g = KnowledgeGraph()
    if path.exists():
        for s, r, o in json.loads(path.read_text()):
            g.add(s, r, o)
    return g


def contradictions(hits: list[Hit], min_overlap: float = 0.6) -> list[dict[str, Any]]:
    """Sentences from *different* documents that talk about the same thing (high content overlap) but
    state different numbers — e.g. two policy revisions with different tolerances."""
    from .grounding import _numbers, split_sentences

    sents = [(h.chunk.doc_id, h.chunk.id, s, h.chunk.meta) for h in hits for s in split_sentences(h.chunk.text)]
    out = []
    for i in range(len(sents)):
        for j in range(i + 1, len(sents)):
            (da, ca, sa, ma), (db, cb, sb, mb) = sents[i], sents[j]
            if da == db:
                continue
            ta, tb = set(tokenize(sa)) - {n for n in _numbers(sa)}, set(tokenize(sb)) - {n for n in _numbers(sb)}
            if not ta or not tb:
                continue
            overlap = len(ta & tb) / min(len(ta), len(tb))
            na, nb = _numbers(sa), _numbers(sb)
            if overlap >= min_overlap and na and nb and na != nb:
                newer = ca if str(ma.get("effective", "")) >= str(mb.get("effective", "")) else cb
                out.append({"a": {"chunk": ca, "text": sa, "revision": ma.get("revision"), "effective": ma.get("effective")},
                            "b": {"chunk": cb, "text": sb, "revision": mb.get("revision"), "effective": mb.get("effective")},
                            "overlap": round(overlap, 3), "prefer": newer})
    return out
