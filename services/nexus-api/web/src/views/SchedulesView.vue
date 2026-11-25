<!-- 依据 AgentScope v2.0.10 官方 schedule 页的表单、列表与执行详情语义适配。
     来源和有意差异见 docs/architecture/employee-schedules.md。 -->
<template>
  <ElCard class="admin-card admin-card--fill" data-testid="schedules-page">
    <template #header>
      <div class="admin-card__header">
        <div class="schedule-heading-lead">
          <h2 class="admin-card__title">日程安排</h2>
          <span v-if="currentUid()" class="owner-chip" :title="`仅当前账号可见：${currentUid()}`">
            <User :size="12" aria-hidden="true" />
            <span>{{ currentUid() }}</span>
          </span>
        </div>
        <div class="admin-card__actions">
          <ElButton :icon="RefreshCw" :loading="loading" data-testid="schedules-refresh" @click="reload">刷新</ElButton>
          <ElButton type="primary" :icon="Plus" data-testid="schedules-create" @click="openCreate">新建日程</ElButton>
        </div>
      </div>
    </template>

    <div class="admin-page admin-page--fill">
      <div class="admin-page__toolbar" role="search" aria-label="日程筛选">
        <div class="admin-page__filters">
          <ElSelect
            v-model="employeeFilter"
            class="admin-page__select"
            clearable
            filterable
            placeholder="员工：全部"
            aria-label="日程员工"
            @change="applySearch"
          >
            <ElOption v-for="employee in employeeOptions" :key="employee.id" :label="employee.name" :value="employee.id" />
          </ElSelect>
          <ElSelect
            v-model="statusFilter"
            class="admin-page__select"
            clearable
            placeholder="状态：全部"
            aria-label="日程状态"
            @change="applySearch"
          >
            <ElOption label="已启用" value="enabled" />
            <ElOption label="已暂停" value="paused" />
          </ElSelect>
        </div>
        <div class="admin-page__search">
          <ElInput
            v-model="query"
            class="admin-page__keyword"
            placeholder="搜索日程名称 / 指令内容"
            aria-label="搜索日程"
            clearable
            :prefix-icon="Search"
            @clear="applySearch"
            @keydown.enter.prevent="applySearch"
          />
          <ElButton type="primary" :icon="Search" @click="applySearch">搜索</ElButton>
          <ElButton :icon="RotateCcw" @click="resetFilters">重置</ElButton>
        </div>
      </div>

      <ElAlert v-if="error && rows.length" :title="error" type="error" :closable="false" show-icon />

      <div class="admin-page__table">
        <ElTable
          ref="tableRef"
          v-loading="loading"
          :data="rows"
          row-key="id"
          height="100%"
          element-loading-text="正在加载日程列表"
          data-testid="schedules-table"
        >
        <ElTableColumn label="日程" min-width="250">
          <template #default="{ row }">
            <div class="schedule-name-cell">
              <button
                type="button"
                class="schedule-title-btn"
                :class="{ 'is-paused': !row.data.enabled }"
                :title="row.data.name"
                @click="openHistory(row as EmployeeSchedule)"
              >{{ row.data.name }}</button>
              <div class="schedule-excerpt" :title="row.data.description">{{ plainExcerpt(row.data.description) }}</div>
            </div>
          </template>
        </ElTableColumn>

        <ElTableColumn label="数字员工" width="144" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.employee_id" class="schedule-emp">{{ row.employee_name }}</span>
            <ElTag v-else size="small" type="info" class="astra-tag--neutral">{{ row.employee_name }}</ElTag>
          </template>
        </ElTableColumn>

        <ElTableColumn label="触发规则" min-width="240">
          <template #default="{ row }">
            <div class="schedule-rule">
              <div class="schedule-rule__human" :class="{ 'is-paused': !row.data.enabled }">
                <i class="status-dot" :class="row.data.enabled ? 'is-on' : 'is-off'" aria-hidden="true" />
                <span>{{ describeCron(row.data.cron_expression).label }}</span>
              </div>
              <div class="schedule-rule__meta">
                <code class="cron-chip" :title="row.data.cron_expression">{{ row.data.cron_expression }}</code>
                <span class="tz-label">{{ row.data.timezone }}</span>
              </div>
            </div>
          </template>
        </ElTableColumn>

        <ElTableColumn label="上下文" width="112" align="center">
          <template #default="{ row }">
            <ElTag size="small" type="info" class="astra-tag--neutral">
              {{ row.data.stateful ? '连续上下文' : '每次独立' }}
            </ElTag>
          </template>
        </ElTableColumn>

        <ElTableColumn label="创建时间" width="156">
          <template #default="{ row }">
            <span class="mono-time" :title="displayTime(row.created_at, true)">{{ displayTime(row.created_at) }}</span>
          </template>
        </ElTableColumn>

        <ElTableColumn label="启用" width="80" align="center">
          <template #default="{ row }">
            <ElSwitch
              size="small"
              :model-value="(row as EmployeeSchedule).data.enabled"
              :loading="busy === row.id"
              :disabled="busy === row.id"
              :aria-label="`${(row as EmployeeSchedule).data.name} 启用状态`"
              @change="toggle(row as EmployeeSchedule, Boolean($event))"
            />
          </template>
        </ElTableColumn>

        <ElTableColumn label="操作" width="168" align="center" fixed="right">
          <template #default="{ row }">
            <div class="schedule-row-actions">
              <ElButton link type="primary" :disabled="busy === row.id" @click="openHistory(row as EmployeeSchedule)">执行记录</ElButton>
              <ElButton link type="primary" :disabled="busy === row.id" @click="openEdit(row as EmployeeSchedule)">编辑</ElButton>
              <ElDropdown trigger="click" :disabled="busy === row.id" @command="cmd => rowCommand(row as EmployeeSchedule, String(cmd))">
                <ElButton link type="primary" :disabled="busy === row.id" :aria-label="`更多操作 ${(row as EmployeeSchedule).data.name}`">更多</ElButton>
                <template #dropdown>
                  <ElDropdownMenu>
                    <ElDropdownItem command="copy-cron">复制 Cron</ElDropdownItem>
                    <ElDropdownItem command="delete" divided class="danger-item">删除日程</ElDropdownItem>
                  </ElDropdownMenu>
                </template>
              </ElDropdown>
            </div>
          </template>
        </ElTableColumn>

        <template #empty>
          <ElEmpty v-if="error" :image-size="72" description="日程数据加载失败">
            <div class="empty-stack">
              <ElText type="danger">{{ error }}</ElText>
              <ElButton :icon="RefreshCw" @click="reload">重新加载</ElButton>
            </div>
          </ElEmpty>
          <ElEmpty v-else-if="!loading" :image-size="72" :description="hasFilters ? '暂无匹配日程' : '暂无日程'">
            <ElButton v-if="hasFilters" @click="resetFilters">清除筛选</ElButton>
            <ElButton v-else type="primary" :icon="Plus" @click="openCreate">新建日程</ElButton>
          </ElEmpty>
        </template>
      </ElTable>
      </div>

      <div class="admin-page__footer">
        <ElPagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50]"
          :total="total"
          layout="total, sizes, prev, pager, next"
          aria-label="日程列表分页"
          data-testid="schedules-pagination"
          @current-change="reload"
          @size-change="changePageSize"
        />
      </div>
    </div>

    <!-- 新建 / 编辑日程抽屉 -->
    <ElDrawer
      v-model="formOpen"
      append-to-body
      :title="editing ? `编辑日程 · ${editing.data.name}` : '新建日程'"
      size="min(660px, 100%)"
      :close-on-click-modal="false"
      :before-close="closeForm"
    >
      <ElForm label-position="top" :disabled="saving" class="schedule-drawer-form" @submit.prevent="save">
        <div class="section-cap">基础信息</div>
        <div class="form-pair">
          <ElFormItem label="日程名称" required>
            <ElInput v-model="form.name" maxlength="120" placeholder="例如：工作日晨间简报" />
          </ElFormItem>
          <ElFormItem label="数字员工" required>
            <ElSelect
              v-model="employeeId"
              filterable
              :disabled="!!editing || saving"
              placeholder="选择数字员工"
              style="width: 100%"
            >
              <ElOption
                v-for="employee in choices"
                :key="employee.id"
                :value="employee.id"
                :label="employeeOptionLabel(employee)"
                :disabled="!employee.enabled || !employee.agentscope_agent_id"
              />
            </ElSelect>
          </ElFormItem>
        </div>

        <div class="section-cap">触发规则</div>
        <div class="preset-row" role="group" aria-label="常用周期预设">
          <button
            v-for="preset in CRON_PRESETS"
            :key="preset.cron"
            type="button"
            class="preset-pill"
            :class="{ 'is-active': effectiveCron === preset.cron }"
            :disabled="saving"
            @click="applyPreset(preset.cron)"
          >
            <span>{{ preset.label }}</span>
          </button>
        </div>

        <ElFormItem label="重复方式">
          <ElRadioGroup
            :model-value="frequency"
            size="small"
            aria-label="重复方式"
            @update:model-value="selectFrequency(String($event) as FrequencyMode)"
          >
            <ElRadioButton v-for="option in frequencies" :key="option.value" :value="option.value">
              {{ option.label }}
            </ElRadioButton>
          </ElRadioGroup>
        </ElFormItem>

        <div class="form-pair">
          <ElFormItem v-if="frequency === 'daily' || frequency === 'weekdays'" label="执行时间">
            <ElInput v-model="time" type="time" :disabled="saving" aria-label="执行时间" />
          </ElFormItem>

          <div v-else-if="frequency === 'weekly'" class="inline-pair">
            <ElFormItem label="星期">
              <ElSelect v-model="weekday" aria-label="星期" style="width: 100%">
                <ElOption v-for="(day, index) in weekdays" :key="day" :label="day" :value="index" />
              </ElSelect>
            </ElFormItem>
            <ElFormItem label="执行时间">
              <ElInput v-model="time" type="time" :disabled="saving" aria-label="执行时间" />
            </ElFormItem>
          </div>

          <div v-else-if="frequency === 'monthly'" class="inline-pair">
            <ElFormItem label="每月日期">
              <ElInputNumber
                v-model="monthDay"
                :min="1"
                :max="31"
                controls-position="right"
                aria-label="每月日期"
                style="width: 100%"
              />
            </ElFormItem>
            <ElFormItem label="执行时间">
              <ElInput v-model="time" type="time" :disabled="saving" aria-label="执行时间" />
            </ElFormItem>
          </div>

          <ElFormItem v-else label="Cron 表达式（分 时 日 月 周）" required>
            <ElInput
              v-model="form.cron_expression"
              class="mono-input"
              maxlength="120"
              placeholder="例如：0 */2 * * *"
            />
          </ElFormItem>

          <ElFormItem label="时区">
            <ElSelect v-model="form.timezone" filterable allow-create default-first-option style="width: 100%">
              <ElOption v-for="zone in timezones" :key="zone" :value="zone" :label="zone" />
            </ElSelect>
          </ElFormItem>
        </div>

        <div class="cron-preview" :class="{ 'is-invalid': !effectiveCronMeta.valid }">
          <div class="cron-preview__lead">
            <Clock :size="14" class="cron-preview__icon" aria-hidden="true" />
            <span class="cron-preview__label">
              {{ effectiveCronMeta.valid ? effectiveCronMeta.label : '表达式无效，请输入 5 位 Cron（分 时 日 月 周）' }}
            </span>
          </div>
          <div class="cron-preview__meta">
            <code class="cron-chip">{{ effectiveCron || '—' }}</code>
            <span class="tz-label">{{ form.timezone }}</span>
          </div>
        </div>

        <div class="section-cap">执行指令</div>
        <ElFormItem label="安排内容" required>
          <div class="prompt-box">
            <div class="prompt-box__bar">
              <span class="prompt-box__count">{{ form.description.length }} / 16000 字 · 支持 Markdown</span>
              <ElRadioGroup v-model="descMode" size="small" aria-label="安排内容视图">
                <ElRadioButton value="edit">编辑</ElRadioButton>
                <ElRadioButton value="preview">预览</ElRadioButton>
              </ElRadioGroup>
            </div>
            <ElInput
              v-if="descMode === 'edit'"
              v-model="form.description"
              class="prompt-box__input"
              type="textarea"
              :rows="6"
              maxlength="16000"
              resize="vertical"
              placeholder="告诉数字员工到时间后需要做什么，以及期望产出格式"
            />
            <div v-else class="prompt-box__preview">
              <MarkdownView :content="form.description" compact empty-text="暂无安排内容" />
            </div>
          </div>
        </ElFormItem>

        <div class="section-cap">会话模式</div>
        <div class="context-mode-row">
          <ElRadioGroup v-model="form.stateful" size="small" aria-label="上下文模式">
            <ElRadioButton :value="false">每次独立</ElRadioButton>
            <ElRadioButton :value="true">连续上下文</ElRadioButton>
          </ElRadioGroup>
          <span class="context-mode-hint">
            {{ form.stateful ? '复用同一会话并保留历史记忆' : '每次触发创建全新会话' }}
          </span>
        </div>
      </ElForm>

      <template #footer>
        <ElAlert v-if="formError" class="drawer-error" :title="formError" type="error" :closable="false" show-icon />
        <div class="drawer-footer">
          <ElSwitch v-model="form.enabled" :disabled="saving" active-text="保存后启用" aria-label="启用日程" />
          <div class="drawer-footer__right">
            <ElTag v-if="formGuard.dirty.value" size="small" type="warning" effect="light">有未保存修改</ElTag>
            <ElButton :disabled="saving" @click="closeForm">取消</ElButton>
            <ElButton type="primary" :loading="saving" @click="save">保存日程</ElButton>
          </div>
        </div>
      </template>
    </ElDrawer>

    <!-- 日程详情与执行记录抽屉 -->
    <ElDrawer
      v-model="historyOpen"
      append-to-body
      :title="selected ? `执行记录 · ${selected.data.name}` : '执行记录'"
      size="min(780px, 96vw)"
    >
      <div v-if="selected" class="detail-stack">
        <div class="detail-hero">
          <div class="detail-hero__top">
            <h3 class="detail-hero__title">{{ selected.data.name }}</h3>
            <ElTag
              size="small"
              :type="selected.data.enabled ? 'success' : 'info'"
              :class="{ 'astra-tag--neutral': !selected.data.enabled }"
            >{{ selected.data.enabled ? '已启用' : '已暂停' }}</ElTag>
            <ElTag size="small" type="info" class="astra-tag--neutral">
              {{ selected.data.stateful ? '连续上下文' : '每次独立' }}
            </ElTag>
          </div>
          <div class="detail-hero__meta">
            <span>数字员工：<b>{{ selected.employee_name }}</b></span>
            <span class="detail-hero__rule">
              触发：<b>{{ describeCron(selected.data.cron_expression).label }}</b>
              <code class="cron-chip">{{ selected.data.cron_expression }}</code>
            </span>
            <span>时区：<b class="mono-inline">{{ selected.data.timezone }}</b></span>
            <span>创建：<b class="mono-time">{{ displayTime(selected.created_at) }}</b></span>
          </div>
        </div>

        <div class="detail-card">
          <div class="detail-card__head">
            <span class="detail-card__title">执行指令</span>
            <ElButton link type="primary" @click="instructionExpanded = !instructionExpanded">
              {{ instructionExpanded ? '收起' : '展开' }}
            </ElButton>
          </div>
          <div v-show="instructionExpanded" class="detail-card__body detail-card__body--scroll">
            <MarkdownView
              :content="selected.data.description"
              compact
              allow-source-toggle
              allow-copy
              empty-text="暂无执行指令"
            />
          </div>
        </div>

        <div class="detail-card">
          <div class="detail-card__head">
            <div class="detail-card__title-group">
              <span class="detail-card__title">执行会话</span>
              <span v-if="historyTotal > 0" class="count-pill">{{ historyTotal }}</span>
              <span v-if="selected.data.stateful" class="detail-card__sub">连续上下文复用同一会话</span>
            </div>
            <ElButton size="small" :icon="RefreshCw" :loading="historyLoading" @click="loadHistory">刷新记录</ElButton>
          </div>

          <div class="detail-card__table-wrap">
            <ElAlert v-if="historyError" class="detail-alert" type="error" :title="historyError" :closable="false" show-icon />
            <ElTable
              v-loading="historyLoading"
              :data="sessions"
              row-key="id"
              max-height="230"
              highlight-current-row
              :current-row-key="activeSession"
              element-loading-text="正在加载执行记录"
              @row-click="row => showMessages((row as ScheduleSession).id)"
            >
              <ElTableColumn label="会话创建时间（上海）" min-width="180">
                <template #default="{ row }">
                  <span class="mono-time">{{ displayTime(row.created_at, true) }}</span>
                </template>
              </ElTableColumn>
              <ElTableColumn label="会话标识" min-width="150" show-overflow-tooltip>
                <template #default="{ row }">
                  <code class="session-id-chip" :title="row.id">{{ shortId(row.id) }}</code>
                </template>
              </ElTableColumn>
              <ElTableColumn label="内容" width="104" align="center">
                <template #default="{ row }">
                  <ElButton link type="primary" @click.stop="showMessages(row.id)">
                    {{ activeSession === row.id ? '当前查看' : '查看内容' }}
                  </ElButton>
                </template>
              </ElTableColumn>
              <template #empty>
                <ElEmpty
                  v-if="!historyLoading"
                  :image-size="56"
                  :description="historyError ? '执行记录加载失败' : '暂无执行会话'"
                />
              </template>
            </ElTable>
            <div v-if="historyTotal > 0" class="detail-card__foot">
              <ElPagination
                v-model:current-page="historyPage"
                v-model:page-size="historyPageSize"
                :page-sizes="[10, 20, 50]"
                :total="historyTotal"
                layout="total, sizes, prev, pager, next"
                aria-label="日程执行记录分页"
                @current-change="loadHistory"
                @size-change="historyPage = 1; loadHistory()"
              />
            </div>
          </div>
        </div>

        <div v-if="activeSession" class="detail-card">
          <div class="detail-card__head">
            <div class="detail-card__title-group">
              <span class="detail-card__title">执行输出</span>
              <code class="session-id-chip" :title="activeSession">{{ shortId(activeSession) }}</code>
              <SemanticTag
                v-if="messagePage"
                :tone="messagePage.is_running ? 'progress' : 'neutral'"
                :label="messagePage.is_running ? '正在执行' : '当前未运行'"
              />
            </div>
            <div class="detail-card__actions">
              <ElButton v-if="messagePage?.has_more" size="small" :disabled="messagesLoading" @click="olderMessages">
                更早内容
              </ElButton>
              <ElButton size="small" :icon="RefreshCw" :loading="messagesLoading" @click="showMessages(activeSession)">
                刷新内容
              </ElButton>
            </div>
          </div>

          <div v-loading="messagesLoading" class="detail-card__body">
            <ElAlert v-if="messagesError" class="detail-body-alert" type="error" :title="messagesError" :closable="false" show-icon />
            <ElEmpty
              v-if="messagePage && !messagePage.messages.length && !messagesError"
              :image-size="56"
              description="会话暂无消息"
            />
            <div v-if="messagePage?.messages.length" class="message-list">
              <article
                v-for="message in messagePage.messages"
                :key="message.id"
                class="message-card"
                :class="message.role === 'assistant' ? 'is-assistant' : 'is-trigger'"
              >
                <div class="message-card__head">
                  <span class="message-card__author">{{ messageAuthor(message) }}</span>
                  <ElTag size="small" type="info" class="astra-tag--neutral">{{ messageRoleLabel(message.role) }}</ElTag>
                </div>
                <div class="message-card__body">
                  <MarkdownView
                    :content="messageText(message.content)"
                    compact
                    allow-source-toggle
                    allow-copy
                    empty-text="本条记录没有文本输出"
                  />
                </div>
              </article>
            </div>
          </div>
        </div>
      </div>

      <template #footer>
        <div class="detail-footer">
          <span class="detail-footer__spacer" />
          <ElButton @click="historyOpen = false">关闭</ElButton>
          <ElButton v-if="selected" type="primary" :icon="Pencil" @click="editFromHistory">编辑日程</ElButton>
        </div>
      </template>
    </ElDrawer>
  </ElCard>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { onBeforeRouteLeave } from 'vue-router';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElDrawer,
  ElDropdown,
  ElDropdownItem,
  ElDropdownMenu,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElInputNumber,
  ElMessage,
  ElOption,
  ElPagination,
  ElRadioButton,
  ElRadioGroup,
  ElSelect,
  ElSwitch,
  ElTable,
  ElTableColumn,
  ElTag,
  ElText,
} from 'element-plus';
import type { TableInstance } from 'element-plus';
import { Clock, Pencil, Plus, RefreshCw, RotateCcw, Search, User } from 'lucide-vue-next';
import {
  currentUid,
  deleteSchedule,
  listEmployees,
  listSchedules,
  saveSchedule,
  scheduleMessages,
  scheduleSessions,
  toggleSchedule,
  type EmployeeSchedule,
  type ScheduleInput,
  type ScheduleMessages,
  type ScheduleSession,
} from '@/api';
import MarkdownView from '@/components/astra/MarkdownView.vue';
import SemanticTag from '@/components/astra/SemanticTag.vue';
import { confirmDialog } from '@/composables/useConfirm';
import { useFormGuard } from '@/composables/useFormGuard';
import type { Employee } from '@/types';

