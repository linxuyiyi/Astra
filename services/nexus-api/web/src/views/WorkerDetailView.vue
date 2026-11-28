<template>
  <section class="worker-detail-page" data-testid="worker-detail">
    <ElAlert v-if="refreshError" :title="refreshError" type="error" show-icon :closable="false">
      <ElButton size="small" @click="refresh">重试</ElButton>
    </ElAlert>
    <ElAlert v-if="actionError" :title="actionError" type="error" show-icon :closable="false" />

    <section
      v-if="!worker"
      v-loading="refreshing"
      class="worker-state-card"
      element-loading-text="正在加载 Worker"
    >
      <ElEmpty v-if="!refreshing" :image-size="64" description="未找到该 Worker，或已被删除">
        <ElButton type="primary" :icon="ArrowLeft" @click="router.push({ name: 'settings-workers' })">
          返回 Worker 列表
        </ElButton>
      </ElEmpty>
    </section>

    <template v-else>
      <!-- 顶部身份卡 -->
      <section class="worker-identity">
        <div class="worker-identity__main">
          <RouterLink class="worker-back" :to="{ name: 'settings-workers' }">
            <ArrowLeft :size="14" aria-hidden="true" />
            <span>Agent Worker</span>
          </RouterLink>

          <div class="worker-identity__title">
            <h2>{{ worker.name }}</h2>
            <SemanticTag :tone="semanticTone(statusTone(worker.status))" :label="statusLabel(worker.status)" />
            <ElTag size="small" type="info" class="astra-tag--neutral">
              {{ worker.mode === 'LINUX_REMOTE' ? 'Linux 远程' : '本机调试' }}
            </ElTag>
            <span class="worker-identity__id">
              <IdDisplay :value="worker.id" :max-width="22" />
            </span>
          </div>

          <p v-if="worker.description" class="worker-identity__desc">{{ worker.description }}</p>

          <div class="worker-identity__endpoint">
            <span class="worker-endpoint-item" :title="endpointHost">
              <Server :size="13" aria-hidden="true" />
              <span>{{ endpointHost }}</span>
            </span>
            <span class="worker-endpoint-sep" aria-hidden="true">·</span>
            <span class="worker-endpoint-item" :title="worker.workspace_root">
              <Folder :size="13" aria-hidden="true" />
              <span>{{ worker.workspace_root }}</span>
            </span>
          </div>
        </div>

        <div class="worker-identity__actions">
          <ElButton :icon="RefreshCw" :loading="refreshing" :disabled="acting" @click="refresh">刷新</ElButton>
          <ElButton type="primary" :icon="Pencil" :disabled="acting" @click="editing = true">编辑配置</ElButton>
          <ElDropdown trigger="click" :disabled="acting" @command="onWorkerCommand">
            <ElButton :loading="acting">更多</ElButton>
            <template #dropdown>
              <ElDropdownMenu>
                <ElDropdownItem command="toggle">
                  {{ worker.status === 'PAUSED' ? '恢复 Worker' : '暂停 Worker' }}
                </ElDropdownItem>
                <ElDropdownItem
                  command="delete"
                  divided
                  :disabled="acting || executionLocked || !health"
                  class="worker-dropdown-danger"
                >
                  删除 Worker
                </ElDropdownItem>
              </ElDropdownMenu>
            </template>
          </ElDropdown>
        </div>
      </section>

      <!-- 实时状态条 -->
      <section class="worker-status">
        <div class="worker-status__main">
          <span class="worker-status__lead">
            <i class="astra-service-dot" :data-state="connectionDotState" aria-hidden="true" />
            <span>{{ connectionLabel }}</span>
          </span>
          <ElTag
            size="small"
            :type="worker.enabled ? 'success' : 'info'"
            :class="{ 'astra-tag--neutral': !worker.enabled }"
          >
            调度{{ worker.enabled ? '已启用' : '已停用' }}
          </ElTag>
        </div>

        <div class="worker-status__facts">
          <span>环境 <b>{{ readinessLabel }}</b></span>
          <span>OpenCode <b class="worker-mono">{{ worker.opencode_port ? `:${worker.opencode_port}` : '待部署' }}</b></span>
          <span>版本 <b class="worker-mono">{{ worker.opencode_version || '—' }}</b></span>
          <span>占用任务 <b class="worker-mono">{{ worker.occupied_tasks.length }}</b></span>
          <span>最近检查 <b class="worker-mono">{{ formatTime(worker.last_checked_at) }}</b></span>
          <span v-if="lastUpdated">刷新 <b class="worker-mono">{{ lastUpdated }}</b></span>
        </div>
      </section>

      <!-- 配置与负载双卡 -->
      <div class="worker-info-grid">
        <section class="worker-card">
          <header class="worker-card__head">
            <h3>节点配置</h3>
            <span class="worker-chip worker-chip--mono">{{ worker.runtime_engine }}</span>
          </header>
          <dl class="worker-kv">
            <div class="worker-kv__item worker-kv__item--full">
              <dt>主机连接</dt>
              <dd class="worker-kv__inline">
                <span class="worker-mono-text">{{ endpointHost }}</span>
                <span
                  v-if="worker.mode === 'LINUX_REMOTE'"
                  class="worker-chip"
                  :class="worker.has_ssh_password ? '' : 'worker-chip--warn'"
                >
                  {{ worker.has_ssh_password ? 'SSH 凭据已保存' : '未配置 SSH 密码' }}
                </span>
              </dd>
            </div>
            <div class="worker-kv__item worker-kv__item--full">
              <dt>工作区根目录</dt>
              <dd><span class="worker-mono-text">{{ worker.workspace_root }}</span></dd>
            </div>
            <div class="worker-kv__item">
              <dt>OpenCode 端口</dt>
              <dd><span class="worker-mono-text">{{ worker.opencode_port || '待部署' }}</span></dd>
            </div>
            <div class="worker-kv__item">
              <dt>模型路由</dt>
              <dd>
                <span v-if="worker.model_name" class="worker-chip worker-chip--mono">{{ worker.model_name }}</span>
                <span v-else class="worker-muted">未配置</span>
              </dd>
            </div>
            <div class="worker-kv__item worker-kv__item--full">
              <dt>代码仓库</dt>
              <dd v-if="worker.repository_url" class="worker-kv__inline">
                <span class="worker-mono-text worker-truncate" :title="worker.repository_url">{{ worker.repository_url }}</span>
                <span v-if="worker.branch" class="worker-chip worker-chip--mono">
                  <GitBranch :size="11" aria-hidden="true" />
                  {{ worker.branch }}
                </span>
                <span v-if="worker.has_git_token" class="worker-chip">Token 已配置</span>
              </dd>
              <dd v-else><span class="worker-muted">未配置</span></dd>
            </div>
          </dl>
        </section>

        <section class="worker-card">
          <header class="worker-card__head">
            <h3>占用任务与能力</h3>
            <ElTag
              size="small"
              type="info"
              :class="{ 'astra-tag--neutral': !worker.occupied_tasks.length }"
            >
              {{ worker.occupied_tasks.length ? `占用 ${worker.occupied_tasks.length}` : '当前空闲' }}
            </ElTag>
          </header>

          <div class="worker-occupied-block">
            <span class="worker-field-label">当前占用任务</span>
            <WorkerOccupiedTasks :tasks="worker.occupied_tasks" />
          </div>

          <dl class="worker-kv worker-kv--spaced">
            <div class="worker-kv__item">
              <dt>任务类型</dt>
              <dd>
                <div v-if="worker.task_types.length" class="worker-chips">
                  <span v-for="type in worker.task_types" :key="type" class="worker-chip">
                    {{ taskTypeLabel(type) }}
                  </span>
                </div>
                <span v-else class="worker-muted">不限</span>
              </dd>
            </div>
            <div class="worker-kv__item">
              <dt>更新时间</dt>
              <dd><span class="worker-mono-text">{{ formatTime(worker.updated_at) }}</span></dd>
            </div>
            <div class="worker-kv__item worker-kv__item--full">
              <dt>绑定技能</dt>
              <dd>
                <div v-if="worker.skill_refs.length" class="worker-chips">
                  <span v-for="skill in worker.skill_refs" :key="skill" class="worker-chip worker-chip--mono">
                    {{ skill }}
                  </span>
                </div>
                <span v-else class="worker-muted">未绑定</span>
              </dd>
            </div>
            <div class="worker-kv__item worker-kv__item--full">
              <dt>绑定 MCP</dt>
              <dd>
                <div v-if="worker.mcp_refs.length" class="worker-chips">
                  <span
                    v-for="mcp in worker.mcp_refs"
                    :key="mcp.name"
                    class="worker-chip worker-chip--mono"
                    :title="mcp.url"
                  >
                    {{ mcp.name }}
                  </span>
                </div>
                <span v-else class="worker-muted">未绑定</span>
              </dd>
            </div>
          </dl>
        </section>
      </div>

      <!-- 运行环境与运维操作 -->
      <WorkerMaintenancePanel :key="worker.id" :worker="worker" :health="health" />
    </template>

    <el-drawer
      v-if="worker"
      v-model="editing"
      title="编辑 Worker 配置"
      size="var(--astra-drawer-md)"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <WorkerConfig :worker="worker" :execution-locked="executionLocked" @saved="configurationSaved" />
    </el-drawer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  ElAlert,
  ElButton,
  ElDropdown,
  ElDropdownItem,
  ElDropdownMenu,
  ElEmpty,
  ElMessageBox,
  ElTag,
  vLoading,
} from 'element-plus'
import { ArrowLeft, Folder, GitBranch, Pencil, RefreshCw, Server } from 'lucide-vue-next'
import { api } from '@/api/client'
import { getWorkerHealth, type WorkerHealth } from '@/api/maintenance'
import type { Worker } from '@/api/types'
import { formatTime, statusLabel, statusTone, taskTypeLabel } from '@/utils/format'
import { semanticTone } from '@/features/workers/semantic'
import IdDisplay from '@/components/astra/IdDisplay.vue'
import SemanticTag from '@/components/astra/SemanticTag.vue'
import WorkerConfig from './settings/WorkerConfig.vue'
import WorkerMaintenancePanel from '@/components/worker/WorkerMaintenancePanel.vue'
import WorkerOccupiedTasks from '@/components/worker/WorkerOccupiedTasks.vue'

