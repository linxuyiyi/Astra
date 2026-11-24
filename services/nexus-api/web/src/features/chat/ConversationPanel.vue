<template>
  <div v-if="error" class="conversation-load-error" role="alert">
    {{ error }} <el-button size="small" @click="open">重试</el-button>
  </div>
  <p v-if="loading" class="conversation-load-error" role="status">正在打开对话…</p>
  <div ref="host" class="conversation-host" />
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
.conversation-host { display: flex; flex: 1; flex-direction: column; min-block-size: 0; min-inline-size: 0; }
.conversation-load-error { margin: 20px 24px; color: var(--astra-text-secondary); }
</style>