type FrequencyMode = 'daily' | 'weekdays' | 'weekly' | 'monthly' | 'custom';

const CRON_PRESETS = [
  { label: '每小时', cron: '0 * * * *' },
  { label: '每 2 小时', cron: '0 */2 * * *' },
  { label: '每天 09:00', cron: '0 9 * * *' },
  { label: '工作日 09:00', cron: '0 9 * * 1-5' },
  { label: '每周一 09:00', cron: '0 9 * * 1' },
  { label: '每月 1 日 09:00', cron: '0 9 1 * *' },
] as const;

const frequencies: { value: FrequencyMode; label: string }[] = [
  { value: 'daily', label: '每天' },
  { value: 'weekdays', label: '工作日' },
  { value: 'weekly', label: '每周' },
  { value: 'monthly', label: '每月' },
  { value: 'custom', label: '自定义 Cron' },
];

const weekdays = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];
const shortWeekdays = ['日', '一', '二', '三', '四', '五', '六'];
const timezones = ['Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Tokyo', 'UTC', 'Europe/London', 'America/New_York', 'America/Los_Angeles'];

const DOW_ALIASES: Record<string, number> = {
  SUN: 0, MON: 1, TUE: 2, WED: 3, THU: 4, FRI: 5, SAT: 6,
};
const MONTH_ALIASES: Record<string, number> = {
  JAN: 1, FEB: 2, MAR: 3, APR: 4, MAY: 5, JUN: 6,
  JUL: 7, AUG: 8, SEP: 9, OCT: 10, NOV: 11, DEC: 12,
};

