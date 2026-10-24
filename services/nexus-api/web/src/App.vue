<template>
  <div class="app" :class="{ 'app--nebula': route.path === '/' }">
    <header v-if="route.path !== '/'" class="top">
      <router-link to="/" class="brand" title="星云首页">
        <div class="mark">✦</div>
        <div class="brandname">NEXUS <span class="version">v1.4.8</span></div>
      </router-link>
      <nav class="top-nav" aria-label="页面导航">
        <router-link
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          :class="{ active: item.active }"
        >{{ item.label }}</router-link>
      </nav>
      <span class="env-banner" :title="banner">
        <i class="dot" :class="{ off: !runtime?.reachable }"></i>
        <span class="env-text">{{ banner }}</span>
      </span>
    </header>
    <main class="page">
      <el-card v-if="identity === 'ask'" class="auth-card">
        <h2>{{ authMode === 'login' ? '登录 Nexus' : '创建 Nexus 账号' }}</h2>
        <el-form label-position="top" @submit.prevent="submitLogin">
          <el-form-item label="用户名"><el-input v-model="username" autocomplete="username" /></el-form-item>
          <el-form-item label="密码"><el-input v-model="password" type="password" show-password :autocomplete="authMode === 'login' ? 'current-password' : 'new-password'" placeholder="至少 10 位" /></el-form-item>
          <el-form-item v-if="authMode === 'register'" label="姓名"><el-input v-model="displayName" autocomplete="name" /></el-form-item>
          <el-alert v-if="authError" :title="authError" type="error" :closable="false" />
          <el-button type="primary" native-type="submit" :loading="authBusy" style="margin-top:16px">{{ authMode === 'login' ? '登录' : '注册并登录' }}</el-button>
          <el-button @click="authMode = authMode === 'login' ? 'register' : 'login'">{{ authMode === 'login' ? '创建账号' : '已有账号' }}</el-button>
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
import { canManagePlatform, getRuntime, hasPermission, resolveOwner, signIn } from "./api";
import type { RuntimeStatus } from "./types";

const store = useAstraStore();
const route = useRoute();
const navItems = computed(() => {
  const items = [
    {
      to: "/chat",
      label: "数字员工",
      active: route.path === "/chat" || route.path === "/team",
      requiredPermission: "chat",
    },
    {
      to: "/tasks",
      label: "任务大厅",
      active: route.path.startsWith("/tasks") || route.path.startsWith("/interventions"),
      requiredPermission: "*",
    },
    {
      to: "/library",
      label: "数字图书馆",
      active: route.path.startsWith("/library"),
      requiredPermission: "*",
    },
    {
      to: "/gateway",
      label: "API 网关",
      active: route.path.startsWith("/gateway"),
      requiredPermission: "*",
    },
    {
      to: "/settings",
      label: "配置中心",
      active:
        route.path.startsWith("/settings") ||
        route.path.startsWith("/employees") ||
        route.path.startsWith("/workers") ||
        route.path.startsWith("/schedules"),
      requiredPermission: "*",
    },
  ];

  return items.filter((item) => {
    if (item.requiredPermission === "*") {
      return canManagePlatform();
    }
    return hasPermission(item.requiredPermission) || canManagePlatform();
  });
});

const runtime = ref<RuntimeStatus | null>(null);
const identity = ref<"loading" | "ready" | "ask">("loading");
const username = ref("");
const password = ref("");
const displayName = ref("");
const authMode = ref<"login" | "register">("login");
const authBusy = ref(false);
const authError = ref("");
const banner = computed(() => {
  if (!runtime.value) return "正在检查 AgentScope";
  if (runtime.value.reachable) return `AgentScope ${runtime.value.agentscope_version} 已连接`;
  if (runtime.value.configured) return `AgentScope ${runtime.value.agentscope_version} 不可达`;
  return `AgentScope ${runtime.value.agentscope_version} 未连接`;
});

async function submitLogin() {
  authBusy.value = true; authError.value = "";
  try { await signIn(authMode.value, username.value, password.value, displayName.value); location.reload(); }
  catch (error) { authError.value = error instanceof Error ? error.message : "登录失败"; }
  finally { authBusy.value = false; }
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
  if (identity.value === "ready" && canManagePlatform() && !store.lastUpdated) void store.refresh();
  await refreshRuntime();
  runtimeTimer = window.setInterval(refreshRuntime, 15_000);
});

onBeforeUnmount(() => {
  if (runtimeTimer) window.clearInterval(runtimeTimer);
});
</script>

<style scoped>
.brand { text-decoration: none; color: inherit; }
.auth-card { width:min(420px, calc(100% - 32px)); margin:64px auto; color:var(--astra-text-primary); }
.app--nebula {
  padding: 0;
  gap: 0;
  height: 100dvh;
  min-height: 100dvh;
  overflow: hidden;
  background: #010e16;
}
.app--nebula .top,
.app--nebula .page { max-width: none; }
.app--nebula .page { height: 100dvh; overflow: hidden; }

.top-nav { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.top-nav a {
  padding: 7px 12px; border-radius: 10px; color: #6c897a; text-decoration: none;
  white-space: nowrap; font-size: 13px;
}
.top-nav a:hover { background: #edf7f1; color: #187956; }
.top-nav a.active { background: #dff3e7; color: #147d59; font-weight: 750; }
.env-text { max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.uid-gate { display: flex; gap: 12px; align-items: end; max-width: 420px; }
.uid-gate label { display: flex; flex-direction: column; gap: 6px; flex: 1; }
.uid-gate input { padding: 8px 10px; border: 1px solid #c9ddd2; border-radius: 8px; }
.uid-gate button { padding: 8px 14px; border: 0; border-radius: 8px; background: #187956; color: white; }
@media (max-width: 900px) {
  .env-text { max-width: 120px; }
  .top-nav a { padding: 6px 9px; }
  .app--nebula { height: 100dvh; min-height: 100dvh; overflow: hidden; }
}
</style>
