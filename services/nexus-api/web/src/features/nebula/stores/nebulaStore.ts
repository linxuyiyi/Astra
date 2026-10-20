import { computed, reactive, ref } from "vue";
import { defineStore } from "pinia";
import { getNebulaSnapshot } from "../api";
import type { NebulaCollaborationEdge, NebulaSnapshot } from "../types";

export type NebulaConnection = "connecting" | "live" | "stale" | "offline";

const PULSE_MS = 9000;
const FLASH_MS = 4200;
const POLL_MS = 15_000;

function collabKey(edge: NebulaCollaborationEdge): string {
  return [edge.fromEmployeeId, edge.toEmployeeId].sort().join(":");
}

export const useNebulaStore = defineStore("nebula", () => {
  const snapshot = ref<NebulaSnapshot | null>(null);
  const loading = ref(false);
  const error = ref("");
  const connection = ref<NebulaConnection>("connecting");
  const lastConfirmedAt = ref("");
  const lastErrorAt = ref("");
  const pulses = reactive<Record<string, number>>({});
  const flashes = reactive<Record<string, number>>({});

  const employees = computed(() => snapshot.value?.employees ?? []);
  const workers = computed(() => snapshot.value?.workers ?? []);
  const employeeById = computed(
    () => new Map(employees.value.map((item) => [item.id, item])),
  );
  const workerById = computed(() => new Map(workers.value.map((item) => [item.id, item])));

  let pollTimer = 0;
  let refreshTimer = 0;
  let reconnectTimer = 0;
  let source: EventSource | null = null;
  let stopped = true;
  let inFlight = false;
  let queued = false;
  let failures = 0;

  function applySnapshot(next: NebulaSnapshot): void {
    const previous = snapshot.value;
    if (previous) {
      const previousTime = new Date(previous.generatedAt).getTime();
      const nextTime = new Date(next.generatedAt).getTime();
      if (Number.isFinite(previousTime) && Number.isFinite(nextTime) && nextTime < previousTime) {
        return;
      }
      const previousEdges = new Map(
        previous.collaborationEdges.map((edge) => [collabKey(edge), edge]),
      );
      for (const edge of next.collaborationEdges) {
        const key = collabKey(edge);
        const before = previousEdges.get(key);
        if (!before) {
          pulses[key] = Date.now() + PULSE_MS;
          continue;
        }
        const changed =
          edge.requestCount > before.requestCount ||
          edge.lastRequestedAt !== before.lastRequestedAt;
        if (changed) pulses[key] = Date.now() + PULSE_MS;
      }
    }
    snapshot.value = next;
    lastConfirmedAt.value = next.generatedAt;
    error.value = "";
  }

  async function refresh(options: { silent?: boolean } = {}): Promise<void> {
    if (stopped) return;
    if (inFlight) {
      queued = true;
      return;
    }
    inFlight = true;
    if (!options.silent) loading.value = true;
    try {
      applySnapshot(await getNebulaSnapshot());
      if (connection.value === "offline") connection.value = "connecting";
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : "星系快照加载失败";
      lastErrorAt.value = new Date().toISOString();
      if (!snapshot.value) connection.value = "offline";
    } finally {
      loading.value = false;
      inFlight = false;
      if (queued) {
        queued = false;
        void refresh({ silent: true });
      }
    }
  }

  function scheduleRefresh(delay: number): void {
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(() => void refresh({ silent: true }), delay);
  }

  function flashCompleted(workerId: string | null | undefined): void {
    if (!workerId) return;
    const worker = snapshot.value?.workers.find((item) => item.id === workerId);
    if (!worker?.digitalEmployeeId) return;
    flashes[worker.digitalEmployeeId] = Date.now() + FLASH_MS;
  }

  function handleEvent(payload: { type?: string; employee_id?: string | null }): void {
    if (payload.type === "TASK_SUCCEEDED") flashCompleted(payload.employee_id);
    if (
      payload.type === "TASK_CREATED" ||
      payload.type === "TASK_RUNNING" ||
      payload.type === "TASK_SUCCEEDED" ||
      payload.type === "TASK_FAILED" ||
      payload.type === "TASK_WAITING"
    ) {
      scheduleRefresh(350);
    }
  }

  function connectEvents(): void {
    if (stopped) return;
    window.clearTimeout(reconnectTimer);
    source?.close();
    source = new EventSource("/api/events/stream");
    connection.value = snapshot.value ? "stale" : "connecting";
    source.addEventListener("connected", () => {
      failures = 0;
      connection.value = "live";
    });
    source.addEventListener("update", (event) => {
      failures = 0;
      connection.value = "live";
      try {
        handleEvent(JSON.parse((event as MessageEvent).data) as {
          type?: string;
          employee_id?: string | null;
        });
      } catch {
        /* 事件只是失效通知，解析失败时交给周期快照兜底 */
      }
    });
    source.onopen = () => {
      failures = 0;
      connection.value = "live";
    };
    source.onerror = () => {
      connection.value = snapshot.value ? "stale" : "offline";
      source?.close();
      source = null;
      if (stopped) return;
      failures += 1;
      const delay = Math.min(30_000, 1_000 * 2 ** Math.min(failures, 5));
      window.clearTimeout(reconnectTimer);
      reconnectTimer = window.setTimeout(connectEvents, delay);
    };
  }

  function onVisibility(): void {
    if (document.hidden) return;
    void refresh({ silent: true });
    if (!source) connectEvents();
  }

  function start(): void {
    if (!stopped) return;
    stopped = false;
    void refresh();
    connectEvents();
    pollTimer = window.setInterval(() => void refresh({ silent: true }), POLL_MS);
    document.addEventListener("visibilitychange", onVisibility);
  }

  function stop(): void {
    stopped = true;
    window.clearInterval(pollTimer);
    window.clearTimeout(refreshTimer);
    window.clearTimeout(reconnectTimer);
    source?.close();
    source = null;
    document.removeEventListener("visibilitychange", onVisibility);
  }

  return {
    snapshot,
    loading,
    error,
    connection,
    lastConfirmedAt,
    lastErrorAt,
    pulses,
    flashes,
    employees,
    workers,
    employeeById,
    workerById,
    start,
    stop,
    refresh,
    flashCompleted,
  };
});
