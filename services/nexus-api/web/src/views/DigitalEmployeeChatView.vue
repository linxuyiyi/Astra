<template>
  <section class="chat-page">
    <div class="workspace" :class="{ 'workspace--focus': !asideOpen }">
      <button
        v-if="mobileRailOpen || asideOpen"
        class="chat-mobile-mask"
        aria-label="关闭聊天侧栏"
        @click="mobileRailOpen = false; asideOpen = false"
      />

      <!-- 左侧会话目录 -->
      <aside class="left-rail" :class="{ 'is-mobile-open': mobileRailOpen }" aria-label="会话列表">
        <button type="button" class="chat-mobile-only chat-mobile-close" @click="mobileRailOpen = false">
          关闭会话列表
        </button>

        <div class="chat-directory-head">
          <h1>会话</h1>
          <button
            type="button"
            class="chat-create-btn"
            aria-label="新建群组"
            title="新建群组"
            data-testid="chat-create-group"
            @click="openGroupDialog()"
          >
            <Plus :size="16" :stroke-width="1.9" />
          </button>
        </div>

        <div class="chat-rail-controls">
          <el-input
            v-model="search"
            class="chat-search"
            placeholder="搜索员工或群组"
            aria-label="搜索联系人或群组"
            clearable
          >
            <template #prefix><Search :size="15" :stroke-width="1.7" /></template>
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
            >
              <span>{{ item.label }}</span>
              <span class="chat-filter-seg__count">{{ filterCounts[item.value] }}</span>
            </button>
          </div>
        </div>

        <div
          id="chat-directory-results"
          class="chat-target-list"
          role="tabpanel"
          :aria-labelledby="`chat-filter-${filter}`"
        >
          <div v-if="directoryLoading && !targets.length" class="chat-list-state" role="status">
            <Loader2 class="chat-spinner" :size="16" />
            <span>加载会话中…</span>
          </div>

          <div v-else-if="directoryError" class="chat-list-state chat-list-state--error" role="alert">
            <p>{{ directoryError }}</p>
            <el-button size="small" @click="loadDirectory">重试</el-button>
          </div>

          <div v-else-if="!filteredTargets.length" class="chat-list-state">
            <p v-if="search.trim()">未找到匹配的会话</p>
            <p v-else-if="filter === 'group'">暂无群组讨论</p>
            <p v-else>暂无可用会话</p>
            <button
              v-if="search.trim()"
              type="button"
              class="chat-list-action"
              @click="search = ''"
            >清除搜索</button>
            <button
              v-else-if="filter === 'group'"
              type="button"
              class="chat-list-action"
              @click="openGroupDialog()"
            >创建第一个群组</button>
          </div>

          <button
            v-for="target in filteredTargets"
            :key="target.key"
            class="chat-target"
            :aria-current="target.key === selectedKey ? 'true' : undefined"
            type="button"
            @click="selectTarget(target.key)"
          >
            <EmployeeAvatar
              v-if="target.kind === 'contact'"
              :avatar="target.avatar"
              :size="34"
              :disabled="!target.enabled"
            />
            <span v-else class="chat-avatar" aria-hidden="true">
              <UsersRound :size="16" :stroke-width="1.7" />
            </span>

            <span class="chat-target__meta">
              <span class="chat-target__row">
                <b class="chat-target__name">{{ target.name }}</b>
                <span v-if="target.kind === 'contact' && !target.enabled" class="chat-mini-badge">禁用</span>
                <span v-else-if="target.kind === 'group'" class="chat-mini-badge">{{ target.channel?.members.length ?? 0 }}人</span>
              </span>
              <small class="chat-target__sub">{{ summaries[target.key] || target.subtitle }}</small>
            </span>
          </button>
        </div>

        <div class="chat-directory-footer">
          <button type="button" class="chat-join-link" @click="joinDialogOpen = true">
            <LogIn :size="15" :stroke-width="1.7" />
            <span>通过邀请码加入群组</span>
          </button>
        </div>
      </aside>

      <!-- 主对话画布 -->
      <article class="chat">
        <header v-if="activeTarget" class="chat-head">
          <button
            type="button"
            class="chat-mobile-only chat-directory-toggle"
            @click="mobileRailOpen = true; asideOpen = false"
          >会话列表</button>

          <EmployeeAvatar
            v-if="activeTarget.kind === 'contact'"
            :avatar="activeTarget.avatar"
            :size="34"
            :disabled="!activeTarget.enabled"
          />
          <span v-else class="chat-avatar chat-avatar--head" aria-hidden="true">
            <UsersRound :size="17" :stroke-width="1.7" />
          </span>

          <div class="chat-head__copy">
            <div class="chat-head__title-row">
              <h2 :title="activeTarget.name">{{ activeTarget.name }}</h2>
              <el-tag
                v-if="activeTarget.kind === 'contact' && !activeTarget.enabled"
                size="small"
                class="astra-tag--neutral"
              >已禁用</el-tag>
            </div>
            <p class="chat-head__desc">{{ activeTarget.subtitle }}</p>
          </div>

          <div class="chat-head__actions">
            <el-dropdown
              v-if="activeTarget.kind === 'group' && canManage"
              trigger="click"
              @command="onChatHeadCommand"
            >
              <button
                type="button"
                class="chat-icon-button"
                aria-label="更多操作"
                title="更多操作"
                data-testid="chat-head-more"
              >
                <Ellipsis :size="18" :stroke-width="1.7" />
              </button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="group-settings">群组设置</el-dropdown-item>
                  <el-dropdown-item command="invite-human">邀请成员</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>

            <button
              type="button"
              class="chat-profile-toggle"
              :aria-expanded="asideOpen"
              @click="toggleAside"
            >
              <PanelRight :size="16" :stroke-width="1.7" />
              <span>{{ asideOpen ? '收起资料' : '资料' }}</span>
            </button>
          </div>
        </header>

        <header v-else class="chat-head">
          <button
            type="button"
            class="chat-mobile-only chat-directory-toggle"
            @click="mobileRailOpen = true; asideOpen = false"
          >会话列表</button>
          <h2>数字员工</h2>
        </header>

        <div v-if="directoryLoading || bindingLoading" class="chat-start" role="status">
          <div class="chat-status-inline">
            <Loader2 class="chat-spinner" :size="18" />
            <span>正在打开会话…</span>
          </div>
        </div>

        <div v-else-if="bindingError" class="chat-start" role="alert">
          <div class="chat-empty-card">
            <AlertCircle class="chat-empty-icon chat-empty-icon--danger" :size="22" />
            <p class="chat-empty-title">{{ bindingError }}</p>
            <el-button size="small" @click="selectTarget(selectedKey, { retry: true })">重试</el-button>
          </div>
        </div>

        <ConversationPanel
          v-else-if="conversation"
          :key="JSON.stringify([conversation.kind, conversation.id, conversation.viewerId, conversation.title, conversation.enabled, conversation.avatar])"
          :conversation="conversation"
        />

        <div v-else class="chat-start">
          <div class="chat-welcome">
            <span class="chat-welcome__badge" aria-hidden="true">
              <MessageSquare :size="20" :stroke-width="1.7" />
            </span>
            <h3>开始协作对话</h3>
            <p>选择一位数字员工私聊，或创建群组开展多人协作</p>

            <div v-if="quickEmployees.length" class="chat-welcome__quick">
              <button
                v-for="emp in quickEmployees"
                :key="emp.id"
                type="button"
                class="chat-quick-pill"
                @click="selectTarget(`contact:${emp.id}`)"
              >
                <EmployeeAvatar :avatar="emp.avatar" :size="22" />
                <span>{{ emp.name }}</span>
              </button>
            </div>

            <div class="chat-welcome__actions">
              <el-button v-if="employees.length" type="primary" @click="openGroupDialog()">
                发起团队讨论
              </el-button>
              <RouterLink v-else to="/settings/employees" class="chat-welcome__link">
                配置数字员工
              </RouterLink>
              <el-button @click="joinDialogOpen = true">加入群组</el-button>
            </div>
          </div>
        </div>
      </article>

      <!-- 右侧资料与任务栏 -->
      <aside v-if="activeTarget && asideOpen" class="chat-aside" aria-label="会话资料">
        <button type="button" class="chat-mobile-only chat-mobile-close" @click="asideOpen = false">
          关闭资料
        </button>

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
          >
            <span>{{ item.label }}</span>
            <span v-if="item.count !== undefined" class="chat-aside-tab__count">{{ item.count }}</span>
          </button>
        </div>

        <div id="chat-aside-content" class="chat-aside__body" role="tabpanel" :aria-labelledby="`chat-aside-${tab}`">
          <!-- 关联任务 -->
          <div v-if="tab === 'tasks'" class="aside-panel">
            <div v-if="taskError" class="aside-inline-error" role="alert">
              <span>{{ taskError }}</span>
              <button type="button" @click="loadTasks">重试</button>
            </div>
            <div v-if="tasksLoading && !tasks.length" class="chat-status-inline aside-loading">
              <Loader2 class="chat-spinner" :size="15" />
              <span>读取关联任务…</span>
            </div>
            <TaskRail
              v-else
              :tasks="tasks"
              :names="nameById"
              hide-responsible
              empty-text="当前会话暂无关联任务"
            />
          </div>

          <!-- 员工资料 -->
          <div v-else-if="tab === 'materials' && activeTarget?.kind === 'contact'" class="aside-panel aside-stack">
            <div class="profile-summary-card">
              <EmployeeAvatar
                :avatar="activeTarget.avatar"
                :size="40"
                :disabled="!activeTarget.enabled"
              />
              <div class="profile-summary-card__copy">
                <div class="profile-summary-card__head">
                  <strong>{{ activeTarget.name }}</strong>
                  <el-tag
                    size="small"
                    :type="activeTarget.enabled ? 'success' : 'info'"
                    :class="{ 'astra-tag--neutral': !activeTarget.enabled }"
                  >{{ activeTarget.enabled ? '启用' : '停用' }}</el-tag>
                </div>
                <span class="profile-summary-card__domain">{{ activeTarget.employee?.domain || '数字员工' }}</span>
              </div>
            </div>

            <section class="aside-block">
              <div class="aside-block__head">
                <span class="aside-block__label">职责范围</span>
              </div>
              <div class="aside-md-box">
                <MarkdownView
                  :content="activeTarget.employee?.description || ''"
                  compact
                  empty-text="暂无职责说明"
                />
              </div>
            </section>

            <section class="aside-block">
              <div class="aside-block__head">
                <span class="aside-block__label">工作指令</span>
                <button
                  v-if="(activeTarget.employee?.instructions || '').length > 260"
                  type="button"
                  class="aside-link-btn"
                  @click="instructionsExpanded = !instructionsExpanded"
                >{{ instructionsExpanded ? '收起' : '展开全部' }}</button>
              </div>
              <div
                class="aside-md-box"
                :class="{ 'aside-md-box--clamped': !instructionsExpanded && (activeTarget.employee?.instructions || '').length > 260 }"
              >
                <MarkdownView
                  :content="activeTarget.employee?.instructions || ''"
                  compact
                  allow-copy
                  empty-text="暂无工作指令"
                />
              </div>
            </section>

            <section class="aside-block">
              <div class="aside-block__head">
                <span class="aside-block__label">能力配置</span>
              </div>
              <div v-if="employeeCapabilities.length" class="cap-groups">
                <div v-for="group in employeeCapabilities" :key="group.label" class="cap-group">
                  <span class="cap-group__title">{{ group.label }}</span>
                  <div class="resource-list">
                    <span v-for="item in group.items" :key="item" class="mini-chip">{{ item }}</span>
                  </div>
                </div>
              </div>
              <p v-else class="aside-muted-note">暂未配置知识、技能或外部工具</p>
            </section>
          </div>

          <!-- 群组成员 -->
          <div v-else-if="tab === 'members' && activeTarget?.kind === 'group'" class="aside-panel aside-stack">
            <div class="member-bar">
              <span class="aside-block__label">群组成员 · {{ activeTarget.channel?.members.length ?? 0 }}</span>
              <el-button v-if="canManage" size="small" @click="inviteHuman">
                <UserPlus :size="13" style="margin-right: 4px" /> 邀请成员
              </el-button>
            </div>

            <div class="member-list">
              <div
                v-for="person in activeTarget.channel?.members || []"
                :key="`${person.member_type}:${person.member_id}`"
                class="member-row"
              >
                <EmployeeAvatar v-if="person.member_type === 'employee'" :avatar="person.avatar" :size="32" />
                <span v-else class="member-human-avatar" aria-hidden="true">
                  <UserRound :size="15" />
                  <i class="member-presence" :class="{ 'is-online': person.online }" />
                </span>

                <div class="member-identity">
                  <div class="member-identity__top">
                    <b class="member-name">{{ person.name }}</b>
                    <span v-if="person.member_id === currentUid()" class="chat-mini-badge">你</span>
                  </div>
                  <div class="member-identity__sub">
                    <el-tag
                      v-if="person.member_type === 'employee'"
                      size="small"
                      class="astra-tag--neutral member-tag"
                    >数字员工</el-tag>
                    <template v-else>
                      <el-tag
                        size="small"
                        :type="person.role === 'owner' ? 'warning' : 'info'"
                        :class="{ 'astra-tag--neutral': person.role !== 'owner' }"
                        class="member-tag"
                      >{{ roleLabels[person.role] }}</el-tag>
                      <span class="member-status-text">{{ person.online ? '在线' : '离线' }}</span>
                    </template>
                  </div>
                </div>

                <el-dropdown
                  v-if="person.member_type === 'human' && person.role !== 'owner' && canManage"
                  trigger="click"
                  @command="onMemberCommand"
                >
                  <button type="button" class="member-more" aria-label="成员操作" data-testid="member-more">
                    <Ellipsis :size="15" />
                  </button>
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
            </div>

            <div v-if="activeTarget.channel?.my_role !== 'owner'" class="member-footer">
              <el-button type="danger" plain size="small" @click="leaveGroup">退出群组</el-button>
            </div>
          </div>

          <!-- 群组资料 -->
          <div v-else-if="activeTarget?.kind === 'group'" class="aside-panel aside-stack">
            <section class="aside-block">
              <div class="aside-block__head">
                <span class="aside-block__label">群组名称</span>
                <button
                  v-if="canManage"
                  type="button"
                  class="aside-link-btn"
                  @click="openGroupDialog(activeTarget.channel)"
                >编辑</button>
              </div>
              <div class="group-title-value">{{ activeTarget.channel?.name }}</div>
            </section>

            <section class="aside-block">
              <div class="aside-block__head">
                <span class="aside-block__label">群组目标</span>
              </div>
              <div class="aside-md-box">
                <MarkdownView
                  :content="activeTarget.channel?.description || ''"
                  compact
                  empty-text="暂无群组目标"
                />
              </div>
            </section>

            <section class="aside-block">
              <div class="aside-block__head">
                <span class="aside-block__label">参与成员</span>
                <span class="aside-count-badge">{{ activeTarget.channel?.members.length ?? 0 }}</span>
              </div>
              <div v-if="activeTarget.channel?.members.length" class="resource-list">
                <span
                  v-for="item in activeTarget.channel.members"
                  :key="`${item.member_type}:${item.member_id}`"
                  class="mini-chip member-chip"
                >
                  <EmployeeAvatar v-if="item.member_type === 'employee'" :avatar="item.avatar" :size="18" />
                  <UserRound v-else :size="12" />
                  <span>{{ item.name }}</span>
                </span>
              </div>
              <p v-else class="aside-muted-note">暂无成员</p>
            </section>
          </div>
        </div>
      </aside>
    </div>

    <!-- 邀请链接弹窗 -->
    <el-dialog v-model="inviteDialogOpen" title="邀请加入群组" width="480px">
      <div class="dialog-stack">
        <div class="invite-meta">
          <el-tag size="small" class="astra-tag--neutral">24 小时有效</el-tag>
          <el-tag size="small" class="astra-tag--neutral">单次使用</el-tag>
        </div>
        <el-input :model-value="inviteLink" readonly class="mono-input" aria-label="群邀请链接" />
      </div>
      <template #footer>
        <el-button @click="inviteDialogOpen = false">关闭</el-button>
        <el-button type="primary" @click="copyInvite">复制链接</el-button>
      </template>
    </el-dialog>

    <!-- 加入群组弹窗 -->
    <el-dialog v-model="joinDialogOpen" title="加入群组" width="440px">
      <el-input
        v-model="joinToken"
        placeholder="粘贴邀请链接或邀请码"
        aria-label="邀请码"
        @keyup.enter="joinGroup"
      />
      <template #footer>
        <el-button @click="joinDialogOpen = false">取消</el-button>
        <el-button type="primary" :disabled="!joinToken.trim()" @click="joinGroup">加入</el-button>
      </template>
    </el-dialog>

    <!-- 创建 / 编辑群组弹窗 -->
    <el-dialog
      v-model="groupDialogOpen"
      :title="groupEditingId ? '群组设置' : '新建群组'"
      width="540px"
      class="nexus-scroll-dialog"
    >
      <el-form label-position="top" class="group-form">
        <el-form-item label="群组名称" required>
          <el-input v-model="groupForm.name" maxlength="32" placeholder="如：发布评审、后端排障" show-word-limit />
        </el-form-item>

        <el-form-item label="群组目标" required>
          <el-input
            v-model="groupForm.description"
            type="textarea"
            :rows="3"
            maxlength="2000"
            show-word-limit
            placeholder="明确群组的协作范围、背景和预期产出"
          />
        </el-form-item>

        <el-form-item v-if="groupEditingId && existingGroupEmployees.length" label="已有数字员工">
          <div class="resource-list">
            <span
              v-for="employee in existingGroupEmployees"
              :key="employee.id"
              class="mini-chip member-chip"
            >
              <EmployeeAvatar :avatar="employee.avatar" :size="20" />
              <span>{{ employee.name }}</span>
            </span>
          </div>
        </el-form-item>

        <el-form-item>
          <template #label>
            <div class="picker-label-row">
              <span>{{ groupEditingId ? '数字员工成员' : '参与的数字员工' }}</span>
              <span class="picker-selected-count">已选 {{ groupForm.memberIds.length }} / {{ employees.length }}</span>
            </div>
          </template>

          <div class="employee-picker">
            <div v-if="employees.length > 5" class="employee-picker__search">
              <el-input v-model="memberKeyword" placeholder="筛选员工名称或领域" clearable>
                <template #prefix><Search :size="14" /></template>
              </el-input>
            </div>
            <el-checkbox-group v-model="groupForm.memberIds" class="employee-picker__list">
              <div
                v-for="employee in pickerEmployees"
                :key="employee.id"
                class="employee-picker__item"
                :class="{ 'is-checked': groupForm.memberIds.includes(employee.id) }"
                @click="toggleGroupMember(employee.id)"
              >
                <el-checkbox :value="employee.id" class="employee-picker__check" @click.stop />
                <EmployeeAvatar :avatar="employee.avatar" :size="26" :disabled="!employee.enabled" />
                <span class="employee-picker__copy">
                  <b>{{ employee.name }}</b>
                  <small>{{ employee.domain || '数字员工' }}</small>
                </span>
                <el-tag v-if="!employee.enabled" size="small" class="astra-tag--neutral">已禁用</el-tag>
              </div>
              <div v-if="!pickerEmployees.length" class="employee-picker__empty">没有匹配的员工</div>
            </el-checkbox-group>
          </div>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="groupDialogOpen = false">取消</el-button>
        <el-button type="primary" :loading="groupSaving" @click="saveGroup">
          {{ groupEditingId ? '保存设置' : '创建群组' }}
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { workerDisplayName } from '@/utils/workerName'
import EmployeeAvatar from "../components/EmployeeAvatar.vue";
import MarkdownView from "@/components/astra/MarkdownView.vue";
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
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
import {
  AlertCircle,
  Ellipsis,
  Loader2,
  LogIn,
  MessageSquare,
  PanelRight,
  Plus,
  Search,
  UserPlus,
  UserRound,
  UsersRound,
} from "lucide-vue-next";
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
const instructionsExpanded = ref(false);

