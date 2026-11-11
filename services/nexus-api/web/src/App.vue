<template>
  <div class="app" :class="{ 'app--office': route.path === '/', 'app--chat': route.path === '/chat' }">
    <header v-if="route.path !== '/'" class="top">
      <router-link to="/" class="brand" aria-label="Nexus 数字办公室">
        <div class="brandname">NEXUS <span class="version">v1.5.0</span></div>
      </router-link>
      <nav class="top-nav" aria-label="页面导航">
        <router-link
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          :aria-current="item.active ? 'page' : undefined"
        >{{ item.label }}</router-link>
      </nav>
      <span class="env-banner" :title="banner">
        <i class="astra-service-dot" :data-state="runtime?.reachable ? 'live' : 'unknown'" aria-hidden="true" />
        <span class="env-text">{{ banner }}</span>
      </span>
    </header>
    <main class="page">
      <el-card v-if="identity === 'ask'" class="uid-card">
        <el-form label-position="top" @submit.prevent="submitUid">
          <el-form-item label="临时 UID"><el-input v-model="uid" maxlength="64" placeholder="输入之前使用的 UID" /></el-form-item>
          <el-alert v-if="identityError" :title="identityError" type="error" :closable="false" />
          <el-button type="primary" native-type="submit">进入</el-button>
        </el-form>
      </el-card>
      <router-view v-else-if="identity === 'ready'" />
    </main>
    <AstraConfirmHost />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import AstraConfirmHost from "@/components/astra/AstraConfirmHost.vue";
import { useAstraStore } from "@/stores/astra";
import { getRuntime, resolveOwner, saveOwnerId } from "./api";
import { getVisibleNavItems } from "./navigation";
import type { RuntimeStatus } from "./types";

const store = useAstraStore();
const route = useRoute();
const navItems = computed(() => getVisibleNavItems(route.path));

const runtime = ref<RuntimeStatus | null>(null);
const identity = ref<"loading" | "ready" | "ask">("loading");
const uid = ref("");
const identityError = ref("");
const banner = computed(() => {
  if (!runtime.value) return "正在检查 AgentScope";
  if (runtime.value.reachable) return `AgentScope ${runtime.value.agentscope_version} 已连接`;
  if (runtime.value.configured) return `AgentScope ${runtime.value.agentscope_version} 不可达`;
  return `AgentScope ${runtime.value.agentscope_version} 未连接`;
});

function submitUid() {
  try {
    saveOwnerId(uid.value);
    identity.value = "ready";
    if (!store.lastUpdated) void store.refresh();
  } catch (error) {
    identityError.value = error instanceof Error ? error.message : "UID 无效";
  }
}

async function refreshRuntime() {
  try {
    runtime.value = await getRuntime();
  } catch {
    runtime.value = { agentscope_version: "2.0.10", configured: false, reachable: false, agentscope_user_id: "nexus" };
  }
}

let runtimeTimer = 0;

onMounted(async () => {
  try {
    await resolveOwner();
    identity.value = "ready";
  } catch {
    identity.value = "ask";
  }
  if (identity.value === "ready" && !store.lastUpdated) void store.refresh();
  await refreshRuntime();
  runtimeTimer = window.setInterval(refreshRuntime, 15_000);
});

onBeforeUnmount(() => {
  if (runtimeTimer) window.clearInterval(runtimeTimer);
});
</script>

<style scoped>
.brand { text-decoration: none; color: inherit; }
.uid-card { width:min(420px, calc(100% - 32px)); margin:64px auto; color:var(--astra-text-primary); }
.app--office {
  padding: 0;
  gap: 0;
  height: 100dvh;
  min-height: 100dvh;
  overflow: hidden;
  background: #010e16;
}
.app--office .top,
.app--office .page { max-width: none; }
.app--office .page { height: 100dvh; overflow: hidden; }

.top-nav { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.top-nav a {
  display: inline-flex;
  align-items: center;
  min-height: 36px;
  padding: 0 12px;
  border-radius: 8px;
  color: var(--astra-text-secondary);
  text-decoration: none;
  white-space: nowrap;
}
.top-nav a:hover { background: var(--astra-bg-elevated); }
.top-nav a[aria-current='page'] {
  background: var(--astra-primary-soft);
  color: var(--astra-primary-bright);
  font-weight: 600;
}
.env-text { max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 900px) {
  .env-text { max-width: 120px; }
  .top-nav a { padding: 0 8px; }
  .app--office { height: 100dvh; min-height: 100dvh; overflow: hidden; }
}
</style>
