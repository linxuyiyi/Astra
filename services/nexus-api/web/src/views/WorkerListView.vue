<template>
  <section class="astra-page" data-testid="worker-list">
    <SettingsHeaderActions>
      <el-button :icon="RefreshCw" :loading="loading" @click="load">刷新</el-button>
      <el-button type="primary" :icon="Plus" @click="creating = true">新增 Worker</el-button>
    </SettingsHeaderActions>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
      <el-button @click="load">重试</el-button>
    </el-alert>
    <form class="admin-page__toolbar" aria-label="Worker 筛选" @submit.prevent="applyFilters">
      <el-select v-model="filter" class="admin-page__select" aria-label="Worker 状态" clearable placeholder="状态：全部" @change="applyFilters">
        <el-option v-for="state in statuses" :key="state" :value="state" :label="statusLabel(state)" />
      </el-select>
      <div class="admin-page__search">
        <el-input v-model="search" class="admin-page__keyword" :prefix-icon="Search" aria-label="Worker 搜索" placeholder="搜索名称或主机" clearable @clear="applyFilters" />
        <el-button type="primary" native-type="submit">搜索</el-button>
        <el-button @click="resetFilters">重置</el-button>
      </div>
    </form>
    <div class="astra-table-panel astra-table-panel--clickable">
      <el-table v-loading="loading" :data="workers" row-key="id" height="100%" aria-label="Worker 列表" @row-click="openWorker">
        <el-table-column prop="name" label="Worker" min-width="150" show-overflow-tooltip />
        <el-table-column label="状态" width="104">
          <template #default="{ row }"><el-tag :type="statusTagType(row.status)">{{ statusLabel(row.status) }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="host" label="主机" min-width="144" show-overflow-tooltip />
        <el-table-column prop="workspace_root" label="工作区" min-width="180" show-overflow-tooltip />
        <el-table-column label="最后心跳" width="168">
          <template #default="{ row }">{{ formatTime(row.last_heartbeat_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="96" align="right" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" :aria-label="`查看 ${row.name}`" @click.stop="openWorker(row)">查看</el-button>
          </template>
        </el-table-column>
        <template #empty><el-empty v-if="!loading" :image-size="72" description="暂无匹配 Worker" /></template>
      </el-table>
    </div>
    <div class="admin-page__footer">
      <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[10, 20, 50]" :total="total" layout="total, sizes, prev, pager, next" aria-label="Worker 分页" />
    </div>
    <el-dialog v-model="creating" title="创建 Worker" width="760px" destroy-on-close>
      <WorkerConfig @saved="configurationSaved" />
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Plus, RefreshCw, Search } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { Worker } from '@/api/types'
import { statusLabel, statusTagType, formatTime } from '@/utils/format'
import SettingsHeaderActions from './settings/SettingsHeaderActions.vue'
import WorkerConfig from './settings/WorkerConfig.vue'

const router = useRouter()
const workers = ref<Worker[]>([])
const creating = ref(false)
const loading = ref(false)
const error = ref('')
const filter = ref('')
const search = ref('')
const keyword = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const statuses = ['OFFLINE', 'IDLE', 'WORKING', 'PAUSED']
let request: AbortController | undefined

function configurationSaved() {
  creating.value = false
  void load()
}
async function load() {
  request?.abort()
  const controller = new AbortController()
  request = controller
  loading.value = true
  error.value = ''
  const query = new URLSearchParams({
    limit: String(pageSize.value),
    offset: String((page.value - 1) * pageSize.value),
    q: keyword.value,
  })
  if (filter.value) query.set('status', filter.value)
  try {
    const result = await api.workerPage(query, controller.signal)
    if (request !== controller) return
    workers.value = result.items
    total.value = result.total
  } catch (reason) {
    if (!controller.signal.aborted) error.value = String(reason)
  } finally {
    if (request === controller) loading.value = false
  }
}
function applyFilters() {
  keyword.value = search.value.trim()
  if (page.value === 1) void load()
  else page.value = 1
}
function resetFilters() {
  filter.value = ''
  search.value = ''
  applyFilters()
}
function openWorker(row: Worker) {
  void router.push({ name: 'settings-worker-detail', params: { id: row.id } })
}
watch([page, pageSize], load)
onMounted(load)
onUnmounted(() => request?.abort())
</script>
