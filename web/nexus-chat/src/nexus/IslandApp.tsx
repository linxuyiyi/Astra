import type { ContentBlock, ToolCallBlock } from "@agentscope-ai/agentscope/message";
import { useEffect, useState } from "react";

import { ChatContent } from "@/components/chat/ChatContent";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useMessages } from "@/hooks/useMessages";
import { onSessionBound } from "@/shims/api";

import { loadBinding, type ChatBinding } from "./binding";

function param(name: string): string {
  return new URLSearchParams(window.location.search).get(name) ?? "";
}

/**
 * 阶段 B：聊天岛接管私聊的读、发、停。
 *
 * 历史与事件流走 Nexus 的原生接口；发送和停止仍由 Nexus 校验归属后转发。
 * 首次发送前会话还没有 Session，这里给 hook 一个占位绑定，等 Nexus 返回真实
 * Session 后再更新绑定，让 hook 重新加载原生历史。
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

  // Nexus 在首次发送时建 Session；绑定更新后 hook 会重新加载原生历史。
  useEffect(
    () =>
      onSessionBound((sessionId) => {
        setBinding((previous) =>
          previous && previous.sessionId !== sessionId
            ? { ...previous, sessionId }
            : previous,
        );
      }),
    [],
  );

  const agentId = binding ? binding.agentId || "nexus" : null;
  const sessionId = binding ? binding.sessionId || "pending" : null;
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
      <div className="flex h-full min-h-0 flex-1 flex-col">
        {chat.error && (
          <div className="mx-4 mt-2 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
            {chat.error.message}
          </div>
        )}
        <div className="relative flex min-h-0 flex-1 justify-center overflow-hidden px-4 [--chat-content-w:48rem]">
          <ChatContent
            className="w-full max-w-[var(--chat-content-w)]"
            msgs={chat.msgs}
            loading={resolving || chat.loading}
            phase={chat.phase}
            disabled={!binding || !binding.enabled}
            onSend={(content: ContentBlock[]) => void chat.send(content)}
            onUserConfirm={async (_tool: ToolCallBlock, _confirm: boolean, _replyId: string) => {
              // 私聊会话固定在 dont_ask 策略下，确认卡不会出现；这里只作为兜底。
              throw new Error("当前会话不提供人工确认");
            }}
            onInterrupt={() => void chat.interrupt()}
            allowedInputTypes={["image/*", "application/pdf", "text/*"]}
            fileProcessor={async () => null}
            agentId={agentId}
            sessionId={sessionId}
            cwd={null}
            onCwdChange={() => undefined}
          />
        </div>
      </div>
    </TooltipProvider>
  );
}
