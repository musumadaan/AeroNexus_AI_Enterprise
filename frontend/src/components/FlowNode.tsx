import { Handle, Position, type NodeProps } from "@xyflow/react";
import { fmtMs } from "../lib/runs";

export interface FlowNodeData extends Record<string, unknown> {
  label: string;
  category: string;
  type: string;
  risk?: string;
  status?: string;
  attempts?: number;
  ms?: number;
  meta?: string;
  selected?: boolean;
  faults?: string;
}

const STATUS_TEXT: Record<string, string> = {
  running: "running", retrying: "retrying", completed: "done", failed: "failed", skipped: "skipped", waiting: "awaiting approval",
  rejected: "rejected", pending: "",
};

export function FlowNode({ data, selected }: NodeProps) {
  const d = data as FlowNodeData;
  const st = d.status ?? "";
  const isCond = d.type === "logic.condition";
  const isTrigger = d.category === "trigger";
  return (
    <div className={`fnode st-${st} ${selected ? "selected" : ""}`}>
      <span className="stripe" style={{ background: CAT_COLOR[d.category] ?? "#b5b6d6" }} />
      {!isTrigger && <Handle type="target" position={Position.Left} />}
      {STATUS_TEXT[st] && <span className="st">{STATUS_TEXT[st]}{d.attempts && d.attempts > 1 ? ` · try ${d.attempts}` : ""}</span>}
      <div className="cat"><span>{d.category}</span>
        {d.risk && d.risk !== "none" && d.risk !== "read" && <span className={`badge risk-${d.risk}`}>{d.risk}</span>}
        {d.faults && <span className="badge">⚡ {d.faults}</span>}
      </div>
      <div className="ttl">{d.label}</div>
      <div className="meta">{d.meta ?? d.type}{d.ms !== undefined && st && st !== "pending" ? ` · ${fmtMs(d.ms)}` : ""}</div>
      {isCond ? (
        <>
          <Handle type="source" id="true" position={Position.Right} style={{ top: "32%" }} className="h-true" />
          <Handle type="source" id="false" position={Position.Right} style={{ top: "74%" }} className="h-false" />
          <span className="hlabel" style={{ top: "22%", right: -34 }}>true</span>
          <span className="hlabel" style={{ top: "64%", right: -38 }}>false</span>
        </>
      ) : <Handle type="source" position={Position.Right} />}
    </div>
  );
}

export function TerminalNode({ data }: NodeProps) {
  const d = data as { label: string };
  return (
    <div style={{ width: 54, height: 54, borderRadius: "50%", display: "grid", placeItems: "center", background: "#2f3169", color: "#fff",
                  fontSize: 11, fontWeight: 700, letterSpacing: ".06em" }}>
      {d.label !== "START" && <Handle type="target" position={Position.Left} />}
      {d.label}
      {d.label === "START" && <Handle type="source" position={Position.Right} />}
    </div>
  );
}

export const CAT_COLOR: Record<string, string> = {
  trigger: "#2f3169", agent: "#53558c", model: "#8284ac", document: "#8284ac", knowledge: "#b5b6d6", calc: "#b5b6d6",
  integration: "#2f3169", data: "#b5b6d6", logic: "#dedff1", human: "#2f3169", supervisor: "#2f3169",
};

export const nodeTypes = { flow: FlowNode, terminal: TerminalNode };
