<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { computeEmployeeDiff, hasChanges, type EmployeeFormValues } from '@/api/diff'
import { createEmployee, updateEmployee } from '@/api/employees'
import { errorMessage, fieldErrorsOf, isUncertainWrite } from '@/api/errors'
import { canSubmit } from '@/api/guards'
import type { Employee, EmployeeInput } from '@/api/types'
import ResourceMultiSelect from '@/components/ResourceMultiSelect.vue'
import {
  AVATARS,
  DEFAULT_AVATAR,
  DEFAULT_INSTRUCTIONS,
  DOMAIN_MAX_LENGTH,
  isKnownAvatar,
  NAME_MAX_LENGTH,
} from '@/constants'
import { loadAllResources } from '@/stores/resources'

const props = defineProps<{
  modelValue: boolean
  mode: 'create' | 'edit'
  employee: Employee | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  saved: [employee: Employee]
  refresh: []
}>()

const title = computed(() =>
  props.mode === 'create' ? '创建数字员工' : '编辑数字员工',
)

const form = reactive<EmployeeFormValues>({
  name: '',
  domain: '',
  responsibilities: '',
  instructions: '',
  avatar: DEFAULT_AVATAR,
  enabled: true,
  knowledge: [],
  skills: [],
  tools: [],
})

const fieldErrors = reactive<Record<string, string>>({})
const serverMessage = ref<string | null>(null)
// 写入结果未确认：保留草稿并锁定再次保存。已知 ID 时展示，未知不编造。
const uncertainWrite = ref<{ employeeId: string | null; message: string } | null>(
  null,
)
const saving = ref(false)

let initialSnapshot = ''

const hasFieldErrors = computed(() => Object.keys(fieldErrors).length > 0)
const submitBlocked = computed(
  () =>
    !canSubmit({ saving: saving.value, uncertain: uncertainWrite.value !== null }),
)

function snapshot(): string {
  return JSON.stringify({
    name: form.name,
    domain: form.domain,
    responsibilities: form.responsibilities,
    instructions: form.instructions,
    avatar: form.avatar,
    enabled: form.enabled,
    knowledge: [...form.knowledge],
    skills: [...form.skills],
    tools: [...form.tools],
  })
}

const dirty = computed(() => snapshot() !== initialSnapshot)

function clearErrors(): void {
  for (const key of Object.keys(fieldErrors)) delete fieldErrors[key]
  serverMessage.value = null
  uncertainWrite.value = null
}

function applyServerError(error: unknown): void {
  Object.assign(fieldErrors, fieldErrorsOf(error))
  serverMessage.value = errorMessage(error)
  uncertainWrite.value = isUncertainWrite(error)
    ? { employeeId: error.employeeId, message: errorMessage(error) }
    : null
}

function resetCreate(): void {
  form.name = ''
  form.domain = ''
  form.responsibilities = ''
  form.instructions = DEFAULT_INSTRUCTIONS
  form.avatar = DEFAULT_AVATAR
  form.enabled = true
  form.knowledge = []
  form.skills = []
  form.tools = []
}

function loadFromEmployee(employee: Employee): void {
  form.name = employee.name
  form.domain = employee.domain
  form.responsibilities = employee.responsibilities
  form.instructions = employee.instructions
  form.avatar = isKnownAvatar(employee.avatar) ? employee.avatar : DEFAULT_AVATAR
  form.enabled = employee.enabled
  form.knowledge = [...employee.knowledge]
  form.skills = [...employee.skills]
  form.tools = [...employee.tools]
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    clearErrors()
    if (props.mode === 'edit' && props.employee) {
      loadFromEmployee(props.employee)
    } else {
      resetCreate()
    }
    initialSnapshot = snapshot()
    void loadAllResources()
  },
)

