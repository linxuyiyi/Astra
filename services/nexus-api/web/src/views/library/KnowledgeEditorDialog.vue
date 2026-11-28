<template>
  <ElDrawer
    v-if="open"
    class="knowledge-editor-drawer"
    data-testid="knowledge-editor-drawer"
    :model-value="true"
    :title="editing ? '编辑知识' : '新建知识'"
    size="var(--astra-drawer-xl)"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :before-close="requestClose"
  >
    <ElForm
      ref="formRef"
      class="ke-form"
      :model="form"
      :rules="rules"
      label-position="top"
      scroll-to-error
    >
      <ElFormItem label="标题" prop="title">
        <ElInput v-model.trim="form.title" maxlength="240" show-word-limit placeholder="输入知识标题，例如：接口超时排查手册" />
      </ElFormItem>

      <div class="ke-row">
        <ElFormItem label="所属目录" prop="space_id">
          <ElSelect v-model="form.space_id" placeholder="选择目录" style="width: 100%">
            <ElOption v-for="dir in directories" :key="dir.id" :value="dir.id" :label="dir.name" />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="知识类型" prop="type">
          <ElSelect v-model="form.type" style="width: 100%">
            <ElOption v-for="item in types" :key="item.value" :value="item.value" :label="item.label" />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="验证状态" prop="verification_status">
          <ElSelect v-model="form.verification_status" style="width: 100%">
            <ElOption value="unverified" label="未验证" />
            <ElOption value="verified" label="已验证" />
            <ElOption value="failed" label="验证失败" />
          </ElSelect>
        </ElFormItem>
      </div>

      <ElCollapse v-model="secondaryOpen" class="ke-collapse">
        <ElCollapseItem title="摘要与标签（可选）" name="meta">
          <div class="ke-meta-grid">
            <ElFormItem label="标签" prop="tags">
              <ElSelect
                v-model="form.tags"
                multiple
                filterable
                allow-create
                default-first-option
                clearable
                placeholder="输入标签后回车添加"
                style="width: 100%"
              />
            </ElFormItem>
            <ElFormItem label="摘要" prop="summary">
              <ElInput v-model.trim="form.summary" type="textarea" :rows="2" maxlength="4000" placeholder="可选，简要概括文档核心结论" />
            </ElFormItem>
          </div>
        </ElCollapseItem>
      </ElCollapse>

      <ElFormItem label="正文（Markdown）" prop="content_md">
        <div class="ke-md">
          <div class="ke-md__bar">
            <div class="ke-md__snippets">
              <button type="button" class="ke-md__btn" title="插入二级标题" @click="insertSnippet('\n## 章节标题\n')">H2</button>
              <button type="button" class="ke-md__btn" title="插入粗体" @click="insertSnippet('**重点内容**')"><b>B</b></button>
              <button type="button" class="ke-md__btn" title="插入列表" @click="insertSnippet('\n- 要点 1\n- 要点 2\n')">列表</button>
              <button type="button" class="ke-md__btn" title="插入代码块" @click="insertSnippet('\n```bash\n# 命令或代码示例\n```\n')">代码</button>
              <button type="button" class="ke-md__btn" title="插入表格" @click="insertSnippet('\n| 列 1 | 列 2 |\n| --- | --- |\n| 值 1 | 值 2 |\n')">表格</button>
              <span class="ke-md__sep" aria-hidden="true" />
              <ElButton size="small" text :icon="Upload" @click="pickMarkdown">导入 .md</ElButton>
              <input ref="fileInput" class="ke-md__file" type="file" accept=".md,text/markdown,text/plain" @change="readMarkdown" />
            </div>
            <ElRadioGroup v-model="markdownMode" size="small" aria-label="编辑视图">
              <ElRadioButton value="edit">编辑</ElRadioButton>
              <ElRadioButton value="split">分屏</ElRadioButton>
              <ElRadioButton value="preview">预览</ElRadioButton>
            </ElRadioGroup>
          </div>

          <div class="ke-md__panes" :class="`is-${markdownMode}`">
            <ElInput
              v-if="markdownMode !== 'preview'"
              v-model="form.content_md"
              class="ke-md__input"
              type="textarea"
              :rows="16"
              resize="none"
              aria-label="Markdown 正文编辑区"
              placeholder="支持标准 Markdown 语法（标题、列表、表格、代码块）……"
            />
            <div v-if="markdownMode !== 'edit'" class="ke-md__preview">
              <MarkdownView :content="form.content_md" compact empty-text="在左侧输入 Markdown 内容后，此处实时预览排版效果" />
            </div>
          </div>
        </div>
      </ElFormItem>

      <ElAlert
        v-if="error"
        data-testid="knowledge-editor-error"
        class="ke-error"
        type="error"
        show-icon
        :closable="false"
        :title="error"
      />
    </ElForm>

    <template #footer>
      <div class="ke-footer">
        <span class="ke-footer__note">
          {{ isEdit ? (editing?.status === 'published' ? '保存后自动重新同步到检索库' : '') : '新建知识默认保存为草稿' }}
        </span>
        <ElSpace :size="8">
          <ElTag v-if="dirty" size="small" type="warning" effect="light">有未保存修改</ElTag>
          <ElButton :disabled="saving" @click="requestClose">取消</ElButton>
          <ElButton type="primary" :loading="saving" data-testid="knowledge-editor-submit" @click="submit">
            {{ isEdit ? '保存修改' : '保存草稿' }}
          </ElButton>
        </ElSpace>
      </div>
    </template>
  </ElDrawer>
