<template>
  <div class="astra-page agentscope-page" data-testid="agentscope-page">
    <div class="agentscope-toolbar">
      <div class="agentscope-status">
        <ElTag :type="runtime?.reachable ? 'success' : 'danger'">{{ runtime?.reachable ? "可达" : "不可达" }}</ElTag>
        <span>AgentScope {{ runtime?.agentscope_version ?? "2.0.10" }}</span>
      </div>
      <div class="agentscope-actions">
        <ElButton :icon="RefreshCw" :loading="loading" @click="load">刷新</ElButton>
        <ElButton :icon="ExternalLink" @click="openWindow">新窗口打开</ElButton>
      </div>
    </div>

    <ElAlert v-if="error" type="warning" show-icon :closable="false" :title="error" />

    <iframe
      v-if="ready"
      :key="iframeKey"
      class="agentscope-frame"
      src="/agentscope-ui/"
      title="AgentScope 管理界面"
      data-testid="agentscope-frame"
    />
    <div v-else class="agentscope-loading">正在载入 AgentScope 管理界面…</div>
  </div>
</template>

<script setup lang="ts">
import { ExternalLink, RefreshCw } from "lucide-vue-next";
import { onMounted, ref } from "vue";
import { ElAlert, ElButton, ElTag } from "element-plus";
import { getRuntime } from "@/api";
import type { RuntimeStatus } from "@/types";

const runtime = ref<RuntimeStatus | null>(null);
const loading = ref(false);
const error = ref("");
const ready = ref(false);
const iframeKey = ref(0);

function seedUiContext() {
  const userId = runtime.value?.agentscope_user_id?.trim() || "nexus";
  localStorage.setItem("server_url", "/agentscope-api/");
  localStorage.setItem("username", userId);
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    runtime.value = await getRuntime();
  } catch (reason) {
    runtime.value = null;
    error.value = reason instanceof Error ? reason.message : "无法读取 AgentScope 状态";
  } finally {
    seedUiContext();
    ready.value = true;
    iframeKey.value += 1;
    loading.value = false;
  }
}

function openWindow() {
  seedUiContext();
  window.open("/agentscope-ui/", "_blank", "noopener");
}

onMounted(load);
</script>

<style scoped>
.agentscope-page { gap: 10px; }
.agentscope-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; flex: none; }
.agentscope-status { display: flex; align-items: center; gap: 8px; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); }
.agentscope-actions { display: flex; gap: 8px; margin-left: auto; }
.agentscope-frame { flex: 1 1 auto; width: 100%; min-height: 0; border: 0; border-radius: 8px; background: #fff; }
.agentscope-loading { flex: 1 1 auto; display: grid; place-items: center; color: var(--astra-text-muted); }
</style>
