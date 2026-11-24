<template>
  <ElDrawer
    v-if="open"
    class="gateway-node-drawer"
    data-testid="gateway-node-drawer"
    :model-value="true"
    :title="node ? '编辑上游节点' : '新增上游节点'"
    size="min(580px, 100%)"
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
      <div class="node-section-title">基础与连接</div>
      <div class="node-grid">
        <ElFormItem label="节点名称" prop="name">
          <ElInput v-model="form.name" placeholder="例如 DeepSeek 主节点" />
        </ElFormItem>
        <ElFormItem label="节点类型">
          <ElSelect v-model="form.probeStrategy" data-testid="gateway-node-probe-strategy" style="width: 100%" @change="applyProbeStrategy">
            <ElOption value="SSE" label="对话（流式）" />
            <ElOption value="NON_STREAM" label="对话（非流式）" />
            <ElOption value="EMBEDDING" label="向量嵌入" />
            <ElOption value="ASR" label="语音识别（ASR）" />
            <ElOption value="SCOPED" label="自定义诊断" />
          </ElSelect>
        </ElFormItem>
      </div>

      <ElFormItem label="上游地址" prop="baseUrl">
        <ElInput v-model="form.baseUrl" placeholder="https://api.example.com" />
        <span class="node-field-hint">填写服务根地址，不包含 /v1</span>
      </ElFormItem>

      <div class="node-grid">
        <ElFormItem label="认证方式">
          <ElSelect v-model="form.authType" style="width: 100%">
            <ElOption value="BEARER" label="Bearer Token" />
            <ElOption value="HEADER" label="自定义 Header" />
            <ElOption value="NONE" label="无认证" />
          </ElSelect>
        </ElFormItem>
        <ElFormItem v-if="form.authType === 'HEADER'" label="Header 名称">
          <ElInput v-model="form.authHeader" placeholder="例如 x-api-key" />
        </ElFormItem>
        <ElFormItem v-else-if="form.authType === 'BEARER'" label="API Key">
          <ElInput
            v-model="form.apiKey"
            type="password"
            show-password
            autocomplete="new-password"
            data-testid="gateway-node-api-key"
            :placeholder="node?.has_api_key ? '已配置，留空保持不变' : 'sk-...'"
          />
        </ElFormItem>
      </div>

      <ElFormItem v-if="form.authType === 'HEADER'" label="凭据值">
        <ElInput
          v-model="form.apiKey"
          type="password"
          show-password
          autocomplete="new-password"
          data-testid="gateway-node-api-key"
          :placeholder="node?.has_api_key ? '已配置，留空保持不变' : '输入认证 Header 对应的值'"
        />
      </ElFormItem>

      <div class="node-section-title">模型与分组</div>
      <ElFormItem label="支持模型" prop="modelsText">
        <ElInput
          v-model="form.modelsText"
          type="textarea"
          :rows="3"
          placeholder="每行或逗号分隔填写上游实际模型名，例如&#10;deepseek-chat&#10;deepseek-reasoner"
        />
      </ElFormItem>

      <div class="node-grid">
        <ElFormItem label="所属资源池">
          <ElSelect v-model="form.poolId" clearable placeholder="暂不分配" style="width: 100%">
            <ElOption v-for="pool in pools" :key="pool.id" :value="pool.id" :label="`${pool.name}${pool.enabled ? '' : '（已禁用）'}`" />
          </ElSelect>
        </ElFormItem>
        <ElFormItem v-if="node" label="调度状态">
          <ElSelect v-model="form.scheduling" style="width: 100%">
            <ElOption value="ACTIVE" :disabled="activeOptionDisabled" label="调度中" />
            <ElOption value="DRAINING" label="安全下线" />
            <ElOption value="BLOCKED" label="已阻止" />
          </ElSelect>
        </ElFormItem>
        <ElFormItem v-else label="上线方式">
          <div class="node-online-toggle">
            <ElSwitch v-model="autoActivate" :disabled="!activationReady" />
            <span>{{ activationReady ? '探测已通过，保存后自动上线' : '需通过连接探测后才可上线' }}</span>
          </div>
        </ElFormItem>
      </div>

      <ElFormItem v-if="node" label="启用节点">
        <ElSwitch v-model="form.enabled" />
      </ElFormItem>

      <!-- 连接测试区：显性放置在主流程中，避免用户找不到探测入口 -->
      <div class="node-probe-card">
        <div class="node-probe-card__head">
          <div class="node-probe-card__status">
            <span class="node-probe-card__title">连接探测</span>
            <ElTag
              size="small"
              :type="tagType(healthTone(currentHealth))"
              :class="{ 'astra-tag--neutral': healthTone(currentHealth) === 'neutral' }"
            >
              {{ healthStateLabel(currentHealth) }}
            </ElTag>
            <ElTag
              v-if="probeChecks.length"
              size="small"
              :type="tagType(authenticationTone(probeAuthentication))"
              :class="{ 'astra-tag--neutral': authenticationTone(probeAuthentication) === 'neutral' }"
            >
              {{ authenticationLabel(probeAuthentication) }}
            </ElTag>
          </div>
          <ElButton
            plain
            :loading="probeRunning"
            :disabled="busy || !form.probeTypes.length"
            data-testid="gateway-node-probe-run"
            @click="runProbe"
          >
            {{ probeRunning ? '探测中…' : '保存并测试连接' }}
          </ElButton>
        </div>

        <ElCheckboxGroup v-if="form.probeStrategy === 'SCOPED'" v-model="form.probeTypes" class="node-probe-card__checks">
          <ElCheckbox v-for="type in probeTypes" :key="type" :value="type">{{ probeTypeLabel(type) }}</ElCheckbox>
        </ElCheckboxGroup>

        <ul v-if="probeChecks.length" class="node-probe-result" data-testid="gateway-node-probe-checks">
          <li v-for="check in probeChecks" :key="check.id">
            <CheckCircle2 v-if="check.success" :size="14" class="ok" /><XCircle v-else :size="14" class="bad" />
            <span>{{ probeTypeLabel(check.probe_type) }}</span>
            <small>{{ check.latency_ms }} ms</small>
            <small v-if="check.status_code">HTTP {{ check.status_code }}</small>
            <em v-if="check.error" :title="check.error">{{ check.error }}</em>
          </li>
        </ul>
      </div>

      <ElCollapse v-model="secondaryOpen" class="node-secondary-collapse">
        <ElCollapseItem title="高级设置：超时与健康检查" name="probe">
          <div class="node-grid">
            <ElFormItem label="健康检查路径">
              <ElInput v-model="form.healthPath" placeholder="/health" />
            </ElFormItem>
            <ElFormItem label="探测间隔（秒）">
              <ElInput v-model="form.probeInterval" type="number" min="10" placeholder="默认" />
            </ElFormItem>
            <ElFormItem label="连接超时（秒）">
              <ElInput v-model="form.connectTimeout" type="number" min="0.1" step="0.1" placeholder="默认" />
            </ElFormItem>
            <ElFormItem label="请求超时（秒）">
              <ElInput v-model="form.requestTimeout" type="number" min="0.1" step="0.1" placeholder="默认" />
            </ElFormItem>
          </div>
        </ElCollapseItem>

        <ElCollapseItem title="高级设置：负载均衡与限额" name="quota">
          <div class="node-grid">
            <ElFormItem label="权重">
              <ElInput v-model="form.weight" type="number" min="1" />
            </ElFormItem>
            <ElFormItem label="最大并发（0 不限）">
              <ElInput v-model="form.maxConcurrency" type="number" min="0" />
            </ElFormItem>
            <ElFormItem label="每小时 Token 限额">
              <ElInput v-model="form.hourly" type="number" min="0" placeholder="不限" />
            </ElFormItem>
            <ElFormItem label="每日 Token 限额">
              <ElInput v-model="form.daily" type="number" min="0" placeholder="不限" />
            </ElFormItem>
            <ElFormItem label="超额策略">
              <ElSelect v-model="form.quotaPolicy" style="width: 100%">
                <ElOption value="STOP" label="超额停止新请求" />
                <ElOption value="ALERT" label="超额仅告警" />
                <ElOption value="RECORD" label="超额仅记录" />
              </ElSelect>
            </ElFormItem>
            <ElFormItem label="排序值">
              <ElInput v-model="form.sortOrder" type="number" />
            </ElFormItem>
          </div>
        </ElCollapseItem>
      </ElCollapse>

      <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="gateway-node-error" class="node-form__error" />
    </ElForm>

    <template #footer>
      <div class="node-drawer__footer">
        <ElTag v-if="dirty" type="warning" effect="light" data-testid="gateway-node-dirty">有未保存修改</ElTag>
        <span class="node-drawer__spacer" />
        <ElSpace :size="8">
          <ElButton :disabled="busy" @click="requestClose">取消</ElButton>
          <ElButton type="primary" :loading="saving" data-testid="gateway-node-save" @click="finish">
            {{ node ? '保存修改' : autoActivate && activationReady ? '创建并上线' : '保存为待上线' }}
          </ElButton>
        </ElSpace>
      </div>
    </template>
  </ElDrawer>
