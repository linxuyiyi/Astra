<template>
  <ElDrawer
    v-if="open"
    class="gateway-node-drawer"
    data-testid="gateway-node-drawer"
    :model-value="true"
    :title="node ? '编辑节点' : '新增节点'"
    size="min(560px, 100%)"
    :close-on-click-modal="false"
    :before-close="() => requestClose()"
  >
    <ElForm
      ref="formRef"
      class="node-form"
      :model="form"
      :rules="rules"
      label-position="top"
      scroll-to-error
      :disabled="busy"
    >
      <ElDivider content-position="left">连接与认证</ElDivider>
      <ElFormItem label="节点名称" prop="name">
        <ElInput v-model="form.name" placeholder="例如 主 Proxy 节点" />
      </ElFormItem>
      <ElFormItem prop="baseUrl">
        <template #label>上游 Base URL（节点根地址）</template>
        <ElInput v-model="form.baseUrl" placeholder="https://proxy.example.com" />
      </ElFormItem>
      <ElFormItem label="认证方式">
        <ElSelect v-model="form.authType">
          <ElOption value="BEARER" label="Bearer Token" />
          <ElOption value="HEADER" label="自定义 Header" />
          <ElOption value="NONE" label="无认证" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem label="认证 Header">
        <ElInput v-model="form.authHeader" :disabled="busy || form.authType === 'NONE'" placeholder="Authorization" />
      </ElFormItem>
      <ElFormItem v-if="form.authType !== 'NONE'">
        <template #label>上游凭据</template>
        <ElInput
          v-model="form.apiKey"
          type="password"
          autocomplete="new-password"
          data-testid="gateway-node-api-key"
          :placeholder="node?.has_api_key ? '已配置，留空保持不变' : '上游 Proxy 的 Token / 凭据值'"
        />
      </ElFormItem>
      <ElFormItem label="健康路径">
        <ElInput v-model="form.healthPath" placeholder="/health" />
      </ElFormItem>
      <ElFormItem label="探测间隔（秒）">
        <ElInput v-model="form.probeInterval" type="number" min="10" placeholder="继承默认" />
      </ElFormItem>
      <ElFormItem label="连接超时（秒）">
        <ElInput v-model="form.connectTimeout" type="number" min="0.1" step="0.1" placeholder="继承默认" />
      </ElFormItem>
      <ElFormItem label="请求超时（秒）">
        <ElInput v-model="form.requestTimeout" type="number" min="0.1" step="0.1" placeholder="继承默认" />
      </ElFormItem>

      <ElDivider content-position="left">模型与探测</ElDivider>
      <ElFormItem label="支持模型（逗号或换行分隔）" prop="modelsText">
        <ElInput v-model="form.modelsText" type="textarea" :rows="3" placeholder="gpt-4o-mini&#10;claude-3-5-sonnet" />
      </ElFormItem>
      <ElFormItem label="探测策略">
        <ElSelect v-model="form.probeStrategy" data-testid="gateway-node-probe-strategy" @change="applyProbeStrategy">
          <ElOption value="SSE" label="聊天流式" />
          <ElOption value="NON_STREAM" label="聊天非流式" />
          <ElOption value="EMBEDDING" label="向量嵌入" />
          <ElOption value="ASR" label="语音识别（ASR）" />
          <ElOption value="SCOPED" label="诊断探测" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem v-if="form.probeStrategy === 'SCOPED'">
        <template #label>探测项目</template>
        <ElCheckboxGroup v-model="form.probeTypes">
          <ElCheckbox v-for="type in probeTypes" :key="type" :value="type">{{ probeTypeLabel(type) }}</ElCheckbox>
        </ElCheckboxGroup>
      </ElFormItem>
      <div class="node-form__probe-bar">
        <ElButton type="primary" plain :loading="probeRunning" :disabled="busy || !form.probeTypes.length" data-testid="gateway-node-probe-run" @click="runProbe">
          {{ probeRunning ? '探测中…' : '保存并执行探测' }}
        </ElButton>
        <span class="node-form__health-line">
          <ElTag :type="tagType(healthTone(currentHealth))">健康：{{ healthStateLabel(currentHealth) }}</ElTag>
          <ElTag v-if="probeChecks.length" :type="tagType(authenticationTone(probeAuthentication))">{{ authenticationLabel(probeAuthentication) }}</ElTag>
        </span>
      </div>
      <ul v-if="probeChecks.length" class="node-probe-result" data-testid="gateway-node-probe-checks">
        <li v-for="check in probeChecks" :key="check.id">
          <CheckCircle2 v-if="check.success" :size="14" class="ok" /><XCircle v-else :size="14" class="bad" />
          <span>{{ probeTypeLabel(check.probe_type) }}</span>
          <small>{{ check.latency_ms }} ms</small>
          <small v-if="check.status_code">HTTP {{ check.status_code }}</small>
          <em v-if="check.error" :title="check.error">{{ check.error }}</em>
        </li>
      </ul>

      <ElDivider content-position="left">限额与额度</ElDivider>
      <ElFormItem label="权重">
        <ElInput v-model="form.weight" type="number" min="1" />
      </ElFormItem>
      <ElFormItem label="最大并发（0 为不限）">
        <ElInput v-model="form.maxConcurrency" type="number" min="0" />
      </ElFormItem>
      <ElFormItem>
        <template #label>每小时 Token 限额</template>
        <ElInput v-model="form.hourly" type="number" min="0" placeholder="不限" />
      </ElFormItem>
      <ElFormItem label="每日 Token 限额">
        <ElInput v-model="form.daily" type="number" min="0" placeholder="不限" />
      </ElFormItem>
      <ElFormItem label="额度策略">
        <ElSelect v-model="form.quotaPolicy">
          <ElOption value="STOP" label="达到限额停止新请求" />
          <ElOption value="ALERT" label="达到限额仅告警" />
          <ElOption value="RECORD" label="达到限额仅记录" />
        </ElSelect>
      </ElFormItem>

      <ElDivider content-position="left">资源池与启用</ElDivider>
      <ElFormItem label="资源池">
        <ElSelect v-model="form.poolId">
          <ElOption value="" label="不分配" />
          <ElOption v-for="pool in pools" :key="pool.id" :value="pool.id" :label="`${pool.name}${pool.enabled ? '' : '（已禁用）'}`" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem label="排序">
        <ElInput v-model="form.sortOrder" type="number" />
      </ElFormItem>
      <ElFormItem>
        <template #label>调度状态</template>
        <ElSelect v-model="form.scheduling">
          <ElOption value="ACTIVE" :disabled="activeOptionDisabled" label="调度中" />
          <ElOption value="DRAINING" label="安全下线" />
          <ElOption value="BLOCKED" label="已阻止" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem label="启用节点">
        <ElSwitch v-model="form.enabled" />
      </ElFormItem>
      <p v-if="!activationReady" class="node-form__note node-form__note--warn">
        <TriangleAlert :size="13" />只有完成所选策略的完整探测并达到 HEALTHY/DEGRADED，且未修改连接配置（Base URL、认证、模型、健康路径）时，才可启用并进入「调度中」。
      </p>

      <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="gateway-node-error" />
    </ElForm>

    <template #footer>
      <div class="node-drawer__footer">
        <ElTag v-if="dirty" type="warning" effect="light" data-testid="gateway-node-dirty">有未保存修改</ElTag>
        <ElSpace :size="8">
          <ElButton :disabled="busy" @click="requestClose">取消</ElButton>
          <ElButton type="primary" :loading="saving" data-testid="gateway-node-save" @click="finish">{{ node ? '保存节点' : '创建并保存' }}</ElButton>
        </ElSpace>
      </div>
    </template>
  </ElDrawer>