function validate(): boolean {
  clearErrors()
  let valid = true
  const name = form.name.trim()
  const domain = form.domain.trim()

  if (!name) {
    fieldErrors.name = '必填'
    valid = false
  } else if (name.length > NAME_MAX_LENGTH) {
    fieldErrors.name = `最多 ${NAME_MAX_LENGTH} 个字符`
    valid = false
  }
  if (!domain) {
    fieldErrors.domain = '必填'
    valid = false
  } else if (domain.length > DOMAIN_MAX_LENGTH) {
    fieldErrors.domain = `最多 ${DOMAIN_MAX_LENGTH} 个字符`
    valid = false
  }
  if (!form.responsibilities.trim()) {
    fieldErrors.responsibilities = '必填'
    valid = false
  }
  if (!form.instructions.trim()) {
    fieldErrors.instructions = '必填'
    valid = false
  }
  if (!isKnownAvatar(form.avatar)) {
    fieldErrors.avatar = '必须从预设员工形象中选择'
    valid = false
  }
  return valid
}

function forceClose(): void {
  emit('update:modelValue', false)
}

async function submit(): Promise<void> {
  // 写入结果未确认或正在保存时，禁止再次提交（含重复点击 / 未知状态）。
  if (submitBlocked.value) return
  if (!validate()) return
  saving.value = true
  try {
    if (props.mode === 'create') {
      const payload: EmployeeInput = {
        name: form.name.trim(),
        domain: form.domain.trim(),
        responsibilities: form.responsibilities.trim(),
        instructions: form.instructions.trim(),
        avatar: form.avatar,
        enabled: form.enabled,
        knowledge: [...form.knowledge],
        skills: [...form.skills],
        tools: [...form.tools],
      }
      const created = await createEmployee(payload)
      ElMessage.success('员工已创建')
      emit('saved', created)
      forceClose()
    } else {
      if (!props.employee) return
      const patch = computeEmployeeDiff(props.employee, {
        name: form.name.trim(),
        domain: form.domain.trim(),
        responsibilities: form.responsibilities.trim(),
        instructions: form.instructions.trim(),
        avatar: form.avatar,
        enabled: form.enabled,
        knowledge: [...form.knowledge],
        skills: [...form.skills],
        tools: [...form.tools],
      })
      if (!hasChanges(patch)) {
        ElMessage.info('没有需要保存的修改')
        forceClose()
        return
      }
      const updated = await updateEmployee(props.employee.id, patch)
      ElMessage.success('员工已保存')
      emit('saved', updated)
      forceClose()
    }
  } catch (error) {
    applyServerError(error)
    if (uncertainWrite.value) {
      // 结果未确认：不动草稿、不自动重发非幂等写入；刷新列表以便核对。
      ElMessage.warning('写入结果未确认，已锁定保存。请刷新列表核对。')
      emit('refresh')
    } else if (hasFieldErrors.value) {
      ElMessage.error('请检查表单中标红的字段')
    } else {
      ElMessage.error(errorMessage(error))
    }
  } finally {
    saving.value = false
  }
}

async function confirmDiscard(): Promise<boolean> {
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm('表单有未保存的修改，关闭后将丢弃。', '放弃修改', {
      confirmButtonText: '放弃并关闭',
      cancelButtonText: '继续编辑',
      type: 'warning',
    })
    return true
  } catch {
    return false
  }
}

async function requestClose(): Promise<void> {
  if (saving.value) return
  if (await confirmDiscard()) forceClose()
}

function handleBeforeClose(done: (cancel?: boolean) => void): void {
  if (saving.value) {
    done(true)
    return
  }
  if (!dirty.value) {
    done()
    return
  }
  void confirmDiscard().then((ok) => {
    if (ok) done()
    else done(true)
  })
}

function onModelUpdate(value: boolean): void {
  emit('update:modelValue', value)
}
</script>

