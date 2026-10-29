<template>
  <section class="chat-page">
    <div class="workspace" :class="{ 'workspace--focus': !asideOpen }">
      <aside class="panel left-rail">
        <div class="chat-search-row">
          <el-input v-model="search" placeholder="搜索联系人 / 群组" clearable />
          <el-tooltip content="新建群组" placement="top">
            <el-button circle aria-label="新建群组" data-testid="chat-create-group" @click="openGroupDialog()">＋</el-button>
          </el-tooltip>
        </div>
        <el-button text size="small" @click="joinDialogOpen = true">通过邀请加入群组</el-button>
        <div class="chat-filters">
          <button
            v-for="item in filterOptions"
            :key="item.value"
            type="button"
            class="chip"
            :class="{ sel: filter === item.value }"
            @click="filter = item.value"
          >{{ item.label }}</button>
        </div>
        <div class="chat-counts muted">{{ filteredTargets.length }} 个会话</div>
        <el-empty v-if="!directoryLoading && !directoryError && !filteredTargets.length" description="没有匹配的联系人或群组" :image-size="56" />
        <div v-if="directoryError" role="alert" style="padding: 12px">
          <p>{{ directoryError }}</p>
          <el-button @click="loadDirectory">重试</el-button>
        </div>
        <button
          v-for="target in filteredTargets"
          :key="target.key"
          class="rail-item chat-target"
          :class="{ active: target.key === selectedKey }"
          type="button"
          @click="selectTarget(target.key)"
        >
          <span class="avatar" :class="{ off: !target.enabled }">{{ target.avatar }}</span>
          <span class="chat-target__meta">
            <b>{{ target.name }}</b>
            <small class="muted">{{ railSummary(target.key) || target.subtitle }}</small>
          </span>
          <span v-if="unreadOf(target.key)" class="unread-badge" :aria-label="`${unreadOf(target.key)} 条未读`">{{ unreadOf(target.key) > 99 ? '99+' : unreadOf(target.key) }}</span>
          <span v-else-if="target.kind === 'group'" class="tag">群组</span>
        </button>
        <div v-if="!filteredTargets.length" class="muted" style="padding: 12px 4px">没有匹配的联系人或群组</div>
      </aside>

      <article class="panel chat">
        <div v-if="activeTarget" class="chat-head">
          <div class="chat-head__copy">
            <h2 :title="activeTarget.name">{{ activeTarget.name }}</h2>
            <p v-if="activeTarget.kind === 'group'" class="muted">{{ activeTarget.channel?.description }}</p>
            <p v-else-if="!activeTarget.enabled" class="muted">已禁用</p>
          </div>
          <div class="chat-head__actions">
            <el-button v-if="activeTarget.kind === 'group' && canManage" size="small" @click="openGroupDialog(activeTarget.channel)">群组设置</el-button>
            <el-button @click="asideOpen = !asideOpen">{{ asideOpen ? "收起资料" : "查看资料" }}</el-button>
          </div>
        </div>
        <div v-else class="chat-head"><h2>数字员工</h2></div>

        <div v-if="directoryLoading || activeState?.status === 'loading'" class="muted" style="padding: 24px">正在读取会话…</div>
        <ChatPanel
          v-else-if="activeState && activeRuntime"
          :key="selectedKey"
          :state="activeState"
          :target="activeTarget"
          :runtime="activeRuntime"
          @retry="selectTarget(selectedKey, { retry: true })"
        />
        <div v-else class="muted" style="padding: 24px">选择左侧联系人或群组开始对话</div>

      </article>

      <aside v-if="activeState && asideOpen" class="panel aside">
        <div class="right-tabs">
          <button
            v-for="item in rightTabs"
            :key="item.value"
            class="chip"
            :class="{ sel: activeState.tab === item.value }"
            type="button"
            @click="activeState.tab = item.value"
          >{{ item.label }}</button>
        </div>

        <div v-if="activeState.tab === 'tasks'" style="padding: 16px">
          <div v-if="activeState.taskError" class="muted" style="margin-bottom: 10px">{{ activeState.taskError }}</div>
          <div v-if="activeState.tasksLoading && !activeState.tasks.length" class="muted">正在读取关联任务…</div>
          <TaskRail v-else :tasks="activeState.tasks" :names="nameById" hide-responsible empty-text="暂无关联任务" />
        </div>

        <div v-else-if="activeState.tab === 'materials' && activeTarget?.kind === 'contact'" style="padding: 16px">
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

        <div v-else-if="activeState.tab === 'members' && activeTarget?.kind === 'group'" style="padding: 0 16px 16px">
          <div class="member-actions">
            <b>群组成员</b>
            <el-button v-if="canManage" size="small" @click="inviteHuman">邀请人类成员</el-button>
          </div>
          <div v-for="person in activeTarget.channel?.members || []" :key="`${person.member_type}:${person.member_id}`" class="member">
            <span class="avatar">{{ person.avatar }}</span>
            <div class="member-identity"><b>{{ person.name }}{{ person.member_id === currentUid() ? '（你）' : '' }}</b>
              <small class="muted">{{ person.member_type === 'employee' ? '数字员工' : `${roleLabels[person.role]} · ${person.online ? '在线' : '离线'}` }}</small>
            </div>
            <template v-if="person.member_type === 'human' && person.role !== 'owner' && canManage">
              <el-select v-if="activeTarget.channel?.my_role === 'owner'" :model-value="person.role" size="small" style="width:88px" @change="(value: unknown) => setRole(person.member_id, String(value))">
                <el-option label="管理员" value="admin" /><el-option label="成员" value="member" />
              </el-select>
              <el-button v-if="person.member_id !== currentUid()" text type="danger" size="small" @click="removeHuman(person.member_id)">移除</el-button>
              <el-button v-if="activeTarget.channel?.my_role === 'owner'" text size="small" @click="transferOwner(person.member_id)">转让</el-button>
            </template>
          </div>
          <el-button v-if="activeTarget.channel?.my_role !== 'owner'" type="danger" plain size="small" @click="leaveGroup">退出群组</el-button>

        </div>

        <div v-else-if="activeTarget?.kind === 'group'" style="padding: 16px">
          <div class="section-label">群组名称</div>
          <div class="detail-excerpt">{{ activeTarget.channel?.name }}</div>
          <div class="section-label">目标</div>
          <div class="detail-excerpt">{{ activeTarget.channel?.description || "暂无" }}</div>
          <div class="section-label">成员</div>
          <div class="detail-excerpt">{{ (activeTarget.members || []).map((item) => item.name).join("、") || "暂无" }}</div>
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
            <span v-for="employee in employees.filter((item) => groupExistingMemberIds.includes(item.id))" :key="employee.id" class="mini-chip">{{ employee.avatar }} {{ employee.name }}</span>
          </div>
        </el-form-item>
        <el-form-item :label="groupEditingId ? '添加数字员工' : '参与的数字员工'">
          <el-checkbox-group v-model="groupForm.memberIds">
            <el-checkbox v-for="employee in employees" :key="employee.id" :value="employee.id">
              {{ employee.avatar }} {{ employee.name }} · {{ employee.domain }}
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
import TaskRail from "@/components/TaskRail.vue";
import { listTasks, listWorkers, type PlatformTask, type PlatformWorker } from "@/platform";
import type { Channel, Employee, ResourceCatalog } from "@/types";
import { GroupChatAdapter, PrivateChatAdapter } from "@/features/chat/adapters";
import { ChatRuntime, createSessionState } from "@/features/chat/ChatRuntime";
import ChatPanel from "@/features/chat/components/ChatPanel.vue";
import { shortText } from "@/features/chat/shortText";
import type { ChatSessionState, ChatTarget } from "@/features/chat/types";
import { selectionIsCurrent } from "@/features/chat/sessionState";

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
const selectedKey = ref("");
const sessions = reactive<Record<string, ChatSessionState>>({});
const asideOpen = ref(true);
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
const preparePromises = new Map<string, Promise<void>>();

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
      createdAt: "",
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
      avatar: "☷",
      enabled: true,
      searchText: `${channel.name} ${channel.description} ${members.map((item) => item.name).join(" ")}`.toLowerCase(),
      createdAt: channel.created_at ?? "",
      members,
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

