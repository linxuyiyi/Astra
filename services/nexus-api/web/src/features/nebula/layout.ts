import type {
  NebulaCollaborationEdge,
  NebulaEmployee,
  NebulaExecutionEdge,
  NebulaSnapshot,
  NebulaTask,
  NebulaWorker,
} from "./types";

export interface Point3 {
  x: number;
  y: number;
  z: number;
}

export interface GalaxyNode {
  employee: NebulaEmployee;
  position: Point3;
  color: string;
  dust: string;
  workerIds: string[];
  queue: NebulaTask[];
  queuedCount: number;
}

export interface WorkerNode {
  worker: NebulaWorker;
  employeeId: string | null;
  position: Point3;
  radius: number;
}

export interface ExecutionEdgeNode {
  key: string;
  employeeId: string;
  workerId: string;
  running: number;
  waiting: number;
  unavailable: number;
  taskIds: string[];
  from: Point3;
  to: Point3;
  control: Point3;
}

export interface CollaborationEdgeNode {
  key: string;
  edge: NebulaCollaborationEdge;
  from: Point3;
  to: Point3;
  control: Point3;
}

export interface TaskSatelliteNode {
  task: NebulaTask;
  employeeId: string;
  position: Point3;
  index: number;
}

export interface NebulaScene {
  galaxies: GalaxyNode[];
  workers: WorkerNode[];
  executionEdges: ExecutionEdgeNode[];
  collaborationEdges: CollaborationEdgeNode[];
  satellites: TaskSatelliteNode[];
  galaxyById: Map<string, GalaxyNode>;
  workerNodeById: Map<string, WorkerNode>;
  employeeById: Map<string, NebulaEmployee>;
  workerById: Map<string, NebulaWorker>;
  radius: number;
}

const PALETTE = [
  { glow: "#21d5ab", dust: "#a3ffe0" },
  { glow: "#1df0b3", dust: "#caffeb" },
  { glow: "#4ba9dd", dust: "#c0f2ff" },
  { glow: "#85bea5", dust: "#d9ffdc" },
  { glow: "#78b1ee", dust: "#d9e7ff" },
  { glow: "#5fd0d5", dust: "#d7fffb" },
];

export const STATUS_COLOR = {
  running: "#5df1b8",
  queued: "#f5bd69",
  waiting: "#ff9470",
  completed: "#8fcab8",
  failed: "#fb6484",
  collaboration: "#7caec1",
  collaborationActive: "#b9f5ff",
} as const;

export function hash32(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function employeeHome(id: string): Point3 {
  const hash = hash32(id);
  const angle = ((hash % 4096) / 4096) * Math.PI * 2;
  const radius = 260 + (((hash >>> 12) % 1024) / 1024) * 170;
  const height = (((hash >>> 22) % 512) / 512 - 0.5) * 340;
  return {
    x: Math.cos(angle) * radius,
    y: height,
    z: Math.sin(angle) * radius * 0.58,
  };
}

function distance(left: Point3, right: Point3): number {
  return Math.hypot(left.x - right.x, left.y - right.y, left.z - right.z);
}

function separate(positions: Map<string, Point3>): void {
  const ids = [...positions.keys()].sort();
  const minimum = 365;
  for (let pass = 0; pass < 8; pass += 1) {
    let moved = false;
    for (let leftIndex = 0; leftIndex < ids.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < ids.length; rightIndex += 1) {
        const left = positions.get(ids[leftIndex]) as Point3;
        const right = positions.get(ids[rightIndex]) as Point3;
        const gap = distance(left, right);
        if (gap >= minimum) continue;
        const direction =
          gap > 0.001
            ? {
                x: (right.x - left.x) / gap,
                y: (right.y - left.y) / gap * 0.7,
                z: (right.z - left.z) / gap,
              }
            : { x: 1, y: 0, z: 0 };
        const push = (minimum - gap) * 0.55;
        right.x += direction.x * push;
        right.y += direction.y * push;
        right.z += direction.z * push;
        moved = true;
      }
    }
    if (!moved) break;
  }
}

