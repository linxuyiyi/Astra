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
  tools: string[];
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

export interface Attachment {
  id: string;
  name: string;
  type: string;
  size: number;
}

export interface ChatMessage {
  id: string;
  sender: string;
  speaker?: string;
  text: string;
  tools?: { id: string; name: string; input: string; output: string; state: string }[];
  attachments: Attachment[];
  created_at: string;
}

export interface Conversation {
  id: string;
  employee_id: string;
  name: string;
  agentscope_session_id: string | null;
  created_at?: string;
}

export interface ChannelMember {
  employee_id: string;
  agentscope_session_id: string | null;
}

export interface Channel {
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
