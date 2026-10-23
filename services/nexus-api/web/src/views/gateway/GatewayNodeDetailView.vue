<template>
  <section class="gateway-node-page" data-testid="gateway-node-detail">
    <ElAlert
      v-if="error"
      type="error"
      show-icon
      :closable="false"
      :title="error"
      data-testid="gateway-node-error"
    >
      <ElButton type="primary" data-testid="gateway-node-retry" @click="load">重试</ElButton>
    </ElAlert>

    <ElCard
      v-if="!node"
      v-loading="loading"
      class="gateway-node-state"
      element-loading-text="正在加载节点"
      data-testid="gateway-node-state"
    >
      <ElEmpty v-if="!loading" :image-size="72" description="未找到该节点，或它已被删除。">
        <ElButton type="primary" :icon="ArrowLeft" data-testid="gateway-node-back-empty" @click="backToList">返回节点列表</ElButton>
      </ElEmpty>
    </ElCard>

    <template v-else>
      <section class="gateway-node-identity" data-testid="gateway-node-header">
        <div class="gateway-node-identity__main">
          <RouterLink class="gateway-node-back" to="/gateway/nodes"><ArrowLeft :size="14" />节点列表</RouterLink>
          <div class="gateway-node-identity__title">
            <h2>{{ node.name }}</h2>
            <span class="gateway-node-mono">{{ node.id }}</span>
          </div>
          <p class="gateway-node-identity__url">{{ node.base_url }}</p>
        </div>
        <ElSpace class="gateway-node-actions" :size="8">
          <ElButton type="primary" :icon="Pencil" data-testid="gateway-node-edit" @click="openEditor">编辑</ElButton>
          <ElDropdown trigger="click" :disabled="working" @command="onNodeCommand">
            <ElButton :loading="working || loading">更多</ElButton>
            <template #dropdown>
              <ElDropdownMenu>
                <ElDropdownItem command="refresh" data-testid="gateway-node-refresh">刷新</ElDropdownItem>
                <ElDropdownItem command="probe" data-testid="gateway-node-probe">探测</ElDropdownItem>
                <ElDropdownItem v-if="node.scheduling_state === 'ACTIVE'" command="drain" data-testid="gateway-node-drain">安全下线</ElDropdownItem>
                <ElDropdownItem v-else command="restore" data-testid="gateway-node-restore">恢复调度</ElDropdownItem>
                <ElDropdownItem command="toggle" data-testid="gateway-node-toggle">{{ node.config_state === 'ENABLED' ? '禁用' : '启用' }}</ElDropdownItem>
              </ElDropdownMenu>
            </template>
          </ElDropdown>
        </ElSpace>
      </section>

      <section class="gateway-node-status" :data-status="serveTone" data-testid="gateway-node-summary">
        <div class="gateway-node-status__main">
          <span class="gateway-node-status__pill">{{ serveSummary.title }}</span>
          <p class="gateway-node-status__detail">{{ serveSummary.detail }}</p>
        </div>
        <div class="gateway-node-status__facts">
          <span>健康 <b>{{ healthStateLabel(node.health_state) }}</b></span>
          <span>调度 <b>{{ schedulingStateLabel(node.scheduling_state) }}</b></span>
          <span>配置 <b>{{ configStateLabel(node.config_state) }}</b></span>
          <span>资源池 <b>{{ poolName(node.pool_id) }}</b></span>
          <span>最近探测 <b>{{ formatRelativeTime(node.last_checked_at) }}</b></span>
        </div>
      </section>

      <div class="gateway-node-tabs">
        <ElRadioGroup v-model="detailView" aria-label="节点内容" data-testid="gateway-node-nav">
          <ElRadioButton value="summary">概况</ElRadioButton>
          <ElRadioButton value="usage">用量</ElRadioButton>
          <ElRadioButton value="ops">运维</ElRadioButton>
        </ElRadioGroup>
        <div v-if="detailView === 'usage'" class="gateway-node-tabs__tools">
          <span class="gateway-node-tabs__label">统计窗口</span>
          <ElRadioGroup v-model="statsWindow" aria-label="统计窗口" data-testid="gateway-node-window">
            <ElRadioButton v-for="item in windows" :key="item.id" :value="item.id">{{ item.label }}</ElRadioButton>
          </ElRadioGroup>
        </div>
      </div>

      <div class="gateway-node-content" data-testid="gateway-node-content">
    <!-- 概览 -->
    <section v-show="showSection('overview')" id="overview" class="gateway-node-section" data-testid="gateway-node-section-overview">
      <div class="gateway-node-grid">
        <ElCard shadow="never" data-testid="gateway-node-info">
          <template #header>
            <div class="gateway-node-card-head">
              <div><h4>节点信息</h4></div>
            </div>
          </template>
          <ElDescriptions :column="2">
            <ElDescriptionsItem label="上游地址">{{ node.base_url }}</ElDescriptionsItem>
            <ElDescriptionsItem label="资源池">{{ poolName(node.pool_id) }}</ElDescriptionsItem>
            <ElDescriptionsItem label="认证方式">{{ authTypeLabel(node.auth_type) }} · {{ node.auth_header }}</ElDescriptionsItem>
            <ElDescriptionsItem label="凭据">{{ node.has_api_key ? '已配置（不回显）' : '未配置' }}</ElDescriptionsItem>
            <ElDescriptionsItem label="健康路径">{{ node.health_path || '继承默认' }}</ElDescriptionsItem>
            <ElDescriptionsItem label="支持模型">{{ node.models.length ? node.models.join('、') : '未配置' }}</ElDescriptionsItem>
            <ElDescriptionsItem label="最近探测">{{ formatDateTime(node.last_checked_at) }}</ElDescriptionsItem>
            <ElDescriptionsItem label="探测间隔">{{ node.probe_interval_seconds ?? '继承默认' }} 秒</ElDescriptionsItem>
          </ElDescriptions>
        </ElCard>

        <ElCard shadow="never" data-testid="gateway-node-probe-latest">
          <template #header>
            <div class="gateway-node-card-head">
              <div><h4>最近一次探测</h4></div>
              <ElTag :type="tagType(authenticationTone(probeAuthentication))">{{ authenticationLabel(probeAuthentication) }}</ElTag>
            </div>
          </template>
          <p v-if="latestProbe" data-testid="gateway-node-probe-table">{{ probeTypeLabel(latestProbe.probe_type) }} · {{ latestProbe.success ? '通过' : '失败' }} · {{ latestProbe.latency_ms }} ms · {{ formatRelativeTime(latestProbe.created_at) }}</p>
          <ElEmpty v-else :image-size="56" description="还没有探测记录。" />
        </ElCard>
      </div>

      <ElCard shadow="never" data-testid="gateway-node-health-history">
        <template #header>
          <div class="gateway-node-card-head">
            <div><h4>探测历史</h4><ElText type="info">最近 {{ healthChecks.length }} 条</ElText></div>
          </div>
        </template>
        <ElTable :data="healthChecks" row-key="id" max-height="360" data-testid="gateway-node-health-table">
          <ElTableColumn label="类型" min-width="100">
            <template #default="{ row }">{{ probeTypeLabel(row.probe_type) }}</template>
          </ElTableColumn>
          <ElTableColumn label="结果" width="80">
            <template #default="{ row }">
              <ElTag :type="row.success ? 'success' : 'danger'">{{ row.success ? '通过' : '失败' }}</ElTag>
            </template>
          </ElTableColumn>
          <ElTableColumn label="延迟" width="88">
            <template #default="{ row }"><span class="gateway-node-num">{{ row.latency_ms }} ms</span></template>
          </ElTableColumn>
          <ElTableColumn label="状态码" width="80">
            <template #default="{ row }"><span class="gateway-node-num">{{ row.status_code ?? '—' }}</span></template>
          </ElTableColumn>
          <ElTableColumn label="错误" min-width="150" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.error" class="gateway-node-err">{{ row.error }}</span>
              <span v-else>—</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="时间" width="120">
            <template #default="{ row }"><span class="gateway-node-num">{{ formatDateTime(row.created_at) }}</span></template>
          </ElTableColumn>
          <template #empty>
            <ElEmpty v-if="!loading" :image-size="56" description="暂无探测记录。" />
          </template>
        </ElTable>
      </ElCard>
    </section>

    <!-- Token -->
    <section v-show="showSection('tokens')" id="tokens" class="gateway-node-section" data-testid="gateway-node-section-tokens">
      <div class="gateway-node-metrics" aria-label="节点指标">
        <ElCard v-for="metric in metricCards" :key="metric.id" shadow="never" :data-testid="`gateway-node-metric-${metric.id}`">
          <ElStatistic :title="metric.label" :value="metric.value as number" />
          <p class="gateway-node-metric-hint">{{ metric.hint }}</p>
        </ElCard>
      </div>
      <div class="gateway-node-metrics gateway-node-metrics--three">
        <ElCard v-for="metric in tokenCards" :key="metric.id" shadow="never" :data-testid="`gateway-node-token-${metric.id}`">
          <ElStatistic :title="metric.label" :value="metric.value as number" />
          <p class="gateway-node-metric-hint">{{ metric.hint }}</p>
        </ElCard>
      </div>
      <ElCard shadow="never" data-testid="gateway-node-token-trend">
        <template #header>
          <div class="gateway-node-card-head">
            <div><h4>Token 趋势</h4><ElText type="info">{{ windowLabel }}</ElText></div>
          </div>
        </template>
        <GatewayTrendChart v-if="stats" :buckets="stats.trend" mode="tokens" tall />
        <ElEmpty v-else :image-size="52" :description="loading ? '加载中…' : '暂无数据'" />
      </ElCard>
    </section>

    <!-- 性能 -->
    <section v-show="showSection('performance')" id="performance" class="gateway-node-section" data-testid="gateway-node-section-performance">
      <div class="gateway-node-grid">
        <ElCard shadow="never" data-testid="gateway-node-latency">
          <template #header>
            <div class="gateway-node-card-head">
              <div><h4>性能指标</h4><ElText type="info">窗口平均</ElText></div>
            </div>
          </template>
          <ElDescriptions :column="1">
            <ElDescriptionsItem v-for="row in latencyRows" :key="row.label" :label="row.label">{{ row.value }}</ElDescriptionsItem>
          </ElDescriptions>
          <details class="gateway-node-advanced">
            <summary>分位统计（p50 / p95）</summary>
            <ElDescriptions :column="1">
              <ElDescriptionsItem v-for="row in latencyAdvancedRows" :key="row.label" :label="row.label">{{ row.value }}</ElDescriptionsItem>
            </ElDescriptions>
          </details>
          <div class="gateway-node-peak">
            <span>峰值并发 <b>{{ stats?.peak_concurrency ?? '—' }}</b></span>
            <span>QPS <b>{{ stats ? stats.qps.toFixed(2) : '—' }}</b></span>
            <span>失败 <b class="gateway-node-peak--bad">{{ stats?.errors ?? '—' }}</b></span>
          </div>
        </ElCard>
        <ElCard shadow="never" data-testid="gateway-node-request-trend">
          <template #header>
            <div class="gateway-node-card-head">
              <div><h4>请求量趋势</h4><ElText type="info">{{ windowLabel }}</ElText></div>
            </div>
          </template>
          <GatewayTrendChart v-if="stats" :buckets="stats.trend" mode="requests" />
          <ElEmpty v-else :image-size="52" :description="loading ? '加载中…' : '暂无数据'" />
        </ElCard>
      </div>
    </section>

    <!-- 限制 -->
    <section v-show="showSection('limits')" id="limits" class="gateway-node-section" data-testid="gateway-node-section-limits">
      <div class="gateway-node-grid">
        <ElCard shadow="never" data-testid="gateway-node-scheduling-limits">
          <template #header>
            <div class="gateway-node-card-head">
              <div><h4>并发与权重</h4><ElText type="info">0 或空表示不限</ElText></div>
            </div>
          </template>
          <ElDescriptions :column="1">
            <ElDescriptionsItem label="最大并发">{{ node.max_concurrency || '不限' }}</ElDescriptionsItem>
            <ElDescriptionsItem label="有效权重">{{ node.weight }}</ElDescriptionsItem>
            <ElDescriptionsItem label="调度状态">
              <ElTag :type="tagType(schedulingTone(node.scheduling_state))">{{ schedulingStateLabel(node.scheduling_state) }}</ElTag>
            </ElDescriptionsItem>
            <ElDescriptionsItem label="配置状态">
              <ElTag :type="node.config_state === 'ENABLED' ? 'success' : 'info'">{{ configStateLabel(node.config_state) }}</ElTag>
            </ElDescriptionsItem>
          </ElDescriptions>
        </ElCard>
        <ElCard shadow="never" data-testid="gateway-node-quota">
          <template #header>
            <div class="gateway-node-card-head">
              <div><h4>Token 额度</h4></div>
            </div>
          </template>
          <ElDescriptions :column="1">
            <ElDescriptionsItem label="额度策略">{{ quotaPolicyLabel(node.quota_policy) }}</ElDescriptionsItem>
            <ElDescriptionsItem label="每小时限额">
              {{ node.hourly_token_limit ?? '不限' }} · 本小时已用 {{ hourTokens ? formatTokens(hourTokens.total_tokens) : '—' }}
            </ElDescriptionsItem>
            <ElDescriptionsItem label="每日限额">
              {{ node.daily_token_limit ?? '不限' }} · 今日已用 {{ dayTokens ? formatTokens(dayTokens.total_tokens) : '—' }}
            </ElDescriptionsItem>
            <ElDescriptionsItem label="未知用量请求（今日）">{{ dayTokens?.unknown_usage_requests ?? '—' }}</ElDescriptionsItem>
          </ElDescriptions>
        </ElCard>
      </div>
    </section>

    <!-- 健康 -->
    <section v-show="showSection('health')" id="health" class="gateway-node-section" data-testid="gateway-node-section-health">
      <div class="gateway-node-grid gateway-node-grid--full">
        <ElCard shadow="never" data-testid="gateway-node-health-state">
          <template #header>
            <div class="gateway-node-card-head">
              <div><h4>健康状态</h4></div>
            </div>
          </template>
          <ElDescriptions :column="1">
            <ElDescriptionsItem label="健康态">
              <ElTag :type="tagType(healthTone(node.health_state))">{{ healthStateLabel(node.health_state) }}</ElTag>
            </ElDescriptionsItem>
            <ElDescriptionsItem label="连续成功">{{ node.success_streak }}</ElDescriptionsItem>
            <ElDescriptionsItem label="连续失败">{{ node.failure_streak }}</ElDescriptionsItem>
            <ElDescriptionsItem label="最近探测">{{ formatDateTime(node.last_checked_at) }}</ElDescriptionsItem>
          </ElDescriptions>
          <ElCollapse>
            <ElCollapseItem title="原始探测详情" name="raw">
              <pre class="gateway-node-snapshot" data-testid="gateway-node-health-detail">{{ JSON.stringify(node.health_detail, null, 2) }}</pre>
            </ElCollapseItem>
          </ElCollapse>
        </ElCard>

      </div>
    </section>

    <!-- 日志 -->
    <section v-show="showSection('logs')" id="logs" class="gateway-node-section" data-testid="gateway-node-section-logs">
      <ElCard shadow="never" data-testid="gateway-node-logs">
        <template #header>
          <div class="gateway-node-card-head">
          <div>
            <h4>调用日志</h4>
            <ElText type="info">节点最近请求 · 元数据与测量值</ElText>
          </div>
          <ElButton :loading="logsLoading" :icon="RefreshCw" data-testid="gateway-node-logs-refresh" @click="loadRequests()">刷新</ElButton>
        </div>
        </template>
        <ElTable
          v-loading="logsLoading"
          :data="requests.items"
          row-key="id"
          element-loading-text="正在加载调用日志"
          data-testid="gateway-node-logs-table"
        >
          <ElTableColumn label="时间" width="116">
            <template #default="{ row }"><span class="gateway-node-num">{{ formatDateTime(row.started_at) }}</span></template>
          </ElTableColumn>
          <ElTableColumn label="Key / 模型" min-width="160">
            <template #default="{ row }">
              <div class="gateway-node-cell">
                <strong>{{ row.key_id ? row.key_id.slice(0, 12) : '—' }}</strong>
                <span>{{ row.model ?? '—' }}</span>
              </div>
            </template>
          </ElTableColumn>
          <ElTableColumn label="状态" min-width="150">
            <template #default="{ row }">
              <ElTag v-if="row.outcome === 'lost'" type="warning">结果未知</ElTag>
              <ElTag v-else-if="!row.finished_at && row.gateway_instance_id" type="info">进行中</ElTag>
              <ElTag v-else-if="!row.finished_at" type="info">历史未完成</ElTag>
              <ElTag v-else-if="row.error_code" type="danger" :title="row.error_code">{{ errorCause(row.error_code) }}</ElTag>
              <ElTag v-else :type="row.status_code && row.status_code < 400 ? 'success' : 'warning'">{{ row.status_code ?? '—' }}</ElTag>
            </template>
          </ElTableColumn>
          <ElTableColumn label="流式" width="88">
            <template #default="{ row }">{{ row.streaming ? 'SSE' : '普通' }}</template>
          </ElTableColumn>
          <ElTableColumn label="重试" width="68">
            <template #default="{ row }"><span class="gateway-node-num">{{ row.retry_count }}</span></template>
          </ElTableColumn>
          <ElTableColumn label="Token（入 / 出 / 总）" width="150">
            <template #default="{ row }"><span class="gateway-node-num">{{ tokenText(row as GatewayRequestLog) }}</span></template>
          </ElTableColumn>
          <ElTableColumn label="TTFT" width="96">
            <template #default="{ row }"><span class="gateway-node-num">{{ latencyText(row.ttft_ms) }}</span></template>
          </ElTableColumn>
          <ElTableColumn label="总时长" width="96">
            <template #default="{ row }"><span class="gateway-node-num">{{ latencyText(row.duration_ms) }}</span></template>
          </ElTableColumn>
          <template #empty>
            <ElEmpty v-if="!logsLoading" :image-size="64" description="暂无调用记录。" />
          </template>
        </ElTable>
        <footer class="gateway-node-footer">
          <ElText type="info" data-testid="gateway-node-logs-total">共 {{ requests.total }} 条 · 第 {{ requests.page }} 页</ElText>
          <ElPagination
            :current-page="requests.page"
            :page-size="requests.page_size"
            :total="requests.total"
            layout="prev, pager, next"
            :disabled="logsLoading"
            aria-label="节点调用日志分页"
            data-testid="gateway-node-logs-pagination"
            @current-change="loadRequests"
          />
        </footer>
      </ElCard>
    </section>

    <!-- 配置 -->
    <section v-show="showSection('config')" id="config" class="gateway-node-section" data-testid="gateway-node-section-config">
      <ElCard shadow="never" data-testid="gateway-node-config">
        <template #header>
          <div class="gateway-node-card-head">
            <div><h4>节点配置</h4></div>
            <ElButton
              type="primary"
              plain
              :icon="Pencil"
              data-testid="gateway-node-config-edit"
              @click="openEditor"
            >编辑配置</ElButton>
          </div>
        </template>
        <ElDescriptions :column="2">
          <ElDescriptionsItem label="名称">{{ node.name }}</ElDescriptionsItem>
          <ElDescriptionsItem label="资源池">{{ poolName(node.pool_id) }}</ElDescriptionsItem>
          <ElDescriptionsItem label="Base URL">{{ node.base_url }}</ElDescriptionsItem>
          <ElDescriptionsItem label="认证">{{ authTypeLabel(node.auth_type) }} · {{ node.auth_header }}</ElDescriptionsItem>
          <ElDescriptionsItem label="连接 / 请求超时">
            {{ node.connect_timeout_seconds ?? '默认' }} / {{ node.request_timeout_seconds ?? '默认' }} 秒
          </ElDescriptionsItem>
          <ElDescriptionsItem label="健康路径">{{ node.health_path || '继承默认' }}</ElDescriptionsItem>
          <ElDescriptionsItem label="探测间隔">{{ node.probe_interval_seconds ?? '继承默认' }} 秒</ElDescriptionsItem>
          <ElDescriptionsItem label="支持模型">{{ node.models.length ? node.models.join('、') : '未配置' }}</ElDescriptionsItem>
          <ElDescriptionsItem label="权重 / 并发">{{ node.weight }} / {{ node.max_concurrency || '不限' }}</ElDescriptionsItem>
          <ElDescriptionsItem label="额度">
            {{ node.hourly_token_limit ?? '不限' }} / {{ node.daily_token_limit ?? '不限' }}（{{ quotaPolicyLabel(node.quota_policy) }}）
          </ElDescriptionsItem>
          <ElDescriptionsItem label="排序">{{ node.sort_order }}</ElDescriptionsItem>
          <ElDescriptionsItem label="创建 / 更新">
            {{ formatDateTime(node.created_at) }} / {{ formatDateTime(node.updated_at) }}
          </ElDescriptionsItem>
        </ElDescriptions>
      </ElCard>
    </section>
      </div>
    </template>

    <GatewayNodeDrawer :open="editorOpen" :node="node" :pools="pools" @close="editorOpen = false" @saved="load" />
  </section>
