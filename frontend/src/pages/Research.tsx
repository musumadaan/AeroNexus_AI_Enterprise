import { gql, Q } from "../api";
import { label } from "../lib/cloud";
import { useAsync } from "../lib/useAsync";

interface ClaimRow { id: string; module: string; claim: string; evidence: string; source_title: string; url: string; source_type: string;
  year: number; how_used: string; verified: boolean; problems: string[]; peer_reviewed: boolean }

const MODULES = ["change", "pdm", "safety", "quote", "mro", "ap", "evals", "rag", "datasets"];
const MOD_COPY: Record<string, string> = { quote: "Drawing to quote", change: "Engineering change", mro: "Directive tracking",
  pdm: "Predictive maintenance", safety: "Safety analytics",
  ap: "Invoice matching", evals: "Evaluation and judges", rag: "Grounded answers", datasets: "Training data" };

export function ResearchPage() {
  const { data, error } = useAsync(() => gql<{ research: ClaimRow[] }>(Q.research).then((d) => d.research));
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Why each part exists</h1>
          <p className="lede">Each problem statement is tied to a source. Before a claim is shown, its numbers and wording are checked
            against a short excerpt of that source; the test suite fails if any claim drifts.</p>
        </div>
      </div>
      {error && <div className="err">{error}</div>}
      {MODULES.map((m) => {
        const rows = (data ?? []).filter((c) => c.module === m);
        if (!rows.length) return null;
        return (
          <section key={m} className="section">
            <h2>{MOD_COPY[m]}</h2>
            {rows.map((c) => (
              <div key={c.id} className="panel" style={{ marginBottom: 12 }}>
                <p style={{ margin: 0 }}><b>{c.claim}</b></p>
                <p className="small" style={{ margin: "6px 0" }}>Excerpt: “{c.evidence}”</p>
                <p className="small muted" style={{ margin: 0 }}>
                  <a href={c.url} target="_blank" rel="noreferrer">{c.source_title}</a> · {c.year} ·{" "}
                  <span className={`tag ${c.peer_reviewed ? "" : "red"}`}>{label(c.source_type)}</span>
                  <span className={`status ${c.verified ? "auto_accepted" : "missing"}`}>{c.verified ? "Checked against excerpt" : c.problems.join("; ")}</span>
                </p>
                <p className="small" style={{ margin: "6px 0 0" }}>Used for: {c.how_used}</p>
              </div>))}
          </section>
        );
      })}
    </>
  );
}
