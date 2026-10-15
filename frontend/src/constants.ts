// 员工表单的固定约束，与后端 backend/nexus_backend/employees.py 保持一致。
export const NAME_MAX_LENGTH = 24
export const DOMAIN_MAX_LENGTH = 42
export const RESOURCE_MAX = 50

export interface AvatarOption {
  value: string
  label: string
}

// 原型固定的五个员工形象（字符串规范枚举，后端 AVATARS）。
export const AVATARS: AvatarOption[] = [
  { value: '👩🏻‍💻', label: '工程师' },
  { value: '🧑🏻‍🔬', label: '分析师' },
  { value: '👩🏽‍🚀', label: '技术专家' },
  { value: '🤖', label: '数字员工' },
  { value: '🧑🏽‍💻', label: '工程师 B' },
]

export const DEFAULT_AVATAR = AVATARS[0].value

// 原型创建默认工作指令（docs/UI_ACCEPTANCE.md 第 103 行）。
export const DEFAULT_INSTRUCTIONS =
  '收到消息后先判断能否直接回答；信息不足时追问；需要其他员工帮助时联系相应员工；不得编造真实执行结果。'

export function avatarLabel(value: string): string {
  return AVATARS.find((item) => item.value === value)?.label ?? '数字员工'
}

export function isKnownAvatar(value: string): boolean {
  return AVATARS.some((item) => item.value === value)
}