export function orbitPosition(center: Point3, radius: number, angle: number, tilt = 0.62): Point3 {
  return {
    x: center.x + Math.cos(angle) * radius,
    y: center.y + Math.sin(angle) * radius * tilt,
    z: center.z + Math.sin(angle + 0.33) * radius * 0.72,
  };
}

export function galaxyColor(employee: NebulaEmployee, paletteColor: string): string {
  const counts = employee.counts;
  if (counts.failed > 0) return STATUS_COLOR.failed;
  if (counts.waiting > 0) return STATUS_COLOR.waiting;
  if (counts.running > 0) return STATUS_COLOR.running;
  return paletteColor;
}

export function buildScene(snapshot: NebulaSnapshot): NebulaScene {
  const employees = [...snapshot.employees].sort((left, right) => left.id.localeCompare(right.id));
  const homes = new Map<string, Point3>();
  for (const employee of employees) homes.set(employee.id, employeeHome(employee.id));
  separate(homes);

  const employeeById = new Map(snapshot.employees.map((item) => [item.id, item]));
  const workerById = new Map(snapshot.workers.map((item) => [item.id, item]));
  const workersByEmployee = new Map<string, NebulaWorker[]>();
  for (const worker of snapshot.workers) {
    const owner = worker.digitalEmployeeId;
    if (!owner) continue;
    const list = workersByEmployee.get(owner) ?? [];
    list.push(worker);
    workersByEmployee.set(owner, list);
  }
  for (const list of workersByEmployee.values()) list.sort((left, right) => left.id.localeCompare(right.id));

  const queuedByEmployee = new Map<string, NebulaTask[]>();
  for (const task of snapshot.queuedTasks) {
    if (!task.ownerId) continue;
    const list = queuedByEmployee.get(task.ownerId) ?? [];
    list.push(task);
    queuedByEmployee.set(task.ownerId, list);
  }

  const galaxies: GalaxyNode[] = [];
  const galaxyById = new Map<string, GalaxyNode>();
  for (const employee of employees) {
    const palette = PALETTE[hash32(employee.id) % PALETTE.length];
    const node: GalaxyNode = {
      employee,
      position: homes.get(employee.id) as Point3,
      color: galaxyColor(employee, palette.glow),
      dust: palette.dust,
      workerIds: [...employee.workerIds].sort(),
      queue: (queuedByEmployee.get(employee.id) ?? []).slice(0, 5),
      queuedCount: employee.counts.queued,
    };
    galaxies.push(node);
    galaxyById.set(employee.id, node);
  }

  const workers: WorkerNode[] = [];
  const workerNodeById = new Map<string, WorkerNode>();
  for (const galaxy of galaxies) {
    const owned = (workersByEmployee.get(galaxy.employee.id) ?? []).filter((worker) =>
      galaxy.workerIds.includes(worker.id),
    );
    owned.forEach((worker, index) => {
      const count = owned.length;
      const base = (hash32(galaxy.employee.id) % 360) / 360 * Math.PI * 2 - 1.25;
      const angle = base + (index / Math.max(1, count)) * Math.PI * 2;
      const radius = count === 1 ? 108 : 112 + (index % 2) * 10;
      const node: WorkerNode = {
        worker,
        employeeId: galaxy.employee.id,
        position: orbitPosition(galaxy.position, radius, angle, 0.7),
        radius,
      };
      workers.push(node);
      workerNodeById.set(worker.id, node);
    });
  }

  const satellites: TaskSatelliteNode[] = [];
  for (const galaxy of galaxies) {
    galaxy.queue.forEach((task, index) => {
      const base = (hash32(galaxy.employee.id) % 360) / 360 * Math.PI * 2 + 0.5;
      const angle = base + (index / Math.max(5, galaxy.queue.length)) * Math.PI * 2;
      satellites.push({
        task,
        employeeId: galaxy.employee.id,
        position: orbitPosition(galaxy.position, 54 + (index % 2) * 11, angle, 0.46),
        index,
      });
    });
  }

  const executionEdges: ExecutionEdgeNode[] = [];
  for (const edge of snapshot.taskEdges) {
    const from = galaxyById.get(edge.digitalEmployeeId)?.position;
    const to = workerNodeById.get(edge.workerId)?.position;
    if (!from || !to) continue;
    executionEdges.push(executionEdgeNode(edge, from, to));
  }

  const collaborationEdges: CollaborationEdgeNode[] = [];
  for (const edge of snapshot.collaborationEdges) {
    const from = galaxyById.get(edge.fromEmployeeId)?.position;
    const to = galaxyById.get(edge.toEmployeeId)?.position;
    if (!from || !to) continue;
    const middle = { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2, z: (from.z + to.z) / 2 };
    const dx = to.x - from.x;
    const dz = to.z - from.z;
    collaborationEdges.push({
      key: collaborationKey(edge.fromEmployeeId, edge.toEmployeeId),
      edge,
      from,
      to,
      control: {
        x: middle.x - dz * 0.1,
        y: middle.y + 40,
        z: middle.z + dx * 0.1 + 110,
      },
    });
  }

  let radius = 760;
  for (const galaxy of galaxies) {
    radius = Math.max(radius, Math.hypot(galaxy.position.x, galaxy.position.y, galaxy.position.z) + 220);
  }

  return {
    galaxies,
    workers,
    executionEdges,
    collaborationEdges,
    satellites,
    galaxyById,
    workerNodeById,
    employeeById,
    workerById,
    radius,
  };
}

