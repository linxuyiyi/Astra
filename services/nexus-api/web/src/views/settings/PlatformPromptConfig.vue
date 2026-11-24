<template>
  <section class="admin-page pp-page" data-testid="platform-prompt-page">
    <SettingsHeaderActions>
      <ElTag v-if="dirty" size="small" type="warning" effect="light">有未保存修改</ElTag>
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

    <div class="pp-workbench">
      <!-- 左侧：平台提示词编辑器 -->
      <section class="pp-panel">
        <header class="pp-panel__head">
          <div>
            <h3>平台全局提示词</h3>
            <span class="pp-panel__sub">{{ draft.length }} 字 · {{ lineCount }} 行</span>
          </div>
          <ElRadioGroup v-model="editorMode" size="small" aria-label="编辑视图">
            <ElRadioButton value="edit">编辑</ElRadioButton>
            <ElRadioButton value="preview">预览</ElRadioButton>
          </ElRadioGroup>
        </header>

        <div class="pp-panel__body">
          <ElInput
            v-if="editorMode === 'edit'"
            v-model="draft"
            class="pp-editor"
            type="textarea"
            :rows="20"
            :disabled="loading || Boolean(loadError)"
            resize="none"
            placeholder="输入全局注入的系统提示词（支持 Markdown），留空则不注入"
            data-testid="platform-prompt-editor"
          />
          <div v-else class="pp-preview-pane">
            <MarkdownView :content="draft" compact empty-text="当前未配置平台提示词" />
          </div>
        </div>

        <footer class="pp-panel__foot" data-testid="platform-prompt-hint">
          保存后在下次同步或新建 Session 时自动生效
        </footer>
      </section>

      <!-- 右侧：员工最终生效提示词实时预览 -->
      <section class="pp-panel">
        <header class="pp-panel__head">
          <div>
            <h3>员工生效预览</h3>
            <span class="pp-panel__sub">查看平台块与员工指令合并后的实际效果</span>
          </div>
          <div class="pp-preview-tools" data-testid="platform-prompt-preview-bar">
            <ElSelect
              v-model="employeeId"
              size="small"
              filterable
              clearable
              placeholder="选择数字员工"
              class="pp-employee-select"
              data-testid="platform-prompt-employee"
              :loading="employeesLoading"
              @change="loadPreview"
            >
              <ElOption v-for="emp in employees" :key="emp.id" :label="emp.name" :value="emp.id" />
            </ElSelect>
            <ElButton
              size="small"
              :icon="RefreshCw"
              :loading="previewLoading"
              :disabled="!employeeId"
              data-testid="platform-prompt-preview-refresh"
              @click="loadPreview"
            />
          </div>
        </header>

        <ElAlert
          v-if="previewError"
          class="pp-preview-error"
          type="error"
          show-icon
          :closable="false"
          :title="previewError"
          data-testid="platform-prompt-preview-error"
        />

        <div v-if="preview" class="pp-panel__body pp-preview-wrap" data-testid="platform-prompt-preview">
          <div class="pp-preview-tabs">
            <ElRadioGroup v-model="previewTab" size="small" aria-label="提示词片段">
              <ElRadioButton value="final">最终生效</ElRadioButton>
              <ElRadioButton value="platform">平台块</ElRadioButton>
              <ElRadioButton value="employee">员工业务块</ElRadioButton>
            </ElRadioGroup>
            <div class="pp-preview-mode">
              <ElRadioGroup v-model="previewRenderMode" size="small" aria-label="展示格式">
                <ElRadioButton value="rendered">阅读</ElRadioButton>
                <ElRadioButton value="source">源码</ElRadioButton>
              </ElRadioGroup>
              <ElButton size="small" text :icon="copiedPreview ? Check : Copy" :disabled="!activePreviewText" @click="copyPreview">
                {{ copiedPreview ? '已复制' : '复制' }}
              </ElButton>
            </div>
          </div>
          <div
            class="pp-preview-content"
            :data-testid="`platform-prompt-preview-${previewTab}`"
          >
            <pre v-if="previewRenderMode === 'source'" class="pp-source-pre">{{ activePreviewText || activePreviewEmpty }}</pre>
            <MarkdownView
              v-else
              :content="activePreviewText"
              compact
              :empty-text="activePreviewEmpty"
            />
          </div>
        </div>

        <div v-else-if="!previewLoading && !previewError" class="pp-empty" data-testid="platform-prompt-preview-empty">
          <ElEmpty :image-size="56" description="在右上角选择数字员工，即可预览合并后的最终提示词" />
        </div>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { Check, Copy, RefreshCw, Save } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import {
  ElAlert,
  ElButton,
  ElEmpty,
  ElInput,
  ElMessage,
  ElOption,
  ElRadioButton,
  ElRadioGroup,
  ElSelect,
  ElTag,
} from 'element-plus'
import MarkdownView from '@/components/astra/MarkdownView.vue'
import { confirmDialog } from '@/composables/useConfirm'
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
const editorMode = ref<'edit' | 'preview'>('edit')