const rows = ref<EmployeeSchedule[]>([]);
const choices = ref<Employee[]>([]);
const loading = ref(false);
const error = ref('');
const page = ref(1);
const pageSize = ref(20);
const total = ref(0);
const tableRef = ref<TableInstance>();
const busy = ref('');
const employeeOptions = ref<{ id: string; name: string }[]>([]);
const employeeFilter = ref('');
const statusFilter = ref('');
const query = ref('');
const appliedQuery = ref('');

const hasFilters = computed(() => Boolean(employeeFilter.value || statusFilter.value || appliedQuery.value));

function applySearch() {
  appliedQuery.value = query.value.trim();
  page.value = 1;
  void reload();
}

function resetFilters() {
  employeeFilter.value = '';
  statusFilter.value = '';
  query.value = '';
  applySearch();
}

function changePageSize() {
  page.value = 1;
  void reload();
}

const formOpen = ref(false);
const saving = ref(false);
const formError = ref('');
const editing = ref<EmployeeSchedule | null>(null);
const employeeId = ref('');
const descMode = ref<'edit' | 'preview'>('edit');

const defaults = (): ScheduleInput => ({
  name: '',
  description: '',
  cron_expression: '0 9 * * 1-5',
  timezone: 'Asia/Shanghai',
  stateful: false,
  enabled: true,
});

