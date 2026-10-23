<template>
  <ElTable
    v-loading="loading"
    class="gateway-node-table"
    element-loading-text="正在加载节点"
    :data="nodes"
    row-key="id"
    :max-height="TABLE_MAX_HEIGHT"
    data-testid="gateway-node-table"
  >
    <ElTableColumn label="节点名称 / 地址" min-width="200">
      <template #default="{ row }">
        <RouterLink class="node-cell" :to="`/gateway/nodes/${row.id}`">
          <strong>{{ row.name }}</strong>
          <span class="node-cell__url mono">{{ row.base_url }}</span>
        </RouterLink>
      </template>
    </ElTableColumn>
    <ElTableColumn label="所属资源池" width="130">
      <template #default="{ row }">
        <ElTag v-if="row.pool_id" size="small" type="info">{{ poolName(row.pool_id) }}</ElTag>
        <span v-else class="node-cell__empty">未分配</span>
      </template>
    </ElTableColumn>
    <ElTableColumn label="支持模型" min-width="180">
      <template #default="{ row }">
        <div class="node-models">
          <ElTag v-for="model in row.models" :key="model" size="small" effect="plain" class="node-model-tag">
            {{ model }}
          </ElTag>
          <span v-if="!row.models.length" class="node-cell__empty">未配置</span>
        </div>
      </template>
    </ElTableColumn>
    <ElTableColumn label="健康状态" width="95" align="center">
      <template #default="{ row }">
        <ElTag size="small" :type="tagType(healthTone(row.health_state))">
          {{ healthStateLabel(row.health_state) }}
        </ElTag>
      </template>
    </ElTableColumn>
    <ElTableColumn label="调度状态" width="105" align="center">
      <template #default="{ row }">
        <ElTag size="small" :type="tagType(schedulingTone(row.scheduling_state))">
          {{ schedulingStateLabel(row.scheduling_state) }}
        </ElTag>
      </template>
    </ElTableColumn>
    <ElTableColumn label="配置" width="85" align="center">
      <template #default="{ row }">
        <ElTag size="small" :type="row.config_state === 'ENABLED' ? 'success' : 'info'">
          {{ configStateLabel(row.config_state) }}
        </ElTag>
      </template>
    </ElTableColumn>
    <ElTableColumn label="操作" width="200" align="center" fixed="right">
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
                <ElDropdownItem command="toggle-config">{{ row.config_state === 'ENABLED' ? '禁用' : '启用' }}</ElDropdownItem>
                <ElDropdownItem command="delete" divided>删除</ElDropdownItem>
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
import { RouterLink } from 'vue-router'
import { ElButton, ElDropdown, ElDropdownItem, ElDropdownMenu, ElEmpty, ElTable, ElTableColumn, ElTag, vLoading } from 'element-plus'
import type { GatewayNode, GatewayPool } from '@/api/gateway'
import { TABLE_MAX_HEIGHT } from '@/utils/table'
import {
  configStateLabel,
  healthStateLabel,
  healthTone,
  schedulingStateLabel,
  schedulingTone,
  serveSentence,
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
  else if (command === 'toggle-config') emit('toggle-config', row)
  else if (command === 'delete') emit('delete', row)
}

function tagType(tone: GatewayTone): 'success' | 'warning' | 'danger' | 'info' {
  if (tone === 'success') return 'success'
  if (tone === 'warning') return 'warning'
  if (tone === 'danger') return 'danger'
  return 'info'
}
</script>

<style scoped>
.node-cell { display: grid; gap: 2px; min-width: 0; text-decoration: none; }
.node-cell strong { color: var(--astra-text-primary); font-size: var(--astra-font-label); }
.node-cell__url { color: var(--astra-text-muted); font-size: 11px; }
.node-cell__url.mono { font-family: var(--astra-font-mono, monospace); }
.node-cell__empty { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.node-models { display: flex; flex-wrap: wrap; gap: 4px; }
.node-model-tag { font-size: 11px; padding: 0 6px; }
</style>
