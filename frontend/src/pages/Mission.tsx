import { Background, Controls, MarkerType, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useEffect, useMemo, useState } from "react";
import { rest } from "../api";
import { nodeTypes } from "../components/FlowNode";
import { Icon } from "../components/Icons";
import { EnterpriseConsole } from "../components/EnterpriseConsole";
import { ApprovalGate, JsonView, RunTimeline } from "../components/RunPanels";
import { StatusPill } from "../components/StatusPill";
import { DONE, fmtMs, useRun, type RunView } from "../lib/runs";

interface Preset { id: string; title: string; description: string; inputs: Record<string, unknown>; faults?: Record<string, unknown>; agents: Record<string, string> }
interface Shape { nodes: Array<{ id: string; label: string }>; edges: Array<{ source: string; target: string; conditional: boolean }> }

const POS: Record<string, [number, number]> = {
  __start__: [-110, 178], planner: [0, 170], drawing_agent: [260, 0], pdm_agent: [260, 115], safety_agent: [260, 230],
  evidence_agent: [260, 345], cost_agent: [510, 0], synthesizer: [740, 170], approval_gate: [980, 170], executor: [1220, 170], __end__: [1460, 178],
};
const CAT: Record<string, [string, string]> = {
  planner: ["supervisor", "LLM routing, validated"], drawing_agent: ["agent", "diff · impact · ERP reads"], pdm_agent: ["agent", "RUL · health · anomalies"],
  safety_agent: ["agent", "calibrated triage · abstains"], evidence_agent: ["agent", "hybrid RAG · cited answer"], cost_agent: ["agent", "re-verifies receipts"],
  synthesizer: ["agent", "deferred join · proposals"], approval_gate: ["human", "interrupt() · named approver"], executor: ["integration", "approved tools only"],
};

