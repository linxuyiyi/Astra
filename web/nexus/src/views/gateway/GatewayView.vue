<template>
  <ElContainer class="gateway-page" data-testid="gateway-page">
    <AstraPageHeader title="API 网关" testid="gateway-header">
      <template #actions>
        <!-- 子页把自己的刷新与主操作 Teleport 到这里，全站按钮位置保持一致。 -->
        <div id="gateway-header-actions" class="gateway-header-actions" />
      </template>
    </AstraPageHeader>

    <ElContainer class="gateway-workspace">
      <ElAside class="gateway-aside">
        <ElMenu class="gateway-menu" mode="vertical" :default-active="activeMenu" router>
          <ElMenuItemGroup v-for="group in menuGroups" :key="group.id" :title="group.label">
            <ElMenuItem
              v-for="item in group.items"
              :key="item.path"
              :index="item.path"
              :data-testid="`gateway-menu-${item.id}`"
            >
              <component :is="item.icon" :size="16" />
              <span>{{ item.label }}</span>
            </ElMenuItem>
          </ElMenuItemGroup>
        </ElMenu>
      </ElAside>
      <ElMain class="gateway-content">
        <RouterView />
      </ElMain>
    </ElContainer>
  </ElContainer>
</template>

<script setup lang="ts">
import { Activity, Boxes, ClipboardList, KeyRound, ScrollText, Server } from 'lucide-vue-next'
import { computed } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import { ElAside, ElContainer, ElMain, ElMenu, ElMenuItem, ElMenuItemGroup } from 'element-plus'
import AstraPageHeader from '@/components/astra/AstraPageHeader.vue'

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
      { id: 'routes', label: '资源池与路由', path: '/gateway/routes', icon: Boxes },
      { id: 'keys', label: 'API Key', path: '/gateway/keys', icon: KeyRound },
    ],
  },
]
const menuItems = menuGroups.flatMap((group) => group.items)

const activeMenu = computed(() => {
  const match = menuItems.find((item) => route.path === item.path || route.path.startsWith(`${item.path}/`))
  return match ? match.path : '/gateway/overview'
})

</script>

<style scoped>
.gateway-page {
  display: flex; flex-direction: column; gap: 14px; height: calc(100vh - 176px); min-height: 480px;
  padding: 0; overflow: hidden; background: var(--astra-bg-base); --astra-text-muted: #3a524c;
}
.gateway-page > .gateway-workspace { flex: 1 1 auto; }
.gateway-header-actions { display: flex; align-items: center; gap: 8px; }
.gateway-workspace { align-items: stretch; }
.gateway-aside { --el-aside-width: 210px; border: 1px solid var(--astra-border); border-radius: 12px; background: var(--astra-bg-panel); }
.gateway-menu { --el-menu-base-level-padding: 14px; --el-menu-item-height: 44px; border-right: 0; background: transparent; }
.gateway-menu :deep(.el-menu-item) { gap: 8px; }
.gateway-content { --el-main-padding: 0; display: flex; flex-direction: column; min-width: 0; overflow: auto; scrollbar-gutter: stable; }
.gateway-content > * { flex: 1 1 auto; min-height: 0; }
@media (max-width: 860px) { .gateway-aside { --el-aside-width: 170px; } }
</style>
