<template>
  <ElDrawer
    v-if="open"
    class="knowledge-editor-drawer"
    data-testid="knowledge-editor-drawer"
    :model-value="true"
    :title="editing ? '编辑知识' : '新建知识'"
    size="min(720px, 100%)"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :before-close="requestClose"
  >
    <ElForm
      ref="formRef"
      class="knowledge-editor-form"
      :model="form"
      :rules="rules"
      label-position="top"
      scroll-to-error
    >
      <ElText class="editor-hint" type="info">{{ editorHint }}</ElText>

      <ElFormItem label="知识标题" prop="title">
        <ElInput v-model.trim="form.title" maxlength="240" show-word-limit placeholder="例如：支付超时排查" />
      </ElFormItem>

      <ElFormItem label="所属目录" prop="space_id">
        <ElSelect v-model="form.space_id" placeholder="请选择">
          <ElOption v-for="dir in directories" :key="dir.id" :value="dir.id" :label="dir.name" />
        </ElSelect>
      </ElFormItem>

      <ElFormItem label="知识类型" prop="type">
        <ElSelect v-model="form.type">
          <ElOption v-for="item in types" :key="item.value" :value="item.value" :label="item.label" />
        </ElSelect>
      </ElFormItem>

      <ElCollapse v-model="secondaryOpen" class="knowledge-metadata-collapse" style="margin-bottom: 18px;">
        <ElCollapseItem title="摘要、标签与验证状态（可选元数据）" name="meta">
          <ElFormItem label="验证状态" prop="verification_status">
            <ElSelect v-model="form.verification_status">
              <ElOption value="unverified" label="未验证" />
              <ElOption value="verified" label="已验证" />
              <ElOption value="failed" label="验证失败" />
            </ElSelect>
          </ElFormItem>

          <ElFormItem label="标签" prop="tags">
            <ElInput v-model="form.tags" placeholder="用逗号分隔" />
          </ElFormItem>

          <ElFormItem label="摘要" prop="summary">
            <ElInput v-model.trim="form.summary" type="textarea" :rows="2" maxlength="4000" show-word-limit placeholder="摘要" />
          </ElFormItem>
        </ElCollapseItem>
      </ElCollapse>

      <ElFormItem label="Markdown 正文" prop="content_md">
        <div class="markdown-editor">
          <div class="markdown-editor__toolbar">
            <ElRadioGroup v-model="markdownMode">
              <ElRadioButton value="edit">编辑</ElRadioButton>
              <ElRadioButton value="preview">预览</ElRadioButton>
            </ElRadioGroup>
            <ElButton :icon="Upload" plain @click="pickMarkdown">导入 .md</ElButton>
            <input ref="fileInput" class="markdown-editor__file" type="file" accept=".md,text/markdown,text/plain" @change="readMarkdown" />
          </div>
          <ElInput v-if="markdownMode === 'edit'" v-model="form.content_md" class="markdown-editor__input" type="textarea" :rows="10" resize="vertical" aria-label="Markdown 正文编辑区" placeholder="## 问题现象&#10;&#10;说明知识内容……" />
          <div v-else class="markdown-editor__preview markdown-body" v-html="safeMarkdown" />
        </div>
      </ElFormItem>

      <ElAlert
        v-if="error"
        data-testid="knowledge-editor-error"
        class="editor-error"
        type="error"
        show-icon
        :closable="false"
        :title="error"
      />
    </ElForm>

    <template #footer>
      <ElSpace :size="8">
        <ElTag v-if="dirty" type="warning" effect="light">有未保存修改</ElTag>
        <ElButton :disabled="saving" @click="requestClose">取消</ElButton>
        <ElButton type="primary" :loading="saving" data-testid="knowledge-editor-submit" @click="submit">{{ isEdit ? '保存修改' : '保存草稿' }}</ElButton>
      </ElSpace>
    </template>
  </ElDrawer>
</template>

<script setup lang="ts">
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { Upload } from 'lucide-vue-next'
import { computed, reactive, ref, watch } from 'vue'
import { ElAlert, ElButton, ElCollapse, ElCollapseItem, ElDrawer, ElForm, ElFormItem, ElInput, ElOption, ElRadioGroup, ElRadioButton, ElSelect, ElSpace, ElTag, ElText } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { confirmDialog } from '@/composables/useConfirm'
import type { KnowledgeDetail } from '@/api/types'

const secondaryOpen = ref<string[]>([])

const props = defineProps<{ open: boolean; directories: Array<{ id: string; name: string; level: number }>; saving: boolean; error: string; editing?: KnowledgeDetail | null; initialContent?: string; initialTitle?: string; initialSpaceId?: string }>()
const emit = defineEmits<{ close: []; save: [payload: Record<string, unknown>] }>()

const isEdit = computed(() => Boolean(props.editing))
const editorHint = computed(() => {
  if (!props.editing) return '知识先保存为草稿，经审核发布后才对数字员工可见。'
  return props.editing.status === 'published'
    ? '这条知识已发布，保存后将进入同步准备；同步完成前数字员工看到的仍是已同步版本。'
    : '保存后修改立即生效，审核状态保持不变。'
})

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
const markdownMode = ref<'edit' | 'preview'>('edit')
const form = reactive({ space_id: '', title: '', type: 'document', verification_status: 'unverified', tags: '', summary: '', content_md: '' })
const snapshot = ref('')
const dirty = computed(() => JSON.stringify(form) !== snapshot.value)

