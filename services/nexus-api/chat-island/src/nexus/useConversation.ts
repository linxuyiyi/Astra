import { useEffect, useMemo, useSyncExternalStore } from "react";
import { ConversationStore } from "./conversationStore";

export function useConversation(kind: "private" | "group", id: string, onMembersChanged?: () => void) {
  const store = useMemo(() => new ConversationStore(kind, id), [kind, id]);
  useEffect(() => {
    store.onMembersChanged = onMembersChanged;
  }, [store, onMembersChanged]);
  useEffect(() => store.start(), [store]);
  const state = useSyncExternalStore(store.subscribe, store.snapshot);
  return {
    ...state,
    send: store.send,
    stop: store.stop,
    retry: store.retry,
    restorePendingDraft: store.restorePendingDraft,
    dismissError: store.dismissError,
    loadOlder: store.loadOlder,
  };
}
