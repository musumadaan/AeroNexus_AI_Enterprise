import { useEffect, useRef, useState } from "react";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { gql, Q, type ChatResponse } from "../api";
import { label } from "../lib/cloud";
import { Grounded } from "./Grounded";
import { SheetView } from "./SheetView";

type Turn = { role: "user"; text: string } | { role: "bot"; res: ChatResponse } | { role: "error"; text: string };

const STARTERS = ["How good is the extraction overall?", "Why does thickness fail?", "Can I trust auto-accepted values?",
  "Is the LLM judge reliable enough?", "What is Cohen's kappa?"];

export function ChatPanel({ onClose }: { onClose: () => void }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const log = useRef<HTMLDivElement>(null);
  useEffect(() => { log.current?.scrollTo({ top: log.current.scrollHeight }); }, [turns]);

  const ask = async (question: string) => {
    if (!question.trim() || busy) return;
    setTurns((t) => [...t, { role: "user", text: question }]);
    setQ("");
    setBusy(true);
    try {
      const { chat } = await gql<{ chat: ChatResponse }>(Q.chat, { q: question });
      setTurns((t) => [...t, { role: "bot", res: chat }]);
    } catch (e) {
      setTurns((t) => [...t, { role: "error", text: e instanceof Error ? e.message : String(e) }]);
    } finally {
      setBusy(false);
    }
  };
  const last = [...turns].reverse().find((t) => t.role === "bot") as { res: ChatResponse } | undefined;

  return (
    <aside className="chat" aria-label="Ask about results">
      <div className="chat-head">
        <div>
          <h3 style={{ margin: 0 }}>Ask about results</h3>
          <div className="small muted">Answers cite the measured results. Claims that can't be checked are dropped.</div>
        </div>
        <button className="btn ghost" onClick={onClose} aria-label="Close chat">Close</button>
      </div>
      <div className="chat-log" ref={log}>
        {!turns.length && (
          <div>
            <p className="muted small">Ask in plain words. You'll get the number, what it means, and a real example from the test drawings.</p>
            <div className="suggest">{STARTERS.map((s) => <button key={s} onClick={() => ask(s)}>{s}</button>)}</div>
          </div>
        )}
        {turns.map((t, i) => t.role === "user" ? <div key={i} className="msg-user">{t.text}</div>
          : t.role === "error" ? <div key={i} className="err">{t.text}</div>
          : (
            <div key={i} className="msg-bot">
              <Grounded text={t.res.answer} sources={t.res.sources} claims={t.res.claims} groundedness={t.res.groundedness} decision={t.res.decision} />
              {t.res.chart && (
                <div style={{ height: 150, marginTop: 10 }}>
                  <div className="small muted">{t.res.chart.title}</div>
                  <ResponsiveContainer>
                    <BarChart data={t.res.chart.data} margin={{ left: -20, right: 4, top: 6 }}>
                      <XAxis dataKey="name" tick={{ fontSize: 9 }} interval={0} angle={-35} textAnchor="end" height={46} />
                      <YAxis domain={[0, 1]} tick={{ fontSize: 10 }} />
                      <Tooltip formatter={(v: number) => v.toFixed(3)} />
                      <Bar dataKey="value" fill="var(--red)" radius={[2, 2, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
              {t.res.instances.slice(0, 1).map((x) => x.sheet && (
                <div key={x.sheet_id + x.field} style={{ marginTop: 12 }}>
                  <div className="small"><b>Example: {x.sheet_id}</b> · {label(x.field)} — read “{x.predicted ?? "nothing"}”, correct is “{x.truth}”. {x.cause}.</div>
                  <SheetView sheet={x.sheet} highlightTokens={x.token_ids} caption={x.sheet_id} />
                </div>
              ))}
            </div>
          ))}
        {busy && <div className="skeleton" style={{ width: "70%" }} />}
        {last && !busy && <div className="suggest">{last.res.followups.map((s) => <button key={s} onClick={() => ask(s)}>{s}</button>)}</div>}
      </div>
      <form className="chat-form" onSubmit={(e) => { e.preventDefault(); void ask(q); }}>
        <textarea value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. Why did drawing G0015 fail?"
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void ask(q); } }}
                  aria-label="Question" />
        <button className="btn primary" disabled={busy || !q.trim()}>Ask</button>
      </form>
    </aside>
  );
}