</template>

<script setup lang="ts">
import { CheckCircle2, TriangleAlert, XCircle } from 'lucide-vue-next'
import { computed, reactive, ref, watch } from 'vue'
import {
  ElAlert,
  ElButton,
  ElCheckbox,
  ElCheckboxGroup,
  ElDivider,
  ElDrawer,
  ElForm,
  ElFormItem,
  ElInput,
  ElOption,
  ElSelect,
  ElSwitch,
  ElTag,
  ElText,
} from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import {
  gatewayApi,
  type GatewayHealthCheck,
  type GatewayHealthState,
  type GatewayNode,
  type GatewayNodeWrite,
  type GatewayPool,
  type GatewayProbeType,
  type GatewayQuotaPolicy,
  type GatewaySchedulingState,
  type GatewayAuthType,
} from '@/api/gateway'
import { confirmDialog } from '@/composables/useConfirm'
import {
  authenticationLabel,
  authenticationResult,
  authenticationTone,
  errorMessage,
  healthStateLabel,
  healthTone,
  parseNumberList,
  probeTypeLabel,
  type GatewayTone,
} from '@/utils/gateway'

const props = defineProps<{ open: boolean; node: GatewayNode | null; pools: GatewayPool[] }>()
const emit = defineEmits<{ close: []; saved: [] }>()

