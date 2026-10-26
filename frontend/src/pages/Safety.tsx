import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { gql, Q } from "../api";
import { label } from "../lib/cloud";
import { useAsync } from "../lib/useAsync";

/* eslint-disable @typescript-eslint/no-explicit-any */
const EXAMPLE = "The pilot's failure to maintain adequate airspeed during the turn to final, which resulted in an aerodynamic stall at low altitude.";
const DECISION_COPY: Record<string, string> = {
  high_priority: "High priority for review", routine: "Routine", abstain: "Too close to call: a person should read it first",
};

export function SafetyPage() {
  const ov = useAsync(() => gql<{ safetyOverview: any }>(Q.safety).then((d) => d.safetyOverview));
  const [text, setText] = useState(EXAMPLE);
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    if (!text.trim()) { setErr("Paste a narrative or probable-cause statement first."); return; }
    setBusy(true); setErr(null);
    try { setRes((await gql<{ safetyTriage: any }>(Q.triage, { t: text })).safetyTriage); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  const o = ov.data;
  const years = o?.available ? Object.keys(o.tag_trend.maintenance).sort() : [];
  const trend = years.map((y) => ({ year: y, ...Object.fromEntries(["maintenance", "loss_of_control", "engine_power", "fuel"]
    .map((k) => [k, +(o.tag_trend[k][y] * 100).toFixed(1)])) }));
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Safety analytics</h1>
          <p className="lede">Prioritise incident narratives for review and find similar past events, using the public NTSB accident
            database. Scores set review order; they never replace an investigation.</p>
        </div>
      </div>
      {ov.error && <div className="err">{ov.error}</div>}
      {o && !o.available && <div className="err">{o.message}</div>}
      <div className="split">
        <div className="panel">
          <h2>Triage a narrative</h2>
          <textarea value={text} onChange={(e) => { setText(e.target.value); setErr(null); }} style={{ width: "100%", height: 110 }}
                    aria-label="Narrative" />
          {err && <div className="small" style={{ color: "var(--red-deep)" }}>{err}</div>}
          <div className="row" style={{ marginTop: 8 }}><button className="btn primary" disabled={busy} onClick={run}>Score and find similar events</button></div>
          {res?.triage?.available && (
            <div className="section">
              <div className="figures">
                <div className={`figure ${res.triage.decision === "high_priority" ? "alert" : ""}`}><b>{DECISION_COPY[res.triage.decision]}</b>
                  <span>probability {res.triage.probability} vs threshold {res.triage.threshold}</span></div>
              </div>
              <p className="small muted">{res.triage.note} Words that state the outcome (killed, fatal…) are masked before scoring.</p>
            </div>
          )}
          {res?.similar?.length > 0 && (
            <div className="section">
              <h3>Most similar NTSB events</h3>
              {res.similar.map((s: any) => (
                <div key={s.id} className="source">
                  <b>{s.title}</b> · {s.section} · {s.damage ?? ""}{s.fatal ? " · fatal" : ""}{s.verified === false ? " · unverified report" : ""}
                  <div>{s.text}</div>
                </div>))}
            </div>
          )}
        </div>
        {o?.available && (
          <div>
            <div className="panel">
              <h3>Model on unseen years</h3>
              <p className="small" style={{ marginTop: 0 }}>Trained on 2008–2015, tested on {o.metrics.n_test.toLocaleString()} events from 2017–2022
                (fatal in {Math.round(o.metrics.test_prevalence * 100)}%). AUROC {o.metrics.test_auroc}, PR-AUC {o.metrics.test_pr_auc},
                calibration error {o.metrics.calibration.ece_prob}. <span className="prov">Public data · NTSB</span></p>
              <table><thead><tr><th>Abstain band</th><th className="num">Coverage</th><th className="num">Accuracy</th></tr></thead>
                <tbody>{o.metrics.selective_prediction.map((r: any) => (
                  <tr key={r.abstain_band}><td>±{r.abstain_band}</td><td className="num">{Math.round(r.coverage * 100)}%</td><td className="num">{Math.round(r.accuracy * 1000) / 10}%</td></tr>))}</tbody></table>
            </div>
            <div className="panel section">
              <h3>Share of probable causes mentioning…</h3>
              <div style={{ height: 220 }}>
                <ResponsiveContainer>
                  <LineChart data={trend} margin={{ left: -18 }}>
                    <CartesianGrid stroke="#eef0f3" /><XAxis dataKey="year" tick={{ fontSize: 11 }} /><YAxis tick={{ fontSize: 11 }} unit="%" />
                    <Tooltip /><Legend wrapperStyle={{ fontSize: 12 }} formatter={(v) => label(String(v))} />
                    <Line dataKey="maintenance" stroke="var(--red)" dot={false} />
                    <Line dataKey="loss_of_control" stroke="var(--ink)" dot={false} />
                    <Line dataKey="engine_power" stroke="#8a909c" dot={false} strokeDasharray="4 3" />
                    <Line dataKey="fuel" stroke="var(--red-deep)" dot={false} strokeDasharray="2 2" />
                  </LineChart>
                </ResponsiveContainer>
              </div>
              <p className="small muted">Deterministic keyword tags, not model output, so you can audit exactly what was counted.</p>
            </div>
            <div className="panel section">
              <h3>Fatal share by cause tag</h3>
              <div style={{ height: 180 }}>
                <ResponsiveContainer>
                  <BarChart data={Object.entries(o.tag_fatality_rate).map(([k, v]: any) => ({ name: label(k), v: +(v * 100).toFixed(1) }))} margin={{ left: -18 }}>
                    <XAxis dataKey="name" tick={{ fontSize: 10 }} /><YAxis tick={{ fontSize: 11 }} unit="%" /><Tooltip />
                    <Bar dataKey="v" fill="var(--red)" barSize={26} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
            <div className="panel section">
              <h3>Cause themes found in the text</h3>
              <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>{o.topics.map((t: any) => <li key={t.id}>{t.top_terms.slice(0, 5).join(", ")}</li>)}</ul>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