</template>

<script setup lang="ts">
import { ArrowLeft, Pencil, RefreshCw } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  ElAlert,
  ElButton,
  ElCard,
  ElCollapse,
  ElCollapseItem,
  ElDescriptions,
  ElDescriptionsItem,
  ElDropdown,
  ElDropdownItem,
  ElDropdownMenu,
  ElEmpty,
  ElPagination,
  ElRadioButton,
  ElRadioGroup,
  ElSpace,
  ElStatistic,
  ElTable,
  ElTableColumn,
  ElTag,
  ElText,
  vLoading,
} from 'element-plus'
import {
  gatewayApi,
  type GatewayHealthCheck,
  type GatewayNode,
  type GatewayPool,
  type GatewayRequestLog,
  type GatewayRequestPage,
  type GatewayScopeStats,
  type GatewayStatsWindow,
} from '@/api/gateway'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import { formatTokens } from '@/utils/format'
import {
  authTypeLabel,
  authenticationLabel,
  authenticationResult,
  authenticationTone,
  configStateLabel,
  errorCause,
  errorMessage,
  formatDateTime,
  formatPercent,
  formatRelativeTime,
  healthStateLabel,
  healthTone,
  probeTypeLabel,
  quotaPolicyLabel,
  schedulingStateLabel,
  schedulingTone,
  type GatewayTone,
} from '@/utils/gateway'
import GatewayNodeDrawer from '@/views/gateway/GatewayNodeDrawer.vue'
import GatewayTrendChart from '@/views/gateway/GatewayTrendChart.vue'

