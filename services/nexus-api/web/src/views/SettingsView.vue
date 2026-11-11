<template>
  <ElContainer class="settings-page" data-testid="settings-page">
    <ElContainer class="admin-workspace">
      <ElAside class="admin-sidebar">
        <div class="admin-sidebar__title">配置中心</div>
        <ElMenu class="admin-menu" mode="vertical" :default-active="activeMenu" router>
          <ElMenuItem
            v-for="item in menuItems"
            :key="item.path"
            :index="item.path"
            :data-testid="`settings-menu-${item.id}`"
          >
            <component :is="item.icon" :size="16" aria-hidden="true" />
            <span>{{ item.label }}</span>
          </ElMenuItem>
        </ElMenu>
      </ElAside>
      <ElMain class="admin-workspace__content">
        <ElCard class="content-shell" shadow="never">
          <template #header>
            <div class="admin-workspace__heading">
              <h2 class="admin-workspace__title">{{ activeSectionLabel }}</h2>
              <div id="settings-header-actions" class="admin-workspace__actions" />
            </div>
          </template>
          <RouterView />
        </ElCard>
      </ElMain>
    </ElContainer>
  </ElContainer>
</template>

<script setup lang="ts">
import { Bot, Boxes, Plug, ScrollText, Wrench, Tags, Users } from "lucide-vue-next";
import { computed } from "vue";
import { RouterView, useRoute } from "vue-router";
import { ElAside, ElCard, ElContainer, ElMain, ElMenu, ElMenuItem } from "element-plus";

const route = useRoute();
const menuItems = [
  { id: "employees", label: "数字员工管理", path: "/settings/employees", icon: Users },
  { id: "workers", label: "Agent Worker", path: "/settings/workers", icon: Bot },
  { id: "skills", label: "技能管理", path: "/settings/skills", icon: Wrench },
  { id: "mcps", label: "MCP 管理", path: "/settings/mcps", icon: Plug },
  { id: "categories", label: "任务分类", path: "/settings/categories", icon: Tags },
  { id: "platform-prompt", label: "平台提示词", path: "/settings/platform-prompt", icon: ScrollText },
  { id: "agentscope", label: "AgentScope", path: "/settings/agentscope", icon: Boxes },
];
const activeMenu = computed(() => {
  const match = menuItems.find((item) => route.path === item.path || route.path.startsWith(`${item.path}/`));
  return match ? match.path : "/settings/employees";
});
const activeSectionLabel = computed(() => menuItems.find((item) => item.path === activeMenu.value)?.label ?? "配置中心");
</script>
