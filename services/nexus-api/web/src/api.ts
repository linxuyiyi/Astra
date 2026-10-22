import type {
  AgentScopeOutcome,
  Attachment,
  Channel,
  ChatMessage,
  Conversation,
  Employee,
  McpHeader,
  McpResource,
  McpTestResult,
  ResourceCatalog,
  RuntimeStatus,
  SkillResource,
  SkillVersion,
} from "./types";

let resolvedUid = "";
export function currentUid(): string { return resolvedUid; }
export function storedOwnerId(): string { return resolvedUid; }
export async function resolveOwner(): Promise<string> {
  const response = await fetch("/api/v1/auth/session", { credentials: "same-origin" });
  if (!response.ok) throw new Error("请先登录");
  const user = await response.json() as { uid: string };
  resolvedUid = user.uid;
  return resolvedUid;
}
export async function signIn(mode: "login" | "register", username: string, password: string, displayName: string) {
  const response = await fetch(`/api/v1/auth/${mode}`, { method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password, display_name: displayName }) });
  const user = await read<{ uid: string; display_name: string }>(response);
  resolvedUid = user.uid;
  return user;
}
function headers(json = true): HeadersInit { return json ? { "Content-Type": "application/json" } : {}; }
export async function groupAction<T>(path: string, method = "POST", body?: unknown): Promise<T> {
  return read(await fetch(`/api/v1/channels/${path}`, { method, headers: headers(), ...(body ? { body: JSON.stringify(body) } : {}) }));
}

async function read<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) detail = payload.detail;
    } catch {
      /* 保留状态文本 */
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export async function getRuntime(): Promise<RuntimeStatus> {
  return read(await fetch("/api/v1/runtime"));
}

export async function getSkills(): Promise<ResourceCatalog> {
  return read(await fetch("/api/v1/skills"));
}

export async function getMcps(): Promise<ResourceCatalog> {
  return read(await fetch("/api/v1/mcps"));
}

export async function getKnowledge(): Promise<ResourceCatalog> {
  return read(await fetch("/api/v1/knowledge"));
}

export async function getTools(): Promise<ResourceCatalog> {
  return read(await fetch("/api/v1/tools"));
}

export async function listEmployees(query = ""): Promise<Employee[]> {
  const payload = await read<{ items: Employee[] }>(
    await fetch(`/api/v1/employees?q=${encodeURIComponent(query)}`),
  );
  return payload.items;
}

export async function saveEmployee(body: Omit<Employee, "id" | "agentscope_agent_id" | "mailbox_session_id">, id?: string) {
  const response = await fetch(id ? `/api/v1/employees/${id}` : "/api/v1/employees", {
    method: id ? "PATCH" : "POST",
    headers: headers(),
    body: JSON.stringify(body),
  });
  return read<{
    employee: Employee;
    agentscope: { ok: boolean; detail: string };
    sync?: { status: string; detail: string };
  }>(response);
}

export async function deleteEmployee(id: string): Promise<void> {
  await read(await fetch(`/api/v1/employees/${id}`, { method: "DELETE" }));
}

export async function uploadAttachment(file: File): Promise<Attachment> {
  const form = new FormData();
  form.append("file", file);
  return read(await fetch("/api/v1/attachments", {
    method: "POST",
    headers: headers(false),
    body: form,
  }));
}

export function attachmentUrl(id: string): string {
  return `/api/v1/attachments/${id}`;
}

export async function listConversations(employeeId: string): Promise<Conversation[]> {
  const payload = await read<{ items: Conversation[] }>(
    await fetch(`/api/v1/employees/${employeeId}/conversations`, { headers: headers(false) }),
  );
  return payload.items;
}

export async function createConversation(employeeId: string): Promise<Conversation> {
  return read(
    await fetch(`/api/v1/employees/${employeeId}/conversations`, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ name: "新对话" }),
    }),
  );
}

export async function getConversation(
  id: string,
): Promise<Conversation & { messages: ChatMessage[]; history_error: string; event_cursor: string }> {
  return read(await fetch(`/api/v1/conversations/${id}`, { headers: headers(false) }));
}

export async function sendConversation(id: string, text: string, attachmentIds: string[]) {
  return read<{ message: ChatMessage; agentscope: AgentScopeOutcome }>(
    await fetch(`/api/v1/conversations/${id}/messages`, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ text, attachment_ids: attachmentIds }),
    }),
  );
}

export async function listChannels(): Promise<Channel[]> {
  const payload = await read<{ items: Channel[] }>(await fetch("/api/v1/channels"));
  return payload.items;
}

