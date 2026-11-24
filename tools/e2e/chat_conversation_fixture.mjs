// 固定的 Nexus HTTP/SSE 响应，用同一组业务事实比较两个 /chat 生产构建。
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

export const owner = 'reviewer';
export const prompt = '请整理本轮验证结果。';
export const conclusion = '本轮记录已汇总。';
export const groupName = '发布评审组';
const employees = [
  { id: 'qa', name: '林舟', domain: '质量工程师', avatar: 'robot-v1-0' },
  { id: 'pm', name: '周宁', domain: '发布负责人', avatar: 'robot-v1-1' },
].map((value) => ({
  ...value,
  description: '跟踪发布验证与回归结果',
  enabled: true,
  knowledge: [],
  skills: [],
  mcps: [],
  tools: [],
  agentscope_agent_id: 'agent-' + value.id,
}));
const members = employees.map((e) => ({
  member_type: 'employee',
  member_id: e.id,
  employee_id: e.id,
  name: e.name,
  role: 'member',
  online: true,
  avatar: e.avatar,
  enabled: e.enabled,
}));
const channel = {
  id: 'release',
  name: groupName,
  description: '协调本轮发布验证',
  viewer_id: owner,
  my_role: 'owner',
  members: [...members, { member_type: 'human', member_id: owner, name: '评审人', role: 'owner' }],
  discussion: null,
};
const stamp = (i) => new Date(Date.UTC(2026, 10, 18, 2, 0, i)).toISOString();
function message(id, role, text, created_at, name) {
  return {
    id,
    role,
    name: name || (role === 'assistant' ? employees[0].name : owner),
    content: [{ id: id + '-text', type: 'text', text }],
    metadata: {},
    created_at,
    finished_at: created_at,
  };
}
const lastHistory = '当前验收记录';
const groupLast = '发布窗口已确认。';
export { lastHistory, groupLast };
function seedHistory() {
  const rows = Array.from({ length: 320 }, (_, index) =>
    message(
      'm-' + index,
      index % 2 ? 'assistant' : 'user',
      index % 2 ? '已核对第 ' + index + ' 批用例，结果已登记。' : '请核对第 ' + index + ' 批用例。',
      stamp(index),
    ),
  );
  const last = rows.at(-1);
  last.content[0].text =
    '### ' +
    lastHistory +
    '\n\n完成两项检查，保留一项待办。\n\n| 范围 | 结果 |\n| --- | --- |\n| 接口回归 | 通过 |\n| 浏览器验收 | 待复核 |';
  last.content.push(
    {
      type: 'tool_call',
      id: 'search',
      name: 'mcp__library__library_search',
      state: 'finished',
      input: '{"query":"验收记录"}',
    },
    {
      type: 'tool_result',
      id: 'search',
      name: 'mcp__library__library_search',
      state: 'success',
      output: [
        { type: 'text', text: '{"resource_id":"release-1","title":"发布验收记录","path":"release/acceptance.md"}' },
        { type: 'text', text: '{"resource_id":"release-2","title":"浏览器复核记录","path":"release/browser.md"}' },
      ],
    },
  );
  return rows;
}
function groupRow(i, name, text, role = 'assistant') {
  const msg = message('g-' + i, role, text, stamp(i), name);
  return {
    seq: i,
    id: msg.id,
    sender_type: role === 'assistant' ? 'employee' : role === 'system' ? 'system' : 'human',
    sender_name: name,
    sender_id: members.find((m) => m.name === name)?.member_id || owner,
    text,
    message: msg,
    discussion_id: '',
    created_at: msg.created_at,
  };
}
function seedGroup() {
  const rows = Array.from({ length: 95 }, (_, i) =>
    groupRow(i + 1, employees[i % 2].name, '群组核对记录 ' + (i + 1)),
  );
  rows[93] = groupRow(94, employees[0].name, '浏览器复核已安排。');
  rows[94] = groupRow(95, employees[1].name, groupLast);
  return rows;
}
const mime = {
  '.js': 'application/javascript',
  '.css': 'text/css',
  '.html': 'text/html',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.woff2': 'font/woff2',
};
export async function fixture(dist, { regression = false, peerFailure = regression } = {}) {
  const data = new Map(
    employees.map((emp) => [
      emp.id,
      {
        history:
          emp.id === 'qa'
            ? seedHistory()
            : [message('other', 'assistant', '仅发布负责人可见的记录', stamp(0), emp.name)],
        running: false,
        events: [],
        listeners: new Set(),
      },
    ]),
  );
  const groups = seedGroup();
  let discussion = null,
    revoked = false;
  const requestLog = [];
  let faultInjected = false;
  if (regression) {
    const history = data.get('qa').history;
    history.at(-2).content[0].text = '请整理今晚发布的回归结果，列出仍需要跟进的事项。';
    const source = history.at(-1);
    source.content[0].text = `### ${lastHistory}\n\n接口回归已通过。发布前还需要在桌面浏览器复核登录和文件上传。\n\n| 检查项 | 结果 | 负责人 |\n| --- | --- | --- |\n| 核心接口回归 | 通过 | 林舟 |\n| 浏览器复核 | 待复核 | 林舟 |\n| 发布窗口 | 待确认 | 周宁 |\n\n**下一步**\n\n复核结束后，把结果补进验收记录，并请周宁确认发布窗口。`;
    source.content.push(
      {
        type: 'tool_call',
        id: 'read',
        name: 'mcp__library__library_get',
        state: 'finished',
        input: '{"path":"release/acceptance.md"}',
      },
      { type: 'tool_result', id: 'read', name: 'mcp__library__library_get', state: 'success',
        output: '{"resource_id":"release-1","path":"release/acceptance.md","content_md":"## 验收清单"}' },
      {
        type: 'tool_call',
        id: 'peer',
        name: 'contact_employee',
        state: 'finished',
        input: JSON.stringify({ employee_name: '周宁', message: '请核实窗口' }),
      },
      {
        type: 'tool_result', id: 'peer', name: 'contact_employee', state: 'success',
        output: peerFailure ? '{"ok":false,"peer":"周宁","error":"该员工已禁用"}' : '已投递',
      },
    );
    const parked = data.get('pm');
    parked.running = true;
    parked.history[0].finished_at = null;
    parked.history[0].content.push({
      type: 'tool_call',
      id: 'confirm',
      name: 'review_report',
      state: 'asking',
      input: '{}',
    });
    const task = groupRow(93, '任务通知', '回归报告已生成', 'system');
    task.message.role = 'user';
    task.message.metadata = { task_id: 'T-RELEASE', task_status: 'SUCCEEDED', task_event_id: 'ev-01' };
    groups[92] = task;
    groups[93] = groupRow(94, employees[0].name, '浏览器复核安排在 18:30，重点看登录和附件上传。');
    groups[94] = groupRow(95, employees[1].name, '发布窗口已确认。请把浏览器复核结果补进回归报告，完成后我会安排发布。');
  }
  const attachments = new Map();
  const groupStreams = new Set();
  const channelEvents = [];
  const storeEvent = (events, listeners, id, event) => {
    const frame = 'id: ' + id + '\ndata: ' + JSON.stringify(event) + '\n\n';
    events.push({ id, frame });
    for (const stream of listeners) stream.write(frame);
  };
  let closed = false,
    transcriptions = 0;
  let server;
  async function handle(req, res) {
    const url = new URL(req.url, 'http://localhost');
    const p = url.pathname;
    if (p.startsWith('/api/'))
      requestLog.push({ method: req.method, path: p, query: url.searchParams.toString() });
    function json(result, status = 200) {
      res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
      res.end(JSON.stringify(result));
    }
    async function body() {
      const buffers = [];
      for await (const chunk of req) buffers.push(chunk);
      return Buffer.concat(buffers);
    }
    const bodyJson = async () => JSON.parse((await body()).toString());
    function stream(events, listeners) {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
      res.write(': open\n\n');
      const after = url.searchParams.get('after');
      const start = events.findIndex((event) => event.id === after);
      for (const event of events.slice(start + 1)) res.write(event.frame);
      listeners.add(res);
      res.on('close', () => listeners.delete(res));
    }
    if (!p.startsWith('/api/')) {
      const file =
        p === '/chat' || p === '/' ? 'index.html' : p.endsWith('/') ? p.slice(1) + 'index.html' : p.slice(1);
      if (!path.resolve(dist, file).startsWith(path.resolve(dist) + path.sep)) return json({}, 403);
      try {
        const content = await fs.readFile(path.join(dist, file));
        res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' });
        res.end(content);
      } catch {
        json({ detail: 'asset missing' }, 404);
      }
      return;
    }
    await delay(15);
    if (p === '/api/asr/transcribe' && regression) {
      const audio = await body();
      if (
        !req.headers['content-type']?.startsWith('multipart/form-data') ||
        audio.length < 500 ||
        !audio.includes('filename="recording.')
      ) {
        return json({ detail: '没有收到录音文件' }, 422);
      }
      transcriptions++;
      await delay(650);
      return json({ text: transcriptions === 1 ? '本轮语音草稿' : '迟到的转写' });
    }
    if (p === '/api/v1/runtime')
      return json({ agentscope_version: '2.0.10', configured: false, reachable: false });
    if (p === '/api/v1/employees') return json({ items: employees });
    if (p === '/api/v1/channels') return json({ items: revoked ? [] : [{ ...channel, discussion }] });
    if (['/api/tasks', '/api/workers'].includes(p)) return json([]);
    if (/^\/api\/v1\/(skills|mcps|knowledge|tools)$/.test(p)) return json({ available: true, items: [] });
    const upload = p.match(/^\/api\/v1\/attachments\/([^/]+)$/);
    if (upload) {
      if (req.method === 'PUT') {
        const bytes = await body(),
          name = url.searchParams.get('name');
        if (name === 'rejected.txt') return json({ detail: '测试附件被拒绝' }, 422);
        const meta = {
          id: upload[1],
          name,
          size: bytes.length,
          type: req.headers['content-type'] || 'text/plain',
        };
        attachments.set(meta.id, { meta, bytes });
        return json(meta);
      }
      const attached = attachments.get(upload[1]);
      if (!attached) return json({}, 404);
      res.writeHead(200, { 'Content-Type': attached.meta.type });
      return res.end(attached.bytes);
    }
    const binding = (id) => ({
      id: 'c-' + id,
      name: '新对话',
      agentscope_session_id: 's-' + id,
    });
    const employee = p.match(/^\/api\/v1\/employees\/(qa|pm)\/conversations$/);
    if (employee && req.method === 'POST') return json(binding(employee[1]));
    const dm = p.match(/^\/api\/v1\/conversations\/c-(qa|pm)\/(messages|stream|interrupt)$/);
    if (dm) {
      const [, who, action] = dm,
        s = data.get(who),
        e = employees.find((item) => item.id === who);
      if (action === 'stream') return stream(s.events, s.listeners);
      if (action === 'interrupt') {
        s.running = false;
        return json({});
      }
      if (req.method === 'GET') {
        const before = url.searchParams.get('before'),
          limit = Number(url.searchParams.get('limit')) || s.history.length;
        const end = before ? s.history.findIndex((row) => row.id === before) : s.history.length;
        const start = Math.max(0, end - limit),
          messages = s.history.slice(start, end);
        return json({
          messages,
          before: start ? messages[0].id : null,
          event_cursor: s.events.at(-1)?.id || '1-0',
          is_running: s.running,
          session_id: 's-' + who,
          viewer_id: owner,
          employee: e,
        });
      }
      const payload = await bodyJson(),
        userText = payload.content.map((b) => b.text || '').join('');
      const entry = requestLog.findLast((row) => row.path === p && row.method === 'POST');
      entry.clientId = payload.client_id;
      entry.text = userText;
      entry.attachmentIds = payload.attachment_ids;
      if (regression && userText === '重试这条投递' && !faultInjected) {
        faultInjected = true;
        return json({ detail: '模拟网关暂不可用' }, 503);
      }
      const user = message(payload.client_id, 'user', userText, stamp(1000 + s.history.length));
      user.metadata.attachments = payload.attachment_ids
        .map((id) => attachments.get(id)?.meta)
        .filter(Boolean);
      for (const file of user.metadata.attachments)
        user.content.push({
          id: file.id,
          name: file.name,
          type: 'data',
          created_at: user.created_at,
          source: { type: 'url', url: '/api/v1/attachments/' + file.id, media_type: file.type },
        });
      s.history.push(user);
      s.running = true;
      json({
        message: {
          id: user.id,
          text: userText,
          sender: 'user',
          speaker: owner,
          sender_type: 'human',
          sender_name: owner,
          created_at: user.created_at,
          attachments: user.metadata.attachments,
        },
        agentscope: { accepted: true, session_id: 's-' + who },
      });
      await delay(700);
      if (closed) return;
      const reply = message('reply-' + user.id, 'assistant', '', stamp(1000 + s.history.length));
      reply.finished_at = null;
      s.history.push(reply);
      const emit = (type, extra = {}) => {
        const id = '2-' + (s.events.length + 1);
        storeEvent(s.events, s.listeners, id, {
          id,
          type,
          reply_id: reply.id,
          session_id: 's-' + who,
          created_at: reply.created_at,
          ...extra,
        });
      };
      const block = reply.content[0];
      emit('REPLY_START', { role: 'assistant', name: e.name });
      emit('TEXT_BLOCK_START', { block_id: block.id });
      for (let i = 0; i < 160 && s.running && !closed; i++) {
        const delta = i === 0 ? '流式验收：' : i === 159 ? conclusion : i % 16 === 0 ? '\n\n' : '记录 ';
        block.text += delta;
        emit('TEXT_BLOCK_DELTA', { block_id: block.id, delta });
        if (regression && userText === '检查连接恢复') {
          if (i === 16) for (const listener of s.listeners) listener.end();
          if (i === 130) {
            for (const listener of s.listeners) {
              listener.write(s.events.at(-1).frame);
              listener.write(s.events.at(-4).frame);
            }
          }
        }
        await delay(4);
      }
      if (closed) return;
      emit('TEXT_BLOCK_END', { block_id: block.id });
      reply.finished_reason = s.running ? 'completed' : 'interrupted';
      reply.finished_at = stamp(1001 + s.history.length);
      s.running = false;
      emit('REPLY_END', { finished_reason: reply.finished_reason });
      return;
    }
    if (p.startsWith('/api/v1/channels/release/')) {
      if (revoked) return json({ detail: '已移出群组' }, 403);
      if (p.endsWith('/events')) return stream(channelEvents, groupStreams);
      if (p.endsWith('/read') || p.endsWith('/interrupt')) return json({});
      if (req.method === 'POST') {
        const payload = await bodyJson(),
          row = groupRow(groups.length + 1, owner, payload.content.map((b) => b.text).join(''), 'user');
        row.id = row.message.id = payload.client_id;
        groups.push(row);
        const emit = (event) =>
          storeEvent(channelEvents, groupStreams, String(channelEvents.length + 1), {
            ...event,
            event_cursor: channelEvents.length + 1,
          });
        const did = 'discussion-' + row.id;
        discussion = { id: did, state: 'queued' };
        json({
          message: row,
          discussion_id: did,
          participants: employees.map((e) => e.id),
          duplicate: false,
        });
        emit({ type: 'message', item: row });
        emit({ type: 'discussion', ...discussion });
        discussion = { id: did, state: 'running', active_employee_id: '' };
        emit({ type: 'discussion', ...discussion });
        for (const e of employees) {
          discussion = { ...discussion, active_employee_id: e.id };
          emit({ type: 'discussion-worker', id: did, active_employee_id: e.id });
          await delay(regression ? 300 : 120);
          const response = groupRow(groups.length + 1, e.name, e.name + '收到群组请求。');
          response.discussion_id = did;
          groups.push(response);
          emit({ type: 'message', item: response });
        }
        discussion = { id: did, state: 'completed' };
        emit({ type: 'discussion', ...discussion });
        return;
      }
      const before = Number(url.searchParams.get('before')),
        limit = Number(url.searchParams.get('limit')) || 50;
      const history = groups.filter((x) => !before || x.seq < before),
        rows = history.slice(-limit);
      return json({
        items: rows,
        channel: { ...channel, discussion },
        before: history.length > limit ? rows[0].seq : 0,
        event_cursor: channelEvents.length,
      });
    }
    json({ detail: 'unknown fixture endpoint ' + p }, 404);
  }
  server = http.createServer((req, res) => {
    handle(req, res).catch((err) => {
      console.error(err);
      res.destroy();
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return {
    requestLog,
    revokeGroup() {
      revoked = true;
      storeEvent(channelEvents, groupStreams, String(channelEvents.length + 1), { type: 'revoked' });
    },
    url: 'http://127.0.0.1:' + server.address().port,
    async close() {
      closed = true;
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}
