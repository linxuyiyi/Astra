<template>
  <section class="admin-page admin-page--fill" data-testid="employees-page">
    <SettingsHeaderActions>
      <ElButton :icon="RefreshCw" :loading="loading" data-testid="employees-refresh" @click="reload">刷新</ElButton>
      <ElButton type="primary" :icon="Plus" data-testid="employees-create" @click="openCreate">新建数字员工</ElButton>
    </SettingsHeaderActions>

    <ElAlert v-if="loadError" type="error" show-icon :closable="false" :title="loadError" data-testid="employees-error">
      <ElButton type="primary" data-testid="employees-retry" @click="reload">重试</ElButton>
    </ElAlert>

    <div class="admin-page__toolbar" role="search" aria-label="数字员工筛选">
      <div class="admin-page__filters">
        <ElSelect
          v-model="statusFilter"
          class="admin-page__select"
          placeholder="状态：全部"
          aria-label="启用状态"
          clearable
          data-testid="employees-filter-status"
        >
          <ElOption value="enabled" label="已启用" />
          <ElOption value="disabled" label="已禁用" />
        </ElSelect>
      </div>
      <div class="admin-page__search">
        <ElInput
          v-model="query"
          class="admin-page__keyword"
          placeholder="搜索员工姓名 / 职责 / 描述"
          aria-label="搜索数字员工"
          clearable
          :prefix-icon="Search"
          data-testid="employees-filter-keyword"
          @keydown.enter.prevent="applySearch"
        />
        <ElButton type="primary" :icon="Search" data-testid="employees-search" @click="applySearch">搜索</ElButton>
      </div>
    </div>

    <div class="admin-page__table">
      <ElTable
        ref="tableRef"
        v-loading="loading"
        :data="paged"
        row-key="id"
        height="100%"
        element-loading-text="正在加载员工列表"
        data-testid="employees-table"
      >
        <ElTableColumn label="数字员工" min-width="170">
          <template #default="{ row }">
            <div class="person-cell">
              <EmployeeAvatar :avatar="row.avatar" :size="36" :disabled="!row.enabled" />
              <div class="person-name">
                <button type="button" class="person-link" :title="`${row.name} (${row.id})`" @click="openView(row as Employee)">{{ row.name }}</button>
                <small class="person-domain" :title="row.domain">{{ row.domain }}</small>
              </div>
            </div>
          </template>
        </ElTableColumn>

        <ElTableColumn label="职责说明" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="desc-text">{{ row.description || '—' }}</span>
          </template>
        </ElTableColumn>

        <ElTableColumn label="模型路由" width="140" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="routeModelName(row.model_route_id)" class="route-chip" :title="routeModelName(row.model_route_id)">{{ routeModelName(row.model_route_id) }}</span>
            <span v-else class="muted">未绑定</span>
          </template>
        </ElTableColumn>

        <ElTableColumn label="能力配置" min-width="160">
          <template #default="{ row }">
            <div v-if="hasAnyCapability(row as Employee)" class="cap-summary">
              <ElTooltip v-if="row.skills.length" placement="top" :content="`技能：${row.skills.map((s: string) => resourceLabel(catalogs.skills, s)).join('、')}`">
                <span class="cap-pill">技能 <b>{{ row.skills.length }}</b></span>
              </ElTooltip>
              <ElTooltip v-if="row.mcps.length" placement="top" :content="`MCP：${row.mcps.map((m: string) => resourceLabel(catalogs.mcps, m)).join('、')}`">
                <span class="cap-pill">MCP <b>{{ row.mcps.length }}</b></span>
              </ElTooltip>
              <ElTooltip v-if="row.tools.length" placement="top" :content="`工具：${row.tools.map((t: string) => toolLabel(t)).join('、')}`">
                <span class="cap-pill">工具 <b>{{ row.tools.length }}</b></span>
              </ElTooltip>
              <ElTooltip v-if="row.knowledge.length" placement="top" :content="`知识库：${row.knowledge.map((k: string) => resourceLabel(catalogs.knowledge, k)).join('、')}`">
                <span class="cap-pill">知识 <b>{{ row.knowledge.length }}</b></span>
              </ElTooltip>
            </div>
            <span v-else class="muted">未配置</span>
          </template>
        </ElTableColumn>

        <ElTableColumn label="同步" width="84" align="center">
          <template #default="{ row }">
            <ElTooltip :content="syncDetail(row as Employee)" placement="top">
              <ElTag
                size="small"
                :type="syncTagType(row as Employee)"
                :class="{ 'astra-tag--neutral': syncTagType(row as Employee) === 'info' }"
              >{{ syncLabel(row as Employee) }}</ElTag>
            </ElTooltip>
          </template>
        </ElTableColumn>

        <ElTableColumn label="启用" width="64" align="center">
          <template #default="{ row }">
            <ElSwitch
              size="small"
              :model-value="(row as Employee).enabled"
              :aria-label="`${(row as Employee).name} 启用`"
              @change="toggle(row as Employee, Boolean($event))"
            />
          </template>
        </ElTableColumn>

        <ElTableColumn label="操作" width="170" align="center" fixed="right">
          <template #default="{ row }">
            <div class="admin-actions">
              <ElButton link type="primary" @click="goChat((row as Employee).id)">对话</ElButton>
              <ElButton link type="primary" @click="openView(row as Employee)">档案</ElButton>
              <ElButton link type="primary" @click="openEdit(row as Employee)">编辑</ElButton>
              <ElDropdown trigger="click" @command="(cmd) => cmd === 'delete' && askDelete(row as Employee)">
                <ElButton link type="primary">更多</ElButton>
                <template #dropdown>
                  <ElDropdownMenu>
                    <ElDropdownItem command="delete" class="danger-item">删除员工</ElDropdownItem>
                  </ElDropdownMenu>
                </template>
              </ElDropdown>
            </div>
          </template>
        </ElTableColumn>

        <template #empty>
          <ElEmpty v-if="!loading" :image-size="64" description="暂无匹配员工" />
        </template>
      </ElTable>
    </div>

    <div class="admin-page__footer">
      <ElPagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50]"
        :total="filtered.length"
        layout="total, sizes, prev, pager, next"
        aria-label="数字员工分页"
        data-testid="employees-pagination"
      />
    </div>

    <!-- 查看档案详情 Drawer（工作指令 Markdown 渲染） -->
    <ElDrawer v-model="viewDrawerOpen" title="数字员工档案" size="min(740px, 100%)" :close-on-click-modal="true">
      <div v-if="viewing" class="view-profile">
        <div class="view-hero">
          <EmployeeAvatar :avatar="viewing.avatar" :size="58" :disabled="!viewing.enabled" />
          <div class="view-hero-text">
            <div class="view-hero-title">
              <h3>{{ viewing.name }}</h3>
              <ElTag
                size="small"
                :type="viewing.enabled ? 'success' : 'info'"
                :class="{ 'astra-tag--neutral': !viewing.enabled }"
              >{{ viewing.enabled ? "已启用" : "已禁用" }}</ElTag>
              <ElTooltip :content="syncDetail(viewing)" placement="top">
                <ElTag
                  size="small"
                  :type="syncTagType(viewing)"
                  :class="{ 'astra-tag--neutral': syncTagType(viewing) === 'info' }"
                >{{ syncLabel(viewing) }}</ElTag>
              </ElTooltip>
            </div>
            <p class="view-hero-domain">{{ viewing.domain }}</p>
            <div class="view-hero-meta">
              <span>模型路由：<b class="mono-inline">{{ routeModelName(viewing.model_route_id) || '未绑定' }}</b></span>
              <span v-if="viewing.agentscope_agent_id">Agent：<code class="mono-inline">{{ viewing.agentscope_agent_id }}</code></span>
            </div>
          </div>
        </div>

        <div class="view-card">
          <div class="view-card__head">职责说明</div>
          <p class="view-desc">{{ viewing.description || '—' }}</p>
        </div>

        <div class="view-card">
          <div class="view-card__head">工作指令</div>
          <div class="view-card__body">
            <MarkdownView
              :content="viewing.instructions || ''"
              compact
              allow-source-toggle
              allow-copy
              empty-text="尚未配置工作指令"
            />
          </div>
        </div>

        <div class="view-cap-grid">
          <div class="view-cap-box">
            <div class="view-cap-box__title">技能（{{ viewing.skills.length }}）</div>
            <div v-if="viewing.skills.length" class="chips">
              <span v-for="skill in viewing.skills" :key="skill" class="neutral-chip">{{ resourceLabel(catalogs.skills, skill) }}</span>
            </div>
            <span v-else class="muted">未配置</span>
          </div>
          <div class="view-cap-box">
            <div class="view-cap-box__title">MCP（{{ viewing.mcps.length }}）</div>
            <div v-if="viewing.mcps.length" class="chips">
              <span v-for="mcp in viewing.mcps" :key="mcp" class="neutral-chip">{{ resourceLabel(catalogs.mcps, mcp) }}</span>
            </div>
            <span v-else class="muted">未配置</span>
          </div>
          <div class="view-cap-box">
            <div class="view-cap-box__title">工具（{{ viewing.tools.length }}）</div>
            <div v-if="viewing.tools.length" class="chips">
              <span v-for="tool in viewing.tools" :key="tool" class="neutral-chip">{{ toolLabel(tool) }}</span>
            </div>
            <span v-else class="muted">未配置</span>
          </div>
          <div class="view-cap-box">
            <div class="view-cap-box__title">知识库（{{ viewing.knowledge.length }}）</div>
            <div v-if="viewing.knowledge.length" class="chips">
              <span v-for="item in viewing.knowledge" :key="item" class="neutral-chip">{{ resourceLabel(catalogs.knowledge, item) }}</span>
            </div>
            <span v-else class="muted">未配置</span>
          </div>
        </div>
      </div>
      <template #footer>
        <div class="view-footer">
          <ElButton v-if="viewing" @click="goChat(viewing.id)">发起对话</ElButton>
          <span class="view-footer__spacer" />
          <ElButton @click="viewDrawerOpen = false">关闭</ElButton>
          <ElButton type="primary" @click="editFromView">编辑员工</ElButton>
        </div>
      </template>
    </ElDrawer>

    <EmployeeDrawer
      :open="drawerOpen"
      :saving="saving"
      :error="saveError"
      :employee="editing"
      :catalogs="catalogs"
      :routes="routes"
      :route-error="routeError"
      @close="drawerOpen = false"
      @save="save"
      @reload-catalogs="reloadCatalogs"
    />

    <ElDialog v-model="deleteOpen" title="删除数字员工" width="440px">
      <p>确定删除 <b>{{ deleting?.name }}</b> 吗？删除后将不再显示该员工。</p>
      <template #footer>
        <ElButton @click="deleteOpen = false">取消</ElButton>
        <ElButton type="danger" @click="confirmDelete">确认删除</ElButton>
      </template>
    </ElDialog>
  </section>