const route = useRoute()
const router = useRouter()
const nodeId = computed(() => String(route.params.id ?? ''))

// 先回答能不能接请求。用量和运维收在同一页的分段里，地址保持 /gateway/nodes/:id。
const detailView = ref<'summary' | 'usage' | 'ops'>('summary')
function showSection(id: string) {
  if (detailView.value === 'summary') return id === 'overview'
  if (detailView.value === 'usage') return id === 'tokens' || id === 'performance'
  return id === 'limits' || id === 'health' || id === 'logs' || id === 'config'
}
const serveTone = computed(() => {
  const current = node.value
  if (!current) return 'unknown'
  if (current.config_state !== 'ENABLED') return 'down'
  if (current.scheduling_state !== 'ACTIVE') return 'degraded'
  if (current.health_state === 'UNHEALTHY') return 'down'
  if (current.health_state === 'DEGRADED') return 'degraded'
  if (current.health_state === 'UNKNOWN' || current.health_state === 'CHECKING') return 'unknown'
  return 'normal'
})
const serveSummary = computed(() => {
  const current = node.value
  if (!current) return { title: '正在确认能否接请求', detail: '' }
  const health = healthStateLabel(current.health_state)
  const scheduling = schedulingStateLabel(current.scheduling_state)
  const config = configStateLabel(current.config_state)
  const facts = `健康 ${health} · 调度 ${scheduling} · 配置 ${config}`
  if (current.config_state !== 'ENABLED') return { title: '不能接请求', detail: `配置已禁用。${facts}` }
  if (current.scheduling_state === 'DRAINING') return { title: '正在下线', detail: `不再接新请求。${facts}` }
  if (current.scheduling_state !== 'ACTIVE') return { title: '不能接请求', detail: facts }
  if (current.health_state === 'UNHEALTHY' || current.health_state === 'DEGRADED') return { title: '不能稳定接请求', detail: facts }
  if (current.health_state === 'UNKNOWN' || current.health_state === 'CHECKING') return { title: '还不能确认', detail: `探测还没完成。${facts}` }
  return { title: '可以接请求', detail: facts }
})
const latestProbe = computed(() => healthChecks.value[0] ?? null)
function onNodeCommand(command: string | number) {
  const name = String(command)
  if (name === 'refresh') void load()
  else if (name === 'probe') void probe()
  else if (name === 'drain') void drain()
  else if (name === 'restore') void restore()
  else if (name === 'toggle') void toggleConfig()
}

