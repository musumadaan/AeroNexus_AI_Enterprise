import { useState } from "react";
import { gql, Q } from "../api";
import { Grounded } from "../components/Grounded";
import { label } from "../lib/cloud";
import { useAsync } from "../lib/useAsync";

interface Row {
  tail: string; model: string; ad: string; component: string; status: string; current_hours: number; next_due: number | null;
  remaining: number | null; needs_review: boolean; confidence: number; evidence: { kind: string } | null;
  explanation: { text: string; groundedness: number; decision: string; sources: Record<string, string> };
}

const STATUS_COPY: Record<string, string> = { overdue: "Overdue", due_soon: "Due soon", compliant: "Compliant", no_record: "No record" };

export function MroPage() {
  const { data, error } = useAsync(() => gql<{ mro: { rows: Row[]; counts: Record<string, number>; disclaimer: string } }>(Q.mro).then((d) => d.mro));
  const [sel, setSel] = useState<number>(0);
  const r = data?.rows[sel];
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Airworthiness directive tracker</h1>
          <p className="lede">Matches maintenance logbook entries to directives, works out when each is next due, and sends anything uncertain to a qualified reviewer.</p>
        </div>
      </div>
      {error && <div className="err">{error}</div>}
      {data && (
        <>
          <p className="small muted">{data.disclaimer}</p>
          <div className="figures">
            {["overdue", "due_soon", "no_record", "compliant"].map((k) => (
              <div key={k} className={`figure ${k !== "compliant" && data.counts[k] ? "alert" : ""}`}><b>{data.counts[k] ?? 0}</b><span>{STATUS_COPY[k]}</span></div>
            ))}
          </div>
          <div className="split section">
            <div className="panel flush">
              <table>
                <thead><tr><th>Aircraft</th><th>Directive</th><th>Status</th><th className="num">Hours left</th><th>Evidence</th></tr></thead>
                <tbody>{data.rows.map((x, i) => (
                  <tr key={i} className={`clickable ${sel === i ? "selected" : ""}`} onClick={() => setSel(i)}>
                    <td>{x.tail}<div className="src">{x.model}</div></td>
                    <td>{x.ad}<div className="src">{x.component}</div></td>
                    <td><span className={`status ${x.status === "compliant" ? "auto_accepted" : x.status === "due_soon" ? "needs_review" : "missing"}`}>{STATUS_COPY[x.status]}</span>
                      {x.needs_review && <div className="src">needs review</div>}</td>
                    <td className="num">{x.remaining ?? "—"}</td>
                    <td className="small">{x.evidence ? (x.evidence.kind === "explicit_ad_reference" ? "Quotes the AD number" : "Matched by part and action") : "None found"}</td>
                  </tr>))}</tbody>
              </table>
            </div>
            {r && (
              <div className="panel">
                <h2>{r.tail} · {r.ad}</h2>
                <Grounded text={r.explanation.text} groundedness={r.explanation.groundedness} decision={r.explanation.decision}
                          sources={Object.entries(r.explanation.sources).map(([id, text]) => ({ id, text, title: label(id.split(":")[0]), section: "", score: 1, source: "" }))} />
              </div>
            )}
          </div>
        </>
      )}
    </>
  );
}
