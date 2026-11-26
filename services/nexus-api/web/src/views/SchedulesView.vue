<!-- 依据 AgentScope v2.0.10 官方 schedule 表单、列表与执行详情语义适配，
     并扩展月历 / 周历 Cron 规则展开与当日排程侧栏。
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
          <span v-if="!loading && total > 0" class="total-chip">
            共 {{ total }} 项
          </span>
        </div>

        <div class="admin-card__actions schedule-card-actions">
          <div class="view-switcher" role="tablist" aria-label="日程展示方式" data-testid="schedules-view-switcher">
            <button
              type="button"
              role="tab"
              class="view-switcher__btn"
              :class="{ 'is-active': viewMode === 'month' }"
              :aria-selected="viewMode === 'month'"
              data-testid="schedules-view-month"
              @click="switchView('month')"
            >
              <CalendarDays :size="14" aria-hidden="true" />
              <span>月历</span>
            </button>
            <button
              type="button"
              role="tab"
              class="view-switcher__btn"
              :class="{ 'is-active': viewMode === 'week' }"
              :aria-selected="viewMode === 'week'"
              data-testid="schedules-view-week"
              @click="switchView('week')"
            >
              <CalendarRange :size="14" aria-hidden="true" />
              <span>周历</span>
            </button>
            <button
              type="button"
              role="tab"
              class="view-switcher__btn"
              :class="{ 'is-active': viewMode === 'list' }"
              :aria-selected="viewMode === 'list'"
              data-testid="schedules-view-list"
              @click="switchView('list')"
            >
              <List :size="14" aria-hidden="true" />
              <span>列表</span>
            </button>
          </div>

          <ElButton :icon="RefreshCw" :loading="loading" data-testid="schedules-refresh" @click="reload">刷新</ElButton>
          <ElButton type="primary" :icon="Plus" data-testid="schedules-create" @click="openCreate">新建日程</ElButton>
        </div>
      </div>
    </template>

    <div class="admin-page admin-page--fill">
      <!-- ── 日历模式控制栏（月历 / 周历） ─────────────────────────── -->
      <div v-if="isCalendarView" class="calendar-toolbar" role="search" aria-label="日历导航与筛选">
        <div class="calendar-nav">
          <div class="calendar-nav__stepper" role="group" aria-label="切换日期范围">
            <ElButton
              class="nav-icon-btn"
              :aria-label="viewMode === 'month' ? '上个月' : '上一周'"
              @click="shiftPeriod(-1)"
            >
              <ChevronLeft :size="15" aria-hidden="true" />
            </ElButton>
            <ElButton data-testid="schedules-cal-today" @click="jumpToday">今天</ElButton>
            <ElButton
              class="nav-icon-btn"
              :aria-label="viewMode === 'month' ? '下个月' : '下一周'"
              @click="shiftPeriod(1)"
            >
              <ChevronRight :size="15" aria-hidden="true" />
            </ElButton>
          </div>

          <span class="calendar-nav__title" data-testid="schedules-cal-title">{{ periodTitle }}</span>
        </div>

        <div class="calendar-filters">
          <ElSelect
            v-model="employeeFilter"
            class="cal-filter-select"
            clearable
            filterable
            placeholder="员工：全部"
            aria-label="日程员工"
            @change="applySearch"
          >
            <ElOption v-for="employee in employeeOptions" :key="employee.id" :label="employee.name" :value="employee.id">
              <span class="emp-option">
                <i class="emp-option__dot" :style="{ background: employeeColor(employee.id) }" aria-hidden="true" />
                <span>{{ employee.name }}</span>
              </span>
            </ElOption>
          </ElSelect>
          <ElSelect
            v-model="statusFilter"
            class="cal-filter-status"
            clearable
            placeholder="状态：全部"
            aria-label="日程状态"
            @change="applySearch"
          >
            <ElOption label="已启用" value="enabled" />
            <ElOption label="已暂停" value="paused" />
          </ElSelect>
          <ElInput
            v-model="query"
            class="cal-filter-keyword"
            placeholder="搜索名称 / 指令"
            aria-label="搜索日程"
            clearable
            :prefix-icon="Search"
            @clear="applySearch"
            @keydown.enter.prevent="applySearch"
          />
          <ElButton v-if="hasFilters || query.trim()" :icon="RotateCcw" @click="resetFilters">重置</ElButton>
        </div>
      </div>

      <!-- ── 列表模式筛选栏（保留原有布局规范） ────────────────────── -->
      <div v-else class="admin-page__toolbar" role="search" aria-label="日程筛选">
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
            <ElOption v-for="employee in employeeOptions" :key="employee.id" :label="employee.name" :value="employee.id">
              <span class="emp-option">
                <i class="emp-option__dot" :style="{ background: employeeColor(employee.id) }" aria-hidden="true" />
                <span>{{ employee.name }}</span>
              </span>
            </ElOption>
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

      <!-- 错误或截断提示 -->
      <ElAlert v-if="error && activeDataCount > 0" :title="error" type="error" :closable="false" show-icon />
      <ElAlert
        v-if="isCalendarView && calendarTruncated"
        :title="`当前共有 ${total} 条日程，日历模式已展开前 ${CALENDAR_FETCH_LIMIT} 条；建议按员工筛选或切换至列表模式分页浏览。`"
        type="info"
        :closable="false"
        show-icon
      />
      <div v-if="isCalendarView && !loading && !error && hasFilters && calendarRows.length === 0" class="filter-empty-bar">
        <span>当前筛选条件下暂无匹配日程</span>
        <ElButton link type="primary" @click="resetFilters">清除筛选</ElButton>
      </div>

      <!-- ── 日历主体区（月历 / 周历 + 右侧当日排程侧栏） ────────────── -->
      <template v-if="isCalendarView">
        <div v-if="error && calendarRows.length === 0" class="calendar-error-panel">
          <ElEmpty :image-size="72" description="日程数据加载失败">
            <div class="empty-stack">
              <ElText type="danger">{{ error }}</ElText>
              <ElButton :icon="RefreshCw" @click="reload">重新加载</ElButton>
            </div>
          </ElEmpty>
        </div>

        <div
          v-else
          v-loading="loading"
          element-loading-text="正在加载日程排班"
          class="calendar-workspace"
        >
          <div class="calendar-workspace__main">
            <ScheduleCalendarMonth
              v-if="viewMode === 'month'"
              :cells="monthCells"
              :day-map="expandedDayMap"
              :selected-ymd="selectedYmd"
              :hovered-schedule-id="hoveredScheduleId"
              @select-date="selectDate"
              @inspect-date="inspectDate"
              @open-schedule="openHistory"
              @create-at="openCreateAt"
              @hover-schedule="hoveredScheduleId = $event"
            />
            <ScheduleCalendarWeek
              v-else
              ref="weekCalRef"
              :days="weekDays"
              :day-map="expandedDayMap"
              :selected-ymd="selectedYmd"
              :hovered-schedule-id="hoveredScheduleId"
              @select-date="selectDate"
              @open-schedule="openHistory"
              @create-at="openCreateAt"
              @hover-schedule="hoveredScheduleId = $event"
            />
          </div>

          <div ref="inspectorWrapRef" class="calendar-workspace__side" :class="{ 'is-pulsed': inspectorPulse }">
            <ScheduleDayInspector
              :selected-ymd="selectedYmd"
              :today-ymd="todayYmd"
              :items="selectedDayItems"
              :busy-id="busy"
              :hovered-schedule-id="hoveredScheduleId"
              :has-filters="hasFilters"
              @open-history="openHistory"
              @edit="openEdit"
              @toggle="toggle"
              @copy-cron="copyCron"
              @delete="remove"
              @create-at="openCreateAt"
              @hover-schedule="hoveredScheduleId = $event"
            />
          </div>
        </div>
      </template>

      <!-- ── 列表模式主体（完整保留 Element Plus 规范与分页） ────────── -->
      <template v-else>
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

            <ElTableColumn label="数字员工" width="148" show-overflow-tooltip>
              <template #default="{ row }">
                <span v-if="row.employee_id" class="schedule-emp">
                  <i class="emp-option__dot" :style="{ background: employeeColor(row.employee_id) }" aria-hidden="true" />
                  <span>{{ row.employee_name }}</span>
                </span>
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
      </template>
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
            <ElRadioButton v-for="option in FREQUENCIES" :key="option.value" :value="option.value">
              {{ option.label }}
            </ElRadioButton>
          </ElRadioGroup>
        </ElFormItem>

        <ElFormItem v-if="frequency === 'daily' || frequency === 'weekdays'" label="执行时间" class="time-single-item">
          <ElInput v-model="time" type="time" :disabled="saving" aria-label="执行时间" />
        </ElFormItem>

        <div v-else-if="frequency === 'weekly'" class="form-pair">
          <ElFormItem label="星期">
            <ElSelect v-model="weekday" aria-label="星期" style="width: 100%">
              <ElOption v-for="(day, index) in WEEKDAYS" :key="day" :label="day" :value="index" />
            </ElSelect>
          </ElFormItem>
          <ElFormItem label="执行时间">
            <ElInput v-model="time" type="time" :disabled="saving" aria-label="执行时间" />
          </ElFormItem>
        </div>

        <div v-else-if="frequency === 'monthly'" class="form-pair">
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

        <div class="cron-preview" :class="{ 'is-invalid': !effectiveCronMeta.valid }">
          <div class="cron-preview__top">
            <div class="cron-preview__lead">
              <Clock :size="14" class="cron-preview__icon" aria-hidden="true" />
              <span class="cron-preview__label">
                {{ effectiveCronMeta.valid ? effectiveCronMeta.label : '表达式无效，请输入 5 位 Cron（分 时 日 月 周）' }}
              </span>
            </div>
            <div class="cron-preview__meta">
              <code class="cron-chip">{{ effectiveCron || '—' }}</code>
            </div>
          </div>

          <div v-if="effectiveCronMeta.valid && formTriggerExamples.length" class="cron-preview__examples">
            <span class="cron-preview__examples-label">规则推演示例：</span>
            <div class="cron-preview__chips">
              <span
                v-for="example in formTriggerExamples"
                :key="`${example.ymd}-${example.time}`"
                class="trigger-example-chip"
              >
                {{ example.dateTimeLabel }}
              </span>
            </div>
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
              <ElTableColumn label="会话创建时间" min-width="180">
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
          <ElSwitch
            v-if="selected"
            size="small"
            :model-value="selected.data.enabled"
            :loading="busy === selected.id"
            :disabled="busy === selected.id"
            active-text="已启用"
            inactive-text="已暂停"
            aria-label="切换当前日程启用状态"
            @change="toggle(selected, Boolean($event))"
          />
          <span class="detail-footer__spacer" />
          <ElButton @click="historyOpen = false">关闭</ElButton>
          <ElButton v-if="selected" type="primary" :icon="Pencil" @click="editFromHistory">编辑日程</ElButton>
        </div>
      </template>
    </ElDrawer>
  </ElCard>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue';
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
import {
  CalendarDays,
  CalendarRange,
  ChevronLeft,
  ChevronRight,
  Clock,
  List,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  User,
} from 'lucide-vue-next';
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
import ScheduleCalendarMonth from '@/features/schedules/components/ScheduleCalendarMonth.vue';
import ScheduleCalendarWeek from '@/features/schedules/components/ScheduleCalendarWeek.vue';
import ScheduleDayInspector from '@/features/schedules/components/ScheduleDayInspector.vue';
import {
  addDaysYmd,
  buildMonthGrid,
  buildWeekDays,
  CRON_PRESETS,
  dayOfWeekForYmd,
  describeCron,
  EMPLOYEE_TONES,
  employeeToneIndex,
  expandSchedulesForRange,
  formatYmd,
  FREQUENCIES,
  pad2,
  parseYmd,
  previewNextTriggers,
  restoreFrequencyFromCron,
  todayYmdInTimezone,
  WEEKDAYS,
  type FrequencyMode,
} from '@/features/schedules/cron';
import type { Employee } from '@/types';

