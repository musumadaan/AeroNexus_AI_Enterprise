import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Area, CartesianGrid, ComposedChart, Legend, Line, LineChart, ReferenceDot, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { rest, wsUrl } from "../api";
import { EngineCanvas } from "../components/EngineModel";
import { Icon } from "../components/Icons";
import { getMeta, getTelemetry, statusOf, type Injection, type Telemetry, type TwinMeta } from "../lib/twin";

const SENSOR_COLORS = ["#2f3169", "#53558c", "#8284ac", "#b5b6d6"];
const DASH = ["", "6 3", "2 2", "8 3 2 3"];

export function SimulatorPage() {
  const [params] = useSearchParams();
  const [meta, setMeta] = useState<TwinMeta | null>(null);
  const [subset, setSubset] = useState(params.get("subset") ?? "FD001");
  const [unit, setUnit] = useState(Number(params.get("unit") ?? 20));
  const [inject, setInject] = useState<Injection[]>([]);
  const [draft, setDraft] = useState<Injection>({ type: "spike", sensor: "s11", start: 80, magnitude: 6 });
  const [t, setT] = useState<Telemetry | null>(null);
  const [base, setBase] = useState<Telemetry | null>(null);
  const [i, setI] = useState(0);
  const [playing, setPlaying] = useState(true);
  const [speed, setSpeed] = useState(10);
  const [sensors, setSensors] = useState<string[]>(["s4", "s11", "s7", "s12"]);
  const [linked, setLinked] = useState<"ws" | "local" | "off">("off");
  const [err, setErr] = useState<string | null>(null);
  const [fired, setFired] = useState<string | null>(null);
  const ws = useRef<WebSocket | null>(null);

  useEffect(() => { getMeta().then(setMeta).catch(() => undefined); }, []);
  useEffect(() => {
    setErr(null);
    getTelemetry(subset, unit).then(setBase).catch((e) => setErr(e.message));
    getTelemetry(subset, unit, inject).then((x) => { if (!x.available) setErr(x.message ?? "not available"); else setT(x); })
      .catch((e) => setErr(e.message));
  }, [subset, unit, inject]);

  // the server streams cycles over a WebSocket; if it can't connect, a local clock replays the same frames
  useEffect(() => {
    if (!t) return;
    setI(0); setPlaying(true);
    let timer: number | undefined;
    const url = wsUrl(`/ws/telemetry?subset=${subset}&unit=${unit}&speed=${speed}&inject=${encodeURIComponent(JSON.stringify(inject))}`);
    let sock: WebSocket | null = null;
    try { sock = new WebSocket(url); } catch { sock = null; }
    ws.current = sock;
    const local = () => {
      setLinked("local");
      timer = window.setInterval(() => setI((x) => Math.min(x + 1, (t?.frames.length ?? 1) - 1)), 1000 / speed);
    };
    if (sock) {
      sock.onopen = () => setLinked("ws");
      sock.onmessage = (m) => {
        const msg = JSON.parse(m.data);
        if (msg.type === "frame") setI(msg.i);
        if (msg.type === "end") setPlaying(false);
      };
      sock.onerror = () => { if (!timer) local(); };
    } else local();
    return () => { sock?.close(); if (timer) clearInterval(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [t]);

  const send = (msg: Record<string, unknown>) => { if (ws.current?.readyState === 1) ws.current.send(JSON.stringify(msg)); };
  const togglePlay = () => { send({ cmd: playing ? "pause" : "play" }); setPlaying(!playing); };
  const seek = (k: number) => { if (!t) return; setI(k); send({ cmd: "seek", cycle: t.frames[k].cycle }); };
  useEffect(() => { send({ cmd: "speed", speed }); }, [speed]);

  const f = t?.frames[i];
  const data = useMemo(() => {
    if (!t) return [];
    return t.frames.slice(0, i + 1).map((fr, k) => ({
      cycle: fr.cycle, rul: fr.rul, band: [fr.low, fr.high], truth: fr.true_rul, health: fr.health,
      baseline: inject.length ? base?.frames[k]?.rul : undefined,
      ...Object.fromEntries(sensors.map((s) => [s, fr.z[s] ?? null])),
    }));
  }, [t, i, sensors, base, inject.length]);
  const events = (t?.events ?? []).filter((e) => f && e.cycle <= f.cycle);
  const alerts = events.filter((e) => e.severity === "alert");
  const thr = t?.health_threshold ?? 0.5;
  const sensorName = (s: string) => meta?.sensors[s]?.name ?? s;

  const fire = async () => {
    try {
      const r = await rest<{ started: Array<{ name: string }> }>("/api/triggers/fire", { body: { type: "sensor_anomaly", payload: { subset, unit } } });
      setFired(r.started.length ? `Workflow started: ${r.started.map((s) => s.name).join(", ")}` : "No enabled workflow listens for sensor anomalies.");
    } catch (e) { setFired(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <p className="eyebrow">Predictive maintenance · simulator</p>
          <h1>Maintenance simulator</h1>
          <p className="lede">Replay an engine's sensor history as live telemetry, watch the remaining-life estimate and its uncertainty band
            update every cycle, and inject faults to see how the models and the anomaly detectors react.</p>
        </div>
        <div className="row">
          <select value={subset} onChange={(e) => { setSubset(e.target.value); setUnit(1); }} aria-label="Fleet">
            {["FD001", "FD002", "FD003", "FD004"].map((s) => <option key={s}>{s}</option>)}
          </select>
          <select value={unit} onChange={(e) => setUnit(Number(e.target.value))} aria-label="Engine">
            {(meta?.engines[subset] ?? [unit]).map((u) => <option key={u} value={u}>Engine #{u}</option>)}
          </select>
        </div>
      </div>
      {err && <div className="err">{err}</div>}

      <div className="tiles">
        <div className="tile dark"><span className="k">Cycle</span><b>{f?.cycle ?? "—"}</b><span>of {t?.frames[t.frames.length - 1]?.cycle ?? "—"} · {linked === "ws" ? "streamed over WebSocket" : linked === "local" ? "local replay" : "connecting"}</span></div>
        <div className="tile"><span className="k">Remaining life</span><b>{f?.rul ?? "—"}</b><span>90% range {f?.low}–{f?.high} cycles</span></div>
        <div className="tile"><span className="k">Health score</span><b>{f?.health.toFixed(3) ?? "—"}</b><span>{f && f.health > thr ? "above" : "below"} the healthy limit {thr.toFixed(3)}</span>
          <i className="bar" style={{ width: `${Math.min(100, ((f?.health ?? 0) / (thr * 1.4)) * 100)}%`, background: "#2f3169" }} /></div>
        <div className={`tile ${alerts.length ? "dark" : ""}`}><span className="k">Alerts</span><b>{alerts.length}</b><span>{events.length} detector events so far</span></div>
        <div className="tile"><span className="k">Mode</span><b style={{ fontSize: 22 }}>{inject.length ? "Simulated faults" : "Recorded history"}</b><span>{inject.length ? `${inject.length} injected` : "NASA C-MAPSS test engine"}</span></div>
      </div>

      <div className="panel section">
        <div className="row">
          <button className="btn primary" onClick={togglePlay} disabled={!t}><Icon name={playing ? "pause" : "play"} size={14} />{playing ? "Pause" : "Play"}</button>
          <button className="btn" onClick={() => seek(0)} disabled={!t}><Icon name="replay" size={14} /> Restart</button>
          <input type="range" min={0} max={(t?.frames.length ?? 1) - 1} value={i} onChange={(e) => seek(Number(e.target.value))} style={{ flex: 1 }} aria-label="Seek" />
          <label className="small muted row" style={{ gap: 6 }}>Speed
            <input type="range" min={2} max={40} value={speed} onChange={(e) => setSpeed(Number(e.target.value))} aria-label="Speed" /> {speed} cycles/s
          </label>
        </div>
      </div>

      <div className="split-r section">
        <div style={{ display: "grid", gap: 16 }}>
          <div className="panel">
            <div className="panel-head"><h3>Remaining useful life with 90% uncertainty band</h3>
              <span className="tiny muted">band = split-conformal interval · dashed = true RUL (evaluation only){inject.length ? " · faint = without faults" : ""}</span></div>
            <div style={{ height: 250 }}>
              <ResponsiveContainer>
                <ComposedChart data={data} margin={{ left: -14, right: 10, top: 6 }}>
                  <CartesianGrid stroke="#eeeef7" />
                  <XAxis dataKey="cycle" type="number" domain={[1, t?.frames[t.frames.length - 1]?.cycle ?? 200]} fontSize={11} stroke="#8284ac" />
                  <YAxis domain={[0, 130]} fontSize={11} stroke="#8284ac" />
                  <Tooltip />
                  <Area dataKey="band" fill="#b5b6d6" stroke="none" fillOpacity={0.55} name="90% interval" isAnimationActive={false} />
                  <ReferenceLine y={30} stroke="#53558c" strokeDasharray="4 4" label={{ value: "inspection threshold", fontSize: 10, fill: "#53558c", position: "insideTopRight" }} />
                  {inject.length > 0 && <Line dataKey="baseline" stroke="#8284ac" strokeOpacity={0.6} dot={false} strokeWidth={1.5} name="without faults" isAnimationActive={false} />}
                  <Line dataKey="truth" stroke="#53558c" strokeDasharray="5 4" dot={false} strokeWidth={1.4} name="true RUL" isAnimationActive={false} />
                  <Line dataKey="rul" stroke="#2f3169" dot={false} strokeWidth={2.4} name="predicted RUL" isAnimationActive={false} />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          </div>
          <div className="panel">
            <div className="panel-head"><h3>Health score and anomaly detection</h3><span className="tiny muted">IsolationForest fitted on healthy early-life windows</span></div>
            <div style={{ height: 190 }}>
              <ResponsiveContainer>
                <LineChart data={data} margin={{ left: -14, right: 10, top: 6 }}>
                  <CartesianGrid stroke="#eeeef7" />
                  <XAxis dataKey="cycle" type="number" domain={[1, t?.frames[t.frames.length - 1]?.cycle ?? 200]} fontSize={11} stroke="#8284ac" />
                  <YAxis fontSize={11} stroke="#8284ac" domain={["auto", "auto"]} />
                  <Tooltip />
                  <ReferenceLine y={thr} stroke="#2f3169" strokeDasharray="5 3" label={{ value: "healthy limit", fontSize: 10, fill: "#2f3169", position: "insideTopLeft" }} />
                  <Line dataKey="health" stroke="#53558c" dot={false} strokeWidth={2} isAnimationActive={false} name="health score" />
                  {events.map((e) => {
                    const fr = t!.frames.find((x) => x.cycle === e.cycle);
                    return fr ? <ReferenceDot key={`${e.kind}${e.cycle}${e.sensor}`} x={e.cycle} y={fr.health} r={e.severity === "alert" ? 7 : 5}
                                              fill={e.severity === "alert" ? "#2f3169" : "#fff"} stroke="#2f3169" strokeWidth={2} /> : null;
                  })}
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
          <div className="panel">
            <div className="panel-head"><h3>Sensor telemetry (regime-normalised z-scores)</h3>
              <div className="row" style={{ gap: 4 }}>
                {(t?.key_sensors ?? []).map((s) => (
                  <button key={s} className={`tag ${sensors.includes(s) ? "strong" : ""}`} style={{ cursor: "pointer" }}
                          onClick={() => setSensors((x) => x.includes(s) ? x.filter((y) => y !== s) : [...x, s].slice(-4))}>{sensorName(s)}</button>
                ))}
              </div></div>
            <div style={{ height: 200 }}>
              <ResponsiveContainer>
                <LineChart data={data} margin={{ left: -14, right: 10, top: 6 }}>
                  <CartesianGrid stroke="#eeeef7" />
                  <XAxis dataKey="cycle" type="number" domain={[1, t?.frames[t.frames.length - 1]?.cycle ?? 200]} fontSize={11} stroke="#8284ac" />
                  <YAxis fontSize={11} stroke="#8284ac" />
                  <Tooltip /><Legend wrapperStyle={{ fontSize: 12 }} />
                  {sensors.map((s, k) => <Line key={s} dataKey={s} name={sensorName(s)} stroke={SENSOR_COLORS[k % 4]} strokeDasharray={DASH[k % 4]}
                                               dot={false} strokeWidth={1.6} isAnimationActive={false} />)}
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>

        <div style={{ display: "grid", gap: 14, alignContent: "start" }}>
          <div className="twin-stage short">
            {f && <EngineCanvas scores={f.components} labels={false} spin={Math.max(0.2, f.rul / 125)}
                                alertIds={(meta?.components ?? []).filter((c) => statusOf(f.components[c.id]) === "poor").map((c) => c.id)} />}
            <div className="twin-legend"><span>Worn</span><i /><span>Healthy</span></div>
          </div>
          <div className="panel">
            <h3>Module condition</h3>
            {(meta?.components ?? []).map((c) => (
              <div key={c.id} style={{ marginBottom: 6 }}>
                <div className="row small" style={{ justifyContent: "space-between" }}><span>{c.label}</span>
                  <b className="mono">{f?.components[c.id] === null || f?.components[c.id] === undefined ? "n/a" : Math.round(f.components[c.id] as number)}</b></div>
                <div className="health-bar"><i style={{ width: `${f?.components[c.id] ?? 0}%` }} /></div>
              </div>
            ))}
          </div>
          <div className="panel">
            <div className="panel-head"><h3>Inject a fault</h3><span className="tag soft">simulated</span></div>
            <div className="two" style={{ gap: 8 }}>
              <label className="field">Type<select value={draft.type} onChange={(e) => setDraft({ ...draft, type: e.target.value as Injection["type"] })}>
                <option value="spike">Spike (one cycle)</option><option value="drift">Drift (ramp)</option>
                <option value="stuck">Stuck sensor</option><option value="noise">Extra noise</option></select></label>
              <label className="field">Sensor<select value={draft.sensor} onChange={(e) => setDraft({ ...draft, sensor: e.target.value })}>
                {(t?.sensors ?? []).map((s) => <option key={s} value={s}>{sensorName(s)} · {meta?.sensors[s]?.label}</option>)}</select></label>
              <label className="field">From cycle<input type="number" value={draft.start} onChange={(e) => setDraft({ ...draft, start: Number(e.target.value) })} /></label>
              <label className="field">Size (× sensor spread)<input type="number" step="0.5" value={draft.magnitude} onChange={(e) => setDraft({ ...draft, magnitude: Number(e.target.value) })} /></label>
            </div>
            <div className="row" style={{ marginTop: 8 }}>
              <button className="btn primary sm" disabled={inject.length >= 4} onClick={() => setInject([...inject, draft])}><Icon name="plus" size={14} />Add and replay</button>
              {inject.length > 0 && <button className="btn sm" onClick={() => setInject([])}>Clear faults</button>}
            </div>
            {inject.map((x, k) => <div key={k} className="tiny muted" style={{ marginTop: 4 }}>• {x.type} on {sensorName(x.sensor)} from cycle {x.start} (×{x.magnitude})</div>)}
          </div>
          <div className="panel">
            <div className="panel-head"><h3>Live detector feed</h3>
              {alerts.length > 0 && <button className="btn sm primary" onClick={fire}><Icon name="bolt" size={14} />Send to workflows</button>}</div>
            {fired && <p className="tiny muted">{fired} · <Link to="/builder">open builder</Link></p>}
            <div className="timeline" style={{ maxHeight: 240 }}>
              {events.slice().reverse().map((e) => (
                <div key={`${e.kind}${e.cycle}${e.sensor}`} className={`ev lv-${e.severity === "alert" ? "error" : "warn"}`} style={{ gridTemplateColumns: "54px 74px 1fr" }}>
                  <span className="t">c{e.cycle}</span><span className="ty">{e.kind.replace("_", " ")}</span><span className="msg">{e.message}</span>
                </div>
              ))}
              {!events.length && <div className="empty small">No events yet.</div>}
            </div>
          </div>
        </div>
      </div>
      {t && <p className="tiny muted">{t.notes.join(" ")}</p>}
    </>
  );
}
