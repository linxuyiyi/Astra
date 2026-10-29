import catalog from "../nexus_api/employee_avatars.json" with { type: "json" };

export interface EmployeeAppearance {
  id: string;
  label: string;
  family: string;
  source: string;
  portrait: string;
  office: { frames: string[]; width: number; height: number; top: number };
}
/** avatar ID 是唯一事实源，不依据姓名、岗位、顺序或状态重新选脸。 */
export const EMPLOYEE_APPEARANCES: readonly EmployeeAppearance[] =
  catalog.items;
export const DEFAULT_EMPLOYEE_AVATAR = catalog.defaultId;
const byId = new Map(EMPLOYEE_APPEARANCES.map((item) => [item.id, item]));
export function employeeAppearance(id: string): EmployeeAppearance | undefined {
  return byId.get(id);
}
