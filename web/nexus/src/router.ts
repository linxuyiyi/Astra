import { createRouter, createWebHistory, type LocationQueryRaw, type RouteLocationGeneric } from "vue-router";
import DigitalEmployeeChatView from "./views/DigitalEmployeeChatView.vue";
import TaskListView from "./views/TaskListView.vue";
import TaskDetailView from "./views/TaskDetailView.vue";
import EmployeeListView from "./views/EmployeeListView.vue";
import EmployeeDetailView from "./views/EmployeeDetailView.vue";
import DigitalLibraryView from "./views/DigitalLibraryView.vue";
import EmployeesView from "./views/EmployeesView.vue";
import SettingsView from "./views/SettingsView.vue";
import AgentscopeView from "./views/settings/AgentscopeView.vue";
import CategoryConfig from "./views/settings/CategoryConfig.vue";
import GatewayView from "./views/gateway/GatewayView.vue";
import GatewayNodeDetailView from "./views/gateway/GatewayNodeDetailView.vue";
import GatewayOverview from "./views/gateway/GatewayOverview.vue";
import GatewayNodes from "./views/gateway/GatewayNodes.vue";
import GatewayPoolsRoutes from "./views/gateway/GatewayPoolsRoutes.vue";
import GatewayKeys from "./views/gateway/GatewayKeys.vue";
import GatewayRequests from "./views/gateway/GatewayRequests.vue";
import GatewayAudits from "./views/gateway/GatewayAudits.vue";

const gatewayTabSegments: Record<string, string> = {
  overview: "overview",
  nodes: "nodes",
  routes: "routes",
  keys: "keys",
  logs: "logs",
};

function gatewayIndexRedirect(to: RouteLocationGeneric) {
  const tab = typeof to.query.tab === "string" ? to.query.tab : "";
  const segment = gatewayTabSegments[tab] ?? "overview";
  const { tab: _tab, ...rest } = to.query;
  return { path: `/gateway/${segment}`, query: rest as LocationQueryRaw };
}

function keepQuery(path: string) {
  return (to: RouteLocationGeneric) => ({ path, query: to.query, hash: to.hash });
}

function teamRedirect(to: RouteLocationGeneric) {
  const { channel, ...rest } = to.query;
  return {
    path: "/chat",
    query: {
      ...rest,
      type: "group",
      ...(typeof channel === "string" ? { group: channel } : {}),
    } as LocationQueryRaw,
    hash: to.hash,
  };
}

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/chat" },
    { path: "/employees", redirect: keepQuery("/settings/employees") },
    { path: "/chat", name: "chat", component: DigitalEmployeeChatView },
    { path: "/team", redirect: teamRedirect },
    { path: "/tasks", name: "tasks", component: TaskListView },
    { path: "/tasks/:id", name: "task-detail", component: TaskDetailView },
    { path: "/workers", redirect: keepQuery("/settings/workers") },
    { path: "/workers/:id", redirect: (to) => ({ path: `/settings/workers/${String(to.params.id)}`, query: to.query, hash: to.hash }) },
    { path: "/library", name: "library", component: DigitalLibraryView },
    {
      path: "/settings",
      name: "settings",
      component: SettingsView,
      children: [
        { path: "", redirect: "/settings/employees" },
        { path: "employees", name: "settings-employees", component: EmployeesView },
        { path: "workers", name: "settings-workers", component: EmployeeListView },
        { path: "workers/:id", name: "settings-worker-detail", component: EmployeeDetailView },
        { path: "categories", name: "settings-categories", component: CategoryConfig },
        { path: "agentscope", name: "settings-agentscope", component: AgentscopeView },
      ],
    },
    {
      path: "/interventions",
      redirect: (to) => ({ path: "/tasks", query: { ...to.query, view: "interventions" }, hash: to.hash }),
    },
    {
      path: "/gateway",
      component: GatewayView,
      children: [
        { path: "", name: "gateway", redirect: gatewayIndexRedirect },
        { path: "overview", name: "gateway-overview", component: GatewayOverview },
        { path: "nodes", name: "gateway-nodes", component: GatewayNodes },
        { path: "routes", name: "gateway-routes", component: GatewayPoolsRoutes },
        { path: "keys", name: "gateway-keys", component: GatewayKeys },
        { path: "logs", name: "gateway-logs", component: GatewayRequests },
        { path: "audits", name: "gateway-audits", component: GatewayAudits },
        { path: "nodes/:id", name: "gateway-node-detail", component: GatewayNodeDetailView },
        { path: ":removed(versions|self)", redirect: "/gateway/overview" },
      ],
    },
  ],
});