type ScheduleViewMode = 'month' | 'week' | 'list';

const DEFAULT_TIMEZONE = 'Asia/Shanghai';
const VIEW_STORAGE_KEY = 'nexus_schedules_view_mode';
const CALENDAR_FETCH_LIMIT = 500;
const CALENDAR_PAGE_SIZE = 100;

function readInitialViewMode(): ScheduleViewMode {
  try {
    const saved = localStorage.getItem(VIEW_STORAGE_KEY);
    if (saved === 'month' || saved === 'week' || saved === 'list') return saved;
  } catch {
    /* 忽略本地存储受限 */
  }
  return 'month';
}

const viewMode = ref<ScheduleViewMode>(readInitialViewMode());
const isCalendarView = computed(() => viewMode.value !== 'list');

const todayYmd = computed(() => todayYmdInTimezone(DEFAULT_TIMEZONE));
const anchorYmd = ref<string>(todayYmd.value);
const selectedYmd = ref<string>(todayYmd.value);
const hoveredScheduleId = ref('');

const rows = ref<EmployeeSchedule[]>([]);
const calendarRows = ref<EmployeeSchedule[]>([]);
const calendarTruncated = ref(false);
const choices = ref<Employee[]>([]);
const loading = ref(false);
const error = ref('');
const page = ref(1);
const pageSize = ref(20);
const total = ref(0);
const tableRef = ref<TableInstance>();
const weekCalRef = ref<InstanceType<typeof ScheduleCalendarWeek> | null>(null);
const inspectorWrapRef = ref<HTMLElement | null>(null);
const inspectorPulse = ref(false);
let inspectorPulseTimer = 0;
let pendingWeekFocusHour: number | null = null;
const busy = ref('');
const employeeOptions = ref<{ id: string; name: string }[]>([]);
const employeeFilter = ref('');
const statusFilter = ref('');
const query = ref('');
const appliedQuery = ref('');

