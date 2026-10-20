/**
 * 工具名称与分类展示辅助
 */

export const BUILTIN_TOOL_LABELS: Record<string, string> = {
  Bash: '终端命令执行 (Bash)',
  Read: '读取文件 (Read)',
  Write: '写入文件 (Write)',
  Edit: '编辑文件 (Edit)',
  Glob: '查找文件 (Glob)',
  Grep: '搜索文本 (Grep)',
  contact_employee: '联系其他数字员工',
  contact_human: '联系人工介入',
}

export function formatToolLabel(id: string, name?: string): string {
  if (BUILTIN_TOOL_LABELS[id]) {
    return BUILTIN_TOOL_LABELS[id]
  }
  if (name && name !== id && name !== '工作区工具') {
    return name
  }
  if (id.startsWith('mcp_') || id.startsWith('mcp__')) {
    const clean = id.replace(/^mcp_+/, '')
    return `[MCP] ${clean}`
  }
  return id
}

export function isMcpTool(id: string): boolean {
  return id.startsWith('mcp_') || id.startsWith('mcp__')
}
