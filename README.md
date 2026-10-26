# AeroNexus AI

**An aerospace engineering intelligence platform that turns drawings, sensor data, maintenance records, safety reports and
supplier documents into evidence-backed engineering insights and traceable business actions.**

AeroNexus combines technical-drawing intelligence, predictive maintenance, aviation safety analytics, engineering change
management and workflow automation. It uses multimodal extraction, machine learning trained on **open public aviation
datasets**, retrieval-augmented generation, a deterministic calculation engine and human-in-the-loop review. Every number
can be re-computed from a receipt, and every case can be replayed from its event log. Nothing is released without a named
approver.

| Workflow | Data it learns from | What it does |
|---|---|---|
| **Engineering change orders** (flagship) | Synthetic revision pairs + ERP/CRM seed | Compares two drawing revisions field by field and finds affected assemblies, orders, work orders, material POs, suppliers and customers. Computes cost and lead time with versioned formulas and produces an auditable ECR/ECO. |
| **Predictive maintenance** | **NASA C-MAPSS** (public, all 4 subsets) | Remaining useful life with a conformal 90% range, plus an engine health score. A versioned rule proposes inspection work orders for a technician to approve. |
| **Safety analytics** | **NTSB accident database** (public, 17,485 probable-cause statements) | Severity triage with abstention, similar-event retrieval and cause themes and trends. ASRS and FAA SDR exports can also be dropped in. |
| Drawing to quote | Synthetic drawings; MechVQA in Colab | Extraction cascade, finish/material checks, price with conformal interval, ERP/CRM drafts. |
| Directive tracker | Synthetic logbooks; MaintNet in Colab | Matches logbook evidence to ADs and works out when each is next due (effective-dated rules). |
| Invoice matching | Synthetic invoices; FATURA/DocILE in Colab | Three-way match with the price tolerance in force on the invoice date. |

### Version 2: twin, agents, automation and observability

| Feature | Built with | What it does |
|---|---|---|
| **3D engine digital twin** | React Three Fiber, Three.js, C-MAPSS models | A turbofan whose seven modules are coloured by condition. Click a module for its sensors and history; replay the engine's life cycle by cycle. |
| **Maintenance simulator** | FastAPI WebSocket, Recharts | Streams an engine's telemetry live, with the RUL uncertainty band, the health score and anomaly detection. Inject spikes, drifts, stuck sensors or noise and watch the models react. |
| **Change-order upgrades** | React Flow, SVG | Animated revision comparison (swipe, blink, side by side), a section view that morphs between dimensions, a dependency graph of every affected record, and a waterfall cost trace with each formula's source. |
| **Mission control** | LangGraph | A supervisor (Llama) plans; drawing, maintenance, safety, evidence and cost agents run in parallel with retries and fallbacks; a synthesizer proposes actions; an `interrupt()` gate waits for a named approver. Live over WebSockets. |
| **Workflow builder** | React Flow, LangGraph | Drag-and-drop automations: 6 triggers, agents, models, OCR, RAG, ERP/CRM/CMMS calls, database steps, conditions and approvals. Each workflow compiles to a LangGraph graph. Includes templates, live runs, test batches on historical data, replay and run comparison. Writes without an approval are refused at save time. |
| **Observability** | MLflow, Langfuse or LangSmith | Model, RAG, citation and extraction quality, latency, tokens and cost, and per-agent reliability. Every evaluation is fingerprinted, logged to MLflow and compared with the previous run. |
| **Learning portal** | In-app slides | Eight short lessons with screenshots, covering every page. |

## Results (latest run — real public data separated from synthetic)

