<template>
  <section class="admin-page" data-testid="skill-resources-page">
    <SettingsHeaderActions>
      <ElButton :icon="RefreshCw" :loading="loading" data-testid="skills-refresh" @click="load">刷新</ElButton>
      <ElButton :icon="Upload" data-testid="skills-upload" @click="triggerUpload">上传技能目录</ElButton>
      <ElButton type="primary" :icon="Plus" data-testid="skills-create" @click="beginCreate">新增技能</ElButton>
    </SettingsHeaderActions>

    <input ref="directoryInput" class="hidden-input" type="file" webkitdirectory multiple @change="onDirectoryPicked" />

    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="skills-error">
      <ElButton type="primary" data-testid="skills-retry" @click="load">重试</ElButton>
    </ElAlert>

    <div class="admin-page__toolbar" role="search" aria-label="技能筛选">
      <div class="admin-page__filters">
        <ElSelect v-model="sourceFilter" class="admin-page__select" aria-label="技能来源" placeholder="来源：全部" clearable>
          <ElOption value="nexus" label="Nexus 自建" />
          <ElOption value="agentscope" label="AgentScope" />
          <ElOption value="builtin" label="内置" />
        </ElSelect>
        <ElSelect v-model="statusFilter" class="admin-page__select" aria-label="启用状态" placeholder="状态：全部" clearable>
          <ElOption value="enabled" label="已启用" />
          <ElOption value="disabled" label="已停用" />
        </ElSelect>
      </div>
      <div class="admin-page__search">
        <ElInput
          v-model="keyword"
          class="admin-page__keyword"
          aria-label="搜索技能"
          placeholder="搜索技能名称 / 说明"
          clearable
          :prefix-icon="Search"
        />
      </div>
    </div>

    <ElTable
      v-loading="loading"
      :data="paged"
      row-key="id"
      aria-label="技能列表"
      element-loading-text="正在加载技能列表"
      data-testid="skills-table"
    >
      <ElTableColumn label="名称" min-width="190">
        <template #default="{ row }">
          <div class="name-cell">
            <b>{{ row.name }}</b>
            <small v-if="row.runtime_name && row.runtime_name !== row.name">运行名：{{ row.runtime_name }}</small>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="说明" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ row.description || '—' }}</template>
      </ElTableColumn>
      <ElTableColumn label="来源" width="118">
        <template #default="{ row }">
          <ElTag size="small" :type="sourceTagType(row.source)" effect="plain">{{ sourceLabel(row.source) }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="版本" width="88">
        <template #default="{ row }">{{ row.version || '—' }}</template>
      </ElTableColumn>
      <ElTableColumn label="状态" width="96">
        <template #default="{ row }">
          <ElTag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '已启用' : '已停用' }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="关联员工" min-width="150">
        <template #default="{ row }">
          <div class="chips">
            <ElTag v-for="employee in row.employees" :key="employee.id" size="small" type="info">{{ employee.name }}</ElTag>
            <span v-if="!row.employees.length" class="muted">未绑定</span>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="同步状态" min-width="150">
        <template #default="{ row }">
          <ElTooltip :content="row.sync.detail || syncLabel(row.sync.status)" placement="top">
            <ElTag size="small" :type="syncTagType(row.sync.status)">{{ syncLabel(row.sync.status) }}</ElTag>
          </ElTooltip>
          <span v-if="row.sync.pending" class="sync-count">待 {{ row.sync.pending }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="操作" width="250" align="center">
        <template #default="{ row }">
          <div class="admin-actions">
            <ElButton link type="primary" @click="openDetail(row as SkillResource)">查看</ElButton>
            <ElButton v-if="row.editable" link type="primary" @click="beginEdit(row as SkillResource)">编辑</ElButton>
            <ElButton v-if="row.source === 'nexus'" link type="primary" @click="openVersions(row as SkillResource)">版本</ElButton>
            <ElButton
              v-if="row.editable"
              link
              :type="row.enabled ? 'warning' : 'primary'"
              :disabled="busyId === row.id"
              @click="toggleEnabled(row as SkillResource)"
            >{{ row.enabled ? '停用' : '启用' }}</ElButton>
            <ElButton
              v-if="row.editable"
              link
              type="danger"
              :disabled="busyId === row.id"
              @click="removeSkill(row as SkillResource)"
            >删除</ElButton>
            <ElButton
              v-if="row.employees.length && row.sync.status !== 'applied'"
              link
              type="primary"
              :disabled="busyId === row.id"
              @click="retrySync(row as SkillResource)"
            >重试同步</ElButton>
          </div>
        </template>
      </ElTableColumn>
      <template #empty>
        <ElEmpty v-if="!loading" :image-size="72" :description="items.length ? '没有匹配的技能' : '暂无技能资源'" />
      </template>
    </ElTable>
    <div class="admin-page__footer">
      <span class="sync-hint">AgentScope {{ agentscopeAvailable ? '已连接' : '不可用' }}；列表合并 Nexus 自建与 AgentScope 已安装技能。</span>
      <ElPagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50]"
        :total="filtered.length"
        layout="total, sizes, prev, pager, next"
        aria-label="技能分页"
        data-testid="skills-pagination"
      />
    </div>

    <ElDrawer v-model="editorOpen" :title="editing ? '编辑技能' : '新增技能'" size="720px" :close-on-click-modal="false">
      <ElForm label-position="top" @submit.prevent="submitSkill">
        <ElAlert type="info" :closable="false" show-icon class="form-note">
          SKILL.md 必须包含 YAML frontmatter，其中 <code>name</code> 与 <code>description</code> 必填；资源文件按相对路径保存并随技能一起装入 AgentScope 工作区。
        </ElAlert>
        <ElFormItem label="SKILL.md" required>
          <ElInput v-model="skillMarkdown" type="textarea" :rows="16" class="code-editor" spellcheck="false" />
        </ElFormItem>
        <div class="file-head">
          <span>资源文件</span>
          <ElButton size="small" :icon="Plus" @click="addResourceFile">添加文件</ElButton>
        </div>
        <div v-if="!resourceFiles.length" class="muted file-empty">没有额外资源文件。技能只包含 SKILL.md。</div>
        <div v-for="(file, index) in resourceFiles" :key="index" class="resource-file">
          <div class="resource-file__head">
            <ElInput v-model="file.path" placeholder="相对路径，例如 references/check.md" />
            <ElButton link type="danger" @click="removeResourceFile(index)">移除</ElButton>
          </div>
          <ElInput
            v-model="file.content"
            type="textarea"
            :rows="5"
            class="code-editor"
            :disabled="file.encoding === 'base64'"
            :placeholder="file.encoding === 'base64' ? '二进制文件，保持原内容' : '文件内容'"
          />
        </div>
        <ElFormItem label="启用">
          <ElSwitch v-model="formEnabled" />
        </ElFormItem>
        <ElAlert v-if="saveError" type="error" show-icon :closable="false" :title="saveError" />
      </ElForm>
      <template #footer>
        <div class="drawer-footer">
          <ElButton @click="editorOpen = false">取消</ElButton>
          <ElButton type="primary" :loading="saving" @click="submitSkill">保存技能</ElButton>
        </div>
      </template>
    </ElDrawer>

    <ElDrawer v-model="detailOpen" title="技能详情" size="680px">
      <div v-if="viewing" class="detail">
        <ElDescriptions :column="2" border>
          <ElDescriptionsItem label="名称">{{ viewing.name }}</ElDescriptionsItem>
          <ElDescriptionsItem label="来源">{{ sourceLabel(viewing.source) }}</ElDescriptionsItem>
          <ElDescriptionsItem label="版本">{{ viewing.version || '—' }}</ElDescriptionsItem>
          <ElDescriptionsItem label="状态">{{ viewing.enabled ? '已启用' : '已停用' }}</ElDescriptionsItem>
          <ElDescriptionsItem label="说明" :span="2">{{ viewing.description || '—' }}</ElDescriptionsItem>
          <ElDescriptionsItem label="同步" :span="2">
            <ElTag size="small" :type="syncTagType(viewing.sync.status)">{{ syncLabel(viewing.sync.status) }}</ElTag>
            <span class="detail-sync">{{ viewing.sync.detail || '—' }}</span>
          </ElDescriptionsItem>
          <ElDescriptionsItem label="关联员工" :span="2">
            <ElTag v-for="employee in viewing.employees" :key="employee.id" size="small" type="info" class="gap-tag">{{ employee.name }}</ElTag>
            <span v-if="!viewing.employees.length" class="muted">未绑定</span>
          </ElDescriptionsItem>
        </ElDescriptions>
        <h4>SKILL.md</h4>
        <pre class="markdown-preview">{{ skillMarkdownOf(viewing) }}</pre>
        <template v-if="resourceFilesOf(viewing).length">
          <h4>资源文件</h4>
          <div v-for="file in resourceFilesOf(viewing)" :key="file.path" class="detail-file">
            <b>{{ file.path }}</b>
            <pre class="markdown-preview">{{ file.encoding === 'base64' ? '（二进制文件）' : file.content }}</pre>
          </div>
        </template>
      </div>
    </ElDrawer>

    <ElDialog v-model="versionsOpen" title="版本管理" width="620px">
      <div v-if="versionsFor" class="version-list">
        <div class="muted">{{ versionsFor.name }}</div>
        <div v-for="version in versionItems" :key="version.version" class="version-row">
          <div>
            <b>v{{ version.version }}</b>
            <span class="muted">{{ version.note || '无说明' }} · {{ formatTime(version.created_at) }}</span>
          </div>
          <ElButton
            link
            type="primary"
            :disabled="version.version === versionsFor.version"
            @click="restore(version.version)"
          >恢复此版本</ElButton>
        </div>
        <ElEmpty v-if="!versionItems.length" :image-size="56" description="暂无版本记录" />
      </div>
    </ElDialog>
  </section>