const employees = ref<Employee[]>([])
const employeesLoading = ref(false)
const employeeId = ref('')
const preview = ref<PlatformPromptPreview | null>(null)
const previewTab = ref<'final' | 'platform' | 'employee'>('final')
const previewRenderMode = ref<'rendered' | 'source'>('rendered')
const copiedPreview = ref(false)
let copyTimer = 0
const previewLoading = ref(false)
const previewError = ref('')

async function copyPreview() {
  if (!activePreviewText.value) return
  try {
    await navigator.clipboard.writeText(activePreviewText.value)
    copiedPreview.value = true
    window.clearTimeout(copyTimer)
    copyTimer = window.setTimeout(() => { copiedPreview.value = false }, 1600)
  } catch {
    copiedPreview.value = false
  }
}
onBeforeUnmount(() => window.clearTimeout(copyTimer))

const dirty = computed(() => draft.value !== saved.value)
const lineCount = computed(() => (draft.value ? draft.value.split('\n').length : 0))

const activePreviewText = computed(() => {
  if (!preview.value) return ''
  if (previewTab.value === 'platform') return preview.value.platform || ''
  if (previewTab.value === 'employee') return preview.value.employee || ''
  return preview.value.final || ''
})

const activePreviewEmpty = computed(() => {
  if (previewTab.value === 'platform') return '平台块为空（不注入平台提示词）'
  if (previewTab.value === 'employee') return '该员工未配置业务指令'
  return '最终提示词为空'
})

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
    ElMessage.success('平台提示词已保存')
    if (employeeId.value) await loadPreview()
  } catch (reason) {
    ElMessage.error(reason instanceof Error ? reason.message : '保存失败')
  } finally {
    saving.value = false
  }
}

onBeforeRouteLeave(async () => {
  if (!dirty.value) return true
  return confirmDialog({
    title: '放弃未保存修改？',
    message: '平台提示词尚未保存，离开后修改将丢失。',
    confirmLabel: '放弃并离开',
    cancelLabel: '继续编辑',
    danger: true,
  })
})

onMounted(async () => {
  await Promise.all([reload(), loadEmployees()])
})
</script>

<style scoped>
.pp-page { display: flex; flex-direction: column; gap: 12px; height: 100%; min-height: 0; }
.pp-workbench {
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(0, 1fr);
  gap: 14px;
  flex: 1 1 auto;
  min-height: 540px;
}

.pp-panel {
  display: flex; flex-direction: column; min-width: 0; min-height: 0;
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-panel);
  overflow: hidden;
}
.pp-panel__head {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--astra-border-subtle);
  background: var(--astra-bg-subtle);
}
.pp-panel__head h3 { margin: 0; color: var(--astra-text-primary); font-size: 14px; font-weight: 600; }
.pp-panel__sub { display: block; margin-top: 2px; color: var(--astra-text-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.pp-panel__body { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
.pp-panel__foot {
  padding: 8px 16px;
  border-top: 1px solid var(--astra-border-subtle);
  background: var(--astra-bg-subtle);
  color: var(--astra-text-muted);
  font-size: 12px;
}

.pp-editor { flex: 1; display: flex; }
.pp-editor :deep(.el-textarea__inner) {
  flex: 1; height: 100% !important; min-height: 440px;
  border: 0; border-radius: 0; box-shadow: none !important;
  padding: 14px 16px;
  font-family: var(--astra-font-mono);
  font-size: 13px; line-height: 1.65;
}
.pp-preview-pane { flex: 1; overflow-y: auto; padding: 14px 16px; }

.pp-preview-tools { display: flex; align-items: center; gap: 6px; }
.pp-employee-select { width: 176px; }
.pp-preview-error { margin: 12px 16px 0; }
.pp-preview-wrap { display: flex; flex-direction: column; min-height: 0; }
.pp-preview-tabs {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;
  padding: 10px 16px; border-bottom: 1px solid var(--astra-border-subtle); flex: none;
}
.pp-preview-mode { display: flex; align-items: center; gap: 6px; }
.pp-preview-content { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 16px 16px; }
.pp-source-pre {
  margin: 0; padding: 12px 14px;
  border: 1px solid var(--astra-border); border-radius: 8px; background: var(--astra-bg-subtle);
  color: var(--astra-text-primary); font-family: var(--astra-font-mono); font-size: 12.5px; line-height: 1.65;
  white-space: pre-wrap; word-break: break-word;
}
.pp-empty { flex: 1; display: grid; place-items: center; padding: 24px; }

@media (max-width: 1060px) {
  .pp-workbench { grid-template-columns: 1fr; }
}
</style>
