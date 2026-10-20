import { nexusGet } from "./http";

export interface ChatBinding {
  conversationId: string;
  employeeId: string;
  employeeName: string;
  agentId: string;
  sessionId: string;
  enabled: boolean;
}

interface ConversationPayload {
  id: string;
  employee_id: string;
  agentscope_session_id: string;
}

interface EmployeePayload {
  id: string;
  name: string;
  enabled: boolean;
  agentscope_agent_id: string;
}

/**
 * 由 Nexus 的 conversation 派生 AgentScope 绑定。
 *
 * 只接受 conversation id：agent / session 必须由后端数据推导，不能让 URL 参数直接指定，
 * 否则改一个参数就能读别人的会话。
 */
export async function loadBinding(conversationId: string): Promise<ChatBinding> {
  const conversation = await nexusGet<ConversationPayload>(
    `/api/v1/conversations/${conversationId}`,
  );
  const employees = await nexusGet<{ items: EmployeePayload[] }>("/api/v1/employees");
  const employee = employees.items.find((item) => item.id === conversation.employee_id);
  if (!employee) throw new Error("会话绑定的员工不存在");
  return {
    conversationId: conversation.id,
    employeeId: employee.id,
    employeeName: employee.name,
    agentId: employee.agentscope_agent_id || "",
    sessionId: conversation.agentscope_session_id || "",
    enabled: employee.enabled,
  };
}