</template>

<script setup lang="ts">
import { Plus, RefreshCw, Search, Upload } from "lucide-vue-next";
import { computed, onMounted, reactive, ref, watch } from "vue";
import {
  ElAlert,
  ElButton,
  ElDescriptions,
  ElDescriptionsItem,
  ElDialog,
  ElDrawer,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
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
  createSkillResource,
  deleteSkillResource,
  getSkillResource,
  listSkillResources,
  restoreSkillVersion,
  skillResourceVersions,
  syncSkillResource,
  updateSkillResource,
  uploadSkillDirectory,
} from "../../api";
import type { SkillFile, SkillResource, SkillVersion } from "../../types";
import { confirmDialog } from "../../composables/useConfirm";
import { notify } from "../../composables/useToast";
import SettingsHeaderActions from "./SettingsHeaderActions.vue";

const DEFAULT_MARKDOWN = `---
name: 新技能
description: 说明这个技能解决什么问题
---

# 新技能

在这里写清适用场景、步骤和验收方式。
`;

const items = ref<SkillResource[]>([]);
const loading = ref(false);
const error = ref("");
const agentscopeAvailable = ref(false);
const keyword = ref("");
const sourceFilter = ref("");
const statusFilter = ref("");
const page = ref(1);
const pageSize = ref(20);
const busyId = ref("");

