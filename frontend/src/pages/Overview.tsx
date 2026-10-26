import { useState } from "react";
import { Link } from "react-router-dom";
import { gql, Q } from "../api";
import { useAsync } from "../lib/useAsync";
import { Icon } from "../components/Icons";

const NEW = [
  { to: "/twin", icon: "twin", title: "3D engine digital twin", text: "Every module coloured by condition, with live sensors and remaining life." },
  { to: "/simulator", icon: "sim", title: "Maintenance simulator", text: "Streamed telemetry, uncertainty bands, fault injection and anomaly detection." },
  { to: "/change", icon: "change", title: "Change orders, traced", text: "Animated revision compare, dependency graph and receipt-backed cost." },
  { to: "/mission", icon: "mission", title: "Multi-agent mission control", text: "LangGraph supervisor, parallel agents, retries and an approval gate." },
  { to: "/builder", icon: "builder", title: "Workflow builder", text: "Drag, connect, run, test on history, replay and compare automations." },
  { to: "/observability", icon: "obs", title: "Evaluation and observability", text: "MLflow runs, regression checks, tokens, latency and agent reliability." },
];

interface Arch {
  layers: Array<{ id: string; label: string; items: string[] }>;
  modules: Array<{ id: string; label: string; path: string; layers: string[] }>;
}
const MODULE_LINK: Record<string, string> = { change: "/change", pdm: "/pdm", safety: "/safety", quote: "/quote", mro: "/mro", ap: "/ap" };

export function OverviewPage() {
  const arch = useAsync(() => gql<{ architecture: Arch }>(Q.architecture));
  const rep = useAsync(() => gql<{ evalReport: any }>(Q.report).then((d) => d.evalReport)); // eslint-disable-line @typescript-eslint/no-explicit-any
  const [hot, setHot] = useState<string>("change");
  const a = arch.data?.architecture;
  const m = a?.modules.find((x) => x.id === hot);
  const lit = new Set(m?.layers ?? []);
  const r = rep.data;
  return (
    <>
      <section className="hero">
        <svg className="hero-art" width="460" height="300" viewBox="0 0 460 300" aria-hidden="true">
          <g fill="none" stroke="#8284ac" strokeOpacity=".45">
            {[150, 115, 80, 45].map((r) => <circle key={r} cx="300" cy="150" r={r} />)}
            {Array.from({ length: 24 }).map((_, i) => { const a = (i * Math.PI) / 12; return <path key={i} d={`M300 150 L${300 + 150 * Math.cos(a)} ${150 + 150 * Math.sin(a)}`} strokeOpacity=".2" />; })}
          </g>
          <circle cx="300" cy="150" r="10" fill="#dedff1" />
        </svg>
        <p className="eyebrow">AeroNexus AI · aerospace engineering intelligence</p>
        <h1>Engineering decisions you can trace back to evidence</h1>
        <p className="lede">Drawings, engine telemetry, maintenance logbooks, safety reports and supplier invoices go in. Agents analyse them with
          models trained on free public aviation data, verified calculations and cited retrieval. Every action waits for a named approver.</p>
        <div className="row" style={{ marginTop: 18 }}>
          <Link className="btn soft" to="/learn"><Icon name="learn" size={15} />Start the tour</Link>
          <Link className="btn" to="/mission" style={{ background: "transparent", color: "#fff", borderColor: "rgba(222,223,241,.4)" }}><Icon name="mission" size={15} />Mission control</Link>
          <Link className="btn" to="/twin" style={{ background: "transparent", color: "#fff", borderColor: "rgba(222,223,241,.4)" }}><Icon name="twin" size={15} />3D engine twin</Link>
        </div>
      </section>
      <div className="cards-3" style={{ marginBottom: 22 }}>
        {NEW.map((x) => (
          <Link key={x.to} className="card" to={x.to}>
            <div className="ico"><Icon name={x.icon} /></div>
            <h3>{x.title}</h3>
            <div className="small muted">{x.text}</div>
          </Link>
        ))}
      </div>
      {arch.error && <div className="err">{arch.error}</div>}
      {r && (
        <div className="figures">
          {r.pdm?.available && <div className="figure"><b>{r.pdm.fd001_rmse}</b><span>cycles RUL error on NASA C-MAPSS FD001</span></div>}
          {r.safety?.available && <div className="figure"><b>{r.safety.test_auroc}</b><span>safety triage AUROC on unseen NTSB years</span></div>}
          <div className="figure"><b>{Math.round(r.change.receipt_reproducibility * 100)}%</b><span>change-order numbers that re-compute exactly</span></div>
          <div className="figure"><b>{r.ap.false_auto_match}</b><span>bad invoices auto-matched</span></div>
          <div className={`figure ${r.gate.passed ? "" : "alert"}`}><b>{r.gate.checks.filter((c: { passed: boolean }) => c.passed).length}/{r.gate.checks.length}</b><span>release gates passing</span></div>
        </div>
      )}
      {a && (
        <section className="section">
          <h2>The eight-stage pipeline behind every workflow</h2>
          <p className="small muted">Pick a workflow to see which stages it uses.</p>
          <div className="row" style={{ marginBottom: 14 }}>
            {a.modules.map((x) => (
              <button key={x.id} className={`btn ${hot === x.id ? "primary" : ""}`} onClick={() => setHot(x.id)}>{x.label}</button>
            ))}
          </div>
          <div className="panel flush">
            <div className="arch" style={{ gridTemplateColumns: `repeat(${a.layers.length}, minmax(118px, 1fr))` }}>
              {a.layers.map((l, i) => (
                <div key={l.id} className={`layer ${lit.has(l.id) ? "hot" : ""}`}>
                  <div className="step">Stage {i + 1}</div>
                  <h3>{l.label}</h3>
                  <ul>{l.items.map((it) => <li key={it}>{it}</li>)}</ul>
                </div>
              ))}
            </div>
          </div>
          <p className="loopnote">{m?.path}. Reviewer corrections become new labelled data, and the assurance stage re-tests every
            stage on each release. <Link to={MODULE_LINK[hot]}>Open this workflow</Link></p>
        </section>
      )}
      <section className="section two">
        <div className="panel">
          <h3>Reliable by construction</h3>
          <ul className="small" style={{ paddingLeft: 18, margin: 0 }}>
            <li>Numbers come from versioned formulas with receipts, never from a language model</li>
            <li>Model answers must appear word for word in the document or they are discarded</li>
            <li>Every case is an event log: it can be replayed and audited, and failed steps resume where they stopped</li>
            <li>Retrieval only returns the policy revision in force on the date, and flags contradictions</li>
          </ul>
        </div>
        <div className="panel">
          <h3>Responsible by default</h3>
          <ul className="small" style={{ paddingLeft: 18, margin: 0 }}>
            <li>Roles decide who can run, approve and evaluate; agents can never approve</li>
            <li>Safety and maintenance outputs are decision support; qualified people decide</li>
            <li>Every dataset records its source, license and checksum; synthetic results are labelled as such</li>
            <li>Customer messages say an AI assistant prepared them</li>
          </ul>
        </div>
      </section>
    </>
  );
}
