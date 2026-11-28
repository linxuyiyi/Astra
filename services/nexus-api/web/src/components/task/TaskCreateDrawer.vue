<template>
  <ElDrawer
    v-if="open"
    class="task-create-drawer"
    data-testid="task-create-modal"
    :model-value="true"
    :title="task ? '编辑任务' : '创建任务'"
    size="var(--astra-drawer-md)"
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
      label-position="top"
      scroll-to-error
    >
      <div class="section-cap">基础信息</div>

      <ElFormItem label="任务标题" prop="title">
        <ElInput
          v-model.trim="form.title"
          maxlength="240"
          placeholder="例如：修复用户登录接口偶发 500 问题"
        />
      </ElFormItem>

      <div class="form-pair">
        <ElFormItem label="任务类型" prop="task_type">
          <ElRadioGroup v-model="form.task_type" class="full-segment" :disabled="Boolean(task)">
            <ElRadioButton value="ISSUE">问题</ElRadioButton>
            <ElRadioButton value="REQUIREMENT">需求</ElRadioButton>
          </ElRadioGroup>
        </ElFormItem>

        <ElFormItem label="优先级" prop="priority">
          <ElRadioGroup v-model="form.priority" class="full-segment">
            <ElRadioButton v-for="item in priorities" :key="item" :value="item">
              {{ priorityLabel(item) }}
            </ElRadioButton>
          </ElRadioGroup>
        </ElFormItem>
      </div>

      <div class="form-pair">
        <ElFormItem label="任务分类" prop="category_id">
          <ElSelect
            v-model="form.category_id"
            filterable
            :disabled="Boolean(task)"
            placeholder="选择任务分类"
            style="width: 100%"
          >
            <ElOption v-for="item in store.categories" :key="item.id" :value="item.id" :label="item.name" />
          </ElSelect>
        </ElFormItem>

        <ElFormItem label="执行 Worker" prop="agent_worker_id">
          <ElSelect
            v-model="form.agent_worker_id"
            filterable
            :disabled="Boolean(task && (task.status !== 'PENDING' || task.claimed_at))"
            style="width: 100%"
          >
            <ElOption value="" label="自动分配" />
            <ElOption v-if="preservedWorker" :value="preservedWorker" :label="task?.worker_name || '当前 Worker 不可用'" />
            <ElOption v-for="item in compatibleEmployees" :key="item.id" :value="item.id" :label="workerDisplayName(item)" />
          </ElSelect>
        </ElFormItem>
      </div>

      <div class="section-cap">任务内容</div>

      <ElFormItem label="任务描述" prop="description">
        <div class="md-editor">
          <div class="md-editor__bar">
            <span class="md-editor__meta">支持 Markdown · {{ form.description.length }} 字</span>
            <ElRadioGroup v-model="descMode" size="small" aria-label="任务描述视图">
              <ElRadioButton value="edit">编辑</ElRadioButton>
              <ElRadioButton value="preview">预览</ElRadioButton>
            </ElRadioGroup>
          </div>
          <ElInput
            v-if="descMode === 'edit'"
            v-model="form.description"
            class="md-editor__input"
            type="textarea"
            :rows="7"
            resize="vertical"
            placeholder="描述背景、复现路径或目标要求（支持 Markdown 语法）"
          />
          <div v-else class="md-editor__preview">
            <MarkdownView :content="form.description" compact empty-text="暂无任务描述" />
          </div>
        </div>
      </ElFormItem>

      <div class="criteria-block">
        <div class="criteria-block__head">
          <span class="criteria-block__label">验收标准</span>
          <ElButton text type="primary" :icon="Plus" @click="addCriterion">添加标准</ElButton>
        </div>

        <ElFormItem
          v-for="(item, index) in form.criteria"
          :key="item.key"
          class="criteria-form-item"
          :prop="`criteria.${index}.content`"
        >
          <div class="criterion-row">
            <span class="criterion-index">{{ String(index + 1).padStart(2, '0') }}</span>
            <ElInput
              v-model.trim="item.content"
              maxlength="2000"
              :placeholder="`验收标准 ${index + 1}`"
            />
            <ElButton
              text
              bg
              :icon="ArrowUp"
              title="上移"
              :disabled="index === 0"
              :aria-label="`验收标准 ${index + 1} 上移`"
              @click="moveCriterion(index, -1)"
            />
            <ElButton
              text
              bg
              :icon="ArrowDown"
              title="下移"
              :disabled="index === form.criteria.length - 1"
              :aria-label="`验收标准 ${index + 1} 下移`"
              @click="moveCriterion(index, 1)"
            />
            <ElButton
              text
              bg
              type="danger"
              :icon="Trash2"
              title="删除"
              :aria-label="`删除验收标准 ${index + 1}`"
              @click="removeCriterion(index)"
            />
          </div>
        </ElFormItem>
      </div>

      <div class="section-cap">协作与附件</div>

      <div class="form-pair">
        <ElFormItem label="责任数字员工">
          <ElSelect
            v-model="form.origin_employee_id"
            clearable
            filterable
            placeholder="可选"
            style="width: 100%"
          >
            <ElOption v-for="item in owners" :key="item.id" :value="item.id" :label="item.name" />
          </ElSelect>
        </ElFormItem>

        <ElFormItem v-if="!task" label="附件">
          <ElUpload
            v-model:file-list="fileList"
            class="task-upload"
            multiple
            :auto-upload="false"
            :on-change="onFileChange"
          >
            <ElButton :icon="Paperclip" plain>添加附件</ElButton>
          </ElUpload>
        </ElFormItem>

        <ElFormItem v-else-if="task.attachments.length" label="已有附件">
          <div class="existing-attachments">
            <ElTag
              v-for="(item, index) in task.attachments"
              :key="index"
              size="small"
              type="info"
              class="astra-tag--neutral"
            >
              {{ attachmentName(item) }}
            </ElTag>
          </div>
        </ElFormItem>
      </div>

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
      <div class="drawer-footer">
        <ElTag v-if="createDirty" type="warning" effect="light">有未保存修改</ElTag>
        <span v-else />
        <ElSpace :size="8">
          <ElButton :disabled="submitting" @click="requestClose">取消</ElButton>
          <ElButton type="primary" :loading="submitting" @click="submit">{{ task ? '保存修改' : '创建任务' }}</ElButton>
        </ElSpace>
      </div>
    </template>
  </ElDrawer>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { ArrowDown, ArrowUp, Paperclip, Plus, Trash2 } from 'lucide-vue-next'
