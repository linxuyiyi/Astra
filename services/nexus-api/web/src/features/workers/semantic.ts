import type { RuntimeTone } from '@/features/runtime/status'

/** 展示语义色：正常/成功、进行中、需要处理、失败、中性。 */
export type SemanticTone = 'success' | 'progress' | 'attention' | 'danger' | 'neutral'

export type SemanticTagType = 'success' | 'primary' | 'warning' | 'danger' | 'info'

const RUNTIME_TONE: Record<RuntimeTone, SemanticTone> = {
  success: 'success',
  info: 'progress',
  warning: 'attention',
  danger: 'danger',
  neutral: 'neutral',
}

/** 把既有状态事实源（task_status.json / runtime status）的语气映射到统一语义色。 */
export function semanticTone(tone: RuntimeTone | SemanticTone): SemanticTone {
  return RUNTIME_TONE[tone as RuntimeTone] ?? (tone as SemanticTone)
}

export interface SemanticTagConfig {
  type: SemanticTagType
  /** 中性色由主题层提供；其余语义直接复用 Element Plus 的类型色。 */
  className: string
}

const TAG_CONFIG: Record<SemanticTone, SemanticTagConfig> = {
  success: { type: 'success', className: '' },
  // 主题里的 primary 是品牌绿，进行中必须走 info（蓝）。
  progress: { type: 'info', className: '' },
  attention: { type: 'warning', className: '' },
  danger: { type: 'danger', className: '' },
  neutral: { type: 'info', className: 'astra-tag--neutral' },
}

export function semanticTag(tone: SemanticTone): SemanticTagConfig {
  return TAG_CONFIG[tone] ?? TAG_CONFIG.neutral
}

const MAINTENANCE_TONES: Record<string, SemanticTone> = {
  QUEUED: 'neutral', PREFLIGHT: 'progress', RUNNING: 'progress', VERIFYING: 'progress',
  SUCCEEDED: 'success', SUCCESS: 'success', PASSED: 'success', FAILED: 'danger',
  CANCELED: 'neutral', INTERRUPTED: 'attention',
}

/** Records and log events share the same palette, including lowercase probe states. */
export function maintenanceSemanticTone(state: string | null | undefined, eventType?: string): SemanticTone {
  if (eventType === 'canceled') return 'neutral'
  if (eventType === 'interrupted' || eventType === 'cancel_requested') return 'attention'
  if (eventType === 'failed') return 'danger'
  return MAINTENANCE_TONES[String(state ?? '').toUpperCase()] ?? 'neutral'
}
