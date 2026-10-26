import { label } from "../lib/cloud";

export type CaseStatus = "verified" | "needs_review" | "blocked" | "approved" | "executed" | "rejected" | null;

const COPY: Record<string, string> = {
  verified: "Verified, awaiting approval", needs_review: "Needs review", blocked: "Blocked",
  approved: "Approved", executed: "Executed", rejected: "Rejected",
};

export function StatusPill({ status }: { status: CaseStatus | string | null }) {
  const s = status ?? "new";
  return <span className={`pill pill-${s}`}>{COPY[s] ?? label(s)}</span>;
}

/** The lifecycle as a row of stations; the current one is filled. Blocked/rejected branch off. */
export function Lifecycle({ status, visited = [] }: { status: string | null; visited?: string[] }) {
  const main = ["needs_review", "verified", "approved", "executed"].filter((m) => m !== "needs_review" || visited.includes(m) || status === m);
  const idx = main.indexOf(status ?? "");
  return (
    <ol className="lifecycle" aria-label="Case lifecycle">
      {main.map((m, i) => (
        <li key={m} className={i < idx ? "done" : i === idx ? "now" : ""}>{COPY[m].split(",")[0]}</li>
      ))}
      {(status === "blocked" || status === "rejected") && <li className="now off">{COPY[status]}</li>}
    </ol>
  );
}
