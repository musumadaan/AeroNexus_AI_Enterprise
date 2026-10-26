import { lazy, Suspense, useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { gql, identity, Q, setIdentity, type Approval, type Identity, type Role } from "./api";
import { useAsync } from "./lib/useAsync";
import { BrandMark, Icon } from "./components/Icons";

const ChatPanel = lazy(() => import("./components/ChatPanel").then((m) => ({ default: m.ChatPanel })));
const OverviewPage = lazy(() => import("./pages/Overview").then((m) => ({ default: m.OverviewPage })));
const ChangeOrdersPage = lazy(() => import("./pages/ChangeOrders").then((m) => ({ default: m.ChangeOrdersPage })));
const PdmPage = lazy(() => import("./pages/Pdm").then((m) => ({ default: m.PdmPage })));
const SafetyPage = lazy(() => import("./pages/Safety").then((m) => ({ default: m.SafetyPage })));
const QuotePage = lazy(() => import("./pages/Quote").then((m) => ({ default: m.QuotePage })));
const MroPage = lazy(() => import("./pages/Mro").then((m) => ({ default: m.MroPage })));
const ApPage = lazy(() => import("./pages/Ap").then((m) => ({ default: m.ApPage })));
const SalesforcePage = lazy(() => import("./pages/Salesforce").then((m) => ({ default: m.SalesforcePage })));
const WorkQueuePage = lazy(() => import("./pages/WorkQueue").then((m) => ({ default: m.WorkQueuePage })));
const ReviewPage = lazy(() => import("./pages/Review").then((m) => ({ default: m.ReviewPage })));
const EvalsPage = lazy(() => import("./pages/Evals").then((m) => ({ default: m.EvalsPage })));
const DatasetsPage = lazy(() => import("./pages/Datasets").then((m) => ({ default: m.DatasetsPage })));
const ResearchPage = lazy(() => import("./pages/Research").then((m) => ({ default: m.ResearchPage })));
const TwinPage = lazy(() => import("./pages/Twin").then((m) => ({ default: m.TwinPage })));
const SimulatorPage = lazy(() => import("./pages/Simulator").then((m) => ({ default: m.SimulatorPage })));
const MissionPage = lazy(() => import("./pages/Mission").then((m) => ({ default: m.MissionPage })));
const BuilderPage = lazy(() => import("./pages/Builder").then((m) => ({ default: m.BuilderPage })));
const ObservabilityPage = lazy(() => import("./pages/Observability").then((m) => ({ default: m.ObservabilityPage })));
const LearnPage = lazy(() => import("./pages/Learn").then((m) => ({ default: m.LearnPage })));

const ROLES: Role[] = ["viewer", "engineer", "approver", "admin"];

function N({ to, icon, children, tag, count, end, onClick }: { to: string; icon: string; children: React.ReactNode; tag?: string; count?: number; end?: boolean; onClick?: () => void }) {
  return (
    <NavLink to={to} end={end} onClick={onClick}>
      <span className="lbl"><Icon name={icon} />{children}</span>
      {count ? <small>{count}</small> : tag ? <span className="new">{tag}</span> : null}
    </NavLink>
  );
}

function WhoAmI() {
  const [me, setMe] = useState<Identity>(identity());
  const update = (patch: Partial<Identity>) => { const next = { ...me, ...patch }; setMe(next); setIdentity(next); };
  return (
    <div className="who">
      <label>Signed in as (demo)
        <input type="text" value={me.user} placeholder="Your name" onChange={(e) => update({ user: e.target.value })} aria-label="Your name" />
      </label>
      <label>Role
        <select value={me.role} onChange={(e) => update({ role: e.target.value as Role })} aria-label="Role">
          {ROLES.map((r) => <option key={r} value={r}>{r[0].toUpperCase() + r.slice(1)}</option>)}
        </select>
      </label>
    </div>
  );
}

export function App() {
  const [chat, setChat] = useState(false);
  const loc = useLocation();
  const health = useAsync(() => gql<{ health: { llm_provider: string; circuit: string; models?: { small: string } } }>(`{ health }`)
    .then((d) => d.health));
  const pending = useAsync(() => gql<{ approvals: Approval[] }>(Q.approvals, { s: "pending" }).then((d) => d.approvals.length), []);
  useEffect(() => {
    const t = setInterval(() => void pending.reload(), 5000);
    return () => clearInterval(t);
  }, [pending.reload]);
  return (
    <div className={`shell ${chat ? "chat-open" : ""}`}>
      <nav className="rail" aria-label="Main">
        <div className="brand"><BrandMark /><div>Aero<span>Nexus</span><small>Aerospace engineering intelligence</small></div></div>
        <div className="nav">
          <N to="/" icon="overview" end>Overview</N>
          <N to="/learn" icon="learn" tag="slides">Learn</N>
          <div className="group">Workflows</div>
          <N to="/change" icon="change">Change orders</N>
          <N to="/twin" icon="twin" tag="3D">Engine digital twin</N>
          <N to="/simulator" icon="sim" tag="live">Maintenance simulator</N>
          <N to="/pdm" icon="pdm">Fleet maintenance</N>
          <N to="/safety" icon="safety">Safety analytics</N>
          <N to="/quote" icon="quote">Drawing to quote</N>
          <N to="/mro" icon="mro">Directive tracker</N>
          <N to="/ap" icon="ap">Invoice matching</N>
          <N to="/salesforce" icon="data" tag="CRM">Salesforce</N>
          <div className="group">Automation</div>
          <N to="/mission" icon="mission" tag="agents">Mission control</N>
          <N to="/builder" icon="builder" tag="new">Workflow builder</N>
          <N to="/queue" icon="queue">Work queue</N>
          <N to="/review" icon="approvals" onClick={() => void pending.reload()} count={pending.data || undefined}>Approvals</N>
          <div className="group">Assurance</div>
          <N to="/observability" icon="obs" tag="new">Observability</N>
          <N to="/evals" icon="evals">Evaluation gate</N>
          <N to="/data" icon="data">Data and rules</N>
          <N to="/research" icon="research">Research basis</N>
        </div>
        <div className="rail-foot">
          <WhoAmI />
          <div style={{ marginTop: 10 }}>
            {health.error ? <><span className="dot down" />API offline</> :
              health.data && <><span className={`dot ${health.data.circuit === "open" ? "down" : ""}`} />Model: {health.data.llm_provider}
                {health.data.models && health.data.llm_provider !== "mock" ? ` · ${health.data.models.small}` : ""}
                {health.data.circuit === "open" ? " (paused, rules only)" : ""}</>}
          </div>
        </div>
      </nav>
      <main>
        <ErrorBoundary resetKey={loc.pathname}>
        <Suspense fallback={<div className="skeleton" style={{ width: 240 }} />}>
          <Routes>
            <Route path="/" element={<OverviewPage />} />
            <Route path="/change" element={<ChangeOrdersPage />} />
            <Route path="/pdm" element={<PdmPage />} />
            <Route path="/safety" element={<SafetyPage />} />
            <Route path="/quote" element={<QuotePage />} />
            <Route path="/mro" element={<MroPage />} />
            <Route path="/ap" element={<ApPage />} />
            <Route path="/salesforce" element={<SalesforcePage />} />
            <Route path="/queue" element={<WorkQueuePage />} />
            <Route path="/review" element={<ReviewPage />} />
            <Route path="/evals" element={<EvalsPage />} />
            <Route path="/data" element={<DatasetsPage />} />
            <Route path="/research" element={<ResearchPage />} />
            <Route path="/twin" element={<TwinPage />} />
            <Route path="/simulator" element={<SimulatorPage />} />
            <Route path="/mission" element={<MissionPage />} />
            <Route path="/builder" element={<BuilderPage />} />
            <Route path="/observability" element={<ObservabilityPage />} />
            <Route path="/learn" element={<LearnPage />} />
          </Routes>
        </Suspense>
        </ErrorBoundary>
      </main>
      {chat ? <Suspense fallback={null}><ChatPanel onClose={() => setChat(false)} /></Suspense> :
        <button className="btn primary chat-toggle" onClick={() => setChat(true)}><Icon name="research" size={16} />Ask about results</button>}
    </div>
  );
}
