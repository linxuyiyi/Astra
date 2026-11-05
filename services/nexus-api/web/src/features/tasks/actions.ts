import { ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type { Task } from '@/api/types'
import type { RowMenuItem } from '@/components/astra/RowMenu.vue'

const confirmations: Record<string, [string, string]> = {
  stop: ['取消任务', '确认停止原生执行并取消任务？连接失败时保留资源占用。'],
  restart: ['重新开始', '确认创建新的原生会话？复盘将使用新的当前会话。'],
  confirm: ['确认完成', '确认任务已经达到验收标准？'],
  delete: ['删除任务', '确认删除此任务？'],
}

export function taskActions(task: Task): RowMenuItem[] {
  const items: RowMenuItem[] = []
  const add = (command: string, label: string, disabled = false) => items.push({ command, label, disabled })
  const pending = Boolean(task.pending_message_id)
  const assigned = Boolean(task.agent_worker_id)
  const active = task.status === 'RUNNING' || task.status === 'WAITING'
  const terminal = task.status === 'SUCCEEDED' || task.status === 'CANCELED'
  if (task.status === 'PENDING' && assigned) add('start', '开始', pending || !!task.current_session_id)
  if (task.status === 'RUNNING') add('pause', '停止回复')
  if (task.status === 'WAITING') {
    add('resume', '继续', pending)
    if (task.current_session_id) add('confirm', '确认完成', pending)
  }
  if (!terminal) {
    add('edit', '编辑')
    add('stop', '取消任务')
  }
  if (assigned && (task.status === 'WAITING' || terminal)) add('restart', '重新开始', pending)
  if (active && task.current_session_id) add('reconnect', '重新检查连接')
  if (active) add('confirm-execution-stop', '确认强制释放')
  if (terminal || task.status === 'PENDING') add('delete', '删除', pending)
  return items
}

export async function runTaskAction(task: Task, command: string): Promise<boolean> {
  let body = {}
  try {
    if (command === 'confirm-execution-stop') {
      const result = await ElMessageBox.prompt(
        '确认远端执行已经停止，或明确接受原任务仍可能写入工作区的后果后，填写释放原因。',
        '确认强制释放',
        { type: 'warning', inputValidator: value => Boolean(value?.trim()) },
      )
      body = { reason: result.value }
    } else if (confirmations[command]) {
      const [title, message] = confirmations[command]
      await ElMessageBox.confirm(message, title, { type: 'warning' })
    }
  } catch (error) {
    if (error === 'cancel' || error === 'close') return false
    throw error
  }
  if (command === 'delete') await api.deleteTask(task.id)
  else await api.taskAction(task.id, command, body)
  return true
}
