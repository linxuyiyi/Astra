<template>
  <el-form label-position="top" class="worker-form" :model="form">
    <div class="wf-cap">基础与调度</div>
    <div class="wf-pair">
      <el-form-item label="Worker ID" required>
        <el-input v-model="form.id" :disabled="Boolean(worker)" placeholder="例如：worker-prod-01" />
      </el-form-item>
      <el-form-item label="名称" required>
        <el-input v-model="form.name" placeholder="例如：研发执行机 01" />
      </el-form-item>
    </div>

    <el-form-item label="描述">
      <el-input v-model="form.description" type="textarea" :rows="2" placeholder="可选，说明该 Worker 负责的仓库或职责" />
    </el-form-item>

    <div class="wf-pair">
      <el-form-item label="支持任务类型">
        <el-checkbox-group v-model="form.task_types">
          <el-checkbox value="ISSUE">问题</el-checkbox>
          <el-checkbox value="REQUIREMENT">需求</el-checkbox>
        </el-checkbox-group>
      </el-form-item>
      <el-form-item label="负责分类">
        <el-select v-model="form.category_ids" multiple collapse-tags collapse-tags-tooltip placeholder="选择任务分类" style="width: 100%">
          <el-option v-for="c in categories" :key="c.id" :value="c.id" :label="c.name" />
        </el-select>
      </el-form-item>
    </div>

    <div class="wf-cap">运行环境与连接</div>
    <el-alert
      v-if="executionLocked"
      class="wf-lock"
      title="当前存在未释放任务、维护中操作或未完成复盘，执行环境参数暂不可修改。"
      type="warning"
      show-icon
      :closable="false"
    />

    <div class="wf-pair">
      <el-form-item label="运行方式">
        <el-select v-model="form.mode" :disabled="executionLocked" style="width: 100%">
          <el-option value="LINUX_REMOTE" label="Linux 远程 Worker" />
          <el-option value="LOCAL" label="本机开发调试" />
        </el-select>
      </el-form-item>
      <el-form-item label="模型路由">
        <el-select
          v-model="form.model_name"
          :disabled="executionLocked"
          filterable
          allow-create
          default-first-option
          clearable
          placeholder="选择或输入网关模型别名"
          style="width: 100%"
        >
          <el-option v-for="route in routeModels" :key="route" :value="route" :label="route" />
        </el-select>
      </el-form-item>
    </div>

    <template v-if="form.mode === 'LINUX_REMOTE'">
      <div class="wf-pair">
        <el-form-item label="SSH 主机">
          <el-input v-model="form.host" :disabled="executionLocked" placeholder="例如：10.0.1.25" />
        </el-form-item>
        <el-form-item label="SSH 端口">
          <el-input-number v-model="form.port" :disabled="executionLocked" :min="1" :max="65535" style="width: 100%" />
        </el-form-item>
      </div>
      <div class="wf-pair">
        <el-form-item label="SSH 用户名">
          <el-input v-model="form.username" :disabled="executionLocked" placeholder="例如：ubuntu" />
        </el-form-item>
        <el-form-item label="SSH 密码">
          <el-input
            v-model="password"
            :disabled="executionLocked"
            type="password"
            show-password
            :placeholder="worker?.has_ssh_password ? '已保存，留空保持不变' : '请输入 SSH 密码'"
          />
        </el-form-item>
      </div>
      <div class="wf-pair">
        <el-form-item label="OpenCode 端口（可选）">
          <el-input-number v-model="form.opencode_port" :disabled="executionLocked" :min="1" :max="65535" placeholder="部署时自动分配" style="width: 100%" />
        </el-form-item>
        <el-form-item label="Workspace 根目录">
          <el-input v-model="form.workspace_root" :disabled="executionLocked" />
        </el-form-item>
      </div>
    </template>
    <el-form-item v-else label="Workspace 根目录">
      <el-input v-model="form.workspace_root" :disabled="executionLocked" />
    </el-form-item>

    <div class="wf-cap">代码仓库与扩展</div>
    <el-form-item label="Git 仓库地址">
      <el-input v-model="form.repository_url" :disabled="executionLocked" placeholder="https://git.example.com/org/repo.git" />
    </el-form-item>
    <div class="wf-pair">
      <el-form-item label="默认分支">
        <el-input v-model="form.branch" :disabled="executionLocked" placeholder="main" />
      </el-form-item>
      <el-form-item label="Git Token">
        <el-input
          v-model="gitToken"
          :disabled="executionLocked"
          type="password"
          show-password
          :placeholder="worker?.has_git_token ? '已保存，留空保持不变' : '可选'"
        />
      </el-form-item>
    </div>

    <el-form-item label="绑定技能">
      <el-select
        v-model="selectedSkills"
        :disabled="executionLocked"
        multiple
        filterable
        allow-create
        default-first-option
        clearable
        placeholder="选择平台技能或输入名称后回车"
        style="width: 100%"
      >
        <el-option v-for="name in skillOptions" :key="name" :value="name" :label="name" />
      </el-select>
    </el-form-item>

    <el-form-item label="MCP 配置（JSON 数组）">
      <el-input
        v-model="mcps"
        :disabled="executionLocked"
        class="wf-mono"
        type="textarea"
        :rows="4"
        placeholder='[{"name":"example","url":"http://host/mcp","config":{}}]'
      />
    </el-form-item>

    <el-alert v-if="error" class="wf-error" :title="error" type="error" show-icon :closable="false" />

    <div class="wf-footer">
      <el-switch v-model="form.enabled" active-text="启用任务调度" />
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </div>
  </el-form>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api } from '@/api/client'
