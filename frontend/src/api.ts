/** Typed GraphQL client. The backend schema lives in backend/aeronexus/api.py. */

export interface Token { id: string; text: string; bbox: [number, number, number, number]; page: number }
export interface Region { label: string; bbox: [number, number, number, number]; token_ids: string[]; confidence: number }
export interface SheetData { id: string; width: number; height: number; tokens: Token[]; regions: Region[]; meta: Record<string, unknown> }

export type FieldStatus = "auto_accepted" | "needs_review" | "missing";
export interface FieldResult {
  name: string; value: string | null; confidence: number; source: "regex" | "ner" | "llm" | "none";
  rule: string; token_ids: string[]; status: FieldStatus; alternatives: string[]; note: string;
}
export interface Extraction { sheet_id: string; fields: Record<string, FieldResult>; regions: Region[]; warnings: string[]; llm_calls: number }
export interface Issue { severity: "high" | "medium"; field: string; kind: string; message: string }
export interface Source { id: string; title: string; section: string; text: string; score: number; source: string }
export interface Claim { sentence: string; cited: string[]; supported: boolean; support: number; reason: string }
export interface Proposal { status: string; approval_id?: string; result?: unknown }
export interface Price { unit_price: number; low: number; high: number; coverage: number; total: number; currency: string }

export interface QuoteResult {
  sheet: SheetData; extraction: Extraction; issues: Issue[]; price: Price | null; assumptions: string[];
  catalog_match: { code: string; description: string } | null;
  rationale: { text: string; decision: string; groundedness: number; sources: Source[]; claims: Claim[] };
  proposals: Proposal[]; account: { id: string; name: string; email: string } | null;
  summary: { auto: number; review: number; missing: number; blocking: number };
}

export interface SheetSummary { id: string; title: string | null; tags: string[]; customer: string | null }
export interface Approval {
  id: string; tool: string; risk: string; reason: string; status: string; module: string;
  args: Record<string, unknown>; decidedBy: string | null; result: unknown;
}

export interface ChatResponse {
  answer: string; decision: string; groundedness: number; model: string; sources: Source[]; claims: Claim[];
  instances: Array<{ sheet_id: string; field: string; predicted: string | null; truth: string | null; cause: string;
    status: string; confidence: number; token_ids: string[]; sheet: SheetData | null }>;
  chart: { type: string; title: string; data: Array<{ name: string; value: number }> } | null;
  followups: string[];
}

export class ApiError extends Error {}

export type Role = "viewer" | "engineer" | "approver" | "admin";
export interface Identity { user: string; role: Role; tenant: string }

/** Demo identity (sent as headers; the backend maps them to a Principal — swap for OIDC in prod). */
export function identity(): Identity {
  try {
    const raw = localStorage.getItem("aeronexus.identity");
    if (raw) return JSON.parse(raw) as Identity;
  } catch { /* storage unavailable */ }
  return { user: "", role: "engineer", tenant: "demo" };
}
export function setIdentity(id: Identity) {
  try { localStorage.setItem("aeronexus.identity", JSON.stringify(id)); } catch { /* storage unavailable */ }
  window.dispatchEvent(new Event("aeronexus-identity"));
}

export async function gql<T>(query: string, variables: Record<string, unknown> = {}): Promise<T> {
  let res: Response;
  const me = identity();
  try {
    res = await fetch("/graphql", {
      method: "POST",
      headers: { "content-type": "application/json", "x-user": me.user, "x-role": me.role, "x-tenant": me.tenant },
      body: JSON.stringify({ query, variables }),
    });
  } catch {
    throw new ApiError("Can't reach the API. Start it with: uvicorn aeronexus.api:app --port 8000");
  }
  if (res.status === 429) throw new ApiError("Too many requests. Wait a few seconds and try again.");
  const body = await res.json();
  if (body.errors?.length) throw new ApiError(body.errors[0].message);
  return body.data as T;
}

function headers(): Record<string, string> {
  const me = identity();
  return { "content-type": "application/json", "x-user": me.user, "x-role": me.role, "x-tenant": me.tenant };
}

/** REST client for the twin, mission control, workflow builder and observability endpoints. */
export async function rest<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { method: init.method ?? (init.body ? "POST" : "GET"), headers: headers(),
      body: init.body !== undefined ? JSON.stringify(init.body) : undefined });
  } catch {
    throw new ApiError("Can't reach the API. Start it with: python -m uvicorn aeronexus.api:app --port 8000");
  }
  if (res.status === 429) throw new ApiError("Too many requests. Wait a few seconds and try again.");
  const ct = res.headers.get("content-type") ?? "";
  const body = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new ApiError(typeof body === "string" ? body : body.detail ?? JSON.stringify(body));
  return body as T;
}

/** WebSocket URL on the same origin (Vite proxies /ws to the API). */
export function wsUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sep = path.includes("?") ? "&" : "?";
  return `${proto}://${location.host}${path}${sep}tenant=${encodeURIComponent(identity().tenant)}`;
}

export const Q = {
  sheets: `{ sheets(limit: 60) { id title tags customer } }`,
  quote: `mutation($id: String!) { quote(sheetId: $id) }`,
  approvals: `query($s: String) { approvals(status: $s) { id tool risk reason status module args decidedBy result } }`,
  decide: `mutation($id: String!, $ok: Boolean!) { decideApproval(id: $id, approve: $ok) { id status } }`,
  chat: `mutation($q: String!) { chat(question: $q) { answer decision groundedness model sources claims instances chart followups } }`,
  report: `{ evalReport }`,
  runEvals: `mutation { runEvals }`,
  changePairs: `{ changePairs }`,
  openChange: `mutation($id: String!) { openChangeOrder(pairId: $id) }`,
  case: `query($id: String!) { case(id: $id) }`,
  cases: `query($s: String, $k: String) { cases(status: $s, kind: $k) }`,
  decideCase: `mutation($id: String!, $ok: Boolean!, $note: String) { decideCase(id: $id, approve: $ok, note: $note) }`,
  replay: `query($id: String!) { replayCase(id: $id) }`,
  fleet: `query($s: String!) { pdmFleet(subset: $s) }`,
  trend: `query($s: String!, $u: Int!) { engineTrend(subset: $s, unit: $u) }`,
  proposeWO: `mutation($s: String!, $u: Int!) { proposeWorkOrder(subset: $s, unit: $u) }`,
  safety: `{ safetyOverview }`,
  triage: `mutation($t: String!) { safetyTriage(text: $t) }`,
  datasets: `{ datasets rules }`,
  me: `{ me }`,
  mro: `{ mro }`,
  ap: `{ ap }`,
  research: `{ research }`,
  architecture: `{ architecture health }`,
  audit: `{ audit(limit: 40) outbox }`,
};
export interface SalesforceAccount {
  Id: string;
  Name: string;
  Industry?: string | null;
  Website?: string | null;
  Phone?: string | null;
}

export interface SalesforceOpportunity {
  Id: string;
  Name: string;
  AccountId: string;
  StageName: string;
  Amount?: number | null;
  CloseDate?: string | null;
  Probability?: number | null;
}

export const salesforceApi = {
  accounts(query = "", partNumber = "") {
    const params = new URLSearchParams();
    if (query) params.set("query", query);
    if (partNumber) params.set("part_number", partNumber);

    return rest<SalesforceAccount[]>(
      `/api/salesforce/accounts?${params.toString()}`
    );
  },

  opportunities(accountId: string) {
    return rest<SalesforceOpportunity[]>(
      `/api/salesforce/accounts/${encodeURIComponent(accountId)}/opportunities`
    );
  },
};