let calendarDataFilterKey = '';
let listDataFilterKey = '';

const hasFilters = computed(() => Boolean(employeeFilter.value || statusFilter.value || appliedQuery.value));
const activeDataCount = computed(() => (isCalendarView.value ? calendarRows.value.length : rows.value.length));

function currentFilterKey(): string {
  return JSON.stringify({
    employee_id: employeeFilter.value,
    status: statusFilter.value,
    q: appliedQuery.value,
  });
}

function currentFiltersPayload() {
  return {
    employee_id: employeeFilter.value,
    enabled: statusFilter.value ? statusFilter.value === 'enabled' : undefined,
    q: appliedQuery.value,
  };
}

// ── 日历网格与展开计算 ────────────────────────────────────────────────────

const monthCells = computed(() => {
  const { year, month } = parseYmd(anchorYmd.value);
  return buildMonthGrid(year, month, todayYmd.value);
});

const weekDays = computed(() => buildWeekDays(anchorYmd.value, todayYmd.value));

const visibleRange = computed(() => {
  if (viewMode.value === 'week') {
    const days = weekDays.value;
    return { start: days[0]!.ymd, end: days[days.length - 1]!.ymd };
  }
  const cells = monthCells.value;
  return { start: cells[0]!.ymd, end: cells[cells.length - 1]!.ymd };
});

