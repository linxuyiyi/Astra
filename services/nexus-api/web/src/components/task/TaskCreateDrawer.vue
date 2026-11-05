<template>
  <ElDrawer
    v-if="open"
    class="task-create-drawer"
    data-testid="task-create-modal"
    :model-value="true"
    :title="task ? '编辑任务' : '创建任务'"
    size="600px"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :before-close="requestClose"
  >
    <ElForm
      :key="formVersion"
      ref="formRef"
      class="task-create-form"
      :model="form"
      :rules="formRules"
      label-position="left"
      label-width="160px"
      scroll-to-error
    >
      <ElDivider content-position="left">基本信息</ElDivider>

      <ElFormItem label="任务标题" prop="title">
        <ElInput v-model.trim="form.title" maxlength="240" placeholder="例如：修复用户登录接口偶发 500 问题" />
      </ElFormItem>
      <ElFormItem label="任务类型" prop="task_type">
        <ElRadioGroup v-model="form.task_type" :disabled="Boolean(task)">
          <ElRadioButton value="ISSUE">问题</ElRadioButton>
          <ElRadioButton value="REQUIREMENT">需求</ElRadioButton>
        </ElRadioGroup>
      </ElFormItem>
      <ElFormItem label="任务分类" prop="category_id">
        <ElSelect v-model="form.category_id" :disabled="Boolean(task)" placeholder="请选择分类">
          <ElOption v-for="item in store.categories" :key="item.id" :value="item.id" :label="item.name" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem label="优先级" prop="priority">
        <ElSelect v-model="form.priority">
          <ElOption v-for="item in priorities" :key="item" :value="item" :label="priorityLabel(item)" />
        </ElSelect>
      </ElFormItem>


      <ElDivider content-position="left">任务内容</ElDivider>

      <ElFormItem label="任务描述" prop="description">
        <ElInput v-model.trim="form.description" type="textarea" :rows="4" placeholder="任务描述" />
      </ElFormItem>
      <ElFormItem v-if="task?.attachments.length" label="已有附件">
        <ElText v-for="(item, index) in task.attachments" :key="index" type="info">{{ attachmentName(item) }}</ElText>
      </ElFormItem>
      <ElFormItem v-if="!task" label="附件">
        <ElUpload v-model:file-list="fileList" multiple :auto-upload="false" :on-change="onFileChange">
          <ElButton :icon="Paperclip" plain>上传附件</ElButton>
        </ElUpload>
      </ElFormItem>

      <ElDivider content-position="left">验收标准（可选）</ElDivider>

      <ElFormItem
        v-for="(item, index) in form.criteria"
        :key="item.key"
        :label="`验收标准 ${index + 1}`"
        :prop="`criteria.${index}.content`"
      >
        <div class="criterion-row">
          <ElInput v-model.trim="item.content" maxlength="500" :placeholder="`验收标准 ${index + 1}`" />
          <ElButton text bg :icon="ArrowUp" title="上移" :disabled="index === 0" :aria-label="`验收标准 ${index + 1} 上移`" @click="moveCriterion(index, -1)" />
          <ElButton text bg :icon="ArrowDown" title="下移" :disabled="index === form.criteria.length - 1" :aria-label="`验收标准 ${index + 1} 下移`" @click="moveCriterion(index, 1)" />
          <ElButton text bg type="danger" :icon="Trash2" title="删除" :aria-label="`删除验收标准 ${index + 1}`" @click="removeCriterion(index)" />
        </div>
      </ElFormItem>
      <ElFormItem>
        <ElButton :icon="Plus" plain @click="addCriterion">添加验收标准</ElButton>
      </ElFormItem>

      <ElDivider content-position="left">谁来执行，问谁</ElDivider>

      <ElFormItem prop="agent_worker_id">
        <template #label>执行 Worker</template>
        <ElSelect v-model="form.agent_worker_id" :disabled="Boolean(task && (task.status !== 'PENDING' || task.claimed_at))">
          <ElOption value="" label="自动分配" />
          <ElOption v-if="preservedWorker" :value="preservedWorker" :label="task?.worker_name || '当前 Worker 不可用'" />
          <ElOption v-for="item in compatibleEmployees" :key="item.id" :value="item.id" :label="`${workerDisplayName(item)}`" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem>
        <template #label>责任数字员工</template>
        <ElSelect v-model="form.origin_employee_id" clearable placeholder="可选，选择要询问进度的数字员工">
          <ElOption v-for="item in owners" :key="item.id" :value="item.id" :label="item.name" />
        </ElSelect>
      </ElFormItem>

      <ElAlert
        v-if="createError"
        data-testid="task-create-error"
        class="form-message"
        type="error"
        data-error="true"
        :closable="false"
        show-icon
        :title="createError"
      />
    </ElForm>

    <template #footer>
      <ElSpace :size="8">
        <ElTag v-if="createDirty" type="warning" effect="light">有未保存修改</ElTag>
        <ElButton :disabled="submitting" @click="requestClose">取消</ElButton>
        <ElButton type="primary" :loading="submitting" @click="submit">{{ task ? '保存修改' : '创建任务' }}</ElButton>
      </ElSpace>
    </template>
  </ElDrawer>
