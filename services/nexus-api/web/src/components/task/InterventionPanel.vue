<template>
  <div class="admin-page" data-testid="intervention-page">
    <div class="admin-page__toolbar" role="search" aria-label="待处理筛选">
      <div class="admin-page__filters">
        <ElButton
          v-for="state in states"
          :key="state"
          :type="selected === state ? 'primary' : undefined"
          :data-testid="`intervention-state-${state}`"
          @click="selected = state"
        >{{ runtimeStatusLabel(state) }} · {{ countOf(state) }}</ElButton>
      </div>
      <div class="admin-page__search">
        <ElButton :icon="RefreshCw" :loading="store.loading" data-testid="intervention-refresh" @click="store.refresh()">刷新</ElButton>
      </div>
    </div>

    <ElTable v-loading="store.loading" :data="paged" row-key="id" data-testid="intervention-table">
      <ElTableColumn label="任务" min-width="260">
        <template #default="{ row }">
          <div class="intervention-task">
            <RouterLink :to="`/tasks/${row.id}`"><strong>{{ row.title }}</strong></RouterLink>
            <ElText type="info" truncated>{{ row.id }} · {{ row.human_required_reason || row.runtime_error || row.failure_reason || row.current_progress || '—' }}</ElText>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="状态" width="220">
        <template #default="{ row }">
          <ElSpace :size="6" wrap>
            <RuntimeStatusBadge :projection="row as Task" :show-detail="false" />
            <ElTag v-if="row.inactivity_alert" type="warning" size="small">长时间无活动</ElTag>
          </ElSpace>
        </template>
      </ElTableColumn>
      <ElTableColumn label="操作" width="240" fixed="right">
        <template #default="{ row }">
          <ElSpace class="astra-row-actions" :size="4">
            <ElButton link type="primary" :icon="ExternalLink" @click="openTask(row as Task)">详情</ElButton>
            <ElButton v-if="row.status === 'WAITING'" link type="primary" @click="run(row as Task, 'confirm')">确认完成</ElButton>
            <ElButton v-if="row.status === 'WAITING'" link type="danger" @click="run(row as Task, 'stop')">终止</ElButton>
            <ElButton v-if="row.status === 'FAILED'" link type="primary" @click="run(row as Task, 'restart')">重试</ElButton>
          </ElSpace>
        </template>
      </ElTableColumn>
      <template #empty>
        <ElEmpty v-if="!store.loading" :image-size="72" description="当前没有待处理任务" />
      </template>
    </ElTable>

    <div class="admin-page__footer">
      <ElPagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50]"
        :total="tasks.length"
        layout="total, sizes, prev, pager, next"
        aria-label="待处理任务分页"
        data-testid="intervention-pagination"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ExternalLink, RefreshCw } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { ElButton, ElEmpty, ElPagination, ElSpace, ElTable, ElTableColumn, ElTag, ElText, vLoading } from 'element-plus'
import { api } from '@/api/client'
import type { Task } from '@/api/types'
import { notify } from '@/composables/useToast'
import RuntimeStatusBadge from '@/components/runtime/RuntimeStatusBadge.vue'
import { runtimeStatusLabel } from '@/features/runtime/status'
import { useAstraStore } from '@/stores/astra'

const store = useAstraStore()
const router = useRouter()
const states = ['WAITING', 'FAILED'] as const
const selected = ref<string>('WAITING')
const page = ref(1)
const pageSize = ref(20)
const tasks = computed(() => store.tasks.filter((task) => task.status === selected.value || task.inactivity_alert))
const paged = computed(() => tasks.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
watch([selected, pageSize], () => { page.value = 1 })
watch(() => tasks.value.length, (length) => {
  const maxPage = Math.max(1, Math.ceil(length / pageSize.value))
  if (page.value > maxPage) page.value = maxPage
})
const countOf = (state: string) => store.tasks.filter((task) => task.status === state).length
const openTask = (task: Task) => router.push(`/tasks/${task.id}`)
async function run(task: Task, action: string) {
  try {
    await api.taskAction(task.id, action)
    await store.refresh()
    notify('操作已提交')
  } catch (error) {
    notify(error instanceof Error ? error.message : '操作失败', 'error')
  }
}
onMounted(() => { void store.refresh() })
</script>

<style scoped>
.intervention-task { display: grid; gap: 2px; min-width: 0; }
.intervention-task strong, .intervention-task a { overflow: hidden; color: var(--astra-text-primary); font-size: var(--astra-font-label); font-weight: 700; text-overflow: ellipsis; white-space: nowrap; text-decoration: none; }
.intervention-task a:hover { color: var(--astra-primary); }
</style>
