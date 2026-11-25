<template>
  <section class="admin-page admin-page--fill" data-testid="skill-resources-page">
    <SettingsHeaderActions>
      <ElButton :icon="RefreshCw" :loading="loading" data-testid="skills-refresh" @click="load">刷新</ElButton>
      <ElButton :icon="Upload" data-testid="skills-upload" @click="triggerUpload">上传目录</ElButton>
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
          placeholder="搜索名称或说明"
          clearable
          :prefix-icon="Search"
        />
      </div>
    </div>

    <div class="admin-page__table">
      <ElTable
        ref="tableRef"
        v-loading="loading"
        :data="paged"
        row-key="id"
        height="100%"
        aria-label="技能列表"
        element-loading-text="正在加载技能列表"
        data-testid="skills-table"
      >
        <ElTableColumn label="技能名称" min-width="210">
          <template #default="{ row }">
            <div class="name-cell">
              <div class="name-cell__top">
                <button type="button" class="name-link" :title="row.name" @click="openDetail(row as SkillResource)">{{ row.name }}</button>
                <span v-if="row.version" class="ver-pill">v{{ row.version }}</span>
              </div>
              <small v-if="row.description" class="name-desc" :title="row.description">{{ row.description }}</small>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="来源" width="112" align="center">
          <template #default="{ row }">
            <ElTag
              size="small"
              type="info"
              class="astra-tag--neutral"
            >{{ sourceLabel(row.source) }}</ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="关联员工" min-width="150">
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
      <ElTableColumn label="同步状态" width="126">
        <template #default="{ row }">
          <ElTooltip :content="row.sync.detail || syncLabel(row.sync.status)" placement="top">
            <ElTag
              size="small"
              :type="syncTagType(row.sync.status)"
              :class="{ 'astra-tag--neutral': syncTagType(row.sync.status) === 'info' }"
            >{{ syncLabel(row.sync.status) }}</ElTag>
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
            @change="toggleEnabled(row as SkillResource)"
          />
          <ElTag
            v-else
            size="small"
            :type="row.enabled ? 'success' : 'info'"
            :class="{ 'astra-tag--neutral': !row.enabled }"
          >{{ row.enabled ? '启用' : '停用' }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="操作" width="156" align="center" fixed="right">
        <template #default="{ row }">
          <div class="admin-actions">
            <ElButton link type="primary" @click="openDetail(row as SkillResource)">查看</ElButton>
            <ElButton v-if="row.editable" link type="primary" @click="beginEdit(row as SkillResource)">编辑</ElButton>
            <ElDropdown
              v-if="hasMoreActions(row as SkillResource)"
              trigger="click"
              :disabled="busyId === row.id"
              @command="(cmd) => onRowCommand(String(cmd), row as SkillResource)"
            >
              <ElButton link type="primary" :disabled="busyId === row.id">更多</ElButton>
              <template #dropdown>
                <ElDropdownMenu>
                  <ElDropdownItem v-if="row.source === 'nexus'" command="versions">版本历史</ElDropdownItem>
                  <ElDropdownItem v-if="row.employees.length && row.sync.status !== 'applied'" command="sync">重试同步</ElDropdownItem>
                  <ElDropdownItem v-if="row.editable" command="delete" divided class="danger-item">删除技能</ElDropdownItem>
                </ElDropdownMenu>
              </template>
            </ElDropdown>
          </div>
        </template>
      </ElTableColumn>
      <template #empty>
        <ElEmpty v-if="!loading" :image-size="64" :description="items.length ? '没有匹配的技能' : '暂无技能资源'" />
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
        aria-label="技能分页"
        data-testid="skills-pagination"
      />
    </div>

    <!-- 新增 / 编辑抽屉（支持 SKILL.md 实时分屏预览） -->
    <ElDrawer
      v-model="editorOpen"
      :title="editing ? `编辑技能 · ${editing.name}` : '新增技能'"
      size="min(920px, 100%)"
      :close-on-click-modal="false"
      :before-close="closeEditor"
    >
      <ElForm label-position="top" :disabled="saving" @submit.prevent="submitSkill">
        <ElFormItem label="SKILL.md" required>
          <div class="skill-md-box">
            <div class="skill-md-box__bar">
              <span class="skill-md-box__hint">使用 YAML Frontmatter 定义 `name` 与 `description`，正文编写技能指令</span>
              <ElRadioGroup v-model="editorView" size="small" aria-label="编辑视图">
                <ElRadioButton value="edit">编辑</ElRadioButton>
                <ElRadioButton value="split">分屏</ElRadioButton>
                <ElRadioButton value="preview">预览</ElRadioButton>
              </ElRadioGroup>
            </div>
            <div class="skill-md-box__panes" :class="`is-${editorView}`">
              <ElInput
                v-if="editorView !== 'preview'"
                v-model="skillMarkdown"
                type="textarea"
                :rows="16"
                resize="none"
                class="code-editor"
                spellcheck="false"
              />
              <div v-if="editorView !== 'edit'" class="skill-md-box__preview">
                <MarkdownView :content="skillMarkdown" strip-frontmatter compact empty-text="输入 SKILL.md 后在此预览排版效果" />
              </div>
            </div>
          </div>
        </ElFormItem>

        <div class="file-head">
          <span>附带资源文件（可选）</span>
          <ElButton size="small" :icon="Plus" @click="addResourceFile">添加文件</ElButton>
        </div>
        <div v-if="!resourceFiles.length" class="muted file-empty">暂无附带脚本或参考文档</div>
        <div v-for="(file, index) in resourceFiles" :key="index" class="resource-file">
          <div class="resource-file__head">
            <ElInput v-model="file.path" :aria-label="`资源文件 ${index + 1} 路径`" placeholder="相对路径，例如 references/guide.md" />
            <ElButton link type="danger" :aria-label="`移除资源文件 ${index + 1}`" @click="removeResourceFile(index)">移除</ElButton>
          </div>
          <ElInput
            v-model="file.content"
            :aria-label="`资源文件 ${index + 1} 内容`"
            type="textarea"
            :rows="5"
            class="code-editor-sub"
            :disabled="saving || file.encoding === 'base64'"
            :placeholder="file.encoding === 'base64' ? '二进制文件，保持原内容' : '文件内容'"
          />
        </div>
        <ElAlert v-if="saveError" type="error" show-icon :closable="false" :title="saveError" />
      </ElForm>
      <template #footer>
        <div class="drawer-footer">
          <ElSwitch v-model="formEnabled" active-text="启用此技能" />
          <div class="drawer-footer__right">
            <ElTag v-if="editorGuard.dirty.value" size="small" type="warning" effect="light">有未保存修改</ElTag>
            <ElButton :disabled="saving" @click="closeEditor">取消</ElButton>
            <ElButton type="primary" :loading="saving" @click="submitSkill">保存技能</ElButton>
          </div>
        </div>
      </template>
    </ElDrawer>

    <!-- 详情抽屉（完整 Markdown 渲染 + Frontmatter 拆解 + 附带文件阅读） -->
    <ElDrawer v-model="detailOpen" class="skill-detail-drawer" :title="viewing?.name || '技能详情'" size="min(880px, 100%)">
      <div v-if="viewing" class="detail">
        <div class="detail-meta-strip">
          <div class="detail-meta-strip__tags">
            <ElTag size="small" type="info" class="astra-tag--neutral">{{ sourceLabel(viewing.source) }}</ElTag>
            <span v-if="viewing.version" class="ver-pill">v{{ viewing.version }}</span>
            <ElTag
              size="small"
              :type="viewing.enabled ? 'success' : 'info'"
              :class="{ 'astra-tag--neutral': !viewing.enabled }"
            >{{ viewing.enabled ? '已启用' : '已停用' }}</ElTag>
            <ElTooltip :content="viewing.sync.detail || syncLabel(viewing.sync.status)" placement="top">
              <ElTag
                size="small"
                :type="syncTagType(viewing.sync.status)"
                :class="{ 'astra-tag--neutral': syncTagType(viewing.sync.status) === 'info' }"
              >{{ syncLabel(viewing.sync.status) }}</ElTag>
            </ElTooltip>
          </div>
          <div class="detail-meta-strip__right">
            <span v-if="viewing.runtime_name && viewing.runtime_name !== viewing.name" class="mono-id">
              {{ viewing.runtime_name }}
            </span>
            <ElButton v-if="viewing.editable" size="small" :icon="Pencil" @click="editFromDetail">编辑</ElButton>
          </div>
        </div>

        <p v-if="viewing.description" class="detail-lead">{{ viewing.description }}</p>

        <div class="detail-emps">
          <span class="detail-emps__label">关联员工</span>
          <div class="chips">
            <span v-for="employee in viewing.employees" :key="employee.id" class="emp-chip">{{ employee.name }}</span>
            <span v-if="!viewing.employees.length" class="muted">未绑定</span>
          </div>
        </div>

        <div class="detail-doc-card">
          <div class="detail-doc-card__head">
            <span class="mono-id">SKILL.md</span>
          </div>
          <div class="detail-doc-card__body">
            <MarkdownView
              :content="skillMdOf(viewing)"
              strip-frontmatter
              show-toc
              allow-source-toggle
              allow-copy
              empty-text="（未包含 SKILL.md 内容）"
            />
          </div>
        </div>

        <template v-if="resourceFilesOf(viewing).length">
          <h4 class="detail-section-title">附带资源文件（{{ resourceFilesOf(viewing).length }}）</h4>
          <div v-for="file in resourceFilesOf(viewing)" :key="file.path" class="detail-doc-card">
            <div class="detail-doc-card__head">
              <span class="mono-id">{{ file.path }}</span>
            </div>
            <div class="detail-doc-card__body">
              <p v-if="file.encoding === 'base64'" class="muted">二进制资源文件（不支持文本预览）</p>
              <MarkdownView
                v-else-if="isMarkdownPath(file.path)"
                :content="file.content || ''"
                compact
                allow-source-toggle
                allow-copy
              />
              <pre v-else class="file-code">{{ file.content }}</pre>
            </div>
          </div>
        </template>
      </div>
    </ElDrawer>

    <ElDialog v-model="versionsOpen" :title="versionsFor ? `版本历史 · ${versionsFor.name}` : '版本历史'" width="580px">
      <div v-if="versionsFor" class="version-list">
        <div v-for="version in versionItems" :key="version.version" class="version-row">
          <div class="version-row__info">
            <span class="ver-pill">v{{ version.version }}</span>
            <span class="version-row__note">{{ version.note || '常规更新' }}</span>
            <span class="muted">{{ formatTime(version.created_at) }}</span>
          </div>
          <ElButton
            link
            type="primary"
            :disabled="version.version === versionsFor.version"
            @click="restore(version.version)"
          >{{ version.version === versionsFor.version ? '当前版本' : '恢复此版本' }}</ElButton>
        </div>
        <ElEmpty v-if="!versionItems.length" :image-size="56" description="暂无版本记录" />
      </div>
    </ElDialog>
  </section>
</template>

<script setup lang="ts">
import { Pencil, Plus, RefreshCw, Search, Upload } from "lucide-vue-next";
import { computed, onMounted, ref, watch } from "vue";
import { onBeforeRouteLeave } from "vue-router";
import { useFormGuard } from "../../composables/useFormGuard";
import {
  ElAlert,
  ElButton,
  ElDialog,
  ElDrawer,
  ElDropdown,
  ElDropdownItem,
  ElDropdownMenu,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElOption,
  ElPagination,
  ElRadioButton,
  ElRadioGroup,
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
import MarkdownView from "@/components/astra/MarkdownView.vue";
import { confirmDialog } from "../../composables/useConfirm";
import { notify } from "../../composables/useToast";
import SettingsHeaderActions from "./SettingsHeaderActions.vue";

const DEFAULT_MARKDOWN = `---
name: 新技能
description: "简述该技能的适用场景"
---

## 适用场景

说明何时触发该技能。

## 执行步骤

1. 确认上下文信息
2. 输出结构化结果
`;

const items = ref<SkillResource[]>([]);
const loading = ref(false);
const error = ref("");
const keyword = ref("");
const sourceFilter = ref("");
const statusFilter = ref("");
const page = ref(1);
const pageSize = ref(20);
const tableRef = ref<TableInstance>();
const busyId = ref("");

const directoryInput = ref<HTMLInputElement | null>(null);

const editorOpen = ref(false);
const editorView = ref<"edit" | "split" | "preview">("split");
const editing = ref<SkillResource | null>(null);
const skillMarkdown = ref(DEFAULT_MARKDOWN);
const resourceFiles = ref<{ path: string; content: string; encoding: "utf8" | "base64" }[]>([]);
const formEnabled = ref(true);
const saving = ref(false);
const saveError = ref("");

const editorGuard = useFormGuard({
  isOpen: () => editorOpen.value,
  isSaving: () => saving.value,
  snapshot: () => JSON.stringify({ markdown: skillMarkdown.value, files: resourceFiles.value, enabled: formEnabled.value }),
  confirmDiscard: () => confirmDialog({
    title: "放弃未保存的技能修改？",
    message: "当前技能配置尚未保存，关闭后将无法恢复。",
    confirmLabel: "放弃并关闭",
    cancelLabel: "继续编辑",
    danger: true,
  }),
});
async function closeEditor() { if (await editorGuard.canClose()) editorOpen.value = false; }
onBeforeRouteLeave(() => editorGuard.canClose());

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
    const payload = await listSkillResources();
    items.value = payload.items;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "技能列表加载失败";
  } finally {
    loading.value = false;
  }
}

