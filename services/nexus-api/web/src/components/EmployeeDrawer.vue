<template>
  <el-drawer
    :model-value="open"
    :before-close="requestClose"
    append-to-body
    :title="employee ? `编辑数字员工 · ${employee.name}` : '新建数字员工'"
    size="min(680px, 100%)"
    :close-on-click-modal="false"
  >
    <el-form ref="formRef" :disabled="saving" :model="form" :rules="rules" label-position="top" scroll-to-error @submit.prevent="submit">
      <div class="section-cap">基础与模型</div>
      <div class="pair">
        <el-form-item label="员工名称" prop="name">
          <el-input v-model="form.name" maxlength="24" placeholder="例如：星河" />
        </el-form-item>
        <el-form-item label="职责领域" prop="domain">
          <el-input v-model="form.domain" maxlength="42" placeholder="例如：质量自动化工程师" />
        </el-form-item>
      </div>

      <el-form-item label="模型路由">
        <el-select v-model="form.model_route_id" clearable filterable placeholder="选择已启用的对话模型路由" style="width: 100%">
          <el-option v-for="item in routes" :key="item.id" :label="item.model" :value="item.id" />
        </el-select>
        <div v-if="routeError" class="field-hint">{{ routeError }}</div>
        <div v-else-if="missingRoute" class="field-hint">当前绑定的路由未在已启用列表中</div>
      </el-form-item>

      <el-form-item label="员工形象">
        <div class="avatar-box" title="像素人物资源：BotPixel · MIT">
          <div class="avatar-grid" role="group" aria-label="可选员工形象">
            <button
              v-for="item in avatars"
              :key="item.id"
              type="button"
              :disabled="saving"
              :aria-pressed="form.avatar === item.id"
              :aria-label="`选择${item.label}`"
              :class="{ selected: form.avatar === item.id }"
              @click="form.avatar = item.id"
            >
              <EmployeeAvatar :avatar="item.id" :size="40" />
              <span>{{ item.label }}</span>
            </button>
          </div>
        </div>
      </el-form-item>

      <el-form-item label="职责说明" prop="description">
        <el-input v-model="form.description" type="textarea" :rows="2" placeholder="简述职责范围与边界" />
      </el-form-item>

      <el-form-item label="工作指令（支持 Markdown）" prop="instructions">
        <div class="inst-box">
          <div class="inst-box__bar">
            <span class="inst-box__count">{{ form.instructions.length }} 字</span>
            <el-radio-group v-model="instMode" size="small" aria-label="工作指令视图">
              <el-radio-button value="edit">编辑</el-radio-button>
              <el-radio-button value="preview">预览</el-radio-button>
            </el-radio-group>
          </div>
          <el-input
            v-if="instMode === 'edit'"
            v-model="form.instructions"
            class="inst-box__input"
            type="textarea"
            :rows="6"
            resize="vertical"
            placeholder="定义该员工的思考方式、回复格式与协作习惯"
          />
          <div v-else class="inst-box__preview">
            <MarkdownView :content="form.instructions" compact empty-text="暂无工作指令" />
          </div>
        </div>
      </el-form-item>

      <div class="section-cap">能力与知识绑定</div>
      <div class="pair">
        <el-form-item label="知识库">
          <el-select
            v-model="form.knowledge"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            :placeholder="catalogs.knowledge.available ? '选择知识库' : '暂不可用'"
            style="width: 100%"
            :disabled="saving || !catalogs.knowledge.available"
          >
            <el-option v-for="item in knowledgeOptions" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
          <el-button v-if="!catalogs.knowledge.available" size="small" style="margin-top: 6px" @click="emit('reload-catalogs')">重新读取</el-button>
        </el-form-item>

        <el-form-item label="技能">
          <el-select
            v-model="form.skills"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            :placeholder="skillOptions.length ? '选择技能' : '暂无可选技能'"
            style="width: 100%"
            :disabled="saving || !skillOptions.length"
          >
            <el-option v-for="item in skillOptions" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </el-form-item>

        <el-form-item label="MCP">
          <el-select
            v-model="form.mcps"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            :placeholder="mcpOptions.length ? '选择 MCP' : '暂无可选 MCP'"
            style="width: 100%"
            :disabled="saving || !mcpOptions.length"
          >
            <el-option v-for="item in mcpOptions" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </el-form-item>

        <el-form-item label="工具">
          <el-select
            v-model="form.tools"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            :placeholder="catalogs.tools.available ? '选择工具' : '暂不可用'"
            style="width: 100%"
            :disabled="saving || !catalogs.tools.available"
          >
            <el-option v-for="item in toolOptions" :key="item.id" :label="toolLabel(item)" :value="item.id" />
          </el-select>
        </el-form-item>
      </div>
    </el-form>

    <template #footer>
      <el-alert v-if="error" class="save-error" :title="error" type="error" show-icon :closable="false" />
      <div class="footer">
        <el-switch v-model="form.enabled" :disabled="saving" active-text="启用此员工" aria-label="启用此员工" />
        <div class="footer-right">
          <el-tag v-if="guard.dirty.value" size="small" type="warning" effect="light">有未保存修改</el-tag>
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
import MarkdownView from "./astra/MarkdownView.vue";
import EmployeeAvatar from "./EmployeeAvatar.vue";
import { EMPLOYEE_APPEARANCES, DEFAULT_EMPLOYEE_AVATAR } from "../../../shared/employeeAvatar.ts";

