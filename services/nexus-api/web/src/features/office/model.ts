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
export const OFFICE_ROOM_SIZE = 12;
const NAMEPLATE_HEIGHT = 44;

export function stationPositions(
  count: number,
  width = 1280,
): Array<{ x: number; y: number }> {
  const preferredColumns =
    count === 4 ? 2 : count === 9 ? 3 : count > 6 && width >= 760 ? 4 : 3;
  const columns = Math.max(1, Math.min(count, preferredColumns));
  return Array.from({ length: count }, (_, index) => {
    const row = Math.floor(index / columns);
    const rowCount = Math.min(columns, count - row * columns);
    return {
      x: 740 + ((index % columns) - (rowCount - 1) / 2) * 405,
      y: 400 + row * (width < 760 ? 420 : 380),
    };
  });
}

/** 始终按整页工位布局；桌面工作台适配宽度，手机浮层不改变视角。 */
export function officeCamera(
  width: number,
  height: number,
  count: number,
  workbenchOpen = false,
) {
  const mobile = width < 760;
  const wallHeight = mobile
    ? 112
    : Math.min(196, Math.max(112, height * 0.218));
  const panelWidth =
    workbenchOpen && !mobile ? Math.min(440, Math.max(365, width * 0.315)) : 0;
  const visibleWidth = width - panelWidth;
  const floorTop = wallHeight + 28;
  const floorBottom = height - 96;
  const floorHeight = Math.max(60, floorBottom - floorTop);
  const positions = stationPositions(count, width);
  const xs = positions.map((station) => station.x);
  const ys = positions.map((station) => station.y);
  const minX = count ? Math.min(...xs) : 740;
  const maxX = count ? Math.max(...xs) : 740;
  const minY = count ? Math.min(...ys) : 400;
  const maxY = count ? Math.max(...ys) : 400;
  // 预留地毯、气泡及姓名牌；姓名牌保留44px屏幕高度，不跟着像素素材缩小。
  const scale = Math.max(
    0.08,
    Math.min(
      mobile ? 0.6 : Math.min(0.78, width / 2000),
      (visibleWidth - (mobile ? 32 : 112)) / (maxX - minX + 350),
      (floorHeight - NAMEPLATE_HEIGHT) / (maxY - minY + 310),
    ),
  );
  const contentHeight = (maxY - minY + 310) * scale + NAMEPLATE_HEIGHT;
  return {
    scale,
    x: visibleWidth / 2 - ((minX + maxX) / 2) * scale,
    y: floorTop + (floorHeight - contentHeight) / 2 + (160 - minY) * scale,
    wallHeight,
    labelScale: 1 / scale,
  };
}
