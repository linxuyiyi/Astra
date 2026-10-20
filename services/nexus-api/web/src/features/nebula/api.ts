import { currentUid } from "@/api";
import type {
  NebulaCollaborationResponse,
  NebulaEmployeeDetail,
  NebulaLinkTasks,
  NebulaSnapshot,
  NebulaTask,
  NebulaTaskPage,
} from "./types";

function ownerHeaders(): HeadersInit {
  const uid = currentUid();
  return uid ? { "X-Owner-Id": uid } : {};
}

async function read<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (typeof payload.detail === "string") detail = payload.detail;
    } catch {
      /* 保留状态文本 */
    }
    throw new Error(detail || "请求失败");
  }
  return (await response.json()) as T;
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export async function getNebulaSnapshot(): Promise<NebulaSnapshot> {
  return read(await fetch("/api/v1/nebula/snapshot"));
}

export async function getNebulaEmployee(employeeId: string): Promise<NebulaEmployeeDetail> {
  return read(
    await fetch(`/api/v1/nebula/employees/${encodeURIComponent(employeeId)}`, {
      headers: ownerHeaders(),
    }),
  );
}

export async function getNebulaTask(
  taskId: string,
): Promise<{ generatedAt: string; task: NebulaTask }> {
  return read(
    await fetch(`/api/v1/nebula/tasks/${encodeURIComponent(taskId)}`),
  );
}

export async function getNebulaEmployeeTasks(
  employeeId: string,
  params: { status?: string; priority?: string; q?: string; page?: number; pageSize?: number } = {},
): Promise<NebulaTaskPage> {
  return read(
    await fetch(
      `/api/v1/nebula/employees/${encodeURIComponent(employeeId)}/tasks${query(params)}`,
    ),
  );
}

export async function getNebulaLinkTasks(
  workerId: string,
  params: { status?: string; limit?: number } = {},
): Promise<NebulaLinkTasks> {
  return read(
    await fetch(`/api/v1/nebula/links/${encodeURIComponent(workerId)}/tasks${query(params)}`),
  );
}

export async function getNebulaCollaborations(params: {
  fromEmployeeId?: string;
  toEmployeeId?: string;
  since?: string;
} = {}): Promise<NebulaCollaborationResponse> {
  return read(
    await fetch(`/api/v1/nebula/collaborations${query(params)}`, {
      headers: ownerHeaders(),
    }),
  );
}

export async function confirmTaskCompletion(taskId: string): Promise<unknown> {
  return read(
    await fetch(`/api/tasks/${encodeURIComponent(taskId)}/confirm`, { method: "POST" }),
  );
}

export type { NebulaTask };
