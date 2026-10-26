import { useMemo, useState } from "react";
import { identity } from "../api";
import { decideRun, fmtMs, type RunView } from "../lib/runs";
import { Icon } from "./Icons";

const FILTERS: Record<string, string[] | null> = {
  All: null,
  Decisions: ["decision", "guardrail", "calculation", "retrieval"],
  "Tool calls": ["tool_call", "db_write"],
  "Errors & retries": ["retry", "node_failed", "fallback"],
  Approvals: ["approval_required", "approved", "rejected", "approval_simulated", "paused"],
  Steps: ["node_started", "node_completed", "step"],
};

export function RunTimeline({ run, height = 420 }: { run: RunView | null; height?: number }) {
  const [f, setF] = useState("All");
  const evs = useMemo(() => {
    const allow = FILTERS[f];
    return (run?.events ?? []).filter((e) => !allow || allow.includes(e.type)).slice().reverse();
  }, [run?.events, f]);
  if (!run) return <div className="empty small">Start a run to see its timeline here.</div>;
  return (
    <div>
      <div className="row" style={{ marginBottom: 8 }}>
        <div className="seg">{Object.keys(FILTERS).map((k) => <button key={k} className={f === k ? "on" : ""} onClick={() => setF(k)}>{k}</button>)}</div>
        <span className="spacer" />
        <span className="small muted">{run.events.length} events</span>
      </div>
      <div className="timeline" style={{ maxHeight: height }} aria-live="polite">
        {evs.map((e) => (
          <div key={e.seq} className={`ev lv-${e.level}`}>
            <span className="t">{fmtMs(e.t_ms)}</span>
            <span className="ty">{e.type.replace(/_/g, " ")}</span>
            <span className="msg">{e.node && <span className="nd">{e.node}</span>}{e.message}</span>
          </div>
        ))}
        {!evs.length && <div className="empty small">No events of this kind yet.</div>}
      </div>
    </div>
  );
}

export function ApprovalGate({ run, onDone }: { run: RunView; onDone?: () => void }) {
  const pending = Object.values(run.approvals ?? {}).filter((a) => a.status === "pending");
  const decided = Object.values(run.approvals ?? {}).filter((a) => a.status !== "pending");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const me = identity();
  const canApprove = me.role === "approver" || me.role === "admin";
  if (!pending.length && !decided.length) return null;
  const go = async (node: string, token: string | undefined, ok: boolean) => {
    setBusy(true); setErr(null);
    try { await decideRun(run.id, node, ok, note, token ?? ""); setNote(""); onDone?.(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  return (
    <div style={{ display: "grid", gap: 10 }}>
      {pending.map((a) => (
        <div key={a.node} className="gatecard" role="region" aria-label="Approval checkpoint">
          <h3><Icon name="lock" /> Approval needed</h3>
          <div className="val">{a.title}</div>
          {!!a.proposals?.length && (
            <ul className="props">{a.proposals.map((p) => (
              <li key={p.id}><b>{p.summary}</b><div className="tiny muted mono">{p.tool}</div></li>))}</ul>
          )}
          <div className="tiny muted" style={{ margin: "6px 0" }}>
            One-time token · role check{a.require_distinct_approver ? " · approver must differ from the person who started the run" : ""}
            {a.timeout_min ? ` · expires after ${Math.round(a.timeout_min / 60)} h` : ""}
          </div>
          {run.mode !== "live" ? <p className="small muted">Test runs are approved by a simulated reviewer.</p> :
            canApprove ? (
              <>
                <textarea aria-label="Approval note" placeholder="Note for the audit trail (what you checked)" value={note}
                          onChange={(e) => setNote(e.target.value)} style={{ width: "100%", height: 54 }} />
                <div className="row" style={{ marginTop: 8 }}>
                  <button className="btn" disabled={busy} onClick={() => go(a.node, a.approval_token, false)}><Icon name="x" /> Reject</button>
                  <button className="btn primary" disabled={busy} onClick={() => go(a.node, a.approval_token, true)}><Icon name="check" /> Approve as {me.user || "unnamed"}</button>
                </div>
                {!me.user && <p className="tiny muted">Enter your name at the bottom left: approvals must come from a named person.</p>}
              </>
            ) : <p className="small muted">Switch to the approver role (bottom left) to decide.</p>}
          {err && <div className="err small" style={{ marginTop: 8 }}>{err}</div>}
        </div>
      ))}
      {decided.map((a) => (
        <div key={a.node} className="note"><b>{a.status === "approved" ? "Approved" : "Rejected"}</b> by {a.decided_by}{a.note ? `: “${a.note}”` : ""} · {a.title}</div>
      ))}
    </div>
  );
}

export function JsonView({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}
