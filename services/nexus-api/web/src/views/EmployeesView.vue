<template>
  <section class="admin-page" data-testid="employees-page">
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

    <ElTable
      v-loading="loading"
      :data="paged"
      row-key="id"
      element-loading-text="正在加载员工列表"
      data-testid="employees-table"
    >
      <ElTableColumn label="数字员工" min-width="190">
        <template #default="{ row }">
          <div class="person-cell">
            <span class="avatar" :class="{ off: !row.enabled }">{{ row.avatar }}</span>
            <span class="person-name">
              <b>{{ row.name }}</b>
              <small class="person-id">{{ row.id }}</small>
            </span>
          </div>
        </template>
      </ElTableColumn>

      <ElTableColumn label="职责领域" min-width="150">
        <template #default="{ row }">
          <b>{{ row.domain }}</b>
        </template>
      </ElTableColumn>

      <ElTableColumn label="技能" min-width="160">
        <template #default="{ row }">
          <div class="chips">
            <ElTag v-for="skill in row.skills" :key="skill" size="small" type="success" class="mini-chip">
              {{ resourceLabel(catalogs.skills, skill) }}
            </ElTag>
            <span v-if="!row.skills.length" class="muted">未配置</span>
          </div>
        </template>
      </ElTableColumn>

      <ElTableColumn label="MCP" min-width="170">
        <template #default="{ row }">
          <div class="chips">
            <ElTag v-for="mcp in row.mcps" :key="mcp" size="small" type="warning" class="mini-chip">
              {{ resourceLabel(catalogs.mcps, mcp) }}
            </ElTag>
            <span v-if="!row.mcps.length" class="muted">未配置</span>
          </div>
        </template>
      </ElTableColumn>

      <ElTableColumn label="工具" min-width="220">
        <template #default="{ row }">
          <div class="chips">
            <ElTag
              v-for="tool in row.tools"
              :key="tool"
              size="small"
              :type="isMcpTool(tool) ? 'warning' : 'info'"
              class="mini-chip"
            >
              {{ toolLabel(tool) }}
            </ElTag>
            <span v-if="!row.tools.length" class="muted">未配置</span>
          </div>
        </template>
      </ElTableColumn>

      <ElTableColumn label="同步" width="110" align="center">
        <template #default="{ row }">
          <ElTooltip :content="syncDetail(row as Employee)" placement="top">
            <ElTag size="small" :type="syncTagType(row as Employee)">{{ syncLabel(row as Employee) }}</ElTag>
          </ElTooltip>
        </template>
      </ElTableColumn>

      <ElTableColumn label="状态" width="100" align="center">
        <template #default="{ row }">
          <ElSwitch
            :model-value="(row as Employee).enabled"
            :aria-label="`${(row as Employee).name} 启用`"
            @change="toggle(row as Employee, Boolean($event))"
          />
        </template>
      </ElTableColumn>

      <ElTableColumn label="操作" width="228" align="center">
        <template #default="{ row }">
          <div class="admin-actions">
            <ElButton link type="primary" @click="openView(row as Employee)">查看</ElButton>
            <ElButton link type="primary" @click="goChat((row as Employee).id)">对话</ElButton>
            <ElButton link type="primary" @click="openEdit(row as Employee)">编辑</ElButton>
            <ElButton link type="danger" @click="askDelete(row as Employee)">删除</ElButton>
          </div>
        </template>
      </ElTableColumn>

      <template #empty>
        <ElEmpty v-if="!loading" :image-size="64" description="没有找到符合条件的员工" />
      </template>
    </ElTable>

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

    <!-- 查看详情 Drawer -->
    <ElDrawer v-model="viewDrawerOpen" title="员工档案详情" size="520px" :close-on-click-modal="true">
      <div v-if="viewing" class="view-profile">
        <div class="view-hero">
          <span class="avatar" :class="{ off: !viewing.enabled }">{{ viewing.avatar }}</span>
          <div class="view-hero-text">
            <h3>{{ viewing.name }}</h3>
            <p class="muted">{{ viewing.domain }}</p>
            <ElTag size="small" :type="viewing.enabled ? 'success' : 'info'">
              {{ viewing.enabled ? "已启用" : "已禁用" }}
            </ElTag>
          </div>
        </div>

        <div class="view-section">
          <div class="view-label">职责说明</div>
          <p class="view-desc">{{ viewing.description }}</p>
        </div>

        <div class="view-section">
          <div class="view-label">工作指令</div>
          <pre class="view-instructions">{{ viewing.instructions }}</pre>
        </div>

        <div class="view-section">
          <div class="view-label">已配置技能</div>
          <div class="chips">
            <ElTag v-for="skill in viewing.skills" :key="skill" size="small" type="success">
              {{ resourceLabel(catalogs.skills, skill) }}
            </ElTag>
            <span v-if="!viewing.skills.length" class="muted">暂无</span>
          </div>
        </div>

        <div class="view-section">
          <div class="view-label">已绑定 MCP</div>
          <div class="chips">
            <ElTag v-for="mcp in viewing.mcps" :key="mcp" size="small" type="warning">
              {{ resourceLabel(catalogs.mcps, mcp) }}
            </ElTag>
            <span v-if="!viewing.mcps.length" class="muted">暂无</span>
          </div>
        </div>

        <div class="view-section">
          <div class="view-label">同步状态</div>
          <ElTag size="small" :type="syncTagType(viewing)">{{ syncLabel(viewing) }}</ElTag>
          <span class="muted">{{ syncDetail(viewing) }}</span>
        </div>

        <div class="view-section">
          <div class="view-label">已授权工具</div>
          <div class="chips">
            <ElTag
              v-for="tool in viewing.tools"
              :key="tool"
              size="small"
              :type="isMcpTool(tool) ? 'warning' : 'info'"
            >
              {{ toolLabel(tool) }}
            </ElTag>
            <span v-if="!viewing.tools.length" class="muted">暂无</span>
          </div>
        </div>

        <div class="view-section">
          <div class="view-label">知识资源</div>
          <div class="chips">
            <ElTag v-for="item in viewing.knowledge" :key="item" size="small" type="info">
              {{ resourceLabel(catalogs.knowledge, item) }}
            </ElTag>
            <span v-if="!viewing.knowledge.length" class="muted">暂无</span>
          </div>
        </div>

        <div class="view-section">
          <div class="view-label">Agent 标识</div>
          <div class="mono-box">{{ viewing.agentscope_agent_id || "尚未创建" }}</div>
        </div>
      </div>
      <template #footer>
        <div class="view-footer">
          <ElButton @click="viewDrawerOpen = false">关闭</ElButton>
          <ElButton type="primary" @click="editFromView">编辑员工</ElButton>
        </div>
      </template>
    </ElDrawer>

    <!-- 编辑/创建 Drawer -->
    <EmployeeDrawer
      :open="drawerOpen"
      :employee="editing"
      :catalogs="catalogs"
      :routes="routes"
      :route-error="routeError"
      @close="drawerOpen = false"
      @save="save"
      @reload-catalogs="reloadCatalogs"
    />

    <ElDialog v-model="deleteOpen" title="删除数字员工" width="460px">
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
} from "element-plus";
import EmployeeDrawer from "../components/EmployeeDrawer.vue";
import SettingsHeaderActions from "./settings/SettingsHeaderActions.vue";
import { deleteEmployee, getKnowledge, getMcps, getSkills, getTools, listEmployees, saveEmployee } from "../api";
import { gatewayApi, type GatewayRoute } from "../api/gateway";
import type { Employee, ResourceCatalog } from "../types";
import { formatToolLabel, isMcpTool } from "../utils/tools";

