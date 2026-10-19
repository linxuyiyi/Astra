import type { ContentBlock, ToolCallBlock } from "@agentscope-ai/agentscope/message";
import { useEffect, useState } from "react";

import { ChatContent } from "@/components/chat/ChatContent";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useMessages } from "@/hooks/useMessages";

import { loadBinding, type ChatBinding } from "./binding";

function param(name: string): string {
  return new URLSearchParams(window.location.search).get(name) ?? "";
}

/**
 * 阶段 A：只读聊天岛。
 *
 * 数据来自 Nexus 的会话历史，渲染完全交给上游 ChatContent / ASMessageBubble。
 * 发送、停止、确认不在本阶段接管，所以输入区禁用；Vue 外壳仍是唯一的发送入口。
 */
export function IslandApp() {
  const conversationId = param("conversation");
  const [binding, setBinding] = useState<ChatBinding | null>(null);
  const [bindingError, setBindingError] = useState("");
  const [resolving, setResolving] = useState(Boolean(conversationId));

  useEffect(() => {
    if (!conversationId) {
      setBindingError("缺少 conversation 参数");
      setResolving(false);
      return;
    }
    let cancelled = false;
    setResolving(true);
    loadBinding(conversationId)
      .then((next) => {
        if (!cancelled) setBinding(next);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setBindingError(error instanceof Error ? error.message : "读取会话绑定失败");
        }
      })
      .finally(() => {
        if (!cancelled) setResolving(false);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId]);

  const agentId = binding?.agentId || null;
  const sessionId = binding?.sessionId || null;
  const chat = useMessages(agentId, sessionId);

  if (bindingError) {
    return (
      <div className="grid h-full place-items-center p-6 text-sm text-muted-foreground">
        {bindingError}
      </div>
    );
  }

  return (
    <TooltipProvider>
      <div className="relative flex h-full min-h-0 flex-1 justify-center overflow-hidden px-4 [--chat-content-w:48rem]">
        <ChatContent
          className="w-full max-w-[var(--chat-content-w)]"
          msgs={chat.msgs}
          loading={resolving || chat.loading}
          phase={chat.phase}
          disabled
          onSend={(_content: ContentBlock[]) => undefined}
          onUserConfirm={async (_tool: ToolCallBlock, _confirm: boolean, _replyId: string) => undefined}
          allowedInputTypes={["image/*", "application/pdf", "text/*"]}
          fileProcessor={async () => null}
          agentId={agentId}
          sessionId={sessionId}
          cwd={null}
          onCwdChange={() => undefined}
        />
      </div>
    </TooltipProvider>
  );
}
