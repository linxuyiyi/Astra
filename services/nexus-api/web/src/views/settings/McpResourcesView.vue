<template>
  <section class="admin-page admin-page--fill" data-testid="mcp-resources-page">
    <SettingsHeaderActions>
      <ElButton :icon="RefreshCw" :loading="loading" data-testid="mcps-refresh" @click="load">刷新</ElButton>
      <ElButton type="primary" :icon="Plus" data-testid="mcps-create" @click="beginCreate">新增 MCP</ElButton>
    </SettingsHeaderActions>

    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="mcps-error">
      <ElButton type="primary" data-testid="mcps-retry" @click="load">重试</ElButton>
    </ElAlert>

    <div class="admin-page__toolbar" role="search" aria-label="MCP 筛选">
      <div class="admin-page__filters">
        <ElSelect v-model="sourceFilter" class="admin-page__select" aria-label="MCP 来源" placeholder="来源：全部" clearable>
          <ElOption value="nexus" label="Nexus 自建" />
          <ElOption value="agentscope" label="AgentScope" />
        </ElSelect>
        <ElSelect v-model="statusFilter" class="admin-page__select" aria-label="启用状态" placeholder="状态：全部" clearable>
          <ElOption value="enabled" label="已启用" />
          <ElOption value="disabled" label="已停用" />
        </ElSelect>
      </div>
      <div class="admin-page__search">
        <ElInput v-model="keyword" class="admin-page__keyword" aria-label="搜索 MCP" placeholder="搜索名称、说明或地址" clearable :prefix-icon="Search" />
      </div>
    </div>

    <div class="admin-page__table">
      <ElTable ref="tableRef" v-loading="loading" :data="paged" row-key="id" height="100%" aria-label="MCP 列表" element-loading-text="正在加载 MCP 列表" data-testid="mcps-table">
        <ElTableColumn label="名称 / 说明" min-width="200">
          <template #default="{ row }">
            <div class="name-cell">
              <button type="button" class="name-link" :title="row.name" @click="openDetail(row as McpResource)">{{ row.name }}</button>
              <small v-if="row.description" class="name-desc" :title="row.description">{{ row.description }}</small>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="连接地址" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.url" class="mono">{{ row.url }}</span>
            <span v-else class="muted">AgentScope 托管</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="关联员工" min-width="140">
          <template #default="{ row }">
            <div v-if="row.employees.length" class="chips">
              <span v-for="employee in row.employees.slice(0, 2)" :key="employee.id" class="emp-chip" :title="employee.name">{{ employee.name }}</span>
              <ElTooltip
                v-if="row.employees.length > 2"
                placement="top"
                :content="row.employees.map((e: { name: string }) => e.name).join('、')"
              >
                <span class="emp-more">+{{ row.employees.length - 2 }}</span>
              </ElTooltip>
            </div>
            <span v-else class="muted">未绑定</span>
          </template>
        </ElTableColumn>
      <ElTableColumn label="同步状态" width="128">
        <template #default="{ row }">
          <ElTooltip :content="combinedSyncTooltip(row as McpResource)" placement="top">
            <ElTag
              size="small"
              :type="syncTagType(combinedSyncStatus(row as McpResource))"
              :class="{ 'astra-tag--neutral': syncTagType(combinedSyncStatus(row as McpResource)) === 'info' }"
            >{{ syncLabel(combinedSyncStatus(row as McpResource)) }}</ElTag>
          </ElTooltip>
          <span v-if="row.sync.pending" class="sync-count">待 {{ row.sync.pending }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="启用" width="76" align="center">
        <template #default="{ row }">
          <ElSwitch
            v-if="row.editable"
            size="small"
            :model-value="row.enabled"
            :disabled="busyId === row.id"
            :aria-label="`${row.name} 启用状态`"
            @change="toggleEnabled(row as McpResource)"
          />
          <ElTag
            v-else
            size="small"
            :type="row.enabled ? 'success' : 'info'"
            :class="{ 'astra-tag--neutral': !row.enabled }"
          >{{ row.enabled ? '启用' : '停用' }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="操作" width="176" align="center" fixed="right">
        <template #default="{ row }">
          <div class="admin-actions">
            <ElButton link type="primary" @click="openDetail(row as McpResource)">查看</ElButton>
            <ElButton link type="primary" :disabled="busyId === row.id" @click="testSaved(row as McpResource)">测试</ElButton>
            <ElButton v-if="row.editable" link type="primary" @click="beginEdit(row as McpResource)">编辑</ElButton>
            <ElDropdown
              v-if="hasMoreActions(row as McpResource)"
              trigger="click"
              :disabled="busyId === row.id"
              @command="(cmd) => onRowCommand(String(cmd), row as McpResource)"
            >
              <ElButton link type="primary" :disabled="busyId === row.id">更多</ElButton>
              <template #dropdown>
                <ElDropdownMenu>
                  <ElDropdownItem v-if="row.editable || (row.employees.length && row.sync.status !== 'applied')" command="sync">
                    {{ row.editable ? '同步配置' : '重试同步' }}
                  </ElDropdownItem>
                  <ElDropdownItem v-if="row.editable" command="delete" divided class="danger-item">删除 MCP</ElDropdownItem>
                </ElDropdownMenu>
              </template>
            </ElDropdown>
          </div>
        </template>
      </ElTableColumn>
      <template #empty>
        <ElEmpty v-if="!loading" :image-size="64" :description="items.length ? '没有匹配的 MCP' : '暂无 MCP 资源'" />
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
        aria-label="MCP 分页"
        data-testid="mcps-pagination"
      />
    </div>

    <ElDrawer v-model="editorOpen" :title="editing ? '编辑 MCP' : '新增 MCP'" size="min(660px, 100%)" :close-on-click-modal="false" :before-close="closeEditor">
      <ElForm label-position="top" :disabled="saving" @submit.prevent="submitMcp">
        <div class="section-cap">基础与连接</div>
        <div class="pair">
          <ElFormItem label="名称" required>
            <ElInput v-model="form.name" maxlength="64" placeholder="例如：发布系统 MCP" />
          </ElFormItem>
          <ElFormItem label="传输方式">
            <ElSelect v-model="form.transport" style="width: 100%">
              <ElOption value="http" label="HTTP（Streamable）" />
            </ElSelect>
          </ElFormItem>
        </div>
        <ElFormItem label="连接地址" required>
          <ElInput v-model="form.url" placeholder="https://mcp.example.com/mcp" />
        </ElFormItem>
        <ElFormItem label="说明">
          <ElInput v-model="form.description" type="textarea" :rows="2" placeholder="可选，简述该 MCP 提供的工具能力" />
        </ElFormItem>

        <div class="section-cap">鉴权与请求配置</div>
        <div class="pair">
          <ElFormItem label="鉴权方式">
            <ElSelect v-model="form.auth_type" style="width: 100%">
              <ElOption value="none" label="无鉴权" />
              <ElOption value="bearer" label="Bearer Token" />
              <ElOption value="header" label="自定义 Header" />
            </ElSelect>
          </ElFormItem>
          <ElFormItem v-if="form.auth_type === 'header'" label="Header 名称">
            <ElInput v-model="form.auth_header" placeholder="X-Api-Key" />
          </ElFormItem>
          <ElFormItem v-else-if="form.auth_type === 'bearer'" :label="editing?.has_token ? 'Token（留空保持不变）' : 'Token'">
            <ElInput v-model="form.token" type="password" show-password autocomplete="new-password" :placeholder="editing?.has_token ? '已保存' : '输入 Token'" />
          </ElFormItem>
          <ElFormItem v-else label="超时时间（秒）">
            <ElInputNumber v-model="form.timeout" :min="1" :max="300" style="width: 100%" />
          </ElFormItem>
        </div>

        <div v-if="form.auth_type === 'header'" class="pair">
          <ElFormItem :label="editing?.has_token ? 'Token（留空保持不变）' : 'Token'">
            <ElInput v-model="form.token" type="password" show-password autocomplete="new-password" :placeholder="editing?.has_token ? '已保存' : '输入 Token'" />
          </ElFormItem>
          <ElFormItem label="超时时间（秒）">
            <ElInputNumber v-model="form.timeout" :min="1" :max="300" style="width: 100%" />
          </ElFormItem>
        </div>
        <div v-else-if="form.auth_type === 'bearer'" class="pair">
          <ElFormItem label="超时时间（秒）">
            <ElInputNumber v-model="form.timeout" :min="1" :max="300" style="width: 100%" />
          </ElFormItem>
          <ElFormItem v-if="editing?.has_token" label="凭据管理">
            <ElCheckbox v-model="clearToken">清除已保存凭据</ElCheckbox>
          </ElFormItem>
        </div>
        <ElCheckbox v-if="form.auth_type === 'header' && editing?.has_token" v-model="clearToken" class="token-clear">清除已保存凭据</ElCheckbox>

        <ElFormItem label="自定义请求头">
          <div class="headers">
            <div v-for="(header, index) in form.headers" :key="index" class="header-row">
              <ElInput v-model="header.key" :aria-label="`请求头 ${index + 1} 名称`" placeholder="Header 名称" />
              <ElInput v-model="header.value" :aria-label="`请求头 ${index + 1} 值`" placeholder="Header 值" />
              <ElButton link type="danger" :aria-label="`移除请求头 ${index + 1}`" @click="form.headers.splice(index, 1)">移除</ElButton>
            </div>
            <ElButton size="small" :icon="Plus" @click="form.headers.push({ key: '', value: '' })">添加请求头</ElButton>
          </div>
        </ElFormItem>

        <div class="test-box">
          <div class="test-head">
            <div>
              <b>连接测试</b>
              <span class="test-sub">验证连通性并拉取可用 Tool 清单</span>
            </div>
            <ElButton size="small" :loading="testing" :icon="PlugZap" @click="runTest">测试连接</ElButton>
          </div>
          <ElAlert v-if="testResult" :type="testResult.ok ? 'success' : 'error'" show-icon :closable="false" :title="testResult.ok ? `连接正常，发现 ${testResult.tools.length} 个 Tool（${testResult.elapsed_ms} ms）` : testResult.error">
            <div v-if="testResult.tools.length" class="tools">
              <span v-for="tool in testResult.tools" :key="tool.name" class="tool-chip">{{ tool.name }}</span>
            </div>
          </ElAlert>
        </div>
        <ElAlert v-if="saveError" type="error" show-icon :closable="false" :title="saveError" />
      </ElForm>
      <template #footer>
        <div class="drawer-footer">
          <ElSwitch v-model="form.enabled" active-text="启用此 MCP" />
          <div class="drawer-footer__right">
            <ElTag v-if="editorGuard.dirty.value" size="small" type="warning" effect="light">有未保存修改</ElTag>
            <ElButton :disabled="saving" @click="closeEditor">取消</ElButton>
            <ElButton type="primary" :loading="saving" @click="submitMcp">保存 MCP</ElButton>
          </div>
        </div>
      </template>
    </ElDrawer>

    <ElDrawer v-model="detailOpen" :title="viewing?.name || 'MCP 详情'" size="min(640px, 100%)">
      <div v-if="viewing" class="detail">
        <div class="detail-meta-strip">
          <div class="detail-meta-strip__tags">
            <ElTag size="small" type="info" class="astra-tag--neutral">{{ viewing.source === "nexus" ? "Nexus 自建" : "AgentScope" }}</ElTag>
            <ElTag
              size="small"
              :type="viewing.enabled ? 'success' : 'info'"
              :class="{ 'astra-tag--neutral': !viewing.enabled }"
            >{{ viewing.enabled ? "已启用" : "已停用" }}</ElTag>
            <ElTag
              size="small"
              :type="syncTagType(combinedSyncStatus(viewing))"
              :class="{ 'astra-tag--neutral': syncTagType(combinedSyncStatus(viewing)) === 'info' }"
            >{{ syncLabel(combinedSyncStatus(viewing)) }}</ElTag>
          </div>
          <ElButton v-if="viewing.editable" size="small" :icon="Pencil" @click="editFromDetail">编辑</ElButton>
        </div>

        <ElDescriptions :column="2" border>
          <ElDescriptionsItem label="连接地址" :span="2">
            <span class="mono">{{ viewing.url || "AgentScope 托管" }}</span>
          </ElDescriptionsItem>
          <ElDescriptionsItem label="传输方式">{{ transportLabel(viewing.transport) }}</ElDescriptionsItem>
          <ElDescriptionsItem label="鉴权方式">{{ authLabel(viewing.auth_type) }}</ElDescriptionsItem>
          <ElDescriptionsItem label="凭据">{{ viewing.has_token ? "已配置（不回显）" : "未配置" }}</ElDescriptionsItem>
          <ElDescriptionsItem label="平台库登记">
            <template v-if="viewing.registry">
              <ElTag
                size="small"
                :type="syncTagType(viewing.registry.status)"
                :class="{ 'astra-tag--neutral': syncTagType(viewing.registry.status) === 'info' }"
              >{{ viewing.registry.status === 'applied' ? '已登记' : syncLabel(viewing.registry.status) }}</ElTag>
            </template>
            <span v-else class="muted">AgentScope 托管</span>
          </ElDescriptionsItem>
          <ElDescriptionsItem label="说明" :span="2"><span class="detail-desc">{{ viewing.description || "—" }}</span></ElDescriptionsItem>
          <ElDescriptionsItem label="关联员工" :span="2">
            <div v-if="viewing.employees.length" class="chips">
              <span v-for="employee in viewing.employees" :key="employee.id" class="emp-chip">{{ employee.name }}</span>
            </div>
            <span v-else class="muted">未绑定</span>
          </ElDescriptionsItem>
        </ElDescriptions>

        <div v-if="viewing.headers.length" class="detail-block">
          <h4>自定义请求头</h4>
          <div class="chips">
            <span v-for="header in viewing.headers" :key="header.key" class="tool-chip">{{ header.key }}</span>
          </div>
        </div>

        <div v-if="viewing.source === 'nexus'" class="test-box">
          <div class="test-head">
            <div>
              <b>在线连通性检测</b>
              <span class="test-sub">拉取该 MCP 当前暴露的工具清单</span>
            </div>
            <ElButton size="small" :loading="testing" :icon="PlugZap" @click="testSaved(viewing)">测试连接</ElButton>
          </div>
          <ElAlert v-if="detailTest" :type="detailTest.ok ? 'success' : 'error'" show-icon :closable="false" :title="detailTest.ok ? `连接正常，发现 ${detailTest.tools.length} 个 Tool（${detailTest.elapsed_ms} ms）` : detailTest.error">
            <div v-if="detailTest.tools.length" class="tools">
              <span v-for="tool in detailTest.tools" :key="tool.name" class="tool-chip">{{ tool.name }}</span>
            </div>
          </ElAlert>
        </div>
      </div>
    </ElDrawer>
  </section>
</template>

<script setup lang="ts">
import { Pencil, PlugZap, Plus, RefreshCw, Search } from "lucide-vue-next";
import { computed, onMounted, reactive, ref, watch } from "vue";
import { onBeforeRouteLeave } from "vue-router";
import { useFormGuard } from "../../composables/useFormGuard";
import {
  ElAlert,
  ElButton,
  ElCheckbox,
  ElDescriptions,
  ElDescriptionsItem,
  ElDrawer,
  ElDropdown,
  ElDropdownItem,
  ElDropdownMenu,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElInputNumber,
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
import {
  createMcpResource,
  deleteMcpResource,
  getMcpResource,
  listMcpResources,
  syncMcpResource,
  testMcpDraft,
  testSavedMcp,
  updateMcpResource,
  type McpWriteBody,
} from "../../api";
import type { McpResource, McpTestResult } from "../../types";
import { confirmDialog } from "../../composables/useConfirm";
import { notify } from "../../composables/useToast";
import SettingsHeaderActions from "./SettingsHeaderActions.vue";

const items = ref<McpResource[]>([]);
const loading = ref(false);
const error = ref("");
const keyword = ref("");
const sourceFilter = ref("");
const statusFilter = ref("");
const page = ref(1);
const pageSize = ref(20);
const tableRef = ref<TableInstance>();
const busyId = ref("");

const editorOpen = ref(false);
const editing = ref<McpResource | null>(null);
const saving = ref(false);
const saveError = ref("");
const clearToken = ref(false);
const testing = ref(false);
const testResult = ref<McpTestResult | null>(null);
const detailTest = ref<McpTestResult | null>(null);

const form = reactive({
  name: "",
  description: "",
  url: "",
  transport: "http",
  headers: [] as { key: string; value: string }[],
  auth_type: "none",
  auth_header: "Authorization",
  token: "",
  timeout: 30,
  enabled: true,
});

const editorGuard = useFormGuard({
  isOpen: () => editorOpen.value,
  isSaving: () => saving.value,
  snapshot: () => JSON.stringify({ form, clearToken: clearToken.value }),
  confirmDiscard: () => confirmDialog({
    title: "放弃未保存的 MCP 修改？",
    message: "当前 MCP 配置尚未保存，关闭后将无法恢复。",
    confirmLabel: "放弃并关闭",
    cancelLabel: "继续编辑",
    danger: true,
  }),
});
async function closeEditor() { if (await editorGuard.canClose()) editorOpen.value = false; }
onBeforeRouteLeave(() => editorGuard.canClose());

let testGeneration = 0;
const testFingerprint = () => JSON.stringify({
  mcp: editing.value?.id, url: form.url, headers: form.headers,
  auth: form.auth_type, authHeader: form.auth_header, token: form.token,
  clearToken: clearToken.value, timeout: form.timeout,
});
watch([editorOpen, testFingerprint], () => {
  ++testGeneration;
  testing.value = false;
  testResult.value = null;
}, { flush: "sync" });

const detailOpen = ref(false);
const viewing = ref<McpResource | null>(null);

const filtered = computed(() => {
  const needle = keyword.value.trim().toLowerCase();
  return items.value.filter((item) => {
    if (sourceFilter.value && item.source !== sourceFilter.value) return false;
    if (statusFilter.value === "enabled" && !item.enabled) return false;
    if (statusFilter.value === "disabled" && item.enabled) return false;
    if (!needle) return true;
    return `${item.name} ${item.description} ${item.url}`.toLowerCase().includes(needle);
  });
});
const paged = computed(() => filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value));

watch([keyword, sourceFilter, statusFilter], () => { page.value = 1; });
watch(pageSize, () => { page.value = 1; });
watch(filtered, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / pageSize.value));
  if (page.value > maxPage) page.value = maxPage;
});
watch([page, pageSize], () => { tableRef.value?.setScrollTop(0); });

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const payload = await listMcpResources();
    items.value = payload.items;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "MCP 列表加载失败";
  } finally {
    loading.value = false;
  }
}

