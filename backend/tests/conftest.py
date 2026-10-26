import os
import sys
from pathlib import Path

os.environ.setdefault("AERONEXUS_STATE_DB", ":memory:")  # tests never touch the durable state file
# Tests are offline and deterministic even if backend/.env selects Groq: never spend free-tier quota in CI.
os.environ["AERONEXUS_LLM_PROVIDER"] = "mock"
os.environ["AERONEXUS_JUDGE_PROVIDER"] = "mock"
for k in ("AERONEXUS_SMALL_MODEL", "AERONEXUS_LARGE_MODEL", "AERONEXUS_JUDGE_MODEL"):
    os.environ.pop(k, None)

import tempfile

_tmp = tempfile.mkdtemp(prefix="aeronexus-test-")
for _k in ("AERONEXUS_RUNS_DIR", "AERONEXUS_WORKFLOWS_DIR", "AERONEXUS_HISTORY_DIR"):
    os.environ[_k] = os.path.join(_tmp, _k.lower())  # runs, saved workflows and eval history never touch data/
os.environ["AERONEXUS_MLFLOW"] = "off"
os.environ["AERONEXUS_AUDIT_PATH"] = os.path.join(_tmp, "audit.jsonl")
os.environ["AERONEXUS_TRACKING_CSV"] = os.path.join(_tmp, "tracking.csv")
for _k in ("GOOGLE_SHEETS_SPREADSHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON_B64", "GOOGLE_APPLICATION_CREDENTIALS"):
    os.environ.pop(_k, None)  # tests never call the real Google API

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def fresh_state():
    """Each test gets fresh singletons (approval queue, caches) so tests are order-independent."""
    import aeronexus.llm as llm
    from aeronexus.observability import tracer
    from aeronexus.services import reset_services

    reset_services()
    llm._router = None
    llm._judge = None
    tracer.reset()
    yield
