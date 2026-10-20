<template>
  <div v-if="employee" class="worker-detail" data-testid="employee-detail-page">
    <ElBreadcrumb separator="/" class="worker-detail__nav">
      <ElBreadcrumbItem><RouterLink to="/settings/workers">Agent Worker</RouterLink></ElBreadcrumbItem>
      <ElBreadcrumbItem>{{ workerTitle }}</ElBreadcrumbItem>
    </ElBreadcrumb>

    <section class="worker-hero">
      <EmployeeAvatar :employee="employee" :emoji="digitalEmployee?.avatar" size="large" />
      <div class="worker-hero__identity">
        <div class="worker-hero__eyebrow">
          <span>Agent Worker</span>
          <ElTag size="small" effect="plain">环境 #{{ environment?.id ?? '—' }}</ElTag>
          <ElTag v-if="environment" size="small" effect="plain">{{ environment.mode === 'LINUX_REMOTE' ? 'Linux 远程' : '本地' }}</ElTag>
          <ElTag v-if="health?.enabled === false" size="small" type="info" effect="plain">进程已禁用</ElTag>
          <ElTag v-if="employee.enabled === false" size="small" type="info" effect="plain">已停止派单</ElTag>
        </div>
        <h2>{{ workerTitle }}</h2>
        <p>
          <template v-if="digitalEmployee">{{ digitalEmployee.name }} · {{ digitalEmployee.domain }}</template>
          <template v-else-if="employee.digital_employee_id">数字员工 {{ employee.digital_employee_id }}</template>
          <template v-else>历史 Worker（未关联数字员工）</template>
        </p>
        <div class="worker-hero__badges">
          <ElTag size="small" :type="connectionTone(connection)">连接 {{ connectionLabel(connection) }}</ElTag>
          <ElTag size="small" :type="runtimeHealthTone(health)">OpenCode {{ runtimeHealthLabel(health) }}</ElTag>
          <ElTag size="small" :type="schedulableTone(health)">{{ schedulableLabel(health) }}</ElTag>
          <ElTag v-if="maintenanceState" size="small" :type="maintenanceStateTone(maintenanceState)">维护 {{ maintenanceStateLabel(maintenanceState) }}</ElTag>
        </div>
      </div>
      <ElSpace class="worker-hero__actions" wrap :size="8">
        <ElButton :icon="Stethoscope" :loading="quickActionLoading === 'check'" :disabled="!environment" @click="quickAction('check')">检查</ElButton>
        <ElButton type="primary" :icon="UploadCloud" :loading="quickActionLoading === 'deploy'" :disabled="!environment || maintenanceActive" @click="quickAction('deploy')">一键部署</ElButton>
        <ElButton :icon="RotateCcw" :loading="quickActionLoading === 'restart'" :disabled="!environment || maintenanceActive" @click="quickAction('restart')">重启</ElButton>
        <ElButton :icon="Settings2" @click="editOpen = true">编辑</ElButton>
        <ElButton :icon="RefreshCw" :loading="maintenance.loading.value" @click="refreshAll">刷新</ElButton>
      </ElSpace>
    </section>

    <ElAlert
      v-if="maintenance.healthError.value"
      type="error"
      show-icon
      :closable="false"
      title="健康与维护接口暂不可用，以下仅显示已保存的环境记录。"
    >
      <p>{{ maintenance.healthError.value }}</p>
      <ElButton size="small" @click="maintenance.load">重新加载</ElButton>
    </ElAlert>

    <ElTabs v-model="detailSection" class="worker-detail__tabs">
      <ElTabPane label="运行概览" name="overview">
        <section class="worker-metrics">
          <article v-for="metric in runtimeMetrics" :key="metric.label" class="worker-metric">
            <span>{{ metric.label }}</span>
            <strong>{{ metric.value }}</strong>
            <small>{{ metric.hint }}</small>
          </article>
        </section>

        <section class="worker-grid">
          <ElCard shadow="never" class="worker-card">
            <template #header>
              <div class="worker-card__title">
                <h3>数字员工</h3>
                <RouterLink class="worker-link" to="/settings/employees">查看目录 <ArrowUpRight :size="13" /></RouterLink>
              </div>
            </template>
            <div v-if="digitalEmployee" class="digital-profile">
              <span class="digital-profile__avatar">{{ digitalEmployee.avatar || '🤖' }}</span>
              <div class="digital-profile__copy">
                <strong>{{ digitalEmployee.name }}</strong>
                <ElText type="info">{{ digitalEmployee.domain }}</ElText>
                <p>{{ digitalEmployee.description || '未填写职责说明' }}</p>
              </div>
            </div>
            <div v-else class="worker-empty">
              <ElText type="info">该 Worker 尚未关联数字员工。</ElText>
              <ElButton link type="primary" @click="editOpen = true">补选数字员工</ElButton>
            </div>
          </ElCard>

          <ElCard shadow="never" class="worker-card">
            <template #header>
              <div class="worker-card__title">
                <h3>当前任务</h3>
                <RouterLink v-if="currentTask" class="worker-link" :to="`/tasks/${currentTask.id}`">查看任务 <ArrowUpRight :size="13" /></RouterLink>
              </div>
            </template>
            <div v-if="currentTask" class="current-task">
              <small>{{ currentTask.id }}</small>
              <strong>{{ currentTask.title }}</strong>
              <ElSpace :size="6" wrap>
                <ElTag size="small" :type="statusTagType(currentTask.runtime_state)">{{ currentTask.runtime_status_label }}</ElTag>
                <ElTag v-if="waitingTask" size="small" type="warning">等待人工确认</ElTag>
              </ElSpace>
              <p>{{ platformCopy(currentTask.current_progress) || '暂无进展更新' }}</p>
            </div>
            <div v-else class="worker-empty">
              <strong>当前空闲</strong>
              <ElText type="info">{{ idleHint }}</ElText>
            </div>
          </ElCard>

          <ElCard shadow="never" class="worker-card worker-card--wide">
            <template #header>
              <div class="worker-card__title">
                <h3>运行环境</h3>
                <ElText type="info">登录、工程与编码 Agent 只读摘要</ElText>
              </div>
            </template>
            <ElDescriptions v-if="environment" :column="3" border size="small">
              <ElDescriptionsItem label="服务器" :span="2">{{ serverAddressText }}</ElDescriptionsItem>
              <ElDescriptionsItem label="执行方式">{{ environment.mode === 'LINUX_REMOTE' ? 'Linux 远程' : '本地' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="工作空间" :span="2">{{ environment.workspace_root || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="SSH 凭据">{{ environment.mode === 'LINUX_REMOTE' ? (environment.has_ssh_password ? '已配置' : '未配置') : '不适用' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="代码仓库" :span="2">{{ repository.url || environment.repository_url || '未绑定仓库' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="目标分支">{{ environment.repository_branch || environment.branch || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="编程工具">OpenCode</ElDescriptionsItem>
              <ElDescriptionsItem label="编程模型">{{ environment.model_name || '使用平台默认模型' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="最近检查">{{ formatTime(environment.last_checked_at) }}</ElDescriptionsItem>
            </ElDescriptions>
          </ElCard>

          <ElCard shadow="never" class="worker-card">
            <template #header>
              <div class="worker-card__title">
                <h3>阻塞对象</h3>
                <ElText type="info">调度与维护共用</ElText>
              </div>
            </template>
            <ul v-if="blocking.length" class="blocking-list">
              <li v-for="reason in blocking" :key="reason">{{ reason }}</li>
            </ul>
            <ElText v-else type="info">当前没有阻塞项</ElText>
            <div v-if="maintenanceState" class="maintenance-summary">
              <ElTag size="small" :type="maintenanceStateTone(maintenanceState)">{{ maintenanceStateLabel(maintenanceState) }}</ElTag>
              <ElText type="info">{{ maintenanceStageLabel(health?.maintenance?.stage) }}</ElText>
              <ElButton link type="primary" @click="detailSection = 'maintenance'">查看维护记录</ElButton>
            </div>
          </ElCard>
        </section>
      </ElTabPane>

      <ElTabPane label="配置" name="config">
        <section class="worker-grid">
          <ElCard shadow="never" class="worker-card">
            <template #header><div class="worker-card__title"><h3>基本信息</h3></div></template>
            <ElDescriptions :column="2" border size="small">
              <ElDescriptionsItem label="Worker 名称" :span="2">{{ workerTitle }}</ElDescriptionsItem>
              <ElDescriptionsItem label="Worker ID">{{ employee.id }}</ElDescriptionsItem>
              <ElDescriptionsItem label="环境 ID">#{{ environment?.id ?? '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="数字员工" :span="2">{{ digitalEmployee ? `${digitalEmployee.name}（${digitalEmployee.id}）` : (employee.digital_employee_id || '未关联') }}</ElDescriptionsItem>
              <ElDescriptionsItem label="环境说明" :span="2">{{ environment?.description || '—' }}</ElDescriptionsItem>
            </ElDescriptions>
          </ElCard>

          <ElCard shadow="never" class="worker-card">
            <template #header><div class="worker-card__title"><h3>工作范围与派单</h3></div></template>
            <ElDescriptions :column="2" border size="small">
              <ElDescriptionsItem label="任务类型" :span="2">{{ taskTypesLabel }}</ElDescriptionsItem>
              <ElDescriptionsItem label="任务分类" :span="2">{{ categoryName(employee.category_ids[0] ?? null) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="平台调度">{{ employee.enabled ? '允许' : '已停止' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="自动领取">{{ employee.auto_claim ? '开启' : '关闭' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="自主执行">{{ employee.auto_execute ? '开启' : '关闭' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="运行状态"><ElTag size="small" :type="statusTagType(employee.status)">{{ employee.runtime_status_label }}</ElTag></ElDescriptionsItem>
            </ElDescriptions>
          </ElCard>

          <ElCard shadow="never" class="worker-card">
            <template #header><div class="worker-card__title"><h3>登录信息</h3><ElText type="info">Linux 远程</ElText></div></template>
            <ElDescriptions v-if="environment?.mode === 'LINUX_REMOTE'" :column="1" border size="small">
              <ElDescriptionsItem label="服务器">{{ environment.host || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="端口">{{ environment.port }}</ElDescriptionsItem>
              <ElDescriptionsItem label="用户名">{{ environment.username || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="SSH 密码">{{ environment.has_ssh_password ? '已配置（不回显）' : '未配置' }}</ElDescriptionsItem>
            </ElDescriptions>
            <ElText v-else type="info">本地 Worker 不需要 SSH 登录信息。</ElText>
          </ElCard>

          <ElCard shadow="never" class="worker-card">
            <template #header><div class="worker-card__title"><h3>工程信息</h3></div></template>
            <ElDescriptions :column="1" border size="small">
              <ElDescriptionsItem label="工作空间">{{ environment?.workspace_root || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="仓库地址">{{ repository.url || environment?.repository_url || '未绑定仓库' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="目标分支">{{ environment?.repository_branch || environment?.branch || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="仓库认证">{{ repository.authentication || '未核验' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="当前提交">{{ repository.commit || '未核验' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="工作区">{{ dirtyLabel(repository.dirty) }}</ElDescriptionsItem>
            </ElDescriptions>
          </ElCard>

          <ElCard shadow="never" class="worker-card">
            <template #header><div class="worker-card__title"><h3>编码 Agent</h3></div></template>
            <ElDescriptions :column="1" border size="small">
              <ElDescriptionsItem label="编程工具">OpenCode</ElDescriptionsItem>
              <ElDescriptionsItem label="编程模型">{{ environment?.model_name || '使用平台默认模型' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="可执行文件">{{ environment?.opencode_path || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="Server 端口">{{ environment?.opencode_server_port ?? '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="Worker 通道">{{ channelLabel(health?.connection?.channel) }}</ElDescriptionsItem>
            </ElDescriptions>
          </ElCard>
        </section>
      </ElTabPane>

      <ElTabPane label="任务" name="tasks">
        <section id="tasks" class="worker-grid">
          <ElCard shadow="never" class="worker-card worker-card--wide">
            <template #header>
              <div class="worker-card__title">
                <h3>当前工作</h3>
                <RouterLink v-if="currentTask" class="worker-link" :to="`/tasks/${currentTask.id}`">查看任务 <ArrowUpRight :size="13" /></RouterLink>
              </div>
            </template>
            <div v-if="currentTask">
              <div class="current-task">
                <small>{{ currentTask.id }}</small>
                <strong>{{ currentTask.title }}</strong>
              </div>
              <ElDescriptions :column="3" border size="small">
                <ElDescriptionsItem label="类型">{{ taskTypeLabel(currentTask.task_type) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="分类">{{ categoryName(currentTask.category_id) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="优先级">{{ priorityLabel(currentTask.priority) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="开始时间">{{ formatTime(currentTask.started_at) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="当前状态"><ElTag size="small" :type="statusTagType(currentTask.runtime_state)">{{ currentTask.runtime_status_label }}</ElTag></ElDescriptionsItem>
                <ElDescriptionsItem label="运行时长">{{ formatTaskDuration(currentTask) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="当前进展" :span="3">{{ platformCopy(currentTask.current_progress) || '暂无进展更新' }}</ElDescriptionsItem>
                <ElDescriptionsItem label="执行会话" :span="3">{{ currentTask.current_session_id ?? '执行会话尚未创建' }}</ElDescriptionsItem>
              </ElDescriptions>
            </div>
            <ElEmpty v-else :image-size="60" description="当前空闲，正在等待下一项匹配任务。" />
            <ElAlert v-if="latestError" class="worker-inline-alert" type="warning" show-icon :closable="false" :title="latestErrorCopy" />
          </ElCard>

          <ElCard shadow="never" class="worker-card worker-card--wide">
            <template #header><div class="worker-card__title"><h3>Worker 任务队列</h3><ElText type="info">当前任务优先，其后按优先级和创建时间</ElText></div></template>
            <ElTable :data="employeeQueue" size="small" @row-click="openTask">
              <ElTableColumn label="顺序" width="64" align="right"><template #default="{ $index }">{{ $index + 1 }}</template></ElTableColumn>
              <ElTableColumn label="任务" min-width="240">
                <template #default="{ row }">
                  <div class="task-cell"><strong>{{ row.title }}</strong><small>{{ row.id }}</small></div>
                </template>
              </ElTableColumn>
              <ElTableColumn label="优先级" width="88"><template #default="{ row }">{{ priorityLabel(row.priority) }}</template></ElTableColumn>
              <ElTableColumn label="状态" width="120"><template #default="{ row }"><ElTag size="small" :type="statusTagType(row.runtime_state)">{{ row.runtime_status_label }}</ElTag></template></ElTableColumn>
              <ElTableColumn label="当前进展" min-width="220" show-overflow-tooltip><template #default="{ row }">{{ platformCopy(row.current_progress) }}</template></ElTableColumn>
              <template #empty><ElText size="small" type="info">当前队列为空</ElText></template>
            </ElTable>
          </ElCard>

          <ElCard shadow="never" class="worker-card worker-card--wide">
            <template #header><div class="worker-card__title"><h3>历史任务</h3><ElText type="info">已完成、未完成和已取消任务都会保留</ElText></div></template>
            <ElTable :data="historicalTasks" size="small" @row-click="openTask">
              <ElTableColumn label="任务" min-width="240">
                <template #default="{ row }">
                  <div class="task-cell"><strong>{{ row.title }}</strong><small>{{ row.id }}</small></div>
                </template>
              </ElTableColumn>
              <ElTableColumn label="类型" width="80"><template #default="{ row }">{{ taskTypeLabel(row.task_type) }}</template></ElTableColumn>
              <ElTableColumn label="分类" width="140" show-overflow-tooltip><template #default="{ row }">{{ categoryName(row.category_id) }}</template></ElTableColumn>
              <ElTableColumn label="优先级" width="88"><template #default="{ row }">{{ priorityLabel(row.priority) }}</template></ElTableColumn>
              <ElTableColumn label="状态" width="120"><template #default="{ row }"><ElTag size="small" :type="statusTagType(row.runtime_state)">{{ row.runtime_status_label }}</ElTag></template></ElTableColumn>
              <ElTableColumn label="运行时长" width="130"><template #default="{ row }">{{ formatTaskDuration(row as Task) }}</template></ElTableColumn>
              <template #empty><ElText size="small" type="info">暂无历史任务</ElText></template>
            </ElTable>
          </ElCard>
        </section>
      </ElTabPane>

      <ElTabPane label="用量" name="usage">
        <section class="worker-metrics" aria-label="今日工作摘要">
          <article v-for="metric in metrics" :key="metric.label" class="worker-metric">
            <span>{{ metric.label }}</span>
            <strong>{{ metric.formatter() }}</strong>
            <small>{{ metric.hint }}</small>
          </article>
        </section>

        <ElAlert v-if="usageError" type="error" show-icon :closable="false" :title="usageError">
          <ElButton size="small" type="primary" @click="loadUsage">重试</ElButton>
        </ElAlert>

        <section class="worker-grid">
          <ElCard shadow="never" class="worker-card">
            <template #header><div class="worker-card__title"><h3>今日工作消耗</h3><ElText type="info">实际用量，缺失数据不估算</ElText></div></template>
            <ElDescriptions :column="2" border size="small">
              <ElDescriptionsItem label="输入 Token">{{ formatTokens(employee.today_usage.input_tokens) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="输出 Token">{{ formatTokens(employee.today_usage.output_tokens) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="缓存读取">{{ formatTokens(employee.today_usage.cache_read_tokens) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="缓存写入">{{ formatTokens(employee.today_usage.cache_write_tokens) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="Token 总计">{{ formatTokens(employee.today_usage.total_tokens) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="工具调用">{{ employee.today_usage.tool_call_count ?? '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="平均每任务 Token">{{ averageTaskTokens }}</ElDescriptionsItem>
              <ElDescriptionsItem label="平均每任务工具调用">{{ averageTaskTools }}</ElDescriptionsItem>
            </ElDescriptions>
          </ElCard>

          <ElCard shadow="never" class="worker-card">
            <template #header><div class="worker-card__title"><h3>工具调用概况</h3><ElText type="info">近 {{ days }} 天</ElText></div></template>
            <ElDescriptions :column="2" border size="small">
              <ElDescriptionsItem label="工具调用总数">{{ usage?.summary.tool_call_count ?? '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="成功次数">{{ usage?.summary.tool_success_count ?? 0 }}</ElDescriptionsItem>
              <ElDescriptionsItem label="失败次数">{{ usage?.summary.tool_failure_count ?? 0 }}</ElDescriptionsItem>
              <ElDescriptionsItem label="成功率">{{ toolSuccessRate }}</ElDescriptionsItem>
            </ElDescriptions>
          </ElCard>

          <ElCard shadow="never" class="worker-card worker-card--wide">
            <template #header>
              <div class="worker-card__title">
                <h3>Token 趋势</h3>
                <ElRadioGroup v-model="days" size="small" @change="loadUsage">
                  <ElRadioButton :value="7">近 7 天</ElRadioButton>
                  <ElRadioButton :value="30">近 30 天</ElRadioButton>
                </ElRadioGroup>
              </div>
            </template>
            <div class="worker-chart"><UsageTrendChart :data="usage?.trend ?? []" /></div>
          </ElCard>

          <ElCard shadow="never" class="worker-card">
            <template #header><div class="worker-card__title"><h3>模型调用统计</h3><ElText type="info">近 {{ days }} 天</ElText></div></template>
            <ElTable :data="usage?.models ?? []" size="small">
              <ElTableColumn label="模型" prop="model_name" min-width="170" show-overflow-tooltip />
              <ElTableColumn label="会话数" prop="session_count" width="80" align="right" />
              <ElTableColumn label="Token" width="100" align="right"><template #default="{ row }">{{ formatTokens(row.total_tokens) }}</template></ElTableColumn>
              <ElTableColumn label="LLM 调用" prop="llm_call_count" width="90" align="right" />
              <ElTableColumn label="工具调用" prop="tool_call_count" width="90" align="right" />
              <template #empty><ElEmpty :image-size="56" description="暂无模型用量数据" /></template>
            </ElTable>
          </ElCard>

          <ElCard shadow="never" class="worker-card">
            <template #header><div class="worker-card__title"><h3>工具调用 Top</h3><ElText type="info">近 {{ days }} 天</ElText></div></template>
            <ElTable :data="usage?.tools.slice(0, 8) ?? []" size="small">
              <ElTableColumn label="工具" prop="tool_name" min-width="170" show-overflow-tooltip />
              <ElTableColumn label="成功" prop="success_count" width="80" align="right" />
              <ElTableColumn label="失败" prop="failure_count" width="80" align="right" />
              <ElTableColumn label="调用次数" prop="call_count" width="95" align="right" />
              <template #empty><ElEmpty :image-size="56" description="暂无工具数据" /></template>
            </ElTable>
          </ElCard>
        </section>
      </ElTabPane>

      <ElTabPane label="维护" name="maintenance">
        <section class="worker-grid">
          <ElCard shadow="never" class="worker-card worker-card--wide">
            <template #header>
              <div class="worker-card__title">
                <h3>部署与维护</h3>
                <ElText type="info">检查、部署、更新、重启和禁用都在服务端持续执行</ElText>
              </div>
            </template>
            <WorkerMaintenancePanel :controller="maintenance" @changed="onMaintenanceChanged" />
          </ElCard>

          <ElCard shadow="never" class="worker-card worker-card--wide">
            <template #header>
              <div class="worker-card__title">
                <h3>装备配置</h3>
                <ElText type="info">配置、部署、验证分开显示；文件存在或端口监听不等于验证成功</ElText>
              </div>
            </template>
            <ElTable v-if="equipment.length" :data="equipment" size="small">
              <ElTableColumn label="装备" min-width="190">
                <template #default="{ row }">
                  <div class="equipment-cell">
                    <strong>{{ row.label }}</strong>
                    <ElText v-if="row.detail" type="info">{{ row.detail }}</ElText>
                  </div>
                </template>
              </ElTableColumn>
              <ElTableColumn label="已配置" width="90">
                <template #default="{ row }"><ElTag size="small" :type="equipmentTone(row.configured)">{{ equipmentStateLabel(row.configured) }}</ElTag></template>
              </ElTableColumn>
              <ElTableColumn label="已部署" width="90">
                <template #default="{ row }"><ElTag size="small" :type="equipmentTone(row.deployed)">{{ equipmentStateLabel(row.deployed) }}</ElTag></template>
              </ElTableColumn>
              <ElTableColumn label="已验证" width="90">
                <template #default="{ row }"><ElTag size="small" :type="equipmentTone(row.verified)">{{ equipmentStateLabel(row.verified) }}</ElTag></template>
              </ElTableColumn>
              <ElTableColumn label="目标版本" min-width="130"><template #default="{ row }">{{ row.desired_revision || '未核验' }}</template></ElTableColumn>
              <ElTableColumn label="实际版本" min-width="130"><template #default="{ row }">{{ row.actual_revision || '未核验' }}</template></ElTableColumn>
              <ElTableColumn label="差异 / 失败" min-width="220">
                <template #default="{ row }">
                  <ElText v-if="row.error" type="danger">{{ row.error }}</ElText>
                  <ElText v-else-if="equipmentDiff(row as WorkerEquipmentItem)" type="warning">{{ equipmentDiff(row as WorkerEquipmentItem) }}</ElText>
                  <ElText v-else-if="row.verified === false" type="danger">验证未通过</ElText>
                  <ElText v-else type="info">—</ElText>
                </template>
              </ElTableColumn>
            </ElTable>
            <ElAlert v-else type="info" show-icon :closable="false" title="尚未完成装备检查">
              执行“检查”后，这里会显示装备的配置、部署、验证状态及版本差异。
            </ElAlert>
          </ElCard>
        </section>
      </ElTabPane>
    </ElTabs>

    <EmployeeConfigModal
      :open="editOpen"
      :employee="employee"
      :creating="false"
      :categories="store.categories"
      :digital-employees="digitalEmployees"
      :digital-loading="digitalLoading"
      @close="editOpen = false"
      @saved="onSaved"
    />
  </div>
  <div v-else-if="!store.loading" class="worker-detail">
    <ElEmpty :image-size="80" description="未找到该 Agent Worker，可能已被删除。">
      <ElButton @click="router.push('/settings/workers')">返回 Agent Worker 列表</ElButton>
    </ElEmpty>
  </div>
</template>

<script setup lang="ts">
import { ArrowUpRight, RefreshCw, RotateCcw, Settings2, Stethoscope, UploadCloud } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  ElAlert,
  ElBreadcrumb,
  ElBreadcrumbItem,
  ElButton,
  ElCard,
  ElDescriptions,
  ElDescriptionsItem,
  ElEmpty,
  ElRadioButton,
  ElRadioGroup,
  ElSpace,
  ElTabPane,
  ElTable,
  ElTableColumn,
  ElTabs,
  ElTag,
  ElText,
} from 'element-plus'
import { api } from '@/api/client'
import { listEmployees } from '@/api'
import { submitMaintenance, type MaintenanceAction } from '@/api/maintenance'
import type { EmployeeUsage, Task } from '@/api/types'
import type { Employee as DigitalEmployee } from '@/types'
import type { WorkerEquipmentItem } from '@/api/maintenance'
import EmployeeAvatar from '@/components/employee/EmployeeAvatar.vue'
import EmployeeConfigModal from '@/components/employee/EmployeeConfigModal.vue'
import UsageTrendChart from '@/components/runtime/UsageTrendChart.vue'
import WorkerMaintenancePanel from '@/components/worker/WorkerMaintenancePanel.vue'
import { useWorkerMaintenance } from '@/composables/useWorkerMaintenance'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import { useAstraStore } from '@/stores/astra'
import {
  blockingReasons,
  connectionLabel,
  connectionTone,
  equipmentDiff,
  equipmentItems,
  equipmentStateLabel,
  maintenanceStageLabel,
  isMaintenanceActive,
  maintenanceStateLabel,
  maintenanceStateTone,
  runtimeHealthLabel,
  runtimeHealthTone,
  schedulableLabel,
  schedulableTone,
  serverAddress,
  workerRepository,
} from '@/features/workers/presentation'
import {
  employeeDisplayName,
  formatDurationSeconds,
  formatTaskDuration,
  formatTime,
  formatTokens,
  platformCopy,
  priorityLabel,
  statusTagType,
  taskTypeLabel,
} from '@/utils/format'

type DetailSection = 'overview' | 'config' | 'tasks' | 'usage' | 'maintenance'
const SECTIONS: DetailSection[] = ['overview', 'config', 'tasks', 'usage', 'maintenance']

const route = useRoute()
const router = useRouter()
const store = useAstraStore()
const id = computed(() => String(route.params.id))
const usage = ref<EmployeeUsage | null>(null)
const usageError = ref('')
const days = ref(7)
const editOpen = ref(false)
const digitalEmployees = ref<DigitalEmployee[]>([])
const digitalLoading = ref(false)
const quickActionLoading = ref<MaintenanceAction | ''>('')

function sectionFromHash(hash: string): DetailSection {
  const name = hash.replace('#', '') as DetailSection
  return SECTIONS.includes(name) ? name : 'overview'
}
const detailSection = ref<DetailSection>(sectionFromHash(route.hash))
watch(() => route.hash, (hash) => { detailSection.value = sectionFromHash(hash) })
watch(detailSection, (value) => {
  const next = `#${value}`
  if (route.hash === next) return
  void router.replace({ hash: next })
})

const ACTIVE_STATUSES = ['PENDING', 'RUNNING', 'WAITING', 'FAILED']
const PRIORITY_WEIGHT: Record<string, number> = { VERY_HIGH: 4, HIGH: 3, MEDIUM: 2, LOW: 1 }

const employee = computed(() => store.employees.find((item) => item.id === id.value))
const employeeTasks = computed(() => store.tasks.filter((task) => task.employee_id === id.value))
const environment = computed(() => employee.value?.execution_environment ?? null)
const maintenance = useWorkerMaintenance(computed(() => environment.value?.id ?? null))
const health = maintenance.health
const maintenanceState = computed(() => health.value?.maintenance?.state ?? null)
const connection = computed(() => health.value?.connection.state ?? 'unknown')
const blocking = computed(() => blockingReasons(health.value))
const maintenanceActive = computed(() => isMaintenanceActive(health.value?.maintenance?.state) || isMaintenanceActive(maintenance.operation.value?.state))
const idleHint = computed(() => {
  if (maintenanceActive.value) return '当前无执行任务，但有进行中的维护操作。'
  if (health.value?.maintenance?.state === 'INTERRUPTED') return '维护操作待核对，请先检查或恢复。'
  if (!health.value) return '尚未获取 Worker 运行状态；部署、更新和重启仍可提交，由后端最终检查。'
  return 'Worker 空闲，可执行维护操作。'
})
const equipment = computed(() => equipmentItems(health.value))
const repository = computed(() => workerRepository(environment.value, health.value))
const currentTask = computed(() => employeeTasks.value.find((task) => ACTIVE_STATUSES.includes(task.status)))
const waitingTask = computed(() => employeeTasks.value.find((task) => task.status === 'WAITING'))
const employeeQueue = computed(() => employeeTasks.value
  .filter((task) => ACTIVE_STATUSES.includes(task.status))
  .sort((a, b) => (PRIORITY_WEIGHT[b.priority] ?? 0) - (PRIORITY_WEIGHT[a.priority] ?? 0) || a.created_at.localeCompare(b.created_at)))
const historicalTasks = computed(() => employeeTasks.value.filter((task) => ['SUCCEEDED', 'FAILED', 'CANCELED'].includes(task.status)))
const latestError = computed(() => employeeTasks.value.filter((task) => task.execution_error).sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0])
const latestErrorCopy = computed(() => latestError.value
  ? `最近异常：${platformCopy(String(latestError.value.execution_error?.reason ?? latestError.value.current_progress))} · ${formatTime(latestError.value.updated_at)}`
  : '')
const workerTitle = computed(() => environment.value?.name || employeeDisplayName(employee.value?.name))
const digitalEmployee = computed(() => digitalEmployees.value.find((item) => item.id === employee.value?.digital_employee_id) ?? null)
const taskTypesLabel = computed(() => (employee.value?.task_types ?? []).map((item) => taskTypeLabel(item)).join('、') || '—')
const serverAddressText = computed(() => environment.value ? serverAddress(environment.value) : '—')

const runtimeMetrics = computed(() => [
  { label: '连接状态', value: connectionLabel(connection.value), hint: health.value?.connection.last_heartbeat_at ? `心跳 ${formatTime(health.value.connection.last_heartbeat_at)}` : '尚未获取心跳' },
  { label: 'OpenCode', value: runtimeHealthLabel(health.value), hint: health.value?.runtime_health.last_checked_at ? `检查于 ${formatTime(health.value.runtime_health.last_checked_at)}` : '尚未检查' },
  { label: '可调度性', value: schedulableLabel(health.value), hint: health.value?.schedulable ? '满足调度条件' : '当前不会领取新任务' },
  { label: '当前任务', value: currentTask.value ? currentTask.value.runtime_status_label : '空闲', hint: currentTask.value?.title ?? '等待匹配任务' },
])

interface DetailMetric {
  label: string
  value: number
  hint: string
  formatter: () => string
}
const metrics = computed<DetailMetric[]>(() => {
  const today = employee.value?.today_usage
  return [
    { label: '今日 Token', value: today?.total_tokens ?? 0, hint: 'Agent 实际用量', formatter: () => formatTokens(today?.total_tokens) },
    { label: '工具调用', value: today?.tool_call_count ?? 0, hint: '成功与失败均保留', formatter: () => today?.tool_call_count == null ? '—' : `${today.tool_call_count} 次` },
    { label: '今日完成', value: employee.value?.today_completed ?? 0, hint: 'Worker 已提交结果', formatter: () => employee.value?.today_completed == null ? '—' : `${employee.value.today_completed} 项` },
    { label: '处理中', value: statusCount('RUNNING'), hint: '当前工作', formatter: () => `${statusCount('RUNNING')} 项` },
    { label: '等待人工', value: statusCount('WAITING'), hint: '需要补充信息', formatter: () => `${statusCount('WAITING')} 项` },
    { label: '今日工作时长', value: today?.duration_seconds ?? 0, hint: '按任务开始到结束计算', formatter: () => formatDurationSeconds(today?.duration_seconds) },
  ]
})
const averageTaskTokens = computed(() => {
  const today = employee.value?.today_usage
  return today?.total_tokens != null && today.task_count ? formatTokens(Math.round(today.total_tokens / today.task_count)) : '—'
})
const averageTaskTools = computed(() => {
  const today = employee.value?.today_usage
  return today?.tool_call_count != null && today.task_count ? (today.tool_call_count / today.task_count).toFixed(1) : '—'
})
const toolSuccessRate = computed(() => {
  const summary = usage.value?.summary
  if (!summary?.tool_call_count) return '—'
  return `${((summary.tool_success_count / summary.tool_call_count) * 100).toFixed(1)}%`
})

function categoryName(categoryId: string | null) {
  return store.categories.find((item) => item.id === categoryId)?.name ?? '—'
}
function statusCount(status: string) {
  return employeeTasks.value.filter((task) => task.status === status).length
}
function openTask(task: Task) {
  router.push(`/tasks/${task.id}`)
}
function equipmentTone(value: boolean | null) {
  if (value === true) return 'success'
  if (value === false) return 'danger'
  return 'info'
}
function dirtyLabel(value: boolean | null) {
  if (value === true) return '有未提交修改'
  if (value === false) return '工作区干净'
  return '未核验'
}
function channelLabel(value: string | null | undefined) {
  if (value === 'worker') return 'Worker 通道'
  if (value === 'ssh') return 'SSH'
  if (value === 'local') return '本机'
  return value || '—'
}

async function loadUsage() {
  usageError.value = ''
  try { usage.value = await api.employeeUsage(id.value, days.value) }
  catch (error) { usageError.value = error instanceof Error ? error.message : '用量数据加载失败' }
}
async function loadDigitalEmployees() {
  digitalLoading.value = true
  try { digitalEmployees.value = await listEmployees() }
  catch { digitalEmployees.value = [] }
  finally { digitalLoading.value = false }
}
async function refreshAll() {
  await Promise.all([store.refresh(), loadDigitalEmployees(), maintenance.load()])
}
async function quickAction(action: MaintenanceAction) {
  if (!environment.value) return
  if (action === 'deploy' || action === 'restart') {
    const confirmed = await confirmDialog({
      title: action === 'deploy' ? '一键部署这个 Worker？' : '重启这个 Worker 的远程服务？',
      message: action === 'deploy'
        ? `将部署 ${workerTitle.value} 的执行环境；有任务占用或维护进行中时会被拒绝。`
        : `将重启 ${workerTitle.value} 的 OpenCode 服务；正在执行或等待确认的任务会被保护。`,
      confirmLabel: action === 'deploy' ? '开始部署' : '确认重启',
      danger: action === 'restart',
    })
    if (!confirmed) return
  }
  quickActionLoading.value = action
  try {
    const created = await submitMaintenance(environment.value.id, action)
    notify(`${action === 'check' ? '检查' : action === 'deploy' ? '一键部署' : '重启'}已提交：${created.id}`)
    await maintenance.load()
  } catch (error) {
    notify(error instanceof Error ? error.message : '维护操作提交失败', 'error')
  } finally {
    quickActionLoading.value = ''
  }
}
async function load() {
  await Promise.all([store.refresh(), loadDigitalEmployees(), loadUsage()])
}
onMounted(load)
async function onSaved(id: string, deployRequested: boolean) {
  await store.refresh()
  await loadDigitalEmployees()
  await maintenance.load()
  if (!deployRequested) return
  const saved = store.employees.find((item) => item.id === id)
  const environmentId = saved?.execution_environment?.id
  if (!environmentId) {
    notify('执行环境尚未创建，无法提交部署', 'error')
    return
  }
  try {
    const created = await submitMaintenance(environmentId, 'deploy')
    notify(`一键部署已提交：${created.id}`)
    await maintenance.load()
  } catch (error) {
    notify(error instanceof Error ? error.message : '一键部署提交失败', 'error')
  }
}
async function onMaintenanceChanged() {
  await store.refresh()
}
</script>

<style scoped>
.worker-detail { display: grid; gap: 14px; height: 100%; min-height: 480px; padding: 20px 24px 24px; overflow: auto; }
.worker-detail__nav { font-size: var(--astra-font-label); }
.worker-detail__nav :deep(.el-breadcrumb__inner a) { color: var(--astra-text-muted); }
.worker-detail__nav :deep(.el-breadcrumb__inner.is-link):hover a { color: var(--astra-primary); }
.worker-hero { display: flex; align-items: center; gap: 16px; padding: 16px; border: 1px solid var(--astra-border); border-radius: 12px; background: var(--astra-bg-panel); box-shadow: var(--astra-shadow-panel); }
.worker-hero__identity { display: grid; gap: 3px; flex: 1; min-width: 0; }
.worker-hero__eyebrow { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.worker-hero__eyebrow > span:first-child { color: var(--astra-primary); font-weight: var(--astra-weight-bold); letter-spacing: .04em; }
.worker-hero__identity h2 { margin: 0; color: var(--astra-text-primary); font-size: var(--astra-font-subtitle); line-height: 1.25; }
.worker-hero__identity p { margin: 0; color: var(--astra-text-secondary); font-size: var(--astra-font-label); }
.worker-hero__badges { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.worker-hero__actions { flex: none; }
.worker-detail :deep(.el-card) { --el-card-border-color: var(--astra-border); --el-card-border-radius: 11px; }
.worker-detail :deep(.el-card__header) { padding: 11px 15px; }
.worker-detail :deep(.el-card__body) { padding: 14px 15px; }
.worker-detail__tabs :deep(.el-tab-pane) { display: grid; gap: 14px; }
.worker-card__title { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.worker-card__title h3 { margin: 0; color: var(--astra-text-primary); font-size: var(--astra-font-label); }
.worker-link, .worker-link:hover { display: inline-flex; align-items: center; gap: 4px; color: var(--astra-primary); font-size: var(--astra-font-label); text-decoration: none; }
.worker-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.worker-metric { display: grid; gap: 2px; padding: 12px 14px; border: 1px solid var(--astra-border); border-radius: 11px; background: var(--astra-bg-panel); }
.worker-metric span { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.worker-metric strong { overflow: hidden; color: var(--astra-text-primary); font-size: var(--astra-font-emphasis); text-overflow: ellipsis; white-space: nowrap; }
.worker-metric small { overflow: hidden; color: var(--astra-text-muted); font-size: var(--astra-font-helper); text-overflow: ellipsis; white-space: nowrap; }
.worker-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; align-items: start; }
.worker-card--wide { grid-column: 1 / -1; }
.worker-detail :deep(.el-descriptions__cell) { min-width: 0; }
.worker-detail :deep(.el-descriptions__label) { white-space: nowrap; }
.worker-detail :deep(.el-descriptions__content) { overflow-wrap: anywhere; word-break: break-word; white-space: normal; }
.digital-profile { display: flex; align-items: flex-start; gap: 12px; }
.digital-profile__avatar { display: inline-flex; align-items: center; justify-content: center; width: 52px; height: 52px; flex: none; border-radius: 15px; background: var(--astra-bg-subtle); font-size: 30px; }
.digital-profile__copy { display: grid; gap: 2px; min-width: 0; }
.digital-profile__copy strong { color: var(--astra-text-primary); font-size: var(--astra-font-emphasis); }
.digital-profile__copy p { margin: 2px 0 0; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); line-height: 1.7; }
.worker-empty { display: grid; gap: 6px; justify-items: start; padding: 8px 0; color: var(--astra-text-secondary); }
.worker-empty strong { color: var(--astra-text-primary); }
.current-task { display: grid; gap: 5px; }
.current-task small { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.current-task strong { color: var(--astra-text-primary); font-size: var(--astra-font-label); }
.current-task p { margin: 2px 0 0; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); line-height: 1.7; }
.blocking-list { margin: 0 0 8px; padding-left: 18px; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); line-height: 1.7; }
.maintenance-summary { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--astra-border); }
.worker-inline-alert { margin-top: 12px; }
.worker-chart { height: 300px; }
.worker-chart > * { height: 100%; }
.worker-grid :deep(.el-table__row) { cursor: pointer; }
.task-cell { display: grid; min-width: 0; }
.task-cell strong, .task-cell small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.task-cell strong { color: var(--astra-text-primary); font-size: var(--astra-font-label); }
.task-cell small { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.equipment-cell { display: grid; gap: 2px; min-width: 0; }
.equipment-cell strong { font-size: var(--astra-font-label); }
@media (max-width: 1100px) { .worker-grid { grid-template-columns: 1fr; } .worker-card--wide { grid-column: auto; } .worker-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 720px) { .worker-hero { flex-wrap: wrap; } .worker-hero__actions { width: 100%; } .worker-detail { padding: 14px; } }
</style>
