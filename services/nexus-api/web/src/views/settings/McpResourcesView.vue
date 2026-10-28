<template>
  <section class="admin-page" data-testid="mcp-resources-page">
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
        <ElInput v-model="keyword" class="admin-page__keyword" aria-label="搜索 MCP" placeholder="搜索名称 / 说明 / 地址" clearable :prefix-icon="Search" />
      </div>
    </div>

    <ElTable v-loading="loading" :data="paged" row-key="id" :max-height="TABLE_MAX_HEIGHT" aria-label="MCP 列表" element-loading-text="正在加载 MCP 列表" data-testid="mcps-table">
      <ElTableColumn label="名称" min-width="170">
        <template #default="{ row }">
          <div class="name-cell">
            <b>{{ row.name }}</b>
            <small v-if="row.runtime_name && row.runtime_name !== row.name">运行名：{{ row.runtime_name }}</small>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="说明" min-width="190" show-overflow-tooltip>
        <template #default="{ row }">{{ row.description || '—' }}</template>
      </ElTableColumn>
      <ElTableColumn label="连接地址" min-width="230" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.url" class="mono">{{ row.url }}</span>
          <span v-else class="muted">由 AgentScope 管理</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="传输方式" width="128">
        <template #default="{ row }">{{ transportLabel(row.transport) }}</template>
      </ElTableColumn>
      <ElTableColumn label="状态" width="96">
        <template #default="{ row }">
          <ElTag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '已启用' : '已停用' }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="关联员工" min-width="140">
        <template #default="{ row }">
          <div class="chips">
            <ElTag v-for="employee in row.employees" :key="employee.id" size="small" type="info">{{ employee.name }}</ElTag>
            <span v-if="!row.employees.length" class="muted">未绑定</span>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="同步状态" min-width="140">
        <template #default="{ row }">
          <ElTooltip :content="row.sync.detail || syncLabel(row.sync.status)" placement="top">
            <ElTag size="small" :type="syncTagType(row.sync.status)">{{ syncLabel(row.sync.status) }}</ElTag>
          </ElTooltip>
          <span v-if="row.sync.pending" class="sync-count">待 {{ row.sync.pending }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="操作" width="240" align="center" fixed="right">
        <template #default="{ row }">
          <div class="admin-actions">
            <ElButton link type="primary" @click="openDetail(row as McpResource)">查看</ElButton>
            <ElButton v-if="row.editable" link type="primary" @click="beginEdit(row as McpResource)">编辑</ElButton>
            <ElButton link type="primary" :disabled="busyId === row.id" @click="testSaved(row as McpResource)">测试连接</ElButton>
            <ElButton
              v-if="row.editable"
              link
              :type="row.enabled ? 'warning' : 'primary'"
              :disabled="busyId === row.id"
              @click="toggleEnabled(row as McpResource)"
            >{{ row.enabled ? '停用' : '启用' }}</ElButton>
            <ElButton v-if="row.editable" link type="danger" :disabled="busyId === row.id" @click="removeMcp(row as McpResource)">删除</ElButton>
            <ElButton
              v-if="row.employees.length && row.sync.status !== 'applied'"
              link
              type="primary"
              :disabled="busyId === row.id"
              @click="retrySync(row as McpResource)"
            >重试同步</ElButton>
          </div>
        </template>
      </ElTableColumn>
      <template #empty>
        <ElEmpty v-if="!loading" :image-size="72" :description="items.length ? '没有匹配的 MCP' : '暂无 MCP 资源'" />
      </template>
    </ElTable>

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

    <ElDrawer v-model="editorOpen" :title="editing ? '编辑 MCP' : '新增 MCP'" size="680px" :close-on-click-modal="false" :before-close="closeEditor">
      <ElForm label-position="top" :disabled="saving" @submit.prevent="submitMcp">
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
        <ElFormItem label="说明">
          <ElInput v-model="form.description" type="textarea" :rows="2" placeholder="例如：发布与回滚" />
        </ElFormItem>
        <ElFormItem label="连接地址" required>
          <ElInput v-model="form.url" placeholder="http://10.0.0.8:9000/mcp 或 https://mcp.example.com/mcp" />
        </ElFormItem>
        <ElFormItem label="请求头">
          <div class="headers">
            <div v-for="(header, index) in form.headers" :key="index" class="header-row">
              <ElInput v-model="header.key" :aria-label="`请求头 ${index + 1} 名称`" placeholder="请求头名称" />
              <ElInput v-model="header.value" :aria-label="`请求头 ${index + 1} 值`" placeholder="请求头值（非敏感）" />
              <ElButton link type="danger" :aria-label="`移除请求头 ${index + 1}`" @click="form.headers.splice(index, 1)">移除</ElButton>
            </div>
            <ElButton size="small" :icon="Plus" @click="form.headers.push({ key: '', value: '' })">添加请求头</ElButton>
          </div>
        </ElFormItem>
        <div class="pair">
          <ElFormItem label="鉴权方式">
            <ElSelect v-model="form.auth_type" style="width: 100%">
              <ElOption value="none" label="无鉴权" />
              <ElOption value="bearer" label="Bearer Token" />
              <ElOption value="header" label="自定义 Header" />
            </ElSelect>
          </ElFormItem>
          <ElFormItem v-if="form.auth_type === 'header'" label="鉴权 Header">
            <ElInput v-model="form.auth_header" placeholder="X-Api-Key" />
          </ElFormItem>
        </div>
        <ElFormItem v-if="form.auth_type !== 'none'" :label="editing?.has_token ? 'Token（已保存，留空不修改）' : 'Token'">
          <ElInput v-model="form.token" type="password" show-password autocomplete="new-password" :placeholder="editing?.has_token ? '已保存，留空不修改' : '输入 Token'" />
          <ElCheckbox v-if="editing?.has_token" v-model="clearToken" class="token-clear">清除已保存凭据</ElCheckbox>
        </ElFormItem>
        <ElFormItem label="超时时间（秒）">
          <ElInputNumber v-model="form.timeout" :min="1" :max="300" />
        </ElFormItem>
        <ElFormItem label="启用">
          <ElSwitch v-model="form.enabled" />
        </ElFormItem>

        <div class="test-box">
          <div class="test-head">
            <b>连接测试</b>
            <ElButton :loading="testing" :icon="PlugZap" @click="runTest">测试连接</ElButton>
          </div>
          <ElAlert v-if="testResult" :type="testResult.ok ? 'success' : 'error'" show-icon :closable="false" :title="testResult.ok ? `连接正常，发现 ${testResult.tools.length} 个 Tool` : testResult.error">
            <div v-if="testResult.tools.length" class="tools">
              <ElTag v-for="tool in testResult.tools" :key="tool.name" size="small" type="info" class="tool-tag">{{ tool.name }}</ElTag>
            </div>
            <div v-if="testResult.ok" class="hint">耗时 {{ testResult.elapsed_ms }} ms</div>
          </ElAlert>
        </div>
        <ElAlert v-if="saveError" type="error" show-icon :closable="false" :title="saveError" />
      </ElForm>
      <template #footer>
        <div class="drawer-footer">
          <ElTag v-if="editorGuard.dirty.value" type="warning">有未保存修改</ElTag>
          <ElButton :disabled="saving" @click="closeEditor">取消</ElButton>
          <ElButton type="primary" :loading="saving" @click="submitMcp">保存 MCP</ElButton>
        </div>
      </template>
    </ElDrawer>

    <ElDrawer v-model="detailOpen" title="MCP 详情" size="620px">
      <div v-if="viewing" class="detail">
        <ElDescriptions :column="2" border>
          <ElDescriptionsItem label="名称">{{ viewing.name }}</ElDescriptionsItem>
          <ElDescriptionsItem label="来源">{{ viewing.source === "nexus" ? "Nexus 自建" : "AgentScope" }}</ElDescriptionsItem>
          <ElDescriptionsItem label="连接地址" :span="2">{{ viewing.url || "由 AgentScope 管理" }}</ElDescriptionsItem>
          <ElDescriptionsItem label="传输方式">{{ transportLabel(viewing.transport) }}</ElDescriptionsItem>
          <ElDescriptionsItem label="状态">{{ viewing.enabled ? "已启用" : "已停用" }}</ElDescriptionsItem>
          <ElDescriptionsItem label="鉴权">{{ authLabel(viewing.auth_type) }}</ElDescriptionsItem>
          <ElDescriptionsItem label="凭据">{{ viewing.has_token ? "已保存（不回显）" : "未保存" }}</ElDescriptionsItem>
          <ElDescriptionsItem label="说明" :span="2"><span class="detail-desc">{{ viewing.description || "—" }}</span></ElDescriptionsItem>
          <ElDescriptionsItem label="同步" :span="2">
            <ElTag size="small" :type="syncTagType(viewing.sync.status)">{{ syncLabel(viewing.sync.status) }}</ElTag>
            <span class="detail-sync">{{ viewing.sync.detail || "—" }}</span>
          </ElDescriptionsItem>
          <ElDescriptionsItem label="关联员工" :span="2">
            <ElTag v-for="employee in viewing.employees" :key="employee.id" size="small" type="info" class="gap-tag">{{ employee.name }}</ElTag>
            <span v-if="!viewing.employees.length" class="muted">未绑定</span>
          </ElDescriptionsItem>
        </ElDescriptions>
        <div v-if="viewing.headers.length" class="detail-block">
          <h4>请求头</h4>
          <ElTag v-for="header in viewing.headers" :key="header.key" size="small" type="info" class="gap-tag">{{ header.key }}</ElTag>
        </div>
        <div v-if="viewing.source === 'nexus'" class="detail-block">
          <ElButton :loading="testing" :icon="PlugZap" @click="testSaved(viewing)">测试连接并获取 Tool 列表</ElButton>
          <ElAlert v-if="detailTest" :type="detailTest.ok ? 'success' : 'error'" show-icon :closable="false" class="detail-test" :title="detailTest.ok ? `连接正常，发现 ${detailTest.tools.length} 个 Tool` : detailTest.error">
            <div class="tools">
              <ElTag v-for="tool in detailTest.tools" :key="tool.name" size="small" type="info" class="tool-tag">{{ tool.name }}</ElTag>
            </div>
          </ElAlert>
        </div>
      </div>
    </ElDrawer>
  </section>
</template>

<script setup lang="ts">
import { PlugZap, Plus, RefreshCw, Search } from "lucide-vue-next";
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
import { TABLE_MAX_HEIGHT } from "../../utils/table";
import SettingsHeaderActions from "./SettingsHeaderActions.vue";

const items = ref<McpResource[]>([]);
const loading = ref(false);
const error = ref("");
const keyword = ref("");
const sourceFilter = ref("");
const statusFilter = ref("");
const page = ref(1);
const pageSize = ref(20);
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
  isOpen: () => editorOpen.value, isSaving: () => saving.value,
  snapshot: () => JSON.stringify({ form, clearToken: clearToken.value }),
  confirmDiscard: () => confirmDialog({ title: "放弃未保存的 MCP 修改？",
    message: "当前 MCP 配置尚未保存，关闭后将无法恢复。", confirmLabel: "放弃并关闭", cancelLabel: "继续编辑", danger: true }),
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

watch([keyword, sourceFilter, statusFilter], () => {
  page.value = 1;
});

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
  if (auth === "managed") return "由 AgentScope 管理";
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
    form.transport = mcp.transport === "http" ? "http" : "http";
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
    if (result.sync.status === "applied") notify(editing.value ? "MCP 已保存并同步" : "MCP 已创建");
    else notify(`MCP 已保存，${result.sync.detail || "等待同步"}`, "info", 4200);
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
    notify(result.sync.status === "applied" ? `MCP 已${row.enabled ? "停用" : "启用"}` : `配置已更新，${result.sync.detail || "待同步"}`, result.sync.status === "applied" ? "success" : "info");
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
.name-cell { display: flex; flex-direction: column; gap: 2px; }
.name-cell small { color: var(--astra-text-muted); font-size: 11px; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.muted { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.mono { font-family: var(--astra-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace); font-size: 12px; }
.sync-count { margin-left: 6px; color: var(--astra-text-muted); font-size: 11px; }
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.headers { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.header-row { display: grid; grid-template-columns: 1fr 1.4fr auto; gap: 8px; }
.hint { color: var(--astra-text-muted); font-size: var(--astra-font-helper); margin-top: 4px; }
.token-clear { margin-top: 6px; }
.test-box { padding: 12px; margin: 6px 0 14px; border: 1px solid var(--astra-border); border-radius: 8px; background: var(--astra-bg-elevated); }
.test-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.tools { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.tool-tag { font-family: var(--astra-font-mono, ui-monospace, monospace); }
.drawer-footer { display: flex; justify-content: flex-end; gap: 8px; }
.detail-desc { white-space: pre-wrap; word-break: break-word; }
.detail { display: flex; flex-direction: column; gap: 12px; }
.detail h4 { margin: 0 0 6px; color: var(--astra-text-secondary); font-size: 13px; }
.detail-sync { margin-left: 8px; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.gap-tag { margin-right: 4px; }
.detail-block { display: flex; flex-direction: column; gap: 6px; }
.detail-test { margin-top: 8px; }
</style>
