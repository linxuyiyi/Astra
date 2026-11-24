<template>
  <ElDrawer
    v-if="open"
    class="category-editor-drawer"
    data-testid="category-editor-drawer"
    :model-value="true"
    :title="isEdit ? '编辑任务分类' : '新增任务分类'"
    size="min(680px, 100%)"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :before-close="requestClose"
  >
    <ElForm
      ref="formRef"
      class="category-editor-form"
      :model="form"
      :rules="rules"
      label-position="top"
      scroll-to-error
    >
      <div class="cat-pair">
        <ElFormItem label="分类名称" prop="name">
          <ElInput v-model.trim="form.name" maxlength="120" placeholder="例如：质量防线" data-testid="category-form-name" />
        </ElFormItem>
        <ElFormItem label="排序" prop="sort_order">
          <ElInputNumber v-model="form.sort_order" style="width: 100%" :min="0" :step="10" :precision="0" controls-position="right" />
        </ElFormItem>
      </div>

      <ElFormItem label="描述" prop="description">
        <ElInput v-model.trim="form.description" type="textarea" :rows="2" placeholder="可选，说明该分类涵盖的业务范围" />
      </ElFormItem>

      <div class="cat-prompt-box">
        <div class="cat-prompt-box__bar">
          <ElRadioGroup v-model="promptTab" size="small" aria-label="Prompt 切换">
            <ElRadioButton value="task">
              任务 Prompt{{ form.task_prompt.trim() ? ' · 已填' : '' }}
            </ElRadioButton>
            <ElRadioButton value="question">
              问题 Prompt{{ form.question_prompt.trim() ? ' · 已填' : '' }}
            </ElRadioButton>
          </ElRadioGroup>
          <ElRadioGroup v-model="promptView" size="small" aria-label="编辑视图">
            <ElRadioButton value="edit">编辑</ElRadioButton>
            <ElRadioButton value="preview">预览</ElRadioButton>
          </ElRadioGroup>
        </div>

        <template v-if="promptTab === 'task'">
          <ElInput
            v-if="promptView === 'edit'"
            v-model="form.task_prompt"
            class="cat-prompt-input"
            type="textarea"
            :rows="12"
            resize="vertical"
            aria-label="任务 Prompt"
            placeholder="为该分类下的需求类任务定义执行约束与输出要求（支持 Markdown）"
          />
          <div v-else class="cat-prompt-preview">
            <MarkdownView :content="form.task_prompt" compact empty-text="未填写任务 Prompt" />
          </div>
        </template>
        <template v-else>
          <ElInput
            v-if="promptView === 'edit'"
            v-model="form.question_prompt"
            class="cat-prompt-input"
            type="textarea"
            :rows="12"
            resize="vertical"
            aria-label="问题 Prompt"
            placeholder="为该分类下的问题排查类任务定义排查路径与规范（支持 Markdown）"
          />
          <div v-else class="cat-prompt-preview">
            <MarkdownView :content="form.question_prompt" compact empty-text="未填写问题 Prompt" />
          </div>
        </template>
      </div>

      <ElAlert
        v-if="error"
        class="category-editor-form__error"
        type="error"
        show-icon
        :closable="false"
        :title="error"
        data-testid="category-editor-error"
      />
    </ElForm>

    <template #footer>
      <div class="category-editor-footer">
        <ElSwitch v-model="form.enabled" active-text="启用此分类" />
        <ElSpace :size="8">
          <ElTag v-if="dirty" size="small" type="warning" effect="light" data-testid="category-editor-dirty">有未保存修改</ElTag>
          <ElButton :disabled="saving" data-testid="category-editor-cancel" @click="requestClose">取消</ElButton>
          <ElButton type="primary" :loading="saving" data-testid="category-editor-submit" @click="submit">保存</ElButton>
        </ElSpace>
      </div>
    </template>
  </ElDrawer>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElAlert, ElButton, ElDrawer, ElForm, ElFormItem, ElInput, ElInputNumber, ElRadioButton, ElRadioGroup, ElSpace, ElSwitch, ElTag } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import MarkdownView from '@/components/astra/MarkdownView.vue'
import { confirmDialog } from '@/composables/useConfirm'
import type { TaskCategory, TaskCategoryWrite } from '@/api/types'

const props = defineProps<{ open: boolean; category: TaskCategory | null; defaultSortOrder: number; saving: boolean; error: string }>()
const emit = defineEmits<{ close: []; save: [payload: TaskCategoryWrite] }>()

const formRef = ref<FormInstance | null>(null)
const promptTab = ref<'task' | 'question'>('task')
const promptView = ref<'edit' | 'preview'>('edit')
const form = reactive({ name: '', description: '', sort_order: 0, enabled: true, task_prompt: '', question_prompt: '' })
const snapshot = ref('')
const isEdit = computed(() => Boolean(props.category))
const dirty = computed(() => props.open && JSON.stringify(form) !== snapshot.value)

const rules: FormRules = {
  name: [{ required: true, message: '请输入分类名称', trigger: 'blur' }],
  sort_order: [
    { required: true, message: '请输入排序', trigger: 'change' },
    { type: 'number', min: 0, message: '排序必须为非负整数', trigger: 'change' },
  ],
}

watch(() => props.open, (open) => {
  if (!open) return
  const category = props.category
  Object.assign(form, {
    name: category?.name ?? '',
    description: category?.description ?? '',
    sort_order: category?.sort_order ?? props.defaultSortOrder,
    enabled: category?.enabled ?? true,
    task_prompt: category?.task_prompt ?? '',
    question_prompt: category?.question_prompt ?? '',
  })
  promptTab.value = 'task'
  promptView.value = 'edit'
  snapshot.value = JSON.stringify(form)
  formRef.value?.clearValidate()
}, { immediate: true })

async function requestClose() {
  if (props.saving) return
  if (dirty.value) {
    const confirmed = await confirmDialog({
      title: '放弃未保存的分类修改？',
      message: '分类的提示词与排序尚未保存，关闭后将无法恢复。',
      confirmLabel: '放弃并关闭',
      cancelLabel: '继续编辑',
      danger: true,
    })
    if (!confirmed) return
  }
  emit('close')
}

async function submit() {
  if (props.saving) return
  const valid = formRef.value ? await formRef.value.validate().catch(() => false) : false
  if (!valid) return
  emit('save', { ...form, sort_order: Number(form.sort_order) })
}

defineExpose({ getDirty: () => dirty.value })
</script>

<style scoped>
.category-editor-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; }
.category-editor-form { display: grid; gap: 4px; }
.cat-pair { display: grid; grid-template-columns: 1.8fr 1fr; gap: 12px; }
.cat-prompt-box {
  display: grid; margin-top: 4px;
  border: 1px solid var(--astra-border); border-radius: 10px; overflow: hidden;
  background: var(--astra-bg-panel);
}
.cat-prompt-box__bar {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--astra-border);
  background: var(--astra-bg-subtle);
}
.cat-prompt-input :deep(.el-textarea__inner) {
  border: 0; border-radius: 0; box-shadow: none !important; padding: 12px 14px;
  font-family: var(--astra-font-mono); font-size: 12.5px; line-height: 1.65;
}
.cat-prompt-preview { min-height: 260px; max-height: 46vh; overflow-y: auto; padding: 14px 16px; }
.category-editor-form__error { margin-top: 10px; }

@media (max-width: 600px) {
  .cat-pair { grid-template-columns: 1fr; }
}
</style>
