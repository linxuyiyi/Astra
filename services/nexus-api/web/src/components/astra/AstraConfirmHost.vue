<template>
  <ElDialog
    v-if="state"
    data-testid="astra-confirm-dialog"
    :model-value="true"
    :title="state.title"
    width="min(440px, 100%)"
    :show-close="false"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
  >
    <ElText v-if="state.message" type="info">{{ state.message }}</ElText>
    <template #footer>
      <ElSpace :size="10">
        <ElButton @click="resolveConfirm(false)">{{ state.cancelLabel }}</ElButton>
        <ElButton :type="state.danger ? 'danger' : 'primary'" @click="resolveConfirm(true)">
          {{ state.confirmLabel }}
        </ElButton>
      </ElSpace>
    </template>
  </ElDialog>
</template>

<script setup lang="ts">
import { ElButton, ElDialog, ElSpace, ElText } from 'element-plus'
import { resolveConfirm, useConfirmState } from '@/composables/useConfirm'

// A danger/confirm dialog must not close by overlay click or ESC; the user has
// to press 取消 or 确认 explicitly. Cancel is always on the left, confirm on the
// right, and a destructive action uses the danger button style. The dialog only
// mounts while a confirm is pending, so Element Plus gives it a z-index above
// any already-open form dialog and the explicit buttons stay reachable. Title,
// body and footer are the native ElDialog structure.
const state = useConfirmState()
</script>
