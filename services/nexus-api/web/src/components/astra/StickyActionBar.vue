<template>
  <transition name="action-bar">
    <div v-if="dirty" class="sticky-action-bar">
      <div class="sticky-action-bar__status" :data-dirty="dirty">
        <i /> 有未保存修改
      </div>
      <div class="sticky-action-bar__actions">
        <slot name="extra" />
        <button type="button" class="button" :disabled="disabled" @click="$emit('cancel')">取消</button>
        <button type="button" class="button button--primary" :disabled="disabled || saving" @click="$emit('save')">
          <Save v-if="!saving" :size="14" />
          <RefreshCw v-else :size="14" class="spin" />
          {{ saving ? '保存中' : saveLabel }}
        </button>
      </div>
    </div>
  </transition>
</template>

<script setup lang="ts">
import { RefreshCw, Save } from 'lucide-vue-next'

defineProps<{
  dirty: boolean
  saving?: boolean
  disabled?: boolean
  saveLabel?: string
}>()
defineEmits<{ (e: 'save'): void; (e: 'cancel'): void }>()
</script>

<style scoped>
.sticky-action-bar {
  position: fixed;
  right: 0;
  bottom: 0;
  left: 0;
  z-index: 300;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 28px;
  border-top: 1px solid var(--astra-border);
  background: rgba(255, 255, 255, 0.97);
  box-shadow: 0 -12px 30px rgba(28, 58, 49, 0.1);
  backdrop-filter: blur(12px);
}
.sticky-action-bar__status {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--astra-warning);
  font-size: var(--astra-font-control);
  font-weight: var(--astra-weight-bold);
}
.sticky-action-bar__status i {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
  animation: pulse 1.8s ease-in-out infinite;
}
.sticky-action-bar__actions { display: flex; align-items: center; gap: 9px; }
.action-bar-enter-active, .action-bar-leave-active { transition: transform 0.2s ease, opacity 0.2s ease; }
.action-bar-enter-from, .action-bar-leave-to { transform: translateY(100%); opacity: 0; }
@media (max-width: 760px) {
  .sticky-action-bar { padding: 10px 16px; }
  .sticky-action-bar__actions { flex: 1; justify-content: flex-end; }
}
</style>
