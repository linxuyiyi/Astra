<template>
  <ElTable
    ref="tableRef"
    v-loading="loading"
    class="gateway-node-table"
    element-loading-text="正在加载节点"
    :data="nodes"
    row-key="id"
    height="100%"
    data-testid="gateway-node-table"
  >
    <ElTableColumn label="节点名称 / 地址" min-width="160">
      <template #default="{ row }">
        <RouterLink class="node-cell" :to="`/gateway/nodes/${row.id}`">
          <strong :title="row.name">{{ row.name }}</strong>
          <span class="node-cell__url mono" :title="row.base_url">{{ row.base_url }}</span>
        </RouterLink>
      </template>
    </ElTableColumn>
    <ElTableColumn label="类型" width="92">
      <template #default="{ row }">
        <ElTag size="small" type="info" class="astra-tag--neutral">{{ probeStrategyLabel(row.probe_strategy) }}</ElTag>
      </template>
    </ElTableColumn>
    <ElTableColumn label="资源池" width="96" show-overflow-tooltip>
      <template #default="{ row }">
        <span v-if="row.pool_id" class="node-pool-name">{{ poolName(row.pool_id) }}</span>
        <span v-else class="node-cell__empty">未分配</span>
      </template>
    </ElTableColumn>
    <ElTableColumn label="支持模型" min-width="150">
      <template #default="{ row }">
        <div v-if="row.models.length" class="node-models">
          <ElTag
            v-for="model in row.models.slice(0, 3)"
            :key="model"
            size="small"
            type="info"
            class="astra-tag--neutral node-model-tag mono"
            :title="model"
          >
            {{ model }}
          </ElTag>
          <ElTooltip v-if="row.models.length > 3" placement="top" :content="row.models.join('、')">
            <ElTag size="small" type="info" class="astra-tag--neutral node-model-more">+{{ row.models.length - 3 }}</ElTag>
          </ElTooltip>
        </div>
        <span v-else class="node-cell__empty">未配置</span>
      </template>
    </ElTableColumn>
    <ElTableColumn label="运行状态" width="144">
      <template #default="{ row }">
        <div class="node-states">
          <ElTag
            size="small"
            :type="tagType(healthTone(row.health_state))"
            :class="{ 'astra-tag--neutral': healthTone(row.health_state) === 'neutral' }"
          >
            {{ healthStateLabel(row.health_state) }}
          </ElTag>
          <ElTag
            size="small"
            :type="tagType(schedulingTone(row.scheduling_state))"
            :class="{ 'astra-tag--neutral': schedulingTone(row.scheduling_state) === 'neutral' }"
          >
            {{ schedulingStateLabel(row.scheduling_state) }}
          </ElTag>
        </div>
      </template>
    </ElTableColumn>
    <ElTableColumn label="权重 / 并发" width="108" align="center">
      <template #default="{ row }">
        <span class="node-weight" :title="`${row.weight} / ${row.max_concurrency || '不限'}`">{{ row.weight }} / {{ row.max_concurrency || '不限' }}</span>
      </template>
    </ElTableColumn>
    <ElTableColumn label="启用" width="58" align="center">
      <template #default="{ row }">
        <ElSwitch
          size="small"
          :model-value="row.config_state === 'ENABLED'"
          :disabled="busyId === row.id"
          :aria-label="row.config_state === 'ENABLED' ? `禁用 ${row.name}` : `启用 ${row.name}`"
          @change="$emit('toggle-config', row as GatewayNode)"
        />
      </template>
    </ElTableColumn>
    <ElTableColumn label="操作" width="172" align="center" fixed="right">
      <template #default="{ row }">
        <div class="admin-actions">
          <ElButton link type="primary" :disabled="busyId === row.id" @click="$emit('detail', row as GatewayNode)">详情</ElButton>
          <ElButton link type="primary" :disabled="busyId === row.id" @click="$emit('edit', row as GatewayNode)">编辑</ElButton>
          <ElButton link type="primary" :disabled="busyId === row.id" @click="$emit('probe', row as GatewayNode)">探测</ElButton>
          <ElDropdown trigger="click" :disabled="busyId === row.id" @command="(command: string) => onNodeCommand(row as GatewayNode, command)">
            <ElButton link type="primary" :disabled="busyId === row.id">更多</ElButton>
            <template #dropdown>
              <ElDropdownMenu>
                <ElDropdownItem :command="row.scheduling_state === 'ACTIVE' ? 'drain' : 'restore'">
                  {{ row.scheduling_state === 'ACTIVE' ? '安全下线' : '恢复调度' }}
                </ElDropdownItem>
                <ElDropdownItem command="delete" divided>删除节点</ElDropdownItem>
              </ElDropdownMenu>
            </template>
          </ElDropdown>
        </div>
      </template>
    </ElTableColumn>
    <template #empty>
      <ElEmpty v-if="!loading" :image-size="64" :description="emptyText" />
    </template>
  </ElTable>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink } from 'vue-router'
