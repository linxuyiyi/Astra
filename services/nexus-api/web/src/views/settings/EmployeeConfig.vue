<template>
  <component :is="embedded ? 'div' : 'section'">
    <AstraPageHeader
      v-if="!embedded"
      :title="creating ? '新建 Agent Worker' : `${workerTitle} 配置`"
      testid="employee-config-header"
    />

    <ElForm
      ref="formRef"
      data-testid="employee-config-form"
      class="worker-config-form"
      :model="form"
      :rules="formRules"
      :disabled="maintenanceBusy"
      label-position="top"
      scroll-to-error
      @submit.prevent="save()"
    >
      <section class="config-section" data-testid="worker-basic-section">
        <header class="config-section__head">
          <div>
            <h3>基本信息</h3>
          </div>
        </header>

        <ElFormItem label="数字员工" prop="digital_employee_id">
          <ElSelect
            v-model="form.digital_employee_id"
            class="worker-config-control"
            filterable
            clearable
            :loading="digitalLoading"
            placeholder="选择已创建的数字员工"
            :disabled="!creating && !form.digital_employee_id"
            @change="onDigitalEmployeeChange"
          >
            <ElOption
              v-for="item in digitalEmployees"
              :key="item.id"
              :value="item.id"
              :label="`${item.name} · ${item.domain}`"
            >
              <span class="digital-option">
                <span class="digital-option__avatar">{{ item.avatar || '🤖' }}</span>
                <span class="digital-option__copy">
                  <strong>{{ item.name }}</strong>
                  <small>{{ item.domain }}</small>
                </span>
              </span>
            </ElOption>
          </ElSelect>
          <p v-if="digitalLoading || !digitalEmployees.length" class="field-hint">
            <span v-if="digitalLoading">正在加载数字员工目录…</span>
            <RouterLink v-else-if="!digitalEmployees.length" class="field-link" to="/settings/employees">先去创建数字员工</RouterLink>
          </p>
        </ElFormItem>

        <div v-if="digitalSummary.found" class="digital-summary" data-testid="worker-digital-summary">
          <span class="digital-summary__avatar">{{ digitalSummary.avatar || '🤖' }}</span>
          <div class="digital-summary__copy">
            <div class="digital-summary__line">
              <strong>{{ digitalSummary.name }}</strong>
              <ElTag v-if="digitalSummary.domain" size="small" effect="plain">{{ digitalSummary.domain }}</ElTag>
              <ElTag size="small" type="info" effect="plain">只读复用</ElTag>
            </div>
            <p>{{ digitalSummary.description || '该数字员工暂未填写职责说明。' }}</p>
          </div>
        </div>
        <ElAlert
          v-else-if="!creating && !form.digital_employee_id"
          type="warning"
          :closable="false"
          show-icon
          title="历史 Worker 尚未关联数字员工，可在此补选。"
        />
        <ElAlert
          v-else-if="form.digital_employee_id"
          type="warning"
          :closable="false"
          show-icon
          title="数字员工目录中暂未找到该员工，目录恢复后会显示完整身份信息。"
        />

        <div class="config-grid">
          <ElFormItem label="Worker 名称" prop="worker_name">
            <ElInput v-model.trim="form.worker_name" maxlength="120" placeholder="例如：星河 · 测试机 01" />
          </ElFormItem>
          <ElFormItem label="环境说明">
            <ElInput v-model="form.description" placeholder="例如：测试平台远程开发环境" />
          </ElFormItem>
        </div>
      </section>

      <section class="config-section" data-testid="worker-scope-section">
        <header class="config-section__head">
          <div>
            <h3>工作范围</h3>
          </div>
        </header>

        <ElFormItem label="支持任务类型" prop="task_types">
          <ElCheckboxGroup v-model="form.task_types">
            <ElCheckbox value="ISSUE">问题</ElCheckbox>
            <ElCheckbox value="REQUIREMENT">需求</ElCheckbox>
          </ElCheckboxGroup>
        </ElFormItem>

        <ElFormItem label="支持任务分类" prop="category_id">
          <ElSelect v-model="form.category_id" class="worker-config-control" placeholder="选择一个任务分类">
            <ElOption v-for="category in enabledCategories" :key="category.id" :value="category.id" :label="category.name">
              {{ category.name }}
            </ElOption>
          </ElSelect>
        </ElFormItem>
      </section>

      <section class="config-section" data-testid="worker-dispatch-section">
        <header class="config-section__head">
          <div>
            <h3>派单配置</h3>
          </div>
        </header>

        <div class="dispatch-grid">
          <ElFormItem>
            <template #label>允许平台调度</template>
            <ElSwitch v-model="form.enabled" aria-label="允许平台调度" />
          </ElFormItem>
          <ElFormItem>
            <template #label>自动领取任务</template>
            <ElSwitch v-model="form.auto_claim" aria-label="自动领取任务" />
          </ElFormItem>
          <ElFormItem>
            <template #label>允许自主执行</template>
            <ElSwitch v-model="form.auto_execute" aria-label="允许自主执行" />
          </ElFormItem>
        </div>

        <ElFormItem v-if="!creating">
          <template #label>手动暂停</template>
          <div class="runtime-row">
            <p>{{ paused ? '已暂停接单' : '未暂停' }}</p>
            <ElButton :disabled="statusChanging || (!paused && currentTaskCount > 0)" @click="toggleRuntime">
              {{ statusChanging ? '处理中…' : paused ? '继续接单' : '暂停接单' }}
            </ElButton>
          </div>
        </ElFormItem>
      </section>

      <section class="config-section" data-testid="environment-form">
        <header class="config-section__head">
          <div>
            <h3>执行资源环境</h3>
          </div>
        </header>

        <ElFormItem label="运行位置">
          <ElRadioGroup v-model="form.environment.mode" class="mode-radio">
            <ElRadioButton value="LOCAL"><Laptop :size="15" /> 本地</ElRadioButton>
            <ElRadioButton value="LINUX_REMOTE"><Server :size="15" /> Linux 远程</ElRadioButton>
          </ElRadioGroup>
        </ElFormItem>

        <div v-if="form.environment.mode === 'LINUX_REMOTE'" class="environment-block" data-testid="worker-login-section">
          <div class="environment-block__head">
            <h4>登录信息</h4>
          </div>
          <div class="config-grid config-grid--three">
            <ElFormItem label="服务器" prop="environment.host">
              <ElInput v-model.trim="form.environment.host" maxlength="255" placeholder="10.0.0.12 或 worker.example.com" />
            </ElFormItem>
            <ElFormItem label="服务器端口" prop="environment.port">
              <ElInput v-model.number="form.environment.port" type="number" min="1" max="65535" />
            </ElFormItem>
            <ElFormItem label="用户名" prop="environment.username">
              <ElInput v-model.trim="form.environment.username" maxlength="120" placeholder="nexus" />
            </ElFormItem>
          </div>
          <ElFormItem label="密码" prop="environment.ssh_password">
            <ElInput
              v-model="form.environment.ssh_password"
              maxlength="1024"
              type="password"
              show-password
              autocomplete="new-password"
              :placeholder="employee?.execution_environment?.has_ssh_password ? '已配置，留空保持不变' : '请输入 SSH 密码'"
            />
          </ElFormItem>
        </div>

        <div class="environment-block" data-testid="worker-engineering-section">
          <div class="environment-block__head">
            <h4>工程信息</h4>
          </div>
          <ElFormItem label="工作空间" prop="environment.workspace_root">
            <ElInput
              v-model.trim="form.environment.workspace_root"
              :placeholder="form.environment.mode === 'LOCAL' ? 'D:\\code\\project 或 ./workspaces/worker-01' : '/home/nexus/project'"
            />
          </ElFormItem>
          <div class="config-grid">
            <ElFormItem label="代码仓库地址">
              <ElInput v-model.trim="form.environment.repository_url" placeholder="https://codeup.aliyun.com/group/project.git" />
            </ElFormItem>
            <ElFormItem label="目标分支">
              <ElInput v-model.trim="form.environment.repository_branch" maxlength="255" placeholder="main" />
            </ElFormItem>
          </div>
          <ElFormItem label="Codeup 账号 / Token">
            <ElInput
              v-model="form.environment.repository_token"
              type="password"
              show-password
              autocomplete="new-password"
              :placeholder="repositoryTokenPlaceholder"
            />
          </ElFormItem>
          <div v-if="form.environment.repository_id" class="repository-bound">
            <ElTag size="small" type="success" effect="plain">已绑定仓库</ElTag>
            <span>{{ form.environment.repository_url || `仓库 ${form.environment.repository_id}` }}</span>
            <ElButton link type="danger" @click="clearRepository">解除绑定</ElButton>
          </div>
        </div>

        <div class="environment-block" data-testid="worker-coding-section">
          <div class="environment-block__head">
            <h4>编码 Agent</h4>
          </div>
          <div class="config-grid">
            <ElFormItem label="编程工具">
              <ElSelect v-model="form.environment.coding_tool" class="worker-config-control">
                <ElOption value="opencode" label="OpenCode" />
                <ElOption value="deepseek-harness" label="DeepSeek Harness（未接入）" disabled />
              </ElSelect>
            </ElFormItem>
            <ElFormItem label="编程模型">
              <ElInput v-model.trim="form.environment.model_name" maxlength="255" placeholder="例如：nexus-gateway/deepseek-v4" />
            </ElFormItem>
          </div>
        </div>

        <ElCollapse v-model="advancedOpen" class="advanced-settings">
          <ElCollapseItem title="高级设置" name="advanced">
            <div class="config-grid">
              <ElFormItem label="OpenCode 可执行文件" prop="environment.opencode_path">
                <ElInput v-model.trim="form.environment.opencode_path" :placeholder="form.environment.mode === 'LOCAL' ? 'opencode' : '/usr/local/bin/opencode'" />
              </ElFormItem>
              <ElFormItem label="OpenCode Server 端口" prop="environment.opencode_server_port">
                <ElInput v-model.number="form.environment.opencode_server_port" type="number" min="1" max="65535" />
              </ElFormItem>
            </div>
          </ElCollapseItem>
        </ElCollapse>
      </section>

      <ElAlert
        v-if="message"
        data-testid="form-message"
        class="form-message"
        :type="messageIsError ? 'error' : 'success'"
        :closable="false"
        :data-error="messageIsError"
        show-icon
        :title="message"
      />
    </ElForm>

    <StickyActionBar
      v-if="canSave && !embedded"
      :dirty="canSave"
      :saving="saving"
      :save-label="creating ? '创建 Agent Worker' : '保存配置'"
      @save="save()"
      @cancel="reset"
    />
  </component>
