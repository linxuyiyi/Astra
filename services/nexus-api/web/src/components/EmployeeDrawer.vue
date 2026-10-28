<template>
  <el-drawer :model-value="open" :before-close="requestClose" append-to-body :title="employee ? '编辑数字员工' : '创建数字员工'" size="610px" :close-on-click-modal="false">
    <el-form ref="formRef" :disabled="saving" :model="form" :rules="rules" label-position="top" scroll-to-error @submit.prevent="submit">
      <div class="form-cap">身份与职责</div>
      <div class="pair">
        <el-form-item label="员工名称" prop="name">
          <el-input v-model="form.name" maxlength="24" placeholder="例如：星河" />
        </el-form-item>
        <el-form-item label="责任领域" prop="domain">
          <el-input v-model="form.domain" maxlength="42" placeholder="例如：测试自动化工程师" />
        </el-form-item>
      </div>
      <el-form-item label="员工形象">
        <el-select v-model="form.avatar" style="width: 100%">
          <el-option v-for="item in avatars" :key="item" :label="item" :value="item" />
        </el-select>
      </el-form-item>
      <el-form-item label="职责说明" prop="description">
        <el-input v-model="form.description" type="textarea" :rows="3" placeholder="例如：负责测试平台的问题定位和需求澄清，不直接改生产配置。" />
      </el-form-item>
      <el-form-item label="工作指令" prop="instructions">
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
          :disabled="saving || !catalogs.knowledge.available"
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
          :disabled="saving || !skillOptions.length"
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
          :disabled="saving || !mcpOptions.length"
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
          :disabled="saving || !catalogs.tools.available"
        >
          <el-option v-for="item in toolOptions" :key="item.id" :label="toolLabel(item)" :value="item.id" />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-alert v-if="error" class="save-error" :title="error" type="error" show-icon :closable="false" />
      <div class="footer">
        <el-switch v-model="form.enabled" :disabled="saving" active-text="启用此员工" aria-label="启用此员工" />
        <div>
          <el-tag v-if="guard.dirty.value" type="warning">有未保存修改</el-tag>
          <el-button :disabled="saving" @click="requestClose">取消</el-button>
          <el-button type="primary" :loading="saving" @click="submit">保存员工</el-button>
        </div>
      </div>
    </template>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from "vue";
import { onBeforeRouteLeave } from "vue-router";
import { confirmDialog } from "../composables/useConfirm";
import { useFormGuard } from "../composables/useFormGuard";
import type { FormInstance, FormRules } from "element-plus";
import type { Employee, ResourceCatalog } from "../types";
import type { GatewayRoute } from "../api/gateway";
import { formatToolLabel } from "../utils/tools";

const formRef = ref<FormInstance>();
const rules: FormRules = {
  name: [
    { required: true, message: "请输入员工名称", trigger: ["blur", "change"] },
    {
      validator: (_rule, value, callback) => {
        if (!value || !value.trim()) {
          callback(new Error("员工名称不能为空"));
        } else {
          callback();
        }
      },
      trigger: ["blur", "change"],
    },
  ],
  domain: [
    { required: true, message: "请输入责任领域", trigger: ["blur", "change"] },
    {
      validator: (_rule, value, callback) => {
        if (!value || !value.trim()) {
          callback(new Error("责任领域不能为空"));
        } else {
          callback();
        }
      },
      trigger: ["blur", "change"],
    },
  ],
  description: [
    { required: true, message: "请输入职责说明", trigger: ["blur", "change"] },
    {
      validator: (_rule, value, callback) => {
        if (!value || !value.trim()) {
          callback(new Error("职责说明不能为空"));
        } else {
          callback();
        }
      },
      trigger: ["blur", "change"],
    },
  ],
  instructions: [
    { required: true, message: "请输入工作指令", trigger: ["blur", "change"] },
    {
      validator: (_rule, value, callback) => {
        if (!value || !value.trim()) {
          callback(new Error("工作指令不能为空"));
        } else {
          callback();
        }
      },
      trigger: ["blur", "change"],
    },
  ],
};

const props = defineProps<{
  open: boolean;
  saving: boolean;
  error: string;
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

const guard = useFormGuard({
  isOpen: () => props.open,
  isSaving: () => props.saving,
  snapshot: () => JSON.stringify(form),
  confirmDiscard: () => confirmDialog({
    title: "放弃未保存的员工修改？", message: "员工配置尚未保存，关闭后将无法恢复。",
    confirmLabel: "放弃并关闭", cancelLabel: "继续编辑", danger: true,
  }),
});
async function requestClose() {
  if (await guard.canClose()) emit("close");
}
onBeforeRouteLeave(() => guard.canClose());

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
    if (!props.open) return;
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
    guard.capture();
    void nextTick(() => {
      formRef.value?.clearValidate();
    });
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

async function submit() {
  if (props.saving || !formRef.value) return;
  const valid = await formRef.value.validate().catch(() => false);
  if (!valid) return;
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
.save-error { margin-bottom: 12px; text-align: left; }
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.footer { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
</style>
