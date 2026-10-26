import { useEffect, useState } from "react";
import type { SheetData } from "../api";
import { label } from "../lib/cloud";
import { SheetView } from "./SheetView";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Change = any;

/** Animated comparison of two drawing revisions: side by side, swipe, or blink, with the changed values clouded. */
export function RevisionCompare({ a, b, change }: { a: SheetData; b: SheetData; change?: Change }) {
  const [mode, setMode] = useState<"side" | "swipe" | "blink">("swipe");
  const [split, setSplit] = useState(50);
  const [auto, setAuto] = useState(true);
  const [showB, setShowB] = useState(false);
  useEffect(() => {
    if (mode === "swipe" && auto) {
      let t = 0;
      const id = setInterval(() => { t += 0.035; setSplit(50 + 42 * Math.sin(t)); }, 30);
      return () => clearInterval(id);
    }
    if (mode === "blink") {
      const id = setInterval(() => setShowB((x) => !x), 1100);
      return () => clearInterval(id);
    }
  }, [mode, auto]);
  const ha = change?.before_tokens ?? [];
  const hb = change?.after_tokens ?? [];
  return (
    <div>
      <div className="row" style={{ marginBottom: 10 }}>
        <div className="seg" role="tablist" aria-label="Comparison mode">
          {(["swipe", "blink", "side"] as const).map((m) => <button key={m} className={mode === m ? "on" : ""} onClick={() => setMode(m)}>
            {m === "side" ? "Side by side" : m === "swipe" ? "Swipe" : "Blink"}</button>)}
        </div>
        {mode === "swipe" && (
          <>
            <input type="range" min={0} max={100} value={split} onChange={(e) => { setAuto(false); setSplit(Number(e.target.value)); }} aria-label="Swipe position" style={{ flex: 1 }} />
            <button className="btn sm" onClick={() => setAuto((x) => !x)}>{auto ? "Pause" : "Animate"}</button>
          </>
        )}
        {change && <span className="small muted">Highlighted: <b>{label(change.field)}</b></span>}
      </div>
      {mode === "side" ? (
        <div className="two">
          <div style={{ position: "relative" }}><SheetView sheet={a} highlightTokens={ha} /><span className="rev-badge">REV A</span></div>
          <div style={{ position: "relative" }}><SheetView sheet={b} highlightTokens={hb} /><span className="rev-badge">REV B</span></div>
        </div>
      ) : mode === "swipe" ? (
        <div className="compare-stage">
          <SheetView sheet={a} highlightTokens={ha} />
          <div className="layer-b" style={{ clipPath: `inset(0 0 0 ${split}%)` }}><SheetView sheet={b} highlightTokens={hb} /></div>
          <div className="swipe-handle" style={{ left: `${split}%` }} />
          <span className="rev-badge">REV A</span><span className="rev-badge" style={{ left: "auto", right: 10 }}>REV B</span>
        </div>
      ) : (
        <div className="compare-stage">
          <SheetView sheet={a} highlightTokens={ha} />
          <div className="layer-b" style={{ opacity: showB ? 1 : 0, transition: "opacity .45s ease" }}><SheetView sheet={b} highlightTokens={hb} /></div>
          <span className="rev-badge">{showB ? "REV B" : "REV A"}</span>
        </div>
      )}
    </div>
  );
}

/** Cross-section that morphs between the two revisions' dimension, with dimension lines. */
export function DimensionMorph({ change }: { change: Change }) {
  const [b, setB] = useState(false);
  useEffect(() => { const id = setInterval(() => setB((x) => !x), 1600); return () => clearInterval(id); }, []);
  const pa = parseFloat(String(change.before)), pb = parseFloat(String(change.after));
  if (!(pa > 0 && pb > 0)) return null;
  const isThick = change.field === "thickness";
  const max = Math.max(pa, pb);
  const scale = isThick ? 70 / max : 300 / max;
  const cur = b ? pb : pa;
  const w = isThick ? 300 : cur * scale, h = isThick ? cur * scale : 40;
  return (
    <svg viewBox="0 0 440 140" width="100%" role="img" aria-label={`${change.field} changes from ${change.before} to ${change.after}`}>
      <defs>
        <pattern id="hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="8" stroke="#8284ac" strokeWidth="1.4" />
        </pattern>
      </defs>
      <rect x={40} y={isThick ? 100 - h : 40} width={w} height={h} fill="url(#hatch)" stroke="#2f3169" strokeWidth="2"
            style={{ transition: "all .7s cubic-bezier(.6,.05,.3,1)" }} />
      {isThick ? (
        <g style={{ transition: "all .7s" }}>
          <line x1={355} y1={100} x2={355} y2={100 - h} stroke="#2f3169" strokeWidth="1.2" style={{ transition: "all .7s cubic-bezier(.6,.05,.3,1)" }} />
          <line x1={345} y1={100} x2={365} y2={100} stroke="#2f3169" /><line x1={345} y1={100 - h} x2={365} y2={100 - h} stroke="#2f3169" style={{ transition: "all .7s" }} />
          <text x={368} y={100 - h / 2 + 4} fontSize="13" fontWeight="700" fill="#2f3169" style={{ transition: "all .7s" }}>{b ? change.after : change.before}</text>
        </g>
      ) : (
        <text x={40 + w / 2} y={110} fontSize="13" fontWeight="700" fill="#2f3169" textAnchor="middle" style={{ transition: "all .7s" }}>{b ? change.after : change.before}</text>
      )}
      <text x={40} y={130} fontSize="11" fill="#53558c">Section view · {b ? "Rev B" : "Rev A"} · scale exaggerated for clarity</text>
    </svg>
  );
}
