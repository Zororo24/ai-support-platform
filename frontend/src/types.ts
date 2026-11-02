export type HealthResponse = {
  status: string;
};

export type Role = "customer" | "admin";

export type User = {
  id: string;
  email: string;
  role: Role;
  tenant_id: string;
  tenant_slug: string;
  tenant_name: string;
};

export type TokenResponse = {
  access_token: string;
  token_type: string;
  user: User;
};

export type Citation = {
  document_id: string;
  title: string;
  snippet: string;
};

export type TicketStatus = "open" | "pending" | "closed";
export type TicketPriority = "low" | "normal" | "high";

export type Ticket = {
  id: string;
  subject: string;
  body: string;
  status: TicketStatus;
  priority: TicketPriority;
  requester_email: string | null;
  assignee_id: string | null;
  assignee_email: string | null;
  conversation_id: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type TicketComment = {
  id: string;
  author_email: string;
  author_role: Role;
  body: string;
  created_at: string;
};

export type TicketDetail = Ticket & { comments: TicketComment[] };

export type TicketList = { items: Ticket[]; total: number };

export type AdminUser = {
  id: string;
  email: string;
  role: Role;
  is_active: boolean;
  created_at: string;
};

export type ConversationSummary = {
  id: string;
  title: string;
  updated_at: string;
};

export type StoredMessage = {
  id: string;
  role: "user" | "assistant" | "tool";
  content: string;
  citations: Citation[] | null;
  ticket: Ticket | null;
  order: Order | null;
  feedback: number | null;
  meta: ReplyMeta | null;
  created_at: string;
};

export type Order = {
  id: string;
  order_number: string;
  status: string;
  total_cents: number;
};

export type ReplyMeta = {
  question?: string;
  intent?: string | null;
  confidence?: number | null;
  outcome?: string;
  cache_hit?: boolean;
  latency_ms?: number;
  llm_calls?: number;
  total_tokens?: number;
  retrieved?: { document_id: string; title: string; score: number }[];
  cited?: string[];
};

export type ChatResponse = {
  conversation_id: string;
  message_id: string | null;
  content: string;
  citations: Citation[];
  ticket: Ticket | null;
  order: Order | null;
  intent: string | null;
  cache_hit: boolean;
  meta: ReplyMeta;
};

export type ChatMessage = {
  id: string; // server id for assistant replies; used for feedback
  feedback?: number | null;
  meta?: ReplyMeta | null;
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  ticket?: Ticket | null;
  order?: Order | null;
};

export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

export type DocumentItem = {
  id: string;
  title: string;
  source: string;
  status: DocumentStatus;
  error: string | null;
  file_name: string | null;
  mime_type: string | null;
  size_bytes: number;
  chunk_count: number;
  collection_id: string | null;
  collection_name: string | null;
  created_by_email: string | null;
  created_at: string;
  updated_at: string;
};

export type DocumentChunk = {
  index: number;
  content: string;
};

export type DocumentDetail = DocumentItem & {
  content: string;
  chunks: DocumentChunk[];
};

export type DocumentList = {
  items: DocumentItem[];
  total: number;
};

export type RetrievalHit = {
  document_id: string;
  title: string;
  content: string;
  score: number;
  dense_rank: number | null;
  dense_score: number | null;
  sparse_rank: number | null;
  sparse_score: number | null;
  section: string | null;
  rerank_score: number | null;
};

export type RetrievalTestResult = {
  query: string;
  cache_hit: boolean;
  reranked: boolean;
  hits: RetrievalHit[];
};

export type InsightsSummary = {
  days: number;
  conversations: number;
  replies: number;
  outcomes: Record<string, number>;
  answered_rate: number | null;
  tickets_from_chat: number;
  thumbs_up: number;
  thumbs_down: number;
  helpful_rate: number | null;
  avg_latency_ms: number | null;
  total_tokens: number;
  series: { date: string; conversations: number; messages: number }[];
  top_gaps: KnowledgeGap[];
};

export type KnowledgeGap = {
  question: string;
  count: number;
  refused: number;
  thumbs_down: number;
  last_seen: string;
};

export type AuditEntry = {
  id: string;
  actor_email: string;
  action: string;
  target_type: string;
  target_id: string | null;
  detail: Record<string, unknown> | null;
  created_at: string;
};

export type AuditList = { items: AuditEntry[]; total: number };

// ---- Agents (Pass 3) ----

export type ParamDef = {
  type: "string" | "integer" | "number" | "boolean";
  description: string;
  required: boolean;
  enum: (string | number)[] | null;
  pattern: string | null;
  min_length: number | null;
  max_length: number | null;
  minimum: number | null;
  maximum: number | null;
};

export type HandoffConfig = { action: string; args: Record<string, string> };

export type ActionDef = {
  name: string;
  description: string;
  // hosted: we call your API through the connection. handback: your own backend performs it.
  execution: "hosted" | "handback";
  connection_id: string | null;
  request: {
    method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
    path: string;
    query: Record<string, string>;
    body: unknown | null;
    headers: Record<string, string>;
  } | null;
  parameters: Record<string, ParamDef>;
  effect: "read" | "write";
  confirmation: "none" | "user" | "staff" | null;
  confirm_summary: string | null;
  guards: { when: string; then: "refuse"; message: string }[];
  limits: { timeout_s: number; max_calls_per_run: number; max_response_bytes: number; retries: number };
  response: { expose: string[]; max_chars: number };
  idempotency_header: string | null;
  mock: unknown | null;
  enabled: boolean;
};

export type WfField = {
  name: string;
  type: "string" | "integer" | "number" | "boolean";
  ask: string;
  invalid_message: string;
  pattern: string | null;
  enum: string[] | null;
  transform: "upper" | "lower" | null;
  max_length: number;
  minimum: number | null;
  maximum: number | null;
};

export type WfNode =
  | { type: "collect"; fields: WfField[]; next: string }
  | {
      type: "action";
      action: string;
      args: Record<string, string>;
      save_as: string | null;
      on_error: string | null;
      confirmed_by: string | null;
      next: string | null;
    }
  | { type: "branch"; cases: { when: string; next: string }[]; otherwise: string | null }
  | { type: "llm"; prompt: string; save_as: string; fields: string[]; next: string | null }
  | { type: "knowledge"; question: string; save_as: string | null; say: boolean; next: string | null }
  | { type: "message"; text: string; next: string | null }
  | { type: "confirm"; summary: string; yes: string; no: string }
  | { type: "handoff"; reason: string; summary: string }
  | { type: "end"; status: "completed" | "failed"; message: string | null };

export type WorkflowDef = {
  name: string;
  description: string;
  start: string;
  nodes: Record<string, WfNode>;
  enabled: boolean;
};

export type WorkflowTemplate = {
  id: string;
  name: string;
  description: string;
  ready: boolean;
  missing: string[];
  workflow: WorkflowDef;
};

export type AgentConfig = {
  instructions: string;
  language: string;
  knowledge: {
    collection_ids: string[];
    top_k: number;
    min_similarity: number;
    not_found_message: string;
    rewrite_queries: boolean;
    rerank: boolean;
  };
  actions: ActionDef[];
  expose_attributes: string[];
  workflows: WorkflowDef[];
  handoff: HandoffConfig | null;
  limits: {
    max_steps_per_turn: number;
    run_ttl_hours: number;
    daily_token_budget: number | null;
    requests_per_minute_per_key: number;
    requests_per_minute_per_end_user: number;
    max_concurrent_requests: number;
  };
  privacy: { store_transcript: "full" | "none"; retention_days: number; redact_pii: boolean };
};

export type AgentSummary = {
  id: string;
  name: string;
  slug: string;
  description: string;
  published_version: number | null;
  has_unpublished_changes: boolean;
  created_at: string;
  updated_at: string;
};

export type AgentDetail = AgentSummary & { draft_config: AgentConfig };

export type AgentVersion = {
  version: number;
  note: string;
  created_at: string;
  created_by_email: string | null;
  is_live: boolean;
  // What the publish gate found when this version went live.
  tests: { passed: number; failed: number; skipped: boolean; override_reason: string | null } | null;
};

export type AgentVersionDetail = AgentVersion & { config: AgentConfig };

export type ApiKeyInfo = {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  allowed_ips: string[];
};

export type ApiKeyCreated = ApiKeyInfo & { key: string };

export type Collection = {
  id: string;
  name: string;
  description: string;
  is_default: boolean;
  document_count: number;
};

export type PendingConfirmation = {
  id: string;
  action: string;
  summary: string;
  args_preview: Record<string, unknown>;
  expires_at: string;
};

export type ActionTaken = { name: string; effect: "read" | "write"; ok: boolean; error?: string };

export type AgentReply = {
  run_id: string | null;
  pending_confirmation: PendingConfirmation | null;
  actions_taken: ActionTaken[];
  pending_approval: { id: string; action: string; summary: string; expires_at: string } | null;
  action_request: { call_id: string; name: string; arguments: Record<string, unknown>; expires_at: string } | null;
  handoff: { reason: string; summary: string; action_result?: Record<string, unknown> | null } | null;
  session_id: string;
  message_id: string | null;
  status: string;
  outcome: string;
  reply: string;
  citations: Citation[];
  usage: { tokens: number; latency_ms: number };
};

export type Connection = {
  id: string;
  name: string;
  base_url: string;
  auth_type: "none" | "bearer" | "header" | "basic";
  auth_header_name: string | null;
  has_secret: boolean;
  secret_hint: string | null;
  used_by: string[];
  created_at: string;
};

export type ActionTestResult = {
  refused: { code: string; message: string } | null;
  request: {
    method: string;
    url: string;
    query: Record<string, unknown>;
    headers: Record<string, string>;
    body: unknown;
  } | null;
  result: {
    ok: boolean;
    status_code: number | null;
    latency_ms: number;
    error_code: string | null;
    error: string | null;
    mock: boolean;
  } | null;
  model_view: Record<string, unknown> | null;
};

export type RunSummary = {
  id: string;
  agent_id: string;
  agent_name: string;
  agent_version: number | null;
  session_id: string;
  end_user_ref: string;
  source: "api" | "playground" | "test" | "dashboard";
  status: string;
  outcome: string | null;
  input_preview: string | null;
  reply_preview: string | null;
  tokens: number;
  actions: number;
  error: string | null;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
};

export type RunStepInfo = {
  seq: number;
  type: string;
  name: string;
  status: string;
  latency_ms: number;
  tokens: number;
  error: string | null;
  input: unknown;
  output: unknown;
  created_at: string;
};

export type RunDetailInfo = RunSummary & {
  pending: { id: string; action: string; summary: string; args_preview: Record<string, unknown>; expires_at: string } | null;
  steps: RunStepInfo[];
};

export type UsageInfo = {
  days: number;
  totals: { runs: number; tokens: number; action_calls: number; failed: number };
  series: { date: string; runs: number; tokens: number; action_calls: number }[];
  by_agent: {
    agent_id: string;
    name: string;
    runs: number;
    tokens: number;
    action_calls: number;
    failed: number;
    tokens_today: number;
    daily_budget: number | null;
  }[];
};

export type WebhookEndpoint = {
  id: string;
  url: string;
  description: string;
  agent_id: string | null;
  events: string[];
  active: boolean;
  secret_hint: string;
  created_at: string;
  secret?: string | null; // only in the response that created or rotated it
};

export type WebhookDelivery = {
  id: string;
  event_id: string;
  event_type: string;
  status: "queued" | "delivered" | "failed";
  attempts: number;
  response_code: number | null;
  last_error: string | null;
  created_at: string;
  delivered_at: string | null;
  payload: Record<string, unknown>;
};

export type Approval = {
  id: string;
  agent_id: string;
  agent_name: string;
  run_id: string;
  end_user_ref: string;
  session_id: string;
  action: string;
  summary: string;
  args: Record<string, unknown>;
  status: "pending" | "approved" | "denied" | "expired";
  requested_at: string;
  expires_at: string;
  decided_by: string | null;
  decided_at: string | null;
  note: string;
};

export type ScenarioExpect = {
  status?: string | null;
  outcome?: string | null;
  reply_contains: string[];
  reply_not_contains: string[];
  cites?: boolean | null;
  actions_called: { name: string; arguments: Record<string, unknown> }[];
  actions_not_called: string[];
  workflow_started?: string | null;
};

export type ScenarioStep = {
  say: string | null;
  confirm: "yes" | "no" | null;
  expect: ScenarioExpect;
};

export type ScenarioDefinition = {
  description: string;
  end_user: { id: string; attributes: Record<string, unknown> };
  workflow: string | null;
  mocks: Record<string, unknown>;
  steps: ScenarioStep[];
};

export type Scenario = {
  id: string;
  name: string;
  enabled: boolean;
  definition: ScenarioDefinition;
  updated_at: string;
  last_result: { passed: boolean; target: string; at: string } | null;
};

export type ScenarioStepResult = {
  index: number;
  say: string | null;
  confirm: "yes" | "no" | null;
  failures: string[];
  error: string | null;
  status?: string;
  outcome?: string;
  reply?: string;
  actions_called?: { name: string; arguments: Record<string, unknown>; ok: boolean }[];
  run_id?: string | null;
};

export type ScenarioResult = {
  scenario_id: string;
  name: string;
  passed: boolean;
  error: string | null;
  steps: ScenarioStepResult[];
  duration_ms: number;
};

export type ScenarioRunSummary = {
  id: string;
  target: string;
  purpose: "manual" | "publish_gate";
  passed: number;
  failed: number;
  created_at: string;
};

export type ScenarioRunOut = ScenarioRunSummary & { results: ScenarioResult[] };

export type ConfigDiff = {
  from: string;
  to: string;
  identical: boolean;
  sections: {
    title: string;
    changes: { label: string; kind: "added" | "removed" | "changed"; before: unknown; after: unknown }[];
  }[];
};

export type ReplayTurn = {
  say: string;
  original: { status: string | null; reply: string | null; actions: string[] };
  replayed: { status: string; reply: string; actions: string[] } | null;
  error: string | null;
  differs: boolean;
};

export type ReplayResult = {
  run_id: string;
  target: string;
  agent_version: number | null;
  truncated: boolean;
  differs: boolean;
  turns: ReplayTurn[];
};

export type QualityProbe = {
  id: string;
  document_id: string;
  document_title: string;
  question: string;
  source: "generated" | "manual";
};

export type QualityReportSummary = {
  id: string;
  k: number;
  rerank: boolean;
  probe_count: number;
  doc_recall: number;
  chunk_recall: number;
  mrr: number;
  created_at: string;
};

export type QualityReport = QualityReportSummary & {
  per_document: { document_id: string; title: string; probes: number; hits: number; recall: number }[];
  misses: { question: string; expected: string; got: string[] }[];
  issues: { document_id: string; title: string; kind: string; detail: string }[];
};

export type CollectionQuality = {
  latest: QualityReport | null;
  previous: QualityReportSummary | null;
  probe_count: number;
  document_count: number;
};

export type LlmKeyStatus = { configured: boolean; hint: string | null };
