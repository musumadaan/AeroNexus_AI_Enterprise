import { useState } from "react";
import { gql, Q, type Approval } from "../api";
import { label } from "../lib/cloud";

const RISK_COPY: Record<string, string> = {
  WRITE: "Writes to ERP/CRM",
  EXTERNAL: "Contacts a customer",
};

export function Approvals({ items, onChange }: { items: Approval[]; onChange: () => void; approver?: string }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  if (!items.length) return <div className="empty">Nothing waiting for approval. Run a module to create proposals.</div>;
  const decide = async (id: string, ok: boolean) => {
    setBusy(id);
    setErr(null);
    try {
      await gql(Q.decide, { id, ok });
      onChange();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };
  return (
    <div>
      {err && <div className="err">{err}</div>}
      {items.map((a) => (
        <div key={a.id} className="approval">
          <div>
            <div><b>{label(a.tool.split(".")[1])}</b> <span className="risk">{RISK_COPY[a.risk] ?? a.risk}</span></div>
            <div className="small muted">{a.reason || "Proposed by the agent"} · {a.module || "manual"}</div>
            <pre>{formatArgs(a.args)}</pre>
          </div>
          {a.status === "pending" ? (
            <div className="row" style={{ alignSelf: "start" }}>
              <button className="btn" disabled={busy === a.id} onClick={() => decide(a.id, false)}>Reject</button>
              <button className="btn primary" disabled={busy === a.id} onClick={() => decide(a.id, true)}>Approve</button>
            </div>
          ) : <span className="small muted">{label(a.status)} by {a.decidedBy}</span>}
        </div>
      ))}
    </div>
  );
}

function formatArgs(args: Record<string, unknown>): string {
  return Object.entries(args).map(([k, v]) => `${label(k)}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join("\n");
}