const route = useRoute()
const router = useRouter()
const id = computed(() => String(route.params.id))
const worker = ref<Worker>()
const health = ref<WorkerHealth>()
const refreshing = ref(false)
const editing = ref(false)
const acting = ref(false)
const refreshError = ref('')
const actionError = ref('')
const lastUpdated = ref('')

const executionLocked = computed(() =>
  Boolean(
    worker.value?.occupied_tasks.length ||
      health.value?.blocking.some((item) => ['maintenance_active', 'occupied_task', 'review_active'].includes(item.code)),
  ),
)

const connectionDotState = computed(() => {
  if (!worker.value || worker.value.online === null) return 'unknown'
  return worker.value.online ? 'live' : 'down'
})

const connectionLabel = computed(() => {
  if (!worker.value || worker.value.online === null) return '连接状态未知'
  return worker.value.online ? '连接在线' : '连接离线'
})

const readinessLabel = computed(() => {
  const map: Record<WorkerHealth['readiness'], string> = {
    READY: '就绪',
    DRIFTED: '待同步',
    DEGRADED: '异常',
    UNKNOWN: '未检查',
  }
  return map[health.value?.readiness ?? 'UNKNOWN']
})

const endpointHost = computed(() => {
  const current = worker.value
  if (!current) return '—'
  if (current.mode === 'LOCAL') return '本机进程'
  if (!current.host) return '未配置主机'
  const userPrefix = current.username ? `${current.username}@` : ''
  const portSuffix = current.port && current.port !== 22 ? `:${current.port}` : ''
  return `${userPrefix}${current.host}${portSuffix}`
})

