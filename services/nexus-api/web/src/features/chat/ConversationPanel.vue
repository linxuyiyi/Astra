<template>
  <div class="conversation-shell">
    <div ref="host" class="conversation-host" />

    <div v-if="loading" class="conversation-overlay" role="status">
      <div class="conversation-loading">
        <Loader2 class="conversation-spinner" :size="18" :stroke-width="1.8" />
        <span>正在连接对话…</span>
      </div>
    </div>

    <div v-else-if="error" class="conversation-overlay" role="alert">
      <div class="conversation-error-card">
        <AlertCircle class="conversation-error-icon" :size="20" :stroke-width="1.7" />
        <p class="conversation-error-title">{{ error }}</p>
        <el-button size="small" @click="open">重新加载</el-button>
      </div>
    </div>
  </div>
</template>

<script lang="ts">
import type { Manifest } from "vite";
import type { MountConversation } from "../../../../shared/conversation";
import bundle from "../../../public/nexus-chat/manifest.json";

const manifest: Manifest = bundle;
const entryKey = "src/mount.tsx";
const base = "/nexus-chat/";
const stylesheets = new Map<string, Promise<void>>();
let ready: Promise<{ mountConversation: MountConversation }> | undefined;

function collectStyles(key: string, seen = new Set<string>()): string[] {
  if (seen.has(key)) return [];
  seen.add(key);
  const { css = [], imports = [] } = manifest[key];
  return [...css, ...imports.flatMap(dependency => collectStyles(dependency, seen))];
}

function loadStyle(file: string) {
  const current = stylesheets.get(file);
  if (current) return current;
  const result = new Promise<void>((resolve, reject) => {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = base + file;
    link.onload = () => resolve();
    link.onerror = () => {
      link.remove();
      stylesheets.delete(file);
      reject(new Error("对话样式加载失败"));
    };
    document.head.append(link);
  });
  stylesheets.set(file, result);
  return result;
}

export function loadConversationBundle() {
  return ready ??= Promise.all([
    import(/* @vite-ignore */ base + manifest[entryKey].file),
    Promise.all(collectStyles(entryKey).map(loadStyle)),
  ]).then(([module]) => module).catch(error => {
    ready = undefined;
    throw error;
  });
}
</script>

<script setup lang="ts">
import { AlertCircle, Loader2 } from "lucide-vue-next";
import { onBeforeUnmount, onMounted, ref } from "vue";
import type { ConversationProps } from "../../../../shared/conversation";

const props = defineProps<{ conversation: ConversationProps }>();
const host = ref<HTMLElement>();
const loading = ref(true);
const error = ref("");
let unmount: (() => void) | undefined;
let disposed = false;

async function open() {
  error.value = "";
  loading.value = true;
  try {
    const { mountConversation } = await loadConversationBundle();
    if (!disposed && host.value) unmount = mountConversation(host.value, props.conversation);
  } catch {
    if (!disposed) error.value = "对话界面加载失败";
  } finally {
    if (!disposed) loading.value = false;
  }
}

onMounted(open);
onBeforeUnmount(() => { disposed = true; unmount?.(); });
</script>

<style scoped>
.conversation-shell {
  position: relative;
  display: flex;
  flex: 1;
  flex-direction: column;
  min-block-size: 0;
  min-inline-size: 0;
}

.conversation-host {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-block-size: 0;
  min-inline-size: 0;
}

.conversation-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--astra-bg-panel);
  z-index: 2;
}

.conversation-loading {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--astra-text-muted);
  font-size: 13px;
}

.conversation-spinner {
  color: var(--astra-text-secondary);
  animation: conversation-spin 0.9s linear infinite;
}

.conversation-error-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  max-width: 320px;
  padding: 20px 24px;
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-subtle);
  text-align: center;
}

.conversation-error-icon {
  color: var(--astra-danger);
}

.conversation-error-title {
  margin: 0;
  color: var(--astra-text-primary);
  font-size: 13.5px;
  font-weight: 500;
}

@keyframes conversation-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
