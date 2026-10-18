<template>
  <section>
    <div class="title-row">
      <div>
        <h1 class="h-title">数字员工管理</h1>
      </div>
      <el-button type="primary" @click="openCreate">创建数字员工</el-button>
    </div>
    <div class="management-grid">
      <div class="panel management-main">
        <div class="manage-toolbar">
          <div><b>员工列表</b> <span class="muted">{{ filtered.length }} 位员工</span></div>
          <el-input v-model="query" placeholder="搜索员工 / 职责" style="width: 240px" clearable />
        </div>
        <table class="employee-table">
          <thead>
            <tr><th>数字员工</th><th>职责</th><th>技能</th><th>状态</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-if="!filtered.length"><td colspan="5"><div class="empty">没有找到符合条件的员工</div></td></tr>
            <tr
              v-for="employee in filtered"
              :key="employee.id"
              :class="{ selected: employee.id === selectedId }"
              @click="selectedId = employee.id"
            >
              <td>
                <div class="person-cell">
                  <span class="avatar" :class="{ off: !employee.enabled }">{{ employee.avatar }}</span>
                  <span class="person-name"><b>{{ employee.name }}</b><small class="person-id">{{ employee.id }}</small></span>
                </div>
              </td>
              <td>
                <b>{{ employee.domain }}</b>
              </td>
              <td>
                <div class="chips">
                  <span v-for="skill in employee.skills" :key="skill" class="mini-chip">{{ resourceLabel(catalogs.skills, skill) }}</span>
                  <span v-if="!employee.skills.length" class="muted">未选择</span>
                </div>
              </td>
              <td>
                <el-switch :model-value="employee.enabled" :aria-label="`${employee.name} 启用`" @change="toggle(employee, Boolean($event))" @click.stop />
              </td>
              <td>
                <el-button link type="primary" @click.stop="goChat(employee.id)">对话</el-button>
                <el-dropdown trigger="click" @command="(command: string) => onEmployeeCommand(employee, command)">
                  <el-button link type="primary" @click.stop>更多</el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="edit">编辑</el-dropdown-item>
                      <el-dropdown-item command="delete">删除</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <aside class="panel management-side">
        <template v-if="selected">
          <div class="profile-hero">
            <span class="avatar" :class="{ off: !selected.enabled }">{{ selected.avatar }}</span>
            <h3>{{ selected.name }}</h3>
            <p class="muted">{{ selected.domain }}</p>
            <span class="tag" :class="{ ash: !selected.enabled }">{{ selected.enabled ? "已启用" : "已禁用" }}</span>
          </div>
          <el-button style="margin-top: 12px" @click="profileOpen = !profileOpen">{{ profileOpen ? "收起档案" : "展开档案" }}</el-button>
          <template v-if="profileOpen">
            <div class="section-label">职责范围</div>
            <div class="detail-excerpt">{{ selected.description }}</div>
            <div class="section-label">知识资源</div>
            <div class="resource-list">
              <span v-for="item in selected.knowledge" :key="item" class="mini-chip">{{ resourceLabel(catalogs.knowledge, item) }}</span>
              <span v-if="!selected.knowledge.length" class="muted">暂无配置</span>
            </div>
            <div class="section-label">技能与工具</div>
            <div class="resource-list">
              <span v-for="item in selected.skills" :key="item" class="mini-chip">{{ resourceLabel(catalogs.skills, item) }}</span>
              <span v-for="item in selected.tools" :key="item" class="mini-chip">{{ toolLabel(item) }}</span>
            </div>
            <div class="section-label">Agent 标识</div>
            <div class="mono">{{ selected.agentscope_agent_id || "尚未创建" }}</div>
            <el-button style="margin-top: 14px" @click="openEdit(selected)">编辑</el-button>
          </template>
        </template>
        <div v-else class="empty">创建第一个数字员工，直接定义他的职责与能力</div>
      </aside>
    </div>
    <EmployeeDrawer :open="drawerOpen" :employee="editing" :catalogs="catalogs" :routes="routes" :route-error="routeError" @close="drawerOpen = false" @save="save" @reload-catalogs="reloadCatalogs" />
    <el-dialog v-model="deleteOpen" title="删除数字员工" width="460px">
      <p>确定删除 <b>{{ deleting?.name }}</b> 吗？删除后将不再显示该员工。</p>
      <template #footer>
        <el-button @click="deleteOpen = false">取消</el-button>
        <el-button type="danger" @click="confirmDelete">确认删除</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import EmployeeDrawer from "../components/EmployeeDrawer.vue";