const probeTypes: GatewayProbeType[] = ['NETWORK', 'SERVICE', 'MODEL', 'STREAM']

const formRef = ref<FormInstance>()
const saving = ref(false)
const probeRunning = ref(false)
const error = ref('')
const probeChecks = ref<GatewayHealthCheck[]>([])
const currentHealth = ref<GatewayHealthState>('UNKNOWN')
const createdId = ref<string | null>(null)
const createdInFlow = ref(false)

const form = reactive({
  name: '',
  baseUrl: '',
  authType: 'BEARER' as GatewayAuthType,
  authHeader: 'Authorization',
  apiKey: '',
  healthPath: '',
  connectTimeout: '',
  requestTimeout: '',
  probeInterval: '',
  modelsText: '',
  probeStrategy: 'SSE',
  probeTypes: ['STREAM'] as GatewayProbeType[],
  weight: '1',
  maxConcurrency: '0',
  hourly: '',
  daily: '',
  quotaPolicy: 'STOP' as GatewayQuotaPolicy,
  poolId: '',
  sortOrder: '0',
  enabled: true,
  scheduling: 'ACTIVE' as GatewaySchedulingState,
})

const busy = computed(() => saving.value || probeRunning.value)
const probeAuthentication = computed(() => authenticationResult(probeChecks.value))

const rules: FormRules = {
  name: [{ required: true, message: '请填写节点名称', trigger: 'blur' }],
  baseUrl: [
    { required: true, message: '请填写节点 Base URL', trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        const text = String(value ?? '').trim()
        if (!text) return callback()
        if (!/^https?:\/\//i.test(text)) return callback(new Error('Base URL 需以 http:// 或 https:// 开头'))
        if (/\/v1\/?$/i.test(text)) return callback(new Error('Base URL 请填写节点根地址，不要包含 /v1'))
        callback()
      },
      trigger: 'blur',
    },
  ],
  modelsText: [
    {
      validator: (_rule, value, callback) => {
        if (parseNumberList(String(value ?? '')).length) return callback()
        if ((props.node?.models.length ?? 0) === 0) {
          return callback(new Error('请至少配置一个支持模型，否则节点不会被任何路由选中'))
        }
        callback()
      },
      trigger: 'blur',
    },
  ],
}

// 连接相关字段一旦改动（相对最近一次已保存配置），服务端会重置旧健康态，
// 因此这次编辑同样视为未就绪，必须重新保存并完整探测。
interface ConnectionBaseline {
  baseUrl: string
  authType: GatewayAuthType
  authHeader: string
  healthPath: string
  probeStrategy: string
  models: string
}
const baseline = ref<ConnectionBaseline | null>(null)

function connectionSnapshot(): ConnectionBaseline {
  return {
    baseUrl: form.baseUrl.trim(),
    authType: form.authType,
    authHeader: form.authHeader.trim() || 'Authorization',
    healthPath: form.healthPath.trim(),
    probeStrategy: form.probeStrategy,
    models: parseNumberList(form.modelsText).join('\u0000'),
  }
}

function baselineOf(node: GatewayNode | null): ConnectionBaseline | null {
  if (!node) return null
  return {
    baseUrl: node.base_url,
    authType: node.auth_type,
    authHeader: node.auth_header,
    healthPath: node.health_path,
    probeStrategy: node.probe_strategy,
    models: (node.models ?? []).join('\u0000'),
  }
}

