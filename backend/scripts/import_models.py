"""Bring models trained in Google Colab into this repo, then test them locally.

  python scripts/import_models.py ~/Downloads/aeronexus_export.zip          # or the unzipped folder
  python scripts/import_models.py ~/Downloads/aeronexus_export.zip --verify # + run tests and the eval gate

What it does
  1. Checks every file against the SHA-256 in manifest.json (written by the notebook's export cell),
     so a truncated download is caught instead of half-loaded.
  2. Warns if Colab's scikit-learn / numpy versions differ from yours. joblib/pickle files are only
     reliably loadable with the same versions: `pip install scikit-learn==<colab version>` if warned.
  3. Copies each artifact to where the backend loads it (table below) and backs up anything it replaces
     to models/_backup/<timestamp>/.
  4. Smoke-loads every joblib model; with --verify also runs pytest and the 32-check regression gate.

Security: joblib/pickle files can run code when loaded. Only import exports you produced yourself.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))  # pickled RUL bundles reference aeronexus.pdm classes
MODELS, REPORTS = BACKEND / "models", BACKEND / "data" / "reports"

# exported name (file or folder) → destination, and what in the app uses it
ROUTES = [
    ("pdm_FD00*.joblib", MODELS, "Predictive maintenance API (RUL + health)"),
    ("safety_ntsb.joblib", MODELS, "Safety triage API"),
    ("price_model.joblib", MODELS, "Drawing-to-quote pricing"),
    ("detector.pt", MODELS, "Layout detector (needs `pip install ultralytics`)"),
    ("ner", MODELS, "NER (set AERONEXUS_USE_HF_NER=1, needs `pip install transformers torch`)"),
    ("vlm-lora", MODELS, "Drawing VLM adapter (serve with vLLM; not loaded by the API directly)"),
    ("safety-transformer", MODELS, "Transformer safety model (comparison; API serves the TF-IDF model)"),
    ("pdm_cmapss.json", REPORTS, "RUL metrics + fleet shown in the UI"),
    ("safety_ntsb.json", REPORTS, "Safety metrics, themes and trends shown in the UI"),
    ("rul_*.keras", MODELS / "deep", "Deep RUL models (comparison; needs keras)"),
    ("rul_deep_results.json", MODELS / "deep", "Deep vs gradient-boosted comparison"),
]


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def unpack(src: Path) -> Path:
    if src.is_dir():
        return src
    if not zipfile.is_zipfile(src):
        sys.exit(f"{src} is neither a folder nor a zip")
    out = Path(tempfile.mkdtemp(prefix="aeronexus_import_"))
    with zipfile.ZipFile(src) as z:
        for m in z.namelist():  # refuse path traversal in the archive
            if m.startswith("/") or ".." in Path(m).parts:
                sys.exit(f"unsafe path in zip: {m}")
        z.extractall(out)
    inner = [p for p in out.iterdir() if p.is_dir()]
    return inner[0] if len(inner) == 1 and not (out / "manifest.json").exists() else out


def check_manifest(root: Path) -> dict:
    mp = root / "manifest.json"
    if not mp.exists():
        print("! no manifest.json — skipping checksum verification (export with the notebook's last cell next time)")
        return {}
    man = json.loads(mp.read_text())
    bad = [rel for rel, digest in man.get("artifacts", {}).items() if not (root / rel).exists() or sha(root / rel) != digest]
    if bad:
        sys.exit(f"checksum mismatch / missing (re-download): {bad[:5]}")
    print(f"✓ {len(man.get('artifacts', {}))} files match manifest (exported {man.get('exported_at', '?')})")
    try:
        import numpy
        import sklearn

        local = {"scikit-learn": sklearn.__version__, "numpy": numpy.__version__}
        for k, v in man.get("versions", {}).items():
            if local.get(k) and local[k] != v:
                print(f"! version mismatch: Colab {k}=={v}, local {k}=={local[k]}. If loading fails: pip install {k}=={v}")
    except ImportError:
        pass
    return man


def install(root: Path) -> list[tuple[str, Path, str]]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    done = []
    for pattern, dest, use in ROUTES:
        for item in sorted(root.glob(pattern)):
            target = dest / item.name
            dest.mkdir(parents=True, exist_ok=True)
            if target.exists():  # keep the previous version so a bad import can be rolled back
                bk = MODELS / "_backup" / stamp
                bk.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), bk / item.name)
            (shutil.copytree if item.is_dir() else shutil.copy2)(item, target)
            done.append((item.name, target, use))
    return done


def smoke_load(done: list[tuple[str, Path, str]]) -> bool:
    import joblib

    ok = True
    for name, target, _ in done:
        if target.suffix == ".joblib":
            try:
                b = joblib.load(target)
                m = b.get("metrics", {}) if isinstance(b, dict) else {}
                headline = {k: m[k] for k in ("test_rmse_capped", "test_auroc", "test_mape") if k in m}
                print(f"✓ loads: {name} {headline}")
            except Exception as e:  # usually a scikit-learn version mismatch
                ok = False
                print(f"✗ cannot load {name}: {type(e).__name__}: {e}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("export", type=Path, help="aeronexus_export.zip from Google Drive, or the unzipped folder")
    ap.add_argument("--verify", action="store_true", help="run pytest + the evaluation gate afterwards")
    a = ap.parse_args()
    root = unpack(a.export.expanduser())
    check_manifest(root)
    done = install(root)
    if not done:
        sys.exit("nothing recognised in the export — expected files like pdm_FD001.joblib, safety_ntsb.joblib, ner/")
    for name, target, use in done:
        print(f"  {name:<28} → {target.relative_to(BACKEND)}   ({use})")
    if not smoke_load(done):
        return 1
    if a.verify:
        env = {"AERONEXUS_STATE_DB": ":memory:"}
        import os

        env = {**os.environ, **env}
        r1 = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=BACKEND, env=env)
        r2 = subprocess.run([sys.executable, "-m", "aeronexus.evals.runner"], cwd=BACKEND, env=env)
        return r1.returncode or r2.returncode
    print("\nNext: python -m aeronexus.evals.runner   (the gate decides if these models ship)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