import { listSkillResources } from '@/api'
import { gatewayApi } from '@/api/gateway'
import type { Worker, TaskCategory, TaskType } from '@/api/types'

const props = defineProps<{ worker?: Worker; executionLocked?: boolean }>()
const emit = defineEmits<{ saved: [worker: Worker] }>()

const worker = props.worker
const form = reactive({
  id: worker?.id || '',
  name: worker?.name || '',
  description: worker?.description || '',
  enabled: worker?.enabled ?? true,
  task_types: [...(worker?.task_types || ['ISSUE', 'REQUIREMENT'])] as TaskType[],
  category_ids: [...(worker?.category_ids || [])],
  runtime_engine: 'opencode',
  mode: worker?.mode || 'LINUX_REMOTE',
  host: worker?.host || '',
  port: worker?.port || 22,
  opencode_port: worker?.opencode_port ?? undefined,
  username: worker?.username || '',
  model_name: worker?.model_name || '',
  workspace_root: worker?.workspace_root || '/opt/nexus/workspace',
  repository_url: worker?.repository_url || '',
  branch: worker ? worker.branch || '' : 'main',
})

const password = ref('')
const gitToken = ref('')
const selectedSkills = ref<string[]>([...(worker?.skill_refs || [])])
const mcps = ref(JSON.stringify(worker?.mcp_refs || [], null, 2))
const categories = ref<TaskCategory[]>([])
const routeModels = ref<string[]>([])
const skillOptions = ref<string[]>([])
const saving = ref(false)
const error = ref('')

onMounted(async () => {
  const [cats, routesRes, skillsRes] = await Promise.allSettled([
    api.categories(),
    gatewayApi.routes(),
    listSkillResources(),
  ])
  if (cats.status === 'fulfilled') categories.value = cats.value
  else error.value = String(cats.reason)
  if (routesRes.status === 'fulfilled') {
    routeModels.value = routesRes.value.filter((r) => r.enabled && r.model_type === 'CHAT').map((r) => r.model)
  }
  if (skillsRes.status === 'fulfilled') {
    skillOptions.value = [...new Set([...skillsRes.value.items.map((s) => s.runtime_name || s.name), ...selectedSkills.value])]
  }
})

async function save() {
  saving.value = true
  error.value = ''
  try {
    const mcpRefs: unknown = JSON.parse(mcps.value || '[]')
    if (!Array.isArray(mcpRefs)) throw Error('MCP 配置必须是 JSON 数组')
    const value = await api.saveWorker(
      {
        ...form,
        ssh_password: password.value || null,
        git_token: gitToken.value || null,
        skill_refs: selectedSkills.value.map((x) => x.trim()).filter(Boolean),
        mcp_refs: mcpRefs,
      },
      worker?.id,
    )
    emit('saved', value)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.worker-form { display: grid; gap: 2px; }
.wf-cap {
  margin: 6px 0 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--astra-border-subtle);
  color: var(--astra-text-muted);
  font-size: 12px;
  font-weight: 600;
}
.wf-cap:first-child { margin-top: 0; }
.wf-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.wf-lock { margin-bottom: 12px; }
.wf-mono :deep(textarea) { font-family: var(--astra-font-mono); font-size: 12px; line-height: 1.55; }
.wf-error { margin-bottom: 12px; }
.wf-footer {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin-top: 8px; padding-top: 14px;
  border-top: 1px solid var(--astra-border-subtle);
}

@media (max-width: 600px) {
  .wf-pair { grid-template-columns: 1fr; }
}
</style>