function toggleAside() {
  asideOpen.value = !asideOpen.value;
}

const mobileRailOpen = ref(false);
const groupDialogOpen = ref(false);
const groupEditingId = ref<string | null>(null);
const groupSaving = ref(false);
const groupExistingMemberIds = ref<string[]>([]);
const memberKeyword = ref("");
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
      subtitle: employee.domain || "数字员工",
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
    const humanCount = channel.members.filter(m => m.member_type === "human").length;
    const empCount = channel.members.filter(m => m.member_type === "employee").length;
    list.push({
      key: `group:${channel.id}`,
      kind: "group",
      id: channel.id,
      name: channel.name,
      subtitle: `${humanCount} 位成员 · ${empCount} 位员工`,
      avatar: "",
      enabled: true,
      searchText: `${channel.name} ${channel.description} ${members.map((item) => item.name).join(" ")}`.toLowerCase(),
      channel,
    });
  }
  return list.sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
});

const filterCounts = computed(() => ({
  all: targets.value.length,
  contact: employees.value.length,
  group: channels.value.length,
}));

const filteredTargets = computed(() => {
  const needle = search.value.trim().toLowerCase();
  return targets.value.filter((target) => {
    if (filter.value !== "all" && target.kind !== filter.value) return false;
    return !needle || target.searchText.includes(needle);
  });
});

