<template>
  <span
    class="employee-avatar-image"
    :class="`employee-avatar-image--${size}`"
    :data-status="employee.status"
    aria-hidden="true"
  >
    <span v-if="emoji" class="employee-avatar-image__emoji">{{ emoji }}</span>
    <img v-else class="employee-avatar-image__sprite" :src="avatarSource" alt="" draggable="false" />
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Employee } from '@/api/types'
import { employeeCharacterSource } from '@/features/workspace/characterAssets'

const props = withDefaults(defineProps<{
  employee: Pick<Employee, 'character_id' | 'status'>
  emoji?: string
  size?: 'small' | 'medium' | 'large'
}>(), { emoji: '', size: 'medium' })
const avatarSource = computed(() => employeeCharacterSource(props.employee.character_id))
</script>

<style scoped>
.employee-avatar-image { position: relative; display: inline-block; flex: none; width: 42px; height: 42px; overflow: hidden; border: 1px solid rgba(183,213,200,.8); border-radius: 14px; background: linear-gradient(145deg,#e8f5ef,#cfe9de); box-shadow: 0 6px 16px rgba(35,76,59,.10); }
.employee-avatar-image--small { width: 34px; height: 34px; border-radius: 11px; }
.employee-avatar-image--large { width: 72px; height: 72px; border-radius: 22px; }
.employee-avatar-image__sprite { position: absolute; top: -18%; left: 50%; display: block; width: 150%; max-width: none; filter: drop-shadow(0 3px 3px rgba(32,71,56,.18)); transform: translateX(-50%); }
.employee-avatar-image__emoji { display: flex; align-items: center; justify-content: center; width: 100%; height: 100%; font-size: 22px; line-height: 1; }
.employee-avatar-image--small .employee-avatar-image__emoji { font-size: 18px; }
.employee-avatar-image--large .employee-avatar-image__emoji { font-size: 38px; }
.employee-avatar-image[data-status='OFFLINE'] .employee-avatar-image__sprite { filter: grayscale(.75); opacity: .62; }
.employee-avatar-image[data-status='OFFLINE'] .employee-avatar-image__emoji { filter: grayscale(.75); opacity: .62; }
</style>