const connectionChanged = computed(() => {
  if (form.apiKey !== '') return true
  const base = baseline.value
  if (!base) return false
  const now = connectionSnapshot()
  return (
    now.baseUrl !== base.baseUrl ||
    now.authType !== base.authType ||
    now.authHeader !== base.authHeader ||
    now.healthPath !== base.healthPath ||
    now.probeStrategy !== base.probeStrategy ||
    now.models !== base.models
  )
})
const activationReady = computed(
  () =>
    !connectionChanged.value &&
    (currentHealth.value === 'HEALTHY' || currentHealth.value === 'DEGRADED'),
)
// 新进入 ACTIVE（当前不是 ACTIVE）才需要健康就绪；保持原有 ACTIVE 不是状态迁移。
const activating = computed(
  () => form.scheduling === 'ACTIVE' && (props.node?.scheduling_state ?? 'DRAINING') !== 'ACTIVE',
)
const activeOptionDisabled = computed(() => activating.value && !activationReady.value)

function formSnapshot(): string {
  const { apiKey, ...rest } = form
  return JSON.stringify({ ...rest, apiKeyFilled: apiKey !== '' })
}
const initialSnapshot = ref(formSnapshot())
const dirty = computed(() => props.open && formSnapshot() !== initialSnapshot.value)

watch(
  () => [props.open, props.node] as const,
  ([open]) => {
    if (!open) return
    error.value = ''
    probeChecks.value = []
    currentHealth.value = props.node?.health_state ?? 'UNKNOWN'
    createdId.value = props.node?.id ?? null
    createdInFlow.value = false
    baseline.value = baselineOf(props.node)
    const node = props.node
    form.name = node?.name ?? ''
    form.baseUrl = node?.base_url ?? ''
    form.authType = node?.auth_type ?? 'BEARER'
    form.authHeader = node?.auth_header ?? 'Authorization'
    // 密钥永不明文下发：编辑时输入框固定为空，仅用户重新录入才提交。
    form.apiKey = ''
    form.healthPath = node?.health_path ?? ''
    form.connectTimeout = node?.connect_timeout_seconds?.toString() ?? ''
    form.requestTimeout = node?.request_timeout_seconds?.toString() ?? ''
    form.probeInterval = node?.probe_interval_seconds?.toString() ?? ''
    form.modelsText = (node?.models ?? []).join('\n')
    form.probeStrategy = node?.probe_strategy ?? 'SSE'
    applyProbeStrategy()
    form.weight = String(node?.weight ?? 1)
    form.maxConcurrency = String(node?.max_concurrency ?? 0)
    form.hourly = node?.hourly_token_limit?.toString() ?? ''
    form.daily = node?.daily_token_limit?.toString() ?? ''
    form.quotaPolicy = node?.quota_policy ?? 'STOP'
    form.poolId = node?.pool_id ?? ''
    form.sortOrder = String(node?.sort_order ?? 0)
    form.enabled = (node?.config_state ?? 'ENABLED') === 'ENABLED'
    form.scheduling = node?.scheduling_state ?? 'ACTIVE'
    initialSnapshot.value = formSnapshot()
  },
  { immediate: true },
)

function applyProbeStrategy() {
  form.probeTypes = form.probeStrategy === 'ASR' ? ['ASR']
    : form.probeStrategy === 'EMBEDDING' ? ['EMBEDDING']
    : form.probeStrategy === 'NON_STREAM' ? ['MODEL']
    : form.probeStrategy === 'SSE' ? ['STREAM'] : [...probeTypes]
}

function intOrNull(value: string): number | null {
  const text = value.trim()
  if (!text) return null
  const parsed = Number(text)
  return Number.isFinite(parsed) ? Math.trunc(parsed) : null
}

function floatOrNull(value: string): number | null {
  const text = value.trim()
  if (!text) return null
  const parsed = Number(text)
  return Number.isFinite(parsed) ? parsed : null
}

function basePayload(): GatewayNodeWrite {
  return {
    name: form.name.trim(),
    base_url: form.baseUrl.trim(),
    auth_type: form.authType,
    auth_header: form.authHeader.trim() || 'Authorization',
    health_path: form.healthPath.trim(),
    probe_strategy: form.probeStrategy,
    connect_timeout_seconds: floatOrNull(form.connectTimeout),
    request_timeout_seconds: floatOrNull(form.requestTimeout),
    probe_interval_seconds: intOrNull(form.probeInterval),
    models: parseNumberList(form.modelsText),
    weight: Math.max(1, Number(form.weight) || 1),
    max_concurrency: Number(form.maxConcurrency) || 0,
    hourly_token_limit: intOrNull(form.hourly),
    daily_token_limit: intOrNull(form.daily),
    quota_policy: form.quotaPolicy,
  }
}

