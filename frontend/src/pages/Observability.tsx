import { useEffect, useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { identity, rest } from "../api";
import { Icon } from "../components/Icons";
import { fmtMs } from "../lib/runs";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Summary = any;

const pct = (x: number | null | undefined, d = 1) => (x === null || x === undefined ? "—" : `${(x * 100).toFixed(d)}%`);
const TREND_METRICS = ["extraction.micro.f1", "extraction.auto_accept_precision", "retrieval.recall_at_3", "retrieval.mrr",
  "grounding.mean_groundedness_answered", "pdm.fd001_rmse", "safety.test_auroc"];

export function ObservabilityPage() {
  const [s, setS] = useState<Summary | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reg, setReg] = useState<any>(null);
  const [report, setReport] = useState<string | null>(null);
  const [metric, setMetric] = useState("retrieval.recall_at_3");
  const [tab, setTab] = useState("quality");
  const me = identity();

  const load = () => rest<Summary>("/api/obs/summary").then(setS).catch((e) => setErr(e.message));
  useEffect(() => { void load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, []);

  const runRegression = async () => {
    setBusy(true); setErr(null);
    try { const r = await rest<any>("/api/obs/regression", { body: {} }); setReg(r); await load(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  const openReport = async (id: string) => {
    try { setReport(await rest<string>(`/api/obs/report/${id}`)); } catch (e) { setErr(String(e)); }
  };

  const q = s?.quality;
  const hist = s?.history ?? [];
  const metricsAvail = useMemo(() => {
    const keys = new Set<string>();
    hist.forEach((h: any) => Object.keys(h.metrics ?? {}).forEach((k) => keys.add(k)));
    return [...TREND_METRICS.filter((k) => keys.has(k)), ...[...keys].filter((k) => !TREND_METRICS.includes(k) && !k.startsWith("latency.")).sort()];
  }, [hist]);
  const trend = hist.map((h: any) => ({ run: h.run_id.slice(4, 13), value: h.metrics?.[metric] ?? null }));
  const stages = Object.entries(s?.latency?.eval ?? {}).filter(([, v]: any) => v && typeof v === "object" && "p95" in v)
    .map(([k, v]: any) => ({ stage: k, p50: +v.p50.toFixed(2), p95: +v.p95.toFixed(2) })).sort((a, b) => b.p95 - a.p95).slice(0, 10);
  const perField = Object.entries(q?.extraction?.per_field ?? {}).map(([k, v]: any) => ({ field: k, f1: v.f1, precision: v.precision, recall: v.recall }));
  const usage = s?.usage;
  const rel = s?.reliability;

  return (
    <>
      <div className="page-head">
        <div>
          <p className="eyebrow">Assurance · MLflow · Langfuse / LangSmith</p>
          <h1>AI evaluation and observability</h1>
          <p className="lede">Model quality, retrieval and citation accuracy, extraction metrics, latency, token cost and agent reliability in one
            place. Every evaluation is fingerprinted, logged to MLflow and compared with the previous run, so regressions show up before they fail CI.</p>
        </div>
        <div className="row">
          <button className="btn" onClick={load}><Icon name="replay" size={14} />Refresh</button>
          <button className="btn primary" disabled={busy || me.role !== "admin"} onClick={runRegression} title={me.role !== "admin" ? "Admins run evaluations" : ""}>
            <Icon name="flask" size={14} />{busy ? "Running suite…" : "Run regression suite"}</button>
        </div>
      </div>
      {err && <div className="err">{err}</div>}
      {me.role !== "admin" && <p className="tiny muted">Switch to the admin role to run the regression suite.</p>}

      {s && (
        <div className="tiles">
          <div className="tile dark"><span className="k">Release gate</span><b>{s.gate.passed ? "Passing" : "Failing"}</b>
            <span>{s.gate.checks.filter((c: any) => c.passed).length}/{s.gate.checks.length} checks · {s.generated_at}</span></div>
          <div className="tile"><span className="k">Extraction F1</span><b>{pct(q.extraction.f1)}</b><span>auto-accept precision {pct(q.extraction.auto_accept_precision)} · synthetic</span></div>
          <div className="tile"><span className="k">Retrieval recall@3</span><b>{pct(q.retrieval.recall_at_3)}</b><span>MRR {q.retrieval.mrr?.toFixed(3)}</span></div>
          <div className="tile"><span className="k">Citation support</span><b>{pct(q.citations.support_rate)}</b><span>answered sentences backed by their source · abstention accuracy {pct(q.citations.abstention_accuracy, 0)}</span></div>
          <div className="tile"><span className="k">LLM tokens</span><b>{(usage.total.prompt_tokens + usage.total.completion_tokens).toLocaleString()}</b>
            <span>{usage.total.calls} calls · ${usage.total.cost_usd.toFixed(4)} at list price</span></div>
          <div className="tile"><span className="k">Agent reliability</span><b>{rel.completion_rate === null ? "—" : pct(rel.completion_rate, 0)}</b>
            <span>{rel.runs} runs · median {fmtMs(rel.run_p50_ms)}</span></div>
        </div>
      )}

      <div className="tabs section" role="tablist">
        {[["quality", "Model and RAG quality"], ["trends", "Regression history"], ["latency", "Latency and cost"], ["agents", "Agent reliability"], ["tracing", "Tracing and MLflow"]].map(([k, l]) => (
          <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)} role="tab" aria-selected={tab === k}>{l}</button>))}
      </div>

      {s && tab === "quality" && (
        <div className="two">
          <div className="panel">
            <h3>Extraction per field (golden drawings, synthetic)</h3>
            <div style={{ height: 420 }}>
              <ResponsiveContainer>
                <BarChart data={perField} layout="vertical" margin={{ left: 40 }}>
                  <CartesianGrid stroke="#eeeef7" /><XAxis type="number" domain={[0.85, 1]} fontSize={11} /><YAxis type="category" dataKey="field" fontSize={11} width={100} interval={0} />
                  <Tooltip /><Legend wrapperStyle={{ fontSize: 12 }} />
                  <Bar dataKey="precision" fill="#2f3169" radius={[0, 3, 3, 0]} /><Bar dataKey="recall" fill="#8284ac" radius={[0, 3, 3, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
            <p className="tiny muted">Calibration error (ECE) {q.extraction.ece} · automation rate {pct(q.extraction.automation_rate)}</p>
          </div>
          <div className="panel">
            <h3>Retrieval: rank of the right document per query</h3>
            <table className="small"><thead><tr><th>Query</th><th>Expected</th><th className="num">Rank</th></tr></thead>
              <tbody>{q.retrieval.queries.map((r: any) => (
                <tr key={r.query}><td>{r.query}</td><td className="mono">{r.expected}</td><td className="num"><b>{r.rank ?? "miss"}</b></td></tr>))}</tbody></table>
          </div>
          <div className="panel">
            <h3>Citation checks on grounded answers</h3>
            <table className="small"><thead><tr><th>Question</th><th>Decision</th><th className="num">Groundedness</th></tr></thead>
              <tbody>{q.citations.rows.map((r: any) => (
                <tr key={r.question}><td>{r.question}</td><td><span className={`sev sev-${r.decision === "pass" ? "low" : "medium"}`}>{r.decision}</span>{!r.answerable && <span className="tiny muted"> (unanswerable on purpose)</span>}</td>
                  <td className="num">{r.groundedness}</td></tr>))}</tbody></table>
          </div>
          <div className="panel">
            <h3>Models on real public data</h3>
            <div className="kv">
              <div><span>RUL RMSE FD001</span><b>{q.pdm?.fd001_rmse ?? "—"}</b></div>
              <div><span>Worst subset RMSE</span><b>{q.pdm?.worst_rmse ?? "—"}</b></div>
              <div><span>Min 90% coverage</span><b>{q.pdm?.min_coverage ?? "—"}</b></div>
              <div><span>Safety AUROC</span><b>{q.safety?.test_auroc ?? "—"}</b></div>
              <div><span>Safety PR-AUC</span><b>{q.safety?.test_pr_auc ?? "—"}</b></div>
              <div><span>Confidence drift (PSI)</span><b>{q.drift?.confidence_psi_vs_first_half ?? "—"}</b></div>
              <div><span>LLM judge κ vs humans</span><b>{q.judge?.judge_human_kappa} vs {q.judge?.human_human_kappa}</b></div>
              <div><span>Judge as a gate</span><b>{q.judge?.approved_as_gate ? "approved" : "refused"}</b></div>
            </div>
          </div>
        </div>
      )}

      {s && tab === "trends" && (
        <div className="split-r">
          <div className="panel">
            <div className="panel-head"><h3>Metric across evaluation runs</h3>
              <select value={metric} onChange={(e) => setMetric(e.target.value)} aria-label="Metric">{metricsAvail.map((m) => <option key={m}>{m}</option>)}</select></div>
            {hist.length ? (
              <div style={{ height: 260 }}>
                <ResponsiveContainer>
                  <LineChart data={trend} margin={{ left: -10, right: 10 }}>
                    <CartesianGrid stroke="#eeeef7" /><XAxis dataKey="run" fontSize={11} /><YAxis fontSize={11} domain={["auto", "auto"]} /><Tooltip />
                    <Line dataKey="value" stroke="#2f3169" strokeWidth={2.4} dot={{ r: 4, fill: "#2f3169" }} name={metric} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            ) : <div className="empty small">No tracked runs yet. Run the regression suite (admin) or <code>python -m aeronexus.evals.runner</code>.</div>}
            <table className="small section">
              <thead><tr><th>Run</th><th>By</th><th>Gate</th><th>Regressed</th><th>Improved</th><th>MLflow</th><th /></tr></thead>
              <tbody>{hist.slice().reverse().map((h: any) => (
                <tr key={h.run_id}><td className="mono">{h.run_id}</td><td>{h.by}</td><td>{h.gate_passed ? "✓ passed" : "✕ failed"}</td>
                  <td>{h.regressed.length ? <b>{h.regressed.join(", ")}</b> : "none"}</td><td>{h.improved.join(", ") || "—"}</td>
                  <td className="mono tiny">{String(h.mlflow_run_id ?? "—").slice(0, 12)}</td>
                  <td><button className="btn ghost sm" onClick={() => openReport(h.run_id)}>Report</button></td></tr>))}</tbody>
            </table>
          </div>
          <div style={{ display: "grid", gap: 12 }}>
            {reg && (
              <div className={`panel ${reg.regression?.regressed?.length ? "dark" : ""}`}>
                <h3>Latest regression check</h3>
                <p className="small">Run {reg.run_id}: gate {reg.gate_passed ? "passed" : "FAILED"}.
                  {reg.regression ? ` Compared with ${reg.regression.previous}: ${reg.regression.regressed.length} regressed, ${reg.regression.improved.length} improved.` : " First tracked run."}</p>
                {reg.regression?.regressed?.map((m: string) => <div key={m} className="tag strong">{m}</div>)}
              </div>
            )}
            <div className="panel">
              <h3>How regression testing works</h3>
              <ul className="small" style={{ paddingLeft: 18, margin: 0 }}>
                <li>32 gated metrics on frozen golden sets and real public benchmarks.</li>
                <li>Each run is fingerprinted: code, data, models, thresholds and library versions.</li>
                <li>A metric that moves the wrong way by more than 0.005 (latency: 25%) is flagged, even if it still passes.</li>
                <li>CI runs the same suite on every change and fails on any gate.</li>
              </ul>
            </div>
            {report && <div className="panel"><div className="panel-head"><h3>Reproducible report</h3>
              <button className="btn sm" onClick={() => { const u = URL.createObjectURL(new Blob([report], { type: "text/markdown" })); const a = document.createElement("a"); a.href = u; a.download = "aeronexus-eval-report.md"; a.click(); }}>Download .md</button></div>
              <pre className="json" style={{ maxHeight: 420 }}>{report}</pre></div>}
          </div>
        </div>
      )}

      {s && tab === "latency" && (
        <div className="two">
          <div className="panel">
            <h3>Latency by pipeline stage (evaluation run)</h3>
            <div style={{ height: 300 }}>
              <ResponsiveContainer>
                <BarChart data={stages} layout="vertical" margin={{ left: 70 }}>
                  <CartesianGrid stroke="#eeeef7" /><XAxis type="number" fontSize={11} unit=" ms" /><YAxis type="category" dataKey="stage" fontSize={11} width={130} />
                  <Tooltip /><Legend wrapperStyle={{ fontSize: 12 }} />
                  <Bar dataKey="p50" fill="#8284ac" radius={[0, 3, 3, 0]} /><Bar dataKey="p95" fill="#2f3169" radius={[0, 3, 3, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
          <div className="panel">
            <h3>LLM tokens and cost by model</h3>
            <table className="small">
              <thead><tr><th>Model</th><th className="num">Calls</th><th className="num">Cached</th><th className="num">Prompt</th><th className="num">Completion</th><th className="num">Cost (USD)</th></tr></thead>
              <tbody>{Object.entries(usage.models).map(([m, v]: any) => (
                <tr key={m}><td className="mono">{m}</td><td className="num">{v.calls}</td><td className="num">{v.cached}</td><td className="num">{v.prompt_tokens.toLocaleString()}</td>
                  <td className="num">{v.completion_tokens.toLocaleString()}</td><td className="num">{v.cost_usd.toFixed(5)}</td></tr>))}</tbody>
            </table>
            {!Object.keys(usage.models).length && <p className="small muted">No LLM calls since the server started.</p>}
            <p className="tiny muted">{usage.note} Mock-provider calls are estimated at ~4 characters per token.</p>
            <h3 style={{ marginTop: 12 }}>Price table (USD per 1M tokens, in / out)</h3>
            {Object.entries(usage.prices_usd_per_1m).map(([m, p]: any) => <div key={m} className="small"><span className="mono">{m}</span>: {p[0]} / {p[1]}</div>)}
          </div>
        </div>
      )}

      {s && tab === "agents" && (
        <div className="panel">
          <div className="panel-head"><h3>Agent and step reliability (mission + workflow runs)</h3>
            <span className="small muted">{Object.entries(rel.statuses).map(([k, v]) => `${k}: ${v}`).join(" · ")}</span></div>
          <table className="small">
            <thead><tr><th>Agent / step</th><th className="num">Executions</th><th>Success</th><th className="num">Failures</th><th className="num">Retries</th><th className="num">Fallbacks</th><th className="num">p50</th><th className="num">p95</th></tr></thead>
            <tbody>{rel.steps.map((r: any) => (
              <tr key={r.type}><td><b>{r.label}</b> <span className="tiny muted mono">{r.type}</span></td><td className="num">{r.executions}</td>
                <td><div className="row" style={{ gap: 6 }}><div className="health-bar" style={{ width: 90, marginTop: 0 }}><i style={{ width: `${(r.success_rate ?? 0) * 100}%` }} /></div>{pct(r.success_rate, 0)}</div></td>
                <td className="num">{r.failed}</td><td className="num">{r.retries}</td><td className="num">{r.fallbacks}</td><td className="num">{fmtMs(r.p50_ms)}</td><td className="num">{fmtMs(r.p95_ms)}</td></tr>))}</tbody>
          </table>
          {!rel.steps.length && <p className="small muted">Run a mission or a workflow to populate this table.</p>}
        </div>
      )}

      {s && tab === "tracing" && (
        <div className="three">
          <div className="panel"><h3>Langfuse</h3>
            <p><span className={`sev ${s.tracing.langfuse.enabled ? "sev-critical" : "sev-low"}`}>{s.tracing.langfuse.enabled ? "exporting" : "not configured"}</span></p>
            <p className="small">Each run becomes a trace; each agent or step a span with input, output and errors.</p>
            <pre className="code">{`LANGFUSE_PUBLIC_KEY=pk-...\nLANGFUSE_SECRET_KEY=sk-...\nLANGFUSE_HOST=http://localhost:3001`}</pre>
            <p className="tiny muted">The open-source server self-hosts with docker compose; add the keys to backend/.env.</p></div>
          <div className="panel"><h3>LangSmith</h3>
            <p><span className={`sev ${s.tracing.langsmith.enabled ? "sev-critical" : "sev-low"}`}>{s.tracing.langsmith.enabled ? "exporting" : "not configured"}</span></p>
            <p className="small">LangGraph traces itself to LangSmith when these are set:</p>
            <pre className="code">{`LANGSMITH_TRACING=true\nLANGSMITH_API_KEY=lsv2_...\nLANGSMITH_PROJECT=aeronexus`}</pre></div>
          <div className="panel"><h3>MLflow</h3>
            <p className="small">Tracking store: <span className="mono">{s.mlflow.tracking_uri.replace(/^.*\/backend\//, "backend/")}</span></p>
            <pre className="code">{s.mlflow.ui}</pre>
            <table className="small"><thead><tr><th>Run</th><th>Gate</th><th className="num">recall@3</th></tr></thead>
              <tbody>{s.mlflow.runs.slice(0, 8).map((r: any) => <tr key={r.run_id}><td className="mono tiny">{r.name}</td><td>{r.gate}</td><td className="num">{r.metrics["retrieval.recall_at_3"] ?? "—"}</td></tr>)}</tbody></table>
            {!s.mlflow.runs.length && <p className="tiny muted">No MLflow runs yet.</p>}</div>
        </div>
      )}
    </>
  );
}
