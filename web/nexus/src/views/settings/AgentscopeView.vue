<template>
  <div class="astra-page agentscope-page" data-testid="agentscope-page">
    <SettingsHeaderActions>
      <div class="settings-header-group">
        <ElButton :icon="RefreshCw" :loading="loading" @click="load">刷新</ElButton>
        <ElButton type="primary" :icon="ExternalLink" @click="openWindow">新窗口打开控制台</ElButton>
      </div>
    </SettingsHeaderActions>

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
import { ElAlert, ElButton } from "element-plus";
import SettingsHeaderActions from "./SettingsHeaderActions.vue";
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

onMounted(async () => {
  await load();
});
</script>

<style scoped>
.agentscope-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  gap: 8px;
}
.settings-header-group {
  display: flex;
  align-items: center;
  gap: 8px;
}
.agentscope-frame {
  flex: 1 1 auto;
  width: 100%;
  height: 100%;
  min-height: 520px;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  background: #fff;
}
.agentscope-loading {
  flex: 1 1 auto;
  display: grid;
  place-items: center;
  color: var(--astra-text-muted);
}
</style>