function sourceLabel(source: string) {
  return source === "nexus" ? "Nexus 自建" : "AgentScope";
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

function hasMoreActions(row: SkillResource) {
  return row.source === "nexus" || row.editable || (row.employees.length > 0 && row.sync.status !== "applied");
}

function onRowCommand(cmd: string, row: SkillResource) {
  if (cmd === "versions") void openVersions(row);
  else if (cmd === "sync") void retrySync(row);
  else if (cmd === "delete") void removeSkill(row);
}

function isMarkdownPath(path: string) {
  return /\.md$/i.test(path);
}

function beginCreate() {
  editing.value = null;
  skillMarkdown.value = DEFAULT_MARKDOWN;
  resourceFiles.value = [];
  formEnabled.value = true;
  saveError.value = "";
  editorView.value = window.innerWidth >= 820 ? "split" : "edit";
  editorOpen.value = true;
  editorGuard.capture();
}

async function beginEdit(row: SkillResource) {
  saveError.value = "";
  try {
    const { skill } = await getSkillResource(row.id);
    editing.value = skill;
    skillMarkdown.value = skillMdOf(skill);
    resourceFiles.value = filesOf(skill).filter((f) => f.path !== "SKILL.md").map((file) => ({
      path: file.path,
      content: file.content ?? "",
      encoding: file.encoding ?? "utf8",
    }));
    formEnabled.value = skill.enabled;
    editorView.value = window.innerWidth >= 820 ? "split" : "edit";
    editorOpen.value = true;
    editorGuard.capture();
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

function editFromDetail() {
  const target = viewing.value;
  detailOpen.value = false;
  if (target) void beginEdit(target);
}

function filesOf(skill: SkillResource): SkillFile[] {
  return skill.files ?? [];
}
function skillMdOf(skill: SkillResource) {
  return filesOf(skill).find((file) => file.path === "SKILL.md")?.content ?? "";
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
    notify(
      result.sync.status === "applied" ? `技能已${row.enabled ? "停用" : "启用"}` : `配置已更新，${result.sync.detail || "待同步"}`,
      result.sync.status === "applied" ? "success" : "info",
    );
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

function triggerUpload() { directoryInput.value?.click(); }

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
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

onMounted(load);
</script>

<style scoped>
.hidden-input { display: none; }
.name-cell { display: grid; gap: 3px; min-width: 0; }
.name-cell__top { display: flex; align-items: center; gap: 6px; min-width: 0; }
.name-link {
  padding: 0; border: 0; background: transparent; cursor: pointer;
  color: var(--astra-text-primary); font-size: 13.5px; font-weight: 600;
  text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.name-link:hover { color: var(--astra-accent-text); text-decoration: underline; }
.name-desc { color: var(--astra-text-muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ver-pill {
  flex: none; padding: 0 6px; border-radius: 5px;
  border: 1px solid var(--astra-border); background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary); font-family: var(--astra-font-mono); font-size: 11px; line-height: 18px;
}
.chips { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.emp-chip {
  max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  padding: 1px 7px; border-radius: 5px;
  border: 1px solid var(--astra-border); background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary); font-size: 12px; line-height: 18px;
}
.emp-more {
  padding: 1px 6px; border-radius: 5px; background: var(--astra-bg-elevated);
  color: var(--astra-text-secondary); font-size: 11px; font-weight: 500;
}
.muted { color: var(--astra-text-muted); font-size: 12.5px; }
.mono-id { font-family: var(--astra-font-mono); font-size: 12px; color: var(--astra-text-muted); }
.sync-count { margin-left: 6px; color: var(--astra-text-muted); font-size: 11px; }
.danger-item { color: var(--astra-danger); }

/* 编辑器分屏 */
.skill-md-box {
  display: grid; width: 100%;
  border: 1px solid var(--astra-border); border-radius: 10px; overflow: hidden;
  background: var(--astra-bg-panel);
}
.skill-md-box__bar {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;
  padding: 8px 12px; border-bottom: 1px solid var(--astra-border); background: var(--astra-bg-subtle);
}
.skill-md-box__hint { color: var(--astra-text-muted); font-size: 12px; }
.skill-md-box__panes { display: grid; min-height: 360px; max-height: 50vh; }
.skill-md-box__panes.is-split { grid-template-columns: 1fr 1fr; }
.code-editor :deep(textarea) {
  height: 100% !important; min-height: 360px; max-height: 50vh;
  border: 0; border-radius: 0; box-shadow: none !important; padding: 12px 14px;
  font-family: var(--astra-font-mono); font-size: 12.5px; line-height: 1.65;
}
.code-editor-sub :deep(textarea) { font-family: var(--astra-font-mono); font-size: 12px; line-height: 1.6; }
.skill-md-box__preview {
  height: 100%; min-height: 360px; max-height: 50vh; overflow-y: auto; padding: 14px 16px;
  background: var(--astra-bg-panel);
}
.skill-md-box__panes.is-split .skill-md-box__preview { border-left: 1px solid var(--astra-border); background: var(--astra-bg-subtle); }

.file-head { display: flex; align-items: center; justify-content: space-between; margin: 4px 0 8px; font-weight: 600; font-size: 13px; color: var(--astra-text-secondary); }
.file-empty { margin-bottom: 12px; }
.resource-file { display: flex; flex-direction: column; gap: 6px; padding: 10px; margin-bottom: 10px; border: 1px solid var(--astra-border); border-radius: 8px; background: var(--astra-bg-subtle); }
.resource-file__head { display: flex; align-items: center; gap: 8px; }
.drawer-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; }
.drawer-footer__right { display: flex; align-items: center; gap: 8px; }

/* 详情抽屉 */
.detail { display: flex; flex-direction: column; gap: 14px; }
.detail-meta-strip {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px 12px;
  padding: 10px 12px; border: 1px solid var(--astra-border); border-radius: 10px; background: var(--astra-bg-subtle);
}
.detail-meta-strip__tags { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.detail-meta-strip__right { display: flex; align-items: center; gap: 10px; }
.detail-lead { margin: 0; color: var(--astra-text-secondary); font-size: 13.5px; line-height: 1.6; }
.detail-emps { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.detail-emps__label { color: var(--astra-text-muted); font-size: 12px; font-weight: 600; }

.detail-doc-card { border: 1px solid var(--astra-border); border-radius: 10px; background: var(--astra-bg-panel); overflow: hidden; }
.detail-doc-card__head { padding: 8px 14px; border-bottom: 1px solid var(--astra-border-subtle); background: var(--astra-bg-subtle); }
.detail-doc-card__body { padding: 14px 16px; }
.detail-section-title { margin: 4px 0 0; color: var(--astra-text-primary); font-size: 13.5px; font-weight: 600; }
.file-code {
  margin: 0; padding: 12px; border-radius: 8px;
  background: var(--astra-bg-subtle); color: var(--astra-text-primary);
  font-family: var(--astra-font-mono); font-size: 12px; line-height: 1.6;
  white-space: pre-wrap; overflow-wrap: anywhere; max-height: 360px; overflow: auto;
}

.version-list { display: flex; flex-direction: column; gap: 8px; }
.version-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; border: 1px solid var(--astra-border); border-radius: 8px; }
.version-row__info { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; min-width: 0; }
.version-row__note { color: var(--astra-text-primary); font-size: 13px; }
</style>
