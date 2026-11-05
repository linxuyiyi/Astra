<template>
  <div class="page">
    <AstraPageHeader title="任务" subtitle="任务执行、人工处理与确认" />
    <div class="toolbar">
      <el-input
        v-model="search"
        clearable
        placeholder="搜索任务编号或标题"
        @change="filterChanged"
      />
      <el-select
        v-model="filter"
        clearable
        placeholder="全部状态"
        @change="filterChanged"
        ><el-option
          v-for="s in statuses"
          :key="s"
          :value="s"
          :label="statusLabel(s)" /></el-select
      ><el-button @click="load">刷新</el-button
      ><el-button type="primary" @click="creating = true">创建任务</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" show-icon
      ><el-button @click="load">重试</el-button></el-alert
    >
    <el-table v-loading="loading" :data="tasks" @row-click="openTask">
      <el-table-column
        prop="title"
        label="任务"
        min-width="300"
        show-overflow-tooltip
      />
      <el-table-column label="状态" width="140"
        ><template #default="{ row }"
          ><el-tag :type="statusTagType(row.status)">{{
            statusLabel(row.status)
          }}</el-tag></template
        ></el-table-column
      >
      <el-table-column prop="agent_worker_id" label="Worker" width="180" />
      <el-table-column
        prop="current_progress"
        label="进展"
        min-width="280"
        show-overflow-tooltip
      />
      <el-table-column label="更新" width="160"
        ><template #default="{ row }">{{
          formatTime(row.updated_at)
        }}</template></el-table-column
      >
      <template #empty><el-empty description="暂无任务" /></template>
    </el-table>
    <el-pagination
      :current-page="page"
      :page-size="50"
      layout="prev, pager, next"
      :total="(page - 1) * 50 + tasks.length + (tasks.length === 50 ? 1 : 0)"
      @current-change="changePage"
    />
    <TaskCreateDrawer
      :open="creating"
      @close="creating = false"
      @created="created"
      @saved="created"
    />
  </div>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import AstraPageHeader from '@/components/astra/AstraPageHeader.vue'
import TaskCreateDrawer from '@/components/task/TaskCreateDrawer.vue'
import { api } from '@/api/client'
import type { Task } from '@/api/types'
import { statusLabel, statusTagType, formatTime } from '@/utils/format'
const router = useRouter(),
  tasks = ref<Task[]>([]),
  loading = ref(false),
  error = ref(''),
  filter = ref(''),
  search = ref(''),
  page = ref(1),
  creating = ref(false)
const statuses = ['PENDING', 'RUNNING', 'WAITING', 'SUCCEEDED', 'CANCELED']
async function load() {
  loading.value = true
  try {
    tasks.value = await api.tasks(
      `?limit=50&offset=${(page.value - 1) * 50}${filter.value ? `&status=${filter.value}` : ''}&q=${encodeURIComponent(search.value)}`,
    )
    error.value = ''
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}
function filterChanged() {
  page.value = 1
  void load()
}
function created() {
  creating.value = false
  void load()
}
function openTask(row: Task) {
  void router.push(`/tasks/${row.id}`)
}
function changePage(v: number) {
  page.value = v
  void load()
}
onMounted(load)
</script>
<style scoped>
.page {
  padding: 24px;
}
.toolbar {
  display: flex;
  gap: 12px;
  margin: 20px 0;
}
.toolbar .el-input {
  width: 300px;
}
.toolbar .el-select {
  width: 180px;
}
.el-pagination {
  margin-top: 20px;
}
</style>
