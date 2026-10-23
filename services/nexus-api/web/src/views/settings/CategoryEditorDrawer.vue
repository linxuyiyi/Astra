<template>
  <ElDrawer
    v-if="open"
    class="category-editor-drawer"
    data-testid="category-editor-drawer"
    :model-value="true"
    :title="isEdit ? '编辑任务分类' : '新增任务分类'"
    size="min(560px, 100%)"
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
      <ElFormItem label="分类名称" prop="name">
        <ElInput v-model.trim="form.name" maxlength="120" placeholder="例如：质量防线" data-testid="category-form-name" />
      </ElFormItem>

      <ElFormItem label="描述" prop="description">
        <ElInput v-model.trim="form.description" type="textarea" :rows="2" placeholder="例如：支付与结算" />
      </ElFormItem>

      <ElFormItem prop="sort_order">
        <template #label>排序</template>
        <ElInputNumber v-model="form.sort_order" class="category-editor-form__sort" :min="0" :step="10" :precision="0" controls-position="right" />
      </ElFormItem>

      <ElFormItem label="启用" prop="enabled">
        <ElSwitch v-model="form.enabled" active-text="启用" inactive-text="停用" />
      </ElFormItem>

      <ElFormItem label="任务 Prompt" prop="task_prompt">
        <ElInput
          v-model="form.task_prompt"
          class="category-editor-form__prompt"
          type="textarea"
          :rows="8"
          resize="vertical"
          aria-label="任务 Prompt"
          placeholder="任务 Prompt"
        />
      </ElFormItem>

      <ElFormItem label="问题 Prompt" prop="question_prompt">
        <ElInput
          v-model="form.question_prompt"
          class="category-editor-form__prompt"
          type="textarea"
          :rows="8"
          resize="vertical"
          aria-label="问题 Prompt"
          placeholder="问题 Prompt"
        />
      </ElFormItem>

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
        <ElSpace :size="8">
          <ElTag v-if="dirty" type="warning" effect="light" data-testid="category-editor-dirty">有未保存修改</ElTag>
          <ElButton :disabled="saving" data-testid="category-editor-cancel" @click="requestClose">取消</ElButton>
          <ElButton type="primary" :loading="saving" data-testid="category-editor-submit" @click="submit">保存</ElButton>
        </ElSpace>
      </div>
    </template>
  </ElDrawer>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElAlert, ElButton, ElDrawer, ElForm, ElFormItem, ElInput, ElInputNumber, ElSpace, ElSwitch, ElTag } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { confirmDialog } from '@/composables/useConfirm'
import type { TaskCategory, TaskCategoryWrite } from '@/api/types'

const props = defineProps<{ open: boolean; category: TaskCategory | null; defaultSortOrder: number; saving: boolean; error: string }>()
const emit = defineEmits<{ close: []; save: [payload: TaskCategoryWrite] }>()

const formRef = ref<FormInstance | null>(null)
const form = reactive({ name: '', description: '', sort_order: 0, enabled: true, task_prompt: '', question_prompt: '' })
const snapshot = ref('')
const isEdit = computed(() => Boolean(props.category))
// dirty 的有效范围就是抽屉本身：关闭时没有可丢失的内容（从未打开、已保存或已确认放弃）。
// snapshot 只在打开时播种，没打开过就一直是初始空串，与 form 的默认值不等 ——
// 不看 open 就会被判成「有未保存修改」，让路由守卫在没有任何改动时也弹放弃确认。
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
  snapshot.value = JSON.stringify(form)
  formRef.value?.clearValidate()
}, { immediate: true })

// 抽屉是分类唯一的编辑入口：遮罩点击与 ESC 不关闭，只走显式取消，且有未保存内容时先二次确认。
// before-close 不调用 done()，关闭始终由父级把 open 置为 false 驱动。
// getDirty 供父级的路由离开守卫复用同一套 dirty 判定，避免两处各写一份。
async function requestClose() {
  if (props.saving) return
  if (dirty.value) {
    const confirmed = await confirmDialog({
      title: '放弃未保存的分类修改？',
      message: '分类的提示词与排序尚未保存，关闭后将无法恢复。',
      confirmLabel: '放弃并关闭',
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
/* footer 槽位内容写在本组件模板里，会带上 scoped 标记；直接用真实元素排版，
   不用 .el-drawer :deep(.el-drawer__footer) —— 抽屉根节点由 Element Plus 渲染，
   不带本组件的 data-v，:deep 选择器匹配不到。 */
.category-editor-footer { display: flex; align-items: center; justify-content: flex-end; }
.category-editor-form { display: grid; gap: 2px; }
.category-editor-form__sort { width: 160px; }
.category-editor-form__prompt :deep(.el-textarea__inner) { line-height: 1.6; }
.category-editor-form__error { margin-top: 4px; }
</style>
