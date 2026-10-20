/** 星系首页 DTO。字段与 `GET /api/v1/nebula/*` 契约一一对应，不在前端派生业务状态。 */

export type InteractionStatus = "available" | "unavailable" | "unknown";
export type WorkerHealth = "online" | "offline" | "error" | "unknown";
export type WorkerOccupancy = "idle" | "running" | "waiting" | "unknown";
export type NebulaTaskStatus = "PENDING" | "RUNNING" | "WAITING" | "SUCCEEDED" | "FAILED" | "CANCELED";

export interface NebulaCounts {
  running: number;
  queued: number;
  waiting: number;
  failed: number;
  completedToday: number;
}

export interface NebulaEmployee {
  id: string;
  name: string;
  domain: string;
  description: string;
  avatar: string;
  enabled: boolean;
  interactionStatus: InteractionStatus;
  counts: NebulaCounts;
  workerIds: string[];
  createdAt: string;
}

export interface NebulaWorker {
  id: string;
  digitalEmployeeId: string | null;
  workerInstanceId: string | null;
  name: string;
  role: string;
  capabilityLabels: string[];
  environmentMode: string | null;
  environmentName: string | null;
  modelName: string | null;
  health: WorkerHealth;
  occupancy: WorkerOccupancy;
  taskCount: number;
  runningTaskCount: number;
  waitingTaskCount: number;
  lastHeartbeatAt: string | null;
}

export interface NebulaTask {
  id: string;
  title: string;
  status: NebulaTaskStatus;
  priority: string;
  taskType: string;
  responsibleEmployeeId: string | null;
  workerId: string | null;
  workerName: string | null;
  ownerId: string | null;
  ownerName: string | null;
  currentProgress: string;
  waitingReason: string;
  failureReason: string;
  runtimeConnection: string;
  currentSessionId: string | null;
  sessionStatus: string | null;
  pendingMessageId: string | null;
  sourceConversationId: string | null;
  sourceChannelId: string | null;
  inactivityAlert: boolean;
  conflict: boolean;
  conflictCodes: string[];
  createdAt: string;
  updatedAt: string;
  completedAt: string | null;
}

export interface NebulaExecutionEdge {
  digitalEmployeeId: string;
  workerId: string;
  running: number;
  waiting: number;
  unavailable: number;
  taskIdsPreview: string[];
}

export interface NebulaCollaborationDirection {
  fromEmployeeId: string;
  toEmployeeId: string;
  count: number;
  lastRequestedAt: string | null;
  references: Array<{ kind: string; id: string | null }>;
}

export interface NebulaCollaborationEdge {
  fromEmployeeId: string;
  toEmployeeId: string;
  requestCount: number;
  lastRequestedAt: string | null;
  latestEventId: string;
  recent: boolean;
  directions: NebulaCollaborationDirection[];
  references: Array<{ kind: string; id: string | null }>;
}

export interface NebulaTotals {
  employees: number;
  workers: number;
  workersOnline: number;
  workersOffline: number;
  tasksRunning: number;
  tasksQueued: number;
  tasksWaiting: number;
  tasksFailed: number;
  completedToday: number;
}

export interface NebulaConflict {
  code: string;
  entityType: string;
  entityId: string;
  message: string;
}

export interface NebulaWarning {
  code: string;
  message: string;
  count: number;
}

export interface NebulaSnapshot {
  snapshotVersion: string;
  generatedAt: string;
  timezone: string;
  degradedSources: string[];
  totals: NebulaTotals;
  employees: NebulaEmployee[];
  workers: NebulaWorker[];
  taskEdges: NebulaExecutionEdge[];
  collaborationEdges: NebulaCollaborationEdge[];
  queuedTasks: NebulaTask[];
  attentionTasks: NebulaTask[];
  unassignedTasks: NebulaTask[];
  unassigned: { workerCount: number; taskCount: number; counts: NebulaCounts };
  dataConflicts: NebulaConflict[];
  dataWarnings: NebulaWarning[];
}
