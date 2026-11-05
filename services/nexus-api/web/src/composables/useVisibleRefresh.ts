import { onMounted, onUnmounted } from 'vue'

export function useVisibleRefresh(refresh: () => void, interval: number) {
  let timer: ReturnType<typeof setInterval>
  function tick() {
    if (!document.hidden) refresh()
  }
  onMounted(() => {
    timer = setInterval(tick, interval)
    document.addEventListener('visibilitychange', tick)
  })
  onUnmounted(() => {
    clearInterval(timer)
    document.removeEventListener('visibilitychange', tick)
  })
}
