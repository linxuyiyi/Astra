<template>
  <section class="chat-page">
    <div class="workspace" :class="{ 'workspace--focus': !asideOpen }">
      <button v-if="mobileRailOpen || asideOpen" class="chat-mobile-mask" aria-label="关闭聊天侧栏" @click="mobileRailOpen = false; asideOpen = false" />
      <aside class="left-rail" :class="{ 'is-mobile-open': mobileRailOpen }" aria-label="会话列表">
        <button type="button" class="chat-mobile-only chat-mobile-close" @click="mobileRailOpen = false">关闭会话列表</button>
        <div class="chat-directory-head">
          <h1>会话</h1>
          <button type="button" class="chat-icon-button" aria-label="新建群组" title="新建群组" data-testid="chat-create-group" @click="openGroupDialog()">
            <Plus :size="18" :stroke-width="1.7" />
          </button>
        </div>
        <el-input v-model="search" class="chat-search" placeholder="搜索会话" aria-label="搜索联系人或群组" clearable>
          <template #prefix><Search :size="16" :stroke-width="1.7" /></template>
        </el-input>
        <div class="chat-filter-seg" role="tablist" aria-label="会话筛选" @keydown="moveTab">
          <button
            v-for="item in filterOptions"
            :key="item.value"
            type="button"
            role="tab"
            :id="`chat-filter-${item.value}`"
            :tabindex="filter === item.value ? 0 : -1"
            aria-controls="chat-directory-results"
            class="chat-filter-seg__item"
            :class="{ 'is-active': filter === item.value }"
            :aria-selected="filter === item.value"
            @click="filter = item.value"
          >{{ item.label }}</button>
        </div>
        <div id="chat-directory-results" class="chat-target-list" role="tabpanel" :aria-labelledby="`chat-filter-${filter}`">
          <p v-if="!directoryLoading && !directoryError && !filteredTargets.length" class="chat-list-note">没有匹配的会话</p>
          <div v-if="directoryError" class="chat-list-note" role="alert">
            <p>{{ directoryError }}</p>
            <el-button @click="loadDirectory">重试</el-button>
          </div>
          <button
            v-for="target in filteredTargets"
            :key="target.key"
            class="chat-target"
            :aria-current="target.key === selectedKey ? 'true' : undefined"
            type="button"
            @click="selectTarget(target.key)"
          >
            <EmployeeAvatar v-if="target.kind === 'contact'" :avatar="target.avatar" :size="36" :disabled="!target.enabled" />
            <span v-else class="chat-avatar" aria-hidden="true"><UsersRound :size="18" :stroke-width="1.6" /></span>
            <span class="chat-target__meta">
              <b>{{ target.name }}</b>
              <small>{{ summaries[target.key] || target.subtitle }}</small>
            </span>
          </button>
        </div>
        <div class="chat-directory-footer">
          <button type="button" class="chat-join-link" @click="joinDialogOpen = true">
            <LogIn :size="16" :stroke-width="1.7" /> 加入群组
          </button>
        </div>
      </aside>

      <article class="chat">
        <div v-if="activeTarget" class="chat-head">
          <button type="button" class="chat-mobile-only chat-directory-toggle" @click="mobileRailOpen = true; asideOpen = false">会话列表</button>
          <EmployeeAvatar v-if="activeTarget.kind === 'contact'" :avatar="activeTarget.avatar" :size="40" :disabled="!activeTarget.enabled" />
          <span v-else class="chat-avatar chat-avatar--head" aria-hidden="true"><UsersRound :size="20" :stroke-width="1.6" /></span>
          <div class="chat-head__copy">
            <h2 :title="activeTarget.name">{{ activeTarget.name }}</h2>
            <p class="chat-head__desc muted">{{ activeTarget.subtitle }}<template v-if="!activeTarget.enabled"> · 已禁用</template></p>
          </div>
          <div class="chat-head__actions">
            <el-dropdown
              v-if="activeTarget.kind === 'group' && canManage"
              trigger="click"
              @command="onChatHeadCommand"
            >
              <button type="button" class="chat-icon-button" aria-label="更多操作" title="更多操作" data-testid="chat-head-more"><Ellipsis :size="19" :stroke-width="1.7" /></button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="group-settings">群组设置</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <button type="button" class="chat-profile-toggle" :aria-expanded="asideOpen" @click="toggleAside">
              <PanelRight :size="17" :stroke-width="1.6" /> {{ asideOpen ? "收起资料" : "查看资料" }}
            </button>
          </div>
        </div>
        <div v-else class="chat-head"><h2>数字员工</h2></div>

        <div v-if="directoryLoading || bindingLoading" class="chat-start" role="status">正在读取会话…</div>
        <div v-else-if="bindingError" class="chat-start" role="alert">
          {{ bindingError }} <el-button size="small" @click="selectTarget(selectedKey, { retry: true })">重试</el-button>
        </div>
        <ConversationPanel
          v-else-if="conversation"
          :key="JSON.stringify([conversation.kind, conversation.id, conversation.viewerId, conversation.title, conversation.enabled, conversation.avatar])"
          :conversation="conversation"
        />
        <div v-else class="chat-start">选择联系人或群组开始对话</div>

      </article>

      <aside v-if="activeTarget && asideOpen" class="chat-aside" aria-label="会话资料">
        <button type="button" class="chat-mobile-only chat-mobile-close" @click="asideOpen = false">关闭资料</button>
        <div class="right-tabs" role="tablist" aria-label="侧栏" @keydown="moveTab">
          <button
            v-for="item in rightTabs"
            :key="item.value"
            type="button"
            role="tab"
            :id="`chat-aside-${item.value}`"
            :tabindex="tab === item.value ? 0 : -1"
            aria-controls="chat-aside-content"
            class="chat-aside-tab"
            :class="{ 'is-active': tab === item.value }"
            :aria-selected="tab === item.value"
            @click="tab = item.value"
          >{{ item.label }}</button>
        </div>

        <div id="chat-aside-content" role="tabpanel" :aria-labelledby="`chat-aside-${tab}`">
        <div v-if="tab === 'tasks'" style="padding: 16px">
          <div v-if="taskError" class="muted" style="margin-bottom: 10px">{{ taskError }}</div>
          <div v-if="tasksLoading && !tasks.length" class="muted">正在读取关联任务…</div>
          <TaskRail v-else :tasks="tasks" :names="nameById" hide-responsible empty-text="暂无关联任务" />
        </div>

        <div v-else-if="tab === 'materials' && activeTarget?.kind === 'contact'" style="padding: 16px">
          <div class="section-label">职责范围</div>
          <div class="detail-excerpt">{{ activeTarget.employee?.description }}</div>
          <div class="section-label">工作指令</div>
          <div class="detail-excerpt">{{ activeTarget.employee?.instructions }}</div>
          <div class="section-label">知识</div>
          <div class="resource-list">
            <span v-for="item in activeTarget.employee?.knowledge || []" :key="item" class="mini-chip">{{ resourceLabel(catalogs.knowledge, item) }}</span>
            <span v-if="!activeTarget.employee?.knowledge.length" class="muted">暂无</span>
          </div>
          <div class="section-label">技能</div>
          <div class="resource-list">
            <span v-for="item in activeTarget.employee?.skills || []" :key="item" class="mini-chip">{{ resourceLabel(catalogs.skills, item) }}</span>
            <span v-if="!activeTarget.employee?.skills.length" class="muted">暂无</span>
          </div>
          <div class="section-label">MCP</div>
          <div class="resource-list">
            <span v-for="item in activeTarget.employee?.mcps || []" :key="item" class="mini-chip">{{ resourceLabel(catalogs.mcps, item) }}</span>
            <span v-if="!activeTarget.employee?.mcps.length" class="muted">暂无</span>
          </div>
          <div class="section-label">工具</div>
          <div class="resource-list">
            <span v-for="item in activeTarget.employee?.tools || []" :key="item" class="mini-chip">{{ toolLabel(item) }}</span>
            <span v-if="!activeTarget.employee?.tools.length" class="muted">暂无</span>
          </div>
        </div>

        <div v-else-if="tab === 'members' && activeTarget?.kind === 'group'" style="padding: 0 16px 16px">
          <div class="member-actions">
            <b>群组成员</b>
            <el-button v-if="canManage" size="small" @click="inviteHuman">邀请人类成员</el-button>
          </div>
          <div v-for="person in activeTarget.channel?.members || []" :key="`${person.member_type}:${person.member_id}`" class="member">
            <EmployeeAvatar v-if="person.member_type === 'employee'" :avatar="person.avatar" :size="36" />
            <span v-else class="avatar" aria-hidden="true"><UserRound :size="16" /></span>
            <div class="member-identity">
              <b class="member-name">{{ person.name }}{{ person.member_id === currentUid() ? '（你）' : '' }}</b>
              <small class="member-role muted">{{ person.member_type === 'employee' ? '数字员工' : `${roleLabels[person.role]} · ${person.online ? '在线' : '离线'}` }}</small>
            </div>
            <el-dropdown
              v-if="person.member_type === 'human' && person.role !== 'owner' && canManage"
              trigger="click"
              @command="onMemberCommand"
            >
              <button type="button" class="member-more" aria-label="成员操作" data-testid="member-more">⋯</button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item
                    v-if="activeTarget.channel?.my_role === 'owner' && person.role !== 'admin'"
                    :command="`role:admin:${person.member_id}`"
                  >设为管理员</el-dropdown-item>
                  <el-dropdown-item
                    v-if="activeTarget.channel?.my_role === 'owner' && person.role !== 'member'"
                    :command="`role:member:${person.member_id}`"
                  >设为成员</el-dropdown-item>
                  <el-dropdown-item
                    v-if="person.member_id !== currentUid()"
                    :command="`remove:${person.member_id}`"
                    divided
                  >移除</el-dropdown-item>
                  <el-dropdown-item
                    v-if="activeTarget.channel?.my_role === 'owner'"
                    :command="`transfer:${person.member_id}`"
                  >转让群主</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
          <el-button v-if="activeTarget.channel?.my_role !== 'owner'" type="danger" plain size="small" @click="leaveGroup">退出群组</el-button>

        </div>

        <div v-else-if="activeTarget?.kind === 'group'" style="padding: 16px">
          <div class="section-label">群组名称</div>
          <div class="detail-excerpt">{{ activeTarget.channel?.name }}</div>
          <div class="section-label">目标</div>
          <div class="detail-excerpt">{{ activeTarget.channel?.description || "暂无" }}</div>
          <div class="section-label">成员</div>
          <div class="detail-excerpt">{{ (activeTarget.channel?.members || []).map((item) => item.name).join("、") || "暂无" }}</div>
        </div>
        </div>
      </aside>
    </div>

    <el-dialog v-model="inviteDialogOpen" title="邀请加入群组" width="520px">
      <p>24 小时有效 · 仅限一次</p>
      <el-input :model-value="inviteLink" readonly aria-label="群邀请链接" />
      <template #footer><el-button @click="copyInvite">复制链接</el-button></template>
    </el-dialog>
    <el-dialog v-model="joinDialogOpen" title="加入群组" width="520px">
      <el-input v-model="joinToken" placeholder="粘贴邀请链接或邀请码" aria-label="邀请码" />
      <template #footer><el-button type="primary" @click="joinGroup">加入</el-button></template>
    </el-dialog>
    <el-dialog v-model="groupDialogOpen" :title="groupEditingId ? '编辑群组' : '创建群组'" width="520px">
      <el-form label-position="top">
        <el-form-item label="群组名称" required><el-input v-model="groupForm.name" maxlength="32" /></el-form-item>
        <el-form-item label="群组目标" required><el-input v-model="groupForm.description" type="textarea" :rows="3" maxlength="2000" show-word-limit /></el-form-item>
        <el-form-item v-if="groupEditingId" label="已有成员">
          <div class="resource-list">
            <span v-for="employee in employees.filter((item) => groupExistingMemberIds.includes(item.id))" :key="employee.id" class="mini-chip"><EmployeeAvatar :avatar="employee.avatar" :size="24" /> {{ employee.name }}</span>
          </div>
        </el-form-item>
        <el-form-item :label="groupEditingId ? '添加数字员工' : '参与的数字员工'">
          <el-checkbox-group v-model="groupForm.memberIds">
            <el-checkbox v-for="employee in employees" :key="employee.id" :value="employee.id">
              <EmployeeAvatar :avatar="employee.avatar" :size="24" /> {{ employee.name }} · {{ employee.domain }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="groupDialogOpen = false">取消</el-button>
        <el-button type="primary" :loading="groupSaving" @click="saveGroup">{{ groupEditingId ? "保存群组" : "创建群组" }}</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { workerDisplayName } from '@/utils/workerName'
import EmployeeAvatar from "../components/EmployeeAvatar.vue";
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import {
  createConversation,
  currentUid,
  getKnowledge,
  groupAction,
  getMcps,
  getSkills,
  getTools,
  listChannels,
  listEmployees,
  saveChannel,
} from "@/api";
import { Ellipsis, LogIn, PanelRight, Plus, Search, UserRound, UsersRound } from "lucide-vue-next";
import TaskRail from "@/components/TaskRail.vue";
import { listTasks, listWorkers, type PlatformTask, type PlatformWorker } from "@/platform";
import type { Channel, Employee, ResourceCatalog } from "@/types";
import ConversationPanel, { loadConversationBundle } from "@/features/chat/ConversationPanel.vue";
import { shortText } from "@/features/chat/shortText";
import type { ConversationProps } from "../../../shared/conversation";

interface ChatTarget {
  key: string;
  kind: "contact" | "group";
  id: string;
  name: string;
  subtitle: string;
  avatar: string;
  enabled: boolean;
  searchText: string;
  employee?: Employee;
  channel?: Channel;
}

const route = useRoute();
const router = useRouter();
const employees = ref<Employee[]>([]);
const directoryLoading = ref(true);
const directoryError = ref("");
const channels = ref<Channel[]>([]);
const workers = ref<PlatformWorker[]>([]);
const search = ref("");
const filter = ref<"all" | "contact" | "group">("all");
const filterOptions = [
  { value: "all" as const, label: "全部" },
  { value: "contact" as const, label: "联系人" },
  { value: "group" as const, label: "群组" },
];
function moveTab(event: KeyboardEvent) {
  const tabs = Array.from((event.currentTarget as HTMLElement).querySelectorAll<HTMLButtonElement>('[role=tab]'));
  const index = tabs.indexOf(event.target as HTMLButtonElement);
  if (index < 0) return;
  const next = {
    ArrowLeft: (index + tabs.length - 1) % tabs.length,
    ArrowRight: (index + 1) % tabs.length,
    Home: 0,
    End: tabs.length - 1,
  }[event.key as "ArrowLeft" | "ArrowRight" | "Home" | "End"];
  if (next === undefined) return;
  event.preventDefault();
  tabs[next].focus();
  tabs[next].click();
}
const selectedKey = ref("");
const summaries = reactive<Record<string, string>>({});
const bindingId = ref("");
const bindingLoading = ref(false);
const bindingError = ref("");
const tab = ref<"tasks" | "members" | "materials">("materials");
const tasks = ref<PlatformTask[]>([]);
const tasksLoading = ref(false);
const taskError = ref("");
const asideOpen = ref(false);
function toggleAside() {
  asideOpen.value = !asideOpen.value;
}
const mobileRailOpen = ref(false);
const groupDialogOpen = ref(false);
const groupEditingId = ref<string | null>(null);
const groupSaving = ref(false);
const groupExistingMemberIds = ref<string[]>([]);
const roleLabels = { owner: "群主", admin: "管理员", member: "成员" };
const canManage = computed(() => ["owner", "admin"].includes(activeTarget.value?.channel?.my_role || ""));
const inviteDialogOpen = ref(false);
const inviteLink = ref("");
const joinDialogOpen = ref(false);
const joinToken = ref("");
const groupForm = reactive({ name: "", description: "", memberIds: [] as string[] });
const emptyCatalog = (): ResourceCatalog => ({ available: false, items: [] });
const catalogs = ref({
  skills: emptyCatalog(),
  mcps: emptyCatalog(),
  knowledge: emptyCatalog(),
  tools: emptyCatalog(),
});
let taskTimer = 0;
let selectionGeneration = 0;
let disposed = false;
let taskGeneration = 0;

const targets = computed<ChatTarget[]>(() => {
  const list: ChatTarget[] = [];
  for (const employee of employees.value) {
    list.push({
      key: `contact:${employee.id}`,
      kind: "contact",
      id: employee.id,
      name: employee.name,
      subtitle: employee.domain,
      avatar: employee.avatar,
      enabled: employee.enabled,
      searchText: `${employee.name} ${employee.domain} ${employee.description}`.toLowerCase(),
      employee,
    });
  }
  for (const channel of channels.value) {
    const members = channel.members
      .map((member) => employees.value.find((item) => item.id === member.employee_id))
      .filter((item): item is Employee => Boolean(item));
    list.push({
      key: `group:${channel.id}`,
      kind: "group",
      id: channel.id,
      name: channel.name,
      subtitle: `${channel.members.filter(m => m.member_type === "human").length} 位人类 · ${channel.members.filter(m => m.member_type === "employee").length} 位数字员工`,
      avatar: "",
      enabled: true,
      searchText: `${channel.name} ${channel.description} ${members.map((item) => item.name).join(" ")}`.toLowerCase(),
      channel,
    });
  }
  return list.sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
});

const filteredTargets = computed(() => {
  const needle = search.value.trim().toLowerCase();
  return targets.value.filter((target) => {
    if (filter.value !== "all" && target.kind !== filter.value) return false;
    return !needle || target.searchText.includes(needle);
  });
});

const activeTarget = computed(() => targets.value.find(item => item.key === selectedKey.value));
const conversation = computed<ConversationProps | undefined>(() => {
  const target = activeTarget.value;
  if (!target || !bindingId.value) return;
  return {
    kind: target.kind === "contact" ? "private" : "group",
    id: bindingId.value,
    viewerId: currentUid(),
    title: target.name,
    avatar: target.avatar,
    enabled: target.enabled,
    onActivity(summary) { summaries[target.key] = summary; },
    onMembersChanged() { void refreshGroups().catch(() => undefined); },
  };
});

const rightTabs = computed(() => activeTarget.value?.kind === "group"
  ? [{ value: "tasks" as const, label: "任务" }, { value: "members" as const, label: "成员" }, { value: "materials" as const, label: "资料" }]
  : [{ value: "tasks" as const, label: "任务" }, { value: "materials" as const, label: "资料" }]);

const nameById = computed(() => {
  const names: Record<string, string> = {};
  for (const employee of employees.value) names[employee.id] = employee.name;
  for (const worker of workers.value) names[worker.id] = workerDisplayName(worker);
  return names;
});

function resourceLabel(catalog: ResourceCatalog, id: string) {
  return catalog.items.find((item) => item.id === id)?.name ?? id;
}

function toolLabel(id: string) {
  if (id === "contact_employee") return "联系其他数字员工";
  const name = resourceLabel(catalogs.value.tools, id);
  return name === id ? "工作区工具" : name;
}

async function loadTasks() {
  const target = activeTarget.value;
  if (!target || !bindingId.value || !asideOpen.value || tab.value !== "tasks") return;
  const generation = ++taskGeneration;
  const query: Record<string, string> = { source: target.kind === "contact" ? "private_chat" : "group_chat", external_task_id: bindingId.value };
  if (target.kind === "contact") query.origin_employee_id = target.id;
  tasksLoading.value = true;
  try {
    const result = await listTasks(query);
    if (generation !== taskGeneration) return;
    tasks.value = result;
    taskError.value = "";
  } catch (error) {
    if (generation === taskGeneration) taskError.value = shortText(error, "关联任务加载失败");
  } finally {
    if (generation === taskGeneration) tasksLoading.value = false;
  }
}

function updateRoute(target: ChatTarget, replace = false) {
  const query = target.kind === "contact" ? { employee: target.id } : { group: target.id };
  const location = { path: "/chat", query };
  if (replace) void router.replace(location);
  else void router.push(location);
}

async function selectTarget(key: string, options: { fromRoute?: boolean; retry?: boolean } = {}) {
  mobileRailOpen.value = false;
  const target = targets.value.find((item) => item.key === key);
  if (!target) return;
  if (!options.retry && selectedKey.value === key && (bindingId.value || bindingLoading.value)) return;
  const generation = ++selectionGeneration;
  ++taskGeneration;
  selectedKey.value = key;
  bindingId.value = "";
  bindingError.value = "";
  bindingLoading.value = true;
  tasks.value = [];
  taskError.value = "";
  tasksLoading.value = false;
  tab.value = target.kind === "group" ? "members" : "materials";
  localStorage.setItem(`nexus_chat_last:${currentUid()}`, key);
  if (!options.fromRoute && routeTargetKey() !== key) updateRoute(target);
  try {
    const id = target.kind === "group" ? target.id : (await createConversation(target.id)).id;
    if (generation === selectionGeneration) bindingId.value = id;
  } catch (error) {
    if (generation === selectionGeneration) bindingError.value = shortText(error, "无法打开对话");
  } finally {
    if (generation === selectionGeneration) bindingLoading.value = false;
  }
}

function routeTargetKey() {
  const group = typeof route.query.group === "string" ? route.query.group : "";
  const employee = typeof route.query.employee === "string" ? route.query.employee : "";
  if (group && targets.value.some((item) => item.key === `group:${group}`)) return `group:${group}`;
  if (employee && targets.value.some((item) => item.key === `contact:${employee}`)) return `contact:${employee}`;
  return "";
}

async function applyRouteTarget() {
  const key = routeTargetKey();
  if (!key || key === selectedKey.value) return;
  await selectTarget(key, { fromRoute: true });
}

function onChatHeadCommand(command: string) {
  if (command === "group-settings" && activeTarget.value?.kind === "group") {
    openGroupDialog(activeTarget.value.channel);
  }
}

function onMemberCommand(command: string | number) {
  const raw = String(command);
  const [action, a, b] = raw.split(":");
  if (action === "role" && a && b) void setRole(b, a);
  else if (action === "remove" && a) void removeHuman(a);
  else if (action === "transfer" && a) void transferOwner(a);
}

function openGroupDialog(channel?: Channel) {
  groupEditingId.value = channel?.id ?? null;
  groupExistingMemberIds.value = channel?.members.filter(item => item.member_type === "employee").map((item) => item.employee_id) ?? [];
  groupForm.name = channel?.name ?? "";
  groupForm.description = channel?.description ?? "";
  groupForm.memberIds = channel
    ? channel.members.filter(item => item.member_type === "employee").map((item) => item.employee_id)
    : employees.value.filter((item) => item.enabled).map((item) => item.id);
  groupDialogOpen.value = true;
}

async function saveGroup() {
  if (!groupForm.name.trim() || !groupForm.description.trim()) {
    ElMessage.warning("请填写群组名称和目标");
    return;
  }
  groupSaving.value = true;
  const wasEditing = Boolean(groupEditingId.value);
  try {
    const saved = await saveChannel(
      { name: groupForm.name.trim(), description: groupForm.description.trim(), member_ids: groupForm.memberIds },
      groupEditingId.value ?? undefined,
    );
    groupDialogOpen.value = false;
    channels.value = await listChannels();
    const key = `group:${saved.id}`;
    await selectTarget(key);
    ElMessage.success(wasEditing ? "群组已保存" : "群组已创建");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存失败");
  } finally {
    groupSaving.value = false;
  }
}

watch(() => [route.query.employee, route.query.group], () => { void applyRouteTarget(); });
watch([asideOpen, tab, bindingId], () => { void loadTasks(); });
watch(asideOpen, open => { if (open) void loadAsideReferences(); });

async function refreshGroups() {
  channels.value = await listChannels();
}
async function act(path: string, method = "POST", body?: unknown) {
  try { await groupAction(path, method, body); await refreshGroups(); }
  catch (error) { ElMessage.error(error instanceof Error ? error.message : "群组操作失败"); }
}
async function inviteHuman() {
  try {
    const result = await groupAction<{ token: string }>(`${activeTarget.value!.id}/invitations`);
    inviteLink.value = `${location.origin}/chat?invite=${encodeURIComponent(result.token)}`;
    inviteDialogOpen.value = true;
  } catch (error) { ElMessage.error(String(error)); }
}
async function copyInvite() {
  try { await navigator.clipboard.writeText(inviteLink.value); ElMessage.success("邀请链接已复制"); }
  catch { ElMessage.info("请选中链接复制"); }
}
async function joinGroup() {
  try {
    let token = joinToken.value.trim();
    if (token.includes("?")) token = new URL(token).searchParams.get("invite") || "";
    const result = await groupAction<Channel>("join", "POST", { token });
    joinDialogOpen.value = false;
    await refreshGroups();
    await selectTarget(`group:${result.id}`);
    filter.value = "group";
    await router.replace({ path: "/chat", query: { group: result.id } });
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "邀请无效"); }
}
function setRole(uid: string, role: string) { void act(`${activeTarget.value!.id}/humans/${uid}`, "PATCH", { role }); }
function removeHuman(uid: string) { void act(`${activeTarget.value!.id}/humans/${uid}`, "DELETE"); }
async function transferOwner(uid: string) { await act(`${activeTarget.value!.id}/transfer/${uid}`); }
async function leaveGroup() {
  await act(`${activeTarget.value!.id}/leave`);
  await loadDirectory();
}
let asideRequested = false;
async function loadAsideReferences() {
  if (asideRequested) return;
  asideRequested = true;
  const [skills, mcps, knowledge, tools, workerList] = await Promise.all([
    getSkills().catch(() => emptyCatalog()),
    getMcps().catch(() => emptyCatalog()),
    getKnowledge().catch(() => emptyCatalog()),
    getTools().catch(() => emptyCatalog()),
    listWorkers().catch(() => []),
  ]);
  if (disposed) return;
  catalogs.value = { skills, mcps, knowledge, tools };
  workers.value = workerList;
}

