import { Bar, BarChart, CartesianGrid, Cell, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { money } from "../lib/cloud";

/* eslint-disable @typescript-eslint/no-explicit-any */

/** Waterfall of the cost delta plus every calculation receipt: inputs → rule versions → output, with the formula source. */
export function CostTrace({ cost, trace, onReverify, replay }: { cost: any; trace: any[]; onReverify: () => void; replay: any }) {
  const c = cost?.cost ?? {};
  const steps = [
    { name: "Re-price open orders", v: c.reprice_open_orders ?? 0 },
    { name: "Scrap work in progress", v: c.scrap_wip ?? 0 },
    { name: "Obsolete stock (net)", v: c.obsolete_inventory ?? 0 },
  ];
  let run = 0;
  const data = steps.map((s) => { const d = { name: s.name, base: run, v: s.v }; run += s.v; return d; });
  data.push({ name: "Total", base: 0, v: c.total ?? run });
  return (
    <div className="split">
      <div>
        <div className="panel">
          <div className="panel-head"><h3>How the cost adds up</h3><span className="tag soft">{money(c.total ?? 0)} · {cost?.lead_time_days} days</span></div>
          <div style={{ height: 260 }}>
            <ResponsiveContainer>
              <BarChart data={data} margin={{ top: 20, left: 0, right: 10 }}>
                <CartesianGrid stroke="#eeeef7" vertical={false} /><XAxis dataKey="name" fontSize={11} interval={0} /><YAxis fontSize={11} />
                <Tooltip formatter={(v: number) => money(v)} />
                <Bar dataKey="base" stackId="a" fill="transparent" isAnimationActive={false} />
                <Bar dataKey="v" stackId="a" radius={[6, 6, 0, 0]} animationDuration={900}>
                  {data.map((d) => <Cell key={d.name} fill={d.name === "Total" ? "#2f3169" : "#8284ac"} />)}
                  <LabelList dataKey="v" position="top" formatter={(v: number) => money(v)} fontSize={11} fill="#2f3169" />
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
          <p className="small muted">Material and finish cost per part: {money(cost?.unit_before ?? 0)} → {money(cost?.unit_after ?? 0)}.
            Work orders at risk: {(cost?.work_orders_at_risk ?? []).join(", ") || "none"}.</p>
        </div>
        <div className="panel section">
          <div className="panel-head"><h3>Reproducibility</h3><button className="btn sm" onClick={onReverify}>Re-check from event log</button></div>
          {replay ? (
            <p className="small"><b>{replay.identical ? "Identical." : "DIFFERS."}</b> Rebuilt from {replay.events} events;
              {" "}{replay.receipts_checked} calculations {replay.receipts_reproduced ? "re-computed bit for bit" : "did NOT reproduce"}.</p>
          ) : <p className="small muted">Rebuild the case from its append-only event log and re-run every formula.</p>}
        </div>
      </div>
      <div>
        <h3>Calculation receipts</h3>
        {trace.map((r) => (
          <details key={r.receipt_hash} className="receipt-card">
            <summary>
              <span className={`sev ${r.verified ? "sev-low" : "sev-critical"}`}>{r.verified ? "✓ re-computes" : "✕ mismatch"}</span>
              <code className="val">{r.formula}</code>
              <span className="spacer" />
              <span className="mono tiny muted">{r.receipt_hash.slice(0, 10)}</span>
            </summary>
            <div className="kv">
              {Object.entries(r.inputs).map(([k, v]) => <div key={k}><span>{k.replace(/_/g, " ")}</span><b>{JSON.stringify(v)}</b></div>)}
            </div>
            <div className="kv">
              <div className="wide"><span>Output</span><b>{JSON.stringify(r.output)}</b></div>
              <div><span>Rule versions</span><b>{Object.keys(r.rules ?? {}).length ? Object.entries(r.rules).map(([k, v]) => `${k} v${v}`).join(", ") : "none"}</b></div>
              <div><span>As of</span><b>{r.as_of}</b></div>
              <div><span>Formula hash</span><b>{r.formula_hash}</b></div>
            </div>
            {r.source && <pre className="code">{r.source}</pre>}
          </details>
        ))}
      </div>
    </div>
  );
}