const directoryInput = ref<HTMLInputElement | null>(null);

const editorOpen = ref(false);
const editing = ref<SkillResource | null>(null);
const skillMarkdown = ref(DEFAULT_MARKDOWN);
const resourceFiles = ref<{ path: string; content: string; encoding: "utf8" | "base64" }[]>([]);
const formEnabled = ref(true);
const saving = ref(false);
const saveError = ref("");

const detailOpen = ref(false);
const viewing = ref<SkillResource | null>(null);

const versionsOpen = ref(false);
const versionsFor = ref<SkillResource | null>(null);
const versionItems = ref<SkillVersion[]>([]);

const filtered = computed(() => {
  const needle = keyword.value.trim().toLowerCase();
  return items.value.filter((item) => {
    if (sourceFilter.value && item.source !== sourceFilter.value) return false;
    if (statusFilter.value === "enabled" && !item.enabled) return false;
    if (statusFilter.value === "disabled" && item.enabled) return false;
    if (!needle) return true;
    return `${item.name} ${item.description} ${item.source}`.toLowerCase().includes(needle);
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
    const payload = await listSkillResources();
    items.value = payload.items;
    agentscopeAvailable.value = payload.agentscope_available;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "技能列表加载失败";
  } finally {
    loading.value = false;
  }
}

function sourceLabel(source: string) {
  return source === "nexus" ? "Nexus 自建" : source === "agentscope" ? "AgentScope" : "内置";
}

function sourceTagType(source: string) {
  return source === "nexus" ? "success" : source === "agentscope" ? "warning" : "info";
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

function beginCreate() {
  editing.value = null;
  skillMarkdown.value = DEFAULT_MARKDOWN;
  resourceFiles.value = [];
  formEnabled.value = true;
  saveError.value = "";
  editorOpen.value = true;
}

async function beginEdit(row: SkillResource) {
  saveError.value = "";
  try {
    const { skill } = await getSkillResource(row.id);
    editing.value = skill;
    skillMarkdown.value = skillMdOf(skill);
    resourceFiles.value = filesOf(skill).map((file) => ({
      path: file.path,
      content: file.content ?? "",
      encoding: file.encoding ?? "utf8",
    }));
    formEnabled.value = skill.enabled;
    editorOpen.value = true;
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : "技能详情加载失败", "error");
  }
}

async function openDetail(row: SkillResource) {
  try {
    const { skill } = await getSkillResource(row.id);
    viewing.value = skill;
    detailOpen.value = true;
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : "技能详情加载失败", "error");
  }
}

function filesOf(skill: SkillResource): SkillFile[] {
  return skill.files ?? [];
}

function skillMdOf(skill: SkillResource) {
  return filesOf(skill).find((file) => file.path === "SKILL.md")?.content ?? "";
}

function skillMarkdownOf(skill: SkillResource) {
  return skillMdOf(skill) || "（没有 SKILL.md）";
}

function resourceFilesOf(skill: SkillResource) {
  return filesOf(skill).filter((file) => file.path !== "SKILL.md");
}

function addResourceFile() {
  resourceFiles.value.push({ path: "", content: "", encoding: "utf8" });
}

function removeResourceFile(index: number) {
  resourceFiles.value.splice(index, 1);
}

async function submitSkill() {
  if (saving.value) return;
  if (!skillMarkdown.value.trim()) {
    saveError.value = "SKILL.md 不能为空";
    return;
  }
  const files = resourceFiles.value
    .filter((file) => file.path.trim())
    .map((file) => ({ path: file.path.trim(), content: file.content, encoding: file.encoding }));
  saving.value = true;
  saveError.value = "";
  try {
    const body = { content: skillMarkdown.value, files, enabled: formEnabled.value };
    const result = editing.value
      ? await updateSkillResource(editing.value.id, body)
      : await createSkillResource(body);
    editorOpen.value = false;
    await load();
    const sync = result.sync;
    if (sync.status === "applied") notify(editing.value ? "技能已保存并同步" : "技能已创建");
    else notify(`技能已保存，${sync.detail || "等待同步"}`, "info", 4200);
  } catch (reason) {
    saveError.value = reason instanceof Error ? reason.message : "技能保存失败";
  } finally {
    saving.value = false;
  }
}

async function toggleEnabled(row: SkillResource) {
  busyId.value = row.id;
  try {
    const result = await updateSkillResource(row.id, { enabled: !row.enabled });
    await load();
    notify(result.sync.status === "applied" ? `技能已${row.enabled ? "停用" : "启用"}` : `配置已更新，${result.sync.detail || "待同步"}`, result.sync.status === "applied" ? "success" : "info");
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : "更新失败", "error");
  } finally {
    busyId.value = "";
  }
}