const quickEmployees = computed(() =>
  employees.value.filter((item) => item.enabled).slice(0, 4),
);

const existingGroupEmployees = computed(() =>
  employees.value.filter((item) => groupExistingMemberIds.value.includes(item.id)),
);

const pickerEmployees = computed(() => {
  const kw = memberKeyword.value.trim().toLowerCase();
  if (!kw) return employees.value;
  return employees.value.filter((item) =>
    `${item.name} ${item.domain}`.toLowerCase().includes(kw),
  );
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

const activeTaskCount = computed(() =>
  tasks.value.filter((t) => !["SUCCEEDED", "CANCELED", "FAILED"].includes(t.status)).length,
);

const rightTabs = computed<Array<{ value: "tasks" | "members" | "materials"; label: string; count?: number }>>(() => {
  const taskCount = activeTaskCount.value > 0 ? activeTaskCount.value : undefined;
  if (activeTarget.value?.kind === "group") {
    return [
      { value: "tasks", label: "任务", count: taskCount },
      { value: "members", label: "成员", count: activeTarget.value.channel?.members.length },
      { value: "materials", label: "资料" },
    ];
  }
  return [
    { value: "tasks", label: "任务", count: taskCount },
    { value: "materials", label: "资料" },
  ];
});

const employeeCapabilities = computed(() => {
  const emp = activeTarget.value?.employee;
  if (!emp) return [];
  const groups = [
    { label: "知识库", items: (emp.knowledge || []).map((id) => resourceLabel(catalogs.value.knowledge, id)) },
    { label: "技能", items: (emp.skills || []).map((id) => resourceLabel(catalogs.value.skills, id)) },
    { label: "MCP", items: (emp.mcps || []).map((id) => resourceLabel(catalogs.value.mcps, id)) },
    { label: "工具", items: (emp.tools || []).map((id) => toolLabel(id)) },
  ];
  return groups.filter((g) => g.items.length > 0);
});

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
  instructionsExpanded.value = false;
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
  } else if (command === "invite-human" && activeTarget.value?.kind === "group") {
    void inviteHuman();
  }
}