import {
  ElAlert,
  ElButton,
  ElDrawer,
  ElForm,
  ElFormItem,
  ElInput,
  ElOption,
  ElRadioButton,
  ElRadioGroup,
  ElSelect,
  ElSpace,
  ElTag,
  ElUpload,
} from 'element-plus'
import type { FormInstance, FormRules, UploadFile, UploadFiles, UploadUserFile } from 'element-plus'
import { api } from '@/api/client'
import type { Task, TaskPriority, TaskType } from '@/api/types'
import MarkdownView from '@/components/astra/MarkdownView.vue'
import { taskDraftSnapshot, type TaskDraftSnapshot } from '@/components/task/taskDraft'
import { confirmDialog } from '@/composables/useConfirm'
import { useFormGuard } from '@/composables/useFormGuard'
import { useAstraStore } from '@/stores/astra'
import { priorityLabel, safeUUID } from '@/utils/format'
import { workerDisplayName } from '@/utils/workerName'
import { listEmployees } from '../../api'
import type { Employee as NexusEmployee } from '../../types'

const props = defineProps<{ open: boolean; task?: Task | null }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'created', id: string): void; (e: 'saved', id: string): void }>()
const store = useAstraStore()
const owners = ref<NexusEmployee[]>([])

const formRef = ref<FormInstance | null>(null)
const submitting = ref(false)
const createError = ref('')
const fileList = ref<UploadUserFile[]>([])
const descMode = ref<'edit' | 'preview'>('edit')

const priorities: TaskPriority[] = ['VERY_HIGH', 'HIGH', 'MEDIUM', 'LOW']

