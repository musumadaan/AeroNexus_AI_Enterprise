import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Icon } from "../components/Icons";

interface Slide { title: string; points: string[]; img?: string; cap?: string; try?: { to: string; label: string } }
interface Course { id: string; title: string; blurb: string; minutes: number; slides: Slide[] }

const COURSES: Course[] = [
  {
    id: "start", title: "Getting started", blurb: "What AeroNexus does, roles, and how to move around", minutes: 3,
    slides: [
      { title: "What AeroNexus does", img: "/learn/overview.jpg", cap: "Overview page",
        points: ["Reads drawing revisions, engine sensor histories, safety reports, logbooks and invoices.",
          "Checks them with deterministic rules, predicts with models trained on free public aviation data, and drafts ERP, CRM and maintenance actions.",
          "Anything uncertain is marked for a person. Nothing is written to another system without a named approver."],
        try: { to: "/", label: "Open the overview" } },
      { title: "Pick your role first", img: "/learn/roles.jpg", cap: "Bottom of the left rail",
        points: ["Type your name and choose a role at the bottom left. The app sends them with every request.",
          "Viewer reads. Engineer runs workflows. Approver signs off. Admin also runs evaluations.",
          "Agents act as their own identity and can never approve anything, including their own proposals."] },
      { title: "How the app is organised", img: "/learn/overview.jpg", cap: "Navigation groups",
        points: ["Workflows: change orders, digital twin, simulator, safety, quotes, directives and invoices.",
          "Automation: mission control (multi-agent), the workflow builder, the work queue and approvals.",
          "Assurance: observability, the evaluation gate, data licences and the research behind each claim."] },
      { title: "Real data versus synthetic data", img: "/learn/evals.jpg", cap: "Every metric is labelled",
        points: ["NASA C-MAPSS (engines) and NTSB (accidents) are real public datasets; their results are labelled public.",
          "Drawings, ERP and CRM records are synthetic and labelled synthetic. Treat those scores as upper bounds.",
          "Ask the chat button (bottom right) about any result; it answers with citations or says it can't."] },
    ],
  },
  {
    id: "change", title: "Engineering change orders", blurb: "Compare revisions, see the impact, trace the cost, approve", minutes: 4,
    slides: [
      { title: "Open a change", img: "/learn/change_summary.jpg", cap: "Change orders · Summary",
        points: ["Pick a revision pair and press Compare revisions. Six steps run: extract, diff, validate, impact, cost, classify.",
          "The case lands in Verified or Needs review. It is saved, so you can come back from the work queue.",
          "Severity is a versioned rule: a part already in production is Critical."], try: { to: "/change", label: "Open change orders" } },
      { title: "Watch the revision change", img: "/learn/change_compare.jpg", cap: "Compare revisions tab",
        points: ["Swipe slides Rev B over Rev A; Blink flips between them; Side by side shows both.",
          "The changed value is clouded on both sheets, the way an engineer marks a drawing.",
          "The section view morphs between the two dimensions and states the delta in mm and %."] },
      { title: "See everything the change touches", img: "/learn/change_graph.jpg", cap: "Dependency graph tab",
        points: ["Assemblies, sales orders, customers, work orders, material POs, suppliers and old stock, all from read-only ERP/CRM calls.",
          "Click a record for its details; the table lists the action each one needs after approval."] },
      { title: "Follow every number", img: "/learn/change_cost.jpg", cap: "Cost trace tab",
        points: ["The waterfall shows how re-pricing, scrap and obsolete stock add up to the total.",
          "Each receipt lists inputs, rule versions, output, a hash and the formula's source code.",
          "Re-check from event log rebuilds the case and re-runs every formula bit for bit."] },
      { title: "Approve and release", img: "/learn/change_summary.jpg", cap: "Decision panel",
        points: ["Switch to the approver role, add a note saying what you checked, and approve.",
          "The ECO is created in the ERP and customers are notified, with an AI disclosure. The case becomes Executed."] },
    ],
  },
  {
    id: "twin", title: "Engine digital twin", blurb: "A 3D engine coloured by module condition", minutes: 3,
    slides: [
      { title: "Read the twin", img: "/learn/twin.jpg", cap: "Engine digital twin",
        points: ["Each module (fan, LPC, HPC, combustor, HPT, LPT, bleed) is coloured by its condition: lighter is healthier, deep indigo is worn.",
          "The HUD shows the cycle, remaining life with its 90% range, and the health score against its limit.",
          "Drag to orbit, scroll to zoom. Exploded view separates the modules; Remove nacelle shows the core."], try: { to: "/twin", label: "Open the twin" } },
      { title: "Click a module", img: "/learn/twin_module.jpg", cap: "Module detail",
        points: ["You get its sensors (T30, Ps30, phi…) with live readings and deviation from normal.",
          "The chart shows the module's condition over the engine's life.",
          "Condition is a transparent sensor-drift index, not a separately trained model; the page says so."] },
      { title: "Replay history and act", img: "/learn/twin.jpg", cap: "History replay",
        points: ["Drag the replay slider or click a detector event to jump to that cycle.",
          "The gauge's band is the 90% interval; the dotted tick is the true life (shown for evaluation only).",
          "Send to workflows fires the sensor-anomaly trigger, which starts any enabled workflow listening for it."] },
    ],
  },
  {
    id: "sim", title: "Maintenance simulator", blurb: "Streamed telemetry, uncertainty and fault injection", minutes: 3,
    slides: [
      { title: "Play an engine's history", img: "/learn/simulator.jpg", cap: "Maintenance simulator",
        points: ["The server streams one cycle at a time over a WebSocket; pause, restart, seek and change speed.",
          "Remaining life is drawn with its uncertainty band; the dashed line is the true value.",
          "The health score crosses its limit where the detector raises an alert."], try: { to: "/simulator", label: "Open the simulator" } },
      { title: "Inject a fault", img: "/learn/simulator_fault.jpg", cap: "Fault injection",
        points: ["Add a spike, drift, stuck sensor or noise on any sensor from any cycle.",
          "The models re-run on the perturbed history; the faint line shows the prediction without the fault.",
          "The live feed shows which detector caught it: spike, stuck sensor, health or a sudden life drop."] },
    ],
  },
  {
    id: "mission", title: "Mission control", blurb: "Multi-agent LangGraph orchestration with a human gate", minutes: 4,
    slides: [
      { title: "Launch a mission", img: "/learn/mission.jpg", cap: "Mission control",
        points: ["Choose a scenario card and press Launch mission. The planner (Llama) decides which agents are needed.",
          "Drawing, maintenance, safety and evidence agents run in parallel; cost follows the drawing agent.",
          "The synthesizer waits for every branch (a deferred join), then writes the brief and proposed actions."], try: { to: "/mission", label: "Open mission control" } },
      { title: "Watch it live", img: "/learn/mission_live.jpg", cap: "Graph and timeline",
        points: ["Nodes pulse while running, show retries, and turn solid when done; edges animate along the active path.",
          "The timeline lists every decision, tool call, calculation and retry. Click a node to inspect its output."] },
      { title: "Approve, or not", img: "/learn/mission_gate.jpg", cap: "Human approval gate",
        points: ["The graph pauses at the gate (LangGraph interrupt). Only an approver sees the one-time token.",
          "Approve and the executor runs exactly the proposed actions; reject and the run stops there."] },
      { title: "Drill the failure modes", img: "/learn/mission_faults.jpg", cap: "Resilience drill",
        points: ["The resilience scenario makes the safety agent time out twice and evidence fail for good.",
          "Retries with backoff recover the first; the second is dropped, the brief says what is missing and lowers its confidence.",
          "Replay re-runs any mission in the sandbox with the same inputs and faults."] },
    ],
  },
  {
    id: "builder", title: "Workflow builder", blurb: "Design, run, test, replay and compare automations", minutes: 6,
    slides: [
      { title: "Start from a template", img: "/learn/builder.jpg", cap: "Workflow builder",
        points: ["Load a template: ECO release, inspection work order, safety triage, drawing extraction, planning notice or the resilience drill.",
          "Or drag steps from the left: triggers, agents, models, OCR, RAG, integrations, database, logic and approvals."], try: { to: "/builder", label: "Open the builder" } },
      { title: "Connect and configure", img: "/learn/builder_inspector.jpg", cap: "Step settings",
        points: ["Drag from a step's right handle to the next step. Conditions have a true (top) and false (bottom) handle.",
          "Click a step to edit it. Reference earlier outputs with {{step.field}}, for example {{rul.low}}.",
          "Set retries, what happens on final failure, simulated timeouts, and the assumed manual minutes."] },
      { title: "Security is built in", img: "/learn/builder_invalid.jpg", cap: "Live validation",
        points: ["The builder refuses to save a workflow where an ERP write or email could run without a human approval first.",
          "It also catches loops, disconnected steps, unlabelled condition branches and a second trigger.",
          "Approvals use a one-time token, a role check and optional separation of duties."] },
      { title: "Run it live", img: "/learn/builder_run.jpg", cap: "Execution overlay",
        points: ["Run live executes against the real connectors; the canvas shows each step's status and timing.",
          "The timeline lists agent decisions, tool calls, data, retries and errors; Step data shows every transformation.",
          "At an approval checkpoint the run pauses until an approver decides."] },
      { title: "Test against history", img: "/learn/builder_test.jpg", cap: "Test results",
        points: ["Run test batch replays N real or synthetic past events in a sandbox with a simulated reviewer.",
          "You get escalation accuracy against ground truth, failure rate, retries, latency and estimated time saved.",
          "Inspect any item to open its full run."] },
      { title: "Replay and compare", img: "/learn/builder_compare.jpg", cap: "Compare and replay",
        points: ["Replay and compare re-executes a run with the same definition, inputs and faults, then diffs every step.",
          "Compare any two runs, for example before and after editing a condition, to see exactly what changed."] },
    ],
  },
  {
    id: "obs", title: "Evaluation and observability", blurb: "MLflow, Langfuse/LangSmith, regression testing", minutes: 4,
    slides: [
      { title: "The health of the AI at a glance", img: "/learn/obs.jpg", cap: "Observability",
        points: ["Gate status, extraction F1, retrieval recall, citation support, tokens and cost, and agent reliability.",
          "Model and RAG quality shows per-field extraction, per-query retrieval ranks and every citation check."], try: { to: "/observability", label: "Open observability" } },
      { title: "Regression testing", img: "/learn/obs_trends.jpg", cap: "Regression history",
        points: ["Run regression suite (admin) runs all 32 gated checks, logs the run to MLflow and compares it with the last one.",
          "A metric that gets worse is flagged even when it still passes, so slow decay shows before CI fails.",
          "Each run has a reproducible report: fingerprints of code, data, models and versions, plus the command to regenerate it."] },
      { title: "Latency, tokens and agent reliability", img: "/learn/obs_agents.jpg", cap: "Agent reliability",
        points: ["Latency by pipeline stage (p50/p95) and tokens and cost per model, priced at Groq's list prices; the free tier costs $0.",
          "Every agent and step: executions, success rate, failures, retries, fallbacks and latency."] },
      { title: "Export traces", img: "/learn/obs_tracing.jpg", cap: "Tracing and MLflow",
        points: ["Add Langfuse keys and every run becomes a trace with a span per agent; or set LangSmith variables for LangGraph tracing.",
          "Browse MLflow runs with: uvx mlflow ui --backend-store-uri sqlite:///backend/data/mlflow.db"] },
    ],
  },
  {
    id: "more", title: "Other workflows", blurb: "Safety, quotes, directives, invoices, approvals", minutes: 3,
    slides: [
      { title: "Safety analytics", img: "/learn/safety.jpg", cap: "Safety analytics",
        points: ["Paste an incident narrative; the calibrated NTSB model sets a review priority or abstains when unsure.",
          "Similar past events and cause-theme trends come from 17,485 public probable-cause statements."], try: { to: "/safety", label: "Open safety" } },
      { title: "Drawing to quote and invoice matching", img: "/learn/quote.jpg", cap: "Drawing to quote",
        points: ["Quotes: the extraction cascade clouds low-confidence values; price comes with a range.",
          "Invoices: three-way match with the tolerance in force on the invoice date; every exception is explained."] },
      { title: "Approvals and the work queue", img: "/learn/approvals.jpg", cap: "Approvals",
        points: ["Drafted ERP changes, maintenance orders and emails wait here for an approver.",
          "The work queue lists every case by status, so a reviewer starts with what needs review."] },
    ],
  },
];