const form = reactive(defaults());
const frequency = ref<FrequencyMode>('weekdays');
const time = ref('09:00');
const weekday = ref(1);
const monthDay = ref(1);

const effectiveCron = computed(() => {
  if (frequency.value === 'custom') return form.cron_expression.trim();
  if (!/^\d{2}:\d{2}$/.test(time.value)) return '';
  const [hour, minute] = time.value.split(':').map(Number);
  const dom = frequency.value === 'monthly' ? String(monthDay.value) : '*';
  const dow = frequency.value === 'weekly' ? String(weekday.value) : frequency.value === 'weekdays' ? '1-5' : '*';
  return `${minute} ${hour} ${dom} * ${dow}`;
});

const effectiveCronMeta = computed(() => describeCron(effectiveCron.value));

const formGuard = useFormGuard({
  isOpen: () => formOpen.value,
  isSaving: () => saving.value,
  snapshot: () => JSON.stringify({
    form,
    employeeId: employeeId.value,
    frequency: frequency.value,
    time: time.value,
    weekday: weekday.value,
    monthDay: monthDay.value,
  }),
  confirmDiscard: () => confirmDialog({
    title: '放弃未保存的日程修改？',
    message: '日程名称、安排内容和触发规则尚未保存，关闭后将无法恢复。',
    confirmLabel: '放弃并关闭',
    cancelLabel: '继续编辑',
    danger: true,
  }),
});

