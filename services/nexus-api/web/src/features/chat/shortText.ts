/**
 * 把后端/网络错误压成短人话，供气泡旁的失败条与重试按钮使用。
 * 不暴露堆栈、上游原文或 agentscope 细节。
 */
const RULES: Array<{ test: RegExp; text: string }> = [
  { test: /abort|abortError|中断|interrupted/i, text: "回复已中断" },
  { test: /network|fetch failed|Failed to fetch|ECONN|超时|timeout/i, text: "网络异常，请重试" },
  { test: /401|未登录|UID|鉴权|unauthorized/i, text: "登录已失效，请刷新后重试" },
  { test: /403|forbidden|没有权限|拒绝/i, text: "没有权限执行此操作" },
  { test: /404|不存在|not found/i, text: "会话不存在或已被删除" },
  { test: /413|过大|too large|20 ?MB/i, text: "附件太大，请压缩后重试" },
  { test: /429|限流|rate.?limit|繁忙/i, text: "请求太频繁，请稍后再试" },
  { test: /5\d\d|upstream|上游|AgentScope|服务暂不可用/i, text: "服务暂时不可用，请稍后重试" },
  { test: /未被.*接受|没有生成回复/i, text: "员工没有接受这条消息" },
];

export function shortText(error: unknown, fallback = "出了点问题，请重试"): string {
  const raw = error instanceof Error ? error.message : String(error ?? "");
  const trimmed = raw.trim();
  if (!trimmed) return fallback;
  for (const rule of RULES) {
    if (rule.test.test(trimmed)) return rule.text;
  }
  // 已经很短的中文说明直接用；长英文/堆栈压成通用短句。
  if (trimmed.length <= 36 && /[\u4e00-\u9fff]/.test(trimmed)) return trimmed;
  return fallback;
}