</template>

<script setup lang="ts">
import { Plus, RefreshCw, Search } from "lucide-vue-next";
import { computed, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import {
  ElAlert,
  ElButton,
  ElDialog,
  ElDrawer,
  ElDropdown,
  ElDropdownItem,
  ElDropdownMenu,
  ElEmpty,
  ElInput,
  ElMessage,
  ElOption,
  ElPagination,
  ElSelect,
  ElSwitch,
  ElTable,
  ElTableColumn,
  ElTag,
  ElTooltip,
  vLoading,
} from "element-plus";
import type { TableInstance } from "element-plus";
import EmployeeDrawer from "../components/EmployeeDrawer.vue";
import EmployeeAvatar from "../components/EmployeeAvatar.vue";
import MarkdownView from "@/components/astra/MarkdownView.vue";
import SettingsHeaderActions from "./settings/SettingsHeaderActions.vue";
import { deleteEmployee, getKnowledge, getMcps, getSkills, getTools, listEmployees, saveEmployee } from "../api";
import { gatewayApi, type GatewayRoute } from "../api/gateway";
import type { Employee, ResourceCatalog } from "../types";
import { formatToolLabel } from "../utils/tools";

const router = useRouter();
const employees = ref<Employee[]>([]);
const query = ref("");
const statusFilter = ref("");
const page = ref(1);
const pageSize = ref(20);
const tableRef = ref<TableInstance>();
const loading = ref(false);
const loadError = ref("");

const drawerOpen = ref(false);
const saving = ref(false);
const saveError = ref("");
const editing = ref<Employee | null>(null);

const viewDrawerOpen = ref(false);
const viewing = ref<Employee | null>(null);

const deleteOpen = ref(false);
const deleting = ref<Employee | null>(null);

const emptyCatalog = (): ResourceCatalog => ({ available: false, items: [] });
const catalogs = ref({
  skills: emptyCatalog(),
  mcps: emptyCatalog(),
  knowledge: emptyCatalog(),
  tools: emptyCatalog(),
});
const routes = ref<GatewayRoute[]>([]);
const routeError = ref("");

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase();
  return employees.value.filter((item) => {
    if (statusFilter.value === "enabled" && !item.enabled) return false;
    if (statusFilter.value === "disabled" && item.enabled) return false;
    if (!needle) return true;
    return `${item.name} ${item.domain} ${item.description}`.toLowerCase().includes(needle);
  });
});

