
import { Fragment, useCallback, useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { identity, rest } from "../api";
import { DONE, fmtMs, useRun, type RunView } from "../lib/runs";
import { Icon } from "./Icons";
import { ApprovalGate, JsonView, RunTimeline } from "./RunPanels";
import { StatusPill } from "./StatusPill";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

/**
 * Formats approval arguments for readable display.
 * Nested objects and arrays are rendered as formatted JSON.
 */
function formatApprovalValue(value: unknown): string {
  if (value == null) return "—";

  if (typeof value === "string") return value;

  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }

  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

const TABS: Array<[string, string]> = [
  ["approvals", "Approval queue"],
  ["procure", "Procurement (SAP → Sheets)"],
  ["traces", "Agent execution traces"],
  ["status", "Workflow status and productivity"],
  ["settings", "Integration settings"],
  ["audit", "Audit log"],
];

/**
 * Enterprise integration console for Mission Control.
 * Renders nothing when the integration layer is switched off
 * (AERONEXUS_FF_INTEGRATIONS=0), leaving the existing dashboard unchanged.
 */
export function EnterpriseConsole() {
  const [status, setStatus] = useState<Any>(null);
  const [off, setOff] = useState(false);
  const [tab, setTab] = useState("approvals");
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(
    () =>
      rest<Any>("/api/integrations/status")
        .then((s) => {
          setStatus(s);
          setOff(false);
        })
        .catch((e) => {
          if (String(e.message).includes("Not Found")) {
            setOff(true);
          } else {
            setErr(e.message);
          }
        }),
    [],
  );

  useEffect(() => {
    void load();
  }, [load]);

  if (off) return null;

  return (
    <section
      className="panel section"
      id="enterprise"
      aria-label="Enterprise integrations"
    >
      <div className="panel-head">
        <div>
          <p className="eyebrow" style={{ margin: 0 }}>
            Enterprise integrations · MCP · SAP (mock) · Google Sheets
          </p>
          <h2 style={{ margin: 0 }}>Enterprise automation console</h2>
        </div>

        {status && (
          <div className="row">
            {status.connectors
              .filter((c: Any) => c.enabled)
              .map((c: Any) => (
                <span key={c.id} className="tag soft">
                  ● {c.label}
                  {c.mode ? ` (${c.mode})` : ""}
                </span>
              ))}
          </div>
        )}
      </div>

      {err && <div className="err small">{err}</div>}

      <div className="tabs" role="tablist">
        {TABS.map(([k, label]) => (
          <button
            key={k}
            role="tab"
            aria-selected={tab === k}
            className={tab === k ? "on" : ""}
            onClick={() => setTab(k)}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "approvals" && <ApprovalQueue />}
      {tab === "procure" && <Procurement onChange={load} />}
      {tab === "traces" && <Traces />}
      {tab === "status" && <StatusAnalytics />}
      {tab === "settings" && status && (
        <Settings status={status} reload={load} />
      )}
      {tab === "audit" && <Audit />}
    </section>
  );
}

function ApprovalQueue() {
  const [q, setQ] = useState<Any>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<string | null>(null);

  const me = identity();
  const canApprove = me.role === "approver" || me.role === "admin";

  const load = useCallback(
    () =>
      rest<Any>("/api/integrations/approvals")
        .then(setQ)
        .catch((e) => setMsg(e.message)),
    [],
  );

  useEffect(() => {
    void load();
    const timer = setInterval(load, 4000);
    return () => clearInterval(timer);
  }, [load]);

  const decide = async (id: string, approve: boolean) => {
    setMsg(null);

    try {
      const r = await rest<Any>(
        `/api/integrations/approvals/${id}`,
        {
          body: {
            approve,
            note: notes[id] ?? "",
          },
        },
      );

      setMsg(
        `${approve ? "Approved" : "Rejected"} ${r.tool}: ${r.status}` +
          `${
            r.result?.PurchaseRequisition
              ? ` · requisition ${r.result.PurchaseRequisition}`
              : ""
          }` +
          `${
            r.result?.PurchaseOrder
              ? ` · PO ${r.result.PurchaseOrder}`
              : ""
          }`,
      );

      void load();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  };

  if (!q) return <div className="skeleton" />;

  return (
    <div className="split">
      <div>
        <h3>Waiting for a decision ({q.pending.length})</h3>

        {!q.pending.length && (
          <p className="small muted">
            Nothing pending. Request a requisition in the Procurement tab,
            or ask an MCP client to.
          </p>
        )}

        {q.pending.map((a: Any) => (
          <div
            key={a.id}
            className="gatecard"
            style={{ marginBottom: 10 }}
          >
            <div
              className="row"
              style={{ justifyContent: "space-between" }}
            >
              <h3 style={{ margin: 0 }}>
                <Icon name="lock" /> {a.tool}
              </h3>

              <span className="tiny muted">
                {a.risk} · {a.module || "agent"} · waiting{" "}
                {fmtMs(a.age_s * 1000)}
              </span>
            </div>

            {/* Request context and originating case */}
            <div
              className="small"
              style={{ margin: "4px 0 6px" }}
            >
              {a.reason || "No reason given"}

              <span className="muted">
                {" "}· requested by {a.proposed_by}
              </span>

              {a.case_id && (
                <div
                  className="tiny muted"
                  style={{ marginTop: 4 }}
                >
                  Case / run: <code>{a.case_id}</code>
                </div>
              )}
            </div>

            {/* Readable arguments, including nested objects and arrays */}
            <div className="kv">
              {Object.entries(a.args ?? {}).map(([k, v]) => (
                <div key={k}>
                  <span>{k.replace(/_/g, " ")}</span>

                  <b
                    style={{
                      whiteSpace: "pre-wrap",
                      overflowWrap: "anywhere",
                      textAlign: "left",
                    }}
                  >
                    {formatApprovalValue(v)}
                  </b>
                </div>
              ))}
            </div>

            {canApprove ? (
              <>
                <textarea
                  aria-label="Decision note"
                  placeholder="Note for the audit log"
                  value={notes[a.id] ?? ""}
                  onChange={(e) =>
                    setNotes({
                      ...notes,
                      [a.id]: e.target.value,
                    })
                  }
                  style={{
                    width: "100%",
                    height: 46,
                    marginTop: 8,
                  }}
                />

                <div className="row" style={{ marginTop: 6 }}>
                  <button
                    className="btn"
                    onClick={() => decide(a.id, false)}
                  >
                    <Icon name="x" size={14} />
                    Reject
                  </button>

                  <button
                    className="btn primary"
                    onClick={() => decide(a.id, true)}
                    disabled={!me.user}
                  >
                    <Icon name="check" size={14} />
                    Approve as {me.user || "…"}
                  </button>
                </div>
              </>
            ) : (
              <p className="tiny muted">
                Switch to the approver role (bottom left) to decide.
              </p>
            )}
          </div>
        ))}

        {msg && <div className="note">{msg}</div>}
      </div>

      <div>
        <h3>Recent decisions</h3>

        <table className="small">
          <thead>
            <tr>
              <th>Tool</th>
              <th>Status</th>
              <th>By</th>
              <th>Reference</th>
            </tr>
          </thead>

          <tbody>
            {q.decided.map((a: Any) => (
              <tr key={a.id}>
                <td className="mono">{a.tool}</td>
                <td>
                  <StatusPill status={a.status} />
                </td>
                <td>{a.decided_by ?? "—"}</td>
                <td className="mono tiny">
                  {a.result?.PurchaseRequisition ??
                    a.result?.PurchaseOrder ??
                    a.result?.CaseNumber ??
                    a.result?.id ??
                    ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {!q.decided.length && (
          <p className="small muted">No decisions yet.</p>
        )}
      </div>
    </div>
  );
}

function Procurement({ onChange }: { onChange: () => void }) {
  const [materials, setMaterials] = useState<Any[]>([]);
  const [prs, setPrs] = useState<Any[]>([]);
  const [pos, setPos] = useState<Any[]>([]);

  const [form, setForm] = useState({
    material: "BSK-ENG-KIT",
    quantity: 1,
    reason: "Borescope inspection for FD001-ENG-020",
  });

  const [engine, setEngine] = useState({
    subset: "FD001",
    unit: 20,
  });

  const [runId, setRunId] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const { run, refresh } = useRun(runId);

  const load = useCallback(() => {
    rest<Any[]>("/api/sap/materials")
      .then(setMaterials)
      .catch(() => undefined);

    rest<Any[]>("/api/sap/purchase-requisitions")
      .then(setPrs)
      .catch(() => undefined);

    rest<Any[]>("/api/sap/purchase-orders?status=released")
      .then(setPos)
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [load]);

  useEffect(() => {
    if (run && DONE.has(run.status)) {
      load();
      onChange();
    }
  }, [run?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const request = async () => {
    setMsg(null);

    try {
      const r = await rest<Any>(
        "/api/sap/purchase-requisitions",
        { body: form },
      );

      setMsg(
        r.status === "duplicate"
          ? "Same request is already in the queue."
          : `Requested: approval ${r.approval_id} is waiting in the Approval queue.`,
      );
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  };

  const convert = async (pr: string) => {
    try {
      const r = await rest<Any>(
        `/api/sap/purchase-requisitions/${pr}/convert`,
        { body: {} },
      );

      setMsg(`Conversion to PO requested: approval ${r.approval_id}.`);
    } catch (e) {
      setMsg(String(e));
    }
  };

  const startFlow = async () => {
    try {
      const r = await rest<{ run_id: string }>(
        "/api/integrations/procurement/demo",
        { body: engine },
      );

      setRunId(r.run_id);
    } catch (e) {
      setMsg(String(e));
    }
  };

  const sel = materials.find((m) => m.Material === form.material);

  return (
    <div>
      <div className="two">
        <div className="panel" style={{ boxShadow: "none" }}>
          <h3>Agentic flow: inspection → SAP requisition → Sheets</h3>

          <p className="small muted" style={{ marginTop: 0 }}>
            Runs the LangGraph workflow: remaining-life model → spares
            suggestion → <b>human approval</b> → SAP purchase requisition
            (mock) → tracking row in Google Sheets.
          </p>

          <div className="row">
            <select
              value={engine.subset}
              onChange={(e) =>
                setEngine({ ...engine, subset: e.target.value })
              }
              aria-label="Fleet"
            >
              {["FD001", "FD002", "FD003", "FD004"].map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>

            <input
              type="number"
              value={engine.unit}
              onChange={(e) =>
                setEngine({
                  ...engine,
                  unit: Number(e.target.value),
                })
              }
              style={{ width: 90 }}
              aria-label="Engine"
            />

            <button className="btn primary" onClick={startFlow}>
              <Icon name="play" size={14} />
              Start workflow
            </button>

            {run && <StatusPill status={run.status} />}
          </div>

          {run && (
            <div style={{ marginTop: 8 }}>
              <ApprovalGate run={run} onDone={refresh} />
            </div>
          )}

          {run && (
            <div style={{ marginTop: 8 }}>
              <RunTimeline run={run} height={220} />
            </div>
          )}
        </div>

        <div className="panel" style={{ boxShadow: "none" }}>
          <h3>Request a purchase requisition by hand</h3>

          <div className="two" style={{ gap: 8 }}>
            <label className="field">
              Material
              <select
                value={form.material}
                onChange={(e) =>
                  setForm({ ...form, material: e.target.value })
                }
              >
                {materials.map((m) => (
                  <option key={m.Material} value={m.Material}>
                    {m.Material} · {m.MaterialName}
                  </option>
                ))}
              </select>
            </label>

            <label className="field">
              Quantity
              <input
                type="number"
                min={1}
                value={form.quantity}
                onChange={(e) =>
                  setForm({
                    ...form,
                    quantity: Number(e.target.value),
                  })
                }
              />
            </label>
          </div>

          <label className="field" style={{ marginTop: 8 }}>
            Reason
            <input
              type="text"
              value={form.reason}
              onChange={(e) =>
                setForm({ ...form, reason: e.target.value })
              }
            />
          </label>

          <div className="row" style={{ marginTop: 8 }}>
            <button className="btn soft" onClick={request}>
              Request (goes to approval)
            </button>

            {sel && (
              <span className="small muted">
                est.{" "}
                {(sel.Price * form.quantity).toLocaleString("en-CA", {
                  style: "currency",
                  currency: "CAD",
                })}
              </span>
            )}
          </div>

          {msg && (
            <div className="note" style={{ marginTop: 8 }}>
              {msg}
            </div>
          )}
        </div>
      </div>

      <div className="two section">
        <div>
          <h3>SAP purchase requisitions (mock)</h3>

          <table className="small">
            <thead>
              <tr>
                <th>Requisition</th>
                <th>Material</th>
                <th className="num">Qty</th>
                <th className="num">Net (CAD)</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>

            <tbody>
              {prs.map((r) => (
                <tr key={r.PurchaseRequisition}>
                  <td className="mono">{r.PurchaseRequisition}</td>
                  <td>{r.items[0].Material}</td>
                  <td className="num">
                    {r.items[0].RequestedQuantity}
                  </td>
                  <td className="num">{r.NetAmount.toLocaleString()}</td>
                  <td>
                    {r.Status}
                    {r.PurchaseOrder ? ` → PO ${r.PurchaseOrder}` : ""}
                  </td>
                  <td>
                    {r.Status === "created" && (
                      <button
                        className="btn ghost sm"
                        onClick={() => convert(r.PurchaseRequisition)}
                      >
                        Convert to PO
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {!prs.length && (
            <p className="small muted">None yet. Only approved requests reach SAP.</p>
          )}
        </div>

        <div>
          <h3>Released purchase orders</h3>

          <table className="small">
            <thead>
              <tr>
                <th>PO</th>
                <th>Supplier</th>
                <th>Material</th>
                <th className="num">Net</th>
              </tr>
            </thead>

            <tbody>
              {pos.map((p) => (
                <tr key={p.PurchaseOrder}>
                  <td className="mono">{p.PurchaseOrder}</td>
                  <td>{p.Supplier}</td>
                  <td>{p.items[0].Material}</td>
                  <td className="num">
                    {p.NetAmount?.toLocaleString() ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {!pos.length && (
            <p className="small muted">No released POs yet.</p>
          )}
        </div>
      </div>
    </div>
  );
}

function Traces() {
  const [data, setData] = useState<Any>(null);
  const [sel, setSel] = useState<string | null>(null);
  const { run } = useRun(sel);

  useEffect(() => {
    const load = () =>
      rest<Any>("/api/integrations/runs")
        .then(setData)
        .catch(() => undefined);

    load();

    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  if (!data) return <div className="skeleton" />;

  return (
    <div className="split">
      <div>
        <table className="small">
          <thead>
            <tr>
              <th>Run</th>
              <th>Kind</th>
              <th>Name</th>
              <th>Status</th>
              <th className="num">Duration</th>
              <th className="num">Retries</th>
            </tr>
          </thead>

          <tbody>
            {data.recent.map((r: RunView) => (
              <tr
                key={r.id}
                className={`clickable ${sel === r.id ? "selected" : ""}`}
                onClick={() => setSel(r.id)}
              >
                <td className="mono">{r.id}</td>
                <td>{r.kind}</td>
                <td>{r.name}</td>
                <td>
                  <StatusPill status={r.status} />
                </td>
                <td className="num">{fmtMs(r.duration_ms)}</td>
                <td className="num">
                  {String(r.metrics?.retries ?? "—")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {!data.recent.length && (
          <p className="small muted">
            No runs yet. Launch a mission or a workflow.
          </p>
        )}
      </div>

      <div>
        {run ? (
          <>
            <div className="panel-head">
              <h3>{run.name}</h3>
              <StatusPill status={run.status} />
            </div>

            <div className="row" style={{ gap: 6, marginBottom: 8 }}>
              {Object.entries(run.nodes ?? {}).map(([k, n]) => (
                <span
                  key={k}
                  className={`tag ${
                    n.status === "failed"
                      ? "strong"
                      : n.status === "completed"
                        ? "soft"
                        : ""
                  }`}
                >
                  {n.label ?? k}: {n.status}
                  {(n.attempts ?? 0) > 1 ? ` ×${n.attempts}` : ""}
                </span>
              ))}
            </div>

            <RunTimeline run={run} height={320} />
          </>
        ) : (
          <p className="small muted">
            Select a run to see its agent decisions, tool calls, retries and
            approvals step by step.
          </p>
        )}
      </div>
    </div>
  );
}

function StatusAnalytics() {
  const [runs, setRuns] = useState<Any>(null);
  const [a, setA] = useState<Any>(null);

  useEffect(() => {
    const load = () => {
      rest<Any>("/api/integrations/runs")
        .then(setRuns)
        .catch(() => undefined);

      rest<Any>("/api/integrations/analytics")
        .then(setA)
        .catch(() => undefined);
    };

    load();

    const timer = setInterval(load, 6000);
    return () => clearInterval(timer);
  }, []);

  if (!runs || !a) return <div className="skeleton" />;

  const statusData = Object.entries(runs.by_status).map(([k, v]) => ({
    status: k.replace("_", " "),
    runs: v,
  }));

  return (
    <div>
      <div className="tiles">
        <div className="tile dark">
          <span className="k">Pending approvals</span>
          <b>{a.pending}</b>
          <span>{a.counts.proposed} requested in total</span>
        </div>

        <div className="tile">
          <span className="k">Approval rate</span>
          <b>
            {a.approval_rate === null
              ? "—"
              : `${Math.round(a.approval_rate * 100)}%`}
          </b>
          <span>
            {a.counts.approved} approved · {a.counts.rejected} rejected
          </span>
        </div>

        <div className="tile">
          <span className="k">Median turnaround</span>
          <b>
            {a.turnaround_s.median === null
              ? "—"
              : fmtMs(a.turnaround_s.median * 1000)}
          </b>
          <span>request → human decision</span>
        </div>

        <div className="tile">
          <span className="k">Actions executed</span>
          <b>{a.counts.executed_writes + a.counts.executed_reads}</b>
          <span>
            {a.counts.executed_writes} approved writes · {a.counts.failed} failed
          </span>
        </div>

        <div className="tile">
          <span className="k">Hands-on time saved (est.)</span>
          <b>{Math.round(a.minutes_saved_est)} min</b>
          <span>assumption-based</span>
        </div>

        <div className="tile">
          <span className="k">Sheets tracking</span>
          <b style={{ fontSize: 22 }}>{a.sheets.mode}</b>
          <span>
            {a.sheets.written} rows written · {a.sheets.failed} failed
          </span>
        </div>
      </div>

      <div className="three section">
        <div>
          <h3>Workflow runs by status</h3>
          <div style={{ height: 200 }}>
            <ResponsiveContainer>
              <BarChart data={statusData}>
                <CartesianGrid stroke="#eeeef7" />
                <XAxis dataKey="status" fontSize={11} />
                <YAxis fontSize={11} allowDecimals={false} />
                <Tooltip />
                <Bar
                  dataKey="runs"
                  fill="#2f3169"
                  radius={[4, 4, 0, 0]}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div>
          <h3>Actions per connector tool</h3>
          <div style={{ height: 200 }}>
            <ResponsiveContainer>
              <BarChart
                data={a.by_tool}
                layout="vertical"
                margin={{ left: 60 }}
              >
                <CartesianGrid stroke="#eeeef7" />
                <XAxis type="number" fontSize={11} allowDecimals={false} />
                <YAxis
                  type="category"
                  dataKey="tool"
                  fontSize={10}
                  width={150}
                />
                <Tooltip />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Bar dataKey="approved" stackId="a" fill="#2f3169" />
                <Bar dataKey="rejected" stackId="a" fill="#b5b6d6" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div>
          <h3>Per workflow</h3>
          <table className="small">
            <thead>
              <tr>
                <th>Workflow</th>
                <th className="num">Runs</th>
                <th className="num">Done</th>
                <th className="num">Waiting</th>
                <th className="num">Failed</th>
              </tr>
            </thead>
            <tbody>
              {runs.by_workflow.map((w: Any) => (
                <tr key={w.name}>
                  <td>{w.name}</td>
                  <td className="num">{w.runs}</td>
                  <td className="num">{w.completed}</td>
                  <td className="num">{w.waiting}</td>
                  <td className="num">{w.failed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <p className="tiny muted">{a.assumption}</p>
    </div>
  );
}

function Settings({
  status,
  reload,
}: {
  status: Any;
  reload: () => void;
}) {
  const [msg, setMsg] = useState<string | null>(null);

  const testSheets = async () => {
    try {
      const s = await rest<Any>("/api/integrations/sheets/test", {
        body: {},
      });

      setMsg(
        `Sheets (${s.mode}): ${s.written} written, last error ${
          s.last_error ?? "none"
        }`,
      );

      reload();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  };

  const copy = (t: string) => {
    try {
      void navigator.clipboard.writeText(t);
      setMsg("Copied.");
    } catch {
      /* clipboard blocked */
    }
  };

  const cfg = JSON.stringify(status.mcp.client_config, null, 2);

  return (
    <div
      className="cols2"
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(0, 1.2fr) minmax(0, 1fr)",
        gap: 24,
        alignItems: "start",
      }}
    >
      <div>
        <h3>Connectors and feature flags</h3>

        <table className="small" style={{ tableLayout: "fixed" }}>
          <thead>
            <tr>
              <th>Connector</th>
              <th>State</th>
              <th>Cost</th>
              <th>Switch</th>
            </tr>
          </thead>

          <tbody>
            {status.connectors.map((c: Any) => (
              <tr key={c.id}>
                <td>
                  <b>{c.label}</b>
                  {c.note && <div className="tiny muted">{c.note}</div>}
                </td>
                <td>
                  <span
                    className={`sev ${
                      c.enabled ? "sev-critical" : "sev-low"
                    }`}
                  >
                    {c.enabled ? (c.mode ?? "on") : "off"}
                  </span>
                </td>
                <td className="tiny">{c.cost}</td>
                <td className="mono tiny">{c.flag}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <p className="tiny muted">
          Flags are environment variables (backend/.env locally, Variables on
          Railway). Changing one needs an API restart.
        </p>

        <h3 style={{ marginTop: 12 }}>LLM provider</h3>

        <div className="kv">
          <div>
            <span>Provider</span>
            <b>{status.llm.provider}</b>
          </div>
          <div>
            <span>Small</span>
            <b>{status.llm.small}</b>
          </div>
          <div>
            <span>Large</span>
            <b>{status.llm.large}</b>
          </div>
          <div>
            <span>Circuit</span>
            <b>{status.llm.circuit}</b>
          </div>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gap: 12,
          alignContent: "start",
        }}
      >
        <div className="panel" style={{ boxShadow: "none" }}>
          <div className="panel-head">
            <h3>Google Sheets tracking</h3>
            <button className="btn sm" onClick={testSheets}>
              Send test row
            </button>
          </div>

          <div className="kv">
            <div>
              <span>Mode</span>
              <b>{status.sheets.mode}</b>
            </div>
            <div>
              <span>Rows written</span>
              <b>{status.sheets.written}</b>
            </div>
            <div>
              <span>Pending</span>
              <b>{status.sheets.pending}</b>
            </div>
            <div>
              <span>Last error</span>
              <b>{status.sheets.last_error ?? "none"}</b>
            </div>
          </div>

          {status.sheets.spreadsheet_url ? (
            <a
              className="small"
              href={status.sheets.spreadsheet_url}
              target="_blank"
              rel="noreferrer"
            >
              Open the tracking sheet ↗
            </a>
          ) : (
            <p className="tiny muted">
              Local CSV mode. Set GOOGLE_SHEETS_SPREADSHEET_ID and
              GOOGLE_SERVICE_ACCOUNT_JSON_B64 to write to a real Google Sheet
              (free).
            </p>
          )}
        </div>

        <div className="panel" style={{ boxShadow: "none" }}>
          <div className="panel-head">
            <h3>MCP server</h3>
            <span
              className={`sev ${
                status.mcp.enabled ? "sev-critical" : "sev-low"
              }`}
            >
              {status.mcp.enabled ? "enabled" : "off"}
            </span>
          </div>

          <p className="tiny muted" style={{ marginTop: 0 }}>
            Exposes SAP, knowledge search, engine health and approval status to
            any MCP client. Requests always wait for a human here.
          </p>

          <pre className="code">{status.mcp.command}</pre>

          <div className="row">
            <span className="small">
              <b>Client config</b> (Claude Desktop / Claude Code)
            </span>
            <button
              className="btn ghost sm"
              onClick={() => copy(cfg)}
            >
              Copy
            </button>
          </div>

          <pre className="code">{cfg}</pre>
        </div>

        <div className="panel" style={{ boxShadow: "none" }}>
          <h3>Secrets (configured / not configured only)</h3>

          {status.secrets.map((s: Any) => (
            <div
              key={s.name}
              className="row small"
              style={{ justifyContent: "space-between" }}
            >
              <span className="mono tiny">{s.name}</span>
              <span className={s.configured ? "" : "muted"}>
                {s.configured ? `✓ set (${s.source})` : "not set"}
              </span>
            </div>
          ))}
        </div>

        <div className="panel" style={{ boxShadow: "none" }}>
          <h3>Databricks / PySpark</h3>

          <div className="small">
            {status.databricks.enabled
              ? status.databricks.configured
                ? `Job ${status.databricks.job_id} on ${status.databricks.host}`
                : "Enabled, but not configured"
              : "Off"}
          </div>

          <pre className="code">{status.databricks.local}</pre>
        </div>

        {msg && <div className="note">{msg}</div>}
      </div>
    </div>
  );
}

function Audit() {
  const [d, setD] = useState<Any>(null);
  const [open, setOpen] = useState<number | null>(null);

  useEffect(() => {
    rest<Any>("/api/integrations/audit?limit=80")
      .then(setD)
      .catch(() => undefined);
  }, []);

  if (!d) return <div className="skeleton" />;

  return (
    <div>
      <p className="small">
        <span
          className={`sev ${
            d.verify.valid ? "sev-low" : "sev-critical"
          }`}
        >
          {d.verify.valid
            ? "✓ hash chain intact"
            : `✕ chain broken at record ${d.verify.broken_at}`}
        </span>{" "}
        {d.verify.records} records
        {d.verify.head ? ` · head ${d.verify.head}` : ""}. Every record
        carries the SHA-256 of the previous one, so edits or deletions are
        detected. Credentials are redacted.
      </p>

      <table className="small">
        <thead>
          <tr>
            <th>Time</th>
            <th>Event</th>
            <th>Actor</th>
            <th>Tool</th>
            <th>Hash</th>
          </tr>
        </thead>

        <tbody>
          {d.records.map((r: Any, i: number) => (
            <Fragment key={r.hash}>
              <tr
                className="clickable"
                onClick={() => setOpen(open === i ? null : i)}
              >
                <td className="mono tiny">
                  {new Date(r.ts * 1000).toLocaleString()}
                </td>
                <td>{r.event}</td>
                <td>{r.actor}</td>
                <td className="mono tiny">{r.detail.tool ?? ""}</td>
                <td className="mono tiny">{r.hash.slice(0, 12)}</td>
              </tr>

              {open === i && (
                <tr>
                  <td colSpan={5}>
                    <JsonView value={r.detail} />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>

      {!d.records.length && (
        <p className="small muted">No audited actions yet.</p>
      )}
    </div>
  );
}
