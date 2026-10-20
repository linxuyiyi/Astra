<template>
  <ElTooltip v-if="help" :content="help" placement="top" :persistent="false" :show-after="0" :hide-after="0" :trigger="['hover', 'focus']">
    <button ref="triggerRef" type="button" class="field-help" :aria-label="`${label}说明`" @click.stop.prevent @keydown.esc="hide">
      <Info :size="14" aria-hidden="true" />
    </button>
  </ElTooltip>
</template>

<script setup lang="ts">
import { Info } from 'lucide-vue-next'
import { ElTooltip } from 'element-plus'
import { ref } from 'vue'

defineProps<{ label: string; help?: string | null }>()
const triggerRef = ref<HTMLElement | null>(null)
// ElTooltip 的 focus 触发只在 focusout 时收起；Escape 主动失焦，保证键盘用户能立即关闭说明。
function hide() {
  triggerRef.value?.blur()
}
</script>

<style scoped>
.field-help { display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; padding: 0; color: var(--el-text-color-secondary); border: 0; border-radius: 3px; background: transparent; cursor: help; line-height: 1; vertical-align: -2px; }
.field-help:hover, .field-help:focus-visible { color: var(--el-color-primary); }
.field-help:focus-visible { outline: 2px solid var(--el-color-primary-light-5); outline-offset: 1px; }
</style>