let request: AbortController | undefined
let timer: ReturnType<typeof setInterval> | undefined

async function refresh() {
  if (request || acting.value) return
  const controller = new AbortController()
  const workerId = id.value
  request = controller
  refreshing.value = true
  const [record, diagnostics] = await Promise.allSettled([
    api.worker(workerId, controller.signal),
    getWorkerHealth(workerId, controller.signal),
  ])
  if (controller.signal.aborted) return
  if (record.status === 'fulfilled') worker.value = record.value
  if (diagnostics.status === 'fulfilled') health.value = diagnostics.value
  const failures = [
    record.status === 'rejected' ? `Worker 状态：${record.reason}` : '',
    diagnostics.status === 'rejected' ? `运行环境：${diagnostics.reason}` : '',
  ].filter(Boolean)
  refreshError.value = failures.length ? `刷新失败：${failures.join('；')}` : ''
  if (!failures.length) lastUpdated.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  request = undefined
  refreshing.value = false
}

function stopRefresh() {
  request?.abort()
  request = undefined
  refreshing.value = false
}

function configurationSaved(updated: Worker) {
  editing.value = false
  if (id.value !== updated.id) return
  stopRefresh()
  worker.value = updated
  void refresh()
}

function onWorkerCommand(command: string | number) {
  const name = String(command)
  if (name === 'toggle') void toggle()
  else if (name === 'delete') void remove()
}