</template>

<script setup lang="ts">
import { onBeforeRouteLeave } from 'vue-router'
import { Upload } from 'lucide-vue-next'
import { computed, reactive, ref, watch } from 'vue'
import {
  ElAlert,
  ElButton,
  ElCollapse,
  ElCollapseItem,
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
} from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import MarkdownView from '@/components/astra/MarkdownView.vue'
import { confirmDialog } from '@/composables/useConfirm'
import type { KnowledgeDetail } from '@/api/types'

const props = defineProps<{
  open: boolean
  directories: Array<{ id: string; name: string; level: number }>
  saving: boolean
  error: string
  editing?: KnowledgeDetail | null
  initialContent?: string
  initialTitle?: string
  initialSpaceId?: string
}>()
const emit = defineEmits<{ close: []; save: [payload: Record<string, unknown>] }>()

const secondaryOpen = ref<string[]>([])
const isEdit = computed(() => Boolean(props.editing))

const types = [
  { value: 'document', label: '文档' },
  { value: 'rule', label: '规范' },
  { value: 'guide', label: '指南' },
  { value: 'experience', label: '经验' },
  { value: 'faq', label: 'FAQ' },
  { value: 'tool', label: '工具' },
  { value: 'example', label: '示例' },
]

const formRef = ref<FormInstance | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const markdownMode = ref<'edit' | 'split' | 'preview'>('split')
const form = reactive({
  space_id: '',
  title: '',
  type: 'document',
  verification_status: 'unverified',
  tags: [] as string[],
  summary: '',
  content_md: '',
})
const snapshot = ref('')
const dirty = computed(() => JSON.stringify(form) !== snapshot.value)

const rules: FormRules = {
  title: [{ required: true, message: '请输入知识标题', trigger: 'blur' }],
  space_id: [{ required: true, message: '请选择所属目录', trigger: 'change' }],
  content_md: [{ required: true, message: '请输入 Markdown 正文', trigger: 'blur' }],
}

watch(() => props.open, (open) => {
  if (!open) return
  const editing = props.editing
  if (editing) {
    Object.assign(form, {
      space_id: editing.space_id || props.directories[0]?.id || '',
      title: editing.title ?? '',
      type: editing.type ?? 'document',
      verification_status: editing.verification_status ?? 'unverified',
      tags: [...(editing.tags ?? [])],
      summary: editing.summary ?? '',
      content_md: editing.content_md ?? '',
    })
    secondaryOpen.value = editing.summary || (editing.tags?.length ?? 0) > 0 ? ['meta'] : []
  } else {
    Object.assign(form, {
      space_id: props.initialSpaceId || props.directories[0]?.id || '',
      title: props.initialTitle ?? '',
      type: 'document',
      verification_status: 'unverified',
      tags: [],
      summary: '',
      content_md: props.initialContent ?? '',
    })
    secondaryOpen.value = []
  }
  markdownMode.value = window.innerWidth >= 820 ? 'split' : 'edit'
  snapshot.value = JSON.stringify(form)
  formRef.value?.clearValidate()
}, { immediate: true })

