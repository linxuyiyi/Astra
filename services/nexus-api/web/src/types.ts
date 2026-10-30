export interface Employee {
  id: string;
  name: string;
  domain: string;
  description: string;
  instructions: string;
  avatar: string;
  enabled: boolean;
  knowledge: string[];
  skills: string[];
  mcps: string[];
  tools: string[];
  capability?: {
    runtime_status?: string;
    error?: string;
    checked_at?: string;
    desired_revision?: string;
    applied_revision?: string;
  };
  agentscope_agent_id: string | null;
  mailbox_session_id: string | null;
  model_route_id?: string;
}

export interface SkillItem {
  id: string;
  name: string;
}

export interface ResourceCatalog {
  available: boolean;
  items: SkillItem[];
}

export type ResourceSource = "nexus" | "agentscope" | "builtin";

export interface ResourceEmployee {
  id: string;
  name: string;
  enabled: boolean;
}

export interface ResourceSync {
  status: "applied" | "pending" | "error" | "disabled" | "unbound";
  detail: string;
  applied?: number;
  pending?: number;
  error?: number;
  last_synced_at?: string;
}

export interface SkillFile {
  path: string;
  size: number;
  content?: string;
  encoding?: "utf8" | "base64";
}

export interface SkillVersion {
  version: number;
  note: string;
  created_at: string;
  files: { path: string; size: number }[];
}

export interface SkillResource {
  id: string;
  name: string;
  description: string;
  source: ResourceSource;
  version: number | string;
  revision?: number;
  runtime_name?: string;
  files: SkillFile[];
  enabled: boolean;
  editable: boolean;
  employees: ResourceEmployee[];
  sync: ResourceSync;
  versions?: SkillVersion[];
  created_at?: string;
  updated_at?: string;
}

export interface McpHeader {
  key: string;
  value: string;
}

export interface McpResource {
  id: string;
  name: string;
  description: string;
  source: ResourceSource | "agentscope";
  url: string;
  transport: string;
  headers: McpHeader[];
  auth_type: string;
  auth_header: string;
  has_token: boolean;
  timeout: number;
  runtime_name?: string;
  enabled: boolean;
  revision?: number;
  editable: boolean;
  employees: ResourceEmployee[];
  sync: ResourceSync;
  created_at?: string;
  updated_at?: string;
}

export interface McpToolPreview {
  name: string;
  description: string;
}

export interface McpTestResult {
  ok: boolean;
  tools: McpToolPreview[];
  error: string;
  elapsed_ms: number;
}

export interface Attachment {
  id: string;
  name: string;
  type: string;
  size: number;
}

export interface ChatMessageUsage {
  input_tokens?: number;
  output_tokens?: number;
}

export interface ChatMessage {
  /** 私聊历史/事件的服务端重放游标，仅用于防止旧视图覆盖新消息。 */
  replay_cursor?: string;
  native?: import("@agentscope-ai/agentscope/message").Msg;
  sender_type?: "human" | "employee" | "system";
  sender_name?: string;
  thinking?: string[];
  hints?: { id: string; source: string; text: string }[];
  data?: import("@agentscope-ai/agentscope/message").DataBlock[];
  finished_reason?: string | null;
  id: string;
  sender: string;
  speaker?: string;
  text: string;
  tools?: { id: string; name: string; input: string; output: string; state: string }[];
  attachments: Attachment[];
  created_at: string;
  /** AgentScope: null while running; ISO string when done. Missing = legacy finished. */
  finished_at?: string | null;
  usage?: ChatMessageUsage | null;
}

export interface Conversation {
  id: string;
  employee_id: string;
  name: string;
  agentscope_session_id: string | null;
  created_at?: string;
}

export interface ChannelMember {
  member_id: string;
  member_type: "human" | "employee";
  name: string;
  avatar: string;
  role: "owner" | "admin" | "member";
  online: boolean;
  read_seq: number;
  employee_id: string;
  agentscope_session_id: string | null;
}

export interface Channel {
  discussion?: { state: string; detail?: string } | null;
  my_role: "owner" | "admin" | "member";
  viewer_id: string;
  id: string;
  name: string;
  description: string;
  members: ChannelMember[];
  created_at?: string;
}

export interface AgentScopeOutcome {
  accepted: boolean;
  detail: string;
  session_id?: string | null;
}

export interface RuntimeStatus {
  agentscope_version: string;
  configured: boolean;
  reachable: boolean;
  agentscope_user_id?: string;
}
