<template>
  <el-form label-width="130px" :model="form">
    <el-form-item label="Worker ID"
      ><el-input v-model="form.id" :disabled="Boolean(worker)"
    /></el-form-item>
    <el-form-item label="名称"><el-input v-model="form.name" /></el-form-item>
    <el-form-item label="描述"
      ><el-input v-model="form.description" type="textarea"
    /></el-form-item>
    <el-form-item label="启用调度"
      ><el-switch v-model="form.enabled"
    /></el-form-item>
    <el-form-item label="任务类型"
      ><el-checkbox-group v-model="form.task_types"
        ><el-checkbox value="ISSUE">问题</el-checkbox
        ><el-checkbox value="REQUIREMENT">需求</el-checkbox></el-checkbox-group
      ></el-form-item
    >
    <el-form-item label="分类"
      ><el-select v-model="form.category_ids" multiple
        ><el-option
          v-for="c in categories"
          :key="c.id"
          :value="c.id"
          :label="c.name" /></el-select
    ></el-form-item>
    <el-form-item label="运行方式"
      ><el-select v-model="form.mode"
        ><el-option value="LINUX_REMOTE" label="Linux 远程 Worker" /><el-option
          value="LOCAL"
          label="本机开发调试" /></el-select
    ></el-form-item>
    <template v-if="form.mode === 'LINUX_REMOTE'"
      ><el-form-item label="Host"><el-input v-model="form.host" /></el-form-item
      ><el-form-item label="SSH 端口"
        ><el-input-number
          v-model="form.port"
          :min="1"
          :max="65535" /></el-form-item
      ><el-form-item label="用户名"
        ><el-input v-model="form.username" /></el-form-item
      ><el-form-item label="SSH 密码"
        ><el-input
          v-model="password"
          type="password"
          show-password
          :placeholder="
            worker?.has_ssh_password ? '已保存，留空保持' : '请输入 SSH 密码'
          " /></el-form-item
    ></template>
    <el-form-item label="模型路由"
      ><el-input
        v-model="form.model_name"
        placeholder="已启用的 Nexus 网关模型别名"
    /></el-form-item>
    <el-form-item label="Workspace 根目录"
      ><el-input v-model="form.workspace_root"
    /></el-form-item>
    <el-form-item label="Git 仓库"
      ><el-input v-model="form.repository_url"
    /></el-form-item>
    <el-form-item label="Git Token"
      ><el-input
        v-model="gitToken"
        type="password"
        show-password
        :placeholder="worker?.has_git_token ? '已保存，留空保持' : '可选'"
    /></el-form-item>
    <el-form-item label="分支"><el-input v-model="form.branch" /></el-form-item>
    <el-form-item label="Skill 名称"
      ><el-input v-model="skills" placeholder="以逗号分隔平台 Skill 名称"
    /></el-form-item>
    <el-form-item label="MCP 配置"
      ><el-input
        v-model="mcps"
        type="textarea"
        :rows="4"
        placeholder='[{"name":"example","url":"http://host/mcp","config":{}}]'
    /></el-form-item>
    <el-alert v-if="error" :title="error" type="error" />
    <el-form-item
      ><el-button type="primary" :loading="saving" @click="save"
        >保存</el-button
      ></el-form-item
    >
  </el-form>
</template>
<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api } from '@/api/client'
import type { Worker, TaskCategory, TaskType } from '@/api/types'
const props = defineProps<{ worker?: Worker }>(),
  emit = defineEmits<{ saved: [worker: Worker] }>()
const worker = props.worker
const form = reactive({
  id: worker?.id || '',
  name: worker?.name || '',
  description: worker?.description || '',
  enabled: worker?.enabled ?? true,
  task_types: [
    ...(worker?.task_types || ['ISSUE', 'REQUIREMENT']),
  ] as TaskType[],
  category_ids: [...(worker?.category_ids || [])],
  runtime_engine: 'opencode',
  mode: worker?.mode || 'LINUX_REMOTE',
  host: worker?.host || '',
  port: worker?.port || 22,
  username: worker?.username || '',
  model_name: worker?.model_name || '',
  workspace_root: worker?.workspace_root || '/opt/nexus/workspace',
  repository_url: worker?.repository_url || '',
  branch: worker ? worker.branch || '' : 'main',
})
const password = ref(''),
  gitToken = ref(''),
  skills = ref(worker?.skill_refs.join(', ') || ''),
  mcps = ref(JSON.stringify(worker?.mcp_refs || [], null, 2)),
  categories = ref<TaskCategory[]>([]),
  saving = ref(false),
  error = ref('')
onMounted(async () => {
  try {
    categories.value = await api.categories()
  } catch (e) {
    error.value = String(e)
  }
})
async function save() {
  saving.value = true
  try {
    const mcpRefs: unknown = JSON.parse(mcps.value)
    if (!Array.isArray(mcpRefs)) throw Error('MCP 配置必须是数组')
    const value = await api.saveWorker(
      {
        ...form,
        ssh_password: password.value || null,
        git_token: gitToken.value || null,
        skill_refs: skills.value
          .split(',')
          .map((x) => x.trim())
          .filter(Boolean),
        mcp_refs: mcpRefs,
      },
      worker?.id,
    )
    emit('saved', value)
  } catch (e) {
    error.value = String(e)
  } finally {
    saving.value = false
  }
}
</script>
