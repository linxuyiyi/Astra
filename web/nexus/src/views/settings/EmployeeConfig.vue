<template>
  <component :is="embedded ? 'div' : 'section'">
    <AstraPageHeader
      v-if="!embedded"
      :title="creating ? '创建 Worker' : `${employeeDisplayName(employee?.name)} 配置`"
      testid="employee-config-header"
    />
    <ElForm
      ref="formRef"
      data-testid="employee-config-form"
      class="employee-config-form"
      :model="form"
      :rules="formRules"
      :disabled="maintenanceBusy"
      label-position="left"
      label-width="160px"
      scroll-to-error
      @submit.prevent="save"
    >
      <ElDivider content-position="left">关联员工与执行资源</ElDivider>

      <ElFormItem v-if="!creating" label="关联员工 ID">
        <IdDisplay :value="employee?.id ?? ''" :max-width="200" />
      </ElFormItem>
      <ElFormItem v-else label="关联员工 ID" prop="id">
        <ElInput v-model.trim="form.id" placeholder="worker-04" />
      </ElFormItem>
      <ElFormItem label="关联员工名称" prop="name">
        <ElInput v-model.trim="form.name" placeholder="Worker-04" />
      </ElFormItem>
      <ElFormItem label="关联员工形象" prop="character_id">
        <div class="employee-character-field">
          <ElRadioGroup v-model="form.character_id" class="employee-character-options" aria-label="关联员工形象" data-testid="employee-character-picker">
            <ElRadio v-for="character in employeeCharacters" :key="character.id" :value="character.id" border>
              <img :src="employeeCharacterSource(character.id)" alt="" draggable="false" />
              <span>{{ character.name }}</span>
            </ElRadio>
          </ElRadioGroup>
          <p class="employee-character-help">沿用关联员工的界面形象，仅用于标识，不改变 Worker 的执行资源与部署方式。</p>
        </div>
      </ElFormItem>
      <ElFormItem label="关联员工岗位" prop="role">
        <ElInput v-model.trim="form.role" placeholder="测试平台开发" />
      </ElFormItem>
      <ElFormItem prop="default_work_area">
        <template #label>默认办公区域 <FieldHelp label="默认办公区域" help="无特定任务场景时，关联员工会出现在这个区域。" /></template>
        <ElSelect v-model="form.default_work_area">
          <ElOption v-for="area in workspaceAreas" :key="area.id" :value="area.id" :label="area.name" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem label="关联员工说明">
        <ElInput v-model="form.description" type="textarea" :rows="4" placeholder="说明关联员工的工作职责与边界；Worker 部署与维护以执行资源字段为准" />
      </ElFormItem>

      <ElDivider content-position="left">工作范围</ElDivider>

      <ElFormItem label="支持任务类型">
        <ElCheckboxGroup v-model="form.task_types">
          <ElCheckbox value="ISSUE">问题 <FieldHelp label="问题" help="缺陷、故障、异常或已有能力失效" /></ElCheckbox>
          <ElCheckbox value="REQUIREMENT">需求 <FieldHelp label="需求" help="新功能、能力增强、改造、优化或重构" /></ElCheckbox>
        </ElCheckboxGroup>
      </ElFormItem>
      <ElFormItem label="支持任务分类">
        <ElCheckboxGroup v-model="form.category_ids">
          <ElCheckbox v-for="category in categories" :key="category.id" :value="category.id">{{ category.name }} <FieldHelp :label="category.name" :help="category.description" /></ElCheckbox>
        </ElCheckboxGroup>
      </ElFormItem>

      <ElDivider content-position="left">派单配置</ElDivider>

      <ElFormItem>
        <template #label>允许平台调度 <FieldHelp label="允许平台调度" help="关闭后不再向关联员工派发新任务；Worker 环境、仓库与维护记录不会被删除。" /></template>
        <ElSwitch v-model="form.enabled" aria-label="允许平台调度" />
      </ElFormItem>
      <ElFormItem>
        <template #label>自动领取任务 <FieldHelp label="自动领取任务" help="空闲时可以领取匹配的新任务。每位关联员工同时只处理一项任务。" /></template>
        <ElSwitch v-model="form.auto_claim" aria-label="自动领取任务" />
      </ElFormItem>
      <ElFormItem>
        <template #label>允许自主执行 <FieldHelp label="允许自主执行" help="领取后自动启动任务独立执行会话" /></template>
        <ElSwitch v-model="form.auto_execute" aria-label="允许自主执行" />
      </ElFormItem>
      <ElFormItem v-if="!creating">
        <template #label>运行状态 <FieldHelp label="运行状态" help="由系统状态机维护（空闲 / 执行中 / 等待输入 / 异常），此表单不可直接修改。" /></template>
        <div class="runtime-row">
          <ElTag :type="statusTagType(employee?.status ?? 'UNKNOWN')">
            {{ runtimeLabel }}
          </ElTag>
          <p>{{ paused ? '关联员工已暂停接单，不会领取新任务。' : '关联员工可正常领取匹配任务。' }}</p>
          <ElButton :disabled="statusChanging || (!paused && currentTaskCount > 0)" @click="toggleRuntime">
            {{ statusChanging ? '处理中…' : paused ? '继续接单' : '暂停接单' }}
          </ElButton>
        </div>
      </ElFormItem>

      <ElDivider content-position="left">执行资源环境</ElDivider>
      <p class="maintenance-help">以下字段描述 Agent Worker 的服务器、工作目录与接入方式；关联员工的身份与职责不在这里扩展。</p>

      <div data-testid="environment-form">
        <ElFormItem>
          <template #label>工作环境类型 <FieldHelp label="工作环境类型" help="本地：NEXUS 所在主机。Linux 远程：SSH 隧道连接 Server。" /></template>
          <ElRadioGroup v-model="form.environment.mode">
            <ElRadioButton value="LOCAL"><Laptop :size="15" /> 本地</ElRadioButton>
            <ElRadioButton value="LINUX_REMOTE"><Server :size="15" /> Linux 远程</ElRadioButton>
          </ElRadioGroup>
        </ElFormItem>
        <ElFormItem prop="environment.name">
          <template #label>Worker 名称 <FieldHelp label="Worker 名称" help="Worker 作为执行资源的显示名称，与环境资源 ID 对应。" /></template>
          <ElInput v-model.trim="form.environment.name" placeholder="生产环境 Worker-04" />
        </ElFormItem>
        <ElFormItem prop="environment.opencode_path">
          <template #label>OpenCode 可执行文件 <FieldHelp label="OpenCode 可执行文件" help="正式 Runtime 由该 OpenCode 启动 Server；显式 fallback 时才使用 CLI。" /></template>
          <ElInput v-model.trim="form.environment.opencode_path" :placeholder="form.environment.mode === 'LOCAL' ? 'opencode 或 C:\\Program Files\\OpenCode\\opencode.exe' : '/usr/local/bin/opencode'" />
        </ElFormItem>
        <ElFormItem>
          <template #label>OpenCode 模型 <FieldHelp label="OpenCode 模型" help="留空时使用 Agent 自身或平台默认模型。" /></template>
          <ElInput v-model.trim="form.environment.model_name" placeholder="opencode/glm-5.1" />
        </ElFormItem>
        <ElFormItem label="环境说明">
          <ElInput v-model="form.environment.description" placeholder="例如：测试平台开发环境" />
        </ElFormItem>
        <ElFormItem prop="environment.workspace_root">
          <template #label>工作空间 <FieldHelp label="工作空间" help="支持 Windows 盘符、空格和中文目录；该路径属于 Worker，会在不同任务间长期复用。" /></template>
          <ElInput v-model.trim="form.environment.workspace_root" :placeholder="form.environment.mode === 'LOCAL' ? 'D:\\code\\nr_test_system' : '/home/nexus/project'" />
        </ElFormItem>
        <ElFormItem prop="environment.opencode_server_port">
          <template #label>OpenCode Server Port <FieldHelp label="OpenCode Server Port" help="人工配置固定端口，同一主机的 Worker 须使用不同端口，避开平台占用的端口。仅监听 127.0.0.1；远程通过 SSH 隧道访问。" /></template>
          <ElInput v-model.number="form.environment.opencode_server_port" type="number" min="1" max="65535" />
        </ElFormItem>
        <template v-if="form.environment.mode === 'LINUX_REMOTE'">
          <ElFormItem label="SSH Host" prop="environment.host">
            <ElInput v-model.trim="form.environment.host" placeholder="10.0.0.12" />
          </ElFormItem>
          <ElFormItem label="SSH Port" prop="environment.port">
            <ElInput v-model.number="form.environment.port" type="number" min="1" max="65535" />
          </ElFormItem>
          <ElFormItem label="Username" prop="environment.username">
            <ElInput v-model.trim="form.environment.username" placeholder="nexus" />
          </ElFormItem>
          <ElFormItem prop="environment.ssh_password">
            <template #label>SSH 密码 <FieldHelp label="SSH 密码" help="SSH 密码保存在部署数据库中且接口不回显；已配置时留空保持不变。" /></template>
            <ElInput v-model="form.environment.ssh_password" type="password" show-password autocomplete="new-password" :placeholder="employee?.execution_environment?.has_ssh_password ? '已配置，留空保持不变' : '请输入 SSH 密码'" />
          </ElFormItem>
        </template>

        <template v-if="!creating && selectedEnvironment">
          <ElFormItem label="Server 地址"><span class="static-value">{{ serverAddress }}</span></ElFormItem>
          <ElFormItem label="工作目录"><span class="static-value">{{ selectedEnvironment.workspace_root || '—' }}</span></ElFormItem>
          <ElFormItem label="模型"><span class="static-value">{{ selectedEnvironment.model_name || '—' }}</span></ElFormItem>
          <ElFormItem label="目标仓库"><span class="static-value">{{ repositoryCopy }}</span></ElFormItem>
          <ElFormItem label="最近检测"><span class="static-value">{{ formatTime(selectedEnvironment.last_checked_at) }}</span></ElFormItem>
        </template>
      </div>

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

    <template v-if="!creating && selectedEnvironment">
      <ElDivider content-position="left">维护操作</ElDivider>
      <WorkerMaintenancePanel
        :controller="maintenance"
        :disabled-reason="maintenanceBlockedReason"
        @changed="onMaintenanceChanged"
      />
    </template>

    <StickyActionBar v-if="isDirty && !embedded" :dirty="isDirty" :saving="saving" :save-label="creating ? '创建 Worker' : '保存配置'" @save="save" @cancel="reset" />
  </component>