export async function saveChannel(body: { name: string; description: string; member_ids: string[] }, id?: string) {
  return read<Channel>(
    await fetch(id ? `/api/v1/channels/${id}` : "/api/v1/channels", {
      method: id ? "PATCH" : "POST",
      headers: headers(),
      body: JSON.stringify(body),
    }),
  );
}

export async function channelMessages(id: string): Promise<Channel & { messages: ChatMessage[]; history_error?: string }> {
  return read(await fetch(`/api/v1/channels/${id}/messages`, { headers: headers(false) }));
}

export interface TimelineItem {
  id: string;
  speaker: string;
  text: string;
  kind: string;
  created_at: string;
  attachments?: Attachment[];
}

export async function channelTimeline(id: string): Promise<{ items: TimelineItem[] }> {
  return read(await fetch(`/api/v1/channels/${id}/timeline`, { headers: headers(false) }));
}

export async function sendChannel(id: string, text: string, attachmentIds: string[]) {
  return read<{ message: ChatMessage; triggered: Array<AgentScopeOutcome & { employee_id: string; name: string }> }>(
    await fetch(`/api/v1/channels/${id}/messages`, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ text, attachment_ids: attachmentIds }),
    }),
  );
}

export async function readEventStream(
  path: string,
  signal: AbortSignal,
  onEvent: (event: Record<string, unknown>) => void,
): Promise<void> {
  const response = await fetch(path, { headers: headers(false), signal });
  if (!response.ok || !response.body) {
    let detail = "没有收到事件流";
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) detail = payload.detail;
    } catch {
      /* 使用默认说明 */
    }
    throw new Error(detail);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (!signal.aborted) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const dataLine = frame.split("\n").find((line) => line.startsWith("data:"));
      if (!dataLine) continue;
      const raw = dataLine.slice(5).trim();
      if (!raw) continue;
      onEvent(JSON.parse(raw) as Record<string, unknown>);
    }
  }
}


// ── 配置中心：Skill / MCP 资源 ─────────────────────────────────────────────

export interface SkillResourceList {
  available: boolean;
  agentscope_available: boolean;
  external_names?: string[];
  items: SkillResource[];
}

function query(params: Record<string, string>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value) search.set(key, value);
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export async function listSkillResources(filters: { q?: string; source?: string; status?: string } = {}): Promise<SkillResourceList> {
  return read(await fetch(`/api/v1/config/skills${query(filters)}`, { headers: headers(false) }));
}

export async function getSkillResource(id: string): Promise<{ skill: SkillResource }> {
  return read(await fetch(`/api/v1/config/skills/${encodeURIComponent(id)}`, { headers: headers(false) }));
}

export interface SkillWriteBody {
  content?: string;
  files?: { path: string; content: string; encoding?: "utf8" | "base64" }[];
  enabled?: boolean;
  note?: string;
}

export async function createSkillResource(body: SkillWriteBody): Promise<{ skill: SkillResource; sync: { status: string; detail: string } }> {
  return read(await fetch("/api/v1/config/skills", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body),
  }));
}

export async function uploadSkillDirectory(files: File[], enabled = true): Promise<{ skill: SkillResource; sync: { status: string; detail: string } }> {
  const form = new FormData();
  form.append("paths", JSON.stringify(files.map((file) => (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name)));
  form.append("enabled", String(enabled));
  for (const file of files) form.append("files", file, file.name);
  return read(await fetch("/api/v1/config/skills/upload", {
    method: "POST",
    headers: headers(false),
    body: form,
  }));
}

export async function updateSkillResource(id: string, body: SkillWriteBody): Promise<{ skill: SkillResource; sync: { status: string; detail: string } }> {
  return read(await fetch(`/api/v1/config/skills/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: headers(),
    body: JSON.stringify(body),
  }));
}

export async function deleteSkillResource(id: string): Promise<void> {
  await read(await fetch(`/api/v1/config/skills/${encodeURIComponent(id)}`, { method: "DELETE", headers: headers(false) }));
}

export async function skillResourceVersions(id: string): Promise<{ versions: SkillVersion[] }> {
  return read(await fetch(`/api/v1/config/skills/${encodeURIComponent(id)}/versions`, { headers: headers(false) }));
}

export async function restoreSkillVersion(id: string, version: number): Promise<{ skill: SkillResource; sync: { status: string; detail: string } }> {
  return read(await fetch(`/api/v1/config/skills/${encodeURIComponent(id)}/versions/${version}/restore`, {
    method: "POST",
    headers: headers(false),
  }));
}

export async function syncSkillResource(id: string): Promise<{ sync: { status: string; detail: string } }> {
  return read(await fetch(`/api/v1/config/skills/${encodeURIComponent(id)}/sync`, {
    method: "POST",
    headers: headers(false),
  }));
}

export interface McpResourceList {
  available: boolean;
  agentscope_available: boolean;
  items: McpResource[];
}

export interface McpWriteBody {
  name?: string;
  description?: string;
  url?: string;
  transport?: string;
  headers?: McpHeader[];
  auth_type?: string;
  auth_header?: string;
  token?: string;
  timeout?: number;
  enabled?: boolean;
}

export async function listMcpResources(filters: { q?: string; source?: string; status?: string } = {}): Promise<McpResourceList> {
  return read(await fetch(`/api/v1/config/mcps${query(filters)}`, { headers: headers(false) }));
}

export async function getMcpResource(id: string): Promise<{ mcp: McpResource }> {
  return read(await fetch(`/api/v1/config/mcps/${encodeURIComponent(id)}`, { headers: headers(false) }));
}

export async function createMcpResource(body: McpWriteBody): Promise<{ mcp: McpResource; sync: { status: string; detail: string } }> {
  return read(await fetch("/api/v1/config/mcps", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body),
  }));
}

export async function updateMcpResource(id: string, body: McpWriteBody): Promise<{ mcp: McpResource; sync: { status: string; detail: string } }> {
  return read(await fetch(`/api/v1/config/mcps/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: headers(),
    body: JSON.stringify(body),
  }));
}

