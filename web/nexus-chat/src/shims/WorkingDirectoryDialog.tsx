/**
 * 替换上游 `@/components/dialog/WorkingDirectoryDialog`。
 *
 * 上游用它选择 AgentScope workspace 的工作目录。Nexus 的会话工作目录由后端在建 Session
 * 时固定（`ws-<conversation_id>`），用户不应在聊天区改它，所以这里渲染空节点：
 * ChatContent 的 header 行因此不出现任何控件，也不必改动 vendored 组件。
 */
export function WorkingDirectoryDialog(_props: {
  agentId?: string | null;
  sessionId?: string | null;
  value?: string | null;
  onChange?: (cwd: string | null) => void | Promise<void>;
  disabled?: boolean;
  className?: string;
}) {
  return null;
}
