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
  if (employee.counts.waiting) return "attention";
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
  return task.status === "WAITING" || task.conflict;
}
export function taskReason(task: NebulaTask): string {
  if (task.status === "WAITING") return task.waitingReason || "等待人工处理";
  if (task.conflict) return "任务关联需要检查";
  return task.currentProgress || (task.status === "PENDING" ? "等待 Worker 调度" : "正在执行");
}
export const OFFICE_ROOM_SIZE = 12;
const NAMEPLATE_HEIGHT = 44;
export const OFFICE_WIDE_BREAKPOINT = 2240;

export function officeWorkbenchWidth(width: number): number {
  if (width < 760) return 0;
  if (width >= OFFICE_WIDE_BREAKPOINT) {
    return Math.min(540, Math.max(460, width * 0.205));
  }
  return Math.min(440, Math.max(365, width * 0.315));
}

export function officeWallLayout(width: number, wallHeight: number) {
  const aspectScaled = width > 1920;
  const wallScale = aspectScaled ? wallHeight / 196 : width / 1480;
  const contentWidth = aspectScaled ? 1480 * wallScale : width;
  const offsetX = aspectScaled ? (width - contentWidth) / 2 : 0;
  return {
    aspectScaled,
    wallScale,
    contentWidth,
    offsetX,
  };
}

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

/** 始终按整页工位布局；桌面工作台适配宽度，2K 宽屏允许等比清晰放大，手机浮层不改变视角。 */
export function officeCamera(
  width: number,
  height: number,
  count: number,
  workbenchOpen = false,
) {
  const mobile = width < 760;
  const wide = width >= OFFICE_WIDE_BREAKPOINT;
  const wallHeight = mobile
    ? 112
    : wide
      ? Math.min(256, Math.max(196, height * 0.178))
      : Math.min(196, Math.max(112, height * 0.218));
  const panelWidth = workbenchOpen ? officeWorkbenchWidth(width) : 0;
  const visibleWidth = width - panelWidth;
  const floorTop = wallHeight + (wide ? 34 : 28);
  const floorBottom = height - (wide ? 108 : 96);
  const floorHeight = Math.max(60, floorBottom - floorTop);
  const positions = stationPositions(count, width);
  const xs = positions.map((station) => station.x);
  const ys = positions.map((station) => station.y);
  const minX = count ? Math.min(...xs) : 740;
  const maxX = count ? Math.max(...xs) : 740;
  const minY = count ? Math.min(...ys) : 400;
  const maxY = count ? Math.max(...ys) : 400;
  const maxScale = mobile ? 0.6 : wide ? 1.0 : Math.min(0.78, width / 2000);
  // 预留地毯、气泡及姓名牌；姓名牌保留44px屏幕高度，不跟着像素素材缩小。
  const scale = Math.max(
    0.08,
    Math.min(
      maxScale,
      (visibleWidth - (mobile ? 32 : wide ? 144 : 112)) / (maxX - minX + 350),
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