const periodTitle = computed(() => {
  if (viewMode.value === 'month') {
    const { year, month } = parseYmd(anchorYmd.value);
    return `${year} 年 ${month} 月`;
  }
  const start = parseYmd(weekDays.value[0]!.ymd);
  const end = parseYmd(weekDays.value[6]!.ymd);
  if (start.year !== end.year) {
    return `${start.year}年${start.month}月${start.day}日 – ${end.year}年${end.month}月${end.day}日`;
  }
  if (start.month !== end.month) {
    return `${start.year}年${start.month}月${start.day}日 – ${end.month}月${end.day}日`;
  }
  return `${start.year}年${start.month}月${start.day}日 – ${end.day}日`;
});

const expandedDayMap = computed(() => {
  if (!isCalendarView.value || !calendarRows.value.length) return new Map();
  return expandSchedulesForRange(
    calendarRows.value,
    visibleRange.value.start,
    visibleRange.value.end,
    DEFAULT_TIMEZONE,
  );
});

const selectedDayItems = computed(() => expandedDayMap.value.get(selectedYmd.value) ?? []);

function switchView(next: ScheduleViewMode) {
  if (next === viewMode.value) return;
  const wasCalendar = isCalendarView.value;
  viewMode.value = next;
  hoveredScheduleId.value = '';
  try {
    localStorage.setItem(VIEW_STORAGE_KEY, next);
  } catch {
    /* 忽略 */
  }
  // 从月切周时把锚点对齐当前选中日期，保持上下文连续
  if (next === 'week' || next === 'month') {
    anchorYmd.value = selectedYmd.value;
  }
  const key = currentFilterKey();
  if (next === 'list') {
    if (listDataFilterKey !== key) void reload();
  } else if (!wasCalendar && calendarDataFilterKey !== key) {
    void reload();
  }
}

