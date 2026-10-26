import { useEffect, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { gql, Q } from "../api";
import { label } from "../lib/cloud";
import { useAsync } from "../lib/useAsync";

interface Engine { unit: number; cycles_flown: number; pred_rul: number; low: number; high: number; true_rul: number; health: number;
  work_order: boolean; reasons: string[] }
interface Fleet { available: boolean; message?: string; subset: string; engines: Engine[]; health_threshold: number; disclaimer: string;
  metrics: { test_rmse_capped: number; interval_coverage_test: number; selected_model: string; health_index_auroc_early_vs_late: number;
    n_train_units: number } }

const SUBSET_COPY: Record<string, string> = { FD001: "FD001 · one operating condition, one fault",
  FD002: "FD002 · six conditions, one fault", FD003: "FD003 · one condition, two faults", FD004: "FD004 · six conditions, two faults" };
const CAP = 125;

export function PdmPage() {
  const [subset, setSubset] = useState("FD001");
  const fleet = useAsync(() => gql<{ pdmFleet: Fleet }>(Q.fleet, { s: subset }).then((d) => d.pdmFleet), [subset]);
  const [unit, setUnit] = useState<number | null>(null);
  const [trend, setTrend] = useState<{ sensors: string[]; series: Array<Record<string, number>> } | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const f = fleet.data;
  useEffect(() => { setUnit(f?.engines?.[0]?.unit ?? null); }, [f?.subset]);
  useEffect(() => {
    if (unit == null) return;
    setMsg(null);
    gql<{ engineTrend: { sensors: string[]; series: Array<Record<string, number>> } }>(Q.trend, { s: subset, u: unit })
      .then((d) => setTrend(d.engineTrend)).catch(() => setTrend(null));
  }, [unit, subset]);
  const e = f?.engines?.find((x) => x.unit === unit);
  const propose = async () => {
    try {
      const d = await gql<{ proposeWorkOrder: { case: string; status: string; proposal: unknown } }>(Q.proposeWO, { s: subset, u: unit });
      setMsg(d.proposeWorkOrder.proposal ? `Case ${d.proposeWorkOrder.case} opened. The work order waits for a technician's approval.`
        : `Case ${d.proposeWorkOrder.case}: no inspection needed under the current rule.`);
    } catch (err) { setMsg(err instanceof Error ? err.message : String(err)); }
  };
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Predictive maintenance</h1>
          <p className="lede">Remaining useful life for each engine with an honest 90% range, trained on NASA's public C-MAPSS run-to-failure data.
            When the low end of the range gets close, an inspection is proposed for a technician to approve.</p>
        </div>
        <select value={subset} onChange={(ev) => setSubset(ev.target.value)} aria-label="C-MAPSS subset">
          {Object.entries(SUBSET_COPY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </div>
      {fleet.error && <div className="err">{fleet.error}</div>}
      {f && !f.available && <div className="err">{f.message}</div>}
      {f?.available && (
        <>
          <p className="small muted">{f.disclaimer} <span className="prov">Public data · NASA C-MAPSS</span></p>
          <div className="figures">
            <div className="figure"><b>{f.metrics.test_rmse_capped}</b><span>cycles typical error (test RMSE)</span></div>
            <div className="figure"><b>{Math.round(f.metrics.interval_coverage_test * 100)}%</b><span>of test engines inside the 90% range</span></div>
            <div className={`figure ${f.engines.filter((x) => x.work_order).length ? "alert" : ""}`}><b>{f.engines.filter((x) => x.work_order).length}</b><span>engines meeting the inspection rule</span></div>
            <div className="figure"><b>{f.metrics.health_index_auroc_early_vs_late}</b><span>health score separation (AUROC)</span></div>
          </div>
          <div className="split section">
            <div className="panel flush" style={{ maxHeight: 620, overflow: "auto" }}>
              <table>
                <thead><tr><th>Engine</th><th className="num">Cycles</th><th>Remaining life (90% range)</th><th>Inspection</th></tr></thead>
                <tbody>{f.engines.map((x) => (
                  <tr key={x.unit} className={`clickable ${unit === x.unit ? "selected" : ""}`} onClick={() => setUnit(x.unit)}>
                    <td>#{x.unit}</td><td className="num">{x.cycles_flown}</td>
                    <td>
                      <div className="rulbar" title={`${x.low}–${x.high} cycles, predicted ${x.pred_rul}, true ${x.true_rul}`}>
                        <i style={{ left: `${(x.low / CAP) * 100}%`, width: `${((x.high - x.low) / CAP) * 100}%` }} />
                        <b style={{ left: `${(x.pred_rul / CAP) * 100}%` }} />
                        <s style={{ left: `${(Math.min(x.true_rul, CAP) / CAP) * 100}%` }} />
                      </div>
                      <div className="src">{x.pred_rul} cycles ({x.low}–{x.high})</div>
                    </td>
                    <td>{x.work_order ? <span className="status missing">Due</span> : <span className="status auto_accepted">Not yet</span>}</td>
                  </tr>))}</tbody>
              </table>
            </div>
            <div>
              {e && (
                <div className="panel">
                  <h2>Engine #{e.unit}</h2>
                  <p style={{ marginTop: 0 }}>Predicted {e.pred_rul} cycles left (90% range {e.low}–{e.high}). Actual from the test labels: {e.true_rul}.</p>
                  {e.reasons.length ? <ul className="small">{e.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
                    : <p className="small muted">The inspection rule is not met.</p>}
                  {trend && (
                    <div style={{ height: 270, marginBottom: 12 }}>
                      <div className="small muted">Degradation-sensitive sensors, normalised per operating condition</div>
                      <ResponsiveContainer>
                        <LineChart data={trend.series} margin={{ left: -18, right: 6 }}>
                          <CartesianGrid stroke="#eef0f3" />
                          <XAxis dataKey="cycle" tick={{ fontSize: 11 }} />
                          <YAxis tick={{ fontSize: 11 }} />
                          <Tooltip />
                          <Legend wrapperStyle={{ fontSize: 12 }} />
                          {trend.sensors.map((s, i) => <Line key={s} dataKey={s} dot={false} stroke={["var(--red)", "var(--ink)", "#8a909c"][i]} strokeWidth={1.5} />)}
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                  )}
                  <div className="row" style={{ marginTop: 10 }}>
                    <button className="btn primary" onClick={propose}>Propose inspection work order</button>
                  </div>
                  {msg && <p className="small">{msg}</p>}
                  <p className="small muted">Model: {label(f.metrics.selected_model)} on {f.metrics.n_train_units} training engines. A certifying technician decides; the model never removes an engine from service.</p>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </>
  );
}
