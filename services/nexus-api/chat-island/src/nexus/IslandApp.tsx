import type { ContentBlock, ToolCallBlock } from "@agentscope-ai/agentscope/message";
import { useEffect, useRef, useState } from "react";

import { ChatContent } from "@/components/chat/ChatContent";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useMessages } from "@/hooks/useMessages";
import { markFreshlyCreated, onSessionBound } from "@/shims/api";

import { GroupApp } from "./GroupApp";
import { uploadAttachment } from "./attachments";
import { loadBinding, type ChatBinding } from "./binding";

function param(name: string): string {
  return new URLSearchParams(window.location.search).get(name) ?? "";
}

/**
 * 聊天岛入口：`channel` 走群组，`conversation` 走私聊。
 *
 * 两种会话的数据模型不同（单人一条 Session vs 每位员工各一条），所以分成两个视图，
 * 但共用同一套 vendored 消息组件与 Nexus 鉴权。
 */
export function IslandApp() {
  const channelId = param("channel");
  return (
    <TooltipProvider>
      {channelId ? (
        <div className="h-full">
          <GroupApp channelId={channelId} />
        </div>
      ) : (
        <PrivateChatApp conversationId={param("conversation")} />
      )}
    </TooltipProvider>
  );
}

/**
 * 私聊：历史与事件流走 Nexus 的原生接口；发送和停止仍由 Nexus 校验归属后转发。
 * 首次发送前会话还没有 Session，这里给 hook 一个占位绑定，等 Nexus 返回真实
 * Session 后再更新绑定，让 hook 重新加载原生历史。
 */
function PrivateChatApp({ conversationId }: { conversationId: string }) {
  const [binding, setBinding] = useState<ChatBinding | null>(null);
  const [bindingError, setBindingError] = useState("");
  const [notice, setNotice] = useState("");
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

  // Nexus 在首次发送时建 Session。绑定更新只用来让事件流连上真实 Session；
  // 此时 POST 可能还没把用户输入落盘，历史可能合法为空，所以标记为“本页新建”，
  // 让 hook 保留刚发出的乐观气泡而不是按空历史重载。
  const bindingRef = useRef<ChatBinding | null>(null);
  useEffect(() => {
    bindingRef.current = binding;
  }, [binding]);
  useEffect(
    () =>
      onSessionBound((sessionId) => {
        const previous = bindingRef.current;
        if (!previous || previous.sessionId === sessionId) return;
        if (!previous.sessionId) markFreshlyCreated(sessionId);
        setBinding({ ...previous, sessionId });
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
    <div className="flex h-full min-h-0 flex-1 flex-col">
      {notice && (
        <div className="mx-4 mt-2 flex items-start justify-between gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          <span>{notice}</span>
          <button type="button" className="shrink-0 underline" onClick={() => setNotice("")}>
            关闭
          </button>
        </div>
      )}
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
          onSend={(content: ContentBlock[]) => chat.send(content)}
          onUserConfirm={async (_tool: ToolCallBlock, _confirm: boolean, _replyId: string) => {
            // 私聊会话固定在 dont_ask 策略下，确认卡不会出现；这里只作为兜底。
            throw new Error("当前会话不提供人工确认");
          }}
          onInterrupt={() => void chat.interrupt()}
          allowedInputTypes={["image/*", "application/pdf", "text/*"]}
          fileProcessor={async (file: File) => {
            try {
              return await uploadAttachment(file);
            } catch (error) {
              setNotice(error instanceof Error ? error.message : "附件上传失败");
              throw error;
            }
          }}
          agentId={agentId}
          sessionId={sessionId}
          cwd={null}
          onCwdChange={() => undefined}
        />
      </div>
    </div>
  );
}