function shiftPeriod(delta: number) {
  if (viewMode.value === 'month') {
    const { year, month } = parseYmd(anchorYmd.value);
    const nextDate = new Date(Date.UTC(year, month - 1 + delta, 1));
    const nextYmd = formatYmd(nextDate.getUTCFullYear(), nextDate.getUTCMonth() + 1, 1);
    anchorYmd.value = nextYmd;
    // 若本月包含今天则默认选中今天，否则选中月初 1 号
    const t = parseYmd(todayYmd.value);
    selectedYmd.value = t.year === nextDate.getUTCFullYear() && t.month === nextDate.getUTCMonth() + 1
      ? todayYmd.value
      : nextYmd;
  } else {
    anchorYmd.value = addDaysYmd(anchorYmd.value, delta * 7);
    selectedYmd.value = addDaysYmd(selectedYmd.value, delta * 7);
  }
}

function jumpToday() {
  anchorYmd.value = todayYmd.value;
  selectedYmd.value = todayYmd.value;
}

function selectDate(ymd: string) {
  selectedYmd.value = ymd;
  // 点击月历中上下月灰显日期时平滑切换到对应月
  if (viewMode.value === 'month') {
    const cur = parseYmd(anchorYmd.value);
    const clicked = parseYmd(ymd);
    if (cur.year !== clicked.year || cur.month !== clicked.month) {
      anchorYmd.value = formatYmd(clicked.year, clicked.month, 1);
    }
  }
}