function transportLabel(transport: string) {
  if (transport === "http") return "HTTP（Streamable）";
  if (transport === "agent-scope") return "AgentScope 托管";
  return transport || "—";
}

function authLabel(auth: string) {
  if (auth === "bearer") return "Bearer Token";
  if (auth === "header") return "自定义 Header";
  if (auth === "managed") return "AgentScope 托管";
  return "无鉴权";
}

function syncLabel(status: string) {
  return {
    applied: "已同步",
    pending: "待同步",
    error: "同步失败",
    disabled: "已停用",
    unbound: "未绑定",
  }[status] ?? status;
}

function syncTagType(status: string) {
  return status === "applied" ? "success" : status === "error" ? "danger" : status === "pending" ? "warning" : "info";
}

function combinedSyncStatus(row: McpResource): string {
  if (row.registry && row.registry.status === "error") return "error";
  if (row.registry && row.registry.status === "pending" && row.sync.status === "applied") return "pending";
  return row.sync.status;
}

function combinedSyncTooltip(row: McpResource): string {
  const parts: string[] = [];
  if (row.registry) {
    parts.push(`平台库：${row.registry.status === "applied" ? "已登记" : syncLabel(row.registry.status)}${row.registry.detail ? `（${row.registry.detail}）` : ""}`);
  }
  parts.push(`会话同步：${syncLabel(row.sync.status)}${row.sync.detail ? `（${row.sync.detail}）` : ""}`);
  return parts.join("；");
}