</template>

<script setup lang="ts">
import { Laptop, Server } from 'lucide-vue-next'
import { computed, reactive, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  ElAlert,
  ElButton,
  ElCheckbox,
  ElCheckboxGroup,
  ElCollapse,
  ElCollapseItem,
  ElForm,
  ElFormItem,
  ElInput,
  ElOption,
  ElRadioButton,
  ElRadioGroup,
  ElSelect,
  ElSwitch,
  ElTag,
} from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { api } from '@/api/client'
import type { Employee, TaskCategory, TaskType } from '@/api/types'
import AstraPageHeader from '@/components/astra/AstraPageHeader.vue'
import StickyActionBar from '@/components/astra/StickyActionBar.vue'
import { useWorkerMaintenance } from '@/composables/useWorkerMaintenance'
import { useAstraStore } from '@/stores/astra'
import { isMaintenanceActive } from '@/features/workers/presentation'
import { employeeDisplayName } from '@/utils/format'
import type { Employee as DigitalEmployee } from '@/types'

type CodingTool = 'opencode' | 'deepseek-harness'

const props = withDefaults(defineProps<{
  employee: Employee | null
  creating: boolean
  categories: TaskCategory[]
  digitalEmployees?: DigitalEmployee[]
  digitalLoading?: boolean
  embedded?: boolean
}>(), { digitalEmployees: () => [], digitalLoading: false, embedded: false })
const emit = defineEmits<{ (e: 'saved', id: string, deployRequested: boolean): void }>()
const store = useAstraStore()

