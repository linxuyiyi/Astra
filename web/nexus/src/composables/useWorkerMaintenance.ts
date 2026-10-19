import { onBeforeUnmount, ref, toValue, watch, type MaybeRefOrGetter } from 'vue'
import {
  MAINTENANCE_EVENT_PAGE_SIZE,
  cancelMaintenance,
  getMaintenanceEventsPage,
  getMaintenanceOperation,
  getWorkerHealth,
  listMaintenanceOperations,
  retryMaintenance,
  submitMaintenance,
  subscribeMaintenanceEvents,
  type MaintenanceAction,
  type MaintenanceEvent,
  type MaintenanceOperation,
  type WorkerHealth,
} from '@/api/maintenance'
import { isMaintenanceActive, maintenanceCopy } from '@/features/workers/presentation'

export type MaintenanceStreamState = 'idle' | 'live' | 'reconnecting' | 'polling' | 'ended'

const POLL_INTERVAL_MS = 5000
const STATUS_REFRESH_MS = 10000
const RECONNECT_LIMIT = 3

export function useWorkerMaintenance(environmentId: MaybeRefOrGetter<number | null>) {
  const health = ref<WorkerHealth | null>(null)
  const operations = ref<MaintenanceOperation[]>([])
  const operation = ref<MaintenanceOperation | null>(null)
  const events = ref<MaintenanceEvent[]>([])
  const loading = ref(false)
  const healthError = ref('')
  const actionError = ref('')
  const busyAction = ref<MaintenanceAction | 'cancel' | 'retry' | null>(null)
  const streamState = ref<MaintenanceStreamState>('idle')
  const streamNote = ref('')
  const selectedOperationId = ref('')
  const historyCursor = ref(0)
  const historyHasMore = ref(false)
  const historyLoading = ref(false)
  const historyError = ref('')

  let streamController: AbortController | null = null
  let queryController: AbortController | null = null
  let reconnectTimer = 0
  let pollTimer = 0
  let statusTimer = 0
  let streamGeneration = 0
  let loadGeneration = 0
  let selectionGeneration = 0
  let finalizing = false
  let polling = false
  let refreshingStatus = false
  let reconnectAttempts = 0
  let disposed = false
  const listeners = new Set<() => void>()

  function onChange(listener: () => void) {
    listeners.add(listener)
    return () => listeners.delete(listener)
  }

  function notifyChanged() {
    for (const listener of listeners) listener()
  }

  function currentEnvironment(): number | null {
    return toValue(environmentId)
  }

  function beginQuery(): AbortSignal {
    queryController?.abort()
    queryController = new AbortController()
    return queryController.signal
  }

  function abortQueries() {
    queryController?.abort()
    queryController = null
  }

  function isCurrent(environment: number, generation: number): boolean {
    return !disposed && currentEnvironment() === environment && generation === loadGeneration
  }

  function reset() {
    abortQueries()
    stopStream()
    loadGeneration += 1
    selectionGeneration += 1
    health.value = null
    operations.value = []
    operation.value = null
    events.value = []
    healthError.value = ''
    actionError.value = ''
    streamState.value = 'idle'
    streamNote.value = ''
    selectedOperationId.value = ''
    resetHistory()
  }

  function resetHistory() {
    historyCursor.value = 0
    historyHasMore.value = false
    historyLoading.value = false
    historyError.value = ''
  }

  async function load() {
    const environment = currentEnvironment()
    if (!environment) {
      reset()
      return
    }
    const generation = ++loadGeneration
    selectionGeneration += 1
    const signal = beginQuery()
    loading.value = true
    healthError.value = ''
    try {
      const [healthValue, operationList] = await Promise.all([
        getWorkerHealth(environment, signal),
        listMaintenanceOperations(environment, 20, signal),
      ])
      if (!isCurrent(environment, generation)) return
      health.value = healthValue
      operations.value = operationList.items
      const preferred =
        operationList.items.find((item) => item.id === healthValue.maintenance?.operation_id) ??
        operationList.items[0] ??
        null
      if (preferred) await selectOperation(preferred.id, { force: true })
      else {
        operation.value = null
        events.value = []
        selectedOperationId.value = ''
        resetHistory()
        stopStream()
        streamState.value = 'idle'
      }
    } catch (error) {
      if (!isCurrent(environment, generation) || signal.aborted) return
      health.value = null
      healthError.value = maintenanceCopy(error instanceof Error ? error.message : '维护状态加载失败')
    } finally {
      if (isCurrent(environment, generation)) loading.value = false
    }
  }

  async function refreshHealth() {
    const environment = currentEnvironment()
    if (!environment) return
    const signal = beginQuery()
    try {
      const [healthValue, operationList] = await Promise.all([
        getWorkerHealth(environment, signal),
        listMaintenanceOperations(environment, 20, signal),
      ])
      if (disposed || currentEnvironment() !== environment || signal.aborted) return
      health.value = healthValue
      operations.value = operationList.items
      healthError.value = ''
      if (operation.value) {
        const updated = operationList.items.find((item) => item.id === operation.value?.id)
        if (updated) operation.value = { ...operation.value, ...updated }
      }
    } catch (error) {
      if (disposed || currentEnvironment() !== environment || signal.aborted) return
      health.value = null
      healthError.value = maintenanceCopy(error instanceof Error ? error.message : '维护状态加载失败')
    }
  }

  async function selectOperation(id: string, options: { force?: boolean } = {}) {
    if (!id) return
    if (!options.force && selectedOperationId.value === id && operation.value) return
    const environment = currentEnvironment()
    if (!environment) return
    const generation = ++selectionGeneration
    const signal = beginQuery()
    stopStream()
    selectedOperationId.value = id
    actionError.value = ''
    resetHistory()
    try {
      const detail = await getMaintenanceOperation(id, signal)
      if (disposed || generation !== selectionGeneration || currentEnvironment() !== environment || signal.aborted) return
      operation.value = detail
      events.value = mergeEvents([], detail.events ?? [])
      updateOperationRow(detail)
      if (isMaintenanceActive(detail.state)) startStream()
      else setEndedNote(detail.state)
    } catch (error) {
      if (disposed || generation !== selectionGeneration || signal.aborted) return
      actionError.value = maintenanceCopy(error instanceof Error ? error.message : '维护详情加载失败')
    }
  }

  function startStream() {
    const current = operation.value
    const environment = currentEnvironment()
    if (!current || !environment || !isMaintenanceActive(current.state)) return
    stopStream()
    const generation = ++streamGeneration
    const controller = new AbortController()
    streamController = controller
    const afterId = lastEventId(events.value)
    const operationId = current.id
    streamState.value = 'live'
    streamNote.value = '正在接收维护日志'
    if (isMaintenanceActive(current.state)) {
      statusTimer = window.setInterval(() => { void refreshActiveOperation(generation, environment, operationId) }, STATUS_REFRESH_MS)
    }
    void subscribeMaintenanceEvents(operationId, {
      signal: controller.signal,
      afterId,
      lastEventId: afterId > 0 ? String(afterId) : undefined,
      onEvent: (event) => {
        if (generation !== streamGeneration || disposed || currentEnvironment() !== environment) return
        reconnectAttempts = 0
        events.value = mergeEvents(events.value, [event])
        if (operation.value && operation.value.id === operationId && event.stage) {
          operation.value = { ...operation.value, stage: event.stage }
        }
        streamState.value = 'live'
        streamNote.value = '日志已实时更新'
      },
      onKeepalive: () => {
        if (generation !== streamGeneration || disposed || currentEnvironment() !== environment) return
        streamState.value = 'live'
        streamNote.value = '已连接，等待新日志'
      },
      onEnd: () => {
        if (generation !== streamGeneration || disposed || currentEnvironment() !== environment) return
        stopStream()
        streamState.value = 'ended'
        streamNote.value = '操作已结束'
        void finishCurrent(operationId)
      },
    }).catch((error) => {
      if (generation !== streamGeneration || disposed || controller.signal.aborted || currentEnvironment() !== environment) return
      void reconnectAfterProbe(operationId, environment, generation, maintenanceCopy(error instanceof Error ? error.message : '日志订阅中断'))
    })
  }

  async function reconnectAfterProbe(operationId: string, environment: number, generation: number, reason: string) {
    try {
      const detail = await getMaintenanceOperation(operationId, beginQuery())
      if (generation !== streamGeneration || disposed || currentEnvironment() !== environment) return
      operation.value = detail
      events.value = mergeEvents(events.value, detail.events ?? [])
      updateOperationRow(detail)
      if (!isMaintenanceActive(detail.state)) {
        stopStream()
        setEndedNote(detail.state)
        await finishCurrent(operationId)
        return
      }
    } catch {
      /* 查询失败仍按活动操作重连 */
    }
    scheduleReconnect(reason)
  }

  async function refreshActiveOperation(generation: number, environment: number, operationId: string) {
    if (refreshingStatus || generation !== streamGeneration || disposed || currentEnvironment() !== environment) return
    refreshingStatus = true
    try {
      const detail = await getMaintenanceOperation(operationId, beginQuery())
      if (generation !== streamGeneration || disposed || currentEnvironment() !== environment) return
      operation.value = detail
      events.value = mergeEvents(events.value, detail.events ?? [])
      updateOperationRow(detail)
      if (!isMaintenanceActive(detail.state)) {
        stopStream()
        setEndedNote(detail.state)
        await finishCurrent(operationId)
      }
    } catch {
      /* SSE 仍可继续；状态刷新失败不改变连接状态 */
    } finally {
      refreshingStatus = false
    }
  }

  function scheduleReconnect(reason: string) {
    const current = operation.value
    if (!current || !isMaintenanceActive(current.state)) return
    streamController = null
    reconnectAttempts += 1
    if (reconnectAttempts > RECONNECT_LIMIT) {
      startPolling(reason)
      return
    }
    const delay = Math.min(8000, 1000 * 2 ** (reconnectAttempts - 1))
    streamState.value = 'reconnecting'
    streamNote.value = `${reason}，${Math.round(delay / 1000)} 秒后重连`
    window.clearTimeout(reconnectTimer)
    reconnectTimer = window.setTimeout(() => startStream(), delay)
  }

  function startPolling(reason: string) {
    stopStream()
    const environment = currentEnvironment()
    const operationId = operation.value?.id
    if (!environment || !operationId) return
    streamState.value = 'polling'
    streamNote.value = `${reason}；改为每 5 秒同步一次进度`
    pollTimer = window.setInterval(async () => {
      if (polling || disposed || currentEnvironment() !== environment) return
      polling = true
      try {
        const detail = await getMaintenanceOperation(operationId, beginQuery())
        if (disposed || currentEnvironment() !== environment) return
        operation.value = detail
        events.value = mergeEvents(events.value, detail.events ?? [])
        updateOperationRow(detail)
        if (!isMaintenanceActive(detail.state)) {
          stopStream()
          setEndedNote(detail.state)
          await finishCurrent(operationId)
        }
      } catch {
        streamNote.value = '暂时无法获取最新进度，将在下一个周期重试'
      } finally {
        polling = false
      }
    }, POLL_INTERVAL_MS)
  }

  async function loadHistoryPage(reset = false) {
    const current = operation.value
    const environment = currentEnvironment()
    if (!current || !environment) return
    const signal = beginQuery()
    historyLoading.value = true
    historyError.value = ''
    try {
      const page = await getMaintenanceEventsPage(
        current.id,
        reset ? 0 : historyCursor.value,
        MAINTENANCE_EVENT_PAGE_SIZE,
        signal,
      )
      if (disposed || currentEnvironment() !== environment || operation.value?.id !== current.id || signal.aborted) return
      if (reset && !isMaintenanceActive(current.state)) events.value = []
      events.value = mergeEvents(events.value, page.items)
      historyCursor.value = page.next_cursor ?? 0
      historyHasMore.value = page.next_cursor !== null
    } catch (error) {
      if (!disposed && !signal.aborted) {
        historyError.value = maintenanceCopy(error instanceof Error ? error.message : '维护日志分页加载失败')
      }
    } finally {
      if (!disposed) historyLoading.value = false
    }
  }

  async function finishCurrent(id: string) {
    const environment = currentEnvironment()
    if (finalizing || !environment || selectedOperationId.value !== id) return
    finalizing = true
    try {
      const signal = beginQuery()
      const [detailResult, healthResult, operationResult] = await Promise.allSettled([
        getMaintenanceOperation(id, signal),
        getWorkerHealth(environment, signal),
        listMaintenanceOperations(environment, 20, signal),
      ])
      if (disposed || currentEnvironment() !== environment || selectedOperationId.value !== id || signal.aborted) return
      if (detailResult.status !== 'fulfilled') return
      const detail = detailResult.value
      operation.value = detail
      events.value = mergeEvents(events.value, detail.events ?? [])
      health.value = healthResult.status === 'fulfilled' ? healthResult.value : null
      if (operationResult.status === 'fulfilled' && operationResult.value.items.length) {
        operations.value = operationResult.value.items
      }
      setEndedNote(detail.state)
      notifyChanged()
    } catch {
      /* 终态已由事件确认，详情补齐失败不阻断页面 */
    } finally {
      finalizing = false
    }
  }

  function setEndedNote(state: string) {
    streamState.value = 'ended'
    streamNote.value = state === 'INTERRUPTED' ? '维护执行已中断，需核对远端状态' : '操作已结束'
  }

  function updateOperationRow(detail: MaintenanceOperation) {
    const index = operations.value.findIndex((item) => item.id === detail.id)
    if (index >= 0) operations.value[index] = { ...operations.value[index], ...detail }
  }

  function stopStream() {
    streamGeneration += 1
    window.clearTimeout(reconnectTimer)
    window.clearInterval(pollTimer)
    window.clearInterval(statusTimer)
    reconnectTimer = 0
    pollTimer = 0
    statusTimer = 0
    streamController?.abort()
    streamController = null
  }

  async function submit(action: MaintenanceAction): Promise<MaintenanceOperation | null> {
    const environment = currentEnvironment()
    if (!environment) return null
    busyAction.value = action
    actionError.value = ''
    try {
      const created = await submitMaintenance(environment, action)
      if (disposed || currentEnvironment() !== environment) return null
      operations.value = [created, ...operations.value.filter((item) => item.id !== created.id)]
      selectedOperationId.value = created.id
      operation.value = created
      events.value = []
      resetHistory()
      startStream()
      void refreshHealth()
      return created
    } catch (error) {
      if (!disposed) actionError.value = maintenanceCopy(error instanceof Error ? error.message : '维护操作提交失败')
      return null
    } finally {
      if (!disposed) busyAction.value = null
    }
  }

  async function cancel(): Promise<boolean> {
    const current = operation.value
    const environment = currentEnvironment()
    if (!current || !environment) return false
    busyAction.value = 'cancel'
    actionError.value = ''
    try {
      const updated = await cancelMaintenance(current.id)
      if (disposed || currentEnvironment() !== environment || operation.value?.id !== current.id) return false
      operation.value = { ...current, ...updated }
      updateOperationRow(updated)
      if (!isMaintenanceActive(updated.state)) {
        stopStream()
        setEndedNote(updated.state)
        await finishCurrent(updated.id)
      } else {
        streamNote.value = '已请求取消，等待安全检查点'
      }
      return true
    } catch (error) {
      if (!disposed) actionError.value = maintenanceCopy(error instanceof Error ? error.message : '取消维护失败')
      return false
    } finally {
      if (!disposed) busyAction.value = null
    }
  }

  async function retry(): Promise<MaintenanceOperation | null> {
    const current = operation.value
    const environment = currentEnvironment()
    if (!current || !environment) return null
    busyAction.value = 'retry'
    actionError.value = ''
    try {
      const updated = await retryMaintenance(current.id)
      if (disposed || currentEnvironment() !== environment || operation.value?.id !== current.id) return null
      operation.value = updated
      events.value = []
      resetHistory()
      selectedOperationId.value = updated.id
      updateOperationRow(updated)
      startStream()
      void refreshHealth()
      return updated
    } catch (error) {
      if (!disposed) actionError.value = maintenanceCopy(error instanceof Error ? error.message : '重试维护失败')
      return null
    } finally {
      if (!disposed) busyAction.value = null
    }
  }

  function clearActionError() {
    actionError.value = ''
  }

  watch(
    () => toValue(environmentId),
    (environment, previous) => {
      if (environment !== previous) reset()
      void load()
    },
    { immediate: true },
  )

  onBeforeUnmount(() => {
    disposed = true
    abortQueries()
    stopStream()
    listeners.clear()
  })

  return {
    health,
    operations,
    operation,
    events,
    loading,
    healthError,
    actionError,
    busyAction,
    streamState,
    streamNote,
    selectedOperationId,
    historyHasMore,
    historyLoading,
    historyError,
    load,
    refreshHealth,
    selectOperation,
    submit,
    cancel,
    retry,
    loadHistoryPage,
    clearActionError,
    onChange,
  }
}

export type WorkerMaintenanceController = ReturnType<typeof useWorkerMaintenance>

function mergeEvents(current: MaintenanceEvent[], incoming: MaintenanceEvent[]): MaintenanceEvent[] {
  const byId = new Map<number, MaintenanceEvent>()
  for (const event of current) byId.set(event.id, event)
  for (const event of incoming) byId.set(event.id, event)
  return [...byId.values()].sort((left, right) => left.id - right.id)
}

function lastEventId(events: MaintenanceEvent[]): number {
  return events.reduce((max, event) => Math.max(max, event.id), 0)
}