function hasMoreActions(row: McpResource) {
  return row.editable || (row.employees.length > 0 && row.sync.status !== "applied");
}

function onRowCommand(cmd: string, row: McpResource) {
  if (cmd === "sync") void retrySync(row);
  else if (cmd === "delete") void removeMcp(row);
}

function resetForm() {
  form.name = "";
  form.description = "";
  form.url = "";
  form.transport = "http";
  form.headers = [];
  form.auth_type = "none";
  form.auth_header = "Authorization";
  form.token = "";
  form.timeout = 30;
  form.enabled = true;
  clearToken.value = false;
  testResult.value = null;
  saveError.value = "";
}

function beginCreate() {
  editing.value = null;
  resetForm();
  editorOpen.value = true;
  editorGuard.capture();
}

async function beginEdit(row: McpResource) {
  try {
    const { mcp } = await getMcpResource(row.id);
    editing.value = mcp;
    form.name = mcp.name;
    form.description = mcp.description;
    form.url = mcp.url;
    form.transport = "http";
    form.headers = mcp.headers.map((header) => ({ key: header.key, value: header.value }));
    form.auth_type = mcp.auth_type;
    form.auth_header = mcp.auth_header || "Authorization";
    form.token = "";
    form.timeout = mcp.timeout || 30;
    form.enabled = mcp.enabled;
    clearToken.value = false;
    testResult.value = null;
    saveError.value = "";
    editorOpen.value = true;
    editorGuard.capture();
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : "MCP 详情加载失败", "error");
  }
}