const activeState = computed(() => (selectedKey.value ? sessions[selectedKey.value] : undefined));
const activeTarget = computed(() => activeState.value?.target ?? null);
const runtimes = new Map<string, ChatRuntime>();
const activeRuntime = computed(() => (selectedKey.value ? runtimes.get(selectedKey.value) : undefined));

function ensureRuntime(state: ChatSessionState) {
  let runtime = runtimes.get(state.key);
  if (!runtime) {
    runtime = new ChatRuntime(state, () => selectedKey.value === state.key);
    runtimes.set(state.key, runtime);
  }
  return runtime;
}

function railSummary(key: string) {
  return sessions[key]?.lastSummary || "";
}

function unreadOf(key: string) {
  return sessions[key]?.unread || 0;
}

const rightTabs = computed(() => activeTarget.value?.kind === "group"
  ? [{ value: "tasks" as const, label: "任务" }, { value: "members" as const, label: "成员" }, { value: "materials" as const, label: "资料" }]
  : [{ value: "tasks" as const, label: "任务" }, { value: "materials" as const, label: "资料" }]);



const nameById = computed(() => {
  const names: Record<string, string> = {};
  for (const employee of employees.value) names[employee.id] = employee.name;
  for (const worker of workers.value) names[worker.id] = worker.name;
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




function ensureState(target: ChatTarget) {
  if (!sessions[target.key]) sessions[target.key] = createSessionState(target);
  return sessions[target.key];
}



async function prepareContact(state: ChatSessionState) {
  const employee = state.target.employee;
  if (!employee) return;
  state.status = "loading";
  try {
    const conversation = await createConversation(employee.id);
    const adapter = new PrivateChatAdapter(employee, conversation);
    state.error = "";
    ensureRuntime(state).bind(adapter);
  } catch (error) {
    ensureRuntime(state).setError(error);
  }
}

function ensurePrepared(state: ChatSessionState) {
  const existing = preparePromises.get(state.key);
  if (existing) return existing;
  const promise = prepareContact(state).finally(() => {
    preparePromises.delete(state.key);
  });
  preparePromises.set(state.key, promise);
  return promise;
}

async function loadTasks(state: ChatSessionState) {
  const adapter = state.adapter;
  if (!adapter) return;
  const generation = ++state.taskGeneration;
  state.tasksLoading = true;
  try {
    const tasks = await listTasks(adapter.taskQuery());
    if (state.taskGeneration !== generation || state.adapter !== adapter) return;
    state.tasks = tasks;
    state.taskError = "";
  } catch (error) {
    if (state.taskGeneration !== generation || state.adapter !== adapter) return;
    state.tasks = [];
    state.taskError = shortText(error, "关联任务加载失败");
  } finally {
    if (state.taskGeneration === generation) state.tasksLoading = false;
  }
}

function updateRoute(target: ChatTarget, replace = false) {
  const query = target.kind === "contact" ? { employee: target.id } : { group: target.id };
  const location = { path: "/chat", query };
  if (replace) void router.replace(location);
  else void router.push(location);
}

async function selectTarget(key: string, options: { fromRoute?: boolean; retry?: boolean } = {}) {
  const target = targets.value.find((item) => item.key === key);
  if (!target) return;
  if (!options.retry && !options.fromRoute && selectedKey.value === key && sessions[key]?.adapter) return;
  const previousKey = selectedKey.value;
  if (previousKey && previousKey !== key && sessions[previousKey]) {
    // 切走时保留未读计数；进入时清零。
  }
  const generation = ++selectionGeneration;
  selectedKey.value = key;
  localStorage.setItem("nexus_chat_last", key);
  if (!options.fromRoute) updateRoute(target);
  const state = ensureState(target);
  const runtime = ensureRuntime(state);
  if (options.retry) {
    runtime.dispose();
    state.adapter = null;
    state.messages = [];
    state.liveLines = [];
    state.notice = "";
  }
  if (!state.adapter) {
    if (target.kind === "contact") await ensurePrepared(state);
    else if (target.channel) runtime.bind(new GroupChatAdapter(target.channel, employees.value));
  } else if (options.retry && target.kind === "contact") {
    await ensurePrepared(state);
  }
  if (!selectionIsCurrent(selectedKey.value, key, generation, selectionGeneration, disposed)) return;
  if (state.adapter) {
    state.status = "ready";
    runtime.markRead();
    void loadTasks(state);
  }
}

function routeTargetKey() {
  const group = typeof route.query.group === "string" ? route.query.group : "";
  const employee = typeof route.query.employee === "string" ? route.query.employee : "";
  if (group && targets.value.some((item) => item.key === `group:${group}`)) return `group:${group}`;
  if (employee && targets.value.some((item) => item.key === `contact:${employee}`)) return `contact:${employee}`;
  return "";
}

function routeType(): "contact" | "group" | "" {
  return route.query.type === "group" ? "group" : route.query.type === "contact" ? "contact" : "";
}

async function applyRouteTarget() {
  if (!employees.value.length && !channels.value.length) return;
  const type = routeType();
  if (type) filter.value = type;
  const key = routeTargetKey() || (type ? targets.value.find((item) => item.kind === type)?.key ?? "" : "");
  if (!key || key === selectedKey.value) return;
  await selectTarget(key, { fromRoute: true });
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
    const target = targets.value.find((item) => item.key === key);
    const existing = sessions[key];
    if (existing && target?.channel) {
      existing.target = target;
      if (existing.adapter instanceof GroupChatAdapter) {
        existing.adapter.setChannel(target.channel, employees.value);
        ensureRuntime(existing).bind(existing.adapter);
      } else {
        ensureRuntime(existing).bind(new GroupChatAdapter(target.channel, employees.value));
      }
      existing.error = "";
      void loadTasks(existing);
    } else {
      await selectTarget(key);
    }
    ElMessage.success(wasEditing ? "群组已保存" : "群组已创建");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存失败");
  } finally {
    groupSaving.value = false;
  }
}

watch(() => [route.query.employee, route.query.group, route.query.type], () => { void applyRouteTarget(); });
watch(() => activeState.value?.tab, (tab) => {
  if (tab === "tasks" && activeState.value) void loadTasks(activeState.value);
});

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
    await router.replace({ path: "/chat", query: { type: "group", group: result.id } });
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "邀请无效"); }
}
function setRole(uid: string, role: string) { void act(`${activeTarget.value!.id}/humans/${uid}`, "PATCH", { role }); }
function removeHuman(uid: string) { void act(`${activeTarget.value!.id}/humans/${uid}`, "DELETE"); }
async function transferOwner(uid: string) { await act(`${activeTarget.value!.id}/transfer/${uid}`); }
async function leaveGroup() {
  await act(`${activeTarget.value!.id}/leave`);
  await loadDirectory();
}
function onGroupMembers(event: MessageEvent) {
  if (event.origin === location.origin && event.data?.type === "nexus-group-members") void refreshGroups();
}
window.addEventListener("message", onGroupMembers);

