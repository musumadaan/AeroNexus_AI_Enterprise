"""Train the models that use real public data (run scripts/fetch_public_data.py first).

  python scripts/train_public_models.py            # C-MAPSS RUL (4 subsets, ~3-4 min CPU) + NTSB safety (~10 s)
  python scripts/train_public_models.py --only safety

Run as a script (not `python -m aeronexus.pdm.cmapss`) so pickled classes resolve to their module path.
"""
import argparse
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from aeronexus.pdm import cmapss  # noqa: E402
from aeronexus.safety import ntsb  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--only", choices=["pdm", "safety"], default=None)
a = ap.parse_args()
if a.only in (None, "safety"):
    r = ntsb.train()
    print("safety:", {k: r["metrics"][k] for k in ("test_auroc", "test_pr_auc", "test_f1")})
if a.only in (None, "pdm"):
    cmapss.train_all()