function onMemberCommand(command: string | number) {
  const raw = String(command);
  const [action, a, b] = raw.split(":");
  if (action === "role" && a && b) void setRole(b, a);
  else if (action === "remove" && a) void removeHuman(a);
  else if (action === "transfer" && a) void transferOwner(a);
}

function toggleGroupMember(id: string) {
  const index = groupForm.memberIds.indexOf(id);
  if (index >= 0) groupForm.memberIds.splice(index, 1);
  else groupForm.memberIds.push(id);
}

function openGroupDialog(channel?: Channel) {
  memberKeyword.value = "";
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
  if (!joinToken.value.trim()) return;
  try {
    let token = joinToken.value.trim();
    if (token.includes("?")) token = new URL(token).searchParams.get("invite") || "";
    const result = await groupAction<Channel>("join", "POST", { token });
    joinDialogOpen.value = false;
    joinToken.value = "";
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
  --chat-bar-height: 56px;
  --chat-rail: var(--astra-bg-sidebar);
  --chat-canvas: var(--astra-bg-panel);
  --chat-hover: var(--astra-bg-elevated);
  --chat-ink: var(--astra-text-primary);
  --chat-text: var(--astra-text-secondary);
  --chat-muted: var(--astra-text-muted);
  --chat-line: var(--astra-border);
  display: flex;
  flex-direction: column;
  min-height: 0;
  width: 100%;
  overflow: hidden;
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--chat-canvas);
  box-shadow: var(--astra-shadow-panel);
  color: var(--chat-ink);
}

