import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/api/client'
import type { AstraEvent, DashboardOverview, Employee, Metrics, Task, TaskCategory } from '@/api/types'

export const useAstraStore = defineStore('astra', () => {
  const metrics = ref<Metrics | null>(null)
  const employees = ref<Employee[]>([])
  const tasks = ref<Task[]>([])
  const categories = ref<TaskCategory[]>([])
  const events = ref<AstraEvent[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const lastUpdated = ref<Date | null>(null)
  const homeOverview = ref<DashboardOverview | null>(null)
  let refreshRequest: Promise<void> | null = null

  const activeTasks = computed(() =>
    tasks.value.filter(
      (task) =>
        task.employee_id &&
        ['PENDING', 'RUNNING', 'WAITING', 'FAILED'].includes(task.status),
    ),
  )

  function refresh() {
    if (refreshRequest) return refreshRequest
    refreshRequest = loadSnapshot().finally(() => { refreshRequest = null })
    return refreshRequest
  }

  async function loadSnapshot() {
    loading.value = true
    try {
      const [metricsValue, employeeValue, taskValue, categoryValue, eventValue] = await Promise.all([
        api.metrics(), api.employees(), api.tasks(), api.categories(), api.events(40),
      ])
      metrics.value = metricsValue
      employees.value = employeeValue
      tasks.value = taskValue
      categories.value = categoryValue
      events.value = eventValue
      error.value = null
      lastUpdated.value = new Date()
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : '控制台数据加载失败'
    } finally {
      loading.value = false
    }
  }

  return { metrics, employees, tasks, categories, events, activeTasks, loading, error, lastUpdated, homeOverview, refresh }
})
