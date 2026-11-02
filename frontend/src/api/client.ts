import type {
  ActionTestResult,
  WorkflowTemplate,
  RunDetailInfo,
  RunSummary,
  UsageInfo,
  AdminUser,
  Approval,
  LlmKeyStatus,
  CollectionQuality,
  QualityProbe,
  ConfigDiff,
  ReplayResult,
  Scenario,
  ScenarioDefinition,
  ScenarioRunOut,
  ScenarioRunSummary,
  Connection,
  AgentConfig,
  AgentDetail,
  AgentReply,
  AgentSummary,
  AgentVersion,
  AgentVersionDetail,
  ApiKeyCreated,
  ApiKeyInfo,
  Collection,
  AuditList,
  InsightsSummary,
  KnowledgeGap,
  ChatResponse,
  ConversationSummary,
  DocumentDetail,
  DocumentItem,
  DocumentList,
  DocumentStatus,
  RetrievalTestResult,
  Role,
  StoredMessage,
  TicketComment,
  TicketDetail,
  TicketList,
  TicketPriority,
  TicketStatus,
  Ticket,
  TokenResponse,
  User,
  WebhookDelivery,
  WebhookEndpoint,
} from "../types";

const TOKEN_KEY = "support.token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

// Set by the auth provider: called when a request carrying a token is rejected as unauthorised
// (expired, revoked, or the account was deactivated) so the app can drop back to the login page.
let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export function notifyUnauthorized(): void {
  unauthorizedHandler?.();
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function errorDetail(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    // FastAPI validation errors arrive as a list of {msg, loc}
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: string };
      if (first.msg) return first.msg;
    }
  }
  return fallback;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  // Let the browser set the multipart boundary for FormData; JSON bodies need the header.
  if (typeof init.body === "string") {
    headers.set("Content-Type", "application/json");
  }
  const token = getToken();
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(path, { ...init, headers });
  if (response.status === 401 && token) {
    notifyUnauthorized();
  }
  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, errorDetail(body, response.statusText));
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function getHealth(): Promise<{ status: string }> {
  return request("/health");
}