const formRef = ref<FormInstance | null>(null)
const saving = ref(false)
const statusChanging = ref(false)
const message = ref('')
const messageIsError = ref(false)
const originalSnapshot = ref('')
const advancedOpen = ref<string[]>([])

const emptyEnvironment = () => ({
  mode: 'LOCAL' as 'LOCAL' | 'LINUX_REMOTE',
  host: '',
  port: 22 as number | string,
  username: '',
  ssh_password: '',
  opencode_server_port: 14096 as number | string,
  opencode_path: 'opencode',
  model_name: '',
  workspace_root: '',
  repository_id: null as string | null,
  repository_url: '',
  repository_branch: 'main',
  repository_token: '',
  coding_tool: 'opencode' as CodingTool,
})

const form = reactive({
  digital_employee_id: '',
  worker_name: '',
  description: '',
  task_types: ['ISSUE', 'REQUIREMENT'] as TaskType[],
  category_id: '',
  enabled: true,
  auto_claim: true,
  auto_execute: true,
  environment: emptyEnvironment(),
})

const enabledCategories = computed(() => props.categories.filter((item) => item.enabled))
const selectedEnvironment = computed(() => props.employee?.execution_environment ?? null)
const currentTaskCount = computed(() => props.employee?.current_task_count ?? 0)
const maintenance = useWorkerMaintenance(computed(() => selectedEnvironment.value?.id ?? null))
const maintenanceBusy = computed(() => maintenance.busyAction.value !== null || isMaintenanceActive(maintenance.operation.value?.state))
const paused = computed(() => props.employee?.status === 'PAUSED')
const workerTitle = computed(() => selectedEnvironment.value?.name || employeeDisplayName(props.employee?.name))

