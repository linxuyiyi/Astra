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
            <small class="muted">{{ target.subtitle }}</small>
          </span>
          <span v-if="target.kind === 'group'" class="tag">群组</span>
        </button>
        <div v-if="!filteredTargets.length" class="muted" style="padding: 12px 4px">没有匹配的联系人或群组</div>
      </aside>

      <article class="panel chat">
        <div v-if="activeTarget" class="chat-head">
          <div>
            <h2 style="margin: 0 0 4px">{{ activeTarget.name }}</h2>
            <p v-if="activeTarget.kind === 'group'" class="muted" style="margin: 0">{{ activeTarget.channel?.description }}</p>
            <p v-else-if="!activeTarget.enabled" class="muted" style="margin: 0">该联系人已禁用，历史消息仍可查看</p>
          </div>
          <div class="chat-head__actions">
            <el-button v-if="activeTarget.kind === 'group'" size="small" @click="openGroupDialog(activeTarget.channel)">群组设置</el-button>
            <el-button @click="asideOpen = !asideOpen">{{ asideOpen ? "收起资料" : "查看资料" }}</el-button>
          </div>
        </div>
        <div v-else class="chat-head"><h2>数字员工</h2></div>

        <div ref="messageBox" class="chat-messages">
          <div v-if="activeState?.status === 'error'" class="msg system"><div class="bubble">{{ activeState.error }}</div></div>
          <div
            v-else-if="activeState && !activeState.messages.length && !activeState.liveLines.length && !activeState.notice"
            class="empty"
          >{{ activeTarget?.kind === "group" ? "开始一次群组讨论，用 @员工名 唤醒成员" : `与 ${activeTarget?.name} 开始交流` }}</div>
          <div v-if="!activeState" class="empty">还没有联系人或群组</div>

          <div
            v-for="message in activeState?.messages || []"
            :key="message.id"
            class="msg"
            :class="{ user: message.sender === 'user' }"
          >
            <div class="msg__body">
              <div class="muted">{{ senderLabel(message) }}</div>
              <div v-if="message.text" class="bubble markdown" v-html="renderMarkdown(message.text)"></div>
              <template v-for="tool in message.tools || []" :key="tool.id">
                <template v-if="tool.name === 'contact_employee'">
                  <details v-if="contactState(tool).kind === 'unknown'" class="tool-status tool-status--unknown">
                    <summary>{{ contactState(tool).text }}</summary>
                    <pre v-if="contactState(tool).raw">{{ contactState(tool).raw }}</pre>
                  </details>
                  <div v-else class="tool-status">{{ contactState(tool).text }}</div>
                </template>
                <details v-else class="tool-card tool-card--collapsed">
                  <summary><b>{{ tool.name || "工具" }}</b><span class="muted">{{ toolSummary(tool) }}</span></summary>
                  <div class="tool-card__body">
                    <div v-if="tool.input"><span class="section-label">参数</span><pre>{{ tool.input }}</pre></div>
                    <div v-if="tool.output"><span class="section-label">结果</span><pre>{{ tool.output }}</pre></div>
                    <small v-if="tool.state === 'error'" class="muted">工具失败</small>
                  </div>
                </details>
              </template>
              <div class="message-attachments">
                <a
                  v-for="file in message.attachments"
                  :key="file.id"
                  class="sent-attachment"
                  :class="{ image: isImage(file.type) }"
                  :href="attachmentUrl(file.id)"
                  :download="file.name"
                  target="_blank"
                >
                  <img v-if="isImage(file.type)" :src="attachmentUrl(file.id)" :alt="file.name" />
                  <span><b>{{ file.name }}</b><small class="muted">{{ formatSize(file.size) }}</small></span>
                </a>
              </div>
            </div>
          </div>

          <div v-for="line in activeState?.liveLines || []" :key="`live-${line.employeeId}`" class="msg">
            <div class="msg__body">
              <div class="muted">{{ line.name }}</div>
              <div v-if="line.text" class="bubble markdown" v-html="renderMarkdown(line.text)"></div>
              <template v-for="tool in line.tools" :key="tool.id">
                <template v-if="tool.name === 'contact_employee'">
                  <details v-if="contactState(tool).kind === 'unknown'" class="tool-status tool-status--unknown">
                    <summary>{{ contactState(tool).text }}</summary>
                    <pre v-if="contactState(tool).raw">{{ contactState(tool).raw }}</pre>
                  </details>
                  <div v-else class="tool-status">{{ contactState(tool).text }}</div>
                </template>
                <details v-else class="tool-card tool-card--collapsed">
                  <summary><b>{{ tool.name || "工具" }}</b><span class="muted">{{ toolSummary(tool) }}</span></summary>
                  <div class="tool-card__body">
                    <div v-if="tool.input"><span class="section-label">参数</span><pre>{{ tool.input }}</pre></div>
                    <div v-if="tool.output"><span class="section-label">结果</span><pre>{{ tool.output }}</pre></div>
                  </div>
                </details>
              </template>
            </div>
          </div>

          <div v-if="activeState?.notice" class="msg system"><div class="bubble">{{ activeState.notice }}</div></div>
        </div>

        <div v-if="activeTarget" class="chat-compose">
          <div v-if="activeTarget.kind === 'group'" class="chips" style="margin-bottom: 8px">
            <button
              v-for="employee in enabledMembers"
              :key="employee.id"
              class="chip"
              type="button"
              @click="insertMention(employee.name)"
            >@{{ employee.name }}</button>
          </div>
          <div v-if="draftFiles.length" class="draft-attachments">
            <div v-for="file in draftFiles" :key="file.id" class="draft-file">
              <img v-if="isImage(file.type)" :src="attachmentUrl(file.id)" :alt="file.name" />
              <div><b>{{ file.name }}</b><small class="muted">{{ formatSize(file.size) }}</small></div>
              <el-button link type="danger" @click="removeDraftFile(file.id)">移除</el-button>
            </div>
          </div>
          <div
            class="compose-box"
            :class="{ dropping }"
            @dragover.prevent="dropping = canSend"
            @dragleave="dropping = false"
            @drop.prevent="onDrop"
          >
            <el-button :disabled="!canSend" @click="fileInput?.click()">附件</el-button>
            <input ref="fileInput" type="file" multiple hidden @change="onPick" />
            <textarea
              v-model="draft"
              :disabled="!canSend"
              rows="2"
              :placeholder="composePlaceholder"
              @input="onInput"
              @paste="onPaste"
              @keydown.enter.exact.prevent="send"
            />
            <el-button type="primary" :disabled="!canSend || (!draft.trim() && !draftFiles.length)" @click="send">发送</el-button>
          </div>
          <div v-if="mentionOpen && mentionMatches.length" class="mention-box">
            <button v-for="employee in mentionMatches" :key="employee.id" type="button" @click="insertMention(employee.name)">
              {{ employee.avatar }} {{ employee.name }} · {{ employee.domain }}
            </button>
          </div>
        </div>
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
          <div class="section-label">工具</div>
          <div class="resource-list">
            <span v-for="item in activeTarget.employee?.tools || []" :key="item" class="mini-chip">{{ toolLabel(item) }}</span>
            <span v-if="!activeTarget.employee?.tools.length" class="muted">暂无</span>
          </div>
        </div>

        <div v-else-if="activeState.tab === 'memory' && activeTarget?.kind === 'contact'" style="padding: 16px">
          <div class="section-label">会话</div>
          <div class="mono">{{ activeState.adapter?.sessionId || "发送后才会创建" }}</div>
          <div v-if="latestReply" class="detail-excerpt" style="margin-top: 12px">{{ latestReply }}</div>
        </div>

        <div v-else-if="activeState.tab === 'members' && activeTarget?.kind === 'group'" style="padding: 0 16px 16px">
          <div style="display: flex; justify-content: space-between; align-items: center">
            <b>群组成员</b>
            <el-button size="small" @click="openGroupDialog(activeTarget.channel)">＋ 调整成员</el-button>
          </div>
          <div class="member"><span class="avatar">👤</span><div><b>你</b><small class="muted">人类成员</small></div></div>
          <div v-for="employee in activeTarget.members || []" :key="employee.id" class="member">
            <span class="avatar" :class="{ off: !employee.enabled }">{{ employee.avatar }}</span>
            <div><b>{{ employee.name }}</b><small class="muted">{{ employee.domain }} · {{ employee.enabled ? "启用" : "禁用" }}</small></div>
          </div>
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

    <el-dialog v-model="groupDialogOpen" :title="groupEditingId ? '编辑群组' : '创建群组'" width="520px">
      <el-form label-position="top">
        <el-form-item label="群组名称" required><el-input v-model="groupForm.name" maxlength="32" /></el-form-item>
        <el-form-item label="群组目标" required><el-input v-model="groupForm.description" type="textarea" :rows="3" /></el-form-item>
        <el-form-item label="参与的数字员工">
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
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import {
  attachmentUrl,
  createConversation,
  currentUid,
  getKnowledge,
  getSkills,
  getTools,
  listChannels,
  listEmployees,
  saveChannel,
  uploadAttachment,
} from "@/api";
import TaskRail from "@/components/TaskRail.vue";
import { listTasks, listWorkers, type PlatformTask, type PlatformWorker } from "@/platform";
import type { Attachment, ChatMessage, Channel, Employee, ResourceCatalog } from "@/types";
import { GroupChatAdapter, PrivateChatAdapter } from "@/features/chat/adapters";
import type { ChatAdapterEvent, ChatSessionState, ChatTarget } from "@/features/chat/types";
import { mergeMessages, sameIds, selectionIsCurrent } from "@/features/chat/sessionState";

