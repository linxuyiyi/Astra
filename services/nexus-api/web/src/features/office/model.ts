import type { NebulaEmployee, NebulaTask, NebulaWorker } from "../nebula/types";

export interface OfficeWorker extends NebulaWorker {
  association: "task" | "dispatch";
  tasks: NebulaTask[];
  employeeTaskCounts: { RUNNING: number; WAITING: number };
}
export interface EmployeeOffice {
  employee: NebulaEmployee;
  generatedAt: string;
  degradedSources: string[];
  workers: OfficeWorker[];
  workerTotal: number;
  workerPage: number;
  issues: NebulaTask[];
  issueTotal: number;
  issuePage: number;
  pageSize: number;
}
export type OfficeState =
  | "attention"
  | "working"
  | "idle"
  | "offline"
  | "unknown";
export const STATE_TEXT: Record<OfficeState, string> = {
  attention: "需要关注",
  working: "工作中",
  idle: "空闲待命",
  offline: "已停用",
  unknown: "状态待确认",
};
export function employeeState(
  employee: NebulaEmployee,
  degraded = false,
): OfficeState {
  if (!employee.enabled) return "offline";
  if (degraded) return "unknown";
  if (employee.counts.waiting || employee.counts.failed) return "attention";
  if (employee.counts.running) return "working";
  return "idle";
}
export function workerState(worker: OfficeWorker): OfficeState {
  if (
    worker.health !== "online" ||
    worker.occupancy === "waiting" ||
    worker.tasks.some(needsAttention)
  )
    return "attention";
  if (worker.occupancy === "running") return "working";
  return worker.occupancy === "idle" ? "idle" : "unknown";
}
export function needsAttention(task: NebulaTask): boolean {
  return (
    task.status === "WAITING" ||
    task.status === "FAILED" ||
    task.runtimeConnection === "UNAVAILABLE" ||
    task.inactivityAlert ||
    task.conflict
  );
}
export function taskReason(task: NebulaTask): string {
  if (task.status === "WAITING")
    return task.waitingReason || "等待人工确认，请进入任务查看";
  if (task.status === "FAILED")
    return task.failureReason || "任务执行失败，请查看失败详情";
  if (task.runtimeConnection === "UNAVAILABLE")
    return "执行连接不可用，请检查运行环境";
  if (task.inactivityAlert) return "任务长时间无活动，需要检查";
  if (task.conflict) return "任务关联存在冲突，请检查责任员工与执行资源";
  return (
    task.currentProgress ||
    (task.status === "PENDING" ? "等待 Worker 调度" : "正在执行")
  );
}
export function stationPositions(
  count: number,
): Array<{ x: number; y: number }> {
  const columns = Math.min(3, count);
  return Array.from({ length: count }, (_, i) => ({
    x: 740 + ((i % 3) - (columns - 1) / 2) * 405,
    y: count > 3 ? 380 + Math.floor(i / 3) * 300 : 475,
  }));
}

export function officeCamera(
  width: number,
  height: number,
  count: number,
  selectedIndex = -1,
) {
  const mobile = width < 760;
  const wallHeight = mobile
    ? 112
    : Math.min(196, Math.max(112, height * 0.218));
  const panelWidth =
    selectedIndex < 0 || mobile
      ? 0
      : Math.min(440, Math.max(365, width * 0.315));
  const visibleWidth = width - panelWidth;
  const visibleHeight = selectedIndex >= 0 && mobile ? height * 0.44 : height;
  const floorHeight = Math.max(80, visibleHeight - wallHeight - 80);
  const base = Math.max(
    0.12,
    Math.min(
      (width - 110) / 1240,
      (height - wallHeight - 100) / (count > 3 ? 625 : 365),
    ),
  );
  const station = stationPositions(count)[selectedIndex];
  const scale = station
    ? Math.min(
        mobile ? 1 : base * 1.18,
        Math.max(0.3, (visibleWidth - 70) / 400),
        Math.max(0.2, floorHeight / 280),
      )
    : base;
  const x = station?.x ?? 740;
  const y = station ? station.y + 15 : count > 3 ? 550 : 490;
  return {
    scale,
    x: visibleWidth / 2 - x * scale,
    y: wallHeight + floorHeight / 2 - y * scale,
    wallHeight,
  };
}
