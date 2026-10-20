import { ref, type Ref } from 'vue'

export interface ConfirmOptions {
  title: string
  message?: string
  confirmLabel?: string
  cancelLabel?: string
  danger?: boolean
}

interface ConfirmState extends ConfirmOptions {
  resolve: (value: boolean) => void
}

const state = ref<ConfirmState | null>(null)

// Imperative confirm rendered by the single AstraConfirmHost. It replaces
// window.confirm and, by design, cannot be dismissed by overlay click or ESC:
// the user must explicitly choose 取消 or 确认.
export function confirmDialog(options: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    state.value = {
      confirmLabel: '确认',
      cancelLabel: '取消',
      danger: false,
      ...options,
      resolve,
    }
  })
}

export function resolveConfirm(value: boolean): void {
  const current = state.value
  state.value = null
  current?.resolve(value)
}

export function useConfirmState(): Ref<ConfirmState | null> {
  return state
}