const paged = computed(() =>
  filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value),
);

watch([query, statusFilter], () => { page.value = 1; });
watch(pageSize, () => { page.value = 1; });
watch([page, pageSize], () => { tableRef.value?.setScrollTop(0); });
watch(filtered, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / pageSize.value));
  if (page.value > maxPage) page.value = maxPage;
});

function applySearch() {
  page.value = 1;
  if (!loading.value) void reload();
}

function resourceLabel(catalog: ResourceCatalog, id: string) {
  return catalog.items.find((item) => item.id === id)?.name ?? id;
}

function routeModelName(routeId?: string) {
  if (!routeId) return "";
  return routes.value.find((item) => item.id === routeId)?.model ?? routeId;
}

function hasAnyCapability(emp: Employee) {
  return Boolean(emp.skills.length || emp.mcps.length || emp.tools.length || emp.knowledge.length);
}

function toolLabel(id: string) {
  const item = catalogs.value.tools.items.find((t) => t.id === id);
  return formatToolLabel(id, item?.name);
}

function syncLabel(employee: Employee) {
  const status = employee.capability?.runtime_status;
  if (status === "applied") return "已同步";
  if (status === "error") return "异常";
  return "待核对";
}

function syncTagType(employee: Employee) {
  const status = employee.capability?.runtime_status;
  return status === "applied" ? "success" : status === "error" ? "danger" : "info";
}