</template>

<script setup lang="ts">
import { onBeforeRouteLeave } from 'vue-router'
import { CheckCircle2, XCircle } from 'lucide-vue-next'
import { computed, reactive, ref, watch } from 'vue'
import {
  ElAlert,
  ElButton,
  ElCheckbox,
  ElCheckboxGroup,
  ElCollapse,
  ElCollapseItem,
  ElDrawer,
  ElForm,
  ElFormItem,
  ElInput,
  ElOption,
  ElSelect,
  ElSpace,
  ElSwitch,
  ElTag,
} from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import {
  gatewayApi,
  type GatewayAuthType,
  type GatewayHealthCheck,
  type GatewayHealthState,
  type GatewayNode,
  type GatewayNodeWrite,
  type GatewayPool,
  type GatewayProbeType,
  type GatewayQuotaPolicy,
  type GatewaySchedulingState,
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
const secondaryOpen = ref<string[]>([])

const formRef = ref<FormInstance>()
const saving = ref(false)
const probeRunning = ref(false)
const error = ref('')
const probeChecks = ref<GatewayHealthCheck[]>([])
const currentHealth = ref<GatewayHealthState>('UNKNOWN')
const createdId = ref<string | null>(null)
const createdInFlow = ref(false)
const autoActivate = ref(false)

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
  scheduling: 'DRAINING' as GatewaySchedulingState,
})