</template>

<script setup lang="ts">
import { Laptop, Server } from 'lucide-vue-next'
import { computed, reactive, ref, watch } from 'vue'
import { ElAlert, ElButton, ElCheckbox, ElCheckboxGroup, ElDivider, ElForm, ElFormItem, ElInput, ElOption, ElRadio, ElRadioGroup, ElRadioButton, ElSelect, ElSwitch, ElTag } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { api } from '@/api/client'
import type { Employee, TaskCategory, TaskType } from '@/api/types'
import AstraPageHeader from '@/components/astra/AstraPageHeader.vue'
import FieldHelp from '@/components/forms/FieldHelp.vue'
import IdDisplay from '@/components/astra/IdDisplay.vue'
import StickyActionBar from '@/components/astra/StickyActionBar.vue'
import WorkerMaintenancePanel from '@/components/worker/WorkerMaintenancePanel.vue'
import { useWorkerMaintenance } from '@/composables/useWorkerMaintenance'
import { workspaceAreas, type WorkspaceAreaId } from '@/features/workspace/sceneLayout'
import { employeeCharacters, employeeCharacterSource } from '@/features/workspace/characterAssets'
import { useAstraStore } from '@/stores/astra'
import { isMaintenanceActive, repositorySummary, workerRepository } from '@/features/workers/presentation'
import { employeeDisplayName, formatTime, statusTagType } from '@/utils/format'