const route = useRoute();
const router = useRouter();
const employees = ref<Employee[]>([]);
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
const messageBox = ref<HTMLElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const dropping = ref(false);
const mentionOpen = ref(false);
const groupDialogOpen = ref(false);
const groupEditingId = ref<string | null>(null);
const groupSaving = ref(false);
const groupForm = reactive({ name: "", description: "", memberIds: [] as string[] });
const emptyCatalog = (): ResourceCatalog => ({ available: false, items: [] });
const catalogs = ref({ skills: emptyCatalog(), knowledge: emptyCatalog(), tools: emptyCatalog() });
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
      subtitle: `${channel.members.length} 位数字员工`,
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
const enabledMembers = computed(() => (activeTarget.value?.members ?? []).filter((item) => item.enabled));
const rightTabs = computed(() => activeTarget.value?.kind === "group"
  ? [{ value: "tasks" as const, label: "任务" }, { value: "members" as const, label: "成员" }, { value: "materials" as const, label: "资料" }]
  : [{ value: "tasks" as const, label: "任务" }, { value: "materials" as const, label: "资料" }, { value: "memory" as const, label: "记忆" }]);
const draft = computed({
  get: () => activeState.value?.draft ?? "",
  set: (value: string) => { if (activeState.value) activeState.value.draft = value; },
});
const draftFiles = computed(() => activeState.value?.draftFiles ?? []);
const canSend = computed(() => Boolean(activeState.value?.adapter?.canSend && !activeState.value.sending && !activeState.value.uploading));
const composePlaceholder = computed(() => {
  if (!activeTarget.value) return "选择联系人或群组后开始交流";
  if (activeTarget.value.kind === "contact" && !activeTarget.value.enabled) return "该联系人已禁用，历史消息仍可查看";
  if (activeState.value?.uploading) return "正在上传附件…";
  return activeTarget.value.kind === "group" ? "发送消息，@数字员工，或拖入、粘贴图片…" : "发送消息，或拖入、粘贴图片…";
});
const mentionMatches = computed(() => {
  if (activeTarget.value?.kind !== "group") return [];
  const match = draft.value.match(/(?:^|\s)@([^\s]*)$/);
  if (!match) return [];
  return enabledMembers.value.filter((item) => item.name.includes(match[1] ?? ""));
});
const nameById = computed(() => {
  const names: Record<string, string> = {};
  for (const employee of employees.value) names[employee.id] = employee.name;
  for (const worker of workers.value) names[worker.id] = worker.name;
  return names;
});
const latestReply = computed(() => [...(activeState.value?.messages ?? [])].reverse().find((item) => item.sender !== "user")?.text ?? "");

