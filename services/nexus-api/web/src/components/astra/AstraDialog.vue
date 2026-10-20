<template>
  <ElDialog
    v-if="open"
    :data-testid="testid"
    :model-value="true"
    :class="cardClass"
    :title="title"
    :width="width"
    :close-on-click-modal="dismissible"
    :close-on-press-escape="dismissible"
    :before-close="requestClose"
  >
    <ElSpace direction="vertical" fill :size="14" alignment="start">
      <ElText v-if="subtitle" type="info">{{ subtitle }}</ElText>
      <slot />
    </ElSpace>
    <template v-if="$slots.footer" #footer>
      <ElSpace :size="10">
        <slot name="footer" />
      </ElSpace>
    </template>
  </ElDialog>
</template>

<script setup lang="ts">
import { ElDialog, ElSpace, ElText } from 'element-plus'

// Form/config/settings dialogs default to a non-dismissible overlay and ESC so a
// stray click can never discard typed input. View-only dialogs can opt in with
// dismissible, and the two dismissal paths always share the same rule.
// The close paths (overlay, ESC, header close button) all funnel into
// before-close, which only requests a close from the parent; done() is never
// called, so the dialog unmounts exactly when the `open` prop flips to false.
// Header/body/footer layout comes from ElDialog natively; ElSpace provides the
// content/footer spacing and ElText the muted subtitle.
const props = withDefaults(
  defineProps<{
    open: boolean
    title: string
    subtitle?: string
    dismissible?: boolean
    busy?: boolean
    testid?: string
    width?: string
    cardClass?: string
  }>(),
  { dismissible: false, busy: false, width: 'min(620px, 100%)' },
)

const emit = defineEmits<{ close: [] }>()

function requestClose(): void {
  if (!props.busy) emit('close')
}
</script>
