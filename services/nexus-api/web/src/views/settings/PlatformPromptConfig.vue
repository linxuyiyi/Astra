<template>
  <section class="admin-page platform-prompt-page" data-testid="platform-prompt-page">
    <SettingsHeaderActions>
      <ElButton :icon="RefreshCw" :loading="loading" data-testid="platform-prompt-refresh" @click="reload">刷新</ElButton>
      <ElButton
        type="primary"
        :icon="Save"
        :loading="saving"
        :disabled="!dirty || loading || Boolean(loadError)"
        data-testid="platform-prompt-save"
        @click="save"
      >保存</ElButton>
    </SettingsHeaderActions>

    <ElAlert
      v-if="loadError"
      type="error"
      show-icon
      :closable="false"
      :title="loadError"
      data-testid="platform-prompt-load-error"
    >
      <ElButton type="primary" data-testid="platform-prompt-retry" @click="reload">重试</ElButton>
    </ElAlert>

    <ElAlert
      type="info"
      show-icon
      :closable="false"
      title="全平台共用一份配置。保存只写入配置中心；数字员工 Agent 将在下次同步或新建 Session 前刷新，不会一点保存立刻改写全部既有会话。"
      data-testid="platform-prompt-hint"
    />

    <ElForm label-position="top" class="platform-prompt-form" @submit.prevent>
      <ElFormItem label="平台提示词">
        <ElInput
          v-model="draft"
          type="textarea"
          :rows="16"
          :disabled="loading || Boolean(loadError)"
          resize="vertical"
          placeholder="留空表示不注入平台块；首次可由后端种子默认文案。"
          data-testid="platform-prompt-editor"
        />
      </ElFormItem>
    </ElForm>

    <ElDivider content-position="left">最终提示词预览</ElDivider>

    <div class="platform-prompt-preview-bar" data-testid="platform-prompt-preview-bar">
      <ElSelect
        v-model="employeeId"
        filterable
        clearable
        placeholder="选择数字员工"
        class="platform-prompt-employee"
        data-testid="platform-prompt-employee"
        :loading="employeesLoading"
        @change="loadPreview"
      >
        <ElOption
          v-for="emp in employees"
          :key="emp.id"
          :label="emp.name"
          :value="emp.id"
        />
      </ElSelect>
      <ElButton
        :icon="RefreshCw"
        :loading="previewLoading"
        :disabled="!employeeId"
        data-testid="platform-prompt-preview-refresh"
        @click="loadPreview"
      >刷新预览</ElButton>
    </div>

    <ElAlert
      v-if="previewError"
      type="error"
      show-icon
      :closable="false"
      :title="previewError"
      data-testid="platform-prompt-preview-error"
    />

    <div v-if="preview" class="platform-prompt-preview" data-testid="platform-prompt-preview">
      <div class="platform-prompt-preview__block">
        <div class="platform-prompt-preview__label">平台块</div>
        <pre class="platform-prompt-preview__pre" data-testid="platform-prompt-preview-platform">{{ preview.platform || '（空：不注入平台块）' }}</pre>
      </div>
      <div class="platform-prompt-preview__block">
        <div class="platform-prompt-preview__label">员工业务块</div>
        <pre class="platform-prompt-preview__pre" data-testid="platform-prompt-preview-employee">{{ preview.employee || '（空）' }}</pre>
      </div>
      <div class="platform-prompt-preview__block">
        <div class="platform-prompt-preview__label">最终组合（与 Session 初始化同一函数）</div>
        <pre class="platform-prompt-preview__pre platform-prompt-preview__final" data-testid="platform-prompt-preview-final">{{ preview.final }}</pre>
      </div>
    </div>
    <ElEmpty
      v-else-if="!previewLoading && !previewError"
      :image-size="64"
      description="选择数字员工后查看平台提示词与业务配置的最终组合"
      data-testid="platform-prompt-preview-empty"
    />
  </section>
</template>

<script setup lang="ts">
import { RefreshCw, Save } from 'lucide-vue-next'
import { computed, onMounted, ref } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import {
  ElAlert,
  ElButton,
  ElDivider,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElMessage,
  ElOption,
  ElSelect,
} from 'element-plus'
import SettingsHeaderActions from './SettingsHeaderActions.vue'
import {
  getPlatformPrompt,
  listEmployees,
  previewPlatformPrompt,
  savePlatformPrompt,
  type PlatformPromptPreview,
} from '@/api'
import type { Employee } from '@/types'

const draft = ref('')
const saved = ref('')
const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
const employees = ref<Employee[]>([])
const employeesLoading = ref(false)
const employeeId = ref('')
const preview = ref<PlatformPromptPreview | null>(null)
const previewLoading = ref(false)
const previewError = ref('')

const dirty = computed(() => draft.value !== saved.value)

async function reload() {
  loading.value = true
  loadError.value = ''
  try {
    const payload = await getPlatformPrompt()
    draft.value = payload.text ?? ''
    saved.value = draft.value
  } catch (reason) {
    loadError.value = reason instanceof Error ? reason.message : '加载平台提示词失败'
    draft.value = ''
    saved.value = ''
  } finally {
    loading.value = false
  }
}

async function loadEmployees() {
  employeesLoading.value = true
  try {
    employees.value = await listEmployees()
  } catch {
    employees.value = []
  } finally {
    employeesLoading.value = false
  }
}

async function loadPreview() {
  previewError.value = ''
  preview.value = null
  if (!employeeId.value) return
  previewLoading.value = true
  try {
    preview.value = await previewPlatformPrompt(employeeId.value)
  } catch (reason) {
    previewError.value = reason instanceof Error ? reason.message : '预览加载失败'
  } finally {
    previewLoading.value = false
  }
}

async function save() {
  if (!dirty.value || loadError.value) return
  saving.value = true
  try {
    const payload = await savePlatformPrompt(draft.value)
    draft.value = payload.text ?? draft.value
    saved.value = draft.value
    ElMessage.success('已保存。Agent 将在下次同步或新建 Session 前刷新。')
    if (employeeId.value) await loadPreview()
  } catch (reason) {
    ElMessage.error(reason instanceof Error ? reason.message : '保存失败')
  } finally {
    saving.value = false
  }
}

onBeforeRouteLeave(() => {
  if (!dirty.value) return true
  return window.confirm('有未保存的平台提示词修改，确定离开？')
})

onMounted(async () => {
  await Promise.all([reload(), loadEmployees()])
})
</script>

<style scoped>
.platform-prompt-page {
  max-width: 960px;
}
.platform-prompt-form :deep(.el-textarea__inner) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 13px;
  line-height: 1.5;
}
.platform-prompt-preview-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.platform-prompt-employee {
  width: min(320px, 100%);
}
.platform-prompt-preview {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.platform-prompt-preview__label {
  margin-bottom: 4px;
  color: var(--astra-text-muted, #6b7280);
  font-size: 12px;
  font-weight: 600;
}
.platform-prompt-preview__pre {
  margin: 0;
  padding: 12px;
  border: 1px solid var(--astra-border, #e5e7eb);
  border-radius: 8px;
  background: var(--astra-surface-muted, #f8fafc);
  color: var(--astra-text-primary, #111827);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 280px;
  overflow: auto;
}
.platform-prompt-preview__final {
  max-height: 420px;
  background: #fff;
}
</style>
