import { onBeforeUnmount, ref, type Ref } from 'vue'

// 轻量响应式断点：用于收起窄屏列/切换布局，不引入状态管理库。
export function useMediaQuery(query: string): Ref<boolean> {
  const matches = ref(false)
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    const mql = window.matchMedia(query)
    matches.value = mql.matches
    const handler = (event: MediaQueryListEvent) => {
      matches.value = event.matches
    }
    mql.addEventListener('change', handler)
    onBeforeUnmount(() => mql.removeEventListener('change', handler))
  }
  return matches
}
