<template>
  <el-drawer
    :model-value="open"
    :before-close="requestClose"
    append-to-body
    :title="employee ? `编辑数字员工 · ${employee.name}` : '新建数字员工'"
    size="var(--astra-drawer-md)"
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

      <el-form-item label="人设与工作指南（支持 Markdown）" prop="instructions">
        <div class="inst-box">
          <div class="inst-box__bar">
            <span class="inst-box__count">{{ form.instructions.length }} 字 · 定义性格口吻、专业立场与协作方式</span>
            <el-radio-group v-model="instMode" size="small" aria-label="人设与工作指南视图">
              <el-radio-button value="edit">编辑</el-radio-button>
              <el-radio-button value="preview">预览</el-radio-button>
            </el-radio-group>
          </div>
          <div v-if="instMode === 'edit'" class="preset-bar" role="group" aria-label="快速套用人设模板">
            <span class="preset-bar__label">参考模板：</span>
            <button
              v-for="tpl in PERSONA_TEMPLATES"
              :key="tpl.id"
              type="button"
              class="preset-chip"
              :class="{ 'is-active': form.instructions.trim() === tpl.content.trim() }"
              :disabled="saving"
              @click="applyPersonaTemplate(tpl)"
            >
              {{ tpl.label }}
            </button>
          </div>
          <el-input
            v-if="instMode === 'edit'"
            v-model="form.instructions"
            class="inst-box__input"
            type="textarea"
            :rows="7"
            resize="vertical"
            placeholder="写清这位员工的性格与说话风格、专业判断偏好、日常对话与交付物的不同节奏"
          />
          <div v-else class="inst-box__preview">
            <MarkdownView :content="form.instructions" compact empty-text="暂无人设与工作指南" />
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

interface PersonaTemplate {
  id: string;
  label: string;
  content: string;
}

const PERSONA_TEMPLATES: readonly PersonaTemplate[] = [
  {
    id: "colleague",
    label: "通用同事",
    content: [
      "## 性格与说话方式",
      "平实、可靠、直奔重点，像同一间办公室里共事的同事。日常讨论用自然的短段落交流，先抛核心判断再顺手问清楚关键前提，不端架子也不说客服套话。",
      "",
      "## 专业判断与协作",
      "- 信息不够时直接开口问清最关键的一两个点，不靠猜也不机械罗列免责声明。",
      "- 只有对方明确要文档、清单或对比表时，才切换到结构化长文输出。",
    ].join("\n"),
  },
  {
    id: "architect",
    label: "后端架构师",
    content: [
      "## 性格与说话方式",
      "沉稳、务实、抓大放小。日常聊方案或排查急事时先稳住节奏，用两三段口语说清最可能的根因与当下最稳的第一步；不炫技、不把测试命令或契约检查清单硬塞进普通聊天。",
      "",
      "## 专业偏好",
      "- 重视边界清晰、可回滚、最小改动；觉得方案有隐患时坦诚直言并给出替代路径。",
      "- 真正进入代码或接口改动评审时，再明确指出契约影响、数据兼容与回归验证方式。",
    ].join("\n"),
  },
  {
    id: "designer",
    label: "体验设计师",
    content: [
      "## 性格与说话方式",
      "敏锐、克制、有审美主见，聊起界面像坐在工位对面一起看稿。先说第一眼体感和信息层级判断，少用设计黑话，随口要截图看实图，不把闲聊写成设计规范论文。",
      "",
      "## 专业偏好",
      "- 相信减法、层级与留白胜过堆装饰和堆颜色；反对没定位病因就盲目大改。",
      "- 只有出正式评审或改版方案时，才用分点或对照表拆细节。",
    ].join("\n"),
  },
  {
    id: "qa",
    label: "测试工程师",
    content: [
      "## 性格与说话方式",
      "冷静、细心但不死板，擅长帮同事提前兜住容易漏测的坑。平时聊天先点出最值钱的 2–3 个风险和边界条件，不一股脑抛十几条测试手册。",
      "",
      "## 专业偏好",
      "- 关注真实用户路径、异常恢复、并发与边界输入；坚持以实际复现证据说话。",
      "- 需要出正式验收清单或回归用例时，再给出清晰的步骤、预期结果与通过标准。",
    ].join("\n"),
  },
  {
    id: "ops",
    label: "运营 / 项目协作",
    content: [
      "## 性格与说话方式",
      "轻快、有条理、推进感强，善于把模糊想法收敛成下一步动作。回复简明直接，先对齐目标、负责人和时间节点，不绕弯子。",
      "",
      "## 专业偏好",
      "- 关注用户真实反馈与业务结果，帮团队从一堆待办里挑出最值得先做的一件事。",
      "- 汇报材料或周报类请求再切到结构化大纲，日常沟通保持短促高效。",
    ].join("\n"),
  },
];

const defaultInstructions = PERSONA_TEMPLATES[0]!.content;

async function applyPersonaTemplate(tpl: PersonaTemplate) {
  const current = form.instructions.trim();
  const isBuiltin = !current || PERSONA_TEMPLATES.some((item) => item.content.trim() === current);
  if (!isBuiltin) {
    const ok = await confirmDialog({
      title: `套用「${tpl.label}」人设模板？`,
      message: "当前填写的人设与工作指南将被替换为模板内容，替换后可继续微调。",
      confirmLabel: "套用模板",
      cancelLabel: "保留当前内容",
    });
    if (!ok) return;
  }
  form.instructions = tpl.content;
}
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
.preset-bar {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  padding: 6px 10px; border-bottom: 1px solid var(--astra-border-subtle); background: var(--astra-bg-panel);
}
.preset-bar__label { color: var(--astra-text-muted); font-size: 11.5px; flex: none; }
.preset-chip {
  height: 22px; padding: 0 9px;
  border: 1px solid var(--astra-border); border-radius: 999px;
  background: var(--astra-bg-subtle); color: var(--astra-text-secondary);
  font-size: 11.5px; font-weight: 500; cursor: pointer;
  transition: border-color 0.14s ease, background-color 0.14s ease, color 0.14s ease;
}
.preset-chip:hover:not(:disabled) {
  border-color: var(--astra-border-hover); background: var(--astra-bg-elevated); color: var(--astra-text-primary);
}
.preset-chip.is-active {
  border-color: var(--astra-primary); background: var(--astra-primary); color: #ffffff;
}
.preset-chip:disabled { opacity: 0.6; cursor: default; }
.inst-box__input :deep(.el-textarea__inner) {
  border: 0; border-radius: 0; box-shadow: none !important; padding: 10px 12px;
  font-family: var(--astra-font-mono); font-size: 12.5px; line-height: 1.6;
}
.inst-box__preview { max-height: 220px; overflow-y: auto; padding: 12px 14px; }

@media (min-width: 2240px) {
  .inst-box__preview { max-height: 360px; }
}

@media (max-width: 640px) {
  .pair { grid-template-columns: 1fr; }
  .avatar-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}
</style>