const props = defineProps<{ employee: Employee | null; creating: boolean; categories: TaskCategory[]; embedded?: boolean }>()
const emit = defineEmits<{ (e: 'saved', id: string): void }>()
const store = useAstraStore()

const formRef = ref<FormInstance | null>(null)
const saving = ref(false)
const statusChanging = ref(false)
const message = ref('')
const messageIsError = ref(false)
const originalSnapshot = ref('')

const emptyEnvironment = () => ({ mode: 'LOCAL' as 'LOCAL' | 'LINUX_REMOTE', name: '本地 Agent 工作环境', description: '', host: '', port: 22 as number | string, username: '', ssh_password: '', opencode_server_port: 14096 as number | string, opencode_path: 'opencode', model_name: '', workspace_root: '', labels: [] as string[] })
const form = reactive({ id: '', name: '', role: '', description: '', character_id: 0, default_work_area: 'development' as WorkspaceAreaId, task_types: ['ISSUE', 'REQUIREMENT'] as TaskType[], category_ids: [] as string[], enabled: true, auto_claim: true, auto_execute: true, environment: emptyEnvironment() })

const portValidator = (_rule: unknown, value: unknown, callback: (error?: Error) => void) => {
  const numeric = Number(value)
  callback(value !== '' && Number.isInteger(numeric) && numeric >= 1 && numeric <= 65535 ? undefined : new Error('端口需在 1-65535 之间'))
}
const formRules = computed<FormRules>(() => ({
  id: [{ required: true, message: '请输入关联员工 ID', trigger: 'blur' }],
  name: [{ required: true, message: '请输入关联员工名称', trigger: 'blur' }],
  role: [{ required: true, message: '请输入岗位', trigger: 'blur' }],
  'environment.name': [{ required: true, message: '请输入 Worker 名称', trigger: 'blur' }],
  'environment.opencode_path': [{ required: true, message: '请输入 Agent 可执行文件', trigger: 'blur' }],
  'environment.workspace_root': [{ required: true, message: '请输入工作空间', trigger: 'blur' }],
  'environment.opencode_server_port': [{ required: true, message: '请输入 OpenCode Server Port', trigger: 'blur' }, { validator: portValidator, trigger: 'blur' }],
  'environment.host': [{ required: true, message: '请输入 SSH Host', trigger: 'blur' }],
  'environment.port': [{ required: true, message: '请输入 SSH Port', trigger: 'blur' }, { validator: portValidator, trigger: 'blur' }],
  'environment.username': [{ required: true, message: '请输入 Username', trigger: 'blur' }],
  'environment.ssh_password': [{ required: props.creating || !props.employee?.execution_environment?.has_ssh_password, message: '请输入 SSH 密码', trigger: 'blur' }],
}))

