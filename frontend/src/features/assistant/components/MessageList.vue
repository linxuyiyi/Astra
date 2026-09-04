<template>
  <div class="message-list">
    <div v-for="msg in store.messages" :key="msg.id" class="message" :class="msg.role.toLowerCase()">
      <div class="content">{{ msg.content }}</div>

      <div v-for="part in msg.parts" :key="part.id" class="message-part">
         <TaskCard v-if="part.part_type === 'TASK_CARD'" :data="part.payload_json" />
         <EmployeeCard v-else-if="part.part_type === 'EMPLOYEE_CARD'" :data="part.payload_json" />
         <RunCard v-else-if="part.part_type === 'RUN_CARD'" :data="part.payload_json" />
         <KnowledgeCard v-else-if="part.part_type === 'KNOWLEDGE_CARD'" :data="part.payload_json" />
         <ConfirmCard v-else-if="part.part_type === 'CONFIRM_CARD'" :data="part.payload_json" @confirm="store.confirmOperation(part.payload_json.operation_id)" @cancel="store.cancelOperation(part.payload_json.operation_id)" />
         <div v-else-if="part.part_type === 'STATUS'" class="status-msg">{{ part.payload_json.text }}</div>
         <div v-else-if="part.part_type === 'ERROR'" class="error-msg">{{ part.payload_json.error }}</div>
      </div>

    </div>
  </div>
</template>

<script setup lang="ts">
import TaskCard from './cards/TaskCard.vue'
import EmployeeCard from './cards/EmployeeCard.vue'
import RunCard from './cards/RunCard.vue'
import KnowledgeCard from './cards/KnowledgeCard.vue'
import ConfirmCard from './cards/ConfirmCard.vue'
import { useAssistantStore } from '../store/assistantStore'
const store = useAssistantStore()
</script>

<style scoped>
.message-list {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.message {
  max-width: 85%;
  padding: 12px;
  border-radius: 8px;
}
.user {
  align-self: flex-end;
  background-color: var(--color-primary-light);
}
.assistant {
  align-self: flex-start;
  background-color: #f5f5f5;
}
</style>
