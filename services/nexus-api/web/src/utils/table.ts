/**
 * 表格统一合理高度策略（Nexus 管理页面统一 UI 规范）
 * 基于视口剩余高度计算，确保表体内部滚动、表头常驻、工具栏与底部分页常驻。
 */
export const TABLE_MAX_HEIGHT = 'var(--nexus-table-max-height, calc(100vh - 280px))'
export const TABLE_MAX_HEIGHT_DENSE = 'var(--nexus-table-max-height-dense, calc(100vh - 340px))'