const selectedEnvironment = computed(() => props.employee?.execution_environment ?? null)
const currentTaskCount = computed(() => props.employee?.current_task_count ?? 0)
const maintenance = useWorkerMaintenance(computed(() => selectedEnvironment.value?.id ?? null))
const maintenanceBusy = computed(() => maintenance.busyAction.value !== null || isMaintenanceActive(maintenance.operation.value?.state))
const maintenanceBlockedReason = computed(() => saving.value ? '正在保存配置，暂不能执行维护操作。' : isDirty.value ? '请先保存配置，再执行部署、更新、重启或禁用。' : '')
const paused = computed(() => props.employee?.status === 'PAUSED')
const serverAddress = computed(() => {
  const env = selectedEnvironment.value
  if (!env) return '—'
  return env.mode === 'LINUX_REMOTE' ? `${env.host ?? ''} → 127.0.0.1:${env.opencode_server_port}（SSH）` : `127.0.0.1:${env.opencode_server_port}`
})
const repositoryCopy = computed(() => repositorySummary(workerRepository(selectedEnvironment.value, maintenance.health.value)))

const EMPLOYEE_RUNTIME_LABEL: Record<string, string> = {
  IDLE: '空闲',
  WORKING: '执行中',
  WAIT_HUMAN: '等待输入',
  ERROR: '异常',
  PAUSED: '已暂停接单',
  OFFLINE: '离线',
}
const runtimeLabel = computed(() => EMPLOYEE_RUNTIME_LABEL[props.employee?.status ?? ''] ?? '未知')