export function login(email: string, password: string): Promise<TokenResponse> {
  return request("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
}

export function me(): Promise<User> {
  return request("/auth/me");
}

export function refreshSession(): Promise<TokenResponse> {
  return request("/auth/refresh", { method: "POST" });
}

export function logoutEverywhere(): Promise<void> {
  return request("/auth/logout-all", { method: "POST" });
}

export function sendMessage(content: string, conversationId: string | null): Promise<ChatResponse> {
  return request("/chat/messages", {
    method: "POST",
    body: JSON.stringify({ content, conversation_id: conversationId }),
  });
}

export function listConversations(): Promise<ConversationSummary[]> {
  return request("/conversations");
}

export function getConversationMessages(id: string): Promise<StoredMessage[]> {
  return request(`/conversations/${id}/messages`);
}

export function setFeedback(messageId: string, value: 1 | -1 | null): Promise<{ feedback: number | null }> {
  return request(`/messages/${messageId}/feedback`, { method: "PUT", body: JSON.stringify({ value }) });
}

// ---- Tickets ----

export type TicketQuery = {
  status?: TicketStatus | "";
  priority?: TicketPriority | "";
  assignee?: string; // "", "me", "unassigned" or a user id
  q?: string;
  limit?: number;
  offset?: number;
};

export function listTickets(query: TicketQuery = {}): Promise<TicketList> {
  const params = new URLSearchParams();
  if (query.status) params.set("status", query.status);
  if (query.priority) params.set("priority", query.priority);
  if (query.assignee) params.set("assignee", query.assignee);
  if (query.q) params.set("q", query.q);
  params.set("limit", String(query.limit ?? 25));
  params.set("offset", String(query.offset ?? 0));
  return request(`/tickets?${params.toString()}`);
}

export function getTicket(id: string): Promise<TicketDetail> {
  return request(`/tickets/${id}`);
}

export function createTicket(subject: string, body: string, priority: TicketPriority): Promise<Ticket> {
  return request("/tickets", { method: "POST", body: JSON.stringify({ subject, body, priority }) });
}

export function patchTicket(
  id: string,
  changes: { status?: TicketStatus; priority?: TicketPriority; assignee_id?: string | null },
): Promise<Ticket> {
  return request(`/tickets/${id}`, { method: "PATCH", body: JSON.stringify(changes) });
}

export function addTicketComment(id: string, body: string): Promise<TicketComment> {
  return request(`/tickets/${id}/comments`, { method: "POST", body: JSON.stringify({ body }) });
}

export function bulkTicketStatus(ids: string[], status: TicketStatus): Promise<{ updated: number }> {
  return request("/tickets/bulk-status", { method: "POST", body: JSON.stringify({ ids, status }) });
}

// ---- Users (admin) ----

export function listUsers(): Promise<AdminUser[]> {
  return request("/admin/users");
}

export function createUser(email: string, password: string, role: Role): Promise<AdminUser> {
  return request("/admin/users", { method: "POST", body: JSON.stringify({ email, password, role }) });
}

export function patchUser(
  id: string,
  changes: { role?: Role; is_active?: boolean; password?: string },
): Promise<AdminUser> {
  return request(`/admin/users/${id}`, { method: "PATCH", body: JSON.stringify(changes) });
}

// ---- Knowledge base (admin) ----

export type DocumentQuery = {
  q?: string;
  collectionId?: string;
  status?: DocumentStatus | "";
  limit?: number;
  offset?: number;
};

export function listDocuments(query: DocumentQuery = {}): Promise<DocumentList> {
  const params = new URLSearchParams();
  if (query.q) params.set("q", query.q);
  if (query.status) params.set("status", query.status);
  if (query.collectionId) params.set("collection_id", query.collectionId);
  params.set("limit", String(query.limit ?? 50));
  params.set("offset", String(query.offset ?? 0));
  return request(`/admin/documents?${params.toString()}`);
}

export function getDocument(id: string): Promise<DocumentDetail> {
  return request(`/admin/documents/${id}`);
}

export function uploadDocument(file: File, title?: string, collectionId?: string): Promise<DocumentItem> {
  const form = new FormData();
  form.append("file", file);
  if (title) form.append("title", title);
  if (collectionId) form.append("collection_id", collectionId);
  return request("/admin/documents/upload", { method: "POST", body: form });
}

export function createDocument(title: string, content: string, collectionId?: string): Promise<DocumentItem> {
  return request("/admin/documents", {
    method: "POST",
    body: JSON.stringify({ title, content, source: "paste", collection_id: collectionId ?? null }),
  });
}

export function patchDocument(
  id: string,
  changes: { title?: string; content?: string; collection_id?: string | null },
): Promise<DocumentItem> {
  return request(`/admin/documents/${id}`, { method: "PATCH", body: JSON.stringify(changes) });
}

export function deleteDocument(id: string): Promise<void> {
  return request(`/admin/documents/${id}`, { method: "DELETE" });
}

export function reindexDocument(id: string): Promise<DocumentItem> {
  return request(`/admin/documents/${id}/reindex`, { method: "POST" });
}

export function testRetrieval(query: string, agentId?: string, rerank?: boolean): Promise<RetrievalTestResult> {
  return request("/admin/retrieval/test", {
    method: "POST",
    body: JSON.stringify({ query, agent_id: agentId ?? null, rerank: rerank ?? null }),
  });
}

// ---- Insights & audit (admin) ----

export function getInsightsSummary(days: number, agentId?: string): Promise<InsightsSummary> {
  const agent = agentId ? `&agent_id=${agentId}` : "";
  return request(`/admin/insights/summary?days=${days}${agent}`);
}

export function getKnowledgeGaps(days: number, agentId?: string): Promise<KnowledgeGap[]> {
  const agent = agentId ? `&agent_id=${agentId}` : "";
  return request(`/admin/insights/gaps?days=${days}${agent}`);
}

export function getAuditLog(params: { action?: string; limit?: number; offset?: number } = {}): Promise<AuditList> {
  const query = new URLSearchParams();
  if (params.action) query.set("action", params.action);
  query.set("limit", String(params.limit ?? 25));
  query.set("offset", String(params.offset ?? 0));
  return request(`/admin/audit?${query.toString()}`);
}

// ---- Collections ----

export function listCollections(): Promise<Collection[]> {
  return request("/admin/collections");
}

export function createCollection(name: string, description: string): Promise<Collection> {
  return request("/admin/collections", { method: "POST", body: JSON.stringify({ name, description }) });
}

export function patchCollection(id: string, changes: { name?: string; description?: string }): Promise<Collection> {
  return request(`/admin/collections/${id}`, { method: "PATCH", body: JSON.stringify(changes) });
}

export function deleteCollection(id: string): Promise<void> {
  return request(`/admin/collections/${id}`, { method: "DELETE" });
}

// ---- Agents ----

export function listAgents(): Promise<AgentSummary[]> {
  return request("/admin/agents");
}

export function createAgent(name: string, description: string, template: "support" | "blank"): Promise<AgentDetail> {
  return request("/admin/agents", { method: "POST", body: JSON.stringify({ name, description, template }) });
}

export function getAgent(id: string): Promise<AgentDetail> {
  return request(`/admin/agents/${id}`);
}

export function patchAgent(id: string, changes: { name?: string; description?: string }): Promise<AgentDetail> {
  return request(`/admin/agents/${id}`, { method: "PATCH", body: JSON.stringify(changes) });
}

export function deleteAgent(id: string): Promise<void> {
  return request(`/admin/agents/${id}`, { method: "DELETE" });
}

export function saveDraft(id: string, config: AgentConfig): Promise<AgentDetail> {
  return request(`/admin/agents/${id}/draft`, { method: "PUT", body: JSON.stringify(config) });
}

export function publishAgent(
  id: string,
  note: string,
  options: { skipTests?: boolean; skipReason?: string } = {},
): Promise<AgentVersion> {
  return request(`/admin/agents/${id}/publish`, {
    method: "POST",
    body: JSON.stringify({ note, skip_tests: options.skipTests ?? false, skip_reason: options.skipReason ?? "" }),
  });
}

export function listVersions(id: string): Promise<AgentVersion[]> {
  return request(`/admin/agents/${id}/versions`);
}

export function getVersion(id: string, version: number): Promise<AgentVersionDetail> {
  return request(`/admin/agents/${id}/versions/${version}`);
}

export function makeVersionLive(id: string, version: number): Promise<AgentVersion> {
  return request(`/admin/agents/${id}/versions/${version}/make-live`, { method: "POST" });
}

export function listApiKeys(id: string): Promise<ApiKeyInfo[]> {
  return request(`/admin/agents/${id}/keys`);
}

export function createApiKey(id: string, name: string): Promise<ApiKeyCreated> {
  return request(`/admin/agents/${id}/keys`, { method: "POST", body: JSON.stringify({ name }) });
}

export function setApiKeyAllowedIps(id: string, keyId: string, allowedIps: string[]): Promise<ApiKeyInfo> {
  return request(`/admin/agents/${id}/keys/${keyId}/allowed-ips`, {
    method: "PUT",
    body: JSON.stringify({ allowed_ips: allowedIps }),
  });
}

export function revokeApiKey(id: string, keyId: string): Promise<ApiKeyInfo> {
  return request(`/admin/agents/${id}/keys/${keyId}`, { method: "DELETE" });
}

export function playgroundMessage(
  id: string,
  message: string,
  sessionId: string | null,
  version: "draft" | "published",
  options: { mockActions?: boolean; attributes?: Record<string, unknown>; workflow?: string } = {},
): Promise<AgentReply> {
  return request(`/admin/agents/${id}/playground/messages`, {
    method: "POST",
    body: JSON.stringify({
      message,
      session_id: sessionId,
      version,
      mock_actions: options.mockActions ?? false,
      end_user_attributes: options.attributes ?? {},
      workflow: options.workflow ?? null,
    }),
  });
}

export function playgroundConfirm(
  id: string,
  sessionId: string,
  confirmationId: string,
  decision: "confirm" | "cancel",
  version: "draft" | "published",
  mockActions: boolean,
): Promise<AgentReply> {
  return request(`/admin/agents/${id}/playground/confirmations`, {
    method: "POST",
    body: JSON.stringify({
      session_id: sessionId,
      confirmation_id: confirmationId,
      decision,
      version,
      mock_actions: mockActions,
    }),
  });
}

export function testAction(
  agentId: string,
  actionName: string,
  body: { arguments: Record<string, unknown>; context: Record<string, unknown>; mock: boolean; send_for_real: boolean },
): Promise<ActionTestResult> {
  return request(`/admin/agents/${agentId}/actions/${encodeURIComponent(actionName)}/test`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ---- Connections ----

export type ConnectionInput = {
  name: string;
  base_url: string;
  auth_type: Connection["auth_type"];
  auth_header_name?: string | null;
  secret?: string | null;
};

export function listConnections(): Promise<Connection[]> {
  return request("/admin/connections");
}

export function createConnection(input: ConnectionInput): Promise<Connection> {
  return request("/admin/connections", { method: "POST", body: JSON.stringify(input) });
}

export function patchConnection(id: string, changes: Partial<ConnectionInput>): Promise<Connection> {
  return request(`/admin/connections/${id}`, { method: "PATCH", body: JSON.stringify(changes) });
}

export function deleteConnection(id: string): Promise<void> {
  return request(`/admin/connections/${id}`, { method: "DELETE" });
}

// ---- Runs & usage (admin) ----

export type RunQuery = {
  agentId?: string;
  status?: string;
  source?: string;
  endUser?: string;
  limit?: number;
  offset?: number;
};

export function listRuns(query: RunQuery = {}): Promise<{ items: RunSummary[]; total: number }> {
  const params = new URLSearchParams();
  if (query.agentId) params.set("agent_id", query.agentId);
  if (query.status) params.set("status", query.status);
  if (query.source) params.set("source", query.source);
  if (query.endUser) params.set("end_user", query.endUser);
  params.set("limit", String(query.limit ?? 25));
  params.set("offset", String(query.offset ?? 0));
  return request(`/admin/runs?${params.toString()}`);
}

export function getRun(id: string): Promise<RunDetailInfo> {
  return request(`/admin/runs/${id}`);
}

export function getUsage(days: number): Promise<UsageInfo> {
  return request(`/admin/usage?days=${days}`);
}

// ---- Workflow templates ----

export function listWorkflowTemplates(agentId: string): Promise<WorkflowTemplate[]> {
  return request(`/admin/agents/${agentId}/workflow-templates`);
}

// ---- Webhooks ----

export type WebhookInput = { url: string; events: string[]; agent_id: string | null; description: string };

export function listWebhookEvents(): Promise<string[]> {
  return request("/admin/webhooks/events");
}

export function listWebhooks(): Promise<WebhookEndpoint[]> {
  return request("/admin/webhooks");
}

export function createWebhook(input: WebhookInput): Promise<WebhookEndpoint> {
  return request("/admin/webhooks", { method: "POST", body: JSON.stringify(input) });
}

export function patchWebhook(
  id: string,
  changes: Partial<Omit<WebhookInput, "agent_id">> & { active?: boolean; rotate_secret?: boolean },
): Promise<WebhookEndpoint> {
  return request(`/admin/webhooks/${id}`, { method: "PATCH", body: JSON.stringify(changes) });
}

export function deleteWebhook(id: string): Promise<void> {
  return request(`/admin/webhooks/${id}`, { method: "DELETE" });
}

export function testWebhook(id: string): Promise<WebhookDelivery> {
  return request(`/admin/webhooks/${id}/test`, { method: "POST" });
}

export function listWebhookDeliveries(id: string): Promise<WebhookDelivery[]> {
  return request(`/admin/webhooks/${id}/deliveries`);
}

export function replayWebhookDelivery(id: string, deliveryId: string): Promise<WebhookDelivery> {
  return request(`/admin/webhooks/${id}/deliveries/${deliveryId}/replay`, { method: "POST" });
}

// ---- Approvals ----

export function listApprovals(status?: Approval["status"]): Promise<{ items: Approval[]; total: number }> {
  const params = new URLSearchParams({ limit: "100" });
  if (status) params.set("status", status);
  return request(`/admin/approvals?${params.toString()}`);
}

export function decideApproval(
  id: string,
  decision: "approve" | "deny",
  note: string,
): Promise<{ status: string; outcome: string; reply: string }> {
  return request(`/admin/approvals/${id}/decision`, { method: "POST", body: JSON.stringify({ decision, note }) });
}

// ---- Tests (scenarios), comparing versions, replay ----

export function listScenarios(agentId: string): Promise<Scenario[]> {
  return request(`/admin/agents/${agentId}/scenarios`);
}

export function createScenario(
  agentId: string,
  input: { name: string; enabled: boolean; definition: ScenarioDefinition },
): Promise<Scenario> {
  return request(`/admin/agents/${agentId}/scenarios`, { method: "POST", body: JSON.stringify(input) });
}

export function updateScenario(
  agentId: string,
  id: string,
  changes: { name?: string; enabled?: boolean; definition?: ScenarioDefinition },
): Promise<Scenario> {
  return request(`/admin/agents/${agentId}/scenarios/${id}`, { method: "PUT", body: JSON.stringify(changes) });
}

export function deleteScenario(agentId: string, id: string): Promise<void> {
  return request(`/admin/agents/${agentId}/scenarios/${id}`, { method: "DELETE" });
}

export function runScenarios(agentId: string, target: string, scenarioIds?: string[]): Promise<ScenarioRunOut> {
  return request(`/admin/agents/${agentId}/scenarios/run`, {
    method: "POST",
    body: JSON.stringify({ target, scenario_ids: scenarioIds ?? null }),
  });
}

export function recordScenario(agentId: string, sessionId: string, name: string): Promise<Scenario> {
  return request(`/admin/agents/${agentId}/scenarios/record`, {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId, name }),
  });
}

export function listScenarioRuns(agentId: string, limit = 10): Promise<ScenarioRunSummary[]> {
  return request(`/admin/agents/${agentId}/scenario-runs?limit=${limit}`);
}

export function getScenarioRun(agentId: string, id: string): Promise<ScenarioRunOut> {
  return request(`/admin/agents/${agentId}/scenario-runs/${id}`);
}

export function getConfigDiff(agentId: string, from: string, to: string): Promise<ConfigDiff> {
  return request(`/admin/agents/${agentId}/diff?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`);
}

export function replayRun(runId: string, target: string): Promise<ReplayResult> {
  return request(`/admin/runs/${runId}/replay`, { method: "POST", body: JSON.stringify({ target }) });
}

// ---- Knowledge quality ----

export function getCollectionQuality(collectionId: string): Promise<CollectionQuality> {
  return request(`/admin/collections/${collectionId}/quality`);
}

export function runCollectionQuality(
  collectionId: string,
  options: { k?: number; rerank?: boolean } = {},
): Promise<CollectionQuality> {
  return request(`/admin/collections/${collectionId}/quality`, {
    method: "POST",
    body: JSON.stringify({ k: options.k ?? 5, rerank: options.rerank ?? false }),
  });
}

export function listProbes(collectionId: string): Promise<QualityProbe[]> {
  return request(`/admin/collections/${collectionId}/probes`);
}

export function generateProbes(collectionId: string, perDocument: number): Promise<{ documents: number; created: number }> {
  return request(`/admin/collections/${collectionId}/probes/generate`, {
    method: "POST",
    body: JSON.stringify({ per_document: perDocument }),
  });
}

export function addProbe(collectionId: string, documentId: string, question: string): Promise<QualityProbe> {
  return request(`/admin/collections/${collectionId}/probes`, {
    method: "POST",
    body: JSON.stringify({ document_id: documentId, question }),
  });
}

export function deleteProbe(id: string): Promise<void> {
  return request(`/admin/probes/${id}`, { method: "DELETE" });
}

export function reindexCollection(collectionId: string): Promise<{ documents: number }> {
  return request(`/admin/collections/${collectionId}/reindex`, { method: "POST" });
}

// ---- Workspace settings ----

export function getLlmKey(): Promise<LlmKeyStatus> {
  return request("/admin/settings/llm-key");
}

export function setLlmKey(apiKey: string): Promise<LlmKeyStatus> {
  return request("/admin/settings/llm-key", { method: "PUT", body: JSON.stringify({ api_key: apiKey }) });
}

export function clearLlmKey(): Promise<LlmKeyStatus> {
  return request("/admin/settings/llm-key", { method: "DELETE" });
}