.workspace {
  display: grid;
  flex: 1;
  grid-template-columns: 272px minmax(0, 1fr) 320px;
  min-height: 0;
}

.workspace--focus {
  grid-template-columns: 272px minmax(0, 1fr);
}

/* ── 左侧会话目录 ─────────────────────────────────────────────── */
.left-rail {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
  border-inline-end: 1px solid var(--chat-line);
  background: var(--chat-rail);
}

.chat-directory-head {
  height: var(--chat-bar-height);
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 14px 0 18px;
}

.chat-directory-head h1 {
  font-size: 15px;
  font-weight: 650;
  letter-spacing: -0.01em;
  margin: 0;
}

.chat-create-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  padding: 0;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  background: var(--astra-bg-panel);
  color: var(--astra-text-primary);
  cursor: pointer;
  box-shadow: 0 1px 1px rgba(9, 9, 11, 0.03);
  transition: background-color 0.15s ease, border-color 0.15s ease;
}

.chat-create-btn:hover {
  background: var(--astra-bg-base);
  border-color: var(--astra-border-hover);
}

.chat-rail-controls {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 0 12px 10px;
  border-bottom: 1px solid var(--astra-border-subtle);
}

.chat-search {
  width: 100%;
}

.chat-search :deep(.el-input__wrapper) {
  background: var(--chat-canvas);
  border-radius: 8px;
  box-shadow: 0 0 0 1px var(--chat-line) inset;
}

