<template>
  <el-drawer v-model="visible" append-to-body :title="employee ? '编辑数字员工' : '创建数字员工'" size="610px" :close-on-click-modal="false" @close="onClose">
    <el-form label-position="top" @submit.prevent="submit">
      <div class="form-cap">身份与职责</div>
      <div class="pair">
        <el-form-item label="员工名称" required>
          <el-input v-model="form.name" maxlength="24" placeholder="例如：星河" />
        </el-form-item>
        <el-form-item label="责任领域" required>
          <el-input v-model="form.domain" maxlength="42" placeholder="例如：测试自动化工程师" />
        </el-form-item>
      </div>
      <el-form-item label="员工形象">
        <el-select v-model="form.avatar" style="width: 100%">
          <el-option v-for="item in avatars" :key="item" :label="item" :value="item" />
        </el-select>
      </el-form-item>
      <el-form-item required>
        <template #label>职责说明</template>
        <el-input v-model="form.description" type="textarea" :rows="3" placeholder="例如：负责测试平台的问题定位和需求澄清，不直接改生产配置。" />
      </el-form-item>
      <el-form-item required>
        <template #label>工作指令</template>
        <el-input v-model="form.instructions" type="textarea" :rows="5" placeholder="例如：先确认现象和影响范围，信息不足时追问，需要同事时再联系对方。" />
      </el-form-item>
      <el-form-item>
        <template #label>模型路由</template>
        <el-select v-model="form.model_route_id" clearable filterable placeholder="选择已启用的路由" style="width: 100%">
          <el-option v-for="item in routes" :key="item.id" :label="item.model" :value="item.id" />
        </el-select>
        <div v-if="routeError" class="muted">{{ routeError }}</div>
        <div v-if="missingRoute" class="muted">当前路由不在已启用列表中。</div>
      </el-form-item>
      <div class="form-cap">知识与工具</div>
      <el-form-item>
        <template #label>知识</template>
        <el-select
          v-model="form.knowledge"
          multiple
          filterable
          :placeholder="catalogs.knowledge.available ? '选择知识库' : '当前无法读取知识库'"
          style="width: 100%"
          :disabled="!catalogs.knowledge.available"
        >
          <el-option v-for="item in knowledgeOptions" :key="item.id" :label="item.name" :value="item.id" />
        </el-select>
        <el-button v-if="!catalogs.knowledge.available" style="margin-top: 8px" @click="emit('reload-catalogs')">重新读取知识库</el-button>
      </el-form-item>
      <el-form-item>
        <template #label>技能</template>
        <el-select
          v-model="form.skills"
          multiple
          filterable
          :placeholder="skillOptions.length ? '选择技能' : '当前没有可选技能'"
          style="width: 100%"
          :disabled="!skillOptions.length"
        >
          <el-option v-for="item in skillOptions" :key="item.id" :label="item.name" :value="item.id" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <template #label>MCP</template>
        <el-select
          v-model="form.mcps"
          multiple
          filterable
          :placeholder="mcpOptions.length ? '选择 MCP' : '当前没有可选 MCP'"
          style="width: 100%"
          :disabled="!mcpOptions.length"
        >
          <el-option v-for="item in mcpOptions" :key="item.id" :label="item.name" :value="item.id" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <template #label>工具</template>
        <el-select
          v-model="form.tools"
          multiple
          filterable
          :placeholder="catalogs.tools.available ? '选择工具' : '当前无法确认工具'"
          style="width: 100%"
          :disabled="!catalogs.tools.available"
        >
          <el-option v-for="item in toolOptions" :key="item.id" :label="toolLabel(item)" :value="item.id" />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <div class="footer">
        <el-switch v-model="form.enabled" active-text="启用此员工" />
        <div>
          <el-button @click="emit('close')">取消</el-button>
          <el-button type="primary" @click="submit">保存员工</el-button>
        </div>
      </div>
    </template>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from "vue";