function inspectDate(ymd: string) {
  selectDate(ymd);
  inspectorPulse.value = false;
  window.clearTimeout(inspectorPulseTimer);
  void nextTick(() => {
    inspectorPulse.value = true;
    inspectorPulseTimer = window.setTimeout(() => {
      inspectorPulse.value = false;
    }, 450);
    if (window.innerWidth <= 900) {
      inspectorWrapRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  });
}

function employeeColor(id: string | null | undefined): string {
  const tone = EMPLOYEE_TONES[employeeToneIndex(id)] ?? EMPLOYEE_TONES[0]!;
  return tone.accent;
}

// ── 筛选与数据加载 ────────────────────────────────────────────────────────

function applySearch() {
  appliedQuery.value = query.value.trim();
  page.value = 1;
  calendarDataFilterKey = '';
  listDataFilterKey = '';
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

function invalidateCaches() {
  calendarDataFilterKey = '';
  listDataFilterKey = '';
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

const formTriggerExamples = computed(() => {
  if (!formOpen.value || !effectiveCronMeta.value.valid) return [];
  return previewNextTriggers(
    effectiveCron.value,
    form.timezone || DEFAULT_TIMEZONE,
    DEFAULT_TIMEZONE,
    4,
  );
});

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
  applyRestoredFrequency(cron);
}

function applyRestoredFrequency(cron: string) {
  const restored = restoreFrequencyFromCron(cron);
  frequency.value = restored.frequency;
  time.value = restored.time;
  weekday.value = restored.weekday;
  monthDay.value = restored.monthDay;
}

async function reload() {
  const generation = ++listGeneration;
  const savedWeekScroll = viewMode.value === 'week' ? weekCalRef.value?.captureScrollTop() ?? null : null;
  loading.value = true;
  error.value = '';
  const filters = currentFiltersPayload();
  const filterKey = currentFilterKey();

  try {
    if (isCalendarView.value) {
      const first = await listSchedules(1, CALENDAR_PAGE_SIZE, filters);
      if (generation !== listGeneration) return;
      const collected = [...first.items];
      const maxPages = Math.min(Math.ceil(first.total / CALENDAR_PAGE_SIZE), Math.ceil(CALENDAR_FETCH_LIMIT / CALENDAR_PAGE_SIZE));
      for (let p = 2; p <= maxPages; p++) {
        const next = await listSchedules(p, CALENDAR_PAGE_SIZE, filters);
        if (generation !== listGeneration) return;
        collected.push(...next.items);
      }
      calendarRows.value = collected.slice(0, CALENDAR_FETCH_LIMIT);
      calendarTruncated.value = first.total > CALENDAR_FETCH_LIMIT;
      total.value = first.total;
      employeeOptions.value = first.employee_options;
      calendarDataFilterKey = filterKey;
      syncSelectedFrom(calendarRows.value);
      if (viewMode.value === 'week') {
        const focusHour = pendingWeekFocusHour;
        pendingWeekFocusHour = null;
        await nextTick();
        if (focusHour !== null) {
          weekCalRef.value?.scrollToHour(focusHour);
        } else if (savedWeekScroll !== null && savedWeekScroll > 0) {
          weekCalRef.value?.restoreScrollTop(savedWeekScroll);
        }
      }
    } else {
      const result = await listSchedules(page.value, pageSize.value, filters);
      if (generation !== listGeneration) return;
      rows.value = result.items;
      total.value = result.total;
      employeeOptions.value = result.employee_options;
      listDataFilterKey = filterKey;
      syncSelectedFrom(rows.value);
      tableRef.value?.setScrollTop(0);
      if (!rows.value.length && page.value > 1) {
        page.value--;
        await reload();
      }
    }
  } catch (e) {
    if (generation === listGeneration) {
      error.value = reason(e);
      if (isCalendarView.value) {
        calendarRows.value = [];
        calendarTruncated.value = false;
      } else {
        rows.value = [];
      }
      total.value = 0;
    }
  } finally {
    if (generation === listGeneration) loading.value = false;
  }
}

function syncSelectedFrom(items: EmployeeSchedule[]) {
  if (!selected.value) return;
  const fresh = items.find(item => item.id === selected.value?.id);
  if (fresh) selected.value = fresh;
}

async function loadChoices() {
  choices.value = await listEmployees();
}

async function openCreate() {
  editing.value = null;
  employeeId.value = employeeFilter.value || '';
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

async function openCreateAt(payload: { ymd: string; hour?: number }) {
  editing.value = null;
  employeeId.value = employeeFilter.value || '';
  const dow = dayOfWeekForYmd(payload.ymd);
  const { day } = parseYmd(payload.ymd);
  const hourStr = payload.hour !== undefined ? `${pad2(payload.hour)}:00` : '09:00';
  Object.assign(form, defaults());
  frequency.value = 'weekly';
  weekday.value = dow;
  monthDay.value = day;
  time.value = hourStr;
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
  applyRestoredFrequency(row.data.cron_expression);
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
    if (viewMode.value === 'week' && frequency.value !== 'custom' && /^\d{2}:\d{2}$/.test(time.value)) {
      pendingWeekFocusHour = Number(time.value.split(':')[0]);
    }
    ElMessage.success(editing.value ? '日程已更新' : '日程已创建');
    invalidateCaches();
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
    invalidateCaches();
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
    if (selected.value?.id === row.id) {
      historyOpen.value = false;
      selected.value = null;
    }
    ElMessage.success('日程已删除');
    invalidateCaches();
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

onBeforeUnmount(() => {
  window.clearTimeout(inspectorPulseTimer);
});
</script>

<style scoped>
.schedule-heading-lead {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 10px;
}

.owner-chip,
.total-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border: 1px solid var(--astra-border);
  border-radius: 6px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-muted);
  font-size: 12px;
  line-height: 18px;
}

.owner-chip {
  font-family: var(--astra-font-mono);
}

.total-chip {
  font-variant-numeric: tabular-nums;
}

/* ── 顶栏操作区窄屏安全折行 ────────────────────────────────────── */
.schedule-card-actions {
  flex-wrap: wrap;
  max-width: 100%;
}

/* ── 三态分段视图切换器（月历 / 周历 / 列表） ────────────────── */
.view-switcher {
  display: inline-flex;
  align-items: center;
  padding: 2px;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
  gap: 2px;
}

.view-switcher__btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 26px;
  padding: 0 10px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--astra-text-secondary);
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  transition: background-color 0.14s ease, color 0.14s ease, box-shadow 0.14s ease;
}

