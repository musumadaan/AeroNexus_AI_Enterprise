import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Scatter, ComposedChart, Tooltip, XAxis, YAxis } from "recharts";
import { gql, Q, type SheetData } from "../api";
import { SheetView } from "../components/SheetView";
import { label, pct } from "../lib/cloud";
import { useAsync } from "../lib/useAsync";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Report = any;

const SLICE_COPY: Record<string, string> = {
  clean: "Clean", ocr_noise: "OCR damage", moved_title: "Moved title block", conflict: "Conflicting values",
  injection: "Injected instructions", dropped_field: "Missing field",
};

export function EvalsPage() {
  const rep = useAsync(() => gql<{ evalReport: Report }>(Q.report).then((d) => d.evalReport));
  const [running, setRunning] = useState(false);
  const [fail, setFail] = useState<any>(null);
  const [sheet, setSheet] = useState<SheetData | null>(null);
  const r = rep.data;

  const rerun = async () => {
    setRunning(true);
    try {
      const d = await gql<{ runEvals: Report }>(Q.runEvals);
      rep.setData(d.runEvals);
    } finally { setRunning(false); }
  };
  const openFailure = async (f: any) => {
    setFail(f);
    const d = await gql<{ sheet: SheetData }>(`query($id: String!) { sheet(id: $id) }`, { id: f.sheet_id });
    setSheet(d.sheet);
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Evaluation</h1>
          <p className="lede">Every number below is measured on frozen golden sets that are never used for training. A release is blocked if any gate fails.</p>
        </div>
        <button className="btn primary" disabled={running} onClick={rerun}>{running ? "Running…" : "Run evaluation"}</button>
      </div>
      {rep.error && <div className="err">{rep.error}</div>}
      {!r && rep.loading && <div className="panel"><div className="skeleton" /><div className="skeleton" /></div>}
      {r && (
        <>
          <div className={`panel ${r.gate.passed ? "" : "err"}`}>
            <h2 style={{ marginBottom: 4 }}>{r.gate.passed ? "All release gates pass" : "Release blocked"}</h2>
            <p className="small muted" style={{ marginTop: 0 }}>{r.gate.checks.filter((c: any) => c.passed).length} of {r.gate.checks.length} checks · generated {r.generated_at} · {r.duration_s}s</p>
            <table>
              <thead><tr><th>Check</th><th className="num">Measured</th><th className="num">Required</th><th>Why it matters</th></tr></thead>
              <tbody>{r.gate.checks.map((c: any) => (
                <tr key={c.metric}>
                  <td><span className={`status ${c.passed ? "auto_accepted" : "missing"}`}>{c.metric}</span></td>
                  <td className="num">{String(c.value)}</td><td className="num">{c.op === "ge" ? "≥" : c.op === "le" ? "≤" : "="} {String(c.target)}</td>
                  <td className="small muted">{c.why}</td>
                </tr>))}</tbody>
            </table>
          </div>

          <div className="two section">
            {r.pdm?.available && (
              <div className="panel">
                <h2>Predictive maintenance <span className="prov">Public data · NASA C-MAPSS</span></h2>
                <table><thead><tr><th>Subset</th><th className="num">RMSE</th><th className="num">PHM score</th><th className="num">90% range coverage</th><th className="num">Health AUROC</th></tr></thead>
                  <tbody>{Object.entries(r.pdm.subsets).map(([k, v]: any) => (
                    <tr key={k}><td>{k}</td><td className="num">{v.test_rmse_capped}</td><td className="num">{v.test_phm_score}</td>
                      <td className="num">{pct(v.interval_coverage_test, 0)}</td><td className="num">{v.health_index_auroc_early_vs_late}</td></tr>))}</tbody></table>
                <p className="small muted">RMSE in cycles against true remaining life capped at 125 (the usual convention). Lower PHM score is better.</p>
              </div>
            )}
            {r.safety?.available && (
              <div className="panel">
                <h2>Safety triage <span className="prov">Public data · NTSB</span></h2>
                <table><tbody>
                  <tr><td>AUROC on 2017–2022 (trained on 2008–2015)</td><td className="num">{r.safety.test_auroc}</td></tr>
                  <tr><td>PR-AUC at {pct(r.safety.test_prevalence, 0)} positives</td><td className="num">{r.safety.test_pr_auc}</td></tr>
                  <tr><td>Calibration error</td><td className="num">{r.safety.ece}</td></tr>
                  <tr><td>AUROC change when outcome words are masked</td><td className="num">{r.safety.leakage_gap}</td></tr>
                </tbody></table>
              </div>
            )}
          </div>

          <div className="panel section">
            <h2>Change orders <span className="prov synthetic">Synthetic revision pairs</span></h2>
            <div className="figures">
              <div className="figure"><b>{pct(r.change.field_change.f1)}</b><span>changed fields detected (F1)</span></div>
              <div className="figure"><b>{pct(r.change.severity_accuracy)}</b><span>severity correct</span></div>
              <div className="figure"><b>{pct(r.change.receipt_reproducibility)}</b><span>calculations reproduced from receipts</span></div>
              <div className="figure"><b>{pct(r.change.replay_identical)}</b><span>cases identical when replayed from the event log</span></div>
              <div className="figure"><b>{pct(r.security.pass_rate)}</b><span>access-control checks passing</span></div>
            </div>
          </div>

          <div className="section">
            <h2>Drawing extraction <span className="prov synthetic">Synthetic golden drawings</span></h2>
            <div className="figures">
              <div className="figure"><b>{pct(r.extraction.micro.f1)}</b><span>F1 over {r.extraction.n_sheets} drawings</span></div>
              <div className="figure"><b>{pct(r.extraction.auto_accept_precision, 2)}</b><span>right when accepted without a person</span></div>
              <div className="figure"><b>{pct(r.extraction.automation_rate, 0)}</b><span>of fields filled with no human work</span></div>
              <div className="figure"><b>{r.extraction.calibration.ece}</b><span>calibration error (lower is better)</span></div>
              <div className="figure"><b>{r.extraction.latency_ms.p95} ms</b><span>p95 per drawing</span></div>
            </div>
          </div>

          <div className="two section">
            <div className="panel">
              <h3>F1 by field</h3>
              <div style={{ height: 260 }}>
                <ResponsiveContainer>
                  <BarChart data={Object.entries(r.extraction.per_field).map(([k, v]: any) => ({ name: label(k), f1: v.f1, recall: v.recall }))} layout="vertical" margin={{ left: 30 }}>
                    <CartesianGrid horizontal={false} stroke="#eef0f3" />
                    <XAxis type="number" domain={[0.8, 1]} tick={{ fontSize: 11 }} />
                    <YAxis type="category" dataKey="name" tick={{ fontSize: 11 }} width={90} />
                    <Tooltip formatter={(v: number) => pct(v)} />
                    <Bar dataKey="f1" fill="var(--red)" barSize={10} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
            <div className="panel">
              <h3>Does confidence mean what it says?</h3>
              <p className="small muted" style={{ marginTop: 0 }}>Points on the dashed line are perfectly calibrated. Confidence decides what goes to a person, so this has to hold.</p>
              <div style={{ height: 220 }}>
                <ResponsiveContainer>
                  <ComposedChart data={r.extraction.calibration.bins} margin={{ left: -10 }}>
                    <CartesianGrid stroke="#eef0f3" />
                    <XAxis type="number" dataKey="confidence" domain={[0.3, 1]} tick={{ fontSize: 11 }} label={{ value: "reported confidence", position: "insideBottom", offset: -2, fontSize: 11 }} height={34} />
                    <YAxis type="number" domain={[0.3, 1]} tick={{ fontSize: 11 }} />
                    <ReferenceLine segment={[{ x: 0.3, y: 0.3 }, { x: 1, y: 1 }]} stroke="var(--graphite)" strokeDasharray="4 4" />
                    <Tooltip formatter={(v: number) => v.toFixed(3)} />
                    <Scatter dataKey="accuracy" fill="var(--red)" />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            </div>
          </div>

          <div className="panel section">
            <h3>Hard cases</h3>
            <div className="three">
              {Object.entries(r.extraction.slices).map(([k, v]: any) => (
                <div key={k} className={`figure ${v.f1 < 0.97 ? "alert" : ""}`}><b>{pct(v.f1)}</b><span>{SLICE_COPY[k] ?? label(k)} (recall {pct(v.recall)})</span></div>))}
            </div>
          </div>

          <div className="split section">
            <div className="panel flush">
              <table>
                <thead><tr><th>Drawing</th><th>Field</th><th>Read</th><th>Correct</th><th>Cause</th></tr></thead>
                <tbody>{r.extraction.failures.map((f: any, i: number) => (
                  <tr key={i} className={`clickable ${fail === f ? "selected" : ""}`} onClick={() => openFailure(f)}>
                    <td>{f.sheet_id}</td><td>{label(f.field)}</td><td>{f.predicted ?? "—"}</td><td className="val">{f.truth ?? "—"}</td>
                    <td className="small">{f.cause}<div className="src">{label(f.status)}</div></td>
                  </tr>))}</tbody>
              </table>
            </div>
            <div>{sheet && fail ? <SheetView sheet={sheet} highlightTokens={fail.token_ids} caption={`${fail.sheet_id} · ${label(fail.field)}`} />
              : <div className="panel empty">Pick a failure to see where it happened on the drawing.</div>}</div>
          </div>

          <div className="two section">
            <div className="panel">
              <h2>LLM judge reliability</h2>
              <p className="small" style={{ marginTop: 0 }}>{r.judge.verdict}</p>
              <div style={{ height: 160 }}>
                <ResponsiveContainer>
                  <BarChart data={[{ name: "Human vs human", k: r.judge.human_human_kappa }, { name: "Judge vs human", k: r.judge.judge_human_kappa }]} margin={{ left: -16 }}>
                    <XAxis dataKey="name" tick={{ fontSize: 11 }} /><YAxis domain={[0, 1]} tick={{ fontSize: 11 }} />
                    <Tooltip formatter={(v: number) => v.toFixed(3)} />
                    <ReferenceLine y={0.6} stroke="var(--graphite)" strokeDasharray="3 3" />
                    <Bar dataKey="k" fill="var(--red)" barSize={46} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <ul className="small">
                <li>Raw agreement {pct(r.judge.judge_human_raw_agreement)} looks better than kappa {r.judge.judge_human_kappa} by {r.judge.kappa_deflation_pp} points: that's agreement by chance.</li>
                <li>95% interval for judge kappa: {r.judge.judge_human_kappa_ci95[0]} to {r.judge.judge_human_kappa_ci95[1]}.</li>
                <li>Changed its verdict when answer order was swapped in {pct(r.judge.position_bias_rate)} of {r.judge.pairs_tested} pairs.</li>
                <li>Pass rate it measured: {pct(r.judge.test_pass_rate_observed)}; corrected for its known errors: {pct(r.judge.test_pass_rate_corrected)}; true: {pct(r.judge.test_pass_rate_true)}.</li>
              </ul>
            </div>
            <div className="panel">
              <h2>Other modules</h2>
              <table><tbody>
                <tr><td>Retrieval: right document in top 3</td><td className="num">{pct(r.retrieval.recall_at_3)}</td></tr>
                <tr><td>Answers abstain when sources can't support them</td><td className="num">{pct(r.grounding.abstention_accuracy)}</td></tr>
                <tr><td>Change: severity classified correctly</td><td className="num">{pct(r.change.severity_accuracy)}</td></tr>
                <tr><td>Directive status correct</td><td className="num">{pct(r.mro.status_accuracy)}</td></tr>
                <tr><td>Invoice match label correct</td><td className="num">{pct(r.ap.accuracy)}</td></tr>
                <tr><td>Exceptions wrongly auto-matched</td><td className="num">{r.ap.false_auto_match}</td></tr>
                <tr><td>Price model typical error</td><td className="num">{pct(r.pricing.test_mape)}</td></tr>
                <tr><td>Price ranges that contained the true price (target 90%)</td><td className="num">{pct(r.pricing.test_coverage)}</td></tr>
              </tbody></table>
            </div>
          </div>

          <div className="panel section">
            <h2>Where the time goes</h2>
            <div style={{ height: 230 }}>
              <ResponsiveContainer>
                <LineChart data={Object.entries(r.latency).filter(([, v]: any) => v.count > 1).map(([k, v]: any) => ({ stage: k, p50: +v.p50.toFixed(3), p95: +v.p95.toFixed(3) }))} margin={{ left: -10 }}>
                  <CartesianGrid stroke="#eef0f3" />
                  <XAxis dataKey="stage" tick={{ fontSize: 10 }} interval={0} angle={-20} textAnchor="end" height={54} />
                  <YAxis tick={{ fontSize: 11 }} unit=" ms" />
                  <Tooltip />
                  <Line dataKey="p50" stroke="var(--ink)" dot />
                  <Line dataKey="p95" stroke="var(--red)" dot />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <p className="small muted">LLM cache hits this run: {r.counters["llm.cache_hit"] ?? 0}. Escalations from the small to the large model: {r.counters["llm.escalate.low_conf"] ?? 0}. Untrusted passages quarantined: {r.counters["rag.quarantined"] ?? 0}.</p>
          </div>
        </>
      )}
    </>
  );
}