async function closeForm() {
  if (await formGuard.canClose()) formOpen.value = false;
}

onBeforeRouteLeave(() => formGuard.canClose());

const historyOpen = ref(false);
const selected = ref<EmployeeSchedule | null>(null);
const instructionExpanded = ref(true);
const historyLoading = ref(false);
const historyError = ref('');
const sessions = ref<ScheduleSession[]>([]);
const historyPage = ref(1);
const historyPageSize = ref(20);
const historyTotal = ref(0);
const activeSession = ref('');
const messagePage = ref<ScheduleMessages | null>(null);
const messagesLoading = ref(false);
const messagesError = ref('');

const reason = (e: unknown) => (e instanceof Error ? e.message : String(e));
let listGeneration = 0;
let historyGeneration = 0;
let messageGeneration = 0;

function employeeOptionLabel(employee: Employee): string {
  if (!employee.enabled) return `${employee.name}（已禁用）`;
  if (!employee.agentscope_agent_id) return `${employee.name}（未绑定）`;
  return employee.name;
}

function selectFrequency(next: FrequencyMode) {
  if (next === frequency.value) return;
  if (next === 'custom' && effectiveCron.value) {
    form.cron_expression = effectiveCron.value;
  }
  frequency.value = next;
}

function applyPreset(cron: string) {
  form.cron_expression = cron;
  restoreFrequency(cron);
}

async function reload() {
  const generation = ++listGeneration;
  loading.value = true;
  error.value = '';
  try {
    const result = await listSchedules(page.value, pageSize.value, {
      employee_id: employeeFilter.value,
      enabled: statusFilter.value ? statusFilter.value === 'enabled' : undefined,
      q: appliedQuery.value,
    });
    if (generation !== listGeneration) return;
    rows.value = result.items;
    total.value = result.total;
    employeeOptions.value = result.employee_options;
    tableRef.value?.setScrollTop(0);
    if (!rows.value.length && page.value > 1) {
      page.value--;
      await reload();
    }
  } catch (e) {
    if (generation === listGeneration) {
      error.value = reason(e);
      rows.value = [];
      total.value = 0;
    }
  } finally {
    if (generation === listGeneration) loading.value = false;
  }
}

async function loadChoices() {
  choices.value = await listEmployees();
}

async function openCreate() {
  editing.value = null;
  employeeId.value = '';
  Object.assign(form, defaults());
  frequency.value = 'weekdays';
  time.value = '09:00';
  weekday.value = 1;
  monthDay.value = 1;
  descMode.value = 'edit';
  formError.value = '';
  formOpen.value = true;
  formGuard.capture();
  try {
    await loadChoices();
  } catch (e) {
    formError.value = reason(e);
  }
}

function restoreFrequency(cron: string) {
  const parts = cron.trim().split(/\s+/);
  const [minute, hour, day, month, week] = parts;
  const valid = parts.length === 5
    && /^\d+$/.test(minute!)
    && /^\d+$/.test(hour!)
    && Number(minute) <= 59
    && Number(hour) <= 23
    && month === '*';
  frequency.value = 'custom';
  if (!valid) return;
  const at = `${hour!.padStart(2, '0')}:${minute!.padStart(2, '0')}`;
  if (day === '*' && week === '*') frequency.value = 'daily';
  else if (day === '*' && week === '1-5') frequency.value = 'weekdays';
  else if (day === '*' && /^[0-7]$/.test(week!)) {
    frequency.value = 'weekly';
    weekday.value = Number(week) % 7;
  } else if (week === '*' && /^\d+$/.test(day!) && Number(day) >= 1 && Number(day) <= 31) {
    frequency.value = 'monthly';
    monthDay.value = Number(day);
  } else {
    return;
  }
  time.value = at;
}

async function openEdit(row: EmployeeSchedule) {
  editing.value = row;
  employeeId.value = row.employee_id || '';
  Object.assign(form, {
    name: row.data.name,
    description: row.data.description,
    cron_expression: row.data.cron_expression,
    timezone: row.data.timezone,
    stateful: row.data.stateful,
    enabled: row.data.enabled,
  });
  restoreFrequency(row.data.cron_expression);
  descMode.value = 'edit';
  formError.value = '';
  formOpen.value = true;
  formGuard.capture();
  try {
    await loadChoices();
  } catch (e) {
    formError.value = reason(e);
  }
}

function editFromHistory() {
  const target = selected.value;
  if (!target) return;
  historyOpen.value = false;
  void openEdit(target);
}

async function save() {
  if (saving.value) return;
  formError.value = '';
  if (!form.name.trim() || !form.description.trim() || (!editing.value && !employeeId.value)) {
    formError.value = '请填写日程名称、数字员工和安排内容';
    return;
  }
  const cron = effectiveCron.value;
  if (!cron || !describeCron(cron).valid) {
    formError.value = frequency.value === 'custom'
      ? '请输入有效的 5 位 Cron 表达式（分 时 日 月 周）'
      : '请选择有效的执行时间';
    return;
  }
  form.cron_expression = cron;
  saving.value = true;
  try {
    await saveSchedule(
      {
        name: form.name.trim(),
        description: form.description.trim(),
        cron_expression: cron,
        timezone: form.timezone.trim() || 'Asia/Shanghai',
        stateful: form.stateful,
        enabled: form.enabled,
        ...(!editing.value ? { employee_id: employeeId.value } : {}),
      },
      editing.value?.id,
    );
    formOpen.value = false;
    ElMessage.success(editing.value ? '日程已更新' : '日程已创建');
    await reload();
  } catch (e) {
    formError.value = reason(e);
  } finally {
    saving.value = false;
  }
}

async function toggle(row: EmployeeSchedule, nextEnabled: boolean) {
  if (busy.value === row.id) return;
  busy.value = row.id;
  try {
    await toggleSchedule(row.id, nextEnabled);
    ElMessage.success(nextEnabled ? '日程已启用，约 60 秒内生效' : '日程已暂停，约 60 秒内生效');
    await reload();
  } catch (e) {
    ElMessage.error(reason(e));
  } finally {
    busy.value = '';
  }
}

async function copyCron(cron: string) {
  try {
    await navigator.clipboard.writeText(cron);
    ElMessage.success(`已复制：${cron}`);
  } catch {
    ElMessage.error('复制失败，请手动选择复制');
  }
}