async function retrySync(row: SkillResource) {
  busyId.value = row.id;
  try {
    const result = await syncSkillResource(row.id);
    await load();
    notify(result.sync.status === "applied" ? "同步完成" : `同步未完成：${result.sync.detail || "待重试"}`, result.sync.status === "applied" ? "success" : "error");
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : "同步失败", "error");
  } finally {
    busyId.value = "";
  }
}

async function removeSkill(row: SkillResource) {
  const confirmed = await confirmDialog({
    title: "删除技能",
    message: `确定删除「${row.name}」吗？被员工引用的技能不能删除。`,
    danger: true,
    confirmLabel: "删除",
  });
  if (!confirmed) return;
  busyId.value = row.id;
  try {
    await deleteSkillResource(row.id);
    await load();
    notify("技能已删除");
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : "删除失败", "error");
  } finally {
    busyId.value = "";
  }
}

async function openVersions(row: SkillResource) {
  versionsFor.value = row;
  versionItems.value = [];
  versionsOpen.value = true;
  try {
    const payload = await skillResourceVersions(row.id);
    versionItems.value = payload.versions;
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : "版本加载失败", "error");
  }
}

async function restore(version: number) {
  if (!versionsFor.value) return;
  const target = versionsFor.value;
  const confirmed = await confirmDialog({
    title: "恢复版本",
    message: `恢复 v${version} 会生成新版本并同步到已绑定员工，继续吗？`,
    confirmLabel: "恢复",
  });
  if (!confirmed) return;
  try {
    const result = await restoreSkillVersion(target.id, version);
    versionsOpen.value = false;
    await load();
    notify(result.sync.status === "applied" ? "版本已恢复并同步" : `版本已恢复，${result.sync.detail || "待同步"}`, result.sync.status === "applied" ? "success" : "info");
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : "恢复失败", "error");
  }
}

