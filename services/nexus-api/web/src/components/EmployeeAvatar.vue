<template>
  <span
    class="employee-avatar"
    :class="{ 'is-off': disabled }"
    :style="{ width: `${size}px`, height: `${size}px` }"
    :title="appearance?.label ?? '员工形象未配置'"
  >
    <img
      v-if="appearance"
      :src="appearance.portrait"
      :alt="alt"
      draggable="false"
    />
    <span
      v-else
      class="avatar-unconfigured"
      role="img"
      aria-label="员工形象未配置"
      >未配置</span
    >
  </span>
</template>
<script setup lang="ts">
import { computed } from "vue";
import { employeeAppearance } from "../../../shared/employeeAvatar.ts";
const props = withDefaults(
  defineProps<{
    avatar: string;
    size?: number;
    alt?: string;
    disabled?: boolean;
  }>(),
  { size: 36, alt: "", disabled: false },
);
const appearance = computed(() => employeeAppearance(props.avatar));
</script>
<style scoped>
.employee-avatar {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  vertical-align: middle;
  background: #e7eee0;
  border: 1px solid #d0ddc7;
  border-radius: 7px;
  box-sizing: border-box;
}
.employee-avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  image-rendering: pixelated;
}
.is-off {
  opacity: 0.45;
}
.avatar-unconfigured {
  font-size: 9px;
  color: #84917b;
  line-height: 1.4;
  text-align: center;
}
</style>
