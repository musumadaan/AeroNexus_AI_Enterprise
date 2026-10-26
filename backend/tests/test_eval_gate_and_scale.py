"""The CI regression gate + scale/latency tests."""
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from aeronexus.config import DATA
from aeronexus.evals.runner import run_all
from aeronexus.extraction.document import load_sheets
from aeronexus.extraction.pipeline import ExtractionPipeline


def test_regression_gate_passes():
    rep = run_all(write=False)
    failed = [c for c in rep["gate"]["checks"] if not c["passed"]]
    assert not failed, failed


def test_throughput_and_p95_on_training_corpus():
    sheets = load_sheets(DATA / "train" / "drawings.jsonl")[:300]
    pipe = ExtractionPipeline()
    lat = []
    t0 = time.perf_counter()
    for s in sheets:
        a = time.perf_counter()
        pipe.run(s)
        lat.append((time.perf_counter() - a) * 1000)
    total = time.perf_counter() - t0
    lat.sort()
    assert lat[int(0.95 * len(lat))] < 250          # ms per sheet on CPU, rules path
    assert len(sheets) / total > 20                  # sheets per second, single worker


def test_concurrent_runs_are_deterministic():
    sheets = load_sheets(DATA / "train" / "drawings.jsonl")[:40]
    pipe = ExtractionPipeline()
    serial = [{k: v.value for k, v in pipe.run(s).fields.items()} for s in sheets]
    with ThreadPoolExecutor(max_workers=8) as ex:
        parallel = list(ex.map(lambda s: {k: v.value for k, v in pipe.run(s).fields.items()}, sheets))
    assert serial == parallel