| Suite | Data | Result |
|---|---|---|
| RUL, C-MAPSS FD001 / FD002 / FD003 / FD004 | **Public, real benchmark** (simulated engines) | Test RMSE **11.6 / 12.9 / 13.5 / 13.5** cycles; 90% ranges cover 89% / 90% / 82% / 92%; health AUROC 0.94–0.995 |
| Safety triage, NTSB (train 2008–15, test 2017–22) | **Public, real** | AUROC **0.92**, PR-AUC 0.66 at 12% prevalence, calibration error 0.017; masking outcome words changes AUROC by 0.004 |
| Change orders | Synthetic | Field-change F1 1.0, severity 100%; **100% of receipts re-compute exactly**, 100% of cases identical on replay |
| Drawing extraction | Synthetic (an upper bound) | F1 0.99; auto-accepted values right 99.8% of the time |
| Access control | Tests | 100% of role and tenant-isolation checks |

All **32 release gates** pass (`python -m aeronexus.evals.runner`). Synthetic-data scores are labelled as such in the UI and
in the report, because real scans and real fleets will be harder. The LLM-judge harness correctly **refuses** the
deliberately flawed demo judge as a gate: its agreement with humans is kappa 0.57, against 0.68 between two humans.

## Run it

```bash
cd backend
pip install -r requirements.txt
python scripts/generate_data.py --train 600     # synthetic golden sets + ERP/CRM seed (deterministic)
python scripts/fetch_public_data.py             # NASA C-MAPSS + NTSB, SHA-256 verified
python scripts/train_public_models.py           # RUL (4 subsets, ~3–4 min CPU) + safety model (~10 s)
AERONEXUS_STATE_DB=:memory: pytest -q           # 157 tests
AERONEXUS_STATE_DB=:memory: python -m aeronexus.evals.runner   # 32-check regression gate
uvicorn aeronexus.api:app --reload --port 8000

cd ../frontend && npm install && npm run dev    # http://localhost:3000
```

Or `docker compose up --build` (fetches, trains and gates during the build).

**Roles:** use the switcher at the bottom left: viewer, engineer, approver or admin. Only approvers can sign off, only admins
can run evaluations, and agents can never approve their own actions. In production, replace the demo headers with
OIDC (`aeronexus/security.py`).

## Enterprise integration layer (optional, free)

- What it adds: an MCP server, mock SAP procurement, Google Sheets tracking, human approvals, a hash-chained audit log and productivity analytics.
- Where it appears: the enterprise console at the bottom of Mission Control.
- Optional extras:
  - Salesforce (mock or Developer Edition);
  - Gemini free tier;
  - PySpark and Databricks Free Edition;
  - CI/CD with a Railway deploy that runs only after the checks pass.
- How to switch it off: set `AERONEXUS_FF_INTEGRATIONS=0`.
- Setup: [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md).

## The new pages

| Page | Try this |
|---|---|
| **Learn** (`/learn`) | Start with "Getting started"; use ← → to move and click a picture to enlarge it. |
| **Engine digital twin** (`/twin`) | Pick engine #20 (it is near the end of its life). Click the HPC, toggle *Exploded view*, then drag the history slider back to cycle 1. |
| **Maintenance simulator** (`/simulator`) | Press play, add a *stuck sensor* fault on T50 from cycle 120, and watch the detector feed. Use *Send to workflows* to fire the sensor-anomaly trigger. |
| **Change orders** (`/change`) | Compare CH000, then open the *Compare revisions*, *Dependency graph* and *Cost trace* tabs. |
| **Mission control** (`/mission`) | Launch the first scenario as an approver and approve at the gate. Then launch the *Resilience drill* to see retries and a graceful fallback. |
| **Workflow builder** (`/builder`) | Load a template, click a step to edit it, *Run live*, approve, then *Replay and compare*. With no step selected, *Run test batch* scores the workflow on 12 historical events. |
| **Observability** (`/observability`) | As admin, *Run regression suite*; open the *Regression history* tab and a reproducible report. |

REST and WebSocket endpoints for these pages live in `backend/aeronexus/api_ops.py`: `/api/twin/*`, `/api/mission/*`,
`/api/workflows/*`, `/api/runs/*`, `/api/obs/*`, `/ws/runs/{id}` and `/ws/telemetry`.

### Observability set-up (optional)

