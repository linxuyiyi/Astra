<!-- 依据 AgentScope v2.0.10 官方 schedule 页的表单、列表与执行详情语义适配。
     来源和有意差异见 docs/architecture/employee-schedules.md。 -->
<template>
  <ElCard class="admin-card" data-testid="schedules-page">
    <template #header>
      <div class="admin-card__header">
        <div class="schedule-heading-lead">
          <h2 class="admin-card__title">日程安排</h2>
          <ElText type="info">我的安排 · {{ currentUid() }}</ElText>
        </div>
        <div class="admin-card__actions">
          <ElButton :icon="RefreshCw" :loading="loading" data-testid="schedules-refresh" @click="reload">刷新</ElButton>
          <ElButton type="primary" :icon="Plus" data-testid="schedules-create" @click="openCreate">新建日程</ElButton>
        </div>
      </div>
    </template>
    <div class="admin-page">
      <div class="admin-page__toolbar" role="search" aria-label="日程筛选">
        <div class="admin-page__filters">
          <ElSelect v-model="employeeFilter" class="admin-page__select" clearable placeholder="员工：全部" aria-label="日程员工" @change="applySearch">
            <ElOption v-for="employee in employeeOptions" :key="employee.id" :label="employee.name" :value="employee.id" />
          </ElSelect>
          <ElSelect v-model="statusFilter" class="admin-page__select" clearable placeholder="状态：全部" aria-label="日程状态" @change="applySearch">
            <ElOption label="已启用" value="enabled" /><ElOption label="已暂停" value="paused" />
          </ElSelect>
        </div>
        <div class="admin-page__search">
          <ElInput v-model="query" class="admin-page__keyword" placeholder="搜索日程名称 / 安排内容" aria-label="搜索日程" clearable :prefix-icon="Search" @keydown.enter.prevent="applySearch" />
          <ElButton type="primary" :icon="Search" @click="applySearch">搜索</ElButton>
          <ElButton :icon="RotateCcw" @click="resetFilters">重置</ElButton>
        </div>
      </div>
      <ElTable v-loading="loading" :data="rows" row-key="id" element-loading-text="正在加载日程列表" data-testid="schedules-table">
        <ElTableColumn label="日程" min-width="220">
          <template #default="{ row }">
            <ElButton class="schedule-title" link type="primary" :title="row.data.name" @click="openHistory(row as EmployeeSchedule)">{{ row.data.name }}</ElButton>
            <div class="schedule-summary" :title="row.data.description">{{ row.data.description }}</div>
          </template>
        </ElTableColumn>
        <ElTableColumn prop="employee_name" label="数字员工" min-width="140" show-overflow-tooltip />
        <ElTableColumn label="执行时间" min-width="180">
          <template #default="{ row }"><div>{{ scheduleTime(row.data.cron_expression) }}</div><ElText type="info">{{ row.data.timezone }}</ElText></template>
        </ElTableColumn>
        <ElTableColumn label="上下文" width="120" align="center">
          <template #default="{ row }">{{ row.data.stateful ? '连续上下文' : '每次独立' }}</template>
        </ElTableColumn>
        <ElTableColumn label="状态" width="100" align="center">
          <template #default="{ row }"><SemanticTag :tone="row.data.enabled ? 'success' : 'neutral'" :label="row.data.enabled ? '已启用' : '已暂停'" /></template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="168" align="center">
          <template #default="{ row }"><div class="admin-actions">
            <RowMenu primary="执行记录" :aria-label="row.data.name" :disabled="busy === row.id" :items="rowActions(row as EmployeeSchedule)" @primary="openHistory(row as EmployeeSchedule)" @command="command => rowCommand(row as EmployeeSchedule, command)" />
          </div></template>
        </ElTableColumn>
        <template #empty>
          <ElEmpty v-if="error" :image-size="72" description="日程数据加载失败">
            <ElText type="danger">{{ error }}</ElText><ElButton :icon="RefreshCw" @click="reload">重新加载</ElButton>
          </ElEmpty>
          <ElEmpty v-else-if="!loading" :image-size="72" :description="hasFilters ? '没有找到符合条件的日程' : '暂无日程'">
            <ElButton v-if="hasFilters" @click="resetFilters">清除筛选</ElButton>
            <ElButton v-else type="primary" :icon="Plus" @click="openCreate">新建日程</ElButton>
          </ElEmpty>
        </template>
      </ElTable>
      <ElAlert v-if="error && rows.length" :title="error" type="error" :closable="false" show-icon />
      <div class="admin-page__footer">
        <ElPagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[10, 20, 50]" :total="total" layout="total, sizes, prev, pager, next" aria-label="日程列表分页" data-testid="schedules-pagination" @current-change="reload" @size-change="changePageSize" />
      </div>
    </div>

    <ElDialog v-model="formOpen" :title="editing ? '编辑日程' : '新建日程'" width="min(560px, 95vw)" :close-on-click-modal="false">
      <ElAlert v-if="formError" :title="formError" type="error" :closable="false" show-icon />
      <ElForm label-position="top" @submit.prevent="save">
        <ElFormItem label="日程名称" required><ElInput v-model="form.name" maxlength="120" placeholder="例如：工作日晨间简报" /></ElFormItem>
        <ElFormItem label="数字员工" required>
          <ElSelect v-model="employeeId" :disabled="!!editing" placeholder="选择数字员工" style="width: 100%">
            <ElOption v-for="employee in choices" :key="employee.id" :value="employee.id" :label="employee.name" :disabled="!employee.enabled || !employee.agentscope_agent_id" />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="安排内容" required><ElInput v-model="form.description" type="textarea" :rows="4" maxlength="16000" placeholder="告诉员工到时间后需要做什么，以及期望产出" /></ElFormItem>
        <div class="schedule-form-row">
          <ElFormItem label="重复频率"><ElSelect v-model="frequency" aria-label="重复频率">
            <ElOption v-for="option in frequencies" :key="option.value" v-bind="option" />
          </ElSelect></ElFormItem>
          <ElFormItem v-if="frequency !== 'custom'" label="执行时间"><ElInput v-model="time" type="time" aria-label="执行时间" /></ElFormItem>
        </div>
        <ElFormItem v-if="frequency === 'weekly'" label="星期"><ElSelect v-model="weekday"><ElOption v-for="(day, index) in weekdays" :key="day" :label="day" :value="index" /></ElSelect></ElFormItem>
        <ElFormItem v-if="frequency === 'monthly'" label="每月日期"><ElInputNumber v-model="monthDay" :min="1" :max="31" /><small>没有该日期的月份不执行。</small></ElFormItem>
        <ElFormItem v-if="frequency === 'custom'" label="Cron 表达式（分 时 日 月 周）"><ElInput v-model="form.cron_expression" placeholder="0 9 * * 1-5" /></ElFormItem>
        <ElFormItem label="时区"><ElSelect v-model="form.timezone" filterable allow-create default-first-option style="width: 100%"><ElOption v-for="zone in timezones" :key="zone" :value="zone" :label="zone" /></ElSelect></ElFormItem>
        <ElFormItem label="上下文"><ElSwitch v-model="form.stateful" active-text="每次沿用本日程上下文" /><small>关闭时每次新建独立会话；不同人的日程互相隔离。</small></ElFormItem>
        <ElFormItem label="启用"><ElSwitch v-model="form.enabled" /></ElFormItem>
      </ElForm>
      <p class="schedule-note">日程只对你可见，其他人可独立安排同一员工。使用员工已绑定的模型路由；路由变化后需重新创建日程。启停可能需要约 60 秒，禁用员工前请先暂停日程。</p>
      <template #footer><ElButton @click="formOpen = false">取消</ElButton><ElButton type="primary" :loading="saving" @click="save">保存日程</ElButton></template>
    </ElDialog>

    <ElDrawer v-model="historyOpen" :title="`执行记录 · ${selected?.data.name || ''}`" size="min(700px, 96vw)">
      <div class="admin-page">
      <div class="admin-page__toolbar"><ElText type="info">{{ selected?.data.description }}</ElText><div class="admin-page__search"><ElButton :icon="RefreshCw" :loading="historyLoading" @click="loadHistory">刷新记录</ElButton></div></div>
      <ElAlert v-if="historyError" type="error" :title="historyError" :closable="false" />
      <ElTable v-loading="historyLoading" :data="sessions" row-key="id" element-loading-text="正在加载执行记录">
        <ElTableColumn label="会话创建时间（上海）" min-width="190"><template #default="{ row }">{{ displayTime(row.created_at) }}</template></ElTableColumn>
        <ElTableColumn prop="id" label="执行会话" min-width="150" show-overflow-tooltip />
        <ElTableColumn label="内容" width="100" align="center"><template #default="{ row }"><ElButton link type="primary" @click="showMessages(row.id)">查看内容</ElButton></template></ElTableColumn>
        <template #empty><ElEmpty v-if="!historyLoading" :image-size="64" :description="historyError ? '执行记录加载失败' : '暂无执行会话'" /></template>
      </ElTable>
      <div class="admin-page__footer"><ElPagination v-model:current-page="historyPage" v-model:page-size="historyPageSize" :page-sizes="[10, 20, 50]" :total="historyTotal" layout="total, sizes, prev, pager, next" aria-label="日程执行记录分页" @current-change="loadHistory" @size-change="historyPage = 1; loadHistory()" /></div>
      <p class="schedule-note">连续上下文会复用同一会话；会话条数不代表执行次数或成功次数。</p>
      <div v-if="activeSession" v-loading="messagesLoading" class="schedule-messages">
        <h3>执行内容</h3>
        <ElButton @click="showMessages(activeSession)">刷新内容</ElButton>
        <ElButton v-if="messagePage?.has_more" :disabled="messagesLoading" @click="olderMessages">更早内容</ElButton>
        <ElAlert v-if="messagesError" type="error" :title="messagesError" :closable="false" />
        <SemanticTag v-if="messagePage" :tone="messagePage.is_running ? 'progress' : 'neutral'" :label="messagePage.is_running ? '正在执行' : '当前未运行'" />
        <ElEmpty v-if="messagePage && !messagePage.messages.length" description="会话暂无消息" />
        <article v-for="message in messagePage?.messages || []" :key="message.id"><b>{{ message.name || message.role }}</b><pre>{{ messageText(message.content) }}</pre></article>
      </div>
      </div>
    </ElDrawer>
  </ElCard>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { ElAlert, ElButton, ElCard, ElDialog, ElDrawer, ElEmpty, ElForm, ElFormItem, ElInput, ElInputNumber, ElOption, ElPagination, ElSelect, ElSwitch, ElTable, ElTableColumn, ElText } from 'element-plus';