const formRef = ref<FormInstance>();
const instMode = ref<"edit" | "preview">("edit");

const nonEmptyRule = (label: string) => [
  { required: true, message: `请输入${label}`, trigger: ["blur", "change"] },
  {
    validator: (_rule: unknown, value: string, callback: (err?: Error) => void) => {
      if (!value || !value.trim()) callback(new Error(`${label}不能为空`));
      else callback();
    },
    trigger: ["blur", "change"],
  },
];

const rules: FormRules = {
  name: nonEmptyRule("员工名称"),
  domain: nonEmptyRule("职责领域"),
  description: nonEmptyRule("职责说明"),
  instructions: nonEmptyRule("工作指令"),
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

const avatars = EMPLOYEE_APPEARANCES;
const defaultInstructions = "收到消息后先判断能否直接回答；信息不足时追问；需要其他员工帮助时联系相应员工。";
const form = reactive({
  name: "",
  domain: "",
  description: "",
  instructions: defaultInstructions,
  avatar: DEFAULT_EMPLOYEE_AVATAR,
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
    title: "放弃未保存的员工修改？",
    message: "员工配置尚未保存，关闭后将无法恢复。",
    confirmLabel: "放弃并关闭",
    cancelLabel: "继续编辑",
    danger: true,
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
    form.avatar = employee?.avatar ?? DEFAULT_EMPLOYEE_AVATAR;
    form.enabled = employee?.enabled ?? true;
    form.knowledge = [...(employee?.knowledge ?? [])];
    form.skills = [...(employee?.skills ?? [])];
    form.mcps = [...(employee?.mcps ?? [])];
    form.tools = [...(employee?.tools ?? defaultTools())];
    form.model_route_id = employee?.model_route_id ?? "";
    instMode.value = "edit";
    guard.capture();
    void nextTick(() => { formRef.value?.clearValidate(); });
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
.section-cap {
  margin: 4px 0 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--astra-border-subtle);
  color: var(--astra-text-muted);
  font-size: 12px;
  font-weight: 600;
}
.save-error { margin-bottom: 12px; text-align: left; }
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.field-hint { margin-top: 4px; color: var(--astra-text-muted); font-size: 12px; }
.footer { display: flex; justify-content: space-between; align-items: center; gap: 12px; width: 100%; }
.footer-right { display: flex; align-items: center; gap: 8px; }

.avatar-box { width: 100%; }
.avatar-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 8px;
}
.avatar-grid button {
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  min-width: 0; padding: 8px 4px 6px;
  border: 1px solid var(--astra-border); border-radius: 8px;
  background: var(--astra-bg-panel); color: var(--astra-text-secondary);
  cursor: pointer; font-size: 11px; line-height: 1.3;
  transition: border-color 0.15s ease, background-color 0.15s ease, color 0.15s ease;
}
.avatar-grid button span { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.avatar-grid button:hover { border-color: var(--astra-border-hover); background: var(--astra-bg-subtle); color: var(--astra-text-primary); }
.avatar-grid button.selected {
  border-color: var(--astra-primary); background: var(--astra-bg-elevated);
  box-shadow: inset 0 0 0 1px var(--astra-primary);
  color: var(--astra-text-primary); font-weight: 600;
}
.avatar-grid button:disabled { opacity: 0.65; cursor: default; }

.inst-box {
  display: grid; width: 100%;
  border: 1px solid var(--astra-border); border-radius: 8px; overflow: hidden; background: var(--astra-bg-panel);
}
.inst-box__bar {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 6px 10px; border-bottom: 1px solid var(--astra-border); background: var(--astra-bg-subtle);
}
.inst-box__count { color: var(--astra-text-muted); font-size: 11.5px; font-variant-numeric: tabular-nums; }
.inst-box__input :deep(.el-textarea__inner) {
  border: 0; border-radius: 0; box-shadow: none !important; padding: 10px 12px;
  font-family: var(--astra-font-mono); font-size: 12.5px; line-height: 1.6;
}
.inst-box__preview { max-height: 220px; overflow-y: auto; padding: 12px 14px; }

@media (max-width: 640px) {
  .pair { grid-template-columns: 1fr; }
  .avatar-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}
</style>