async function loadDirectory() {
  directoryLoading.value = true;
  directoryError.value = "";
  try {
    const [employeeList, channelList] = await Promise.all([listEmployees(), listChannels()]);
    if (disposed) return;
    employees.value = employeeList;
    channels.value = channelList;
    const routeKey = routeTargetKey();
    const stored = localStorage.getItem(`nexus_chat_last:${currentUid()}`) ?? "";
    const storedTarget = stored && targets.value.some((item) => item.key === stored) ? stored : "";
    const initial = routeKey || storedTarget || targets.value[0]?.key;
    if (initial) await selectTarget(initial, { fromRoute: Boolean(routeKey) });
  } catch (error) {
    if (!disposed) directoryError.value = shortText(error, "会话列表加载失败");
  } finally {
    if (!disposed) directoryLoading.value = false;
  }
}

onMounted(async () => {
  void loadConversationBundle().catch(() => undefined);
  if (typeof route.query.invite === "string") { joinToken.value = route.query.invite; joinDialogOpen.value = true; }
  await loadDirectory();
  if (disposed) return;
  taskTimer = window.setInterval(() => {
    void loadTasks();
    if (activeTarget.value?.kind === "group") void refreshGroups().catch(() => undefined);
  }, 8000);
});

onBeforeUnmount(() => {
  disposed = true;
  ++selectionGeneration;
  ++taskGeneration;
  window.clearInterval(taskTimer);
});
</script>