import { ElButton, ElDropdown, ElDropdownItem, ElDropdownMenu, ElEmpty, ElSwitch, ElTable, ElTableColumn, ElTag, ElTooltip, vLoading } from 'element-plus'
import type { TableInstance } from 'element-plus'
import type { GatewayNode, GatewayPool } from '@/api/gateway'
import {
  healthStateLabel,
  healthTone,
  probeStrategyLabel,
  schedulingStateLabel,
  schedulingTone,
  type GatewayTone,
} from '@/utils/gateway'

const props = defineProps<{
  nodes: GatewayNode[]
  pools: GatewayPool[]
  busyId?: string
  loading?: boolean
  emptyText?: string
}>()
const emit = defineEmits<{
  detail: [node: GatewayNode]
  probe: [node: GatewayNode]
  drain: [node: GatewayNode]
  restore: [node: GatewayNode]
  'toggle-config': [node: GatewayNode]
  edit: [node: GatewayNode]
  delete: [node: GatewayNode]
}>()

function poolName(poolId: string | null): string {
  if (!poolId) return '未分配'
  return props.pools.find((pool) => pool.id === poolId)?.name ?? poolId
}

function onNodeCommand(row: GatewayNode, command: string) {
  if (command === 'drain') emit('drain', row)
  else if (command === 'restore') emit('restore', row)
  else if (command === 'delete') emit('delete', row)
}

function tagType(tone: GatewayTone): 'success' | 'warning' | 'danger' | 'info' {
  if (tone === 'success') return 'success'
  if (tone === 'warning') return 'warning'
  if (tone === 'danger') return 'danger'
  return 'info'
}

const tableRef = ref<TableInstance>()
defineExpose({
  scrollToTop: () => tableRef.value?.setScrollTop(0),
})
</script>

<style scoped>
.node-cell { display: grid; gap: 2px; min-width: 0; text-decoration: none; }
.node-cell strong {
  overflow: hidden;
  color: var(--astra-text-primary);
  font-size: var(--astra-font-label);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.node-cell:hover strong { color: var(--astra-accent-text); }
.node-cell__url { color: var(--astra-text-muted); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.node-cell__url.mono { font-family: var(--astra-font-mono); }
.node-cell__empty { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.node-pool-name { color: var(--astra-text-secondary); font-size: var(--astra-font-helper); }
.node-models { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; min-width: 0; }
.node-model-tag { max-width: 152px; font-size: 11px; padding: 0 6px; height: 22px; line-height: 20px; }
.node-model-tag :deep(.el-tag__content) { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.node-model-more { font-size: 11px; padding: 0 6px; height: 22px; line-height: 20px; cursor: default; }
.node-states { display: flex; align-items: center; gap: 5px; flex-wrap: nowrap; }
.gateway-node-table :deep(.admin-actions) { flex-wrap: nowrap; white-space: nowrap; }
.node-weight { color: var(--astra-text-secondary); font-size: var(--astra-font-helper); font-variant-numeric: tabular-nums; white-space: nowrap; }
</style>