function insertSnippet(snippet: string) {
  form.content_md = `${form.content_md}${snippet}`
}

async function requestClose() {
  if (!props.open) return true
  if (props.saving) return false
  if (dirty.value) {
    const confirmed = await confirmDialog(
      isEdit.value
        ? { title: '放弃未保存的知识修改？', message: '对该知识的修改尚未保存，关闭后将无法恢复。', confirmLabel: '放弃并关闭', cancelLabel: '继续编辑', danger: true }
        : { title: '放弃未保存的知识草稿？', message: '编辑内容尚未保存，关闭后将无法恢复。', confirmLabel: '放弃并关闭', cancelLabel: '继续编辑', danger: true },
    )
    if (!confirmed) return false
  }
  emit('close')
  return true
}
onBeforeRouteLeave(() => requestClose())

async function submit() {
  if (props.saving) return
  const valid = formRef.value ? await formRef.value.validate().catch(() => false) : false
  if (!valid) return
  emit('save', {
    ...form,
    tags: [...new Set(form.tags.map((tag) => tag.trim()).filter(Boolean))],
  })
}

function pickMarkdown() {
  fileInput.value?.click()
}

async function readMarkdown(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) {
    form.content_md = await file.text()
    if (!form.title) form.title = file.name.replace(/\.md$/i, '')
  }
  input.value = ''
}
</script>

<style scoped>
.ke-form { display: grid; gap: 2px; }
.ke-row { display: grid; grid-template-columns: 1.4fr 1fr 1fr; gap: 12px; }
.ke-collapse { margin-bottom: 14px; }
.ke-meta-grid { display: grid; gap: 4px; }

.ke-md { display: grid; gap: 0; width: 100%; border: 1px solid var(--astra-border); border-radius: 10px; overflow: hidden; background: var(--astra-bg-panel); }
.ke-md__bar {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--astra-border);
  background: var(--astra-bg-subtle);
}
.ke-md__snippets { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
.ke-md__btn {
  height: 26px; padding: 0 8px;
  border: 1px solid transparent; border-radius: 6px; background: transparent;
  color: var(--astra-text-secondary); font-size: 12px; cursor: pointer;
}
.ke-md__btn:hover { background: var(--astra-bg-panel); border-color: var(--astra-border); color: var(--astra-text-primary); }
.ke-md__sep { width: 1px; height: 14px; background: var(--astra-border); margin: 0 4px; }
.ke-md__file { display: none; }

.ke-md__panes { display: grid; min-height: 390px; max-height: 52vh; }
.ke-md__panes.is-split { grid-template-columns: 1fr 1fr; }
.ke-md__input { height: 100%; }
.ke-md__input :deep(.el-textarea__inner) {
  height: 100% !important; min-height: 390px; max-height: 52vh;
  border: 0; border-radius: 0; box-shadow: none !important;
  padding: 12px 14px;
  font-family: var(--astra-font-mono); font-size: 13px; line-height: 1.65;
}
.ke-md__preview {
  height: 100%; min-height: 390px; max-height: 52vh;
  overflow-y: auto; padding: 14px 16px;
  background: var(--astra-bg-panel);
}
.ke-md__panes.is-split .ke-md__preview { border-left: 1px solid var(--astra-border); background: var(--astra-bg-subtle); }

.ke-error { margin-top: 4px; }
.ke-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; }
.ke-footer__note { color: var(--astra-text-muted); font-size: 12px; }

@media (min-width: 2240px) and (min-height: 1180px) {
  .ke-md__panes,
  .ke-md__input :deep(.el-textarea__inner),
  .ke-md__preview {
    min-height: 520px;
    max-height: 64vh;
  }
}

@media (max-width: 760px) {
  .ke-row { grid-template-columns: 1fr; }
  .ke-md__panes.is-split { grid-template-columns: 1fr; }
}
</style>

<style>
.knowledge-editor-drawer .el-drawer__footer { display: flex; align-items: center; }
</style>