export function MissionPage() {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [shape, setShape] = useState<Shape | null>(null);
  const [pick, setPick] = useState("full");
  const [inputs, setInputs] = useState<Record<string, unknown>>({});
  const [faults, setFaults] = useState<Record<string, number>>({});
  const [permanent, setPermanent] = useState<string[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [history, setHistory] = useState<RunView[]>([]);
  const [sel, setSel] = useState<string | null>("synthesizer");
  const [err, setErr] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const { run, live, refresh } = useRun(runId);

  const loadHistory = () => rest<RunView[]>("/api/runs?kind=mission&limit=12").then(setHistory).catch(() => undefined);
  useEffect(() => {
    rest<Preset[]>("/api/mission/presets").then((p) => { setPresets(p); apply(p[0]); }).catch((e) => setErr(e.message));
    rest<Shape>("/api/mission/graph").then(setShape).catch(() => undefined);
    void loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { if (run && DONE.has(run.status)) void loadHistory(); }, [run?.status]);

  const apply = (p: Preset) => {
    setPick(p.id); setInputs(p.inputs);
    const f = (p.faults ?? {}) as Record<string, unknown>;
    setFaults(Object.fromEntries(Object.entries(f).filter(([k]) => k !== "_permanent")) as Record<string, number>);
    setPermanent((f._permanent as string[]) ?? []);
  };
  const start = async () => {
    setErr(null);
    try {
      const r = await rest<{ run_id: string }>("/api/mission/start", { body: { inputs, faults: { ...faults, _permanent: permanent } } });
      setRunId(r.run_id); setSel("planner");
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  const replay = async (id: string) => {
    try { const r = await rest<{ run_id: string }>(`/api/runs/${id}/replay`, { body: {} }); setRunId(r.run_id); } catch (e) { setErr(String(e)); }
  };

  const nodes: Node[] = useMemo(() => (shape?.nodes ?? []).map((n) => {
    const st = run?.nodes?.[n.id];
    const terminal = n.id.startsWith("__");
    const fl = faults[n.id] ? `${faults[n.id]} timeouts` : permanent.includes(n.id) ? "fails" : undefined;
    return {
      id: n.id, type: terminal ? "terminal" : "flow", position: { x: POS[n.id]?.[0] ?? 0, y: POS[n.id]?.[1] ?? 0 },
      data: terminal ? { label: n.id === "__start__" ? "START" : "END" } :
        { label: n.label, category: CAT[n.id]?.[0] ?? "agent", type: n.id, meta: CAT[n.id]?.[1], status: st?.status, attempts: st?.attempts,
          ms: st?.ms, risk: n.id === "executor" ? "write" : undefined, faults: fl },
      selected: sel === n.id,
    };
  }), [shape, run?.nodes, sel, faults, permanent]);

  const edges: Edge[] = useMemo(() => (shape?.edges ?? []).map((e) => {
    const src = run?.nodes?.[e.source]?.status;
    const tgt = run?.nodes?.[e.target]?.status;
    const active = (src === "completed" || e.source === "__start__") && (tgt === "running" || tgt === "retrying" || tgt === "waiting");
    const used = src === "completed" && tgt && !["pending", "skipped"].includes(tgt);
    return {
      id: `${e.source}-${e.target}`, source: e.source, target: e.target, animated: !!(active || (run && !DONE.has(run.status) && used && tgt !== "completed")),
      style: { strokeDasharray: e.conditional ? "6 4" : undefined, opacity: run && !used && !active ? 0.35 : 1, strokeWidth: used ? 2.2 : 1.5 },
      markerEnd: { type: MarkerType.ArrowClosed, color: used ? "#2f3169" : "#8284ac" },
    };
  }), [shape, run]);

  const syn = run?.outputs?.synthesizer as { risk: string; brief: string; lines: string[]; proposals: Array<{ summary: string; tool: string }>; missing: string[]; confidence: number } | undefined;
  const selOut = sel ? run?.outputs?.[sel] : undefined;
  const preset = presets.find((p) => p.id === pick);

  return (
    <>
      <div className="page-head">
        <div>
          <p className="eyebrow">Automation · LangGraph</p>
          <h1>Mission control</h1>
          <p className="lede">A supervisor agent plans the mission, specialist agents work in parallel, a synthesizer joins their findings into
            proposed actions, and nothing reaches the ERP, CMMS or email until a named approver signs off. Every step streams here live.</p>
        </div>
        <div className="row">
          <span className="small muted">{run ? (live ? "● live stream" : "○ stream closed") : ""}</span>
          <button className="btn primary" onClick={start}><Icon name="play" size={14} /> Launch mission</button>
        </div>
      </div>
      {err && <div className="err">{err}</div>}

      <div className="cards">
        {presets.map((p) => (
          <button key={p.id} className={`card ${pick === p.id ? "on" : ""}`} onClick={() => apply(p)}>
            <div className="ico"><Icon name={p.id === "faults" ? "bolt" : p.id === "maintenance" ? "twin" : p.id === "change" ? "change" : "mission"} /></div>
            <h3>{p.title}</h3>
            <div className="small muted">{p.description}</div>
          </button>
        ))}
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <button className="btn ghost sm" onClick={() => setShowForm((x) => !x)}>{showForm ? "Hide" : "Customise"} inputs and fault injection</button>
        {preset && <span className="tiny muted">Inputs: {Object.entries(inputs).map(([k, v]) => `${k}=${String(v).slice(0, 28)}`).join(" · ")}</span>}
      </div>
      {showForm && (
        <div className="panel" style={{ marginTop: 8 }}>
          <div className="four">
            <label className="field">Revision pair<input type="text" value={String(inputs.pair_id ?? "")} onChange={(e) => setInputs({ ...inputs, pair_id: e.target.value })} placeholder="CH000 (blank = skip)" /></label>
            <label className="field">Fleet<input type="text" value={String(inputs.subset ?? "")} onChange={(e) => setInputs({ ...inputs, subset: e.target.value })} placeholder="FD001" /></label>
            <label className="field">Engine<input type="number" value={String(inputs.unit ?? "")} onChange={(e) => setInputs({ ...inputs, unit: e.target.value ? Number(e.target.value) : "" })} /></label>
            <label className="field">Question for evidence<input type="text" value={String(inputs.question ?? "")} onChange={(e) => setInputs({ ...inputs, question: e.target.value })} /></label>
          </div>
          <label className="field" style={{ marginTop: 8 }}>Incident narrative (safety agent)
            <textarea value={String(inputs.narrative ?? "")} onChange={(e) => setInputs({ ...inputs, narrative: e.target.value })} style={{ height: 50 }} /></label>
          <div className="row small" style={{ marginTop: 8 }}>
            <b>Inject faults:</b>
            {["drawing_agent", "pdm_agent", "safety_agent", "evidence_agent", "cost_agent"].map((a) => (
              <span key={a} className="row" style={{ gap: 4 }}>
                <select value={permanent.includes(a) ? "perm" : String(faults[a] ?? 0)} aria-label={`Faults for ${a}`}
                        onChange={(e) => {
                          const v = e.target.value;
                          setPermanent((p) => v === "perm" ? [...new Set([...p, a])] : p.filter((x) => x !== a));
                          setFaults((f) => ({ ...f, [a]: v === "perm" ? 0 : Number(v) }));
                        }}>
                  <option value="0">{a.replace("_agent", "")}: none</option><option value="1">1 timeout</option>
                  <option value="2">2 timeouts</option><option value="3">3 timeouts (exhausts retries)</option><option value="perm">fails for good</option>
                </select>
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="flow-wrap section" style={{ height: 470 }}>
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.06 }} nodesDraggable={false}
                   nodesConnectable={false} onNodeClick={(_, n) => setSel(n.id)} proOptions={{ hideAttribution: true }} zoomOnScroll={false} preventScrolling={false}>
          <Background color="#dedff1" gap={22} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>

      <div className="three section" style={{ alignItems: "start" }}>
        <div className="panel">
          <div className="panel-head"><h3>{run ? run.name : "No mission running"}</h3>{run && <StatusPill status={run.status} />}</div>
          {run ? (
            <div className="kv">
              <div><span>Run</span><b>{run.id}</b></div>
              <div><span>Duration</span><b>{fmtMs(run.metrics?.duration_ms as number ?? (Date.now() / 1000 - run.started) * 1000)}</b></div>
              <div><span>Retries</span><b>{Object.values(run.nodes ?? {}).reduce((a, n) => a + Math.max(0, (n.attempts ?? 0) - 1), 0)}</b></div>
              <div><span>Tool calls</span><b>{run.events.filter((e) => e.type === "tool_call").length}</b></div>
              <div><span>Mode</span><b>{run.mode}{run.replay_of ? ` of ${run.replay_of}` : ""}</b></div>
              <div><span>Started by</span><b>{run.created_by}</b></div>
            </div>
          ) : <p className="small muted">Pick a scenario and launch. The graph lights up as each agent works.</p>}
          {syn && (
            <div style={{ marginTop: 14 }}>
              <div className="panel-head"><h3>Mission brief</h3><span className={`sev sev-${syn.risk}`}>{syn.risk} risk</span></div>
              {syn.lines.map((l, k) => <p key={k} className="small" style={{ margin: "0 0 6px" }}>{l}</p>)}
              <div className="tiny muted">Confidence {syn.confidence}{syn.missing.length ? ` · missing: ${syn.missing.join(", ")}` : ""}</div>
            </div>
          )}
        </div>
        <div>{run ? <ApprovalGate run={run} onDone={refresh} /> : <div className="panel"><h3>Human approval gate</h3>
          <p className="small muted">When the synthesizer proposes actions, the graph pauses here (LangGraph interrupt) until a named approver decides.</p></div>}
          {run && !Object.keys(run.approvals ?? {}).length && <div className="panel"><h3>Human approval gate</h3><p className="small muted">No actions proposed yet.</p></div>}
        </div>
        <div className="panel">
          <div className="panel-head"><h3>Inspector: {sel ?? "click a node"}</h3>
            {sel && run?.nodes?.[sel] && <StatusPill status={run.nodes[sel].status} />}</div>
          {sel && run?.nodes?.[sel]?.error && <div className="err small">{run.nodes[sel].error}</div>}
          {selOut !== undefined ? <JsonView value={selOut} /> : <p className="small muted">Outputs appear when the node completes.</p>}
        </div>
      </div>

      <div className="panel section"><h3>Live timeline</h3><RunTimeline run={run} height={360} /></div>

      <div className="panel section">
        <h3>Recent missions</h3>
        <table>
          <thead><tr><th>Run</th><th>Status</th><th>Mode</th><th>Started by</th><th className="num">Duration</th><th>Retries</th><th /></tr></thead>
          <tbody>{history.map((h) => (
            <tr key={h.id} className={`clickable ${h.id === runId ? "selected" : ""}`} onClick={() => setRunId(h.id)}>
              <td className="mono">{h.id}</td><td><StatusPill status={h.status} /></td><td>{h.mode}{h.replay_of ? " (replay)" : ""}</td>
              <td>{h.created_by}</td><td className="num">{fmtMs(h.duration_ms)}</td><td>{String(h.metrics?.retries ?? "—")}</td>
              <td className="num"><button className="btn ghost sm" onClick={(e) => { e.stopPropagation(); void replay(h.id); }}><Icon name="replay" size={14} />Replay</button></td>
            </tr>))}</tbody>
        </table>
        {!history.length && <p className="small muted">No missions yet.</p>}
      </div>
      <EnterpriseConsole />
    </>
  );
}