const KEY = "aeronexus.learn.done";
function loadDone(): Record<string, boolean> {
  try { return JSON.parse(localStorage.getItem(KEY) ?? "{}"); } catch { return {}; }
}

export function LearnPage() {
  const [cid, setCid] = useState("start");
  const [i, setI] = useState(0);
  const [done, setDone] = useState<Record<string, boolean>>(loadDone);
  const [imgOk, setImgOk] = useState(true);
  const [zoom, setZoom] = useState<string | null>(null);
  const course = COURSES.find((c) => c.id === cid)!;
  const slide = course.slides[i];
  const total = useMemo(() => COURSES.length, []);
  const finished = Object.values(done).filter(Boolean).length;

  const mark = (id: string) => setDone((d) => { const n = { ...d, [id]: true }; try { localStorage.setItem(KEY, JSON.stringify(n)); } catch { /* no storage */ } return n; });
  const next = () => {
    if (i < course.slides.length - 1) setI(i + 1);
    else { mark(course.id); const k = COURSES.findIndex((c) => c.id === cid); if (k < COURSES.length - 1) { setCid(COURSES[k + 1].id); setI(0); } }
  };
  const prev = () => { if (i > 0) setI(i - 1); };
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.tagName === "INPUT" || (e.target as HTMLElement)?.tagName === "TEXTAREA") return;
      if (e.key === "ArrowRight") next();
      if (e.key === "ArrowLeft") prev();
      if (e.key === "Escape") setZoom(null);
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  });
  useEffect(() => setImgOk(true), [cid, i]);

  return (
    <>
      <div className="page-head">
        <div>
          <p className="eyebrow">Learning portal</p>
          <h1>Learn AeroNexus</h1>
          <p className="lede">Short slide lessons for every part of the application. Use the arrow keys (<span className="kbd">←</span> <span className="kbd">→</span>)
            to move, and the “Try it” button to jump straight to the page being explained.</p>
        </div>
        <div style={{ minWidth: 220 }}>
          <div className="small muted">{finished} of {total} lessons completed</div>
          <div className="progress" style={{ marginTop: 6 }}><i style={{ width: `${(finished / total) * 100}%` }} /></div>
        </div>
      </div>
      <div className="learn">
        <nav className="course-list" aria-label="Lessons">
          {COURSES.map((c, k) => (
            <button key={c.id} className={`${cid === c.id ? "on" : ""} ${done[c.id] ? "done" : ""}`} onClick={() => { setCid(c.id); setI(0); }}>
              <span className="n">{done[c.id] ? "✓" : k + 1}</span>
              <span><b>{c.title}</b><small>{c.blurb}</small></span>
              <small>{c.minutes} min</small>
            </button>
          ))}
        </nav>
        <div>
          <article className="slide" aria-roledescription="slide" aria-label={`${course.title}, slide ${i + 1} of ${course.slides.length}`}>
            <div className="slide-body slide-anim" key={`${cid}-${i}`}>
              <div className="slide-text">
                <p className="eyebrow">{course.title} · {i + 1} / {course.slides.length}</p>
                <h2>{slide.title}</h2>
                <ul>{slide.points.map((p) => <li key={p}><span>{p}</span></li>)}</ul>
                <div className="spacer" />
                {slide.try && <div><Link className="btn primary" to={slide.try.to}><Icon name="play" size={14} />{slide.try.label}</Link></div>}
              </div>
              <div className="slide-visual">
                {slide.img && imgOk ? <img src={slide.img} alt={slide.cap ?? slide.title} onError={() => setImgOk(false)} onClick={() => setZoom(slide.img!)} />
                  : <div style={{ color: "#dedff1", textAlign: "center" }}><Icon name="learn" size={64} stroke={1.2} /><p className="small">{slide.cap}</p></div>}
                {slide.cap && <div className="cap">{slide.cap}</div>}
              </div>
            </div>
            <div className="slide-foot">
              <button className="btn sm" onClick={prev} disabled={i === 0}>← Back</button>
              <div className="dots">{course.slides.map((_, k) => <button key={k} className={k === i ? "on" : ""} onClick={() => setI(k)} aria-label={`Slide ${k + 1}`} />)}</div>
              <span className="spacer" />
              <span className="tiny muted">{done[course.id] ? "Completed" : `${course.minutes} min lesson`}</span>
              <button className="btn primary sm" onClick={next}>{i < course.slides.length - 1 ? "Next →" : "Finish lesson ✓"}</button>
            </div>
          </article>
          <p className="tiny muted" style={{ marginTop: 8 }}>Click a picture to enlarge it.</p>
        </div>
        {zoom && <div className="lightbox" role="dialog" aria-label="Enlarged screenshot" onClick={() => setZoom(null)}><img src={zoom} alt="" /></div>}
      </div>
    </>
  );
}
