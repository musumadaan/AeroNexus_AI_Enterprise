import { gql, identity, Q, type Approval } from "../api";
import { Approvals } from "../components/Approvals";
import { label } from "../lib/cloud";
import { useAsync } from "../lib/useAsync";

export function ReviewPage() {
  const me = identity();
  const who = me.role === "approver" || me.role === "admin" ? me.user : "";
  const pend = useAsync(() => gql<{ approvals: Approval[] }>(Q.approvals, { s: "pending" }).then((d) => d.approvals));
  const log = useAsync(() => gql<{ audit: Array<{ ts: number; actor: string; action: string; detail: Record<string, unknown> }>; outbox: Array<Record<string, string>> }>(Q.audit));
  const refresh = () => { void pend.reload(); void log.reload(); };
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Approval queue</h1>
          <p className="lede">The agents draft; people decide. Anything that writes to the ERP or CRM, or contacts a customer, waits here for a named approver.</p>
        </div>
        <span className="small muted">{who ? `Approving as ${who} (${me.role})` : "Read-only: switch to an approver role and enter your name (bottom left)"}</span>
      </div>
      {pend.error && <div className="err">{pend.error}</div>}
      <div className="split">
        <div className="panel">
          {!who.trim() ? <div className="empty">Only a named approver can decide. Every decision is recorded with their name.</div>
            : <Approvals items={pend.data ?? []} onChange={refresh} approver={who.trim()} />}
        </div>
        <div>
          <div className="panel">
            <h3>Sent on approval</h3>
            {(log.data?.outbox ?? []).length ? log.data!.outbox.slice(0, 5).map((m, i) => (
              <div key={i} className="issue"><i /><div><b>{label(m.channel)}</b> to {m.to}<div className="src">{(m.subject ?? m.script ?? "").slice(0, 120)}</div></div></div>
            )) : <p className="muted small">Nothing sent yet.</p>}
          </div>
          <div className="panel section">
            <h3>Audit trail</h3>
            {(log.data?.audit ?? []).slice(0, 14).map((e, i) => (
              <div key={i} className="small" style={{ padding: "4px 0", borderBottom: "1px solid #eef0f3" }}>
                <b>{e.actor}</b> {e.action.replace(/_/g, " ")} {String(e.detail.tool ?? "")}
                <span className="muted"> · {new Date(e.ts * 1000).toLocaleTimeString()}</span>
              </div>))}
          </div>
        </div>
      </div>
    </>
  );
}