async function openDetail(row: McpResource) {
  try {
    const { mcp } = await getMcpResource(row.id);
    viewing.value = mcp;
    detailTest.value = null;
    detailOpen.value = true;
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : "MCP 详情加载失败", "error");
  }
}

function editFromDetail() {
  const target = viewing.value;
  detailOpen.value = false;
  if (target) void beginEdit(target);
}

function writeBody(includeToken: boolean): McpWriteBody {
  const body: McpWriteBody = {
    name: form.name.trim(),
    description: form.description.trim(),
    url: form.url.trim(),
    transport: form.transport,
    headers: form.headers.filter((header) => header.key.trim()).map((header) => ({ key: header.key.trim(), value: header.value })),
    auth_type: form.auth_type,
    auth_header: form.auth_header.trim() || "Authorization",
    timeout: Number(form.timeout) || 30,
    enabled: form.enabled,
  };
  if (includeToken) {
    if (clearToken.value) body.token = "";
    else if (form.token) body.token = form.token;
    else if (!editing.value?.has_token) body.token = "";
  }
  return body;
}

async function runTest() {
  if (testing.value || saving.value) return;
  const generation = ++testGeneration;
  testing.value = true;
  testResult.value = null;
  try {
    const payload: McpWriteBody & { id?: string } = {
      url: form.url.trim(),
      headers: form.headers.filter((header) => header.key.trim()).map((header) => ({ key: header.key.trim(), value: header.value })),
      auth_type: form.auth_type,
      auth_header: form.auth_header.trim() || "Authorization",
      timeout: Number(form.timeout) || 30,
    };
    if (editing.value) payload.id = editing.value.id;
    if (form.token) payload.token = form.token;
    const result = await testMcpDraft(payload);
    if (generation === testGeneration && editorOpen.value) testResult.value = result;
  } catch (reason) {
    if (generation === testGeneration && editorOpen.value) {
      testResult.value = { ok: false, tools: [], error: reason instanceof Error ? reason.message : "连接测试失败", elapsed_ms: 0 };
    }
  } finally {
    if (generation === testGeneration) testing.value = false;
  }
}