.chat-search :deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 1px var(--astra-primary) inset;
}

.chat-search :deep(.el-input__prefix) {
  color: var(--chat-muted);
}

.chat-filter-seg {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 2px;
  padding: 3px;
  border-radius: 8px;
  background: var(--astra-bg-elevated);
}

.chat-filter-seg__item {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  height: 26px;
  padding: 0 6px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--chat-muted);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  transition: background-color 0.15s ease, color 0.15s ease, box-shadow 0.15s ease;
}

.chat-filter-seg__item:hover {
  color: var(--chat-ink);
}

.chat-filter-seg__item.is-active {
  background: var(--astra-bg-panel);
  color: var(--chat-ink);
  font-weight: 600;
  box-shadow: 0 1px 2px rgba(9, 9, 11, 0.06);
}

.chat-filter-seg__count {
  color: var(--chat-muted);
  font-family: var(--astra-font-mono);
  font-size: 10.5px;
  font-weight: 500;
}

.chat-target-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.chat-list-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 28px 14px;
  color: var(--chat-muted);
  font-size: 12.5px;
  text-align: center;
}

.chat-list-state p {
  margin: 0;
}

.chat-list-state--error {
  color: var(--astra-danger);
}

.chat-list-action {
  border: 0;
  background: transparent;
  color: var(--astra-accent-text);
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 5px;
}

.chat-list-action:hover {
  background: var(--astra-accent-soft);
}

.chat-target {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: transparent;
  color: var(--chat-ink);
  text-align: left;
  cursor: pointer;
  transition: background-color 0.14s ease, border-color 0.14s ease, box-shadow 0.14s ease;
}

.chat-target:hover {
  background: var(--chat-hover);
}

.chat-target[aria-current] {
  background: var(--astra-bg-panel);
  border-color: var(--astra-border);
  box-shadow: 0 1px 2px rgba(9, 9, 11, 0.05);
}

.chat-target:focus-visible {
  outline-offset: -2px;
}

.chat-target__meta {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
  gap: 2px;
}

.chat-target__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  min-width: 0;
}

.chat-target__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.35;
}

.chat-target__sub {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--chat-muted);
  line-height: 1.35;
}

.chat-mini-badge {
  flex: none;
  padding: 1px 5px;
  border-radius: 5px;
  background: var(--astra-neutral-soft);
  color: var(--astra-neutral-text);
  font-size: 10.5px;
  font-weight: 500;
  line-height: 1.4;
}

.chat-directory-footer {
  padding: 8px 10px 10px;
  border-top: 1px solid var(--chat-line);
}

.chat-join-link {
  display: flex;
  width: 100%;
  gap: 8px;
  align-items: center;
  height: 34px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  padding: 0 10px;
  color: var(--chat-text);
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 0.14s ease, color 0.14s ease;
}

.chat-join-link:hover {
  background: var(--chat-hover);
  color: var(--chat-ink);
}

.chat-avatar {
  display: inline-grid;
  place-items: center;
  flex: none;
  width: 34px;
  height: 34px;
  border-radius: 8px;
  border: 1px solid var(--astra-border);
  background: var(--astra-bg-panel);
  color: var(--chat-text);
}

.chat-avatar--head {
  background: var(--astra-bg-subtle);
}

/* ── 主对话区 ─────────────────────────────────────────────────── */
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
  gap: 11px;
  height: var(--chat-bar-height);
  padding: 0 18px;
  border-bottom: 1px solid var(--chat-line);
  background: var(--chat-canvas);
}

.chat-head__copy {
  flex: 1;
  min-width: 0;
}