async function loadDirectory() {
  directoryLoading.value = true;
  directoryError.value = "";
  try {
    const [skills, mcps, knowledge, tools, employeeList, channelList, workerList] = await Promise.all([
      getSkills().catch(() => emptyCatalog()),
      getMcps().catch(() => emptyCatalog()),
      getKnowledge().catch(() => emptyCatalog()),
      getTools().catch(() => emptyCatalog()),
      listEmployees(),
      listChannels(),
      listWorkers().catch(() => []),
    ]);
    if (disposed) return;
    catalogs.value = { skills, mcps, knowledge, tools };
    employees.value = employeeList;
    channels.value = channelList;
    workers.value = workerList;
    const type = routeType();
    if (type) filter.value = type;
    const routeKey = routeTargetKey();
    const stored = localStorage.getItem("nexus_chat_last") ?? "";
    const storedTarget = stored && targets.value.some((item) => item.key === stored) ? stored : "";
    const initial = routeKey
      || (type ? targets.value.find((item) => item.kind === type)?.key ?? "" : "")
      || storedTarget
      || targets.value[0]?.key
      || "";
    if (initial) await selectTarget(initial, { fromRoute: Boolean(routeKey || type) });
  } catch (error) {
    if (!disposed) directoryError.value = shortText(error, "会话列表加载失败");
  } finally {
    if (!disposed) directoryLoading.value = false;
  }
}