const windows: Array<{ id: GatewayStatsWindow; label: string }> = [
  { id: 'hour', label: '本小时' },
  { id: 'today', label: '今日' },
  { id: '7d', label: '近 7 天' },
  { id: '30d', label: '近 30 天' },
]
const statsWindow = ref<GatewayStatsWindow>('today')
const node = ref<GatewayNode | null>(null)
const pools = ref<GatewayPool[]>([])
const healthChecks = ref<GatewayHealthCheck[]>([])
const stats = ref<GatewayScopeStats | null>(null)
const hourTokens = ref<GatewayScopeStats | null>(null)
const dayTokens = ref<GatewayScopeStats | null>(null)
const requests = ref<GatewayRequestPage>({ items: [], total: 0, page: 1, page_size: 50 })
const loading = ref(false)
const logsLoading = ref(false)
const working = ref(false)
const error = ref('')
const editorOpen = ref(false)

// 竞态守卫：切换节点或整页刷新都会推进 generation，只有最新一轮响应可以落地，
// 否则慢响应会把上一个节点 / 上一个统计窗口的数据写回页面。
let generation = 0
let statsSeq = 0

const windowLabel = computed(() => windows.find((item) => item.id === statsWindow.value)?.label ?? '')
const probeAuthentication = computed(() => authenticationResult(healthChecks.value))