function triggerUpload() {
  directoryInput.value?.click();
}

async function onDirectoryPicked(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  input.value = "";
  if (!files.length) return;
  loading.value = true;
  try {
    const result = await uploadSkillDirectory(files, true);
    await load();
    notify(result.sync.status === "applied" ? "技能目录已上传" : `技能目录已上传，${result.sync.detail || "待同步"}`, result.sync.status === "applied" ? "success" : "info");
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : "技能目录上传失败", "error");
  } finally {
    loading.value = false;
  }
}

function formatTime(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

onMounted(load);
</script>

<style scoped>
.hidden-input { display: none; }
.name-cell { display: flex; flex-direction: column; gap: 2px; }
.name-cell small { color: var(--astra-text-muted); font-size: 11px; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.muted { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.sync-count { margin-left: 6px; color: var(--astra-text-muted); font-size: 11px; }
.sync-hint { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.form-note { margin-bottom: 14px; }
.code-editor :deep(textarea) { font-family: var(--astra-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace); font-size: 12px; line-height: 1.6; }
.file-head { display: flex; align-items: center; justify-content: space-between; margin: 4px 0 8px; font-weight: var(--astra-weight-bold); color: var(--astra-text-secondary); }
.file-empty { margin-bottom: 12px; }
.resource-file { display: flex; flex-direction: column; gap: 6px; padding: 10px; margin-bottom: 10px; border: 1px solid var(--astra-border); border-radius: 8px; background: var(--astra-bg-elevated); }
.resource-file__head { display: flex; align-items: center; gap: 8px; }
.drawer-footer { display: flex; justify-content: flex-end; gap: 8px; }
.detail { display: flex; flex-direction: column; gap: 12px; }
.detail h4 { margin: 6px 0 0; color: var(--astra-text-secondary); font-size: 13px; }
.detail-sync { margin-left: 8px; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.gap-tag { margin-right: 4px; }
.markdown-preview { margin: 0; padding: 12px; border-radius: 8px; background: var(--astra-bg-elevated); color: var(--astra-text-primary); font-family: var(--astra-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace); font-size: 12px; line-height: 1.6; white-space: pre-wrap; max-height: 360px; overflow: auto; }
.detail-file { display: flex; flex-direction: column; gap: 4px; }
.version-list { display: flex; flex-direction: column; gap: 8px; }
.version-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 10px; border: 1px solid var(--astra-border); border-radius: 8px; }
.version-row span { margin-left: 8px; }
</style>