<template>
  <el-drawer
    :model-value="modelValue"
    :title="title"
    direction="rtl"
    size="610px"
    append-to-body
    class="employee-drawer"
    :before-close="handleBeforeClose"
    :close-on-click-modal="true"
    :close-on-press-escape="true"
    @update:model-value="onModelUpdate"
  >
    <div class="drawer-body">
      <el-form :model="form" label-position="top" @submit.prevent>
        <div class="form-cap">身份与职责</div>
        <div class="form-2">
          <el-form-item label="员工名称" :error="fieldErrors.name" required>
            <el-input
              v-model="form.name"
              maxlength="24"
              show-word-limit
              placeholder="例如：星河"
            />
          </el-form-item>
          <el-form-item label="责任领域" :error="fieldErrors.domain" required>
            <el-input
              v-model="form.domain"
              maxlength="42"
              show-word-limit
              placeholder="例如：测试自动化工程师"
            />
          </el-form-item>
        </div>

        <el-form-item label="员工形象" :error="fieldErrors.avatar">
          <el-select v-model="form.avatar" class="avatar-select">
            <el-option
              v-for="item in AVATARS"
              :key="item.value"
              :value="item.value"
              :label="item.label"
            >
              <span class="avatar-option">{{ item.value }} {{ item.label }}</span>
            </el-option>
          </el-select>
        </el-form-item>

        <el-form-item
          label="职责说明"
          :error="fieldErrors.responsibilities"
          required
        >
          <el-input
            v-model="form.responsibilities"
            type="textarea"
            :rows="3"
            placeholder="负责什么工作、处理什么问题、需要交付什么"
          />
        </el-form-item>

        <el-form-item
          label="工作指令"
          :error="fieldErrors.instructions"
          required
        >
          <el-input
            v-model="form.instructions"
            type="textarea"
            :rows="5"
            placeholder="定义什么时候直接回答、什么时候追问、什么时候联系其他员工"
          />
        </el-form-item>

        <div class="form-cap">知识与工具</div>
        <ResourceMultiSelect
          v-model="form.knowledge"
          field="knowledge"
          label="知识"
        />
        <ResourceMultiSelect v-model="form.skills" field="skills" label="技能" />
        <ResourceMultiSelect v-model="form.tools" field="mcps" label="工具" />

        <el-alert
          v-if="uncertainWrite"
          class="form-alert"
          type="warning"
          :closable="false"
          show-icon
          title="写入结果未确认"
        >
          <p>{{ uncertainWrite.message }}</p>
          <p v-if="uncertainWrite.employeeId">
            员工 ID：<span class="mono">{{ uncertainWrite.employeeId }}</span
            >。请刷新列表，对该员工点击「核对」确认结果。
          </p>
          <p v-else>
            服务端未返回员工 ID，结果可能未生效。请刷新列表核对，不要直接重复创建。
          </p>
          <p>已锁定保存，关闭抽屉后可刷新列表查看真实状态。</p>
        </el-alert>
        <el-alert
          v-else-if="serverMessage && !hasFieldErrors"
          class="form-alert"
          type="error"
          :closable="false"
          show-icon
          :title="serverMessage"
        />
      </el-form>
    </div>

    <template #footer>
      <div class="drawer-foot">
        <div class="foot-left">
          <el-switch v-model="form.enabled" />
          <span class="muted small">启用此员工</span>
        </div>
        <div class="foot-right">
          <el-button :disabled="saving" @click="requestClose">取消</el-button>
          <el-button
            type="primary"
            :loading="saving"
            :disabled="submitBlocked"
            @click="submit"
          >
            保存员工
          </el-button>
        </div>
      </div>
    </template>
  </el-drawer>
</template>

<style scoped>
.drawer-body {
  padding: 0 2px 8px;
}

.form-cap {
  margin: 16px 0 12px;
  color: #319875;
  font-size: 10px;
  font-weight: 750;
  letter-spacing: 1.4px;
}

.form-cap:first-child {
  margin-top: 0;
}

.form-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 13px;
}

.avatar-select {
  width: 100%;
}

.avatar-option {
  font-size: 14px;
}

.form-alert {
  margin-top: 8px;
}

.form-alert p {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.7;
}

.drawer-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.foot-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.foot-right {
  display: flex;
  gap: 9px;
}

@media (max-width: 650px) {
  .form-2 {
    grid-template-columns: 1fr;
  }
}
</style>
