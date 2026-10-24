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
        <ElInput v-model.trim="form.title" placeholder="例如：修复用户登录接口偶发 500 问题" />
      </ElFormItem>
      <ElFormItem label="任务类型" prop="task_type">
        <ElRadioGroup v-model="form.task_type">
          <ElRadioButton value="ISSUE">问题</ElRadioButton>
          <ElRadioButton value="REQUIREMENT">需求</ElRadioButton>
        </ElRadioGroup>
      </ElFormItem>
      <ElFormItem label="任务分类" prop="category_id">
        <ElSelect v-model="form.category_id" placeholder="请选择分类">
          <ElOption v-for="item in store.categories" :key="item.id" :value="item.id" :label="item.name" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem v-if="!task" label="代码仓库" prop="repository_id">
        <ElSelect v-model="form.repository_id" clearable filterable :loading="repositoriesLoading" placeholder="选择任务代码仓库">
          <ElOption v-for="item in repositories" :key="item.id" :value="item.id" :label="`${item.url} · ${item.branch}`" />
        </ElSelect>
        <ElAlert v-if="repositoriesError" class="form-message" type="error" :closable="false" :title="repositoriesError">
          <ElButton text @click="loadRepositories">重试</ElButton>
        </ElAlert>
      </ElFormItem>
      <ElFormItem label="优先级" prop="priority">
        <ElSelect v-model="form.priority">
          <ElOption v-for="item in priorities" :key="item" :value="item" :label="`${priorityLabel(item)}：${priorityHint(item)}`" />
        </ElSelect>
      </ElFormItem>


      <ElDivider content-position="left">任务内容</ElDivider>

      <ElFormItem label="任务描述" prop="description">
        <ElInput v-model.trim="form.description" type="textarea" :rows="4" placeholder="说明背景、当前情况、需要实现的能力和必要限制" />
      </ElFormItem>
      <ElFormItem v-if="task?.attachments.length" label="已有附件">
        <ElText v-for="(item, index) in task.attachments" :key="index" type="info">{{ attachmentName(item) }}</ElText>
      </ElFormItem>
      <ElFormItem v-if="!task" label="附件">
        <ElUpload v-model:file-list="fileList" multiple :auto-upload="false" :on-change="onFileChange">
          <ElButton :icon="Paperclip" plain>上传附件</ElButton>
          <template #tip><div class="el-upload__tip">可选，支持一次选择多个文件。</div></template>
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
          <ElInput v-model.trim="item.content" :placeholder="`验收标准 ${index + 1}`" />
          <ElButton text bg :icon="ArrowUp" title="上移" :disabled="index === 0" :aria-label="`验收标准 ${index + 1} 上移`" @click="moveCriterion(index, -1)" />
          <ElButton text bg :icon="ArrowDown" title="下移" :disabled="index === form.criteria.length - 1" :aria-label="`验收标准 ${index + 1} 下移`" @click="moveCriterion(index, 1)" />
          <ElButton text bg type="danger" :icon="Trash2" title="删除" :aria-label="`删除验收标准 ${index + 1}`" @click="removeCriterion(index)" />
        </div>
      </ElFormItem>
      <ElFormItem>
        <ElButton :icon="Plus" plain @click="addCriterion">添加验收标准</ElButton>
      </ElFormItem>

      <ElDivider content-position="left">谁来执行，问谁</ElDivider>

      <ElFormItem prop="employee_id">
        <template #label>执行 Worker <FieldHelp label="执行 Worker" help="自动分配会综合 Worker 启用状态、空闲状态、任务类型和任务分类；指定 Worker 时任务只由该执行资源处理。" /></template>
        <ElSelect v-model="form.employee_id" :disabled="Boolean(task && task.status !== 'PENDING')">
          <ElOption value="" label="自动分配" />
          <ElOption v-for="item in compatibleEmployees" :key="item.id" :value="item.id" :label="`${item.execution_environment?.name || employeeDisplayName(item.name)} · ${item.role}`" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem>
        <template #label>责任数字员工 <FieldHelp label="责任数字员工" help="责任数字员工是你在任务中心询问进度的对象；Agent Worker 是实际执行任务的运行实例，一个数字员工可以对应多个 Worker。" /></template>
        <ElSelect v-model="form.responsible_employee_id" clearable placeholder="可选，选择要询问进度的数字员工">
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
import { ArrowDown, ArrowUp, Paperclip, Plus, Trash2 } from 'lucide-vue-next'
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { ElAlert, ElButton, ElDivider, ElDrawer, ElForm, ElFormItem, ElInput, ElOption, ElRadioButton, ElRadioGroup, ElSelect, ElSpace, ElTag, ElText, ElUpload } from 'element-plus'
import type { FormInstance, FormRules, UploadFile, UploadFiles, UploadUserFile } from 'element-plus'
import { api } from '@/api/client'
import type { CodeRepository, Task, TaskPriority, TaskType } from '@/api/types'
import FieldHelp from '@/components/forms/FieldHelp.vue'
import { listEmployees } from '../../api'
import type { Employee as NexusEmployee } from '../../types'
import { confirmDialog } from '@/composables/useConfirm'
import { useAstraStore } from '@/stores/astra'
import { employeeDisplayName, priorityLabel, safeUUID } from '@/utils/format'
import { applyDefaultCategory, taskDraftSnapshot, type TaskDraftSnapshot } from '@/components/task/taskDraft'

