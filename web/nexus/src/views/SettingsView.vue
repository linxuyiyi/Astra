<template>
  <ElContainer class="settings-page" data-testid="settings-page">
    <ElContainer class="settings-workspace">
      <ElAside class="settings-aside">
        <div class="settings-aside__title">配置中心</div>
        <ElMenu class="settings-menu" mode="vertical" :default-active="activeMenu" router>
          <ElMenuItem
            v-for="item in menuItems"
            :key="item.path"
            :index="item.path"
            :data-testid="`settings-menu-${item.id}`"
          >
            <component :is="item.icon" :size="16" />
            <span>{{ item.label }}</span>
          </ElMenuItem>
        </ElMenu>
      </ElAside>
      <ElMain class="settings-content">
        <ElCard class="content-shell" shadow="never">
          <RouterView />
        </ElCard>
      </ElMain>
    </ElContainer>
  </ElContainer>
</template>

<script setup lang="ts">
import { Bot, Boxes, Tags, Users } from "lucide-vue-next";
import { computed } from "vue";
import { RouterView, useRoute } from "vue-router";
import { ElAside, ElCard, ElContainer, ElMain, ElMenu, ElMenuItem } from "element-plus";

const route = useRoute();
const menuItems = [
  { id: "employees", label: "数字员工管理", path: "/settings/employees", icon: Users },
  { id: "workers", label: "Agent Worker", path: "/settings/workers", icon: Bot },
  { id: "categories", label: "任务分类", path: "/settings/categories", icon: Tags },
  { id: "agentscope", label: "AgentScope", path: "/settings/agentscope", icon: Boxes },
];
const activeMenu = computed(() => {
  const match = menuItems.find((item) => route.path === item.path || route.path.startsWith(`${item.path}/`));
  return match ? match.path : "/settings/employees";
});
</script>

<style scoped>
.settings-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 480px;
  padding: 0;
  overflow: hidden;
  background: var(--astra-bg-base);
}
.settings-workspace { align-items: stretch; }
.settings-aside {
  --el-aside-width: 210px;
  border: 0;
  border-right: 1px solid var(--astra-border);
  border-radius: 12px 0 0 12px;
  background: transparent;
  overflow: auto;
}
.settings-aside__title {
  padding: 16px 18px 8px;
  color: var(--astra-text-primary);
  font-size: var(--astra-font-section);
  font-weight: var(--astra-weight-bold);
}
.settings-menu {
  --el-menu-base-level-padding: 14px;
  --el-menu-item-height: 44px;
  border-right: 0;
  background: transparent;
}
.settings-menu :deep(.el-menu-item) { gap: 8px; }
.settings-content {
  --el-main-padding: 0;
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
}
.settings-content > .content-shell { flex: 1 1 auto; min-height: 0; }
.settings-content :deep(.astra-page) { height: 100%; min-height: 0; }
@media (max-width: 860px) {
  .settings-aside { --el-aside-width: 170px; }
}
</style>
