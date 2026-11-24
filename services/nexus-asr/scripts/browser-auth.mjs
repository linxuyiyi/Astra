// 给本地浏览器验收设置与 Nexus 页面相同的临时 UID。
export async function prepareChatIdentity(context, base, uid) {
  const url = new URL(base);
  if (!['127.0.0.1', 'localhost'].includes(url.hostname)) {
    throw new Error('语音验收脚本只允许连接本机 Nexus');
  }
  const value = encodeURIComponent(uid);
  await context.addCookies([{ name: 'nexus_uid', value, url: url.origin }]);
  await context.addInitScript(owner => localStorage.setItem('nexus_owner_id', owner), uid);
  return `nexus_uid=${value}`;
}

export function messageText(message) {
  return message.content?.filter(block => block.type === 'text').map(block => block.text).join('\n') ?? '';
}
