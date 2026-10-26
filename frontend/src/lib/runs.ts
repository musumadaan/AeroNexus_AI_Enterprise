import { useCallback, useEffect, useRef, useState } from "react";
import { rest, wsUrl } from "../api";

export interface RunEvent { seq: number; ts: number; t_ms: number; type: string; node: string | null; message: string; level: string; data: unknown }
export interface NodeState { status: string; attempts: number; ms?: number; label?: string; type?: string; error?: string; output?: unknown; last_error?: string }
export interface ApprovalState {
  node: string; status: string; title: string; requested: number; proposals?: Array<{ id: string; tool: string; summary: string; args: Record<string, unknown> }>;
  decided_by?: string; note?: string; require_distinct_approver?: boolean; timeout_min?: number; approval_token?: string; context?: Record<string, unknown>;
}
export interface RunView {
  id: string; kind: string; name: string; mode: string; status: string; created_by: string; started: number; ended: number | null;
  events: RunEvent[]; nodes: Record<string, NodeState>; outputs: Record<string, unknown>; approvals: Record<string, ApprovalState>;
  metrics: Record<string, unknown>; inputs: Record<string, unknown>; definition?: Record<string, unknown>; replay_of?: string | null;
  faults?: Record<string, unknown>; error?: string | null; duration_ms?: number;
}

export const DONE = new Set(["completed", "failed", "rejected"]);

/** Live view of one run: WebSocket stream for events and node states, REST for full outputs and approval tokens. */
export function useRun(runId: string | null) {
  const [run, setRun] = useState<RunView | null>(null);
  const [live, setLive] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const refresh = useCallback(async () => {
    if (!runId) return;
    try { setRun(await rest<RunView>(`/api/runs/${runId}`)); } catch { /* transient */ }
  }, [runId]);

  useEffect(() => {
    setRun(null);
    if (!runId) return;
    let closed = false;
    void refresh();
    let ws: WebSocket;
    try { ws = new WebSocket(wsUrl(`/ws/runs/${runId}`)); } catch { return; }
    wsRef.current = ws;
    ws.onopen = () => setLive(true);
    ws.onclose = () => { setLive(false); if (!closed) void refresh(); };
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.type === "update") {
        setRun((r) => {
          const base: RunView = r ?? ({ id: runId, events: [], nodes: {}, outputs: {}, approvals: {}, metrics: {} } as unknown as RunView);
          const seen = new Set(base.events.map((e) => e.seq));
          const evs = [...base.events, ...(msg.events as RunEvent[]).filter((e) => !seen.has(e.seq))];
          const approvals = { ...msg.approvals };
          for (const [k, v] of Object.entries(base.approvals ?? {})) if (v.approval_token && approvals[k]?.status === "pending") approvals[k].approval_token = v.approval_token;
          return { ...base, events: evs, nodes: msg.nodes, status: msg.status, approvals, metrics: msg.metrics };
        });
        if ((msg.events as RunEvent[]).some((e) => ["approval_required", "node_completed", "run_finished", "paused"].includes(e.type))) void refresh();
      }
      if (msg.type === "done") void refresh();
    };
    return () => { closed = true; ws.close(); };
  }, [runId, refresh]);

  return { run, live, refresh };
}

export async function decideRun(runId: string, node: string, approve: boolean, note: string, token: string) {
  return rest<{ ok: boolean; status: string }>(`/api/runs/${runId}/approve`, { body: { node, approve, note, token } });
}

export function fmtMs(ms?: number | null) {
  if (ms === undefined || ms === null) return "—";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
}