import type { Employee, ResourceCatalog } from "../types";
import type { GatewayRoute } from "../api/gateway";
import { formatToolLabel } from "../utils/tools";

const props = defineProps<{
  open: boolean;
  employee: Employee | null;
  catalogs: {
    skills: ResourceCatalog;
    mcps: ResourceCatalog;
    knowledge: ResourceCatalog;
    tools: ResourceCatalog;
  };
  routes: GatewayRoute[];
  routeError: string;
}>();
const emit = defineEmits<{
  close: [];
  save: [body: Omit<Employee, "id" | "agentscope_agent_id" | "mailbox_session_id">];
  "reload-catalogs": [];
}>();

const avatars = ["👩🏻‍💻", "🧑🏻‍🔬", "👩🏽‍🚀", "🤖", "🧑🏽‍💻"];
const defaultInstructions = "收到消息后先判断能否直接回答；信息不足时追问；需要其他员工帮助时联系相应员工。";
const form = reactive({
  name: "",
  domain: "",
  description: "",
  instructions: defaultInstructions,
  avatar: "👩🏻‍💻",
  enabled: true,
  knowledge: [] as string[],
  skills: [] as string[],
  mcps: [] as string[],
  tools: [] as string[],
  model_route_id: "",
});

const visible = ref(false);
let openedAt = 0;
watch(
  () => props.open,
  async (open) => {
    if (!open) {
      visible.value = false;
      return;
    }
    await nextTick();
    openedAt = Date.now();
    visible.value = true;
  },
  { immediate: true },
);
function onClose() {
  if (props.open && Date.now() - openedAt < 400) {
    visible.value = true;
    return;
  }
  if (props.open) emit("close");
}

const skillOptions = computed(() => optionsFor(props.catalogs.skills, form.skills));
const mcpOptions = computed(() => optionsFor(props.catalogs.mcps, form.mcps));
const knowledgeOptions = computed(() => optionsFor(props.catalogs.knowledge, form.knowledge));
const toolOptions = computed(() => optionsFor(props.catalogs.tools, form.tools));
const missingRoute = computed(
  () => Boolean(form.model_route_id) && !props.routes.some((item) => item.id === form.model_route_id),
);

watch(
  () => [props.open, props.employee] as const,
  () => {
    const employee = props.employee;
    form.name = employee?.name ?? "";
    form.domain = employee?.domain ?? "";
    form.description = employee?.description ?? "";
    form.instructions = employee?.instructions ?? defaultInstructions;
    form.avatar = employee?.avatar ?? "👩🏻‍💻";
    form.enabled = employee?.enabled ?? true;
    form.knowledge = [...(employee?.knowledge ?? [])];
    form.skills = [...(employee?.skills ?? [])];
    form.mcps = [...(employee?.mcps ?? [])];
    form.tools = [...(employee?.tools ?? defaultTools())];
    form.model_route_id = employee?.model_route_id ?? "";
  },
  { immediate: true },
);

function optionsFor(catalog: ResourceCatalog, selected: string[]) {
  const options = new Map(catalog.items.map((item) => [item.id, item]));
  for (const id of selected) {
    if (!options.has(id)) options.set(id, { id, name: id });
  }
  return [...options.values()];
}

function toolLabel(item: { id: string; name: string }) {
  return formatToolLabel(item.id, item.name);
}

function defaultTools(): string[] {
  if (!props.catalogs.tools.available) return [];
  const known = props.catalogs.tools.items.some((item) => item.id === "contact_employee");
  return known ? ["contact_employee"] : [];
}

function submit() {
  emit("save", {
    name: form.name.trim(),
    domain: form.domain.trim(),
    description: form.description.trim(),
    instructions: form.instructions.trim(),
    avatar: form.avatar,
    enabled: form.enabled,
    knowledge: [...form.knowledge],
    skills: [...form.skills],
    mcps: [...form.mcps],
    tools: [...form.tools],
    model_route_id: form.model_route_id,
  });
}
</script>

<style scoped>
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.footer { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
</style>