async function submitMcp() {
  if (saving.value) return;
  if (!form.name.trim() || !form.url.trim()) {
    saveError.value = "名称和连接地址不能为空";
    return;
  }
  saving.value = true;
  saveError.value = "";
  try {
    const result = editing.value
      ? await updateMcpResource(editing.value.id, writeBody(true))
      : await createMcpResource(writeBody(true));
    editorOpen.value = false;
    await load();
    if (result.sync.status === "applied") notify(editing.value ? "MCP 已保存并同步" : "MCP 已创建并登记");
    else notify(`MCP 已保存，${result.sync.detail || "等待同步"}`, result.sync.status === "error" ? "error" : "info", 4200);
  } catch (reason) {
    saveError.value = reason instanceof Error ? reason.message : "MCP 保存失败";
  } finally {
    saving.value = false;
  }
}

async function toggleEnabled(row: McpResource) {
  busyId.value = row.id;
  try {
    const result = await updateMcpResource(row.id, { enabled: !row.enabled });
    await load();
    notify(
      result.sync.status === "applied" ? `MCP 已${row.enabled ? "停用" : "启用"}` : `配置已更新，${result.sync.detail || "待同步"}`,
      result.sync.status === "applied" ? "success" : result.sync.status === "error" ? "error" : "info",
    );
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : "更新失败", "error");
  } finally {
    busyId.value = "";
  }
}

