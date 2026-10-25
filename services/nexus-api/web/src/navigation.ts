
export interface NavItemConfig {
  to: string;
  label: string;
  isActive: (path: string) => boolean;
}

export interface NavItem {
  to: string;
  label: string;
  active: boolean;
}

export const PRIMARY_NAV_CONFIGS: readonly NavItemConfig[] = [
  {
    to: "/chat",
    label: "数字员工",
    isActive: (path: string) => path === "/chat" || path === "/team",
  },
  {
    to: "/tasks",
    label: "任务大厅",
    isActive: (path: string) => path.startsWith("/tasks") || path.startsWith("/interventions"),
  },
  {
    to: "/library",
    label: "数字图书馆",
    isActive: (path: string) => path.startsWith("/library"),
  },
  {
    to: "/gateway",
    label: "API 网关",
    isActive: (path: string) => path.startsWith("/gateway"),
  },
  {
    to: "/settings",
    label: "配置中心",
    isActive: (path: string) =>
      path.startsWith("/settings") ||
      path.startsWith("/employees") ||
      path.startsWith("/workers") ||
      path.startsWith("/schedules"),
  },
];

export function getVisibleNavItems(currentPath: string = ""): NavItem[] {
  return PRIMARY_NAV_CONFIGS.map((item) => ({
    to: item.to,
    label: item.label,
    active: item.isActive(currentPath),
  }));
}
