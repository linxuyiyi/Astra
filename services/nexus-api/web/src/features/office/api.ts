import type { EmployeeOffice } from "./model";

export async function getEmployeeOffice(
  id: string,
  workerPage: number,
  issuePage: number,
  signal: AbortSignal,
): Promise<EmployeeOffice> {
  const query = new URLSearchParams({
    workerPage: String(workerPage),
    issuePage: String(issuePage),
    pageSize: "6",
  });
  const response = await fetch(
    `/api/v1/nebula/employees/${encodeURIComponent(id)}/office?${query}`,
    { signal },
  );
  if (!response.ok) {
    const error = (await response.json().catch(() => ({}))) as {
      detail?: string;
    };
    throw new Error(error.detail || "工作台加载失败，请重试");
  }
  return response.json() as Promise<EmployeeOffice>;
}