const metricCards = computed<Array<{ id: string; label: string; value: number | string; hint: string }>>(() => {
  const current = stats.value
  return [
    { id: 'requests', label: '窗口请求', value: current?.requests ?? '—', hint: '窗口内完成的调用总量' },
    { id: 'success-rate', label: '成功率', value: formatPercent(current?.success_rate), hint: '2xx/3xx 且未被中断' },
    { id: 'active', label: '当前并发', value: current?.active ?? '—', hint: '正在处理的请求数' },
    { id: 'qps', label: 'QPS', value: current ? current.qps.toFixed(2) : '—', hint: '窗口平均每秒请求数' },
    { id: 'tokens', label: '总 Token', value: current ? formatTokens(current.total_tokens) : '—', hint: '输入 + 输出，未知不估算' },
    { id: 'streak', label: '连续成功 / 失败', value: `${node.value?.success_streak ?? 0} / ${node.value?.failure_streak ?? 0}`, hint: '探测连击计数' },
  ]
})

const tokenCards = computed<Array<{ id: string; label: string; value: number | string; hint: string }>>(() => {
  const current = stats.value
  return [
    { id: 'input', label: '输入 Token', value: current ? formatTokens(current.input_tokens) : '—', hint: '上游 usage 累计' },
    { id: 'output', label: '输出 Token', value: current ? formatTokens(current.output_tokens) : '—', hint: '上游 usage 累计' },
    { id: 'total', label: '总 Token', value: current ? formatTokens(current.total_tokens) : '—', hint: '输入 + 输出' },
  ]
})