import { Plus, RefreshCw, RotateCcw, Search } from 'lucide-vue-next';
import { currentUid, deleteSchedule, listEmployees, listSchedules, saveSchedule, scheduleMessages, scheduleSessions, toggleSchedule, type EmployeeSchedule, type ScheduleInput, type ScheduleMessages, type ScheduleSession } from '@/api';
import type { Employee } from '@/types';
import RowMenu from '@/components/astra/RowMenu.vue';
import SemanticTag from '@/components/astra/SemanticTag.vue';
import { confirmDialog } from '@/composables/useConfirm';

const rows = ref<EmployeeSchedule[]>([]), choices = ref<Employee[]>([]);
const loading = ref(false), error = ref(''), page = ref(1), pageSize = ref(20), total = ref(0), busy = ref('');
const employeeOptions = ref<{ id: string; name: string }[]>([]);
const employeeFilter = ref(''), statusFilter = ref(''), query = ref(''), appliedQuery = ref('');
const hasFilters = computed(() => Boolean(employeeFilter.value || statusFilter.value || appliedQuery.value));
function applySearch() { appliedQuery.value = query.value.trim(); page.value = 1; void reload(); }
function resetFilters() { employeeFilter.value = ''; statusFilter.value = ''; query.value = ''; applySearch(); }
function changePageSize() { page.value = 1; void reload(); }
function rowActions(row: EmployeeSchedule) { return [{ command: 'edit', label: '编辑' }, { command: 'toggle', label: row.data.enabled ? '暂停' : '恢复' }, { command: 'delete', label: '删除' }]; }
function rowCommand(row: EmployeeSchedule, command: string) {
  if (command === 'edit') void openEdit(row);
  else if (command === 'toggle') void toggle(row);
  else if (command === 'delete') void remove(row);
}
const formOpen = ref(false), saving = ref(false), formError = ref(''), editing = ref<EmployeeSchedule | null>(null), employeeId = ref('');
const defaults = (): ScheduleInput => ({ name: '', description: '', cron_expression: '0 9 * * 1-5', timezone: 'Asia/Shanghai', stateful: false, enabled: true });
const form = reactive(defaults());
const frequency = ref('weekdays'), time = ref('09:00'), weekday = ref(1), monthDay = ref(1);
const frequencies = [{ value: 'daily', label: '每天' }, { value: 'weekdays', label: '工作日（周一至周五）' }, { value: 'weekly', label: '每周' }, { value: 'monthly', label: '每月' }, { value: 'custom', label: '自定义 Cron' }];
const weekdays = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];
const timezones = ['Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Tokyo', 'UTC', 'Europe/London', 'America/New_York', 'America/Los_Angeles'];
const historyOpen = ref(false), selected = ref<EmployeeSchedule | null>(null), historyLoading = ref(false), historyError = ref('');
const sessions = ref<ScheduleSession[]>([]), historyPage = ref(1), historyPageSize = ref(20), historyTotal = ref(0);
const activeSession = ref(''), messagePage = ref<ScheduleMessages | null>(null), messagesLoading = ref(false), messagesError = ref('');
const reason = (e: unknown) => e instanceof Error ? e.message : String(e);
let listGeneration = 0, historyGeneration = 0, messageGeneration = 0;
async function reload() {
  const generation = ++listGeneration;
  loading.value = true; error.value = '';
  try {
    const result = await listSchedules(page.value, pageSize.value, {
      employee_id: employeeFilter.value,
      enabled: statusFilter.value ? statusFilter.value === 'enabled' : undefined,
      q: appliedQuery.value,
    });
    if (generation !== listGeneration) return;
    rows.value = result.items; total.value = result.total; employeeOptions.value = result.employee_options;
    if (!rows.value.length && page.value > 1) { page.value--; await reload(); }
  } catch (e) { if (generation === listGeneration) { error.value = reason(e); rows.value = []; total.value = 0; } }
  finally { if (generation === listGeneration) loading.value = false; }
}
async function loadChoices() { choices.value = await listEmployees(); }
async function openCreate() {
  editing.value = null; employeeId.value = ''; Object.assign(form, defaults());
  frequency.value = 'weekdays'; time.value = '09:00'; formError.value = ''; formOpen.value = true;
  try { await loadChoices(); } catch (e) { formError.value = reason(e); }
}
async function openEdit(row: EmployeeSchedule) {
  editing.value = row; employeeId.value = row.employee_id || ''; Object.assign(form, {
    name: row.data.name, description: row.data.description, cron_expression: row.data.cron_expression,
    timezone: row.data.timezone, stateful: row.data.stateful, enabled: row.data.enabled,
  });
  frequency.value = 'custom'; formError.value = ''; formOpen.value = true;
  try { await loadChoices(); } catch (e) { formError.value = reason(e); }
}
async function save() {
  if (saving.value) return;
  formError.value = '';
  if (!form.name.trim() || !form.description.trim() || (!editing.value && !employeeId.value)) { formError.value = '请填写名称、员工和安排内容'; return; }
  if (frequency.value !== 'custom') {
    if (!/^\d{2}:\d{2}$/.test(time.value)) { formError.value = '请选择执行时间'; return; }
    const [hour, minute] = time.value.split(':').map(Number);
    form.cron_expression = `${minute} ${hour} ${frequency.value === 'monthly' ? monthDay.value : '*'} * ${frequency.value === 'weekly' ? weekday.value : frequency.value === 'weekdays' ? '1-5' : '*'}`;
  }
  saving.value = true;
  try {
    await saveSchedule({ ...form, ...(!editing.value ? { employee_id: employeeId.value } : {}) }, editing.value?.id);
    formOpen.value = false; await reload();
  } catch (e) { formError.value = reason(e); } finally { saving.value = false; }
}
async function toggle(row: EmployeeSchedule) {
  busy.value = row.id;
  try { await toggleSchedule(row.id, !row.data.enabled); await reload(); }
  catch (e) { error.value = reason(e); } finally { busy.value = ''; }
}
async function remove(row: EmployeeSchedule) {
  if (!await confirmDialog({ title: '删除日程', message: `删除“${row.data.name}”及其执行会话，并中断正在进行的运行？`, danger: true, confirmLabel: '删除' })) return;
  busy.value = row.id;
  try { await deleteSchedule(row.id); await reload(); }
  catch (e) { error.value = reason(e); } finally { busy.value = ''; }
}
async function openHistory(row: EmployeeSchedule) {
  selected.value = row; historyPage.value = 1; historyOpen.value = true;
  activeSession.value = ''; messagePage.value = null; ++messageGeneration;
  await loadHistory();
}
async function loadHistory() {
  if (!selected.value) return;
  const generation = ++historyGeneration;
  historyLoading.value = true; historyError.value = ''; sessions.value = [];
  try { const result = await scheduleSessions(selected.value.id, historyPage.value, historyPageSize.value); if (generation === historyGeneration) { sessions.value = result.items; historyTotal.value = result.total; } }
  catch (e) { if (generation === historyGeneration) historyError.value = reason(e); }
  finally { if (generation === historyGeneration) historyLoading.value = false; }
}
async function showMessages(id: string, before = '') {
  if (!selected.value) return;
  const generation = ++messageGeneration;
  activeSession.value = id; messagesLoading.value = true; messagesError.value = ''; messagePage.value = null;
  try { const result = await scheduleMessages(selected.value.id, id, before); if (generation === messageGeneration) messagePage.value = result; }
  catch (e) { if (generation === messageGeneration) messagesError.value = reason(e); }
  finally { if (generation === messageGeneration) messagesLoading.value = false; }
}
function olderMessages() { const before = messagePage.value?.messages[0]?.id; if (before) void showMessages(activeSession.value, before); }
function messageText(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.filter(block => typeof block === 'object' && block && block.type === 'text' && typeof block.text === 'string').map(block => block.text).join('\n') || '本条记录没有文本输出';
  return '本条记录没有文本输出';
}
function scheduleTime(cron: string): string {
  const parts = cron.split(/\s+/);
  const [minute, hour, day, month, week] = parts;
  if (parts.length !== 5 || !/^\d+$/.test(minute || '') || !/^\d+$/.test(hour || '')) return `Cron: ${cron}`;
  const at = `${hour!.padStart(2, '0')}:${minute!.padStart(2, '0')}`;
  if (day === '*' && month === '*') {
    if (week === '*') return `每天 ${at}`;
    if (week === '1-5') return `工作日 ${at}`;
    if (/^[0-7]$/.test(week || '')) return `${weekdays[Number(week) % 7]} ${at}`;
  }
  if (month === '*' && week === '*' && /^\d+$/.test(day || '')) return `每月 ${day} 日 ${at}`;
  return `Cron: ${cron}`;
}
function displayTime(value: string) { if (!value) return '—'; const utc = /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`; return new Date(utc).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }); }
onMounted(reload);
</script>

<style scoped>
.schedule-heading-lead { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; }
.schedule-title { max-width: 100%; overflow: hidden; text-overflow: ellipsis; font-weight: 600; }
.schedule-summary { margin-top: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.schedule-form-row { display: flex; gap: 12px; }
.schedule-form-row > * { flex: 1; min-width: 0; }
small, .schedule-note { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
small { display: block; width: 100%; }
.schedule-messages { margin-top: 24px; }
article { padding: 16px 0; border-bottom: 1px solid var(--astra-border); }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; }
</style>