</template>

<script setup lang="ts">
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { ArrowDown, ArrowUp, Paperclip, Plus, Trash2 } from 'lucide-vue-next'
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { ElAlert, ElButton, ElDivider, ElDrawer, ElForm, ElFormItem, ElInput, ElOption, ElRadioButton, ElRadioGroup, ElSelect, ElSpace, ElTag, ElText, ElUpload } from 'element-plus'
import type { FormInstance, FormRules, UploadFile, UploadFiles, UploadUserFile } from 'element-plus'
import { api } from '@/api/client'
import type { Task, TaskPriority, TaskType } from '@/api/types'
import { listEmployees } from '../../api'
import type { Employee as NexusEmployee } from '../../types'
import { useFormGuard } from '@/composables/useFormGuard'
import { confirmDialog } from '@/composables/useConfirm'
import { useAstraStore } from '@/stores/astra'
import { priorityLabel, safeUUID } from '@/utils/format'
import { workerDisplayName } from '@/utils/workerName'
import { taskDraftSnapshot, type TaskDraftSnapshot } from '@/components/task/taskDraft'

const props = defineProps<{ open: boolean; task?: Task | null }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'created', id: string): void; (e: 'saved', id: string): void }>()
const store = useAstraStore()
const owners = ref<NexusEmployee[]>([])

const formRef = ref<FormInstance | null>(null)
const submitting = ref(false)
const createError = ref('')
const fileList = ref<UploadUserFile[]>([])

const priorities: TaskPriority[] = ['VERY_HIGH', 'HIGH', 'MEDIUM', 'LOW']
function criteriaFrom(task?: Task | null) {
  const source = task?.acceptance_criteria.length ? task.acceptance_criteria : [{ id: safeUUID(), content: '' }]
  return source.map((item) => ({ key: item.id || safeUUID(), content: item.content }))
}
function initialForm(task?: Task | null) {
  return {
    title: task?.title ?? '',
    task_type: (task?.task_type ?? 'ISSUE') as TaskType,
    category_id: task?.category_id ?? '',
    priority: (task?.priority ?? 'MEDIUM') as TaskPriority,
    description: task?.description ?? '',
    criteria: criteriaFrom(task),
    agent_worker_id: task?.agent_worker_id || '',
    origin_employee_id: task?.origin_employee_id || '',
  }
}
const form = reactive(initialForm())
const formVersion = ref(0)

function currentDraft(): TaskDraftSnapshot {
  return {
    title: form.title,
    task_type: form.task_type,
    category_id: form.category_id,
    priority: form.priority,
    description: form.description,
    criteria: form.criteria.map((item) => item.content),
    agent_worker_id: form.agent_worker_id || '',
    origin_employee_id: form.origin_employee_id || '',
    files: fileList.value.length,
  }
}

const formRules: FormRules = {
  title: [{ required: true, message: '请输入任务标题', trigger: 'blur' }],
  category_id: [{ required: true, message: '请选择任务分类', trigger: 'change' }],
  description: [{ required: true, message: '请输入任务描述', trigger: 'blur' }],
}

onMounted(async () => { owners.value = await listEmployees().catch(() => []) })