async function toggle() {
  if (!worker.value || acting.value) return
  const workerId = id.value
  const action = worker.value.status === 'PAUSED' ? 'resume' : 'pause'
  stopRefresh()
  acting.value = true
  actionError.value = ''
  try {
    const updated = await api.workerAction(workerId, action)
    if (id.value === workerId) worker.value = updated
  } catch (reason) {
    if (id.value === workerId) actionError.value = String(reason)
  } finally {
    if (id.value === workerId) {
      acting.value = false
      void refresh()
    }
  }
}

async function remove() {
  if (acting.value || executionLocked.value) return
  const workerId = id.value
  const workerName = worker.value?.name || workerId
  acting.value = true
  actionError.value = ''
  try {
    await ElMessageBox.confirm(`确认删除 Worker「${workerName}」？此操作不可撤销。`, '删除 Worker', {
      type: 'warning',
      confirmButtonText: '删除',
      confirmButtonClass: 'el-button--danger',
    })
    if (id.value !== workerId) return
    await api.deleteWorker(workerId)
    if (id.value === workerId) await router.push({ name: 'settings-workers' })
  } catch (reason) {
    if (id.value === workerId && reason !== 'cancel' && reason !== 'close') actionError.value = String(reason)
  } finally {
    if (id.value === workerId) acting.value = false
  }
}

function refreshWhenVisible() {
  if (!document.hidden) void refresh()
}

watch(
  id,
  () => {
    stopRefresh()
    worker.value = health.value = undefined
    refreshError.value = actionError.value = lastUpdated.value = ''
    editing.value = acting.value = false
    void refresh()
  },
  { immediate: true },
)

onMounted(() => {
  timer = setInterval(refreshWhenVisible, 10_000)
  document.addEventListener('visibilitychange', refreshWhenVisible)
})

onUnmounted(() => {
  clearInterval(timer)
  document.removeEventListener('visibilitychange', refreshWhenVisible)
  stopRefresh()
})
</script>

<style scoped>
/* 统一由配置中心壳 .content-shell 滚动，避免嵌套双滚动条 */
.worker-detail-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.worker-state-card {
  display: grid;
  place-items: center;
  min-height: 240px;
  padding: 24px;
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-panel);
}

/* ── 顶部身份卡（对齐 GatewayNodeDetailView） ─────────────────────── */
.worker-identity {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 16px;
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-panel);
}

.worker-identity__main {
  display: grid;
  gap: 6px;
  min-width: 0;
}

.worker-back {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  width: fit-content;
  color: var(--astra-text-muted);
  font-size: var(--astra-font-helper);
  text-decoration: none;
  transition: color 0.15s ease;
}

.worker-back:hover {
  color: var(--astra-text-primary);
}

.worker-identity__title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 10px;
  min-width: 0;
}

.worker-identity__title h2 {
  margin: 0;
  color: var(--astra-text-primary);
  font-size: 18px;
  font-weight: 600;
  line-height: 26px;
}