const props = defineProps<{ open: boolean; task?: Task | null }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'created', id: string): void; (e: 'saved', id: string): void }>()
const store = useAstraStore()
const owners = ref<NexusEmployee[]>([])
const repositories = ref<CodeRepository[]>([])
const repositoriesLoading = ref(false)
const repositoriesError = ref('')

const formRef = ref<FormInstance | null>(null)
const submitting = ref(false)
const createError = ref('')
const fileList = ref<UploadUserFile[]>([])

const priorities: TaskPriority[] = ['VERY_HIGH', 'HIGH', 'MEDIUM', 'LOW']
const priorityHint = (value: TaskPriority) => ({ VERY_HIGH: '重要且紧急', HIGH: '重要但可以计划处理', MEDIUM: '有一定影响或时效要求', LOW: '一般优化或可延期事项' }[value])
function criteriaFrom(task?: Task | null) {
  const source = task?.acceptance_criteria.length ? task.acceptance_criteria : [{ id: safeUUID(), content: '' }]
  return source.map((item) => ({ key: item.id || safeUUID(), content: item.content }))
}
const form = reactive({
  title: props.task?.title ?? '',
  task_type: (props.task?.task_type ?? 'ISSUE') as TaskType,
  category_id: props.task?.category_id ?? '',
  repository_id: props.task?.repository_id ?? '',
  priority: (props.task?.priority ?? 'MEDIUM') as TaskPriority,
  description: props.task?.description ?? '',
  criteria: criteriaFrom(props.task),
  employee_id: props.task?.worker_id || props.task?.employee_id || '',
  responsible_employee_id: props.task?.responsible_employee_id || '',
})
const openedSnapshot = ref('')

function currentDraft(): TaskDraftSnapshot {
  return {
    title: form.title,
    task_type: form.task_type,
    category_id: form.category_id,
    repository_id: form.repository_id,
    priority: form.priority,
    description: form.description,
    criteria: form.criteria.map((item) => item.content),
    employee_id: form.employee_id || '',
    responsible_employee_id: form.responsible_employee_id || '',
    files: fileList.value.length,
  }
}

function syncBaseline() {
  const applied = applyDefaultCategory(
    currentDraft(),
    store.categories.map((item) => item.id),
    openedSnapshot.value,
  )
  if (applied.draft.category_id !== form.category_id) form.category_id = applied.draft.category_id
  openedSnapshot.value = applied.baseline
}

const formRules: FormRules = {
  title: [{ required: true, message: '请输入任务标题', trigger: 'blur' }],
  category_id: [{ required: true, message: '请选择任务分类', trigger: 'change' }],
  description: [{ required: true, message: '请输入任务描述', trigger: 'blur' }],
}

// 不预选第一项分类。列表晚到时只确立基线，不替用户选分类。
watch(() => store.categories.map((item) => item.id).join('\0'), () => syncBaseline())
onMounted(async () => {
  await nextTick()
  syncBaseline()
  if (!props.task) void loadRepositories()
  owners.value = await listEmployees().catch(() => [])
})

async function loadRepositories() {
  repositoriesLoading.value = true
  repositoriesError.value = ''
  try { repositories.value = await api.repositories() }
  catch (error) { repositoriesError.value = error instanceof Error ? error.message : '仓库列表加载失败' }
  finally { repositoriesLoading.value = false }
}

const compatibleEmployees = computed(() => store.employees.filter((item) => item.enabled && item.task_types.includes(form.task_type) && item.category_ids.includes(form.category_id)))
watch(() => [form.task_type, form.category_id], () => {
  if (form.employee_id && !compatibleEmployees.value.some((item) => item.id === form.employee_id)) form.employee_id = ''
})

const createDirty = computed(() => openedSnapshot.value !== '' && taskDraftSnapshot(currentDraft()) !== openedSnapshot.value)

// 创建表单是草稿：遮罩点击与 ESC 不关闭，只走显式取消/关闭，且有未保存内容时先二次确认。
// before-close 不调用 done()，关闭始终由父级把 open 置为 false 驱动。
async function requestClose() {
  if (submitting.value) return
  if (createDirty.value) {
    const confirmed = await confirmDialog({
      title: props.task ? '放弃未保存的修改？' : '放弃未保存的任务草稿？',
      message: props.task ? '任务修改尚未提交，关闭后将无法恢复。' : '创建任务的内容尚未提交，关闭后将无法恢复。',
      confirmLabel: '放弃并关闭',
      danger: true,
    })
    if (!confirmed) return
  }
  emit('close')
}

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
  const valid = formRef.value ? await formRef.value.validate().catch(() => false) : false
  if (!valid) return
  submitting.value = true
  createError.value = ''
  try {
    const acceptance_criteria = form.criteria.filter((item) => item.content.trim()).map((item) => ({ content: item.content }))
    if (props.task) {
      const body: Record<string, unknown> = {
        title: form.title,
        description: form.description,
        priority: form.priority,
        acceptance_criteria,
        responsible_employee_id: form.responsible_employee_id || null,
      }
      if (props.task.status === 'PENDING') body.employee_id = form.employee_id || null
      const task = await api.updateTask(props.task.id, body)
      emit('saved', task.id)
      return
    }
    const task = await api.createTask({
      source: 'manual',
      title: form.title,
      task_type: form.task_type,
      category_id: form.category_id,
      repository_id: form.repository_id || null,
      priority: form.priority,
      description: form.description,
      acceptance_criteria,
      attachments: await attachmentPayload(),
      employee_id: form.employee_id || null,
      responsible_employee_id: form.responsible_employee_id || null,
    })
    emit('created', task.id)
  } catch (error) {
    createError.value = error instanceof Error ? error.message : '创建失败'
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