function isImage(type: string) {
  return type.startsWith("image/");
}

function formatSize(size: number) {
  if (size < 1024) return "小于 1 KB";
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / 1048576).toFixed(1)} MB`;
}

function renderMarkdown(value: string) {
  const escaped = value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return escaped
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\n/g, "<br>");
}

function resourceLabel(catalog: ResourceCatalog, id: string) {
  return catalog.items.find((item) => item.id === id)?.name ?? id;
}

function toolLabel(id: string) {
  if (id === "contact_employee") return "联系其他数字员工";
  const name = resourceLabel(catalogs.value.tools, id);
  return name === id ? "工作区工具" : name;
}

function senderLabel(message: ChatMessage) {
  const speaker = message.speaker || "";
  const me = currentUid();
  if (message.sender === "user" && (!speaker || speaker === "user" || (me && speaker === me))) return "你";
  const identity = speaker && speaker !== "user" ? speaker : message.sender;
  return employees.value.find((item) => item.id === identity)?.name ?? identity ?? "数字员工";
}

function toolSummary(tool: { state: string }) {
  if (tool.state === "error") return "失败";
  if (tool.state === "running") return "执行中";
  return tool.state === "done" ? "完成" : "";
}

function contactState(tool: { output: string; state: string }) {
  const raw = tool.output?.trim() ?? "";
  if (raw) {
    try {
      const parsed = JSON.parse(raw) as { ok?: boolean; peer?: string; error?: string };
      if (parsed.ok) return { kind: "ok" as const, text: `已联系 ${parsed.peer ?? "同事"}`, raw };
      return { kind: "error" as const, text: `联系失败：${parsed.error || "未投递"}`, raw };
    } catch {
      /* 运行中的流式 JSON 不完整，按状态继续展示。 */
    }
  }
  if (tool.state === "running") return { kind: "running" as const, text: "正在联系同事…", raw };
  if (tool.state === "error") return { kind: "error" as const, text: "联系失败", raw };
  return { kind: "unknown" as const, text: "联系结果未知", raw };
}

function createState(target: ChatTarget): ChatSessionState {
  return {
    key: target.key,
    target,
    adapter: null,
    status: "loading",
    error: "",
    messages: [],
    liveLines: [],
    notice: "",
    draft: "",
    draftFiles: [],
    uploading: false,
    sending: false,
    tasks: [],
    tasksLoading: false,
    taskError: "",
    taskGeneration: 0,
    generation: 0,
    abort: null,
    tab: "tasks",
  };
}

function ensureState(target: ChatTarget) {
  if (!sessions[target.key]) sessions[target.key] = createState(target);
  return sessions[target.key];
}

function stopFollow(key: string) {
  const state = sessions[key];
  if (!state) return;
  state.abort?.abort();
  state.abort = null;
  state.generation += 1;
}

function startFollow(state: ChatSessionState) {
  if (disposed || !state.adapter) return;
  state.abort?.abort();
  const controller = new AbortController();
  state.abort = controller;
  const generation = ++state.generation;
  const emit = (event: ChatAdapterEvent) => {
    if (disposed || state.generation !== generation || controller.signal.aborted) return;
    if (event.type === "messages") state.messages = mergeMessages(state.messages, event.messages);
    else if (event.type === "live") state.liveLines = event.lines;
    else if (event.type === "notice") state.notice = event.text;
  };
  void state.adapter.follow(controller.signal, emit).catch((error) => {
    if (controller.signal.aborted || state.generation !== generation) return;
    state.notice = error instanceof Error ? error.message : "事件流中断";
  });
}

async function prepareContact(state: ChatSessionState) {
  const employee = state.target.employee;
  if (!employee) return;
  state.status = "loading";
  try {
    const conversation = await createConversation(employee.id);
    state.adapter = new PrivateChatAdapter(employee, conversation);
    state.status = "ready";
    state.error = "";
  } catch (error) {
    state.status = "error";
    state.error = error instanceof Error ? error.message : "无法打开该联系人的会话";
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
    state.taskError = error instanceof Error ? error.message : "关联任务加载失败";
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

async function selectTarget(key: string, options: { fromRoute?: boolean } = {}) {
  const target = targets.value.find((item) => item.key === key);
  if (!target) return;
  if (!options.fromRoute && selectedKey.value === key && sessions[key]?.adapter) return;
  const generation = ++selectionGeneration;
  if (selectedKey.value && selectedKey.value !== key) stopFollow(selectedKey.value);
  selectedKey.value = key;
  mentionOpen.value = false;
  localStorage.setItem("nexus_chat_last", key);
  if (!options.fromRoute) updateRoute(target);
  const state = ensureState(target);
  if (!state.adapter) {
    if (target.kind === "contact") await ensurePrepared(state);
    else if (target.channel) state.adapter = new GroupChatAdapter(target.channel, employees.value);
  }
  if (!selectionIsCurrent(selectedKey.value, key, generation, selectionGeneration, disposed)) return;
  if (state.adapter) {
    state.status = "ready";
    startFollow(state);
    void loadTasks(state);
  }
  await nextTick();
  if (messageBox.value) messageBox.value.scrollTop = messageBox.value.scrollHeight;
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

async function addFileList(files: File[]) {
  const state = activeState.value;
  if (!state || !canSend.value) return;
  const key = state.key;
  if (state.draftFiles.length + files.length > 6) ElMessage.warning("每条消息最多添加 6 个附件");
  state.uploading = true;
  for (const file of files.slice(0, 6 - state.draftFiles.length)) {
    if (!file.size) {
      ElMessage.warning("不能添加空文件");
      continue;
    }
    if (file.size > 20 * 1024 * 1024) {
      ElMessage.warning("单个附件不能超过 20 MB");
      continue;
    }
    const named = file.name ? file : new File([file], "截图.png", { type: file.type || "image/png" });
    try {
      const attachment = await uploadAttachment(named);
      const current = sessions[key];
      if (current) current.draftFiles.push(attachment);
    } catch (error) {
      ElMessage.error(error instanceof Error ? error.message : "附件上传失败");
    }
  }
  const current = sessions[key];
  if (current) current.uploading = false;
}

function removeDraftFile(id: string) {
  if (!activeState.value) return;
  activeState.value.draftFiles = activeState.value.draftFiles.filter((item) => item.id !== id);
}

function onPick(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  input.value = "";
  void addFileList(files);
}

function onDrop(event: DragEvent) {
  dropping.value = false;
  void addFileList(Array.from(event.dataTransfer?.files ?? []));
}

function onPaste(event: ClipboardEvent) {
  const fromFiles = Array.from(event.clipboardData?.files ?? []);
  const fromItems = Array.from(event.clipboardData?.items ?? [])
    .filter((item) => item.type.startsWith("image/"))
    .map((item) => item.getAsFile())
    .filter((file): file is File => Boolean(file));
  const files = fromFiles.length ? fromFiles : fromItems;
  if (!files.length) return;
  event.preventDefault();
  void addFileList(files);
}

function insertMention(name: string) {
  if (!activeState.value) return;
  activeState.value.draft = activeState.value.draft.replace(/(?:^|\s)@[^\s]*$/, "").trim();
  activeState.value.draft = `${activeState.value.draft}${activeState.value.draft ? " " : ""}@${name} `;
  mentionOpen.value = false;
}

function onInput() {
  mentionOpen.value = activeTarget.value?.kind === "group" && mentionMatches.value.length > 0 && /(?:^|\s)@[^\s]*$/.test(draft.value);
}

async function send() {
  const state = activeState.value;
  if (!state?.adapter || !canSend.value) return;
  const text = state.draft.trim();
  const fileIds = state.draftFiles.map((item) => item.id);
  if (!text && !fileIds.length) return;
  const key = state.key;
  const adapter = state.adapter;
  const sentDraft = state.draft;
  state.sending = true;
  state.notice = "";
  try {
    const result = await adapter.send(text, fileIds, new AbortController().signal);
    const current = sessions[key];
    if (!current || current.adapter !== adapter) return;
    current.messages = mergeMessages(current.messages, [result.message]);
    if (current.draft === sentDraft) current.draft = "";
    if (sameIds(current.draftFiles.map((item) => item.id), fileIds)) current.draftFiles = [];
    current.notice = result.notice;
    mentionOpen.value = false;
    if (selectedKey.value === key) {
      startFollow(current);
      void loadTasks(current);
    }
  } catch (error) {
    const current = sessions[key];
    if (current && current.adapter === adapter) current.notice = error instanceof Error ? error.message : "发送失败";
  } finally {
    const current = sessions[key];
    if (current) current.sending = false;
  }
}

function openGroupDialog(channel?: Channel) {
  groupEditingId.value = channel?.id ?? null;
  groupForm.name = channel?.name ?? "";
  groupForm.description = channel?.description ?? "";
  groupForm.memberIds = channel
    ? channel.members.map((item) => item.employee_id)
    : employees.value.filter((item) => item.enabled).map((item) => item.id);
  groupDialogOpen.value = true;
}

async function saveGroup() {
  if (!groupForm.name.trim() || !groupForm.description.trim()) {
    ElMessage.warning("请填写群组名称和目标");
    return;
  }
  if (!groupForm.memberIds.length) {
    ElMessage.warning("至少选择一名员工");
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
      const wasSelected = selectedKey.value === key;
      if (wasSelected) stopFollow(key);
      existing.target = target;
      if (existing.adapter instanceof GroupChatAdapter) {
        existing.adapter.setChannel(target.channel, employees.value);
      } else {
        existing.adapter = new GroupChatAdapter(target.channel, employees.value);
      }
      existing.status = "ready";
      existing.error = "";
      if (wasSelected) {
        startFollow(existing);
        void loadTasks(existing);
      }
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

watch(search, () => { mentionOpen.value = false; });
watch(() => [route.query.employee, route.query.group, route.query.type], () => { void applyRouteTarget(); });
watch(() => activeState.value?.messages, async () => {
  await nextTick();
  if (messageBox.value) messageBox.value.scrollTop = messageBox.value.scrollHeight;
});
watch(() => activeState.value?.liveLines, async () => {
  await nextTick();
  if (messageBox.value) messageBox.value.scrollTop = messageBox.value.scrollHeight;
});
watch(() => activeState.value?.tab, (tab) => {
  if (tab === "tasks" && activeState.value) void loadTasks(activeState.value);
});

onMounted(async () => {
  const [skills, knowledge, tools, employeeList, channelList, workerList] = await Promise.all([
    getSkills().catch(() => emptyCatalog()),
    getKnowledge().catch(() => emptyCatalog()),
    getTools().catch(() => emptyCatalog()),
    listEmployees(),
    listChannels(),
    listWorkers().catch(() => []),
  ]);
  if (disposed) return;
  catalogs.value = { skills, knowledge, tools };
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
  if (disposed) return;
  taskTimer = window.setInterval(() => {
    const state = activeState.value;
    if (state?.tab === "tasks") void loadTasks(state);
  }, 8000);
});

onBeforeUnmount(() => {
  disposed = true;
  selectionGeneration += 1;
  for (const state of Object.values(sessions)) {
    state.abort?.abort();
    state.generation += 1;
    state.taskGeneration += 1;
  }
  preparePromises.clear();
  window.clearInterval(taskTimer);
});
</script>

<style scoped>
.chat-page { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.chat-search-row { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }
.chat-search-row :deep(.el-input) { flex: 1; }
.chat-filters { display: flex; gap: 6px; margin-bottom: 10px; }
.chat-counts { margin: 0 0 8px 2px; font-size: 11px; }
.chat-target { width: 100%; border: 0; background: transparent; text-align: left; }
.chat-target__meta { display: grid; gap: 2px; min-width: 0; flex: 1; }
.chat-target__meta b, .chat-target__meta small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chat-head__actions { display: flex; gap: 8px; align-items: center; }
.msg__body { min-width: 0; }
.chip.sel { background: #e5f6ed; border-color: #aad8c2; color: #187956; }
.compose-box.dropping { outline: 2px dashed #187956; }
.sent-attachment.image, .draft-file { display: flex; gap: 8px; align-items: center; }
.sent-attachment img, .draft-file img { width: 72px; height: 72px; object-fit: cover; border-radius: 8px; }
.tool-card { margin-top: 8px; padding: 8px; border: 1px solid #d7e3dc; border-radius: 8px; background: #f7fbf8; }
.tool-card pre { white-space: pre-wrap; margin: 4px 0 0; font-size: 12px; }
.tool-card--collapsed { padding: 0; overflow: hidden; }
.tool-card--collapsed summary { display: flex; align-items: center; gap: 8px; padding: 8px 10px; cursor: pointer; list-style: none; }
.tool-card--collapsed summary::-webkit-details-marker { display: none; }
.tool-card--collapsed summary::before { content: "▸"; color: #7b9386; }
.tool-card--collapsed[open] summary::before { transform: rotate(90deg); }
.tool-card--collapsed summary span { margin-left: auto; }
.tool-card__body { padding: 0 10px 10px; }
.tool-status { display: inline-flex; margin-top: 8px; padding: 5px 9px; border: 1px solid #cfe5d8; border-radius: 999px; background: #f0faf4; color: #2a7d5c; font-size: 11px; }
.tool-status--unknown { display: block; width: fit-content; cursor: pointer; }
.tool-status--unknown summary { list-style: none; }
.tool-status--unknown summary::-webkit-details-marker { display: none; }
.tool-status--unknown pre { margin: 6px 0 0; max-width: 480px; white-space: pre-wrap; color: #5f746a; }
.markdown :deep(code) { font-family: ui-monospace, monospace; }
.mention-box { margin-top: 8px; background: white; border: 1px solid #dbece0; border-radius: 12px; padding: 6px; }
.mention-box button { display: block; width: 100%; text-align: left; border: 0; background: transparent; padding: 8px; cursor: pointer; }
</style>