function fill(employee?: Employee) {
  const environment = employee?.execution_environment
  Object.assign(form, {
    id: employee?.id ?? '', name: employee?.name ?? '', role: employee?.role ?? '', description: employee?.description ?? '',
    character_id: employee?.character_id ?? 0,
    default_work_area: employee?.default_work_area ?? 'development',
    task_types: [...(employee?.task_types ?? ['ISSUE', 'REQUIREMENT'])],
    category_ids: [...(employee?.category_ids ?? props.categories.filter((item) => item.enabled).map((item) => item.id))],
    enabled: employee?.enabled ?? true, auto_claim: employee?.auto_claim ?? true, auto_execute: employee?.auto_execute ?? true,
    environment: environment ? { mode: environment.mode, name: environment.name, description: environment.description, host: environment.host ?? '', port: environment.port, username: environment.username ?? '', ssh_password: '', opencode_server_port: environment.opencode_server_port ?? 14096, opencode_path: environment.opencode_path, model_name: environment.model_name ?? '', workspace_root: environment.workspace_root, labels: [...environment.labels] } : emptyEnvironment(),
  })
  message.value = ''; messageIsError.value = false
  originalSnapshot.value = snapshot()
  formRef.value?.clearValidate()
}

function snapshot(): string {
  return JSON.stringify({
    id: form.id, name: form.name, role: form.role, description: form.description, default_work_area: form.default_work_area,
    character_id: form.character_id,
    task_types: form.task_types, category_ids: form.category_ids, enabled: form.enabled, auto_claim: form.auto_claim, auto_execute: form.auto_execute,
    environment: { mode: form.environment.mode, name: form.environment.name, description: form.environment.description, host: form.environment.host ?? null, port: Number(form.environment.port), username: form.environment.username ?? null, opencode_server_port: Number(form.environment.opencode_server_port ?? 14096), opencode_path: form.environment.opencode_path, model_name: form.environment.model_name ?? null, workspace_root: form.environment.workspace_root, ssh_password: form.environment.ssh_password || null },
  })
}
const isDirty = computed(() => snapshot() !== originalSnapshot.value)
const busy = computed(() => saving.value || maintenanceBusy.value)

watch([() => props.employee?.id, () => props.creating], () => { fill(props.employee ?? undefined) }, { immediate: true })

function environmentPayload() {
  return { ...form.environment, host: form.environment.mode === 'LOCAL' ? null : form.environment.host || null, username: form.environment.mode === 'LOCAL' ? null : form.environment.username || null, ssh_password: form.environment.mode === 'LINUX_REMOTE' ? form.environment.ssh_password || null : null, model_name: form.environment.model_name || null }
}
function employeePayload() {
  return { name: form.name, role: form.role, description: form.description, character_id: form.character_id, default_work_area: form.default_work_area, task_types: form.task_types, category_ids: form.category_ids, enabled: form.enabled, auto_claim: form.auto_claim, auto_execute: form.auto_execute }
}
function normalizedEnvironment(value: ReturnType<typeof environmentPayload> | Employee['execution_environment']) {
  return value ? { mode: value.mode, name: value.name, description: value.description ?? '', host: value.host ?? null, port: Number(value.port), username: value.username ?? null, opencode_server_port: Number(value.opencode_server_port ?? 14096), opencode_path: value.opencode_path, model_name: value.model_name ?? null, workspace_root: value.workspace_root, labels: [...(value.labels ?? [])] } : null
}
function environmentChanged() {
  return Boolean(form.environment.ssh_password) || JSON.stringify(normalizedEnvironment(environmentPayload())) !== JSON.stringify(normalizedEnvironment(props.employee?.execution_environment ?? null))
}