async function retrySync(row: McpResource) {
  busyId.value = row.id;
  try {
    const result = await syncMcpResource(row.id);
    await load();
    notify(result.sync.status === "applied" ? "同步完成" : `同步未完成：${result.sync.detail || "待重试"}`, result.sync.status === "applied" ? "success" : "error");
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : "同步失败", "error");
  } finally {
    busyId.value = "";
  }
}

async function testSaved(row: McpResource) {
  if (detailOpen.value) {
    detailTest.value = null;
    testing.value = true;
    try {
      detailTest.value = await testSavedMcp(row.id);
    } catch (reason) {
      detailTest.value = { ok: false, tools: [], error: reason instanceof Error ? reason.message : "连接测试失败", elapsed_ms: 0 };
    } finally {
      testing.value = false;
    }
    return;
  }
  busyId.value = row.id;
  try {
    const result = await testSavedMcp(row.id);
    await load();
    notify(result.ok ? `连接正常，发现 ${result.tools.length} 个 Tool` : `连接失败：${result.error}`, result.ok ? "success" : "error", 4200);
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : "连接测试失败", "error");
  } finally {
    busyId.value = "";
  }
}

async function removeMcp(row: McpResource) {
  const confirmed = await confirmDialog({
    title: "删除 MCP",
    message: `确定删除「${row.name}」吗？被员工引用的 MCP 不能删除。`,
    danger: true,
    confirmLabel: "删除",
  });
  if (!confirmed) return;
  busyId.value = row.id;
  try {
    await deleteMcpResource(row.id);
    await load();
    notify("MCP 已删除");
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : "删除失败", "error");
  } finally {
    busyId.value = "";
  }
}