const compatibleEmployees = computed(() => store.employees.filter((item) =>
  item.enabled && item.task_types.includes(form.task_type) && item.category_ids.includes(form.category_id),
))
const preservedWorker = computed(() => {
  const id = props.task?.agent_worker_id
  return id && !compatibleEmployees.value.some(item => item.id === id) ? id : ''
})
watch(() => [form.task_type, form.category_id], () => {
  if (!props.task && form.agent_worker_id && !compatibleEmployees.value.some(item => item.id === form.agent_worker_id)) {
    form.agent_worker_id = ''
  }
})

const { dirty: createDirty, capture, canClose } = useFormGuard({
  isOpen: () => props.open,
  isSaving: () => submitting.value,
  snapshot: () => taskDraftSnapshot(currentDraft()),
  confirmDiscard: () => confirmDialog({
    title: props.task ? '放弃未保存的修改？' : '放弃未保存的任务草稿？',
    message: '关闭后将无法恢复未保存的内容。',
    confirmLabel: '放弃并关闭',
    cancelLabel: '继续编辑',
    danger: true,
  }),
})

watch([() => props.open, () => props.task?.id], ([open]) => {
  Object.assign(form, initialForm(open ? props.task : null))
  fileList.value = []
  createError.value = ''
  formVersion.value++
  capture()
}, { immediate: true })

async function requestClose() {
  if (!props.open) return true
  if (!await canClose()) return false
  emit('close')
  return true
}
onBeforeRouteLeave(() => requestClose())
onBeforeRouteUpdate((to, from) => to.params.id === from.params.id || requestClose())

function addCriterion() { form.criteria.push({ key: safeUUID(), content: '' }) }
function removeCriterion(index: number) { form.criteria.splice(index, 1) }
function moveCriterion(index: number, direction: number) {
  const target = index + direction
  if (target < 0 || target >= form.criteria.length) return
  const items = form.criteria
  ;[items[index], items[target]] = [items[target], items[index]]
  const paths = items.map((_, i) => `criteria.${i}.content`)
  nextTick(() => formRef.value?.clearValidate(paths))
}

const attachmentName = (item: Record<string, unknown>) => String(item.name || item.path || "附件")
const readFile = (file: File) => new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(reader.error); reader.readAsDataURL(file) })
function onFileChange(file: UploadFile, files: UploadFiles) {
  if (files.some((item) => item.uid !== file.uid && item.name === file.name && item.size === file.size)) {
    fileList.value = files.filter((item) => item.uid !== file.uid)
  }
}
function attachmentPayload() {
  return Promise.all(fileList.value.map(async (file) => {
    const raw = file.raw
    return { name: file.name, size: raw?.size ?? file.size ?? 0, content_type: raw?.type || 'application/octet-stream', content: raw ? await readFile(raw) : '' }
  }))
}

async function submit() {
  if (submitting.value) return
  submitting.value = true
  const version = formVersion.value
  const editingTask = props.task
  createError.value = ''
  try {
    if (!await formRef.value?.validate().catch(() => false) || version !== formVersion.value) return
    const acceptance_criteria = form.criteria.filter((item) => item.content.trim()).map((item) => ({ content: item.content }))
    if (editingTask) {
      const body: Record<string, unknown> = {
        title: form.title,
        description: form.description,
        priority: form.priority,
        acceptance_criteria,
        origin_employee_id: form.origin_employee_id || null,
      }
      if (editingTask.status === 'PENDING' && !editingTask.claimed_at && form.agent_worker_id !== (editingTask.agent_worker_id || '')) {
        body.agent_worker_id = form.agent_worker_id || null
      }
      const task = await api.updateTask(editingTask.id, body)
      if (version === formVersion.value) emit('saved', task.id)
      return
    }
    const task = await api.createTask({
      source: 'manual',
      title: form.title,
      task_type: form.task_type,
      category_id: form.category_id,
      priority: form.priority,
      description: form.description,
      acceptance_criteria,
      attachments: await attachmentPayload(),
      agent_worker_id: form.agent_worker_id || null,
      origin_employee_id: form.origin_employee_id || null,
    })
    if (version === formVersion.value) emit('created', task.id)
  } catch (error) {
    if (version === formVersion.value) createError.value = error instanceof Error ? error.message : '保存失败'
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.task-create-drawer :deep(.el-drawer__footer) { display: flex; align-items: center; justify-content: flex-end; }
.criterion-row { display: flex; align-items: center; gap: 6px; width: 100%; }
.criterion-row .el-input { flex: 1; min-width: 0; }
.form-message { margin-top: 4px; }
</style>
