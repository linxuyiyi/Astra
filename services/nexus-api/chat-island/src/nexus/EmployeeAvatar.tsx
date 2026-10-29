import { employeeAppearance } from "../../../shared/employeeAvatar.ts";

export function EmployeeAvatar({ avatar }: { avatar: string }) {
  const appearance = employeeAppearance(avatar);
  return appearance ? (
    <img className="employee-pixel-avatar" src={appearance.portrait} alt="" />
  ) : (
    <span className="employee-avatar-unconfigured" aria-label="员工形象未配置">
      未配置
    </span>
  );
}
