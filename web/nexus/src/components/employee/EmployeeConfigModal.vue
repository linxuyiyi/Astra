<template>
  <ElDrawer
    v-if="open"
    class="employee-config-drawer"
    data-testid="employee-config-modal"
    :model-value="true"
    :title="drawerTitle"
    size="min(600px, 100vw)"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :before-close="requestClose"
  >
    <EmployeeConfig ref="configRef" :employee="employee" :creating="creating" :categories="categories" embedded @saved="onSaved" />

    <template #footer>
      <ElSpace :size="8">
        <ElTag v-if="configRef?.dirty" type="warning" effect="light">有未保存修改</ElTag>
        <ElButton :disabled="configRef?.saving" @click="requestClose">取消</ElButton>
        <ElButton
          type="primary"
          :loading="configRef?.saving"
          :disabled="!configRef?.dirty"
          @click="configRef?.save()"
        >
          {{ configRef?.saving ? '保存中' : creating ? '创建 Worker' : '保存' }}
        </ElButton>
      </ElSpace>
    </template>
  </ElDrawer>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElButton, ElDrawer, ElSpace, ElTag } from 'element-plus'
import type { Employee, TaskCategory } from '@/api/types'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import { employeeDisplayName } from '@/utils/format'
import EmployeeConfig from '@/views/settings/EmployeeConfig.vue'

const props = defineProps<{ open: boolean; employee: Employee | null; creating: boolean; categories: TaskCategory[] }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'saved', id: string): void }>()
const configRef = ref<InstanceType<typeof EmployeeConfig> | null>(null)

const drawerTitle = computed(() =>
  props.creating ? '新建 Worker' : `编辑 ${employeeDisplayName(props.employee?.name)}`,
)

// The config drawer is a form: overlay click and ESC never close it. Only the
// explicit 取消 / 关闭 actions do, and they confirm first when there are edits.
// before-close never calls done(), so closing is always driven by the `open`
// prop after the parent handled the emitted close event.
async function requestClose() {
  if (configRef.value?.saving) return
  if (configRef.value?.getDirty()) {
    const confirmed = await confirmDialog({
      title: '放弃未保存的 Worker 配置？',
      message: '当前修改尚未保存，关闭后将无法恢复。',
      confirmLabel: '放弃并关闭',
      danger: true,
    })
    if (!confirmed) return
  }
  emit('close')
}
function onSaved(id: string) {
  notify(props.creating ? 'Worker 已创建，请执行装备检查' : '配置已保存，请执行装备检查')
  emit('saved', id)
  emit('close')
}
</script>

<style scoped>
.employee-config-drawer :deep(.el-drawer__footer) { display: flex; align-items: center; justify-content: flex-end; }
</style>