const selectedDigitalEmployee = computed(() =>
  props.digitalEmployees.find((item) => item.id === form.digital_employee_id) ?? null,
)
const digitalSummary = computed(() => {
  const digital = selectedDigitalEmployee.value
  if (digital) {
    return { found: true, name: digital.name, domain: digital.domain, description: digital.description, avatar: digital.avatar }
  }
  if (form.digital_employee_id) {
    return { found: false, name: form.digital_employee_id, domain: '', description: '', avatar: '' }
  }
  return { found: false, name: '', domain: '', description: '', avatar: '' }
})
const repositoryTokenPlaceholder = computed(() =>
  form.environment.repository_id && form.environment.repository_url === (selectedEnvironment.value?.repository_url ?? '')
    ? '已配置，留空保持不变'
    : '请输入 Codeup Token / 访问令牌',
)

const portValidator = (_rule: unknown, value: unknown, callback: (error?: Error) => void) => {
  const numeric = Number(value)
  callback(value !== '' && Number.isInteger(numeric) && numeric >= 1 && numeric <= 65535 ? undefined : new Error('端口需在 1-65535 之间'))
}
const formRules = computed<FormRules>(() => ({
  digital_employee_id: props.creating ? [{ required: true, message: '请选择数字员工', trigger: 'change' }] : [],
  worker_name: [{ required: true, message: '请输入 Worker 名称', trigger: 'blur' }],
  task_types: [{ type: 'array', required: true, message: '至少选择一种任务类型', trigger: 'change' }],
  category_id: [{ required: true, message: '请选择任务分类', trigger: 'change' }],
  'environment.workspace_root': [{ required: true, message: '请输入工作空间', trigger: 'blur' }],
  'environment.opencode_path': [{ required: true, message: '请输入 OpenCode 可执行文件', trigger: 'blur' }],
  'environment.opencode_server_port': [{ required: true, message: '请输入 OpenCode Server 端口', trigger: 'blur' }, { validator: portValidator, trigger: 'blur' }],
  'environment.host': [{ required: true, message: '请输入服务器地址', trigger: 'blur' }],
  'environment.port': [{ required: true, message: '请输入服务器端口', trigger: 'blur' }, { validator: portValidator, trigger: 'blur' }],
  'environment.username': [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  'environment.ssh_password': [{ required: props.creating || !props.employee?.execution_environment?.has_ssh_password, message: '请输入 SSH 密码', trigger: 'blur' }],
}))

function defaultCategoryId() {
  return enabledCategories.value[0]?.id ?? ''
}

function fill(employee?: Employee) {
  const environment = employee?.execution_environment
  const storedCategory = employee?.category_ids?.[0] ?? ''
  Object.assign(form, {
    digital_employee_id: employee?.digital_employee_id ?? '',
    worker_name: environment?.name ?? '',
    description: environment?.description ?? '',
    task_types: [...(employee?.task_types ?? ['ISSUE', 'REQUIREMENT'])],
    category_id: enabledCategories.value.some((item) => item.id === storedCategory) ? storedCategory : defaultCategoryId(),
    enabled: employee?.enabled ?? true,
    auto_claim: employee?.auto_claim ?? true,
    auto_execute: employee?.auto_execute ?? true,
    environment: environment
      ? {
          mode: environment.mode,
          host: environment.host ?? '',
          port: environment.port,
          username: environment.username ?? '',
          ssh_password: '',
          opencode_server_port: environment.opencode_server_port ?? 14096,
          opencode_path: environment.opencode_path || 'opencode',
          model_name: environment.model_name ?? '',
          workspace_root: environment.workspace_root,
          repository_id: environment.repository_id ?? null,
          repository_url: environment.repository_url ?? '',
          repository_branch: environment.repository_branch ?? environment.branch ?? 'main',
          repository_token: '',
          coding_tool: 'opencode',
        }
      : emptyEnvironment(),
  })
  message.value = ''
  messageIsError.value = false
  originalSnapshot.value = snapshot()
  formRef.value?.clearValidate()
}

function snapshot(): string {
  return JSON.stringify({
    digital_employee_id: form.digital_employee_id,
    worker_name: form.worker_name,
    description: form.description,
    task_types: form.task_types,
    category_id: form.category_id,
    enabled: form.enabled,
    auto_claim: form.auto_claim,
    auto_execute: form.auto_execute,
    environment: {
      mode: form.environment.mode,
      host: form.environment.mode === 'LINUX_REMOTE' ? form.environment.host : '',
      port: Number(form.environment.port),
      username: form.environment.mode === 'LINUX_REMOTE' ? form.environment.username : '',
      ssh_password: form.environment.ssh_password,
      opencode_server_port: Number(form.environment.opencode_server_port ?? 14096),
      opencode_path: form.environment.opencode_path,
      model_name: form.environment.model_name,
      workspace_root: form.environment.workspace_root,
      repository_id: form.environment.repository_id,
      repository_url: form.environment.repository_url,
      repository_branch: form.environment.repository_branch,
      repository_token: form.environment.repository_token,
      coding_tool: form.environment.coding_tool,
    },
  })
}
const isDirty = computed(() => snapshot() !== originalSnapshot.value)
const canSave = computed(() => props.creating || isDirty.value)
const busy = computed(() => saving.value || maintenanceBusy.value)
const canDeploy = computed(() => form.environment.mode === 'LINUX_REMOTE')

watch([() => props.employee?.id, () => props.creating], () => { fill(props.employee ?? undefined) }, { immediate: true })
watch(() => enabledCategories.value.map((item) => item.id).join(','), () => {
  if (!form.category_id && !props.employee) form.category_id = defaultCategoryId()
})

function onDigitalEmployeeChange(id: string) {
  const digital = props.digitalEmployees.find((item) => item.id === id)
  if (!digital) return
  if (!form.worker_name || form.worker_name.endsWith('的 Worker')) {
    form.worker_name = `${digital.name} 的 Worker`
  }
}

function environmentPayload(repositoryId: string | null) {
  const remote = form.environment.mode === 'LINUX_REMOTE'
  return {
    mode: form.environment.mode,
    name: form.worker_name.trim(),
    description: form.description,
    host: remote ? form.environment.host.trim() || null : null,
    port: Number(form.environment.port),
    username: remote ? form.environment.username.trim() || null : null,
    ssh_password: remote ? form.environment.ssh_password || null : null,
    opencode_server_port: Number(form.environment.opencode_server_port ?? 14096),
    opencode_path: form.environment.opencode_path.trim() || 'opencode',
    model_name: form.environment.model_name.trim() || null,
    workspace_root: form.environment.workspace_root.trim(),
    repository_id: repositoryId,
    branch: repositoryId ? form.environment.repository_branch.trim() || 'main' : null,
    labels: [...(selectedEnvironment.value?.labels ?? [])],
  }
}

function employeePayload() {
  const digital = selectedDigitalEmployee.value
  const fallback = props.employee
  return {
    digital_employee_id: form.digital_employee_id || null,
    name: digital?.name ?? fallback?.name ?? form.worker_name.trim(),
    role: digital?.domain ?? fallback?.role ?? 'Agent Worker',
    description: digital?.description ?? fallback?.description ?? '',
    character_id: fallback?.character_id ?? 0,
    task_types: form.task_types,
    category_ids: form.category_id ? [form.category_id] : [],
    enabled: form.enabled,
    auto_claim: form.auto_claim,
    auto_execute: form.auto_execute,
  }
}

function normalizedEnvironment(value: Record<string, unknown> | ReturnType<typeof emptyEnvironment>) {
  const data = value as Record<string, unknown>
  return {
    mode: data.mode,
    name: data.name ?? '',
    description: data.description ?? '',
    host: data.host ?? null,
    port: Number(data.port ?? 22),
    username: data.username ?? null,
    opencode_server_port: Number(data.opencode_server_port ?? 14096),
    opencode_path: data.opencode_path ?? 'opencode',
    model_name: data.model_name ?? null,
    workspace_root: data.workspace_root ?? '',
    repository_id: data.repository_id ?? null,
    branch: data.branch ?? null,
  }
}

async function resolveRepositoryId(): Promise<string | null> {
  const url = form.environment.repository_url.trim()
  const current = selectedEnvironment.value
  if (!url) return null
  const token = form.environment.repository_token.trim()
  if (current?.repository_id && url === (current.repository_url ?? '') && !token) {
    return current.repository_id
  }
  if (!token) {
    throw new Error('填写代码仓库地址后，需要提供 Codeup Token / 访问令牌才能完成绑定')
  }
  const created = await api.createRepository({
    url,
    branch: form.environment.repository_branch.trim() || 'main',
    token,
  })
  return created.id
}

function environmentChanged(repositoryId: string | null) {
  if (!selectedEnvironment.value) return true
  if (form.environment.ssh_password || form.environment.repository_token) return true
  return JSON.stringify(normalizedEnvironment(environmentPayload(repositoryId)))
    !== JSON.stringify(normalizedEnvironment(selectedEnvironment.value as unknown as Record<string, unknown>))
}

function newWorkerId(): string {
  const suffix = globalThis.crypto?.randomUUID?.().replaceAll('-', '').slice(0, 12)
    ?? `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`
  return `worker-${suffix}`
}

async function save(deployRequested = false) {
  if (maintenanceBusy.value) return
  message.value = ''
  messageIsError.value = false
  if (deployRequested && !canDeploy.value) {
    message.value = '本地 Worker 不支持远程部署，请保存配置后执行检查。'
    messageIsError.value = true
    return
  }
  const valid = formRef.value ? await formRef.value.validate().catch(() => false) : true
  if (!valid) return
  if (!form.task_types.length) {
    message.value = '工作范围至少需要一种任务类型'
    messageIsError.value = true
    return
  }
  saving.value = true
  let savedId = ''
  try {
    const repositoryId = await resolveRepositoryId()
    let employeeId = props.employee?.id ?? ''
    if (props.creating) {
      const created = await api.createEmployee({
        id: newWorkerId(),
        ...employeePayload(),
        execution_environment: environmentPayload(repositoryId),
      })
      employeeId = created.id
    } else {
      await api.updateEmployee(employeeId, employeePayload())
      if (environmentChanged(repositoryId)) {
        await api.updateEnvironment(employeeId, environmentPayload(repositoryId))
      }
    }
    savedId = employeeId
    await store.refresh()
    form.environment.ssh_password = ''
    form.environment.repository_token = ''
    form.environment.repository_id = repositoryId
    originalSnapshot.value = snapshot()
    message.value = deployRequested ? '配置已保存，正在提交一键部署。' : '配置已保存。'
    messageIsError.value = false
    emit('saved', employeeId, deployRequested)
  } catch (error) {
    messageIsError.value = true
    message.value = error instanceof Error ? error.message : '保存失败'
    if (savedId) {
      await store.refresh()
      emit('saved', savedId, false)
    }
  } finally {
    saving.value = false
  }
}

async function toggleRuntime() {
  if (!props.employee) return
  statusChanging.value = true
  message.value = ''
  try {
    const action = paused.value ? 'resume' : 'pause'
    await api.employeeAction(props.employee.id, action)
    await store.refresh()
    message.value = action === 'resume' ? '该 Worker 已恢复接单。' : '该 Worker 已暂停接单，不会领取新任务。'
  } catch (error) {
    messageIsError.value = true
    message.value = error instanceof Error ? error.message : '状态更新失败'
  } finally {
    statusChanging.value = false
  }
}

function clearRepository() {
  form.environment.repository_id = null
  form.environment.repository_url = ''
  form.environment.repository_branch = 'main'
  form.environment.repository_token = ''
}

function reset() {
  fill(props.employee ?? undefined)
}

defineExpose({ getDirty: () => isDirty.value, dirty: isDirty, canSave, canDeploy, saving: busy, reset, save })
</script>

<style scoped>
.worker-config-form { display: grid; gap: 14px; width: 100%; }
.config-section {
  display: grid;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-panel);
}
.config-section__head h3 { margin: 0; color: var(--astra-text-primary); font-size: var(--astra-font-emphasis); }
.config-section__head p { margin: 3px 0 0; color: var(--astra-text-muted); font-size: var(--astra-font-helper); line-height: 1.6; }
.config-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; }
.config-grid--three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.worker-config-control { width: 100%; }
.field-hint { margin: 4px 0 0; color: var(--astra-text-muted); font-size: var(--astra-font-helper); line-height: 1.6; }
.field-link { color: var(--astra-primary); }
.worker-config-form :deep(.el-form-item) { margin-bottom: 14px; }
.worker-config-form :deep(.el-input), .worker-config-form :deep(.el-select) { width: 100%; }
.worker-config-form :deep(.el-checkbox-group) { display: flex; flex-wrap: wrap; gap: 4px 18px; }
.digital-option { display: flex; align-items: center; gap: 8px; }
.digital-option__avatar { display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px; border-radius: 8px; background: var(--astra-bg-subtle); font-size: 16px; }
.digital-option__copy { display: grid; gap: 0; line-height: 1.3; }
.digital-option__copy small { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.digital-summary { display: flex; align-items: flex-start; flex-wrap: wrap; gap: 12px; padding: 12px; border: 1px solid var(--astra-primary-border); border-radius: 10px; background: var(--astra-primary-soft); }
.digital-summary__avatar { display: inline-flex; align-items: center; justify-content: center; width: 42px; height: 42px; flex: none; border-radius: 12px; background: var(--astra-bg-panel); font-size: 24px; }
.digital-summary__copy { display: grid; gap: 4px; min-width: 0; flex: 1; }
.digital-summary__line { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.digital-summary__copy p { max-height: 4.8em; overflow-y: auto; overflow-wrap: anywhere; margin: 0; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); line-height: 1.6; }
.digital-summary__alert { grid-column: 1 / -1; width: 100%; }
.dispatch-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0 18px; }
.runtime-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; width: 100%; }
.runtime-row p { flex: 1; min-width: 0; margin: 0; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.mode-radio :deep(.el-radio-button__inner) { display: inline-flex; align-items: center; gap: 5px; }
.environment-block { display: grid; gap: 4px; padding: 14px; border: 1px dashed var(--astra-border); border-radius: 10px; }
.environment-block__head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
.environment-block__head h4 { margin: 0; color: var(--astra-text-primary); font-size: var(--astra-font-label); }
.environment-block__head p { margin: 0; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.repository-bound { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 8px 10px; border-radius: 8px; background: var(--astra-bg-subtle); color: var(--astra-text-secondary); font-size: var(--astra-font-helper); }
.advanced-settings { border: 0; }
.advanced-settings :deep(.el-collapse-item__header), .advanced-settings :deep(.el-collapse-item__wrap) { background: transparent; }
.form-message { margin-top: 0; }
@media (max-width: 720px) {
  .config-grid, .config-grid--three, .dispatch-grid { grid-template-columns: 1fr; }
  .config-section { padding: 13px; }
}
</style>