function criteriaFrom(task?: Task | null) {
  const source = task?.acceptance_criteria.length ? task.acceptance_criteria : [{ id: undefined, content: '' }]
  return source.map(({ id, content }) => ({ key: safeUUID(), id, content }))
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
  description: [
    { required: true, message: '请输入任务描述', trigger: 'blur' },
    {
      validator: (_rule: unknown, value: string, callback: (err?: Error) => void) => {
        if (!value || !value.trim()) callback(new Error('请输入任务描述'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

onMounted(async () => {
  owners.value = await listEmployees().catch(() => [])
})

const compatibleEmployees = computed(() =>
  store.employees.filter(
    (item) =>
      item.enabled && item.task_types.includes(form.task_type) && item.category_ids.includes(form.category_id),
  ),
)

const preservedWorker = computed(() => {
  const id = props.task?.agent_worker_id
  return id && !compatibleEmployees.value.some((item) => item.id === id) ? id : ''
})

watch(
  () => [form.task_type, form.category_id],
  () => {
    if (
      !props.task &&
      form.agent_worker_id &&
      !compatibleEmployees.value.some((item) => item.id === form.agent_worker_id)
    ) {
      form.agent_worker_id = ''
    }
  },
)

const { dirty: createDirty, capture, canClose } = useFormGuard({
  isOpen: () => props.open,
  isSaving: () => submitting.value,
  snapshot: () => taskDraftSnapshot(currentDraft()),
  confirmDiscard: () =>
    confirmDialog({
      title: props.task ? '放弃未保存的修改？' : '放弃未保存的任务草稿？',
      message: '关闭后将无法恢复未保存的内容。',
      confirmLabel: '放弃并关闭',
      cancelLabel: '继续编辑',
      danger: true,
    }),
})

watch(
  [() => props.open, () => props.task?.id],
  ([open]) => {
    Object.assign(form, initialForm(open ? props.task : null))
    fileList.value = []
    createError.value = ''
    descMode.value = 'edit'
    formVersion.value++
    capture()
  },
  { immediate: true },
)

async function requestClose() {
  if (!props.open) return true
  if (!(await canClose())) return false
  emit('close')
  return true
}

onBeforeRouteLeave(() => requestClose())
onBeforeRouteUpdate((to, from) => to.params.id === from.params.id || requestClose())

function addCriterion() {
  form.criteria.push({ key: safeUUID(), id: undefined, content: '' })
}

function removeCriterion(index: number) {
  form.criteria.splice(index, 1)
}

function moveCriterion(index: number, direction: number) {
  const target = index + direction
  if (target < 0 || target >= form.criteria.length) return
  const items = form.criteria
  ;[items[index], items[target]] = [items[target], items[index]]
  const paths = items.map((_, i) => `criteria.${i}.content`)
  nextTick(() => formRef.value?.clearValidate(paths))
}

const attachmentName = (item: Record<string, unknown>) => String(item.name || item.path || '附件')

const readFile = (file: File) =>
  new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })

function onFileChange(file: UploadFile, files: UploadFiles) {
  if (files.some((item) => item.uid !== file.uid && item.name === file.name && item.size === file.size)) {
    fileList.value = files.filter((item) => item.uid !== file.uid)
  }
}

function attachmentPayload() {
  return Promise.all(
    fileList.value.map(async (file) => {
      const raw = file.raw
      return {
        name: file.name,
        size: raw?.size ?? file.size ?? 0,
        content_type: raw?.type || 'application/octet-stream',
        content: raw ? await readFile(raw) : '',
      }
    }),
  )
}

async function submit() {
  if (submitting.value) return
  submitting.value = true
  const version = formVersion.value
  const editingTask = props.task
  createError.value = ''
  try {
    if (!form.description.trim() && descMode.value === 'preview') descMode.value = 'edit'
    if (!(await formRef.value?.validate().catch(() => false)) || version !== formVersion.value) return
    const acceptance_criteria = form.criteria
      .filter((item) => item.content.trim())
      .map((item) => ({ id: item.id, content: item.content }))
    const description = form.description.trim()
    if (editingTask) {
      const body: Record<string, unknown> = {
        title: form.title,
        description,
        priority: form.priority,
        acceptance_criteria,
        origin_employee_id: form.origin_employee_id || null,
      }
      if (
        editingTask.status === 'PENDING' &&
        !editingTask.claimed_at &&
        form.agent_worker_id !== (editingTask.agent_worker_id || '')
      ) {
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
      description,
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
.task-create-form {
  display: grid;
  gap: 2px;
}

.section-cap {
  margin: 6px 0 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--astra-border-subtle);
  color: var(--astra-text-muted);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.section-cap:first-child {
  margin-top: 0;
}

.form-pair {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.full-segment {
  display: flex;
  width: 100%;
}

.full-segment :deep(.el-radio-button) {
  flex: 1;
}

.full-segment :deep(.el-radio-button__inner) {
  width: 100%;
  padding-left: 8px;
  padding-right: 8px;
}

/* ── Markdown 编辑/预览框 ───────────────────────────────────────────── */
.md-editor {
  display: grid;
  width: 100%;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  overflow: hidden;
  background: var(--astra-bg-panel);
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.md-editor:focus-within {
  border-color: var(--astra-primary);
  box-shadow: 0 0 0 3px var(--astra-primary-glow);
}

.md-editor__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--astra-border);
  background: var(--astra-bg-subtle);
}

.md-editor__meta {
  color: var(--astra-text-muted);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.md-editor__input :deep(.el-textarea__inner) {
  border: 0;
  border-radius: 0;
  box-shadow: none !important;
  padding: 10px 12px;
  font-family: var(--astra-font-mono);
  font-size: 12.5px;
  line-height: 1.65;
}

.md-editor__preview {
  min-height: 168px;
  max-height: 280px;
  overflow-y: auto;
  padding: 12px 14px;
}

/* ── 验收标准紧凑列表 ───────────────────────────────────────────────── */
.criteria-block {
  display: grid;
  gap: 6px;
  margin-bottom: 12px;
}

.criteria-block__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.criteria-block__label {
  color: var(--astra-text-secondary);
  font-size: var(--el-font-size-base);
  font-weight: 500;
}

.criteria-form-item {
  margin-bottom: 6px;
}

.criterion-row {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
}

.criterion-index {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  width: 26px;
  height: 26px;
  border: 1px solid var(--astra-border);
  border-radius: 6px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-muted);
  font-family: var(--astra-font-mono);
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.criterion-row .el-input {
  flex: 1;
  min-width: 0;
}

.criterion-row :deep(.el-button + .el-button) {
  margin-left: 0;
}

.task-upload {
  width: 100%;
}

.existing-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.form-message {
  margin-top: 6px;
}

.drawer-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}

@media (min-width: 2240px) {
  .md-editor__preview {
    max-height: 380px;
  }
}

@media (max-width: 640px) {
  .form-pair {
    grid-template-columns: 1fr;
  }
}
</style>