```bash
# MLflow: every evaluation run is already logged to backend/data/mlflow.db
uvx mlflow ui --backend-store-uri sqlite:///backend/data/mlflow.db      # or: pipx run mlflow ui ...
# Langfuse: add LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST to backend/.env
# LangSmith: add LANGSMITH_TRACING=true and LANGSMITH_API_KEY to backend/.env
```

Run the MLflow UI in its own environment (`uvx` or `pipx`). The full `mlflow` package pins an older `graphql-core` that
conflicts with Strawberry, so the backend only installs `mlflow-skinny`.

## Free LLM: Llama on Groq (no OpenAI or Claude spend)

```bash
cd backend
cp .env.example .env            # then paste your free key from https://console.groq.com/keys into GROQ_API_KEY
python scripts/check_groq.py    # one tiny call per model: confirms the key, shows latency and remaining quota
uvicorn aeronexus.api:app --reload --port 8000
```

| Role | Default Groq model | Why |
|---|---|---|
| First pass (extraction fallback, grounded answers, chatbot) | `llama-3.1-8b-instant` | Fast and cheap on quota; most work never reaches an LLM anyway (rules → NER first) |
| Escalation (low confidence or invalid JSON) | `llama-3.3-70b-versatile` | Only when the 8B model is unsure |
| LLM judge | `openai/gpt-oss-20b` (open weights, also free on Groq) | A different model family, so the judge isn't grading its own family's answers |

How the system stays inside the free tier:
- **Pacing.** Requests are spaced to about 28 per minute (`AERONEXUS_GROQ_RPM`).
- **Rate limits.** A 429 waits for Groq's `retry-after` and doesn't trip the circuit breaker. A wait longer than 30 s degrades
  to rules, extractive answers and human review instead of hanging.
- **Caching and batching.** Responses are cached, and each drawing makes at most one batched LLM call.
- **No key, no problem.** Without a key the app still runs; LLM-only fields go to review.

Tests and CI always use the offline mock, even if `.env` selects Groq, so they never spend your quota. To evaluate the real
Llama models, run `python -m aeronexus.evals.runner` with your `.env` in place. The 32 gates then measure Llama itself, and
some thresholds may need re-calibrating with your own labels.

The predictive-maintenance and safety models are classic ML trained on public data. They don't use an LLM.

## Train in Colab, then test in VS Code

```
Colab (TPU/GPU)                        Google Drive                         VS Code (your laptop)
run notebook ──► last cell saves ──►  MyDrive/aeronexus/aeronexus_export.zip ──► python scripts/import_models.py <zip> --verify
                 models + reports                                                 checksums → place files → load → tests → gate
                 + manifest.json
```

1. Push this repo to GitHub and set `REPO` in `notebooks/build_notebooks.py` to your fork. Run that script, upload the
   notebook to Colab, and choose the runtime it names (TPU or GPU).
2. Run all cells. The **last cell** mounts Google Drive and writes `MyDrive/aeronexus/aeronexus_export.zip`. The zip holds
   the model files, the metric reports, and a `manifest.json` with SHA-256 checksums and Colab's scikit-learn and numpy
   versions. Running several notebooks adds to the same zip.
3. Download the zip from Drive, then in VS Code:
   ```bash
   cd backend
   python scripts/import_models.py ~/Downloads/aeronexus_export.zip --verify
   ```
   The import script:
   - verifies every checksum, so a truncated download is rejected;
   - warns if your library versions differ from Colab's;
   - backs up the models it replaces to `models/_backup/`;
   - copies each file where the API loads it;
   - test-loads each model, then runs the tests and the 32-check gate.
4. Start the API (`uvicorn aeronexus.api:app --reload`) and the UI. The app now serves the Colab-trained models.

