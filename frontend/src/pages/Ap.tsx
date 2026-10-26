import { useState } from "react";
import { gql, Q } from "../api";
import { label, money, pct } from "../lib/cloud";
import { useAsync } from "../lib/useAsync";

interface Inv {
  id: string; vendor: string; status: string; action: string; explanation: string; groundedness: number; anomaly_z: number;
  parsed: { po_number?: string; total?: number; lines: Array<{ item: string; qty: number; unit_price: number }> };
  exceptions: Array<{ type: string; detail: string }>;
}

export function ApPage() {
  const { data, error } = useAsync(() => gql<{ ap: { invoices: Inv[]; counts: Record<string, number>; touchless_rate: number } }>(Q.ap).then((d) => d.ap));
  const [filter, setFilter] = useState("all");
  const rows = (data?.invoices ?? []).filter((i) => filter === "all" || (filter === "exceptions" ? i.status !== "match" : i.status === "match"));
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Invoice three-way match</h1>
          <p className="lede">Checks each supplier invoice against the purchase order and what was actually received. Matches are queued for approval; everything else goes to accounts payable with the reason.</p>
        </div>
        <div className="row">
          {["all", "exceptions", "matched"].map((f) => <button key={f} className={`btn ${filter === f ? "primary" : ""}`} onClick={() => setFilter(f)}>{label(f)}</button>)}
        </div>
      </div>
      {error && <div className="err">{error}</div>}
      {data && (
        <>
          <div className="figures">
            <div className="figure"><b>{pct(data.touchless_rate, 0)}</b><span>matched with no manual work</span></div>
            {Object.entries(data.counts).filter(([k]) => k !== "match").map(([k, v]) => (
              <div key={k} className="figure alert"><b>{v}</b><span>{label(k)}</span></div>))}
          </div>
          <div className="panel flush section">
            <table>
              <thead><tr><th>Invoice</th><th>Vendor</th><th>PO</th><th className="num">Total</th><th>Result</th><th>Why</th></tr></thead>
              <tbody>{rows.map((i) => (
                <tr key={i.id}>
                  <td>{i.id}</td><td>{i.vendor}</td><td>{i.parsed.po_number ?? "—"}</td>
                  <td className="num">{i.parsed.total ? money(i.parsed.total) : "—"}{Math.abs(i.anomaly_z) > 3.5 && <div className="src">unusual amount for vendor</div>}</td>
                  <td><span className={`status ${i.status === "match" ? "auto_accepted" : "missing"}`}>{i.status === "match" ? "Queued for approval" : label(i.status)}</span></td>
                  <td className="small">{i.explanation.replace(/\s*\[c:[^\]]+\]/g, "")}</td>
                </tr>))}</tbody>
            </table>
          </div>
          <p className="small muted">Payment is never released by the system. A named person approves every payment run.</p>
        </>
      )}
    </>
  );
}
