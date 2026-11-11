<template>
  <ElContainer class="gateway-page" data-testid="gateway-page">
    <ElContainer class="admin-workspace">
      <ElAside class="admin-sidebar">
        <div class="admin-sidebar__title">API 网关</div>
        <ElMenu class="admin-menu" mode="vertical" :default-active="activeMenu" router>
          <ElMenuItemGroup v-for="group in menuGroups" :key="group.id" :title="group.label">
            <ElMenuItem
              v-for="item in group.items"
              :key="item.path"
              :index="item.path"
              :data-testid="`gateway-menu-${item.id}`"
            >
              <component :is="item.icon" :size="16" aria-hidden="true" />
              <span>{{ item.label }}</span>
            </ElMenuItem>
          </ElMenuItemGroup>
        </ElMenu>
      </ElAside>
      <ElMain class="admin-workspace__content">
        <ElCard class="content-shell" shadow="never">
          <template #header>
            <div class="admin-workspace__heading">
              <h2 class="admin-workspace__title">{{ activeSectionLabel }}</h2>
              <!-- 子页把刷新与主操作 Teleport 到这里，工具栏与内容卡顶对齐。 -->
              <div id="gateway-header-actions" class="admin-workspace__actions" />
            </div>
          </template>
          <RouterView />
        </ElCard>
      </ElMain>
    </ElContainer>
  </ElContainer>
</template>

<script setup lang="ts">
import { Activity, Boxes, ClipboardList, KeyRound, Route as RouteIcon, ScrollText, Server } from 'lucide-vue-next'
import { computed } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import { ElAside, ElCard, ElContainer, ElMain, ElMenu, ElMenuItem, ElMenuItemGroup } from 'element-plus'

const route = useRoute()

const menuGroups = [
  {
    id: 'monitor',
    label: '监控',
    items: [
      { id: 'overview', label: '运行总览', path: '/gateway/overview', icon: Activity },
      { id: 'logs', label: '调用日志', path: '/gateway/logs', icon: ScrollText },
      { id: 'audits', label: '审计', path: '/gateway/audits', icon: ClipboardList },
    ],
  },
  {
    id: 'config',
    label: '配置',
    items: [
      { id: 'nodes', label: '上游节点', path: '/gateway/nodes', icon: Server },
      { id: 'pools', label: '资源池', path: '/gateway/pools', icon: Boxes },
      { id: 'routes', label: '模型路由', path: '/gateway/routes', icon: RouteIcon },
      { id: 'keys', label: 'API Key', path: '/gateway/keys', icon: KeyRound },
    ],
  },
]
const menuItems = menuGroups.flatMap((group) => group.items)

const activeMenu = computed(() => {
  const match = menuItems.find((item) => route.path === item.path || route.path.startsWith(`${item.path}/`))
  return match ? match.path : '/gateway/overview'
})
const activeSectionLabel = computed(() => menuItems.find((item) => item.path === activeMenu.value)?.label ?? '运行总览')

</script>
