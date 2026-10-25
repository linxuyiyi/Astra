import { ref, computed } from 'vue'

const windowHeight = ref(typeof window !== 'undefined' ? window.innerHeight : 900)

if (typeof window !== 'undefined') {
  window.addEventListener('resize', () => {
    windowHeight.value = window.innerHeight
  }, { passive: true })
}

/**
 * 表格统一合理高度策略（Nexus 管理页面统一 UI 规范）
 * 运行时解析为像素数值，确保 Element Plus Table 内部能正确计算表体高度并开启内滚，
 * 保证表头常驻、工具栏与底部分页常驻。视口 resize 时自动响应式更新。
 */
export function useTableMaxHeight(offset = 280, minHeight = 320) {
  return computed(() => Math.max(minHeight, windowHeight.value - offset))
}

export const TABLE_MAX_HEIGHT = computed(() => Math.max(320, windowHeight.value - 280))
export const TABLE_MAX_HEIGHT_DENSE = computed(() => Math.max(220, windowHeight.value - 460))