<style scoped>
.chat-page {
  --chat-bar-height: 60px;
  --chat-rail: #f6f7f5;
  --chat-canvas: #fff;
  --chat-wash: #f0f3ef;
  --chat-hover: #e9ede8;
  --chat-selected: #e3ebe5;
  --chat-ink: #1e2924;
  --chat-text: #435149;
  --chat-muted: #606d65;
  --chat-line: #e1e6e1;
  --chat-accent: var(--astra-primary-bright);
  display: flex;
  flex-direction: column;
  min-height: 0;
  width: 100%;
  overflow: hidden;
  border: 1px solid var(--astra-border);
  border-radius: 14px;
  background: var(--chat-canvas);
  color: var(--chat-ink);
}
.workspace {
  display: grid;
  flex: 1;
  grid-template-columns: 268px minmax(0, 1fr) 300px;
  min-height: 0;
}
.workspace--focus { grid-template-columns: 268px minmax(0, 1fr); }
.left-rail {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
  border-inline-end: 1px solid var(--chat-line);
  background: var(--chat-rail);
}
.chat-directory-head {
  min-height: var(--chat-bar-height);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px 0 20px;
}
.chat-directory-head h1 {
  font-size: 17px;
  font-weight: 650;
  margin: 0;
}
.chat-icon-button,
.chat-profile-toggle,
.member-more {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  height: 36px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--chat-text);
  cursor: pointer;
}
.chat-icon-button, .member-more { width: 36px; padding: 0; }
.chat-directory-head .chat-icon-button { background: var(--chat-hover); color: var(--chat-ink); }
.chat-icon-button:hover, .chat-profile-toggle:hover, .member-more:hover { background: var(--chat-wash); color: var(--chat-ink); }
.chat-directory-head .chat-icon-button:hover { background: var(--chat-selected); }
.chat-search { width: auto; margin: 0 16px; }
.chat-search :deep(.el-input__wrapper) {
  min-height: 36px;
  background: var(--chat-canvas);
  border-radius: 8px;
  box-shadow: 0 0 0 1px var(--chat-line) inset;
}
.chat-search :deep(.el-input__wrapper.is-focus) { box-shadow: 0 0 0 1px var(--chat-accent) inset; }
.chat-search :deep(.el-input__prefix) { color: var(--chat-muted); }
.chat-filter-seg {
  display: flex;
  gap: 4px;
  margin: 14px 16px 6px;
  border-bottom: 1px solid var(--chat-line);
}
.chat-filter-seg__item {
  min-height: 36px;
  padding: 0 10px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--chat-muted);
  cursor: pointer;
  font-size: 13px;
  white-space: nowrap;
}
.chat-filter-seg__item:hover { color: var(--chat-ink); }
.chat-filter-seg__item.is-active {
  color: var(--chat-accent);
  border-bottom-color: var(--chat-accent);
  font-weight: 600;
}
.chat-target-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 4px 8px;
}
.chat-list-note { margin: 8px; color: var(--chat-muted); font-size: 13px; }
.chat-target {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  min-height: 60px;
  margin-bottom: 4px;
  padding: 8px 12px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--chat-ink);
  text-align: left;
  cursor: pointer;
}
.chat-target:hover { background: var(--chat-hover); }
.chat-target[aria-current] { background: var(--chat-selected); }
.chat-target:focus-visible { outline-offset: -2px; }
.chat-target__meta {
  display: grid;
  min-width: 0;
  flex: 1;
  gap: 2px;
  line-height: 1.45;
}
.chat-target__meta b, .chat-target__meta small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chat-target__meta b { font-size: 14px; font-weight: 600; }
.chat-target__meta small { font-size: 12px; color: var(--chat-muted); }
.chat-directory-footer { padding: 8px 16px 12px; border-top: 1px solid var(--chat-line); }
.chat-join-link {
  display: flex;
  width: 100%;
  gap: 10px;
  align-items: center;
  min-height: 36px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  padding: 0 8px;
  color: var(--chat-text);
  font-size: 13px;
  cursor: pointer;
}
.chat-join-link:hover { background: var(--chat-hover); }
.chat-avatar {
  display: inline-grid;
  place-items: center;
  flex: none;
  width: 36px;
  height: 36px;
  border-radius: 9px;
  background: var(--chat-hover);
  color: var(--chat-text);
}
.chat-avatar--head { width: 40px; height: 40px; background: var(--chat-wash); }
.chat {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}
.chat-head {
  display: flex;
  align-items: center;
  flex: none;
  gap: 12px;
  min-height: var(--chat-bar-height);
  padding: 8px clamp(20px, 2.2vw, 32px);
  border-bottom: 1px solid var(--chat-line);
}
.chat-head__copy { flex: 1; min-width: 0; }
.chat-head h2 {
  margin: 0;
  color: var(--chat-ink);
  font-size: 18px;
  font-weight: 650;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chat-head__desc {
  margin: 2px 0 0;
  color: var(--chat-muted);
  font-size: 13px;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chat-head__actions { display: flex; flex: none; gap: 6px; align-items: center; }
.chat-profile-toggle { gap: 8px; padding: 0 10px; font-size: 13px; }
.chat-profile-toggle[aria-expanded="true"] { background: var(--chat-wash); color: var(--chat-accent); }
.chat-start {
  display: flex;
  flex: 1;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 24px;
  color: var(--chat-muted);
}
.chat-aside {
  min-height: 0;
  overflow: auto;
  border-inline-start: 1px solid var(--chat-line);
  background: var(--chat-canvas);
}
.right-tabs {
  display: flex;
  align-items: flex-end;
  gap: 16px;
  min-height: var(--chat-bar-height);
  padding: 0 16px;
  border-bottom: 1px solid var(--chat-line);
}
.chat-aside-tab {
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--chat-text);
  min-height: 36px;
  padding: 0 2px;
  font-size: 13px;
  cursor: pointer;
}
.chat-aside-tab.is-active { color: var(--chat-accent); border-bottom-color: var(--chat-accent); font-weight: 600; }
.chat-aside .section-label { color: var(--chat-ink); font-size: 13px; font-weight: 600; }
.chat-aside .detail-excerpt { color: var(--chat-text); font-size: 13px; line-height: 1.65; }
.member-actions { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding-top: 12px; }
.member { flex-wrap: nowrap; }
.member-identity { flex: 1; min-width: 0; overflow-wrap: anywhere; display: grid; gap: 2px; }
.member-name { font-size: 13px; font-weight: 600; }
.member-role { font-size: 12px; }
.chat-mobile-only, .chat-mobile-mask { display: none; }
@media (max-width: 1000px) {
  .chat-page { border-radius: 0; }
  .workspace, .workspace--focus { position: relative; grid-template-columns: minmax(0, 1fr); }
  .left-rail { display: none; }
  .chat-mobile-only { display: inline-flex; align-items: center; border: 1px solid var(--chat-line); background: var(--chat-canvas); color: var(--chat-ink); border-radius: 8px; padding: 7px 9px; cursor: pointer; }
  .chat-mobile-close { align-self: start; margin: 12px 16px 0; }
  .left-rail.is-mobile-open, .chat-aside { display: flex; flex-direction: column; position: absolute; z-index: 20; top: 0; bottom: 0; width: min(320px, 92%); box-shadow: 0 0 24px #1e29241a; }
  .left-rail.is-mobile-open { left: 0; }
  .chat-aside { right: 0; }
  .chat-mobile-mask { display: block; position: absolute; z-index: 19; inset: 0; border: 0; background: #1e292433; }
  .chat-head { gap: 8px; padding-inline: 12px; }
}
</style>