const busy = computed(() => saving.value || probeRunning.value)
const probeAuthentication = computed(() => authenticationResult(probeChecks.value))

const rules: FormRules = {
  name: [{ required: true, message: '请填写节点名称', trigger: 'blur' }],
  baseUrl: [
    { required: true, message: '请填写上游地址', trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        const text = String(value ?? '').trim()
        if (!text) return callback()
        if (!/^https?:\/\//i.test(text)) return callback(new Error('地址需以 http:// 或 https:// 开头'))
        if (/\/v1\/?$/i.test(text)) return callback(new Error('请填写根地址，不要包含 /v1'))
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
          return callback(new Error('请至少填写一个支持模型'))
        }
        callback()
      },
      trigger: 'blur',
    },
  ],
}

interface ConnectionBaseline {
  baseUrl: string
  authType: GatewayAuthType
  authHeader: string
  healthPath: string
  probeStrategy: string
  models: string
}
const baseline = ref<ConnectionBaseline | null>(null)

function resolvedAuthHeader(): string {
  if (form.authType === 'BEARER') return 'Authorization'
  return form.authHeader.trim() || 'Authorization'
}

function connectionSnapshot(): ConnectionBaseline {
  return {
    baseUrl: form.baseUrl.trim(),
    authType: form.authType,
    authHeader: resolvedAuthHeader(),
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
    secondaryOpen.value = []
    currentHealth.value = props.node?.health_state ?? 'UNKNOWN'
    createdId.value = props.node?.id ?? null
    createdInFlow.value = false
    autoActivate.value = false
    baseline.value = baselineOf(props.node)
    const node = props.node
    form.name = node?.name ?? ''
    form.baseUrl = node?.base_url ?? ''
    form.authType = node?.auth_type ?? 'BEARER'
    form.authHeader = node?.auth_header ?? 'Authorization'
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
    form.scheduling = node?.scheduling_state ?? 'DRAINING'
    initialSnapshot.value = formSnapshot()
  },
  { immediate: true },
)

