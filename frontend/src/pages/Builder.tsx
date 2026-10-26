import {
  addEdge, Background, Controls, MarkerType, MiniMap, ReactFlow, ReactFlowProvider, useEdgesState, useNodesState, useReactFlow,
  type Connection, type Edge, type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { identity, rest } from "../api";
import { CAT_COLOR, nodeTypes, type FlowNodeData } from "../components/FlowNode";
import { Icon } from "../components/Icons";
import { ApprovalGate, JsonView, RunTimeline } from "../components/RunPanels";
import { StatusPill } from "../components/StatusPill";
import { DONE, fmtMs, useRun, type RunView } from "../lib/runs";

interface FieldSpec { key: string; label: string; kind: string; default: unknown; options: string[]; help: string }
interface CatalogNode { type: string; label: string; category: string; risk: string; description: string; fields: FieldSpec[]; manual_minutes: number }
interface DefNode { id: string; type: string; label?: string; position?: { x: number; y: number }; config?: Record<string, unknown> }
interface DefEdge { id?: string; source: string; target: string; branch?: string }
interface Definition { id?: string; name: string; description?: string; enabled?: boolean; trigger?: string; version?: number; nodes: DefNode[]; edges: DefEdge[]; updated_by?: string; n_versions?: number }
interface Validation { valid: boolean; errors: string[]; warnings: string[] }
interface Compare { identical: boolean; summary: string; rows: Array<{ node: string; label: string; status_a: string; status_b: string; ms_a: number; ms_b: number; same_output: boolean; changed_keys: string[] }> }

const CAT_ORDER = ["trigger", "agent", "model", "document", "knowledge", "calc", "integration", "data", "logic", "human"];
const CAT_LABEL: Record<string, string> = { trigger: "Triggers", agent: "AI agents", model: "Predictive models", document: "Document OCR", knowledge: "RAG retrieval",
  calc: "Calculations", integration: "API integrations", data: "Database", logic: "Logic", human: "Human checkpoints" };

function toFlow(def: Definition, cat: Record<string, CatalogNode>): { nodes: Node[]; edges: Edge[] } {
  return {
    nodes: def.nodes.map((n, i) => ({
      id: n.id, type: "flow", position: n.position ?? { x: 260 * i, y: 160 },
      data: { label: n.label ?? cat[n.type]?.label ?? n.type, category: cat[n.type]?.category ?? "logic", type: n.type, risk: cat[n.type]?.risk,
              config: n.config ?? {}, meta: cat[n.type]?.label } as FlowNodeData,
    })),
    edges: def.edges.map((e) => edgeOf(e.source, e.target, e.branch)),
  };
}
function edgeOf(source: string, target: string, branch?: string | null): Edge {
  return { id: `${source}-${branch ?? "x"}-${target}`, source, target, sourceHandle: branch ?? undefined, label: branch ?? undefined,
           markerEnd: { type: MarkerType.ArrowClosed, color: "#53558c" } };
}
function toDef(meta: Pick<Definition, "id" | "name" | "description" | "enabled">, nodes: Node[], edges: Edge[]): Definition {
  return {
    ...meta,
    nodes: nodes.map((n) => {
      const d = n.data as FlowNodeData & { config?: Record<string, unknown> };
      return { id: n.id, type: d.type, label: d.label, position: { x: Math.round(n.position.x), y: Math.round(n.position.y) }, config: d.config ?? {} };
    }),
    edges: edges.map((e) => ({ source: e.source, target: e.target, ...(e.sourceHandle ? { branch: e.sourceHandle } : {}) })),
  };
}
const baseId = (t: string) => (t.split(".").pop() ?? "step").replace(/[^A-Za-z0-9_]/g, "_").replace(/^[^A-Za-z]/, "s");

function BuilderInner() {
  const rf = useReactFlow();
  const wrap = useRef<HTMLDivElement>(null);
  const [catalog, setCatalog] = useState<CatalogNode[]>([]);
  const [templates, setTemplates] = useState<Definition[]>([]);
  const [saved, setSaved] = useState<Definition[]>([]);
  const [meta, setMeta] = useState<Pick<Definition, "id" | "name" | "description" | "enabled">>({ name: "Untitled workflow", enabled: true });
  const [version, setVersion] = useState<number | undefined>();
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [check, setCheck] = useState<Validation | null>(null);
  const [faults, setFaults] = useState<Record<string, number>>({});
  const [permanent, setPermanent] = useState<string[]>([]);
  const [payload, setPayload] = useState("{}");
  const [runId, setRunId] = useState<string | null>(null);
  const [batchId, setBatchId] = useState<string | null>(null);
  const [n, setN] = useState(12);
  const [tab, setTab] = useState("timeline");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [recent, setRecent] = useState<RunView[]>([]);
  const [cmpA, setCmpA] = useState(""); const [cmpB, setCmpB] = useState("");
  const [cmp, setCmp] = useState<Compare | null>(null);
  const [pendingCompare, setPendingCompare] = useState<{ a: string; b: string } | null>(null);
  const { run, refresh } = useRun(runId);
  const { run: batch } = useRun(batchId);
  const cat = useMemo(() => Object.fromEntries(catalog.map((c) => [c.type, c])), [catalog]);
  const me = identity();

  const loadSaved = () => rest<Definition[]>("/api/workflows").then(setSaved).catch(() => undefined);
  const loadRecent = () => rest<RunView[]>("/api/runs?kind=workflow&limit=30").then(setRecent).catch(() => undefined);
  useEffect(() => {
    Promise.all([rest<{ nodes: CatalogNode[] }>("/api/workflows/catalog"), rest<Definition[]>("/api/workflows/templates")])
      .then(([c, t]) => {
        setCatalog(c.nodes); setTemplates(t);
        const m = Object.fromEntries(c.nodes.map((x) => [x.type, x]));
        load(t[1], m, true);
      }).catch((e) => setErr(e.message));
    void loadSaved(); void loadRecent();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { if (run && DONE.has(run.status)) void loadRecent(); }, [run?.status]);
  useEffect(() => {
    if (pendingCompare && run?.id === pendingCompare.b && DONE.has(run.status)) {
      setCmpA(pendingCompare.a); setCmpB(pendingCompare.b); void doCompare(pendingCompare.a, pendingCompare.b); setPendingCompare(null); setTab("compare");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.status, pendingCompare]);

  const load = (d: Definition, m = cat, asTemplate = false) => {
    const f = toFlow(d, m);
    setNodes(f.nodes); setEdges(f.edges);
    setMeta({ id: asTemplate ? undefined : d.id, name: d.name, description: d.description, enabled: d.enabled ?? true });
    setVersion(asTemplate ? undefined : d.version);
    setSel(null); setCheck(null); setRunId(null); setFaults({}); setPermanent([]);
    setTimeout(() => rf.fitView({ padding: 0.05 }), 60);
  };

  const def = () => toDef(meta, nodes, edges);
  const validate = async () => {
    try { const v = await rest<Validation>("/api/workflows/validate", { body: def() }); setCheck(v); return v; }
    catch (e) { setErr(String(e)); return null; }
  };
  // live validation as the graph changes
  useEffect(() => {
    if (!catalog.length) return;
    const t = setTimeout(() => { void validate(); }, 350);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes.length, edges, catalog.length]);

  const save = async () => {
    setErr(null); setMsg(null);
    try {
      const r = await rest<Definition>("/api/workflows", { method: "PUT", body: def() });
      setMeta((m) => ({ ...m, id: r.id })); setVersion(r.version); setMsg(`Saved version ${r.version}`); void loadSaved();
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  const allFaults = () => ({ ...Object.fromEntries(Object.entries(faults).filter(([, v]) => v > 0)), _permanent: permanent });
  const runLive = async (mode: "live" | "test") => {
    setErr(null); setMsg(null);
    let p: Record<string, unknown> = {};
    try { p = JSON.parse(payload || "{}"); } catch { setErr("Trigger payload must be JSON"); return; }
    try {
      const r = await rest<{ run_id: string }>("/api/workflows/run", { body: { definition: def(), payload: p, mode, faults: allFaults() } });
      setRunId(r.run_id); setTab("timeline");
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  const testBatch = async () => {
    setErr(null);
    try {
      const r = await rest<{ run_id: string }>("/api/workflows/test", { body: { definition: def(), n, faults: allFaults() } });
      setBatchId(r.run_id); setTab("test");
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  const replay = async (id: string) => {
    try {
      const r = await rest<{ run_id: string }>(`/api/runs/${id}/replay`, { body: {} });
      setRunId(r.run_id); setPendingCompare({ a: id, b: r.run_id });
    } catch (e) { setErr(String(e)); }
  };
  const doCompare = async (a = cmpA, b = cmpB) => {
    if (!a || !b) return;
    try { setCmp(await rest<Compare>(`/api/runs/compare?a=${a}&b=${b}`)); } catch (e) { setErr(String(e)); }
  };

  const onConnect = useCallback((c: Connection) => {
    setEdges((es) => addEdge(edgeOf(c.source!, c.target!, c.sourceHandle), es));
  }, [setEdges]);
  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const type = e.dataTransfer.getData("application/aeronexus");
    const c = cat[type];
    if (!c) return;
    const pos = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    let id = baseId(type); let k = 2;
    while (nodes.some((x) => x.id === id)) id = `${baseId(type)}${k++}`;
    const config = Object.fromEntries(c.fields.map((f) => [f.key, f.default]));
    setNodes((ns) => [...ns, { id, type: "flow", position: pos, data: { label: c.label, category: c.category, type, risk: c.risk, config, meta: c.label } }]);
    setSel(id);
  };

  // overlay live run state onto the canvas
  const shown: Node[] = useMemo(() => nodes.map((nd) => {
    const st = run?.definition && (run.definition as { nodes?: DefNode[] }).nodes?.some((x) => x.id === nd.id) ? run.nodes?.[nd.id] : undefined;
    const fl = faults[nd.id] ? `${faults[nd.id]} timeouts` : permanent.includes(nd.id) ? "fails" : undefined;
    return { ...nd, selected: nd.id === sel, data: { ...nd.data, status: st?.status, attempts: st?.attempts, ms: st?.ms, faults: fl } };
  }), [nodes, run, sel, faults, permanent]);
  const shownEdges: Edge[] = useMemo(() => edges.map((e) => {
    const a = run?.nodes?.[e.source]?.status, b = run?.nodes?.[e.target]?.status;
    const used = a === "completed" && b && !["pending", "skipped"].includes(b);
    return { ...e, animated: !!used && !!run && (!DONE.has(run.status) || b === "running"),
             style: { ...(e.style ?? {}), opacity: run && run.nodes?.[e.source] && !used ? 0.35 : 1 } };
  }), [edges, run]);

  const selNode = nodes.find((x) => x.id === sel);
  const sd = selNode?.data as (FlowNodeData & { config: Record<string, unknown> }) | undefined;
  const spec = sd ? cat[sd.type] : undefined;
  const setCfg = (k: string, v: unknown) => setNodes((ns) => ns.map((x) => x.id === sel ? { ...x, data: { ...x.data, config: { ...(x.data as { config: Record<string, unknown> }).config, [k]: v } } } : x));
  const setLabel = (v: string) => setNodes((ns) => ns.map((x) => x.id === sel ? { ...x, data: { ...x.data, label: v } } : x));
  const upstream = useMemo(() => {
    if (!sel) return [];
    const seen = new Set<string>(); const stack = [sel];
    while (stack.length) { const u = stack.pop()!; edges.filter((e) => e.target === u).forEach((e) => { if (!seen.has(e.source)) { seen.add(e.source); stack.push(e.source); } }); }
    return [...seen];
  }, [sel, edges]);

  const bm = (batch?.metrics && "runs" in batch.metrics ? batch.metrics : undefined) as undefined | {
    runs: number; completed: number; failed: number; failure_rate: number; retries: number; latency_ms_p50: number; latency_ms_p95: number;
    escalation: { labelled: number; accuracy: number; precision: number | null; recall: number | null; escalated: number } | null;
    extraction_accuracy: number | null; time: { manual_minutes_assumed: number; review_minutes: number; machine_minutes: number; saved_minutes: number; saved_pct: number | null; note: string };
    source: string;
  };
  const rows = (batch?.outputs?.rows ?? []) as Array<{ run_id: string; payload: Record<string, unknown>; status: string; ms: number; retries: number; escalated: boolean; expected: boolean | null; correct: boolean | null; extraction_accuracy: number | null; failed_step: string | null }>;
  const progress = batch ? batch.events.filter((e) => e.type === "test_item").length : 0;

  return (
    <>
      <div className="page-head">
        <div>
          <p className="eyebrow">Automation · React Flow + LangGraph</p>
          <h1>Workflow builder</h1>
          <p className="lede">Drag steps onto the canvas and connect them. Each workflow compiles to a LangGraph state graph; writes to other
            systems are refused unless a human approval comes first.</p>
        </div>
      </div>

      <div className="panel" style={{ padding: "12px 14px" }}>
        <div className="row">
          <select aria-label="Load template" value="" style={{ maxWidth: 220 }} onChange={(e) => { const t = templates.find((x) => x.id === e.target.value); if (t) load(t, cat, true); }}>
            <option value="">Templates…</option>{templates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
          <select aria-label="Open saved workflow" value={meta.id ?? ""} style={{ maxWidth: 240 }} onChange={(e) => { const w = saved.find((x) => x.id === e.target.value); if (w) rest<Definition>(`/api/workflows/${w.id}`).then((d) => load(d)); }}>
            <option value="">Saved workflows…</option>{saved.map((w) => <option key={w.id} value={w.id}>{w.name} (v{w.version}{w.enabled ? "" : ", off"})</option>)}
          </select>
          <input type="text" value={meta.name} onChange={(e) => setMeta({ ...meta, name: e.target.value })} aria-label="Workflow name" style={{ flex: 1, minWidth: 220 }} />
          <label className="small row" style={{ gap: 4 }}><input type="checkbox" checked={meta.enabled ?? true} onChange={(e) => setMeta({ ...meta, enabled: e.target.checked })} />Listen for its trigger</label>
          <span className="spacer" />
          {version && <span className="tag soft">v{version}</span>}
          <button className="btn" onClick={save} disabled={me.role === "viewer"}><Icon name="save" size={14} />Save</button>
          <button className="btn" onClick={() => runLive("test")}><Icon name="flask" size={14} />Test run</button>
          <button className="btn primary" onClick={() => runLive("live")} disabled={me.role === "viewer"}><Icon name="play" size={14} />Run live</button>
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          {check && (check.valid ? <span className="tag soft">✓ valid: compiles to a LangGraph graph</span> :
            <span className="tag strong">✕ {check.errors.length} problem{check.errors.length > 1 ? "s" : ""}</span>)}
          {check?.errors.map((x) => <span key={x} className="small" style={{ color: "#2f3169" }}>• {x}</span>)}
          {check?.warnings.map((x) => <span key={x} className="small muted">• {x}</span>)}
          {msg && <span className="small"><b>{msg}</b></span>}
        </div>
        {err && <div className="err small" style={{ marginTop: 8 }}>{err}</div>}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "196px minmax(0,1fr) 300px", gap: 14, marginTop: 14 }}>
        <div className="panel" style={{ padding: 12 }}>
          <h3>Steps</h3>
          <p className="tiny muted" style={{ margin: 0 }}>Drag onto the canvas.</p>
          <div className="palette">
            {CAT_ORDER.map((c) => (
              <div key={c}>
                <div className="grp">{CAT_LABEL[c]}</div>
                {catalog.filter((x) => x.category === c).map((x) => (
                  <div key={x.type} className="item" draggable onDragStart={(e) => { e.dataTransfer.setData("application/aeronexus", x.type); e.dataTransfer.effectAllowed = "move"; }}
                       title={x.description} style={{ borderLeft: `3px solid ${CAT_COLOR[c]}` }}>
                    <b>{x.label}</b><span>{x.description.slice(0, 74)}{x.description.length > 74 ? "…" : ""}</span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>

        <div className="flow-wrap tall" ref={wrap} onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; }} onDrop={onDrop}>
          <ReactFlow nodes={shown} edges={shownEdges} nodeTypes={nodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
                     onConnect={onConnect} onNodeClick={(_, nd) => setSel(nd.id)} onPaneClick={() => setSel(null)} fitView fitViewOptions={{ padding: 0.05 }}
                     deleteKeyCode={["Backspace", "Delete"]} proOptions={{ hideAttribution: true }} zoomOnScroll={false} preventScrolling={false}>
            <Background color="#dedff1" gap={22} />
            <MiniMap pannable zoomable nodeColor={(nd) => CAT_COLOR[(nd.data as FlowNodeData).category] ?? "#b5b6d6"} />
            <Controls />
          </ReactFlow>
        </div>

        <div style={{ display: "grid", gap: 12, alignContent: "start" }}>
          {run && (
            <div className="panel">
              <div className="panel-head"><h3>{run.mode === "live" ? "Live run" : run.mode === "replay" ? "Replay" : "Test run"}</h3><StatusPill status={run.status} /></div>
              <div className="kv">
                <div><span>Run</span><b>{run.id}</b></div><div><span>Duration</span><b>{fmtMs(run.duration_ms ?? (run.metrics?.duration_ms as number))}</b></div>
                <div><span>Retries</span><b>{String(run.metrics?.retries ?? Object.values(run.nodes ?? {}).reduce((a, x) => a + Math.max(0, (x.attempts ?? 0) - 1), 0))}</b></div>
                <div><span>Tool calls</span><b>{run.events.filter((e) => e.type === "tool_call").length}</b></div>
              </div>
              {run.error && <div className="err small" style={{ marginTop: 8 }}>{run.error}</div>}
              <div className="row" style={{ marginTop: 8 }}>
                {DONE.has(run.status) && <button className="btn sm" onClick={() => replay(run.id)}><Icon name="replay" size={14} />Replay and compare</button>}
                <button className="btn ghost sm" onClick={() => setRunId(null)}>Clear overlay</button>
              </div>
            </div>
          )}
          {run && <ApprovalGate run={run} onDone={refresh} />}
          {sd && spec ? (
            <div className="panel">
              <div className="panel-head"><h3>Step settings</h3><span className="tag soft mono">{sel}</span></div>
              <p className="tiny muted" style={{ marginTop: 0 }}>{spec.description}</p>
              <label className="field">Label<input type="text" value={sd.label} onChange={(e) => setLabel(e.target.value)} /></label>
              {spec.fields.map((f) => {
                const v = sd.config?.[f.key];
                return (
                  <label key={f.key} className="field" style={{ marginTop: 8 }}>{f.label}
                    {f.kind === "select" ? <select value={String(v ?? "")} onChange={(e) => setCfg(f.key, e.target.value)}>{f.options.map((o) => <option key={o} value={o}>{o || "(any)"}</option>)}</select>
                      : f.kind === "bool" ? <input type="checkbox" checked={!!v} onChange={(e) => setCfg(f.key, e.target.checked)} />
                      : f.kind === "number" ? <input type="text" value={String(v ?? "")} onChange={(e) => setCfg(f.key, e.target.value)} />
                      : f.kind === "json" ? <textarea className="mono" style={{ height: 80 }} defaultValue={JSON.stringify(v ?? {}, null, 1)} key={`${sel}-${f.key}`}
                                                       onBlur={(e) => { try { setCfg(f.key, JSON.parse(e.target.value || "{}")); } catch { setErr(`${f.label}: invalid JSON`); } }} />
                      : f.kind === "textarea" ? <textarea style={{ height: 60 }} value={String(v ?? "")} onChange={(e) => setCfg(f.key, e.target.value)} />
                      : <input type="text" value={String(v ?? "")} onChange={(e) => setCfg(f.key, e.target.value)} />}
                  </label>
                );
              })}
              {sd.category !== "trigger" && sd.type !== "human.approval" && (
                <div className="two" style={{ gap: 8, marginTop: 8 }}>
                  <label className="field">Retries<select value={String(sd.config?.retries ?? 2)} onChange={(e) => setCfg("retries", Number(e.target.value))}>
                    {[0, 1, 2, 3, 4].map((x) => <option key={x}>{x}</option>)}</select></label>
                  <label className="field">On final failure<select value={String(sd.config?.on_error ?? "fail")} onChange={(e) => setCfg("on_error", e.target.value)}>
                    <option value="fail">Stop the run</option><option value="continue">Continue without it</option></select></label>
                  <label className="field">Simulate timeouts<select value={permanent.includes(sel!) ? "perm" : String(faults[sel!] ?? 0)}
                    onChange={(e) => { const v = e.target.value; setPermanent((p) => v === "perm" ? [...new Set([...p, sel!])] : p.filter((x) => x !== sel)); setFaults((f) => ({ ...f, [sel!]: v === "perm" ? 0 : Number(v) })); }}>
                    <option value="0">none</option><option value="1">1</option><option value="2">2</option><option value="3">3</option><option value="perm">always fail</option></select></label>
                  <label className="field">Manual minutes (assumed)<input type="text" value={String(sd.config?.manual_minutes ?? spec.manual_minutes)} onChange={(e) => setCfg("manual_minutes", Number(e.target.value) || 0)} /></label>
                </div>
              )}
              {upstream.length > 0 && <p className="tiny muted">Reference earlier outputs with {"{{"}step.field{"}}"}: {upstream.map((u) => <code key={u} style={{ marginRight: 6 }}>{u}</code>)}</p>}
              <div className="row" style={{ marginTop: 8 }}>
                <button className="btn sm" onClick={() => { setNodes((ns) => ns.filter((x) => x.id !== sel)); setEdges((es) => es.filter((e) => e.source !== sel && e.target !== sel)); setSel(null); }}>
                  <Icon name="x" size={14} />Delete step</button>
              </div>
              {run?.outputs?.[sel!] !== undefined && <><h3 style={{ marginTop: 12 }}>Last output</h3><JsonView value={run.outputs[sel!]} /></>}
            </div>
          ) : (
            <div className="panel">
              <h3>Trigger payload</h3>
              <p className="tiny muted" style={{ marginTop: 0 }}>Used for “Run live” and “Test run”. Leave as {"{}"} for the trigger's defaults.</p>
              <textarea className="mono" value={payload} onChange={(e) => setPayload(e.target.value)} style={{ width: "100%", height: 90 }} aria-label="Trigger payload" />
              <h3 style={{ marginTop: 12 }}>Test against history</h3>
              <p className="tiny muted" style={{ marginTop: 0 }}>Runs the workflow on N real or synthetic past events in a sandbox (simulated reviewer, no live writes) and scores it against ground truth.</p>
              <div className="row"><input type="number" min={1} max={40} value={n} onChange={(e) => setN(Number(e.target.value))} style={{ width: 80 }} aria-label="Batch size" />
                <button className="btn soft" onClick={testBatch}><Icon name="flask" size={14} />Run test batch</button></div>
              <p className="tiny muted">Select a step to edit it. Connect a condition's top handle for “true” and bottom for “false”.</p>
            </div>
          )}
        </div>
      </div>

      <div className="panel section">
        <div className="tabs" role="tablist">
          {[["timeline", "Execution timeline"], ["data", "Step data"], ["test", "Test results"], ["compare", "Compare and replay"]].map(([k, l]) => (
            <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)} role="tab" aria-selected={tab === k}>{l}</button>))}
        </div>
        {tab === "timeline" && <RunTimeline run={run} />}
        {tab === "data" && (run ? (
          <div className="two">{Object.entries(run.outputs ?? {}).map(([k, v]) => (
            <div key={k}><div className="row"><b>{k}</b>{run.nodes?.[k] && <StatusPill status={run.nodes[k].status} />}<span className="tiny muted">{fmtMs(run.nodes?.[k]?.ms)}</span></div><JsonView value={v} /></div>))}</div>
        ) : <div className="empty small">Run the workflow to inspect each step's data transformations.</div>)}
        {tab === "test" && (batch ? (
          <div>
            <div className="row" style={{ marginBottom: 10 }}>
              <StatusPill status={batch.status} />
              <div className="progress" style={{ flex: 1 }}><i style={{ width: `${(progress / Math.max(1, Number(batch.inputs?.n ?? n))) * 100}%` }} /></div>
              <span className="small mono">{progress}/{String(batch.inputs?.n ?? n)}</span>
            </div>
            {bm && (
              <>
                <div className="tiles">
                  {bm.escalation && <div className="tile dark"><span className="k">Escalation accuracy</span><b>{(bm.escalation.accuracy * 100).toFixed(0)}%</b>
                    <span>precision {bm.escalation.precision ?? "—"} · recall {bm.escalation.recall ?? "—"} · {bm.escalation.labelled} labelled</span></div>}
                  {bm.extraction_accuracy !== null && <div className="tile dark"><span className="k">Extraction accuracy</span><b>{(bm.extraction_accuracy * 100).toFixed(1)}%</b><span>fields vs golden truth</span></div>}
                  <div className="tile"><span className="k">Failure rate</span><b>{(bm.failure_rate * 100).toFixed(1)}%</b><span>{bm.failed} of {bm.runs} runs · {bm.retries} retries</span></div>
                  <div className="tile"><span className="k">Latency</span><b>{fmtMs(bm.latency_ms_p50)}</b><span>p50 · p95 {fmtMs(bm.latency_ms_p95)}</span></div>
                  <div className="tile"><span className="k">Time saved (est.)</span><b>{Math.round(bm.time.saved_minutes / 60 * 10) / 10} h</b>
                    <span>{bm.time.saved_pct ?? "—"}% of {Math.round(bm.time.manual_minutes_assumed)} manual min · {Math.round(bm.time.review_minutes)} min review kept</span></div>
                </div>
                <p className="tiny muted">Data: {bm.source}. {bm.time.note}</p>
                <div style={{ height: 180 }}>
                  <ResponsiveContainer>
                    <BarChart data={rows.map((r, k) => ({ k: k + 1, ms: r.ms, correct: r.correct }))} margin={{ left: -10 }}>
                      <CartesianGrid stroke="#eeeef7" /><XAxis dataKey="k" fontSize={11} /><YAxis fontSize={11} unit=" ms" /><Tooltip />
                      <Bar dataKey="ms" name="run time (ms)" fill="#53558c" radius={[4, 4, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </>
            )}
            <table className="small">
              <thead><tr><th>#</th><th>Input</th><th>Status</th><th className="num">Time</th><th>Retries</th><th>Escalated</th><th>Expected</th><th>Result</th><th /></tr></thead>
              <tbody>{rows.map((r, k) => (
                <tr key={r.run_id}>
                  <td>{k + 1}</td><td className="mono">{JSON.stringify(r.payload).slice(0, 60)}</td><td><StatusPill status={r.status} /></td>
                  <td className="num">{fmtMs(r.ms)}</td><td>{r.retries}</td><td>{r.escalated ? "yes" : "no"}</td><td>{r.expected === null ? "—" : r.expected ? "yes" : "no"}</td>
                  <td>{r.correct === null ? (r.extraction_accuracy !== null ? `${(r.extraction_accuracy * 100).toFixed(0)}% fields` : "—") : r.correct ? "✓ correct" : "✕ wrong"}</td>
                  <td><button className="btn ghost sm" onClick={() => { setRunId(r.run_id); setTab("timeline"); }}>Inspect</button></td>
                </tr>))}</tbody>
            </table>
          </div>
        ) : <div className="empty small">Run a test batch to measure accuracy, failure rate, latency and time saved on historical data.</div>)}
        {tab === "compare" && (
          <div>
            <div className="row">
              <select value={cmpA} onChange={(e) => setCmpA(e.target.value)} aria-label="Run A"><option value="">Run A…</option>{recent.map((r) => <option key={r.id} value={r.id}>{r.id} · {r.name} · {r.mode} · {r.status}</option>)}</select>
              <select value={cmpB} onChange={(e) => setCmpB(e.target.value)} aria-label="Run B"><option value="">Run B…</option>{recent.map((r) => <option key={r.id} value={r.id}>{r.id} · {r.name} · {r.mode} · {r.status}</option>)}</select>
              <button className="btn" onClick={() => doCompare()}>Compare</button>
            </div>
            {cmp && (
              <>
                <p className="small" style={{ marginTop: 10 }}><span className={`sev ${cmp.identical ? "sev-low" : "sev-high"}`}>{cmp.identical ? "Identical" : "Different"}</span> {cmp.summary}</p>
                <table className="small">
                  <thead><tr><th>Step</th><th>Run A</th><th>Run B</th><th className="num">Time A</th><th className="num">Time B</th><th>Output</th></tr></thead>
                  <tbody>{cmp.rows.map((r) => (
                    <tr key={r.node}><td>{r.label}</td><td><StatusPill status={r.status_a} /></td><td><StatusPill status={r.status_b} /></td>
                      <td className="num">{fmtMs(r.ms_a)}</td><td className="num">{fmtMs(r.ms_b)}</td>
                      <td>{r.same_output ? "same" : <b>differs: {r.changed_keys.join(", ") || "value"}</b>}</td></tr>))}</tbody>
                </table>
              </>
            )}
          </div>
        )}
      </div>
    </>
  );
}

export function BuilderPage() {
  return <ReactFlowProvider><BuilderInner /></ReactFlowProvider>;
}
