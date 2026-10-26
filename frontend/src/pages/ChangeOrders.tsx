import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { gql, identity, Q, type SheetData } from "../api";
import { CostTrace } from "../components/CostTrace";
import { DependencyGraph } from "../components/DependencyGraph";
import { DimensionMorph, RevisionCompare } from "../components/RevisionCompare";
import { Lifecycle, StatusPill } from "../components/StatusPill";
import { label, money } from "../lib/cloud";
import { useAsync } from "../lib/useAsync";

/* eslint-disable @typescript-eslint/no-explicit-any */
type CaseData = any;

const SEV_COPY: Record<string, string> = {
  critical: "Critical: changes a part already in production",
  high: "High: changes material, finish or a critical dimension",
  medium: "Medium: changes quantity or a standard",
  low: "Low: cosmetic, no notifications needed",
};

function download(name: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

export function ChangeOrdersPage() {
  const [params, setParams] = useSearchParams();
  const pairs = useAsync(() => gql<{ changePairs: Array<{ id: string; part_number: string; kind: string[] }> }>(Q.changePairs)
    .then((d) => d.changePairs));
  const [pair, setPair] = useState("CH000");
  const [c, setC] = useState<CaseData | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [replay, setReplay] = useState<any>(null);
  const [tab, setTab] = useState("summary");
  const caseId = params.get("case");

  const load = async (id: string) => {
    setErr(null);
    setReplay(null);
    try {
      const d = await gql<{ case: CaseData }>(Q.case, { id });
      setC(d.case);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  useEffect(() => { if (caseId) void load(caseId); }, [caseId]);
  useEffect(() => {
    setSel(c?.data?.diff?.changes.find((x: any) => x.field !== "revision")?.field ?? null);
  }, [c?.id]);
  const sheets = c?.sheets as { a: SheetData; b: SheetData } | undefined;

  const open = async () => {
    setBusy(true);
    setErr(null);
    try {
      const d = await gql<{ openChangeOrder: CaseData }>(Q.openChange, { id: pair });
      setParams({ case: d.openChangeOrder.id });
      setC(d.openChangeOrder);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  const decide = async (ok: boolean) => {
    setBusy(true);
    setErr(null);
    try {
      const d = await gql<{ decideCase: CaseData }>(Q.decideCase, { id: c.id, ok, note });
      setC(d.decideCase);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  const checkReplay = async () => setReplay((await gql<{ replayCase: any }>(Q.replay, { id: c.id })).replayCase);

  const d = c?.data ?? {};
  const change = d.diff?.changes?.find((x: any) => x.field === sel);
  const me = identity();
  const canApprove = me.role === "approver" || me.role === "admin";

  return (
    <>
      <div className="page-head">
        <div>
          <p className="eyebrow">Flagship workflow</p>
          <h1>Engineering change orders</h1>
          <p className="lede">Compare two drawing revisions, see every affected assembly, order, work order, purchase order and
            customer, with cost and lead time you can re-calculate. Findings that need a person stop the case until someone signs off.</p>
        </div>
        <div className="row">
          <select value={pair} onChange={(e) => setPair(e.target.value)} aria-label="Revision pair">
            {(pairs.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.id} · {p.part_number} ({p.kind.filter((k) => k !== "revision").join(", ")})</option>)}
          </select>
          <button className="btn primary" disabled={busy} onClick={open}>Compare revisions</button>
        </div>
      </div>
      {err && <div className="err">{err}</div>}
      {!c && <div className="panel empty">Pick a revision pair and compare it. The result is saved as a case you can come back to from the work queue.</div>}
      {c && (
        <>
          <div className="panel">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div>
                <h2 style={{ margin: 0 }}>{c.title}</h2>
                <div className="small muted">Case {c.id}{c.assignee ? ` · assigned to ${c.assignee}` : ""}</div>
              </div>
              <StatusPill status={c.status} />
            </div>
            <Lifecycle status={c.status} visited={c.history.filter((h: any) => h.type === "status").map((h: any) => String(h.summary).split("→")[1]?.split(":")[0]?.trim())} />
          </div>

          {d.classify && (
            <div className="figures section">
              <div className="figure"><div style={{ marginBottom: 6 }}><span className={`sev big sev-${d.classify.severity}`}>{label(d.classify.severity)}</span></div><span>{SEV_COPY[d.classify.severity]}</span></div>
              <div className="figure"><b>{d.cost?.available ? money(d.cost.cost.total) : "—"}</b><span>estimated cost impact</span></div>
              <div className="figure"><b>{d.cost?.lead_time_days ?? "—"} days</b><span>to implement</span></div>
              <div className="figure"><b>{(d.impact?.open_orders?.length ?? 0) + (d.impact?.work_orders?.length ?? 0)}</b><span>orders and work orders affected</span></div>
            </div>
          )}

          <div className="tabs section" role="tablist">
            {[["summary", "Summary and decision"], ["compare", "Compare revisions"], ["graph", "Dependency graph"], ["cost", "Cost trace"], ["history", "History"]].map(([k, l]) => (
              <button key={k} className={tab === k ? "on" : ""} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}>{l}</button>))}
          </div>

          {tab === "compare" && sheets && (
            <div className="split-r">
              <div className="panel"><RevisionCompare a={sheets.a} b={sheets.b} change={change} /></div>
              <div style={{ display: "grid", gap: 12, alignContent: "start" }}>
                <div className="panel">
                  <h3>What changed</h3>
                  {(d.diff?.changes ?? []).filter((x: any) => x.field !== "revision").map((x: any) => (
                    <button key={x.field} className={`card ${sel === x.field ? "on" : ""}`} style={{ marginBottom: 8 }} onClick={() => setSel(x.field)}>
                      <div className="tiny muted">{label(x.field)}{x.uncertain ? " · check by eye" : ""}</div>
                      <div className="delta-callout"><span className="big">{x.before ?? "—"}</span><span className="arrow">→</span><span className="big">{x.after ?? "—"}</span>
                        {x.delta_mm !== undefined && <span className="sev sev-high">{x.delta_mm > 0 ? "+" : ""}{x.delta_mm} mm ({(x.delta_pct * 100).toFixed(1)}%)</span>}</div>
                    </button>))}
                </div>
                {change && change.delta_mm !== undefined && <div className="panel"><h3>Section view</h3><DimensionMorph change={change} /></div>}
                {(d.diff?.text_ops ?? []).length > 0 && (
                  <div className="panel"><h3>Text changes on the sheet</h3>
                    {d.diff.text_ops.slice(0, 6).map((o: any, k: number) => <div key={k} className="small mono" style={{ marginBottom: 4 }}>
                      <s style={{ color: "#8284ac" }}>{o.before}</s> → <b>{o.after}</b></div>)}
                    <div className="tiny muted">Similarity {(d.diff.similarity * 100).toFixed(1)}%</div></div>)}
              </div>
            </div>
          )}
          {tab === "graph" && (c.dependency_graph ? <DependencyGraph graph={c.dependency_graph} /> : <div className="empty">No impact data for this case.</div>)}
          {tab === "cost" && (d.cost?.available ? <CostTrace cost={d.cost} trace={c.cost_trace ?? []} onReverify={checkReplay} replay={replay} />
            : <div className="empty">Cost could not be computed: {d.cost?.reason ?? "step not run"}.</div>)}

          {tab === "summary" && (<div className="split">
            <div>
              <div className="panel flush">
                <table>
                  <thead><tr><th>Field</th><th>Rev A</th><th>Rev B</th><th>Change</th></tr></thead>
                  <tbody>{(d.diff?.changes ?? []).map((x: any) => (
                    <tr key={x.field} className={`clickable ${sel === x.field ? "selected" : ""}`} onClick={() => setSel(x.field)}>
                      <td>{label(x.field)}{x.uncertain && <div className="src">low confidence, check by eye</div>}</td>
                      <td>{x.before ?? "—"}</td><td className="val">{x.after ?? "—"}</td>
                      <td className="small">{x.delta_mm !== undefined ? `${x.delta_mm > 0 ? "+" : ""}${x.delta_mm} mm (${(x.delta_pct * 100).toFixed(1)}%)` : ""}</td>
                    </tr>))}</tbody>
                </table>
              </div>
              {sheets && (
                <div className="panel section">
                  <div className="panel-head"><h3>Revision B with the change clouded</h3><button className="btn sm" onClick={() => setTab("compare")}>Animated comparison →</button></div>
                  <RevisionCompare a={sheets.a} b={sheets.b} change={change} />
                </div>
              )}
              <div className="section">
                <h2>Findings</h2>
                {(d.validate?.findings ?? []).length ? d.validate.findings.map((f: any, i: number) => (
                  <div key={i} className={`issue ${f.severity}`}><i /><div>{f.message}</div></div>
                )) : <p className="muted">No findings. Every check on revision B passed.</p>}
              </div>
            </div>
            <div>
              <div className="panel">
                <h2>Affected records</h2>
                <table><tbody>
                  <tr><td>Assemblies</td><td>{(d.impact?.assemblies ?? []).join(", ") || "none"}</td></tr>
                  <tr><td>Sales orders</td><td>{(d.impact?.open_orders ?? []).map((o: any) => `${o.order_id} (${label(o.status)})`).join(", ") || "none"}</td></tr>
                  <tr><td>Work orders</td><td>{(d.impact?.work_orders ?? []).map((w: any) => `${w.wo} (${w.status}, WIP ${w.wip_qty})`).join(", ") || "none"}</td></tr>
                  <tr><td>Material POs</td><td>{(d.impact?.material_pos ?? []).map((p: any) => `${p.po} ${p.kg} kg`).join(", ") || "none"}</td></tr>
                  <tr><td>Customers</td><td>{(d.impact?.accounts ?? []).map((a: any) => a.name).join(", ") || "none"}</td></tr>
                  {d.impact?.new_supplier && <tr><td>New supplier</td><td>{d.impact.new_supplier.name}, {d.impact.new_supplier.lead_days} days lead time</td></tr>}
                </tbody></table>
              </div>
              {d.cost?.available && (
                <div className="panel section">
                  <h2>Cost and schedule</h2>
                  <table><tbody>
                    <tr><td>Material + finish per part</td><td className="num">{money(d.cost.unit_before)} → {money(d.cost.unit_after)}</td></tr>
                    <tr><td>Re-pricing open orders</td><td className="num">{money(d.cost.cost.reprice_open_orders)}</td></tr>
                    <tr><td>Scrap of work in progress</td><td className="num">{money(d.cost.cost.scrap_wip)}</td></tr>
                    <tr><td>Obsolete inventory, net of recovery</td><td className="num">{money(d.cost.cost.obsolete_inventory)}</td></tr>
                    <tr><td><b>Total</b></td><td className="num"><b>{money(d.cost.cost.total)}</b></td></tr>
                    <tr><td>Work orders at risk</td><td className="num">{d.cost.work_orders_at_risk.join(", ") || "none"}</td></tr>
                  </tbody></table>
                  <div className="receipt" style={{ marginTop: 8 }}>
                    Every figure is computed by a versioned formula. Receipts: {[...d.cost.receipts, d.classify?.receipt].filter(Boolean).map((r: any) =>
                      <span key={r.receipt_hash}><code>{r.formula}</code> {r.receipt_hash.slice(0, 8)} · </span>)}
                  </div>
                </div>
              )}
              <div className="panel section">
                <h2>Decision</h2>
                {["verified", "needs_review"].includes(c.status) ? (
                  canApprove ? (
                    <>
                      <textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note for the record (what you checked)"
                                style={{ width: "100%", height: 60 }} aria-label="Decision note" />
                      <div className="row" style={{ marginTop: 8 }}>
                        <button className="btn" disabled={busy} onClick={() => decide(false)}>Reject</button>
                        <button className="btn primary" disabled={busy} onClick={() => decide(true)}>Approve and release ECO</button>
                      </div>
                      <p className="small muted">Approving releases the ECO in the ERP and emails affected customers, as {me.user || "unnamed user"}.</p>
                    </>
                  ) : <p className="muted">Switch to an approver role (bottom left) to approve or reject this change.</p>
                ) : <p className="muted">This case is {label(c.status ?? "new").toLowerCase()}; no decision needed.</p>}
                <div className="row" style={{ marginTop: 10 }}>
                  <button className="btn ghost" onClick={() => download(`${c.id}.md`, c.report.markdown, "text/markdown")}>Download report (.md)</button>
                  <button className="btn ghost" onClick={() => download(`${c.id}.html`, c.report.html, "text/html")}>Download report (.html)</button>
                  <button className="btn ghost" onClick={checkReplay}>Re-check from event log</button>
                </div>
                {replay && <p className="small" style={{ marginBottom: 0 }}>
                  Rebuilt from {replay.events} events: {replay.identical ? "identical to the live case" : "DIFFERS from the live case"};{" "}
                  {replay.receipts_checked} calculations {replay.receipts_reproduced ? "re-computed exactly" : "did NOT reproduce"}.</p>}
              </div>
            </div>
          </div>)}

          {tab === "history" && <div className="panel section">
            <h2>History</h2>
            {c.history.map((h: any, i: number) => (
              <div key={i} className="small" style={{ padding: "4px 0", borderBottom: "1px solid #eef0f3" }}>
                <b>{h.actor}</b> · {h.summary} <span className="muted">· {new Date(h.ts * 1000).toLocaleTimeString()}</span>
              </div>))}
          </div>}
        </>
      )}
    </>
  );
}