function executionEdgeNode(edge: NebulaExecutionEdge, from: Point3, to: Point3): ExecutionEdgeNode {
  return {
    key: `${edge.digitalEmployeeId}:${edge.workerId}`,
    employeeId: edge.digitalEmployeeId,
    workerId: edge.workerId,
    running: edge.running,
    waiting: edge.waiting,
    unavailable: edge.unavailable,
    taskIds: edge.taskIdsPreview,
    from,
    to,
    control: {
      x: (from.x + to.x) / 2,
      y: (from.y + to.y) / 2 - 8,
      z: Math.max(from.z, to.z) + 20,
    },
  };
}

export function collaborationKey(left: string, right: string): string {
  return [left, right].sort().join(":");
}

export function bezierPoint(start: Point3, control: Point3, end: Point3, unit: number): Point3 {
  const inverse = 1 - unit;
  return {
    x: inverse * inverse * start.x + 2 * inverse * unit * control.x + unit * unit * end.x,
    y: inverse * inverse * start.y + 2 * inverse * unit * control.y + unit * unit * end.y,
    z: inverse * inverse * start.z + 2 * inverse * unit * control.z + unit * unit * end.z,
  };
}

export function bezierPath(
  start: Point3,
  control: Point3,
  end: Point3,
  steps = 34,
): Point3[] {
  const points: Point3[] = [];
  for (let index = 0; index <= steps; index += 1) {
    points.push(bezierPoint(start, control, end, index / steps));
  }
  return points;
}

export const STATUS_TEXT: Record<string, string> = {
  PENDING: "排队中",
  RUNNING: "执行中",
  WAITING: "等待人工",
  SUCCEEDED: "已完成",
  FAILED: "执行异常",
  CANCELED: "已取消",
};

export const RUNTIME_TEXT: Record<string, string> = {
  CONNECTED: "已连接",
  UNAVAILABLE: "连接异常",
  UNKNOWN: "状态未知",
};

export const SESSION_TEXT: Record<string, string> = {
  RUNNING: "执行中",
  WAITING: "等待人工",
  SUCCEEDED: "本轮已结束",
  FAILED: "执行失败",
  INTERRUPTED: "已中断",
  UNKNOWN: "状态待确认",
};

export const HEALTH_TEXT: Record<string, string> = {
  online: "在线",
  offline: "离线",
  error: "异常",
  unknown: "状态未知",
};

export const OCCUPANCY_TEXT: Record<string, string> = {
  idle: "空闲",
  running: "执行中",
  waiting: "等待人工",
  unknown: "状态未知",
};

export function priorityText(priority: string): string {
  return (
    {
      VERY_HIGH: "紧急",
      HIGH: "高",
      MEDIUM: "中",
      LOW: "低",
    }[priority] ?? priority
  );
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}