function syncDetail(employee: Employee) {
  return employee.capability?.error || employee.capability?.checked_at || "尚未核对 AgentScope 运行环境";
}

async function reload() {
  loading.value = true;
  loadError.value = "";
  try {
    employees.value = await listEmployees();
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : "员工列表加载失败";
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  saveError.value = "";
  editing.value = null;
  drawerOpen.value = true;
}

function openEdit(employee: Employee) {
  saveError.value = "";
  editing.value = employee;
  drawerOpen.value = true;
}

function openView(employee: Employee) {
  viewing.value = employee;
  viewDrawerOpen.value = true;
}

function editFromView() {
  const target = viewing.value;
  viewDrawerOpen.value = false;
  if (target) openEdit(target);
}

function goChat(id: string) {
  void router.push({ path: "/chat", query: { employee: id } });
}

function askDelete(employee: Employee) {
  deleting.value = employee;
  deleteOpen.value = true;
}

async function save(body: Omit<Employee, "id" | "agentscope_agent_id" | "mailbox_session_id">) {
  if (saving.value) return;
  if (!body.name || !body.domain || !body.description || !body.instructions) {
    ElMessage.warning("请完善必填信息");
    return;
  }
  saving.value = true;
  saveError.value = "";
  const employeeId = editing.value?.id;
  try {
    const result = await saveEmployee(body, employeeId);
    drawerOpen.value = false;
    await reload();
    const sync = result.sync;
    if (sync && sync.status !== "applied") {
      ElMessage.warning(`员工已保存，资源同步${sync.status === "pending" ? "待重试" : "失败"}：${sync.detail || "请到技能/MCP 管理页重试"}`);
    } else {
      ElMessage.success(result.agentscope.ok ? "员工已保存，并同步到 AgentScope" : `员工已保存。${result.agentscope.detail}`);
    }
  } catch (error) {
    saveError.value = error instanceof Error ? error.message : "保存失败";
  } finally {
    saving.value = false;
  }
}

async function toggle(employee: Employee, enabled: boolean) {
  try {
    await saveEmployee({ ...employee, enabled }, employee.id);
    await reload();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "更新失败");
  }
}

async function confirmDelete() {
  if (!deleting.value) return;
  try {
    await deleteEmployee(deleting.value.id);
    deleteOpen.value = false;
    await reload();
    ElMessage.success("员工已删除");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "删除失败");
  }
}

