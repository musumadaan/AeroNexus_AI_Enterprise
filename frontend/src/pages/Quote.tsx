import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { gql, Q, type QuoteResult, type SheetSummary } from "../api";
import { Grounded } from "../components/Grounded";
import { SheetView } from "../components/SheetView";
import { label, money } from "../lib/cloud";
import { useAsync } from "../lib/useAsync";

const TAG_COPY: Record<string, string> = {
  ocr_noise: "OCR damage", moved_title: "Moved title block", conflict: "Conflicting values",
  injection: "Injected instructions", dropped_field: "Missing field",
};

export function QuotePage() {
  const sheets = useAsync(() => gql<{ sheets: SheetSummary[] }>(Q.sheets).then((d) => d.sheets));
  const [id, setId] = useState("G0001");
  const [res, setRes] = useState<QuoteResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [focus, setFocus] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setBusy(true);
    setErr(null);
    setFocus(null);
    gql<{ quote: QuoteResult }>(Q.quote, { id })
      .then((d) => live && setRes(d.quote))
      .catch((e) => live && setErr(e.message))
      .finally(() => live && setBusy(false));
    return () => { live = false; };
  }, [id]);

  const fields = res ? Object.values(res.extraction.fields) : [];
  const order = { missing: 0, needs_review: 1, auto_accepted: 2 } as const;
  fields.sort((a, b) => order[a.status] - order[b.status]);
  const pending = res?.proposals.filter((p) => p.status === "pending_approval").length ?? 0;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Drawing to quote</h1>
          <p className="lede">Reads a customer drawing, checks it against shop policy, prices it with an honest range, and drafts
            the ERP and CRM entries for a person to approve.</p>
        </div>
        <label className="row small">Drawing
          <select value={id} onChange={(e) => setId(e.target.value)} aria-label="Choose drawing">
            {(sheets.data ?? []).map((s) => (
              <option key={s.id} value={s.id}>{s.id} · {s.title}{s.tags.length ? ` (${s.tags.map((t) => TAG_COPY[t] ?? t).join(", ")})` : ""}</option>
            ))}
          </select>
        </label>
      </div>
      {err && <div className="err">{err}</div>}
      {!res && busy && <div className="panel"><div className="skeleton" /><div className="skeleton" /><div className="skeleton" /></div>}
      {res && (
        <div className="split" style={{ opacity: busy ? 0.55 : 1 }}>
          <div>
            <SheetView sheet={res.sheet} fields={res.extraction.fields} focus={focus} onSelect={setFocus}
                       caption={`${res.extraction.llm_calls} LLM call${res.extraction.llm_calls === 1 ? "" : "s"}`} />
            <div className="section">
              <h2>Why this needs attention</h2>
              {res.issues.length ? res.issues.map((i, k) => (
                <div key={k} className={`issue ${i.severity}`}><i /><div>{i.message}</div></div>
              )) : <p className="muted">No blocking issues. Every required field was found and checks passed.</p>}
            </div>
            <div className="section">
              <h2>What the policy says</h2>
              <Grounded text={res.rationale.text} sources={res.rationale.sources} claims={res.rationale.claims}
                        groundedness={res.rationale.groundedness} decision={res.rationale.decision} />
            </div>
          </div>
          <div>
            <div className="figures">
              <div className="figure"><b>{res.summary.auto}</b><span>accepted automatically</span></div>
              <div className={`figure ${res.summary.review ? "alert" : ""}`}><b>{res.summary.review}</b><span>to confirm</span></div>
              <div className={`figure ${res.summary.missing ? "alert" : ""}`}><b>{res.summary.missing}</b><span>missing</span></div>
            </div>
            <div className="panel section flush">
              <table>
                <thead><tr><th>Field</th><th>Value</th><th>Confidence</th><th>Status</th></tr></thead>
                <tbody>
                  {fields.map((f) => (
                    <tr key={f.name} className={`clickable ${focus === f.name ? "selected" : ""}`} onClick={() => setFocus(f.name)}>
                      <td>{label(f.name)}<div className="src">{f.source === "none" ? "—" : `via ${f.source === "llm" ? "language model" : f.source.toUpperCase()}`}</div></td>
                      <td className="val">{f.value ?? <span className="muted">not found</span>}
                        {f.note && <div className="src">{f.note}</div>}
                        {f.alternatives.length > 0 && <div className="src">also seen: {f.alternatives.join(", ")}</div>}</td>
                      <td><span className={`confbar ${f.confidence < 0.9 ? "low" : ""}`}><i style={{ width: `${f.confidence * 100}%` }} /></span>
                        <span className="small"> {Math.round(f.confidence * 100)}</span></td>
                      <td><span className={`status ${f.status}`}>{label(f.status).replace("Auto accepted", "Accepted")}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="panel section">
              <h2>Price</h2>
              {res.price ? (
                <>
                  <div className="figures">
                    <div className="figure"><b>{money(res.price.unit_price)}</b><span>per unit</span></div>
                    <div className="figure"><b>{money(res.price.total)}</b><span>total</span></div>
                  </div>
                  <p className="small" style={{ marginBottom: 0 }}>
                    90% of similar past jobs landed between <b>{money(res.price.low)}</b> and <b>{money(res.price.high)}</b> per unit
                    (conformal interval). Catalog match: {res.catalog_match?.description}.
                  </p>
                  {res.assumptions.map((a) => <p key={a} className="src" style={{ margin: "4px 0 0" }}>Assumption: {a}</p>)}
                </>
              ) : <p className="muted">Not priced: required inputs are missing or inconsistent.</p>}
            </div>
            <div className="panel section">
              <h2>Drafted actions</h2>
              {res.proposals.length ? (
                <p style={{ margin: 0 }}>{pending} action{pending === 1 ? "" : "s"} drafted for {res.account?.name ?? "the customer"}.
                  Nothing is sent or saved until someone approves it. <Link to="/review">Open the approval queue</Link></p>
              ) : <p className="muted" style={{ margin: 0 }}>No actions drafted. Documents with suspicious instructions are never acted on.</p>}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