onMounted(load);
</script>

<style scoped>
.name-cell { display: grid; gap: 2px; min-width: 0; }
.name-link {
  padding: 0; border: 0; background: transparent; cursor: pointer;
  color: var(--astra-text-primary); font-size: 13.5px; font-weight: 600;
  text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.name-link:hover { color: var(--astra-accent-text); text-decoration: underline; }
.name-desc { color: var(--astra-text-muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chips { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.emp-chip {
  max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  padding: 1px 7px; border-radius: 5px;
  border: 1px solid var(--astra-border); background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary); font-size: 12px; line-height: 18px;
}
.emp-more { padding: 1px 6px; border-radius: 5px; background: var(--astra-bg-elevated); color: var(--astra-text-secondary); font-size: 11px; font-weight: 500; }
.muted { color: var(--astra-text-muted); font-size: 12.5px; }
.mono { font-family: var(--astra-font-mono); font-size: 12px; color: var(--astra-text-secondary); }
.sync-count { margin-left: 6px; color: var(--astra-text-muted); font-size: 11px; }
.danger-item { color: var(--astra-danger); }

.section-cap {
  margin: 4px 0 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--astra-border-subtle);
  color: var(--astra-text-muted);
  font-size: 12px;
  font-weight: 600;
}
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.headers { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.header-row { display: grid; grid-template-columns: 1fr 1.4fr auto; gap: 8px; }
.token-clear { margin-bottom: 12px; }

.test-box {
  display: grid; gap: 10px;
  padding: 12px 14px; margin: 4px 0 14px;
  border: 1px solid var(--astra-border); border-radius: 10px; background: var(--astra-bg-subtle);
}
.test-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.test-head b { color: var(--astra-text-primary); font-size: 13px; margin-right: 8px; }
.test-sub { color: var(--astra-text-muted); font-size: 12px; }
.tools { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.tool-chip {
  padding: 2px 7px; border: 1px solid var(--astra-border); border-radius: 5px;
  background: var(--astra-bg-panel); color: var(--astra-text-primary);
  font-family: var(--astra-font-mono); font-size: 11.5px; line-height: 18px;
}

.drawer-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; }
.drawer-footer__right { display: flex; align-items: center; gap: 8px; }

.detail { display: flex; flex-direction: column; gap: 14px; }
.detail-meta-strip {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;
  padding: 10px 12px; border: 1px solid var(--astra-border); border-radius: 10px; background: var(--astra-bg-subtle);
}
.detail-meta-strip__tags { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.detail-desc { white-space: pre-wrap; word-break: break-word; }
.detail-block { display: flex; flex-direction: column; gap: 6px; }
.detail-block h4 { margin: 0; color: var(--astra-text-secondary); font-size: 13px; font-weight: 600; }
</style>
