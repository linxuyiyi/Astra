<template>
  <div class="row-menu" @click.stop>
    <ElButton v-if="primary" link type="primary" :disabled="disabled" :aria-label="primaryLabel" @click="emit('primary')">{{ primary }}</ElButton>
    <ElDropdown v-if="items.length" trigger="click" :disabled="disabled" @command="onCommand">
      <ElButton link type="primary" :disabled="disabled" :aria-label="moreLabel">更多</ElButton>
      <template #dropdown>
        <ElDropdownMenu>
          <ElDropdownItem v-for="item in items" :key="item.command" :command="item.command" :disabled="item.disabled">
            {{ item.label }}
          </ElDropdownItem>
        </ElDropdownMenu>
      </template>
    </ElDropdown>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ElButton, ElDropdown, ElDropdownItem, ElDropdownMenu } from 'element-plus'

export interface RowMenuItem {
  command: string
  label: string
  disabled?: boolean
}

const props = defineProps<{
  primary?: string
  items: RowMenuItem[]
  disabled?: boolean
  /** Row subject, used as “编辑 标题” / “更多操作 标题”. */
  ariaLabel?: string
}>()

const primaryLabel = computed(() => props.ariaLabel && props.primary ? `${props.primary} ${props.ariaLabel}` : undefined)
const moreLabel = computed(() => props.ariaLabel ? `更多操作 ${props.ariaLabel}` : undefined)

const emit = defineEmits<{ primary: []; command: [command: string] }>()

function onCommand(command: string | number) {
  emit('command', String(command))
}
</script>

<style scoped>
.row-menu { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }
</style>