async function remove(row: EmployeeSchedule) {
  if (!await confirmDialog({
    title: '删除日程',
    message: `删除“${row.data.name}”及其执行会话，并中断正在进行的运行？`,
    danger: true,
    confirmLabel: '删除',
  })) return;
  busy.value = row.id;
  try {
    await deleteSchedule(row.id);
    ElMessage.success('日程已删除');
    await reload();
  } catch (e) {
    ElMessage.error(reason(e));
  } finally {
    busy.value = '';
  }
}

function rowCommand(row: EmployeeSchedule, command: string) {
  if (command === 'copy-cron') void copyCron(row.data.cron_expression);
  else if (command === 'delete') void remove(row);
}

async function openHistory(row: EmployeeSchedule) {
  selected.value = row;
  instructionExpanded.value = true;
  historyPage.value = 1;
  historyOpen.value = true;
  activeSession.value = '';
  messagePage.value = null;
  ++messageGeneration;
  await loadHistory();
}

async function loadHistory() {
  if (!selected.value) return;
  const generation = ++historyGeneration;
  historyLoading.value = true;
  historyError.value = '';
  sessions.value = [];
  try {
    const result = await scheduleSessions(selected.value.id, historyPage.value, historyPageSize.value);
    if (generation !== historyGeneration) return;
    sessions.value = result.items;
    historyTotal.value = result.total;
    if (result.items.length) {
      if (!activeSession.value || !result.items.some(item => item.id === activeSession.value)) {
        void showMessages(result.items[0]!.id);
      }
    } else {
      activeSession.value = '';
      messagePage.value = null;
      ++messageGeneration;
    }
  } catch (e) {
    if (generation === historyGeneration) historyError.value = reason(e);
  } finally {
    if (generation === historyGeneration) historyLoading.value = false;
  }
}

async function showMessages(id: string, before = '') {
  if (!selected.value) return;
  const generation = ++messageGeneration;
  activeSession.value = id;
  messagesLoading.value = true;
  messagesError.value = '';
  messagePage.value = null;
  try {
    const result = await scheduleMessages(selected.value.id, id, before);
    if (generation === messageGeneration) messagePage.value = result;
  } catch (e) {
    if (generation === messageGeneration) messagesError.value = reason(e);
  } finally {
    if (generation === messageGeneration) messagesLoading.value = false;
  }
}

function olderMessages() {
  const before = messagePage.value?.messages[0]?.id;
  if (before) void showMessages(activeSession.value, before);
}

function messageText(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .filter((block): block is { type: string; text: string } => (
        typeof block === 'object'
        && block !== null
        && (block as { type?: unknown }).type === 'text'
        && typeof (block as { text?: unknown }).text === 'string'
      ))
      .map(block => block.text)
      .join('\n');
  }
  return '';
}

function messageAuthor(message: { role: string; name?: string }): string {
  if (message.name) return message.name;
  if (message.role === 'assistant') return selected.value?.employee_name || '数字员工';
  if (message.role === 'user') return '日程触发';
  return message.role;
}

function messageRoleLabel(role: string): string {
  if (role === 'assistant') return '员工回复';
  if (role === 'user') return '触发指令';
  if (role === 'system') return '系统';
  if (role === 'tool') return '工具';
  return role;
}

