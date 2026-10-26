# Enterprise integration layer

An optional, feature-flagged layer on top of AeroNexus. With `AERONEXUS_FF_INTEGRATIONS=0` the app behaves exactly as
before; CI runs the full regression suite and the evaluation gate in both modes to prove it.

## What is included, and what it costs

| Capability | Status | Cost |
|---|---|---|
| MCP server (`aeronexus.integrations.mcp_server`) | built | free (open-source SDK) |
| SAP procurement connector | local mock with SAP field names (purchase requisitions, POs, suppliers, materials) | free |
| Google Sheets tracking | real Sheets API, or a local CSV when not configured | free (Google account + service account) |
| Human-in-the-loop approvals | approval queue in Mission Control; every write waits for a named approver | free |
| Audit log | tamper-evident (SHA-256 hash chain), secrets redacted | free |
| Productivity analytics | approvals, turnaround, actions per connector, estimated minutes saved | free |
| Salesforce | local mock, or a real **Developer Edition** org | free |
| Gemini API (Google AI Studio) | LLM provider `gemini` | free tier (rate-limited; free-tier prompts may be used to improve Google products) |
| Vertex AI | LLM provider `vertex` | billing account normally required: optional |
| PySpark / Databricks | PySpark feature job + Databricks notebook + Jobs API trigger | free (local Spark, or Databricks Free Edition, non-commercial) |
| GitHub Actions CI/CD | tests in both flag modes, eval gate, PySpark parity, gitleaks, gated Railway deploy | free (public repos; free minutes on private) |
| Railway | one-service deploy of API + UI | paid usage (your account) |
| SAP S/4HANA writes, AppSheet | not implemented | paid |

## Where the code lives (separate modules; minimal hooks into the core)

```
backend/aeronexus/integrations/
  flags.py        feature flags + the connector catalogue shown in the UI
  sap_mock.py     mock SAP procurement + agent tools (reads free; writes need approval)
  sheets.py       Google Sheets tracking (background writer, retries, CSV fallback)
  audit.py        hash-chained audit log + productivity analytics
  salesforce.py   mock / Developer Edition connector
  providers.py    Gemini (AI Studio) and Vertex AI providers for the existing LLM router
  secrets.py      env or *_FILE secrets; reports configured/not configured only
  nodes.py        workflow-builder steps + the "Inspection → SAP → Sheets" template
  mcp_server.py   MCP tools over the REST API (stdio or streamable HTTP)
  api.py          /api/integrations/*, /api/sap/*, /api/salesforce/*
backend/aeronexus/pipelines/   spark_features.py (PySpark), databricks.py (Jobs API)
backend/aeronexus/static_site.py   serve frontend/dist from FastAPI (single-service deploys)
frontend/src/components/EnterpriseConsole.tsx   mounted at the bottom of Mission Control
notebooks/databricks/cmapss_feature_pipeline.py
Dockerfile.railway, railway.json, .github/workflows/ci.yml
```

Hooks in existing files, each a few lines:
- `agents.py`: runtime listeners and `register_tools()`;
- `services.py`: `attach()`;
- `api.py`: mounts the router, plus the optional static site;
- `llm.py` and `config.py`: optional providers;
- `evals/runner.py`: pauses tracking during evaluations;
- `Mission.tsx`: one `<EnterpriseConsole />`.

## Set-up, step by step (all free)

### 1. MCP (Claude Desktop, Claude Code or any MCP client)
1. Start the API: `cd backend && python -m uvicorn aeronexus.api:app --port 8000`.
2. Add the server to your MCP client config (Mission Control › Integration settings shows it, ready to copy):
   ```json
   {"mcpServers": {"aeronexus": {"command": "python", "args": ["-m", "aeronexus.integrations.mcp_server"],
     "cwd": "/path/to/aeronexus-ai/backend", "env": {"AERONEXUS_API_URL": "http://localhost:8000"}}}}
   ```
