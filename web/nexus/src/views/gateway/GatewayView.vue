<template>
  <ElContainer class="gateway-page" data-testid="gateway-page">
    <ElContainer class="gateway-workspace">
      <ElAside class="gateway-aside">
        <div class="gateway-aside__title">API 网关</div>
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
        <ElCard class="content-shell gateway-shell" shadow="never">
          <template #header>
            <div class="gateway-shell__header">
              <span class="gateway-shell__title">{{ activeSectionLabel }}</span>
              <!-- 子页把刷新与主操作 Teleport 到这里，工具栏与内容卡顶对齐。 -->
              <div id="gateway-header-actions" class="gateway-header-actions" />
            </div>
          </template>
          <RouterView />
        </ElCard>
      </ElMain>
    </ElContainer>
  </ElContainer>
</template>

<script setup lang="ts">
import { Activity, Boxes, ClipboardList, KeyRound, ScrollText, Server } from 'lucide-vue-next'
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
const activeSectionLabel = computed(() => menuItems.find((item) => item.path === activeMenu.value)?.label ?? '运行总览')

</script>

<style scoped>
.gateway-page {
  display: flex; flex-direction: column; height: 100%; min-height: 480px;
  padding: 0; overflow: hidden; background: var(--astra-bg-base); --astra-text-muted: #3a524c;
}
.gateway-page > .gateway-workspace { flex: 1 1 auto; }
.gateway-header-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-left: auto; }
.gateway-workspace { align-items: stretch; }
.gateway-aside { --el-aside-width: 210px; border: 0; border-right: 1px solid var(--astra-border); border-radius: 12px 0 0 12px; background: transparent; overflow: auto; }
.gateway-aside__title { min-height: 60px; display: flex; align-items: center; padding: 0 18px; color: var(--astra-text-primary); font-size: var(--astra-font-control); font-weight: var(--astra-weight-bold); }
.gateway-menu { --el-menu-base-level-padding: 14px; --el-menu-item-height: 44px; border-right: 0; background: transparent; }
.gateway-menu :deep(.el-menu-item) { gap: 8px; }
.gateway-content { --el-main-padding: 0; display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
.gateway-content > .content-shell { flex: 1 1 auto; min-height: 0; }
.gateway-shell > .el-card__header { padding: 14px 16px; }
.gateway-shell__header { display: flex; align-items: center; gap: 12px; min-height: 32px; }
.gateway-shell__title { color: var(--astra-text-secondary); font-size: var(--astra-font-control); font-weight: var(--astra-weight-bold); }
@media (max-width: 860px) { .gateway-aside { --el-aside-width: 170px; } }
</style>
