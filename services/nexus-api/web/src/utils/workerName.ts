/** Worker 名称直接来自统一 Worker 配置。 */
export function workerDisplayName(worker: {id: string; name: string} | null | undefined): string {
  return worker?.name || (worker ? worker.id : '未分配 Worker')
}