const router = useRouter();
const employees = ref<Employee[]>([]);
const query = ref("");
const statusFilter = ref("");
const page = ref(1);
const pageSize = ref(20);
const loading = ref(false);
const loadError = ref("");

const drawerOpen = ref(false);
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

watch([query, statusFilter], () => {
  page.value = 1;
});

watch(pageSize, () => {
  page.value = 1;
});

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

function toolLabel(id: string) {
  const item = catalogs.value.tools.items.find((t) => t.id === id);
  return formatToolLabel(id, item?.name);
}

function syncLabel(employee: Employee) {
  const status = employee.capability?.runtime_status;
  if (status === "applied") return "已同步";
  if (status === "error") return "同步异常";
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
  editing.value = null;
  drawerOpen.value = true;
}

function openEdit(employee: Employee) {
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
  if (!body.name || !body.domain || !body.description || !body.instructions) {
    ElMessage.warning("请完善必填信息");
    return;
  }
  try {
    const result = await saveEmployee(body, editing.value?.id);
    drawerOpen.value = false;
    await reload();
    const sync = result.sync;
    if (sync && sync.status !== "applied") {
      ElMessage.warning(`员工已保存，资源同步${sync.status === "pending" ? "待重试" : "失败"}：${sync.detail || "请到技能/MCP 管理页重试"}`);
    } else {
      ElMessage.success(result.agentscope.ok ? "员工已保存，并同步到 AgentScope" : `员工已保存。${result.agentscope.detail}`);
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存失败");
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
    routes.value = (await gatewayApi.routes()).filter((item) => item.enabled && item.model_type === 'CHAT');
  } catch (error) {
    routeError.value = error instanceof Error ? error.message : "无法读取模型路由";
  }
  await reload();
});
</script>

<style scoped>
/* 页面骨架、查询工具栏、表格与分页统一走 styles/page-layout.css，
   这里只保留表格单元格与详情抽屉内部的排版。 */
.person-cell { display: flex; align-items: center; gap: 10px; }
.person-name { display: flex; flex-direction: column; gap: 2px; }
.person-id { color: var(--astra-text-muted); font-size: 11px; }
.avatar { font-size: 20px; line-height: 1; }
.avatar.off { opacity: 0.4; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.mini-chip { font-size: 11px; }
.muted { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }

.view-profile { display: flex; flex-direction: column; gap: 16px; padding: 4px 8px; }
.view-hero { display: flex; align-items: center; gap: 14px; padding-bottom: 12px; border-bottom: 1px solid var(--astra-border); }
.view-hero .avatar { font-size: 36px; }
.view-hero-text h3 { margin: 0 0 4px; color: var(--astra-text-primary); font-size: 18px; }
.view-hero-text p { margin: 0 0 6px; }
.view-section { display: flex; flex-direction: column; gap: 6px; }
.view-label { font-size: 12px; font-weight: var(--astra-weight-bold); color: var(--astra-text-secondary); }
.view-desc { margin: 0; color: var(--astra-text-primary); line-height: 1.5; font-size: var(--astra-font-helper); }
.view-instructions { margin: 0; padding: 10px 12px; background: var(--astra-bg-elevated); border-radius: 8px; font-family: inherit; font-size: 12px; line-height: 1.6; white-space: pre-wrap; color: var(--astra-text-primary); }
.mono-box { font-family: var(--astra-font-mono, monospace); font-size: 12px; color: var(--astra-text-muted); background: var(--astra-bg-elevated); padding: 6px 10px; border-radius: 6px; }
.view-footer { display: flex; justify-content: flex-end; gap: 8px; }
</style>