const rules: FormRules = {
  title: [{ required: true, message: '请输入知识标题', trigger: 'blur' }],
  space_id: [{ required: true, message: '请选择所属目录', trigger: 'change' }],
  content_md: [{ required: true, message: '请输入 Markdown 正文', trigger: 'blur' }],
}

marked.setOptions({ gfm: true, breaks: true })
const safeMarkdown = computed(() => DOMPurify.sanitize(marked.parse(form.content_md || '', { async: false }) as string, { USE_PROFILES: { html: true } }))

watch(() => props.open, (open) => {
  if (!open) return
  const editing = props.editing
  if (editing) {
    Object.assign(form, {
      space_id: editing.space_id || props.directories[0]?.id || '',
      title: editing.title ?? '',
      type: editing.type ?? 'document',
      verification_status: editing.verification_status ?? 'unverified',
      tags: (editing.tags ?? []).join(', '),
      summary: editing.summary ?? '',
      content_md: editing.content_md ?? '',
    })
  } else {
    Object.assign(form, {
      space_id: props.initialSpaceId || props.directories[0]?.id || '',
      title: props.initialTitle ?? '',
      type: 'document',
      verification_status: 'unverified',
      tags: '',
      summary: '',
      content_md: props.initialContent ?? '',
    })
  }
  markdownMode.value = 'edit'
  snapshot.value = JSON.stringify(form)
  formRef.value?.clearValidate()
}, { immediate: true })

async function requestClose() {
  if (props.saving) return
  if (dirty.value) {
    const confirmed = await confirmDialog(
      isEdit.value
        ? { title: '放弃未保存的知识修改？', message: '对该知识的修改尚未保存，关闭后将无法恢复。', confirmLabel: '放弃并关闭', danger: true }
        : { title: '放弃未保存的知识草稿？', message: '编辑内容尚未保存，关闭后将无法恢复。', confirmLabel: '放弃并关闭', danger: true },
    )
    if (!confirmed) return
  }
  emit('close')
}

async function submit() {
  if (props.saving) return
  const valid = formRef.value ? await formRef.value.validate().catch(() => false) : false
  if (!valid) return
  emit('save', { ...form, tags: form.tags.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean) })
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
.knowledge-editor-form { display: grid; gap: 2px; }
.editor-hint { margin-bottom: 6px; }
.markdown-editor { display: grid; gap: 8px; width: 100%; }
.markdown-editor__toolbar { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.markdown-editor__file { display: none; }
.markdown-editor__input :deep(.el-textarea__inner) { min-height: 320px; font-family: var(--astra-font-family); font-size: var(--astra-font-body); line-height: 1.55; }
.markdown-editor__preview { min-height: 320px; max-height: 46vh; overflow: auto; padding: 12px 14px; color: var(--astra-text-secondary); border: 1px solid var(--astra-border); border-radius: 8px; background: #fbfdfc; font-size: var(--astra-font-body); line-height: 1.6; }
.editor-error { margin-top: 4px; }
.markdown-editor__preview :deep(h1), .markdown-editor__preview :deep(h2), .markdown-editor__preview :deep(h3) { margin: 12px 0 8px; color: var(--astra-text-primary); line-height: 1.35; }
.markdown-editor__preview :deep(h1) { font-size: var(--astra-font-title); }
.markdown-editor__preview :deep(h2) { font-size: var(--astra-font-section); }
.markdown-editor__preview :deep(h3) { font-size: var(--astra-font-label); }
.markdown-editor__preview :deep(p) { margin: 8px 0; }
.markdown-editor__preview :deep(ul), .markdown-editor__preview :deep(ol) { padding-left: 22px; margin: 8px 0; }
.markdown-editor__preview :deep(blockquote) { margin: 10px 0; padding: 8px 12px; color: var(--astra-text-secondary); border-left: 3px solid var(--astra-primary); background: var(--astra-primary-soft); }
.markdown-editor__preview :deep(code) { padding: 2px 5px; color: #b45140; border-radius: 5px; background: #f4efec; font-family: var(--astra-font-family); font-size: .9em; }
.markdown-editor__preview :deep(pre) { overflow: auto; padding: 12px; border: 1px solid var(--astra-border); border-radius: 8px; background: #15231f; }
.markdown-editor__preview :deep(pre code) { padding: 0; color: #dcebe5; background: transparent; }
.markdown-editor__preview :deep(table) { display: block; max-width: 100%; overflow: auto; border-collapse: collapse; }
.markdown-editor__preview :deep(th), .markdown-editor__preview :deep(td) { padding: 7px 10px; border: 1px solid var(--astra-border); }
.markdown-editor__preview :deep(th) { background: #f3f8f5; }
.markdown-editor__preview :deep(a) { color: var(--astra-primary-bright); text-decoration: underline; }
</style>

<style>
.knowledge-editor-drawer .el-drawer__footer { display: flex; align-items: center; justify-content: flex-end; }
</style>
