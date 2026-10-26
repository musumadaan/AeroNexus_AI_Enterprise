import { Fragment, useState } from "react";
import type { Claim, Source } from "../api";

/** Renders an answer whose sentences carry [c:id] citations; unsupported claims are underlined. */
export function Grounded({ text, sources, claims = [], groundedness, decision }: {
  text: string; sources: Source[]; claims?: Claim[]; groundedness?: number; decision?: string;
}) {
  const [active, setActive] = useState<string | null>(null);
  const bad = new Set(claims.filter((c) => !c.supported).map((c) => c.sentence.replace(/\s*\[c:[^\]]+\]/g, "").trim()));
  const parts = text.split(/(\[c:[^\]]+\])/g);
  const cited = Array.from(new Set((text.match(/\[c:([^\]]+)\]/g) ?? []).map((m) => m.slice(3, -1))));
  const num = (id: string) => cited.indexOf(id) + 1;
  return (
    <div>
      <p className="answer" style={{ margin: 0 }}>
        {parts.map((p, i) => {
          const m = p.match(/^\[c:([^\]]+)\]$/);
          if (m) {
            const id = m[1];
            return (
              <button key={i} className="cite" onClick={() => setActive(active === id ? null : id)}
                      aria-label={`Show source ${num(id)}`}>[{num(id)}]</button>
            );
          }
          return <Fragment key={i}><span className={bad.has(p.trim()) ? "claim-bad" : undefined}>{p}</span></Fragment>;
        })}
      </p>
      {groundedness !== undefined && (
        <p className="ground" style={{ margin: "6px 0 0" }}>
          {decision === "abstain" ? "Declined to answer: the sources don't support a confident answer." :
            <>Checked against sources: <b>{Math.round(groundedness * 100)}%</b> of statements supported{decision === "pruned" ? " (unsupported statements removed)" : ""}.</>}
        </p>
      )}
      {cited.length > 0 && (
        <div className="sources">
          {cited.map((id) => {
            const s = sources.find((x) => x.id === id);
            return (
              <div key={id} className={`source ${active === id ? "hl" : ""}`}>
                <b>[{num(id)}]</b> {s ? <>{s.title}{s.section ? ` › ${s.section}` : ""}: {s.text.slice(0, 220)}{s.text.length > 220 ? "…" : ""}</> : id}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