.chat-head__title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.chat-head h2 {
  margin: 0;
  color: var(--chat-ink);
  font-size: 15px;
  font-weight: 650;
  line-height: 1.3;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-head__desc {
  margin: 1px 0 0;
  color: var(--chat-muted);
  font-size: 12px;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-head__actions {
  display: flex;
  flex: none;
  gap: 6px;
  align-items: center;
}

.chat-icon-button,
.chat-profile-toggle,
.member-more {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  height: 32px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: var(--chat-text);
  cursor: pointer;
  transition: background-color 0.14s ease, color 0.14s ease, border-color 0.14s ease;
}

.chat-icon-button,
.member-more {
  width: 32px;
  padding: 0;
}

.chat-icon-button:hover,
.chat-profile-toggle:hover,
.member-more:hover {
  background: var(--astra-bg-base);
  color: var(--chat-ink);
}

.chat-profile-toggle {
  gap: 6px;
  padding: 0 10px;
  font-size: 12.5px;
  font-weight: 500;
}

.chat-profile-toggle[aria-expanded="true"] {
  background: var(--astra-bg-elevated);
  border-color: var(--astra-border);
  color: var(--chat-ink);
}

/* 空态与欢迎引导 */
.chat-start {
  display: flex;
  flex: 1;
  align-items: center;
  justify-content: center;
  padding: 28px;
  background: var(--chat-canvas);
}

.chat-status-inline {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--chat-muted);
  font-size: 13px;
}

.chat-spinner {
  animation: chat-spin 0.9s linear infinite;
}

@keyframes chat-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.chat-empty-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  max-width: 320px;
  padding: 20px 24px;
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-subtle);
  text-align: center;
}

.chat-empty-icon--danger {
  color: var(--astra-danger);
}

.chat-empty-title {
  margin: 0;
  color: var(--chat-ink);
  font-size: 13.5px;
  font-weight: 500;
}

.chat-welcome {
  display: flex;
  flex-direction: column;
  align-items: center;
  max-width: 440px;
  text-align: center;
}

.chat-welcome__badge {
  display: inline-grid;
  place-items: center;
  width: 44px;
  height: 44px;
  margin-bottom: 14px;
  border-radius: 12px;
  border: 1px solid var(--astra-border);
  background: var(--astra-bg-subtle);
  color: var(--astra-text-primary);
}

.chat-welcome h3 {
  margin: 0;
  font-size: 17px;
  font-weight: 650;
  color: var(--astra-text-primary);
}

.chat-welcome p {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--astra-text-muted);
  line-height: 1.6;
}

.chat-welcome__quick {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
  margin-top: 18px;
}

.chat-quick-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 11px 5px 6px;
  border: 1px solid var(--astra-border);
  border-radius: 999px;
  background: var(--astra-bg-panel);
  color: var(--astra-text-primary);
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  transition: border-color 0.14s ease, background-color 0.14s ease;
}

.chat-quick-pill:hover {
  border-color: var(--astra-border-hover);
  background: var(--astra-bg-subtle);
}

.chat-welcome__actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  margin-top: 18px;
}

.chat-welcome__link {
  display: inline-flex;
  align-items: center;
  height: 32px;
  padding: 0 14px;
  border-radius: 8px;
  background: var(--astra-primary);
  color: #ffffff;
  font-size: 13px;
  font-weight: 500;
  text-decoration: none;
}

.chat-welcome__link:hover {
  background: var(--astra-primary-bright);
}

/* ── 右侧资料与任务侧栏 ───────────────────────────────────────── */
.chat-aside {
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  border-inline-start: 1px solid var(--chat-line);
  background: var(--chat-canvas);
}

.right-tabs {
  display: flex;
  align-items: stretch;
  gap: 16px;
  height: var(--chat-bar-height);
  flex: none;
  padding: 0 16px;
  border-bottom: 1px solid var(--chat-line);
}

.chat-aside-tab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--chat-muted);
  padding: 0 2px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease;
}

.chat-aside-tab:hover {
  color: var(--chat-ink);
}

.chat-aside-tab.is-active {
  color: var(--chat-ink);
  border-bottom-color: var(--astra-primary);
  font-weight: 600;
}

.chat-aside-tab__count {
  padding: 0 5px;
  border-radius: 999px;
  background: var(--astra-bg-elevated);
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 10.5px;
  line-height: 1.5;
}

.chat-aside__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.aside-panel {
  padding: 16px;
}

.aside-stack {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.aside-loading {
  padding: 12px 0;
}

.aside-inline-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 12px;
  padding: 8px 10px;
  border: 1px solid color-mix(in srgb, var(--astra-danger) 24%, transparent);
  border-radius: 8px;
  background: var(--astra-danger-soft);
  color: var(--astra-danger);
  font-size: 12px;
}

.aside-inline-error button {
  border: 0;
  background: transparent;
  color: inherit;
  font-weight: 600;
  cursor: pointer;
  padding: 2px 4px;
}

.profile-summary-card {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 11px 12px;
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-subtle);
}

.profile-summary-card__copy {
  min-width: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.profile-summary-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.profile-summary-card__head strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
  font-weight: 650;
  color: var(--astra-text-primary);
}

.profile-summary-card__domain {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--astra-text-muted);
}

.aside-block {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.aside-block__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.aside-block__label {
  color: var(--astra-text-secondary);
  font-size: 12px;
  font-weight: 600;
}

.aside-count-badge {
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--astra-bg-elevated);
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 11px;
}

.aside-link-btn {
  border: 0;
  background: transparent;
  color: var(--astra-accent-text);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  padding: 0;
}

.aside-link-btn:hover {
  color: var(--astra-accent-hover);
  text-decoration: underline;
}

