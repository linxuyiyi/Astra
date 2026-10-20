<template>
  <section class="employee-conversation" aria-label="数字员工对话">
    <div v-if="!employee" class="conversation-empty">
      <div class="empty-orbit">✧</div>
      <p>选择一座星系，打开与该数字员工的真实私聊。</p>
    </div>
    <template v-else>
      <div class="conversation-head">
        <span class="conversation-avatar">{{ employee.avatar }}</span>
        <div class="conversation-meta">
          <b>{{ employee.name }}</b>
          <small>{{ employee.domain }} · {{ employee.enabled ? "已启用" : "已停用" }}</small>
        </div>
        <RouterLink
          class="conversation-link"
          :to="{ path: '/chat', query: { employee: employee.id } }"
          title="在完整聊天页打开"
        >完整聊天</RouterLink>
      </div>
      <div v-if="!employee.enabled" class="conversation-note">
        该员工已停用，不能开始新的对话；历史消息仍可在完整聊天页查看。
      </div>
      <div v-else-if="error" class="conversation-note error">
        {{ error }}
        <el-button size="small" text type="primary" @click="prepare">重试</el-button>
      </div>
      <div v-else-if="loading || !conversationId" class="conversation-note">
        正在打开 {{ employee.name }} 的私聊会话…
      </div>
      <iframe
        v-if="conversationId && employee.enabled"
        :key="conversationId"
        class="conversation-island"
        :src="islandSrc"
        :title="`与 ${employee.name} 的对话`"
        @load="ready = true"
      />
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { RouterLink } from "vue-router";
import { createConversation } from "@/api";
import type { NebulaEmployee } from "../types";

const props = defineProps<{
  employee: NebulaEmployee | null;
}>();

const conversationId = ref("");
const loading = ref(false);
const error = ref("");
const ready = ref(false);
let generation = 0;

const islandSrc = computed(
  () => `/nexus-chat/index.html?conversation=${encodeURIComponent(conversationId.value)}`,
);

async function prepare(): Promise<void> {
  const employee = props.employee;
  const current = ++generation;
  conversationId.value = "";
  error.value = "";
  ready.value = false;
  if (!employee || !employee.enabled) return;
  loading.value = true;
  try {
    const conversation = await createConversation(employee.id);
    if (current !== generation) return;
    conversationId.value = conversation.id;
  } catch (reason) {
    if (current !== generation) return;
    error.value = reason instanceof Error ? reason.message : "无法打开该员工的私聊会话";
  } finally {
    if (current === generation) loading.value = false;
  }
}

watch(
  () => props.employee?.id,
  () => void prepare(),
  { immediate: true },
);
</script>

<style scoped>
.employee-conversation {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  background: #fbfefc;
}
.conversation-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid #e3efe8;
  flex: none;
}
.conversation-avatar {
  width: 34px;
  height: 34px;
  border-radius: 11px;
  display: grid;
  place-items: center;
  background: linear-gradient(135deg, #e2f5ee, #d3ede4);
  font-size: 17px;
  flex: none;
}
.conversation-meta { display: grid; gap: 2px; min-width: 0; flex: 1; }
.conversation-meta b { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conversation-meta small { color: #8aa196; font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conversation-link { color: #0b9e7b; font-size: 11px; text-decoration: none; white-space: nowrap; }
.conversation-link:hover { text-decoration: underline; }
.conversation-note {
  margin: 10px 14px;
  padding: 10px 12px;
  border: 1px dashed #cfe5da;
  border-radius: 9px;
  color: #7c9589;
  font-size: 11px;
  line-height: 1.7;
  flex: none;
}
.conversation-note.error { border-color: #f0cbd0; color: #b25a66; background: #fff7f8; }
.conversation-island { flex: 1 1 auto; width: 100%; min-height: 0; border: 0; }
.conversation-empty {
  margin: auto;
  padding: 34px 26px;
  text-align: center;
  color: #7f9b8e;
  font-size: 12px;
  line-height: 1.8;
}
.empty-orbit { font-size: 30px; color: #37b98d; margin-bottom: 8px; }
</style>