async function validateForm(fields?: string[]): Promise<boolean> {
  if (!formRef.value) return false
  if (fields) return formRef.value.validateField(fields).then(() => true).catch(() => false)
  return formRef.value.validate().then(() => true).catch(() => false)
}

async function persistBasic(): Promise<boolean> {
  const payload = basePayload()
  if (form.apiKey) payload.api_key = form.apiKey
  try {
    let saved: GatewayNode
    if (!createdId.value) {
      saved = await gatewayApi.createNode({ ...payload, config_state: 'DISABLED', scheduling_state: 'DRAINING' })
      createdId.value = saved.id
      createdInFlow.value = true
    } else {
      saved = await gatewayApi.updateNode(createdId.value, payload)
    }
    form.apiKey = ''
    // 连接配置变化后服务端会重置健康，以返回的节点为准同步界面状态。
    currentHealth.value = saved.health_state
    baseline.value = connectionSnapshot()
    error.value = ''
    return true
  } catch (reason) {
    error.value = errorMessage(reason, '保存节点失败')
    return false
  }
}

async function runProbe() {
  if (!(await validateForm(['name', 'baseUrl']))) return
  if (!form.probeTypes.length) { error.value = '请至少勾选一个探测项目'; return }
  probeRunning.value = true
  error.value = ''
  try {
    if (!(await persistBasic())) return
    if (!createdId.value) return
    const result = await gatewayApi.probeNode(createdId.value, form.probeTypes)
    probeChecks.value = result.checks
    currentHealth.value = result.node.health_state
  } catch (reason) {
    error.value = errorMessage(reason, '探测失败')
  } finally {
    probeRunning.value = false
  }
}

async function finish() {
  if (!(await validateForm())) return
  if (activating.value && !activationReady.value) {
    error.value = '节点需完成 NETWORK/SERVICE/MODEL/STREAM 完整探测并达到 HEALTHY/DEGRADED，才能进入「调度中」；请先执行完整探测，或改为安全下线 / 已阻止。'
    return
  }
  saving.value = true
  error.value = ''
  try {
    if (!(await persistBasic())) return
    if (!createdId.value) return
    await gatewayApi.updateNode(createdId.value, {
      pool_id: form.poolId || null,
      sort_order: Number(form.sortOrder) || 0,
      config_state: form.enabled ? 'ENABLED' : 'DISABLED',
      scheduling_state: form.scheduling,
    })
    createdInFlow.value = false
    emit('saved')
    emit('close')
  } catch (reason) {
    error.value = errorMessage(reason, '保存节点失败')
  } finally {
    saving.value = false
  }
}

async function requestClose() {
  if (busy.value) return
  if (dirty.value) {
    const confirmed = await confirmDialog({ title: '放弃未保存修改', message: '当前节点配置尚未保存完成，确认放弃并关闭窗口？', confirmLabel: '放弃并关闭', cancelLabel: '继续编辑', danger: true })
    if (!confirmed) return
  }
  if (createdInFlow.value) emit('saved')
  emit('close')
}

function tagType(tone: GatewayTone): 'success' | 'warning' | 'danger' | 'info' {
  if (tone === 'success') return 'success'
  if (tone === 'warning') return 'warning'
  if (tone === 'danger') return 'danger'
  return 'info'
}
</script>

<style scoped>
.node-form { display: grid; }
.node-form :deep(.el-form-item) { margin-bottom: 14px; }
.node-form :deep(.el-divider) { margin: 6px 0 16px; }
.node-form__note { display: flex; align-items: center; gap: 6px; margin: 0 0 14px; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.node-form__note--warn { color: var(--astra-warning); }
.node-form__probe-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
.node-form__health-line { display: inline-flex; gap: 6px; }
.node-probe-result { display: grid; gap: 5px; margin: 0 0 14px; padding: 0; list-style: none; }
.node-probe-result li { display: grid; grid-template-columns: 16px 70px auto auto minmax(0, 1fr); align-items: center; gap: 8px; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); }
.node-probe-result .ok { color: var(--astra-success); }
.node-probe-result .bad { color: var(--astra-danger); }
.node-probe-result em { overflow: hidden; color: var(--astra-danger); font-style: normal; text-overflow: ellipsis; white-space: nowrap; }
.node-drawer__footer { display: flex; align-items: center; justify-content: flex-end; gap: 10px; }
</style>
