<template>
  <div class="page">
    <div class="toolbar">
      <h2>Workers</h2>
      <el-button @click="load">刷新</el-button
      ><el-button type="primary" @click="creating = true"
        >新增 Worker</el-button
      >
    </div>
    <el-alert v-if="error" :title="error" type="error" /><el-table
      v-loading="loading"
      :data="workers"
      @row-click="openWorker"
      ><el-table-column
        prop="name"
        label="Worker"
        min-width="200" /><el-table-column label="状态" width="140"
        ><template #default="{ row }"
          ><el-tag :type="statusTagType(row.status)">{{ statusLabel(row.status) }}</el-tag></template
        ></el-table-column
      ><el-table-column
        prop="host"
        label="主机"
        min-width="200" /><el-table-column
        prop="workspace_root"
        label="Workspace"
        min-width="250" /><el-table-column label="心跳" width="180"
        ><template #default="{ row }">{{
          formatTime(row.last_heartbeat_at)
        }}</template></el-table-column
      ><template #empty
        ><el-empty description="暂无 Worker" /></template></el-table
    ><el-dialog
      v-model="creating"
      title="创建 Worker"
      width="760px"
      destroy-on-close
      ><WorkerConfig
        @saved="configurationSaved"
    /></el-dialog>
  </div>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import type { Worker } from '@/api/types'
import { statusLabel, statusTagType, formatTime } from '@/utils/format'
import WorkerConfig from './settings/WorkerConfig.vue'
const router = useRouter(),
  workers = ref<Worker[]>([]),
  creating = ref(false),
  loading = ref(false),
  error = ref('')
function configurationSaved() {
  creating.value = false
  void load()
}
async function load() {
  loading.value = true
  try {
    workers.value = await api.workers()
    error.value = ''
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}
function openWorker(row: Worker) {
  void router.push(`/settings/workers/${row.id}`)
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
  align-items: center;
  margin-bottom: 20px;
}
.toolbar h2 {
  flex: 1;
}
</style>