import { deleteEmployee, getKnowledge, getSkills, getTools, listEmployees, saveEmployee } from "../api";
import { gatewayApi, type GatewayRoute } from "../api/gateway";
import type { Employee, ResourceCatalog } from "../types";

const router = useRouter();
const employees = ref<Employee[]>([]);
const query = ref("");
const selectedId = ref<string | null>(null);
const drawerOpen = ref(false);
const editing = ref<Employee | null>(null);
const deleteOpen = ref(false);
const deleting = ref<Employee | null>(null);
const profileOpen = ref(false);
const emptyCatalog = (): ResourceCatalog => ({ available: false, items: [] });
const catalogs = ref({ skills: emptyCatalog(), knowledge: emptyCatalog(), tools: emptyCatalog() });
const routes = ref<GatewayRoute[]>([]);
const routeError = ref("");

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase();
  if (!needle) return employees.value;
  return employees.value.filter((item) => `${item.name} ${item.domain} ${item.description}`.toLowerCase().includes(needle));
});
const selected = computed(() => employees.value.find((item) => item.id === selectedId.value) ?? null);

function resourceLabel(catalog: ResourceCatalog, id: string) {
  return catalog.items.find((item) => item.id === id)?.name ?? id;
}
function toolLabel(id: string) {
  if (id === "contact_employee") return "联系其他数字员工";
  const name = resourceLabel(catalogs.value.tools, id);
  return name === id ? "工作区工具" : name;
}
function onEmployeeCommand(employee: Employee, command: string) {
  if (command === "edit") openEdit(employee);
  if (command === "delete") askDelete(employee);
}

async function reload() {
  employees.value = await listEmployees();
  if (!employees.value.some((item) => item.id === selectedId.value)) {
    selectedId.value = employees.value[0]?.id ?? null;
  }
}

function openCreate() {
  editing.value = null;
  drawerOpen.value = true;
}
function openEdit(employee: Employee) {
  editing.value = employee;
  drawerOpen.value = true;
}
function goChat(id: string) {
  void router.push({ path: "/chat", query: { employee: id } });
}
function askDelete(employee: Employee) {
  deleting.value = employee;
  deleteOpen.value = true;
}

async function save(body: Omit<Employee, "id" | "agentscope_agent_id" | "mailbox_session_id">) {
  if (!body.name || !body.domain || !body.description || !body.instructions) {
    ElMessage.warning("请完善必填信息");
    return;
  }
  try {
    const result = await saveEmployee(body, editing.value?.id);
    selectedId.value = result.employee.id;
    drawerOpen.value = false;
    await reload();
    ElMessage.success(result.agentscope.ok ? "员工已保存，并同步到 AgentScope" : `员工已保存。${result.agentscope.detail}`);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存失败");
  }
}

async function toggle(employee: Employee, enabled: boolean) {
  try {
    await saveEmployee({ ...employee, enabled }, employee.id);
    await reload();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "更新失败");
  }
}

async function confirmDelete() {
  if (!deleting.value) return;
  try {
    await deleteEmployee(deleting.value.id);
    deleteOpen.value = false;
    await reload();
    ElMessage.success("员工已删除");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "删除失败");
  }
}

async function reloadCatalogs() {
  const [skills, knowledge, tools] = await Promise.all([getSkills(), getKnowledge(), getTools()]);
  catalogs.value = { skills, knowledge, tools };
}

onMounted(async () => {
  await reloadCatalogs();
  try {
    routes.value = (await gatewayApi.routes()).filter((item) => item.enabled);
  } catch (error) {
    routeError.value = error instanceof Error ? error.message : "无法读取模型路由";
  }
  await reload();
});
</script>
