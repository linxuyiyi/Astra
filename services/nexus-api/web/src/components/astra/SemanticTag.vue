<template>
  <ElTag :type="config.type" :size="size" :class="config.className">{{ label }}</ElTag>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ElTag } from 'element-plus'
import { semanticTag, type SemanticTone } from '@/features/workers/semantic'

const props = withDefaults(defineProps<{
  tone: SemanticTone
  label: string
  size?: 'small' | 'default' | 'large'
}>(), { size: 'small' })

const config = computed(() => semanticTag(props.tone))
</script>
