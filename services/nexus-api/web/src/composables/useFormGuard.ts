import { computed, ref } from 'vue'

/** 编辑窗口共用的草稿判定；确认框仍由现有 confirmDialog 提供。 */
export function useFormGuard(options: {
  isOpen: () => boolean
  isSaving: () => boolean
  snapshot: () => string
  confirmDiscard: () => Promise<boolean>
}) {
  const baseline = ref('')
  let revision = 0
  let confirmation: Promise<boolean> | null = null
  const dirty = computed(() => options.isOpen() && options.snapshot() !== baseline.value)

  function capture() {
    baseline.value = options.snapshot()
    revision++
  }

  function canClose(): Promise<boolean> {
    if (!options.isOpen()) return Promise.resolve(true)
    if (options.isSaving()) return Promise.resolve(false)
    if (!dirty.value) return Promise.resolve(true)
    if (!confirmation) {
      const current = revision
      confirmation = options.confirmDiscard()
        .then(accepted => accepted && current === revision && !options.isSaving())
        .finally(() => { confirmation = null })
    }
    return confirmation
  }

  return { dirty, capture, canClose }
}
