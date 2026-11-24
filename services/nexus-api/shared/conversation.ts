/** 数字员工页与对话区之间的边界。id 是 Nexus 私聊或群组 id。 */
export interface ConversationProps {
  kind: "private" | "group";
  id: string;
  viewerId: string;
  title: string;
  avatar?: string;
  enabled: boolean;
  onActivity?: (summary: string) => void;
  onMembersChanged?: () => void;
}

export type MountConversation = (element: HTMLElement, props: ConversationProps) => () => void;