async function reloadCatalogs() {
  const [skills, mcps, knowledge, tools] = await Promise.all([
    getSkills(),
    getMcps(),
    getKnowledge(),
    getTools(),
  ]);
  catalogs.value = { skills, mcps, knowledge, tools };
}

onMounted(async () => {
  await reloadCatalogs();
  try {
    routes.value = (await gatewayApi.routes()).filter((item) => item.enabled && item.model_type === "CHAT");
  } catch (error) {
    routeError.value = error instanceof Error ? error.message : "无法读取模型路由";
  }
  await reload();
});
</script>

<style scoped>
.person-cell { display: flex; align-items: center; gap: 10px; min-width: 0; }
.person-name { display: grid; gap: 1px; min-width: 0; }
.person-link {
  padding: 0; border: 0; background: transparent; cursor: pointer;
  color: var(--astra-text-primary); font-size: 13.5px; font-weight: 600;
  text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.person-link:hover { color: var(--astra-accent-text); text-decoration: underline; }
.person-domain { color: var(--astra-text-muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.desc-text { color: var(--astra-text-secondary); font-size: 13px; }
.route-chip {
  display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  padding: 1px 7px; border: 1px solid var(--astra-border); border-radius: 5px;
  background: var(--astra-bg-subtle); color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono); font-size: 12px; line-height: 18px;
}
.cap-summary { display: flex; flex-wrap: wrap; gap: 5px; }
.cap-pill {
  padding: 1px 7px; border: 1px solid var(--astra-border); border-radius: 5px;
  background: var(--astra-bg-subtle); color: var(--astra-text-muted);
  font-size: 12px; line-height: 18px; font-variant-numeric: tabular-nums; cursor: default;
}
.cap-pill b { color: var(--astra-text-primary); font-weight: 600; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.neutral-chip {
  padding: 2px 8px; border: 1px solid var(--astra-border); border-radius: 6px;
  background: var(--astra-bg-panel); color: var(--astra-text-secondary);
  font-size: 12px; line-height: 18px;
}
.muted { color: var(--astra-text-muted); font-size: 12.5px; }
.danger-item { color: var(--astra-danger); }

/* 详情档案 */
.view-profile { display: flex; flex-direction: column; gap: 14px; }
.view-hero {
  display: flex; align-items: center; gap: 14px;
  padding: 14px 16px;
  border: 1px solid var(--astra-border); border-radius: 10px; background: var(--astra-bg-subtle);
}
.view-hero-text { display: grid; gap: 4px; min-width: 0; }
.view-hero-title { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.view-hero-title h3 { margin: 0; color: var(--astra-text-primary); font-size: 17px; font-weight: 600; overflow-wrap: anywhere; word-break: break-word; }
.view-hero-domain { margin: 0; color: var(--astra-text-secondary); font-size: 13px; overflow-wrap: anywhere; word-break: break-word; }
.view-hero-meta { display: flex; flex-wrap: wrap; gap: 6px 14px; color: var(--astra-text-muted); font-size: 12px; }
.mono-inline { font-family: var(--astra-font-mono); color: var(--astra-text-secondary); font-weight: 500; }

.view-card { border: 1px solid var(--astra-border); border-radius: 10px; background: var(--astra-bg-panel); overflow: hidden; }
.view-card__head {
  padding: 9px 14px;
  border-bottom: 1px solid var(--astra-border-subtle);
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-size: 12.5px;
  font-weight: 600;
}
.view-card__body { padding: 12px 14px; }
.view-desc { margin: 0; padding: 12px 14px; color: var(--astra-text-primary); font-size: 13.5px; line-height: 1.65; white-space: pre-wrap; word-break: break-word; }

.view-cap-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.view-cap-box {
  display: grid; gap: 8px; align-content: start;
  padding: 12px 14px;
  border: 1px solid var(--astra-border); border-radius: 10px; background: var(--astra-bg-subtle);
}
.view-cap-box__title { color: var(--astra-text-secondary); font-size: 12px; font-weight: 600; }
.view-footer { display: flex; align-items: center; gap: 8px; width: 100%; }
.view-footer__spacer { flex: 1; }

@media (max-width: 640px) {
  .view-cap-grid { grid-template-columns: 1fr; }
}
</style>
