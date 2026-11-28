<template>
  <div class="app" :class="{ 'app--office': route.path === '/', 'app--chat': route.path === '/chat' }">
    <header v-if="route.path !== '/'" class="top">
      <div class="top-inner">
        <router-link to="/" class="brand" aria-label="Nexus 数字办公室">
          <span class="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none">
              <path d="M12 3.5L14.1 9.9L20.5 12L14.1 14.1L12 20.5L9.9 14.1L3.5 12L9.9 9.9L12 3.5Z" fill="currentColor" />
            </svg>
          </span>
          <span class="brandname">NEXUS</span>
          <span class="version">v1.6.0</span>
        </router-link>
        <span class="top-divider" aria-hidden="true" />
        <nav class="top-nav" aria-label="页面导航">
          <router-link to="/" class="top-nav__item" title="返回数字办公室">
            <LayoutGrid :size="15" aria-hidden="true" />
            <span>办公室</span>
          </router-link>
          <template v-for="item in navItems" :key="item.to">
            <span v-if="item.to === '/gateway'" class="top-nav__sep" aria-hidden="true" />
            <router-link
              :to="item.to"
              class="top-nav__item"
              :aria-current="item.active ? 'page' : undefined"
            >
              <component :is="navIcon(item.to)" :size="15" aria-hidden="true" />
              <span>{{ item.label }}</span>
            </router-link>
          </template>
        </nav>
        <span class="env-banner" :title="banner">
          <i class="astra-service-dot" :data-state="runtime?.reachable ? 'live' : 'unknown'" aria-hidden="true" />
          <span class="env-text">{{ banner }}</span>
        </span>
      </div>
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
import { BookOpen, CalendarClock, LayoutGrid, ListTodo, MessageSquare, Network, Settings } from "lucide-vue-next";
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

const navIcons: Record<string, unknown> = {
  "/chat": MessageSquare,
  "/schedules": CalendarClock,
  "/tasks": ListTodo,
  "/library": BookOpen,
  "/gateway": Network,
  "/settings": Settings,
};

function navIcon(to: string) {
  return navIcons[to] ?? LayoutGrid;
}

const runtime = ref<RuntimeStatus | null>(null);
const identity = ref<"loading" | "ready" | "ask">("loading");
const uid = ref("");
const identityError = ref("");
const banner = computed(() => {
  if (!runtime.value) return "检查 AgentScope…";
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
.brand {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex: none;
  text-decoration: none;
  color: var(--astra-text-primary);
}
.brand-mark {
  display: inline-grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: 7px;
  background: var(--astra-primary);
  color: #fafafa;
  flex: none;
}
.brand-mark svg { width: 15px; height: 15px; }
.top-divider {
  width: 1px;
  height: 18px;
  background: var(--astra-border);
  flex: none;
}
.uid-card { width: min(420px, calc(100% - 32px)); margin: 64px auto; color: var(--astra-text-primary); }
.app--office {
  padding: 0;
  gap: 0;
  height: 100dvh;
  min-height: 100dvh;
  overflow: hidden;
  background: var(--astra-bg-base);
}
.app--office .page { max-width: none; padding: 0; height: 100dvh; overflow: hidden; }

.top-nav {
  display: flex;
  align-items: center;
  gap: 2px;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
}
.top-nav::-webkit-scrollbar { display: none; }
.top-nav__sep {
  width: 1px;
  height: 16px;
  margin: 0 6px;
  background: var(--astra-border);
  flex: none;
}
.top-nav__item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 10px;
  border-radius: 7px;
  color: var(--astra-text-secondary);
  font-size: 13px;
  font-weight: 500;
  text-decoration: none;
  white-space: nowrap;
  flex: none;
  transition: background-color 0.15s ease, color 0.15s ease;
}
.top-nav__item svg { color: var(--astra-text-muted); stroke-width: 1.75; flex: none; transition: color 0.15s ease; }
.top-nav__item:hover { background: var(--astra-bg-base); color: var(--astra-text-primary); }
.top-nav__item:hover svg { color: var(--astra-text-primary); }
.top-nav__item[aria-current='page'] {
  background: var(--astra-bg-elevated);
  color: var(--astra-text-primary);
  font-weight: 600;
}
.top-nav__item[aria-current='page'] svg { color: var(--astra-text-primary); }
.env-text { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (min-width: 2240px) {
  .brand-mark { width: 26px; height: 26px; border-radius: 8px; }
  .brand-mark svg { width: 16px; height: 16px; }
  .top-nav { gap: 4px; }
  .top-nav__item { height: 34px; padding: 0 12px; gap: 7px; font-size: 13.5px; }
  .top-nav__sep { margin: 0 8px; }
  .env-text { max-width: 340px; }
}
@media (max-width: 1060px) {
  .env-text { max-width: 120px; }
  .top-nav__item { padding: 0 8px; }
}
@media (max-width: 900px) {
  .top-divider { display: none; }
  .app--office { height: 100dvh; min-height: 100dvh; overflow: hidden; }
}
</style>
