import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { gql, Q } from "../api";
import { StatusPill } from "../components/StatusPill";
import { label } from "../lib/cloud";
import { useAsync } from "../lib/useAsync";

interface CaseRow { id: string; kind: string; title: string; status: string | null; assignee: string | null; updated: number; severity?: string }

const FILTERS = [null, "needs_review", "verified", "blocked", "approved", "executed", "rejected"] as const;

export function WorkQueuePage() {
  const [status, setStatus] = useState<string | null>(null);
  const nav = useNavigate();
  const { data, error, reload } = useAsync(() => gql<{ cases: CaseRow[] }>(Q.cases, { s: status }).then((d) => d.cases), [status]);
  const open = (c: CaseRow) => (c.kind === "change_order" ? nav(`/change?case=${c.id}`) : nav(`/pdm?case=${c.id}`));
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Work queue</h1>
          <p className="lede">Every workflow run is a case with an explicit status. Start with what needs review or is blocked.</p>
        </div>
        <button className="btn" onClick={() => void reload()}>Refresh</button>
      </div>
      <div className="row" style={{ marginBottom: 14 }}>
        {FILTERS.map((f) => (
          <button key={f ?? "all"} className={`btn ${status === f ? "primary" : ""}`} onClick={() => setStatus(f)}>{f ? label(f) : "All"}</button>
        ))}
      </div>
      {error && <div className="err">{error}</div>}
      <div className="panel flush">
        {data && !data.length ? <div className="empty">No cases here yet. Compare two revisions on the change-order page, or propose
          an inspection from predictive maintenance.</div> : (
          <table>
            <thead><tr><th>Case</th><th>Type</th><th>Title</th><th>Severity</th><th>Status</th><th>Updated</th></tr></thead>
            <tbody>{(data ?? []).map((c) => (
              <tr key={c.id} className="clickable" onClick={() => open(c)}>
                <td>{c.id}</td><td>{label(c.kind)}</td><td>{c.title}</td><td>{c.severity ? label(c.severity) : "—"}</td>
                <td><StatusPill status={c.status} /></td><td className="small muted">{new Date(c.updated * 1000).toLocaleString()}</td>
              </tr>))}</tbody>
          </table>
        )}
      </div>
    </>
  );
}
