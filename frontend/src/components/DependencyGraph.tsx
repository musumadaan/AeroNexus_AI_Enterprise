import { Background, Controls, MarkerType, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useMemo, useState } from "react";
import { label } from "../lib/cloud";
import { nodeTypes } from "./FlowNode";

interface GNode { id: string; kind: string; label: string; detail: string; impacted: boolean }
interface GEdge { source: string; target: string; relation: string }

// where each kind of record sits around the changed part (angle in degrees, ring radius)
const PLACE: Record<string, [number, number]> = {
  assembly: [-90, 190], sales_order: [-25, 250], customer: [-5, 500], work_order: [40, 250], material_po: [125, 230],
  supplier: [180, 260], inventory: [-140, 240],
};
const KIND_LABEL: Record<string, string> = { part: "changed part", assembly: "assembly", sales_order: "sales order", customer: "customer",
  work_order: "work order", material_po: "material PO", supplier: "supplier", inventory: "old stock" };

export function DependencyGraph({ graph }: { graph: { nodes: GNode[]; edges: GEdge[] } }) {
  const [sel, setSel] = useState<string | null>(null);
  const nodes: Node[] = useMemo(() => {
    const byKind: Record<string, GNode[]> = {};
    graph.nodes.forEach((n) => { (byKind[n.kind] ??= []).push(n); });
    return graph.nodes.map((n) => {
      let x = 0, y = 0;
      if (n.kind !== "part") {
        const [deg, r] = PLACE[n.kind] ?? [90, 300];
        const group = byKind[n.kind];
        const k = group.indexOf(n);
        const a = ((deg + (k - (group.length - 1) / 2) * 22) * Math.PI) / 180;
        x = r * Math.cos(a) * 1.15; y = r * Math.sin(a) * 0.8;
      }
      return {
        id: n.id, type: "flow", position: { x: x - 100, y: y - 30 }, selected: sel === n.id,
        data: { label: n.label, category: KIND_LABEL[n.kind] ?? n.kind, type: n.kind, meta: n.detail,
                status: n.kind === "part" ? "waiting" : n.impacted ? "completed" : "skipped" },
      };
    });
  }, [graph, sel]);
  const edges: Edge[] = useMemo(() => graph.edges.map((e) => ({
    id: `${e.source}-${e.target}`, source: e.source, target: e.target, label: e.relation, animated: true,
    markerEnd: { type: MarkerType.ArrowClosed, color: "#2f3169" },
  })), [graph]);
  const s = graph.nodes.find((n) => n.id === sel);
  return (
    <div className="split-r">
      <div className="flow-wrap" style={{ height: 520 }}>
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.08 }} nodesConnectable={false}
                   onNodeClick={(_, n) => setSel(n.id)} proOptions={{ hideAttribution: true }} zoomOnScroll={false} preventScrolling={false}>
          <Background color="#dedff1" gap={22} /><Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <div className="panel">
        <h3>Affected-component analysis</h3>
        {s ? (
          <div className="note" style={{ marginBottom: 10 }}><b>{s.label}</b> · {KIND_LABEL[s.kind]}<br />{s.detail}</div>
        ) : <p className="small muted">Click a record in the graph. Every node is a real ERP/CRM record found by the read-only tools.</p>}
        <table className="small">
          <thead><tr><th>Record</th><th>Kind</th><th>Action after approval</th></tr></thead>
          <tbody>{graph.nodes.filter((n) => n.kind !== "part").map((n) => (
            <tr key={n.id} className={`clickable ${sel === n.id ? "selected" : ""}`} onClick={() => setSel(n.id)}>
              <td><b>{n.label}</b></td><td>{label(KIND_LABEL[n.kind] ?? n.kind)}</td>
              <td>{ACTION[n.kind] ?? "review"}{!n.impacted ? " (nothing to do)" : ""}</td>
            </tr>))}</tbody>
        </table>
      </div>
    </div>
  );
}

const ACTION: Record<string, string> = {
  assembly: "Re-check fit and BOM", sales_order: "Re-price open quantity", customer: "Send change notice",
  work_order: "Hold WIP; scrap or rework", material_po: "Amend or cancel PO", supplier: "Order new material", inventory: "Write off or recover stock",
};