function plainExcerpt(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, ' [代码] ')
    .replace(/[#>*`_~\-\[\]()]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function shortId(id: string): string {
  if (!id) return '—';
  return id.length > 14 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
}

function pad2(n: number): string {
  return String(n).padStart(2, '0');
}

function parseCronAtom(token: string, minVal: number, maxVal: number, aliases?: Record<string, number>): number | null {
  const upper = token.toUpperCase();
  if (aliases && upper in aliases) return aliases[upper]!;
  if (!/^\d+$/.test(token)) return null;
  const num = Number(token);
  return num >= minVal && num <= maxVal ? num : null;
}

function validateCronField(field: string, minVal: number, maxVal: number, aliases?: Record<string, number>): boolean {
  if (!field) return false;
  const segments = field.split(',');
  for (const seg of segments) {
    if (!seg) return false;
    const [base, step, ...extra] = seg.split('/');
    if (extra.length) return false;
    if (step !== undefined && !/^[1-9]\d*$/.test(step)) return false;
    if (base === '*') continue;
    if (base!.includes('-')) {
      const [startRaw, endRaw, ...more] = base!.split('-');
      if (more.length || !startRaw || !endRaw) return false;
      const start = parseCronAtom(startRaw, minVal, maxVal, aliases);
      const end = parseCronAtom(endRaw, minVal, maxVal, aliases);
      if (start === null || end === null || start > end) return false;
      continue;
    }
    if (parseCronAtom(base!, minVal, maxVal, aliases) === null) return false;
  }
  return true;
}

function normalizeDowField(raw: string): string {
  return raw.toUpperCase().replace(/\b(SUN|MON|TUE|WED|THU|FRI|SAT)\b/g, m => String(DOW_ALIASES[m]!));
}

function formatDowLabel(dow: string): string | null {
  const norm = normalizeDowField(dow);
  if (norm === '*') return null;
  if (norm === '1-5' || norm === '1,2,3,4,5') return '工作日';
  if (norm === '0,6' || norm === '6,0' || norm === '6,7' || norm === '6-7') return '周末';
  if (/^[0-7]$/.test(norm)) return `每周${shortWeekdays[Number(norm) % 7]}`;
  const rangeMatch = /^([0-7])-([0-7])$/.exec(norm);
  if (rangeMatch) {
    const start = Number(rangeMatch[1]) % 7;
    const end = Number(rangeMatch[2]) % 7;
    return `每周${shortWeekdays[start]}至周${shortWeekdays[end]}`;
  }
  if (/^[0-7](,[0-7])+$/.test(norm)) {
    const days = [...new Set(norm.split(',').map(d => Number(d) % 7))];
    if (days.length <= 5) return `每周${days.map(d => shortWeekdays[d]).join('、')}`;
  }
  return null;
}

function formatTimePart(min: string, hour: string): { text: string; isInterval: boolean } | null {
  const isSingleMin = /^\d+$/.test(min);
  const isSingleHour = /^\d+$/.test(hour);
  const minStep = /^\*\/([1-9]\d*)$/.exec(min);
  const hourStep = /^\*\/([1-9]\d*)$/.exec(hour);
  const hourRange = /^(\d+)-(\d+)$/.exec(hour);
  const hourRangeStep = /^(\d+)-(\d+)\/([1-9]\d*)$/.exec(hour);

  if (min === '*' && hour === '*') return { text: '每分钟', isInterval: true };

  if (minStep && hour === '*') {
    const n = Number(minStep[1]);
    return { text: n === 1 ? '每分钟' : `每 ${n} 分钟`, isInterval: true };
  }

  if (minStep && hourRange) {
    const n = Number(minStep[1]);
    return {
      text: `${pad2(Number(hourRange[1]))}:00–${pad2(Number(hourRange[2]))}:59 每 ${n} 分钟`,
      isInterval: false,
    };
  }

  if (isSingleMin && hour === '*') {
    const m = Number(min);
    return { text: m === 0 ? '每小时整点' : `每小时第 ${m} 分`, isInterval: true };
  }

  if (isSingleMin && hourStep) {
    const m = Number(min);
    const n = Number(hourStep[1]);
    if (m === 0) return { text: n === 1 ? '每小时' : `每 ${n} 小时`, isInterval: true };
    return { text: `每 ${n} 小时（第 ${m} 分）`, isInterval: true };
  }

  if (isSingleMin && hourRange) {
    const m = Number(min);
    const suffix = pad2(m);
    return {
      text: `${pad2(Number(hourRange[1]))}:${suffix}–${pad2(Number(hourRange[2]))}:${suffix} 每小时`,
      isInterval: false,
    };
  }

  if (isSingleMin && hourRangeStep) {
    const m = Number(min);
    const suffix = pad2(m);
    return {
      text: `${pad2(Number(hourRangeStep[1]))}:${suffix}–${pad2(Number(hourRangeStep[2]))}:${suffix} 每 ${Number(hourRangeStep[3])} 小时`,
      isInterval: false,
    };
  }

  if (isSingleMin && isSingleHour) {
    return { text: `${pad2(Number(hour))}:${pad2(Number(min))}`, isInterval: false };
  }

  if (isSingleMin && /^\d+(,\d+)+$/.test(hour)) {
    const hours = hour.split(',').map(Number);
    if (hours.length <= 4) {
      const m = pad2(Number(min));
      return { text: hours.map(h => `${pad2(h)}:${m}`).join('、'), isInterval: false };
    }
  }

  if (/^\d+(,\d+)+$/.test(min) && hour === '*') {
    const mins = min.split(',').map(Number);
    if (mins.length === 2 && mins[0] === 0 && mins[1] === 30) {
      return { text: '每 30 分钟', isInterval: true };
    }
    if (mins.length <= 4) {
      return { text: `每小时第 ${mins.join('、')} 分`, isInterval: true };
    }
  }

  if (/^\d+(,\d+)+$/.test(min) && isSingleHour) {
    const mins = min.split(',').map(Number);
    if (mins.length <= 4) {
      const h = pad2(Number(hour));
      return { text: mins.map(m => `${h}:${pad2(m)}`).join('、'), isInterval: false };
    }
  }

  return null;
}

function describeCron(cron: string): { label: string; valid: boolean } {
  const trimmed = cron.trim();
  const parts = trimmed.split(/\s+/);
  if (parts.length !== 5) return { label: '表达式格式无效', valid: false };
  const [min, hour, dom, month, dow] = parts as [string, string, string, string, string];
  const valid = validateCronField(min, 0, 59)
    && validateCronField(hour, 0, 23)
    && validateCronField(dom, 1, 31)
    && validateCronField(month, 1, 12, MONTH_ALIASES)
    && validateCronField(dow, 0, 7, DOW_ALIASES);
  if (!valid) return { label: '表达式格式无效', valid: false };

  const timePart = formatTimePart(min, hour);
  if (!timePart) return { label: '自定义周期', valid: true };

  if (dom === '*' && month === '*') {
    if (dow === '*') {
      return { label: timePart.isInterval ? timePart.text : `每天 ${timePart.text}`, valid: true };
    }
    const dowLabel = formatDowLabel(dow);
    if (dowLabel) return { label: `${dowLabel} ${timePart.text}`, valid: true };
  }

  if (month === '*' && dow === '*') {
    if (/^\d+$/.test(dom)) return { label: `每月 ${Number(dom)} 日 ${timePart.text}`, valid: true };
    if (/^\d+(,\d+)+$/.test(dom)) {
      const days = dom.split(',').map(Number);
      if (days.length <= 4) return { label: `每月 ${days.join('、')} 日 ${timePart.text}`, valid: true };
    }
    const domStep = /^\*\/([1-9]\d*)$/.exec(dom);
    if (domStep) return { label: `每 ${Number(domStep[1])} 天 ${timePart.text}`, valid: true };
  }

  if (/^\d+$/.test(month) && /^\d+$/.test(dom) && dow === '*') {
    return { label: `每年 ${Number(month)} 月 ${Number(dom)} 日 ${timePart.text}`, valid: true };
  }

  const monthStep = /^\*\/([1-9]\d*)$/.exec(month);
  if (monthStep && /^\d+$/.test(dom) && dow === '*') {
    return { label: `每 ${Number(monthStep[1])} 个月 ${Number(dom)} 日 ${timePart.text}`, valid: true };
  }

  return { label: '自定义周期', valid: true };
}

function displayTime(value: string, withSeconds = false): string {
  if (!value) return '—';
  const utc = /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`;
  const date = new Date(utc);
  if (Number.isNaN(date.getTime())) return value;
  const formatted = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    ...(withSeconds ? { second: '2-digit' } : {}),
    hour12: false,
  }).format(date);
  return formatted.replace(/\//g, '-');
}

onMounted(() => {
  void reload();
});
</script>

<style scoped>
.schedule-heading-lead {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
}

.owner-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border: 1px solid var(--astra-border);
  border-radius: 6px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-muted);
  font-family: var(--astra-font-mono);
  font-size: 12px;
  line-height: 18px;
}

.schedule-name-cell {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.schedule-title-btn {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--astra-text-primary);
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.4;
  text-align: left;
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.schedule-title-btn:hover {
  color: var(--astra-accent-text);
  text-decoration: underline;
}

.schedule-title-btn.is-paused {
  color: var(--astra-text-secondary);
}

.schedule-excerpt {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--astra-text-muted);
  font-size: 12px;
  line-height: 1.4;
}

.schedule-emp {
  color: var(--astra-text-secondary);
  font-size: 13px;
  font-weight: 500;
}

.schedule-rule {
  display: grid;
  gap: 4px;
  min-width: 0;
}

.schedule-rule__human {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--astra-text-primary);
  font-size: 13px;
  font-weight: 500;
  line-height: 1.35;
}

.schedule-rule__human.is-paused {
  color: var(--astra-text-muted);
}

.status-dot {
  width: 6px;
  height: 6px;
  flex: none;
  border-radius: 50%;
}

.status-dot.is-on {
  background: var(--astra-success);
}

.status-dot.is-off {
  background: var(--astra-neutral);
}

.schedule-rule__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.cron-chip {
  display: inline-block;
  padding: 1px 6px;
  border: 1px solid var(--astra-border);
  border-radius: 5px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 11.5px;
  line-height: 17px;
  white-space: nowrap;
}

.tz-label {
  color: var(--astra-text-muted);
  font-family: var(--astra-font-mono);
  font-size: 11.5px;
  white-space: nowrap;
}

.mono-time {
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.mono-inline {
  font-family: var(--astra-font-mono);
  font-weight: 500;
}

.schedule-row-actions {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  white-space: nowrap;
}

.schedule-row-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.danger-item {
  color: var(--astra-danger);
}

.empty-stack {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}

/* ── 新建 / 编辑抽屉表单 ──────────────────────────────────────── */
.schedule-drawer-form {
  display: flex;
  flex-direction: column;
}

.section-cap {
  margin: 4px 0 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--astra-border-subtle);
  color: var(--astra-text-muted);
  font-size: 12px;
  font-weight: 600;
}

.schedule-drawer-form .section-cap:not(:first-child) {
  margin-top: 12px;
}

.form-pair {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 0 12px;
}

.inline-pair {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 8px;
}

.preset-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}

.preset-pill {
  display: inline-flex;
  align-items: center;
  height: 26px;
  padding: 0 10px;
  border: 1px solid var(--astra-border);
  border-radius: 999px;
  background: var(--astra-bg-panel);
  color: var(--astra-text-secondary);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease, color 0.15s ease;
}

.preset-pill:hover:not(:disabled) {
  border-color: var(--astra-border-hover);
  background: var(--astra-bg-subtle);
  color: var(--astra-text-primary);
}

.preset-pill.is-active {
  border-color: var(--astra-primary);
  background: var(--astra-primary);
  color: #ffffff;
}

.preset-pill:disabled {
  opacity: 0.6;
  cursor: default;
}

.mono-input :deep(.el-input__inner) {
  font-family: var(--astra-font-mono);
}

.cron-preview {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 12px;
  margin-bottom: 12px;
  padding: 9px 12px;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
}

.cron-preview.is-invalid {
  border-color: color-mix(in srgb, var(--astra-danger) 40%, transparent);
  background: var(--astra-danger-soft);
}

.cron-preview__lead {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

.cron-preview__icon {
  flex: none;
  color: var(--astra-text-muted);
}

.cron-preview.is-invalid .cron-preview__icon,
.cron-preview.is-invalid .cron-preview__label {
  color: var(--astra-danger);
}

.cron-preview__label {
  color: var(--astra-text-primary);
  font-size: 13px;
  font-weight: 600;
}

.cron-preview__meta {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.prompt-box {
  display: grid;
  width: 100%;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  overflow: hidden;
  background: var(--astra-bg-panel);
}

.prompt-box__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--astra-border);
  background: var(--astra-bg-subtle);
}

.prompt-box__count {
  color: var(--astra-text-muted);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.prompt-box__input :deep(.el-textarea__inner) {
  border: 0;
  border-radius: 0;
  box-shadow: none !important;
  padding: 10px 12px;
  font-family: var(--astra-font-mono);
  font-size: 12.5px;
  line-height: 1.6;
}

.prompt-box__preview {
  max-height: 240px;
  overflow-y: auto;
  padding: 12px 14px;
}

.context-mode-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 14px;
}

.context-mode-hint {
  color: var(--astra-text-muted);
  font-size: 12.5px;
}

.drawer-error {
  margin-bottom: 12px;
  text-align: left;
}

.drawer-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}

.drawer-footer__right {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* ── 执行记录与详情抽屉 ───────────────────────────────────────── */
.detail-stack {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.detail-hero {
  display: grid;
  gap: 8px;
  padding: 14px 16px;
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-subtle);
}

.detail-hero__top {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.detail-hero__title {
  margin: 0;
  color: var(--astra-text-primary);
  font-size: 16px;
  font-weight: 600;
  line-height: 1.35;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.detail-hero__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 16px;
  color: var(--astra-text-muted);
  font-size: 12.5px;
}

.detail-hero__meta b {
  color: var(--astra-text-secondary);
  font-weight: 600;
}

.detail-hero__rule {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.detail-card {
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-panel);
  overflow: hidden;
}

.detail-card__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 12px;
  padding: 9px 14px;
  border-bottom: 1px solid var(--astra-border-subtle);
  background: var(--astra-bg-subtle);
}

.detail-card__title-group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.detail-card__title {
  color: var(--astra-text-primary);
  font-size: 13px;
  font-weight: 600;
}

.detail-card__sub {
  color: var(--astra-text-muted);
  font-size: 12px;
}

.detail-card__actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

.detail-card__body {
  padding: 12px 14px;
}

.detail-card__body--scroll {
  max-height: 260px;
  overflow-y: auto;
}

.detail-card__table-wrap {
  padding: 0 0 4px;
}

.detail-card__foot {
  display: flex;
  justify-content: flex-end;
  padding: 8px 14px 6px;
  border-top: 1px solid var(--astra-border-subtle);
}

.detail-alert {
  margin: 10px 14px;
}

.detail-body-alert {
  margin-bottom: 12px;
}

.count-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 20px;
  height: 18px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--astra-bg-elevated);
  color: var(--astra-text-secondary);
  font-size: 11.5px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.session-id-chip {
  display: inline-block;
  padding: 1px 6px;
  border: 1px solid var(--astra-border);
  border-radius: 5px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 11.5px;
  line-height: 17px;
}

.message-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.message-card {
  border: 1px solid var(--astra-border);
  border-radius: 9px;
  background: var(--astra-bg-panel);
  overflow: hidden;
}

.message-card.is-trigger {
  background: var(--astra-bg-subtle);
}

.message-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 12px;
  border-bottom: 1px solid var(--astra-border-subtle);
  background: var(--astra-bg-subtle);
}

.message-card.is-trigger .message-card__head {
  background: var(--astra-bg-elevated);
}

.message-card__author {
  color: var(--astra-text-primary);
  font-size: 12.5px;
  font-weight: 600;
}

.message-card__body {
  padding: 10px 14px;
}

.detail-footer {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}

.detail-footer__spacer {
  flex: 1;
}

@media (max-width: 640px) {
  .form-pair,
  .inline-pair {
    grid-template-columns: 1fr;
  }
}
</style>