.aside-md-box {
  padding: 10px 12px;
  border: 1px solid var(--astra-border-subtle);
  border-radius: 9px;
  background: var(--astra-bg-subtle);
}

.aside-md-box--clamped {
  max-height: 220px;
  overflow-y: auto;
}

.aside-muted-note {
  margin: 0;
  color: var(--astra-text-muted);
  font-size: 12.5px;
}

.cap-groups {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.cap-group {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.cap-group__title {
  color: var(--astra-text-muted);
  font-size: 11.5px;
  font-weight: 500;
}

.group-title-value {
  color: var(--astra-text-primary);
  font-size: 14px;
  font-weight: 600;
}

/* 群组成员 */
.member-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.member-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.member-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 8px;
  border-radius: 8px;
  transition: background-color 0.14s ease;
}

.member-row:hover {
  background: var(--astra-bg-subtle);
}

.member-human-avatar {
  position: relative;
  display: inline-grid;
  place-items: center;
  width: 32px;
  height: 32px;
  flex: none;
  border-radius: 8px;
  border: 1px solid var(--astra-border);
  background: var(--astra-bg-elevated);
  color: var(--astra-text-secondary);
}

.member-presence {
  position: absolute;
  right: -2px;
  bottom: -2px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  border: 1.5px solid var(--astra-bg-panel);
  background: var(--astra-neutral);
}

.member-presence.is-online {
  background: var(--astra-success);
}

.member-identity {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.member-identity__top {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.member-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
  color: var(--astra-text-primary);
}

.member-identity__sub {
  display: flex;
  align-items: center;
  gap: 6px;
}

.member-tag {
  height: 19px !important;
  padding: 0 6px !important;
  font-size: 11px !important;
}

.member-status-text {
  color: var(--astra-text-muted);
  font-size: 11.5px;
}

.member-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px 3px 5px;
}

.member-footer {
  padding-top: 12px;
  border-top: 1px solid var(--astra-border-subtle);
}

/* 弹窗细节 */
.dialog-stack {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.invite-meta {
  display: flex;
  gap: 6px;
}

.mono-input :deep(.el-input__inner) {
  font-family: var(--astra-font-mono);
  font-size: 12.5px;
}

.group-form :deep(.el-form-item__label) {
  width: 100%;
}

.picker-label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}

.picker-selected-count {
  color: var(--astra-text-muted);
  font-family: var(--astra-font-mono);
  font-size: 11.5px;
  font-weight: 500;
}

.employee-picker {
  width: 100%;
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-subtle);
  overflow: hidden;
}

.employee-picker__search {
  padding: 8px;
  border-bottom: 1px solid var(--astra-border-subtle);
  background: var(--astra-bg-panel);
}

.employee-picker__list {
  display: flex;
  flex-direction: column;
  max-height: 240px;
  overflow-y: auto;
  padding: 6px;
  gap: 3px;
}

.employee-picker__item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 9px;
  border: 1px solid transparent;
  border-radius: 8px;
  cursor: pointer;
  transition: background-color 0.14s ease, border-color 0.14s ease;
}

.employee-picker__item:hover {
  background: var(--astra-bg-panel);
}

.employee-picker__item.is-checked {
  background: var(--astra-bg-panel);
  border-color: var(--astra-border);
}

.employee-picker__check {
  height: auto !important;
  margin-right: 0;
}

.employee-picker__check :deep(.el-checkbox__label) {
  display: none;
}

.employee-picker__copy {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  line-height: 1.35;
}

.employee-picker__copy b {
  font-size: 13px;
  font-weight: 600;
  color: var(--astra-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.employee-picker__copy small {
  font-size: 11.5px;
  color: var(--astra-text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.employee-picker__empty {
  padding: 18px 12px;
  text-align: center;
  color: var(--astra-text-muted);
  font-size: 12.5px;
}

.chat-mobile-only,
.chat-mobile-mask {
  display: none;
}

@media (max-width: 1000px) {
  .chat-page { border-radius: 0; }
  .workspace, .workspace--focus { position: relative; grid-template-columns: minmax(0, 1fr); }
  .left-rail { display: none; }
  .chat-mobile-only {
    display: inline-flex;
    align-items: center;
    border: 1px solid var(--chat-line);
    background: var(--chat-canvas);
    color: var(--chat-ink);
    border-radius: 8px;
    padding: 5px 9px;
    font-size: 12.5px;
    cursor: pointer;
  }
  .chat-mobile-close { align-self: start; margin: 12px 14px 0; }
  .left-rail.is-mobile-open, .chat-aside {
    display: flex;
    flex-direction: column;
    position: absolute;
    z-index: 20;
    top: 0;
    bottom: 0;
    width: min(320px, 92%);
    box-shadow: var(--astra-shadow-float);
  }
  .left-rail.is-mobile-open { left: 0; }
  .chat-aside { right: 0; }
  .chat-mobile-mask {
    display: block;
    position: absolute;
    z-index: 19;
    inset: 0;
    border: 0;
    background: rgba(9, 9, 11, 0.24);
  }
  .chat-head { gap: 8px; padding-inline: 12px; }
}
</style>