const latencyRows = computed(() => {
  const current = stats.value
  if (!current) return [] as Array<{ label: string; value: string }>
  const line = (label: string, value: { avg: number | null }, unit = 'ms') =>
    ({ label, value: value.avg === null ? '—' : `${value.avg.toFixed(0)} ${unit}` })
  return [
    line('连接', current.connect_ms),
    line('首字节（TTFB）', current.ttfb_ms),
    line('首 Token 延迟（TTFT）', current.ttft_ms),
    line('总时长', current.duration_ms),
    line('输出速度（Token/s）', current.output_tps, 'Token/s'),
  ]
})

const latencyAdvancedRows = computed(() => {
  const current = stats.value
  if (!current) return [] as Array<{ label: string; value: string }>
  const line = (label: string, value: { p50: number | null; p95: number | null }, unit = 'ms') =>
    ({ label, value: value.p50 == null && value.p95 == null ? '—' : `${value.p50?.toFixed(0) ?? '—'} / ${value.p95?.toFixed(0) ?? '—'} ${unit}` })
  return [
    line('连接', current.connect_ms),
    line('首字节（TTFB）', current.ttfb_ms),
    line('首 Token 延迟（TTFT）', current.ttft_ms),
    line('总时长', current.duration_ms),
    line('输出速度（Token/s）', current.output_tps, 'Token/s'),
  ]
})

function tagType(tone: GatewayTone): 'success' | 'warning' | 'danger' | 'info' {
  if (tone === 'success') return 'success'
  if (tone === 'warning') return 'warning'
  if (tone === 'danger') return 'danger'
  return 'info'
}

function tokenText(row: GatewayRequestLog): string {
  if (!row.usage_known) return 'unknown'
  return `${row.input_tokens ?? 0} / ${row.output_tokens ?? 0} / ${row.total_tokens ?? 0}`
}

function latencyText(value: number | null): string {
  return value === null || value === undefined ? '—' : `${value} ms`
}

function poolName(poolId: string | null): string {
  if (!poolId) return '未分配'
  return pools.value.find((pool) => pool.id === poolId)?.name ?? poolId
}

async function load(): Promise<void> {
  const current = ++generation
  loading.value = true
  error.value = ''
  try {
    const [nodeValue, poolList, checks, requestPage, hours, days] = await Promise.all([
      gatewayApi.node(nodeId.value),
      gatewayApi.pools(),
      gatewayApi.healthChecks(nodeId.value, 100),
      // 整页刷新与节点操作后停在当前日志页：只有切换节点才会回到第 1 页。
      gatewayApi.requests({ node_id: nodeId.value, page: requests.value.page, page_size: requests.value.page_size }),
      gatewayApi.stats('node', 'hour', nodeId.value),
      gatewayApi.stats('node', 'today', nodeId.value),
    ])
    if (current !== generation) return
    node.value = nodeValue
    pools.value = poolList
    healthChecks.value = checks
    requests.value = requestPage
    hourTokens.value = hours
    dayTokens.value = days
    // 日志总量变少会让当前页越界，回退到末页重取，避免停在空白页。
    const lastPage = Math.max(1, Math.ceil(requestPage.total / requestPage.page_size))
    if (requestPage.page > lastPage) await loadRequests(lastPage)
    await loadStats()
  } catch (reason) {
    if (current !== generation) return
    error.value = errorMessage(reason, '节点详情加载失败')
  } finally {
    if (current === generation) loading.value = false
  }
}

