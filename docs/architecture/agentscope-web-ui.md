# AgentScope 官方 Web UI 接入

## 上游

- 仓库：`https://github.com/agentscope-ai/agentscope`
- 版本：`v2.0.10`
- commit：`e6c014f0be49b2a6f6f62a9453082a0b975484e1`
- 路径：`examples/web_ui/frontend`
- 已核对 GitHub `refs/tags/v2.0.10` 指向该 commit。

构建脚本直接从官方仓库 `v2.0.10` tag 克隆，校验 HEAD 等于上述 commit，在 `examples/web_ui` 执行 `pnpm install --frozen-lockfile` 后构建。仓库内不依赖任何本地快照或 `/tmp` 现存源码；Astra 提交的是构建产物、固定 commit 的构建脚本和元数据。

## 最小适配

只改官方前端的三个部署点，不改官方页面业务：

1. `src/App.tsx`：`createBrowserRouter(routes, { basename: '/agentscope-ui' })`。
2. `src/api/client.ts`：新增 `apiUrl(path, baseUrl?)`，`server_url` 支持 `/agentscope-api/` 这类同源路径前缀；central client、workspace 上传 XHR（`src/api/workspace.ts`）、knowledge 上传与下载 URL（`src/api/knowledgeBase.ts`）全部使用该 helper，避免只修 central client 而漏掉 XHR/文件 URL。
3. `index.html`：写入默认 `localStorage.server_url = '/agentscope-api/'`、`localStorage.username = 'nexus'`；已被操作员设置时不覆盖。

构建入口：

```bash
ops/deploy/build_agentscope_web_ui.sh
```

脚本不接收本地源码参数；它克隆固定 tag、校验 commit、按 frozen pnpm lock 安装并应用上述补丁，失败时直接退出且不覆盖现有产物。产物放在 `services/nexus-api/web/public/agentscope-ui/`，随 `npm run build` 进入 `dist/`。

构建产物约 15 MB，主要是官方站点的动态语法高亮语言包、Mermaid/图表渲染器和代码差异组件；它们在聊天与工具结果中按需加载，删减会改变官方 UI 行为。因此保留官方构建输出，不做多版本兼容或裁剪。

## 同源部署契约

- UI 静态/深链接：`/agentscope-ui/`，Vite middleware 与 nginx 都回退到 `/agentscope-ui/index.html`；`/agentscope-ui/assets/` 直接读缓存文件。
- API：`/agentscope-api/` 代理到 AgentScope 服务（默认 `http://127.0.0.1:18000/`），前缀剥离，保留浏览器 `X-User-ID`。
- 开发：`services/nexus-api/web/vite.config.ts` 的 `VITE_AGENTSCOPE_PROXY_TARGET`。
- 生产：`ops/dev/astra_service.py` 把 `NEXUS_AGENTSCOPE_BASE_URL` 传给 Vite preview；`ops/deploy/nginx-nexus.conf` 提供同样的代理。
- 用户上下文：`GET /api/v1/runtime` 返回 `agentscope_user_id`；`AgentscopeView` 在 iframe 加载前写入同源 `localStorage.username`。

## 原生能力

`services/agent-service/agent_service.py` 在 `create_app` 注入：

- `skill_hubs=[ClawSkillHub(...)]`
- `mcp_hubs=[GitHubMCPHub(...)]`

health 的 `skill_hubs` / `mcp_hubs` 为 `ok` 时，官方 UI 的 Skill Hub / MCP Hub 页面才展示真实资源。不可用时官方 UI 显示真实错误，不填假数据。

## 隔离验证

`.astra/qa-v141-v142/start_agentscope_ui.py` 在端口 `18001`、Redis DB `9`、临时 workspace/Qdrant/blob 启动隔离服务；不触碰原 `18000` 服务或原 Redis 数据。`Start`/`Stop` 使用独立进程组。

验证包含：health 的 hubs=ok、ClawHub/GitHub Registry 列表、测试 Skill 安装、测试 MCP 安装、测试 Agent 创建、官方 UI 在 Nexus 配置中心内列出这些资源、深链接刷新、1440/1920 截图。

## 升级

升级 AgentScope 时：

1. 核对新 tag 的 `examples/web_ui/frontend`。
2. 更新本文件的 commit。
3. 用 `ops/deploy/build_agentscope_web_ui.sh` 重建产物（脚本固定 commit，不接受本地 source 参数）。
4. 重新跑隔离服务与 `qa-agentscope-ui.mjs`。