async function save() {
  if (maintenanceBusy.value) return
  message.value = ''; messageIsError.value = false
  const valid = formRef.value ? await formRef.value.validate().catch(() => false) : true
  if (!valid) return
  if (!form.task_types.length || !form.category_ids.length) { message.value = '工作范围至少需要一种任务类型和一个任务分类'; messageIsError.value = true; return }
  saving.value = true
  try {
    let employeeId = props.employee?.id ?? ''
    if (props.creating) {
      const created = await api.createEmployee({ id: form.id, ...employeePayload(), execution_environment: environmentPayload() })
      employeeId = created.id
    } else {
      await api.updateEmployee(employeeId, employeePayload())
      if (environmentChanged()) await api.updateEnvironment(employeeId, environmentPayload())
    }
    await store.refresh()
    originalSnapshot.value = snapshot()
    messageIsError.value = false
    message.value = '配置已保存。请执行“检查”以读取 Worker 健康与装备状态。'
    emit('saved', employeeId)
  } catch (error) {
    messageIsError.value = true; message.value = error instanceof Error ? error.message : '保存失败'
  } finally { saving.value = false }
}

async function toggleRuntime() {
  if (!props.employee) return
  statusChanging.value = true; message.value = ''
  try {
    const action = paused.value ? 'resume' : 'pause'
    await api.employeeAction(props.employee.id, action)
    await store.refresh()
    message.value = action === 'resume' ? '关联员工已恢复接单。' : '关联员工已暂停接单，不会领取新任务。'
  } catch (error) { messageIsError.value = true; message.value = error instanceof Error ? error.message : '状态更新失败' } finally { statusChanging.value = false }
}

async function onMaintenanceChanged() {
  await store.refresh()
  if (isDirty.value) return
  const fresh = store.employees.find((item) => item.id === props.employee?.id)
  if (fresh) fill(fresh)
}

function reset() {
  fill(props.employee ?? undefined)
}

defineExpose({ getDirty: () => isDirty.value, dirty: isDirty, saving: busy, reset, save })
</script>

<style scoped>
.employee-config-form { width: 100%; }
.employee-character-field { width: 100%; }
.employee-character-options { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; width: 100%; }
.employee-character-options :deep(.el-radio) { display: flex; justify-content: center; height: auto; min-width: 0; margin: 0; padding: 8px 4px; border-radius: var(--el-border-radius-base); }
.employee-character-options :deep(.el-radio__input) { position: absolute; top: 8px; left: 8px; }
.employee-character-options :deep(.el-radio__label) { display: flex; flex-direction: column; align-items: center; gap: 2px; padding: 0; font-size: var(--astra-font-helper); }
.employee-character-options img { display: block; width: 64px; height: 80px; object-fit: contain; }
.employee-character-options :deep(.el-radio.is-checked) { background: var(--el-color-primary-light-9); }
.employee-character-help { margin: 8px 0 0; color: var(--el-text-color-secondary); font-size: var(--astra-font-helper); line-height: 1.6; }
@media (max-width: 600px) { .employee-character-options { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.maintenance-help { margin: 8px 0 0; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); line-height: 1.6; }
.employee-config-form :deep(.el-input), .employee-config-form :deep(.el-select) { width: 100%; }
.employee-config-form :deep(.el-checkbox-group) { display: flex; flex-wrap: wrap; gap: 4px 18px; }
.runtime-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; width: 100%; }
.runtime-row p { flex: 1; min-width: 0; margin: 0; color: var(--el-text-color-secondary); font-size: var(--el-font-size-extra-small); }
.static-value { color: var(--el-text-color-primary); overflow-wrap: anywhere; }
.form-message { margin-top: 4px; }
</style>