.view-switcher__btn:hover {
  color: var(--astra-text-primary);
}

.view-switcher__btn.is-active {
  background: var(--astra-bg-panel);
  color: var(--astra-text-primary);
  font-weight: 600;
  box-shadow: 0 1px 2px rgba(9, 9, 11, 0.08);
}

/* ── 日历导航与筛选工具栏 ─────────────────────────────────────── */
.calendar-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 12px;
}

.calendar-nav {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

.calendar-nav__stepper {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.calendar-nav__stepper :deep(.el-button + .el-button) {
  margin-left: 0;
}

.nav-icon-btn {
  padding: 0 8px !important;
}

.calendar-nav__title {
  color: var(--astra-text-primary);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.01em;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.calendar-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}

.calendar-filters :deep(.el-button + .el-button) {
  margin-left: 0;
}

.cal-filter-select {
  width: 146px;
}

.cal-filter-status {
  width: 118px;
}

.cal-filter-keyword {
  width: 184px;
}

.emp-option {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.emp-option__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
}

.filter-empty-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 12px;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-size: 12.5px;
}

/* ── 日历主体工作区（主网格 + 右侧当日排程侧栏） ─────────────── */
.calendar-workspace {
  flex: 1 1 auto;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 332px;
  gap: 12px;
  overflow: hidden;
}

.calendar-workspace__main,
.calendar-workspace__side {
  min-width: 0;
  min-height: 0;
  border-radius: 10px;
  transition: box-shadow 0.25s ease;
}

.calendar-workspace__side.is-pulsed {
  box-shadow: 0 0 0 2px var(--astra-primary);
}

.calendar-error-panel {
  flex: 1 1 auto;
  display: grid;
  place-items: center;
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-panel);
}

/* ── 列表表格单元格样式 ──────────────────────────────────────── */
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
  display: inline-flex;
  align-items: center;
  gap: 6px;
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

.mono-time {
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
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

.time-single-item {
  max-width: 240px;
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
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
  padding: 9px 12px;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
}

.cron-preview__top {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 12px;
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

.cron-preview__examples {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding-top: 7px;
  border-top: 1px solid var(--astra-border-subtle);
}

.cron-preview__examples-label {
  color: var(--astra-text-muted);
  font-size: 11.5px;
  flex: none;
}

.cron-preview__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.trigger-example-chip {
  padding: 1px 6px;
  border: 1px solid var(--astra-border);
  border-radius: 5px;
  background: var(--astra-bg-panel);
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
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

@media (max-width: 1120px) {
  .calendar-workspace {
    grid-template-columns: minmax(0, 1fr) 292px;
  }
}

@media (max-width: 900px) {
  .calendar-filters {
    margin-left: 0;
  }
  .calendar-workspace {
    grid-template-columns: 1fr;
    overflow: visible;
  }
  .calendar-workspace__main {
    min-height: 500px;
  }
  .calendar-workspace__side {
    min-height: 320px;
  }
}

@media (max-width: 640px) {
  .form-pair {
    grid-template-columns: 1fr;
  }
  .time-single-item {
    max-width: none;
  }
  .cal-filter-select,
  .cal-filter-status,
  .cal-filter-keyword {
    width: 100%;
  }
  .admin-page__search {
    flex-wrap: wrap;
    width: 100%;
  }
  .admin-page__keyword {
    flex: 1 1 160px;
    width: auto;
  }
}
</style>