| Notebook | Runtime | What the export contains | Used by the app? |
|---|---|---|---|
| `05_cmapss_rul_tpu` | **TPU** (Keras 3 / JAX) | `pdm_FD00x.joblib` + `pdm_cmapss.json`; deep `rul_*.keras` for comparison | joblib models: yes (predictive maintenance). Keras models: comparison only |
| `06_safety_text` | **TPU** (PyTorch/XLA) or GPU | `safety_ntsb.joblib` + `safety_ntsb.json`; `safety-transformer/` | joblib: yes (safety triage). Transformer: comparison only |
| `01_ner_tpu` | **TPU** or GPU | `ner/` | yes, with `AERONEXUS_USE_HF_NER=1` |
| `04_price_model` | CPU | `price_model.joblib` | yes (pricing) |
| `03_detector` | GPU | `detector.pt` | yes, with `pip install ultralytics` |
| `02_vlm_lora` | GPU (L4/A100) | `vlm-lora/` | serve with vLLM, then point `AERONEXUS_LLM_PROVIDER=openai` at it |
| `07_opensky_trajectories` | CPU | nothing (license: no redistribution) | — |

**Version tip:** pickled scikit-learn models only load reliably with the same scikit-learn version. If the import warns
about a mismatch, run `pip install scikit-learn==<Colab's version>` locally (it's printed for you) and re-run the import.
Only import exports you made yourself, because pickle files can run code when loaded.

You can also skip Colab for 05 and 06's baseline models: `python scripts/train_public_models.py` trains them on a laptop
CPU in about 4 minutes. Use Colab for the TPU and GPU models (deep RUL, transformers, NER, VLM).

## What's inside

```
backend/aeronexus/
  calc.py          units · gauge tables · versioned effective-dated rules · formulas with reproducible receipts
  cases.py         durable SQLite event log · status machine · resumable steps with retry · replay
  security.py      RBAC (viewer/engineer/approver/admin/agent) · tenant context
  public_data.py   dataset registry: source of record, license, terms, pinned SHA-256
  pdm/cmapss.py    regime normalisation · window features · GBT vs MLP · conformal RUL · IsolationForest health
  safety/ntsb.py   masked outcome words · temporal split · calibrated TF-IDF model · NMF themes · rule tags
  modules/         change_order (flagship) · pdm · safety · quote · pricing · mro · ap_match
  extraction/      regex rules · NER · layout · cascade with hallucination guard and cross-checks
  rag.py           hybrid BM25 + dense + RRF + MMR · revision-aware · contradiction detection · quarantine
  grounding.py     per-sentence citation verification · generation.py coverage-based abstention
  agents.py        tools by risk tier (ERP, CRM, CMMS, email, voice) · approvals · idempotency · audit
  evals/           metrics · judge harness · runner + thresholds.json (32 gates)
  twin.py          digital twin + simulator: per-cycle RUL/interval/health, module drift, fault injection, anomaly detectors
  orchestration/   LangGraph mission control · workflow compiler/engine · node library · run records · Langfuse export
  mlops.py         MLflow logging · fingerprints · reproducible reports · regression vs previous · agent reliability
  usage.py         token + cost metering per model
  api_ops.py       REST + WebSocket API for the twin, missions, workflows, runs and observability
frontend/src/      React + TypeScript, indigo design; 3D twin (R3F), simulator, mission control and builder (React Flow), learning portal
notebooks/         Colab training (TPU/GPU) · docs/ architecture, datasets, roadmap, research, responsible AI, demo
```

Docs: [Architecture](docs/ARCHITECTURE.md) · [Datasets & licenses](docs/DATASETS.md) · [Roadmap vs vision](docs/ROADMAP.md) ·
[Research basis](docs/RESEARCH.md) · [Responsible AI](docs/RESPONSIBLE_AI.md) · [Demo script](docs/DEMO_SCRIPT.md)

## Before you publish the name

Two similarly named organisations turned up in a quick web search: a Northeastern University student club called
"Aero Nexus", and a Dubai company called "Aero Nex FZCO". Neither is an aerospace AI product. Before committing publicly,
check:
- the CIPO and USPTO trademark databases for "AeroNexus" in classes 9 and 42;
- the domain;
- the GitHub organisation name.
