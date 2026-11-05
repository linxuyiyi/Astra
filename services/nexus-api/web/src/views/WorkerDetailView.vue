<template>
  <section class="admin-page" v-loading="loading" data-testid="worker-detail">
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
      <el-button @click="load">重试</el-button>
    </el-alert>
    <template v-if="worker">
      <AstraPageHeader :title="worker.name" :description="worker.description">
        <el-tag :type="statusTagType(worker.status)">{{ statusLabel(worker.status) }}</el-tag>
        <template #actions>
          <el-button @click="router.push({ name: 'settings-workers' })">返回列表</el-button>
          <el-button @click="editing = true">编辑配置</el-button>
          <el-button @click="toggle">{{ worker.status === 'PAUSED' ? '恢复调度' : '暂停调度' }}</el-button>
          <el-button type="danger" plain @click="remove">删除</el-button>
        </template>
      </AstraPageHeader>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="主机">{{ worker.host || '—' }}</el-descriptions-item>
        <el-descriptions-item label="工作区">{{ worker.workspace_root }}</el-descriptions-item>
        <el-descriptions-item label="连接代次">{{ worker.epoch }}</el-descriptions-item>
        <el-descriptions-item label="最后心跳">{{ formatTime(worker.last_heartbeat_at) }}</el-descriptions-item>
        <el-descriptions-item label="模型路由">{{ worker.model_name || '—' }}</el-descriptions-item>
        <el-descriptions-item label="工程仓库">{{ worker.repository_url || '—' }}</el-descriptions-item>
      </el-descriptions>
      <WorkerMaintenancePanel :key="worker.id" :worker="worker" />
      <el-dialog v-model="editing" title="编辑 Worker" width="760px" destroy-on-close>
        <WorkerConfig :worker="worker" @saved="configurationSaved" />
      </el-dialog>
    </template>
  </section>
</template>
<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type { Worker } from '@/api/types'
import { statusLabel, statusTagType, formatTime } from '@/utils/format'
import AstraPageHeader from '@/components/astra/AstraPageHeader.vue'
import WorkerConfig from './settings/WorkerConfig.vue'
import WorkerMaintenancePanel from '@/components/worker/WorkerMaintenancePanel.vue'
const route = useRoute(),
  router = useRouter(),
  worker = ref<Worker>(),
  loading = ref(false),
  editing = ref(false),
  error = ref('')
function configurationSaved() {
  editing.value = false
  void load()
}
async function load() {
  loading.value = true
  try {
    worker.value = await api.worker(String(route.params.id))
    error.value = ''
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}
async function toggle() {
  try {
    worker.value = await api.workerAction(
      String(route.params.id),
      worker.value?.status === 'PAUSED' ? 'resume' : 'pause',
    )
  } catch (e) {
    error.value = String(e)
  }
}
async function remove() {
  try {
    await ElMessageBox.confirm(
      '删除此 Worker 配置？有未释放任务或维护操作时不能删除。',
      '删除 Worker',
    )
    await api.deleteWorker(String(route.params.id))
    await router.push('/settings/workers')
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') error.value = String(e)
  }
}
watch(() => route.params.id, load, { immediate: true })
</script>
