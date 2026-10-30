/** Worker 名称来自执行环境；未配置名称时显示 Worker ID，避免与数字员工混淆。 */
export function workerDisplayName(worker: {
  id: string
  execution_environment?: { name: string } | null
} | null | undefined): string {
  return worker?.execution_environment?.name?.trim() || (worker ? `Worker · ${worker.id}` : '未分配 Worker')
}