onMounted(async () => {
  if (typeof route.query.invite === "string") { joinToken.value = route.query.invite; joinDialogOpen.value = true; }
  await loadDirectory();
  if (disposed) return;
  taskTimer = window.setInterval(() => {
    const state = activeState.value;
    if (state?.tab === "tasks") void loadTasks(state);
    if (activeTarget.value?.kind === "group") void refreshGroups().catch(() => undefined);
  }, 8000);
});

onBeforeUnmount(() => {
  window.removeEventListener("message", onGroupMembers);
  disposed = true;
  selectionGeneration += 1;
  for (const state of Object.values(sessions)) {
    state.taskGeneration += 1;
    runtimes.get(state.key)?.dispose();
  }
  runtimes.clear();
  preparePromises.clear();
  window.clearInterval(taskTimer);
});
</script>

<style scoped>
.member-actions { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.member { flex-wrap:wrap; }
.member-identity { flex:1; min-width:0; overflow-wrap:anywhere; }
.chat-page { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.chat-search-row { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }
.chat-search-row :deep(.el-input) { flex: 1; }
.chat-filters { display: flex; gap: 6px; margin-bottom: 10px; }
.chat-counts { margin: 0 0 8px 2px; font-size: 11px; }
.chat-target { width: 100%; border: 0; background: transparent; text-align: left; }
.chat-target__meta { display: grid; gap: 2px; min-width: 0; flex: 1; }
.chat-target__meta b, .chat-target__meta small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chat-head__actions { display: flex; flex: none; flex-wrap: wrap; gap: 8px; align-items: center; }
.chip.sel { background: #e5f6ed; border-color: #aad8c2; color: #187956; }
.unread-badge {
  flex: none;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: #e45757;
  color: #fff;
  font-size: 10px;
  line-height: 18px;
  text-align: center;
}
.panel.chat { display: flex; flex-direction: column; min-height: 0; }
</style>
