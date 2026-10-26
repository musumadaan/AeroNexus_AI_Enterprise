import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis, ReferenceLine } from "recharts";
import { gql, Q, rest } from "../api";
import { EngineCanvas } from "../components/EngineModel";
import { Icon } from "../components/Icons";
import { RulGauge } from "../components/RulGauge";
import { getMeta, getTelemetry, statusOf, type Telemetry, type TwinMeta } from "../lib/twin";

interface FleetRow { unit: number; pred_rul: number; low: number; high: number; health: number; work_order: boolean }

export function TwinPage() {
  const [meta, setMeta] = useState<TwinMeta | null>(null);
  const [subset, setSubset] = useState("FD001");
  const [unit, setUnit] = useState(20);
  const [fleet, setFleet] = useState<FleetRow[]>([]);
  const [t, setT] = useState<Telemetry | null>(null);
  const [idx, setIdx] = useState(0);
  const [sel, setSel] = useState<string>("hpc");
  const [exploded, setExploded] = useState(false);
  const [cutaway, setCutaway] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [fired, setFired] = useState<string | null>(null);

  useEffect(() => { getMeta().then(setMeta).catch((e) => setErr(String(e.message ?? e))); }, []);
  useEffect(() => {
    gql<{ pdmFleet: { engines: FleetRow[] } }>(Q.fleet, { s: subset }).then((d) => setFleet(d.pdmFleet.engines ?? [])).catch(() => setFleet([]));
  }, [subset]);
  useEffect(() => {
    setT(null); setErr(null);
    getTelemetry(subset, unit).then((x) => {
      if (!x.available) { setErr(x.message ?? "Models not trained"); return; }
      setT(x); setIdx(x.frames.length - 1);
    }).catch((e) => setErr(e.message));
  }, [subset, unit]);

  const f = t?.frames[idx];
  const comps = meta?.components ?? [];
  const comp = comps.find((c) => c.id === sel);
  const events = (t?.events ?? []).filter((e) => f && e.cycle <= f.cycle);
  const alertIds = useMemo(() => comps.filter((c) => statusOf(f?.components[c.id]) === "poor").map((c) => c.id), [f, comps]);
  const series = useMemo(() => {
    if (!t || !comp) return [];
    return t.frames.slice(0, idx + 1).map((fr) => ({ cycle: fr.cycle, score: fr.components[comp.id] }));
  }, [t, comp, idx]);

  const fire = async () => {
    try {
      const r = await rest<{ started: Array<{ name: string; run_id: string }> }>("/api/triggers/fire", { body: { type: "sensor_anomaly", payload: { subset, unit } } });
      setFired(r.started.length ? `Started ${r.started.map((s) => s.name).join(", ")}` : "No enabled workflow listens for sensor anomalies.");
    } catch (e) { setFired(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <p className="eyebrow">Predictive maintenance · digital twin</p>
          <h1>Engine digital twin</h1>
          <p className="lede">A turbofan from NASA's C-MAPSS fleet, coloured by the condition of each module. Click a module for its live sensor
            readings; drag the timeline to replay the engine's history. Lighter means healthier; deep indigo means worn.</p>
        </div>
        <div className="row">
          <label className="field">Fleet
            <select value={subset} onChange={(e) => { setSubset(e.target.value); setUnit(1); }}>
              {["FD001", "FD002", "FD003", "FD004"].map((s) => <option key={s}>{s}</option>)}
            </select>
          </label>
          <label className="field">Engine
            <select value={unit} onChange={(e) => setUnit(Number(e.target.value))} aria-label="Engine">
              {(fleet.length ? fleet : (meta?.engines[subset] ?? []).map((u) => ({ unit: u } as FleetRow))).map((r) => (
                <option key={r.unit} value={r.unit}>#{r.unit}{r.low !== undefined ? ` · RUL ${r.pred_rul} (${r.low}–${r.high})${r.work_order ? " · inspect" : ""}` : ""}</option>
              ))}
            </select>
          </label>
        </div>
      </div>
      {err && <div className="err">{err}</div>}

      <div className="split-r">
        <div>
          <div className="twin-stage">
            {t && f ? (
              <EngineCanvas scores={f.components} selected={sel} onSelect={(id) => id && setSel(id)} exploded={exploded} cutaway={cutaway}
                            alertIds={alertIds} spin={Math.max(0.25, f.rul / 125)} />
            ) : <div className="empty" style={{ paddingTop: 220 }}>Loading the twin…</div>}
            {f && (
              <div className="twin-hud">
                <div className="row" style={{ gap: 8 }}>
                  <div className="chip">Cycle<br /><b>{f.cycle}</b></div>
                  <div className="chip">Remaining life<br /><b>{f.rul}</b> <span className="tiny">({f.low}–{f.high})</span></div>
                  <div className="chip">Health score<br /><b>{f.health.toFixed(3)}</b> <span className="tiny">limit {t!.health_threshold.toFixed(3)}</span></div>
                </div>
                <div className="chip" style={{ alignSelf: "flex-start" }}>{subset} · engine #{unit}{f.anomaly ? " · anomaly at this cycle" : ""}</div>
              </div>
            )}
            <div className="twin-legend"><span>Worn</span><i /><span>Healthy</span></div>
            <div className="twin-tools">
              <button className={`btn sm ${exploded ? "on" : ""}`} onClick={() => setExploded((x) => !x)}>Exploded view</button>
              <button className={`btn sm ${cutaway ? "on" : ""}`} onClick={() => setCutaway((x) => !x)}>Remove nacelle</button>
            </div>
          </div>
          {t && (
            <div className="panel section" style={{ marginTop: 14 }}>
              <div className="row">
                <b className="small">History replay</b>
                <input type="range" min={0} max={t.frames.length - 1} value={idx} onChange={(e) => setIdx(Number(e.target.value))}
                       style={{ flex: 1 }} aria-label="Cycle" />
                <span className="mono">cycle {f?.cycle} / {t.frames[t.frames.length - 1].cycle}</span>
                <Link className="btn sm" to={`/simulator?subset=${subset}&unit=${unit}`}><Icon name="sim" size={14} /> Open in simulator</Link>
              </div>
              <div className="row tiny muted" style={{ marginTop: 6 }}>
                {t.events.map((e) => (
                  <button key={`${e.kind}-${e.cycle}-${e.sensor}`} className="tag" style={{ cursor: "pointer" }}
                          onClick={() => setIdx(t.frames.findIndex((fr) => fr.cycle >= e.cycle))}>
                    {e.kind === "health" ? "⚠ " : ""}cycle {e.cycle}: {e.kind.replace("_", " ")}{e.sensor ? ` ${meta?.sensors[e.sensor]?.name}` : ""}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        <div style={{ display: "grid", gap: 14 }}>
          {f && (
            <div className="panel" style={{ textAlign: "center" }}>
              <h3 style={{ textAlign: "left" }}>Remaining useful life</h3>
              <RulGauge rul={f.rul} low={f.low} high={f.high} truth={f.true_rul} />
              <div className="gauge-num">{f.rul} <span className="small muted">cycles</span></div>
              <div className="small muted">90% range {f.low}–{f.high} · true {f.true_rul} (evaluation only)</div>
              <div className="row" style={{ justifyContent: "center", marginTop: 10 }}>
                <span className={`sev sev-${f.low <= 30 || f.health > (t?.health_threshold ?? 1) ? "critical" : f.low <= 60 ? "medium" : "low"}`}>
                  {f.low <= 30 || f.health > (t?.health_threshold ?? 1) ? "Inspection rule met" : f.low <= 60 ? "Watch" : "Healthy"}
                </span>
                <button className="btn sm" onClick={fire}><Icon name="bolt" size={14} /> Send to workflows</button>
              </div>
              {fired && <p className="tiny muted">{fired} · <Link to="/builder">open builder</Link></p>}
            </div>
          )}
          <div className="panel comp-list">
            <h3>Modules</h3>
            {comps.map((c) => {
              const s = f?.components[c.id];
              return (
                <button key={c.id} className={sel === c.id ? "on" : ""} onClick={() => setSel(c.id)}>
                  <div>
                    <div className="val small">{c.label}</div>
                    <div className="health-bar"><i style={{ width: `${s ?? 0}%` }} /></div>
                  </div>
                  <span className={`sev sev-${statusOf(s) === "poor" ? "critical" : statusOf(s) === "watch" ? "medium" : "low"}`}>
                    {s === null || s === undefined ? "n/a" : Math.round(s)}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {comp && f && meta && (
        <div className="two section">
          <div className="panel">
            <div className="panel-head"><h2>{comp.label}</h2><span className="tag soft">{statusOf(f.components[comp.id])}</span></div>
            <p className="small muted" style={{ marginTop: 0 }}>{comp.what}</p>
            <table>
              <thead><tr><th>Sensor</th><th className="num">Reading</th><th className="num">Deviation (z)</th></tr></thead>
              <tbody>
                {comp.sensors.filter((s) => s in f.raw).map((s) => (
                  <tr key={s}>
                    <td><b>{meta.sensors[s].name}</b> <span className="small muted">{meta.sensors[s].label}</span></td>
                    <td className="num mono">{f.raw[s]} {meta.sensors[s].unit}</td>
                    <td className="num mono">{f.z[s] !== undefined ? (f.z[s] > 0 ? "+" : "") + f.z[s].toFixed(2) : "—"}</td>
                  </tr>
                ))}
                {!comp.sensors.some((s) => s in f.raw) && <tr><td colSpan={3} className="muted small">This fleet's normaliser drops these sensors (they don't vary).</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="panel">
            <h3>{comp.label} condition over the engine's life</h3>
            <div style={{ height: 210 }}>
              <ResponsiveContainer>
                <LineChart data={series} margin={{ left: -18, right: 8, top: 6 }}>
                  <XAxis dataKey="cycle" fontSize={11} stroke="#8284ac" minTickGap={28} />
                  <YAxis domain={[0, 100]} fontSize={11} stroke="#8284ac" />
                  <Tooltip />
                  <ReferenceLine y={50} stroke="#b5b6d6" strokeDasharray="4 3" label={{ value: "poor below 50", fontSize: 10, fill: "#53558c", position: "insideBottomRight" }} />
                  <Line dataKey="score" stroke="#2f3169" strokeWidth={2} dot={false} isAnimationActive={false} name="Condition (0–100)" />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <p className="tiny muted">Condition index = how far this module's sensors have drifted from the engine's own first 20 cycles.
              It is a transparent sensor-group measure, not a separately trained model.</p>
            {!!events.length && (
              <>
                <h3 style={{ marginTop: 10 }}>Detector events so far</h3>
                {events.slice(-4).map((e) => <div key={`${e.kind}${e.cycle}${e.sensor}`} className="issue high"><i /><div className="small">{e.message}</div></div>)}
              </>
            )}
          </div>
        </div>
      )}
      {t && <p className="tiny muted section">{t.notes.join(" ")} Model: {t.model.selected.toUpperCase()}, test RMSE {t.model.test_rmse} cycles,
        90% interval coverage {(t.model.coverage * 100).toFixed(0)}%.</p>}
    </>
  );
}