export async function deleteMcpResource(id: string): Promise<void> {
  await read(await fetch(`/api/v1/config/mcps/${encodeURIComponent(id)}`, { method: "DELETE", headers: headers(false) }));
}

export async function testMcpDraft(body: McpWriteBody & { id?: string }): Promise<McpTestResult> {
  return read(await fetch("/api/v1/config/mcps/test", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body),
  }));
}

export async function testSavedMcp(id: string): Promise<McpTestResult> {
  return read(await fetch(`/api/v1/config/mcps/${encodeURIComponent(id)}/test`, {
    method: "POST",
    headers: headers(false),
  }));
}

export async function syncMcpResource(id: string): Promise<{ sync: { status: string; detail: string } }> {
  return read(await fetch(`/api/v1/config/mcps/${encodeURIComponent(id)}/sync`, {
    method: "POST",
    headers: headers(false),
  }));
}

export interface EmployeeSchedule {
  id: string;
  employee_id: string | null;
  employee_name: string;
  owner_uid: string;
  created_at: string;
  data: ScheduleInput;
}
export interface ScheduleInput {
  name: string;
  description: string;
  cron_expression: string;
  timezone: string;
  enabled: boolean;
  stateful: boolean;
}
export interface ScheduleSession { id: string; created_at: string }
export interface ScheduleMessages {
  messages: { id: string; role: string; name?: string; content: unknown }[];
  has_more: boolean;
  is_running: boolean;
}
export async function listSchedules(page = 1, pageSize = 20, filters: { employee_id?: string; enabled?: boolean; q?: string } = {}) {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (filters.employee_id) params.set('employee_id', filters.employee_id);
  if (filters.enabled !== undefined) params.set('enabled', String(filters.enabled));
  if (filters.q) params.set('q', filters.q);
  return read<{ items: EmployeeSchedule[]; total: number; employee_options: { id: string; name: string }[] }>(await fetch(
    `/api/v1/schedules?${params}`, { headers: headers(false) },
  ));
}
export async function saveSchedule(body: ScheduleInput & { employee_id?: string }, id?: string) {
  return read(await fetch(id ? `/api/v1/schedules/${encodeURIComponent(id)}` : '/api/v1/schedules', {
    method: id ? 'PUT' : 'POST', headers: headers(), body: JSON.stringify(body),
  }));
}
export async function toggleSchedule(id: string, enabled: boolean) {
  return read(await fetch(`/api/v1/schedules/${encodeURIComponent(id)}`, {
    method: 'PATCH', headers: headers(), body: JSON.stringify({ enabled }),
  }));
}
export async function deleteSchedule(id: string) {
  const response = await fetch(`/api/v1/schedules/${encodeURIComponent(id)}`, {
    method: 'DELETE', headers: headers(false),
  });
  if (!response.ok) await read(response);
}
export async function scheduleSessions(id: string, page = 1, pageSize = 20) {
  return read<{ items: ScheduleSession[]; total: number }>(await fetch(
    `/api/v1/schedules/${encodeURIComponent(id)}/sessions?page=${page}&page_size=${pageSize}`, { headers: headers(false) },
  ));
}
export async function scheduleMessages(id: string, sessionId: string, before = '') {
  const query = before ? `?before=${encodeURIComponent(before)}` : '';
  return read<ScheduleMessages>(await fetch(
    `/api/v1/schedules/${encodeURIComponent(id)}/sessions/${encodeURIComponent(sessionId)}/messages${query}`,
    { headers: headers(false) },
  ));
}
