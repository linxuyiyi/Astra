<template>
  <div class="astra-page" data-testid="agentscope-page">
    <AstraPageHeader title="AgentScope" testid="agentscope-header">
      <template #actions>
        <ElButton :icon="RefreshCw" :loading="loading" @click="load">刷新</ElButton>
        <ElButton v-if="runtime?.ui_url" type="primary" :icon="ExternalLink" @click="openUi">打开管理界面</ElButton>
      </template>
    </AstraPageHeader>

    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error">
      <ElButton type="primary" @click="load">重试</ElButton>
    </ElAlert>

    <ElAlert
      v-if="runtime && !runtime.ui_url"
      type="info"
      show-icon
      :closable="false"
      title="当前部署未配置 AgentScope 图形管理入口"
    >
      <p>Agent、Session、Skill、Knowledge 与记忆由 AgentScope 管理；Nexus 不保存第二套数据，也不在此页面伪造 Skill/MCP 管理。</p>
      <p>如需外部管理界面，请由部署方配置 <code>NEXUS_AGENTSCOPE_UI_URL</code>。</p>
    </ElAlert>

    <ElCard shadow="never">
      <template #header><h3 class="agentscope-card__title">运行状态</h3></template>
      <ElDescriptions :column="3" border size="small">
        <ElDescriptionsItem label="版本">{{ runtime?.agentscope_version ?? "—" }}</ElDescriptionsItem>
        <ElDescriptionsItem label="配置">
          <ElTag :type="runtime?.configured ? 'success' : 'info'">{{ runtime?.configured ? "已配置" : "未配置" }}</ElTag>
        </ElDescriptionsItem>
        <ElDescriptionsItem label="连接">
          <ElTag :type="runtime?.reachable ? 'success' : 'danger'">{{ runtime?.reachable ? "可达" : "不可达" }}</ElTag>
        </ElDescriptionsItem>
      </ElDescriptions>
    </ElCard>

    <section class="agentscope-grid">
      <ElCard v-for="catalog in catalogs" :key="catalog.key" shadow="never">
        <template #header>
          <div class="agentscope-card__head">
            <h3 class="agentscope-card__title">{{ catalog.label }}</h3>
            <ElText type="info">{{ catalog.available ? `${catalog.items.length} 项` : "不可读取" }}</ElText>
          </div>
        </template>
        <div v-if="catalog.available" class="agentscope-tags">
          <ElTag v-for="item in catalog.items" :key="item.id" effect="plain">{{ item.name }}</ElTag>
          <ElText v-if="!catalog.items.length" type="info">暂无</ElText>
        </div>
        <ElText v-else type="info">{{ catalog.error || "AgentScope 未连接或接口不可用" }}</ElText>
      </ElCard>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ExternalLink, RefreshCw } from "lucide-vue-next";
import { onMounted, ref } from "vue";
import {
  ElAlert,
  ElButton,
  ElCard,
  ElDescriptions,
  ElDescriptionsItem,
  ElTag,
  ElText,
} from "element-plus";
import { getKnowledge, getRuntime, getSkills, getTools } from "@/api";
import AstraPageHeader from "@/components/astra/AstraPageHeader.vue";
import type { ResourceCatalog, RuntimeStatus, SkillItem } from "@/types";

interface CatalogView {
  key: string;
  label: string;
  available: boolean;
  items: SkillItem[];
  error: string;
}

const runtime = ref<RuntimeStatus | null>(null);
const loading = ref(false);
const error = ref("");
const catalogs = ref<CatalogView[]>([
  { key: "skills", label: "Skill", available: false, items: [], error: "" },
  { key: "knowledge", label: "Knowledge", available: false, items: [], error: "" },
  { key: "tools", label: "Tool", available: false, items: [], error: "" },
]);

function toView(key: string, label: string, catalog: ResourceCatalog): CatalogView {
  return { key, label, available: catalog.available, items: catalog.items, error: catalog.available ? "" : "AgentScope 接口不可用" };
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const [runtimeValue, skills, knowledge, tools] = await Promise.all([
      getRuntime(),
      getSkills().catch(() => ({ available: false, items: [] })),
      getKnowledge().catch(() => ({ available: false, items: [] })),
      getTools().catch(() => ({ available: false, items: [] })),
    ]);
    runtime.value = runtimeValue;
    catalogs.value = [
      toView("skills", "Skill", skills),
      toView("knowledge", "Knowledge", knowledge),
      toView("tools", "Tool", tools),
    ];
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "无法读取 AgentScope 状态";
    runtime.value = null;
  } finally {
    loading.value = false;
  }
}

function openUi() {
  if (!runtime.value?.ui_url) return;
  window.open(runtime.value.ui_url, "_blank", "noopener,noreferrer");
}

onMounted(load);
</script>

<style scoped>
.agentscope-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; align-items: start; }
.agentscope-card__head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.agentscope-card__title { margin: 0; font-size: var(--astra-font-label); }
.agentscope-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.astra-page p { margin: 4px 0; }
.astra-page code { padding: 1px 4px; border-radius: 4px; background: #f2f7f4; font-family: ui-monospace, monospace; }
@media (max-width: 1000px) { .agentscope-grid { grid-template-columns: 1fr; } }
</style>