async function loadStats(): Promise<void> {
  const current = generation
  const seq = ++statsSeq
  try {
    const value = await gatewayApi.stats('node', statsWindow.value, nodeId.value)
    // 连续切换窗口时只认最后一次响应，慢的旧响应直接丢弃。
    if (current !== generation || seq !== statsSeq) return
    stats.value = value
  } catch (reason) {
    if (current !== generation || seq !== statsSeq) return
    error.value = errorMessage(reason, '统计加载失败')
  }
}

async function loadRequests(target: number = requests.value.page): Promise<void> {
  const current = generation
  logsLoading.value = true
  try {
    const page = await gatewayApi.requests({ node_id: nodeId.value, page: target, page_size: requests.value.page_size })
    if (current !== generation) return
    requests.value = page
  } catch (reason) {
    if (current !== generation) return
    notify(errorMessage(reason, '日志加载失败'), 'error')
  } finally {
    if (current === generation) logsLoading.value = false
  }
}

// 切换节点时立刻清空上一个节点的数据：旧节点的名称、指标、探测记录与操作按钮
// 都不该留在新路由下；同时推进 generation 作废在途响应。
function resetNodeState(): void {
  generation += 1
  node.value = null
  healthChecks.value = []
  stats.value = null
  hourTokens.value = null
  dayTokens.value = null
  requests.value = { items: [], total: 0, page: 1, page_size: requests.value.page_size }
  error.value = ''
}

async function probe(): Promise<void> {
  working.value = true
  try {
    const result = await gatewayApi.probeNode(nodeId.value, [])
    const passed = result.checks.filter((check) => check.success).length
    notify(`探测完成：${passed}/${result.checks.length} 项通过`, passed === result.checks.length ? 'success' : 'info')
    await load()
  } catch (reason) {
    notify(errorMessage(reason, '探测失败'), 'error')
  } finally {
    working.value = false
  }
}

async function drain(): Promise<void> {
  const confirmed = await confirmDialog({ title: '安全下线节点', message: '活跃请求会继续完成，新请求改走其它节点。', confirmLabel: '安全下线', danger: true })
  if (!confirmed) return
  working.value = true
  try {
    await gatewayApi.drainNode(nodeId.value)
    notify('节点已进入安全下线')
    await load()
  } catch (reason) {
    notify(errorMessage(reason, '下线失败'), 'error')
  } finally {
    working.value = false
  }
}

async function restore(): Promise<void> {
  working.value = true
  try {
    await gatewayApi.activateNode(nodeId.value)
    notify('节点已恢复调度')
    await load()
  } catch (reason) {
    notify(errorMessage(reason, '恢复失败'), 'error')
  } finally {
    working.value = false
  }
}

async function toggleConfig(): Promise<void> {
  if (!node.value) return
  const enable = node.value.config_state !== 'ENABLED'
  working.value = true
  try {
    await gatewayApi.updateNode(nodeId.value, { config_state: enable ? 'ENABLED' : 'DISABLED' })
    notify(enable ? '节点已启用' : '节点已禁用')
    await load()
  } catch (reason) {
    notify(errorMessage(reason, '状态更新失败'), 'error')
  } finally {
    working.value = false
  }
}

function openEditor(): void {
  editorOpen.value = true
}

function backToList(): void {
  void router.push('/gateway/nodes')
}

watch(nodeId, () => {
  resetNodeState()
  void load()
})
watch(statsWindow, () => { void loadStats() })

onMounted(() => {
  void load()
})
</script>

<style scoped>
/* 节点详情：身份卡 → 服务状态 → 分段导航 → 内容卡片。
   页面自身不再滚动，统一由网关壳的卡片 body 滚动，避免双层滚动。 */
.gateway-node-page { display: flex; flex-direction: column; gap: 16px; }
.gateway-node-state { min-height: 240px; }

