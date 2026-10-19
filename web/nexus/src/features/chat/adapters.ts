/**
 * 会话绑定：把 Nexus 的目标变成聊天岛需要的 agent/session 与任务查询条件。
 *
 * 消息的读取、订阅与发送已经交给聊天岛；这里不再解析事件，也不再拼装文本增量。
 */
import type { Channel, Conversation, Employee } from "@/types";
import type { ChatAdapter } from "./types";

export class PrivateChatAdapter implements ChatAdapter {
  readonly key: string;
  readonly kind = "contact" as const;

  constructor(private employee: Employee, private conversation: Conversation) {
    this.key = `contact:${employee.id}`;
  }

  get title() {
    return this.employee.name;
  }

  get subtitle() {
    return this.employee.domain;
  }

  get canSend() {
    return this.employee.enabled;
  }

  get conversationId() {
    return this.conversation.id;
  }

  get sessionId() {
    return this.conversation.agentscope_session_id;
  }

  taskQuery(): Record<string, string> {
    if (this.conversation.agentscope_session_id) {
      return { source_session_id: this.conversation.agentscope_session_id };
    }
    return { source_conversation_id: this.conversation.id };
  }
}

export class GroupChatAdapter implements ChatAdapter {
  readonly key: string;
  readonly kind = "group" as const;

  constructor(private channel: Channel, private employees: Employee[]) {
    this.key = `group:${channel.id}`;
  }

  setChannel(channel: Channel, employees: Employee[]) {
    this.channel = channel;
    this.employees = employees;
  }

  get title() {
    return this.channel.name;
  }

  get subtitle() {
    return `${this.channel.members.length} 位数字员工`;
  }

  get canSend() {
    return true;
  }

  get conversationId() {
    return null;
  }

  get sessionId() {
    return null;
  }

  get memberEmployees() {
    return this.channel.members
      .map((member) => this.employees.find((item) => item.id === member.employee_id))
      .filter((item): item is Employee => Boolean(item));
  }

  taskQuery(): Record<string, string> {
    return { source_channel_id: this.channel.id };
  }
}