3. Ask the client to "request 2 fan blades for FD001 engine 20". The request appears in the approval queue; approve it as an
   approver in Mission Control.

MCP clients always act as `engineer` at most, so they can request but never approve. For HTTP:
`python -m aeronexus.integrations.mcp_server --http --port 8765`.

### 2. Google Sheets tracking
1. Go to console.cloud.google.com and create a project (no billing needed). Enable the **Google Sheets API**.
2. Under IAM › Service accounts, create one and add a JSON key. Encode the key with `base64 -w0 key.json`; on a Mac use
   `base64 -i key.json`.
3. Create a Google Sheet with a tab called `Tracking`, and share it with the service account's email as Editor.
4. In `backend/.env`, set `GOOGLE_SHEETS_SPREADSHEET_ID=<id from the URL>` and
   `GOOGLE_SERVICE_ACCOUNT_JSON_B64=<the base64>`.
5. In Mission Control › Integration settings, click **Send test row**.

### 3. Free Gemini instead of (or as well as) Groq
Get a key at aistudio.google.com, then set `AERONEXUS_LLM_PROVIDER=gemini` and `GEMINI_API_KEY=...`. The default models are
`gemini-3.5-flash-lite` (small) and `gemini-3.6-flash` (large); override them with `AERONEXUS_SMALL_MODEL` and
`AERONEXUS_LARGE_MODEL`. Don't send confidential data on the free tier.

### 4. Salesforce Developer Edition (optional)
1. Sign up free at developer.salesforce.com.
2. Create a Connected App with the client-credentials flow.
3. Set `AERONEXUS_FF_SALESFORCE=live`, `SALESFORCE_DOMAIN_URL`, `SALESFORCE_CLIENT_ID` and `SALESFORCE_CLIENT_SECRET`.

`AERONEXUS_FF_SALESFORCE=mock` needs nothing.

### 5. Databricks Free Edition (optional)
1. Sign up for Databricks Free Edition.
2. Upload `backend/data/public/cmapss/*.txt` to a volume.
3. Import `notebooks/databricks/cmapss_feature_pipeline.py` and run it on serverless compute. It writes a Delta table.
4. To trigger it from AeroNexus, create a Job from the notebook, then set `AERONEXUS_FF_DATABRICKS=1`, `DATABRICKS_HOST`,
   `DATABRICKS_TOKEN` and `DATABRICKS_JOB_ID`.

To run it locally instead: `pip install -r requirements-spark.txt` (needs Java 17+), then
`python -m aeronexus.pipelines.spark_features --subset FD001`.

### 6. GitHub Actions + Railway (deploy only after the checks pass)
1. Push the branch and open a pull request. CI runs these jobs:
   - gitleaks;
   - backend tests, once with the layer on and once with it off;
   - the evaluation gate;
   - an MCP launch check;
   - the PySpark parity test;
   - the frontend typecheck, tests and build.
2. In Railway, create a project from the GitHub repo. It uses `railway.json` and `Dockerfile.railway`.
3. Add a volume mounted at `/app/state` and copy the variables from `backend/.env` into Railway › Variables. Never
   commit `.env`.
4. In Railway, create a project token. In GitHub, add the repository secret `RAILWAY_TOKEN`, the variable
   `RAILWAY_SERVICE` (your service name) and, optionally, `RAILWAY_PUBLIC_URL` for the post-deploy smoke test.
5. Merge to `main`. The `deploy-railway` job runs only after every check is green, then smoke-tests `/health` and the
   integration status.

## Security notes
- Writes are WRITE or EXTERNAL tools in the agent runtime. They wait for a named human approver; agents and MCP clients
  can't approve.
- The audit log is hash-chained (`/api/integrations/audit` verifies it) and redacts anything that looks like a credential.
- Secrets come only from the environment or `*_FILE` mounts. The status endpoint reports configured or not configured,
  never values.
- Test runs and evaluations never write to the live mock SAP, the audit log or Sheets.