/* ── 身份卡 ───────────────────────────────────────────────────────── */
.gateway-node-identity {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 16px;
  padding: 16px; border: 1px solid var(--astra-border); border-radius: 12px; background: var(--astra-bg-panel);
}
.gateway-node-identity__main { display: grid; gap: 4px; min-width: 0; }
.gateway-node-identity__title { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px 12px; min-width: 0; }
.gateway-node-identity__title h2 { margin: 0; color: var(--astra-text-primary); font-size: 18px; line-height: 26px; }
.gateway-node-identity__url { margin: 0; color: var(--astra-text-secondary); font-family: var(--astra-font-family); font-size: 12px; overflow-wrap: anywhere; }
.gateway-node-back { display: inline-flex; align-items: center; gap: 4px; width: fit-content; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.gateway-node-back:hover { color: var(--astra-primary-bright); }
.gateway-node-actions { flex: none; }

/* ── 服务状态 ─────────────────────────────────────────────────────── */
.gateway-node-status {
  display: flex; align-items: center; flex-wrap: wrap; gap: 10px 16px;
  padding: 12px 16px; border: 1px solid var(--astra-border); border-radius: 12px; background: var(--astra-bg-panel);
}
.gateway-node-status[data-status='normal'] { border-color: #cfe9dd; background: #f3fbf6; }
.gateway-node-status[data-status='degraded'] { border-color: #e8cd8f; background: #fff9ec; }
.gateway-node-status[data-status='down'] { border-color: #e7a8ae; background: #fff5f6; }
.gateway-node-status[data-status='unknown'] { border-color: #d5e0da; background: #f6f8f7; }
.gateway-node-status__main { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px 12px; min-width: 0; }
.gateway-node-status__pill {
  flex: none; padding: 3px 10px; border-radius: 999px;
  background: #e3f4ec; color: #187956; font-size: 12px; font-weight: var(--astra-weight-bold);
}
.gateway-node-status[data-status='degraded'] .gateway-node-status__pill { background: #fbeac4; color: #8a5a08; }
.gateway-node-status[data-status='down'] .gateway-node-status__pill { background: #f8dadd; color: #9f2f3a; }
.gateway-node-status[data-status='unknown'] .gateway-node-status__pill { background: #e7eeeb; color: #5c736a; }
.gateway-node-status__detail { margin: 0; color: var(--astra-text-secondary); }
.gateway-node-status__facts { display: flex; flex-wrap: wrap; gap: 6px 18px; margin-left: auto; color: var(--astra-text-muted); font-size: 12px; }
.gateway-node-status__facts b { color: var(--astra-text-primary); font-weight: 600; }

/* ── 分段导航 ─────────────────────────────────────────────────────── */
.gateway-node-tabs {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px 16px;
  padding: 8px 12px; border: 1px solid var(--astra-border); border-radius: 12px; background: var(--astra-bg-panel);
}
.gateway-node-tabs__tools { display: flex; align-items: center; gap: 8px; }
.gateway-node-tabs__label { color: var(--astra-text-muted); font-size: 12px; }

/* ── 内容 ─────────────────────────────────────────────────────────── */
.gateway-node-content { display: grid; gap: 16px; }
.gateway-node-section { display: grid; gap: 16px; }
/* 网关壳会去掉内层卡片边框与左右内边距；详情页恢复，让每个区块成为独立卡片。 */
.gateway-node-page :deep(.el-card) { border: 1px solid var(--astra-border); border-radius: 12px; background: var(--astra-bg-panel); }
.gateway-node-page :deep(.el-card) > .el-card__header { padding-left: 16px; padding-right: 16px; }
.gateway-node-page :deep(.el-card) > .el-card__body { padding-left: 16px; padding-right: 16px; }
.gateway-node-metrics { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 16px; }
.gateway-node-metrics--three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.gateway-node-metrics :deep(.el-statistic__head) { color: var(--astra-text-muted); font-size: var(--astra-font-control); }
.gateway-node-metrics :deep(.el-statistic__content) { color: var(--astra-text-primary); font-size: var(--astra-font-subtitle); font-weight: var(--astra-weight-bold); }
.gateway-node-metric-hint { margin: 4px 0 0; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.gateway-node-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; align-items: start; }
.gateway-node-grid--full { grid-template-columns: 1fr; }
.gateway-node-card-head { display: flex; align-items: flex-start; gap: 12px; }
.gateway-node-card-head > div { margin-right: auto; }
.gateway-node-card-head h4 { margin: 0; color: var(--astra-text-primary); font-size: 14px; line-height: 20px; }
.gateway-node-card-head .el-text { margin-top: 2px; font-size: 12px; }
.gateway-node-advanced { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--astra-border); }
.gateway-node-advanced summary { color: var(--astra-text-muted); font-size: var(--astra-font-helper); cursor: pointer; }
.gateway-node-advanced summary::-webkit-details-marker { display: none; }
.gateway-node-advanced summary::before { content: '▸ '; }
.gateway-node-advanced[open] summary::before { content: '▾ '; }
.gateway-node-peak { display: flex; gap: 16px; margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--astra-border); color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.gateway-node-peak b { color: var(--astra-text-primary); font-variant-numeric: tabular-nums; }
.gateway-node-peak--bad { color: var(--astra-danger); }
.gateway-node-snapshot { max-height: 220px; margin: 10px 0 0; padding: 10px; overflow: auto; color: var(--astra-text-secondary); border: 1px solid var(--astra-border); border-radius: 8px; background: var(--astra-bg-base); font: var(--astra-font-helper)/1.5 var(--astra-font-family); white-space: pre-wrap; }
.gateway-node-mono { font-family: var(--astra-font-family); font-size: var(--astra-font-helper); color: var(--astra-text-muted); }
.gateway-node-num { white-space: nowrap; font-size: var(--astra-font-helper); font-variant-numeric: tabular-nums; }
.gateway-node-err { color: var(--astra-danger); font-size: var(--astra-font-helper); }
.gateway-node-cell { display: grid; min-width: 0; }
.gateway-node-cell strong { color: var(--astra-text-primary); font-size: var(--astra-font-helper); }
.gateway-node-cell span { color: var(--astra-text-muted); font-size: var(--astra-font-micro); }
.gateway-node-footer { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding-top: 12px; }
@media (max-width: 1280px) {
  .gateway-node-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .gateway-node-grid { grid-template-columns: 1fr; }
}
@media (max-width: 1000px) {
  .gateway-node-identity { flex-wrap: wrap; }
}
@media (max-width: 760px) {
  .gateway-node-actions { width: 100%; }
  .gateway-node-metrics, .gateway-node-metrics--three { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .gateway-node-tabs { align-items: flex-start; }
  .gateway-node-status__facts { margin-left: 0; }
  .gateway-node-footer { flex-direction: column; align-items: flex-start; }
}
</style>