watch(activationReady, (ready) => {
  if (!props.node) autoActivate.value = ready
})

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
    auth_header: resolvedAuthHeader(),
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
  if (!(await validateForm(['name', 'baseUrl', 'modelsText']))) return
  if (!form.probeTypes.length) { error.value = '请至少勾选一个探测项目'; return }
  probeRunning.value = true
  error.value = ''
  try {
    if (!(await persistBasic())) return
    if (!createdId.value) return
    const result = await gatewayApi.probeNode(createdId.value, form.probeTypes)
    probeChecks.value = result.checks
    currentHealth.value = result.node.health_state
    if (!props.node && (currentHealth.value === 'HEALTHY' || currentHealth.value === 'DEGRADED')) {
      autoActivate.value = true
    }
  } catch (reason) {
    error.value = errorMessage(reason, '探测失败')
  } finally {
    probeRunning.value = false
  }
}

async function finish() {
  if (!(await validateForm())) return
  const targetScheduling: GatewaySchedulingState = props.node
    ? form.scheduling
    : (autoActivate.value && activationReady.value ? 'ACTIVE' : 'DRAINING')
  const targetEnabled = props.node
    ? form.enabled
    : (autoActivate.value && activationReady.value)

  if (props.node && activating.value && !activationReady.value) {
    error.value = '请先点击「保存并测试连接」验证节点健康后，再切换为调度中。'
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
      config_state: targetEnabled ? 'ENABLED' : 'DISABLED',
      scheduling_state: targetScheduling,
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
  if (!props.open) return true
  if (busy.value) return false
  if (dirty.value) {
    const confirmed = await confirmDialog({
      title: '放弃未保存修改',
      message: '当前节点配置尚未保存，确认放弃并关闭？',
      confirmLabel: '放弃并关闭',
      cancelLabel: '继续编辑',
      danger: true,
    })
    if (!confirmed) return false
  }
  if (createdInFlow.value) emit('saved')
  emit('close')
  return true
}
onBeforeRouteLeave(() => requestClose())

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
.node-section-title {
  margin: 4px 0 12px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--astra-border-subtle);
  color: var(--astra-text-muted);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.03em;
}
.node-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 12px; }
.node-field-hint { display: block; margin-top: 4px; color: var(--astra-text-muted); font-size: 12px; line-height: 1.4; }
.node-online-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: var(--astra-control-height);
  color: var(--astra-text-muted);
  font-size: 12px;
}
.node-probe-card {
  margin: 4px 0 16px;
  padding: 12px 14px;
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-subtle);
}
.node-probe-card__head { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.node-probe-card__status { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.node-probe-card__title { color: var(--astra-text-primary); font-size: 13px; font-weight: 600; }
.node-probe-card__checks { margin-top: 10px; }
.node-probe-result { display: grid; gap: 6px; margin: 10px 0 0; padding: 10px 0 0; border-top: 1px solid var(--astra-border-subtle); list-style: none; }
.node-probe-result li { display: grid; grid-template-columns: 16px 72px auto auto minmax(0, 1fr); align-items: center; gap: 8px; color: var(--astra-text-secondary); font-size: 12px; }
.node-probe-result .ok { color: var(--astra-success); }
.node-probe-result .bad { color: var(--astra-danger); }
.node-probe-result em { overflow: hidden; color: var(--astra-danger); font-style: normal; text-overflow: ellipsis; white-space: nowrap; }
.node-secondary-collapse { border-top: 1px solid var(--astra-border-subtle); }
.node-secondary-collapse :deep(.el-collapse-item__header) { color: var(--astra-text-secondary); font-size: 13px; font-weight: 500; }
.node-form__error { margin-top: 12px; }
.node-drawer__footer { display: flex; align-items: center; gap: 10px; }
.node-drawer__spacer { flex: 1; }
@media (max-width: 520px) {
  .node-grid { grid-template-columns: 1fr; }
}
</style>