.worker-identity__id {
  display: inline-flex;
  align-items: center;
  padding: 1px 6px;
  border: 1px solid var(--astra-border-subtle);
  border-radius: 6px;
  background: var(--astra-bg-subtle);
}

.worker-identity__id :deep(.id-display) {
  font-family: var(--astra-font-mono);
  font-size: 12px;
}

.worker-identity__id :deep(.id-display__value) {
  color: var(--astra-text-muted);
}

.worker-identity__desc {
  margin: 0;
  color: var(--astra-text-secondary);
  font-size: 13px;
  line-height: 1.5;
}

.worker-identity__endpoint {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
  color: var(--astra-text-muted);
  font-family: var(--astra-font-mono);
  font-size: 12px;
  line-height: 18px;
}

.worker-endpoint-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  max-width: 100%;
  overflow-wrap: anywhere;
}

.worker-endpoint-sep {
  color: var(--astra-border-hover);
}

.worker-identity__actions {
  display: flex;
  flex: none;
  align-items: center;
  gap: 8px;
}

.worker-identity__actions .el-button + .el-button {
  margin-left: 0;
}

/* ── 实时状态条 ──────────────────────────────────────────────────── */
.worker-status {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
  padding: 12px 16px;
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-panel);
}

.worker-status__main {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 10px;
}

.worker-status__lead {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--astra-text-primary);
  font-size: 13.5px;
  font-weight: 600;
}

.worker-status__facts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-left: auto;
  color: var(--astra-text-muted);
  font-size: 12px;
}

.worker-status__facts b {
  margin-left: 4px;
  color: var(--astra-text-primary);
  font-weight: 600;
}

.worker-mono {
  font-family: var(--astra-font-mono);
  font-variant-numeric: tabular-nums;
}

/* ── 配置与负载双卡 ──────────────────────────────────────────────── */
.worker-info-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  align-items: stretch;
}

.worker-card {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-panel);
  overflow: hidden;
}

.worker-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--astra-border-subtle);
}

.worker-card__head h3 {
  margin: 0;
  color: var(--astra-text-primary);
  font-size: 14px;
  font-weight: 600;
  line-height: 20px;
}

.worker-occupied-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--astra-border-subtle);
  background: var(--astra-bg-subtle);
}

.worker-field-label {
  color: var(--astra-text-muted);
  font-size: 12px;
  font-weight: 500;
}

.worker-kv {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px 16px;
  margin: 0;
  padding: 14px 16px;
}

.worker-kv--spaced {
  padding-top: 14px;
}

.worker-kv__item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.worker-kv__item--full {
  grid-column: 1 / -1;
}

.worker-kv__item dt {
  color: var(--astra-text-muted);
  font-size: 12px;
  font-weight: 500;
  line-height: 16px;
}

.worker-kv__item dd {
  margin: 0;
  min-width: 0;
  color: var(--astra-text-primary);
  font-size: 13px;
  line-height: 20px;
}

.worker-kv__inline {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
}

.worker-mono-text {
  color: var(--astra-text-primary);
  font-family: var(--astra-font-mono);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}

.worker-truncate {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.worker-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.worker-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 8px;
  border: 1px solid var(--astra-border);
  border-radius: 6px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-size: 12px;
  line-height: 20px;
}

.worker-chip--mono {
  font-family: var(--astra-font-mono);
}

.worker-chip--warn {
  border-color: color-mix(in srgb, var(--astra-warning) 28%, transparent);
  background: var(--astra-warning-soft);
  color: var(--astra-warning);
}

.worker-muted {
  color: var(--astra-text-muted);
  font-size: 13px;
}

:global(.worker-dropdown-danger:not(.is-disabled)) {
  color: var(--astra-danger) !important;
}

@media (min-width: 2240px) {
  .worker-info-grid {
    gap: 20px;
  }
  .worker-kv {
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 16px 20px;
    padding: 16px 20px;
  }
}

@media (max-width: 1120px) {
  .worker-info-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 900px) {
  .worker-identity {
    flex-wrap: wrap;
  }
  .worker-status__facts {
    margin-left: 0;
  }
}
</style>
