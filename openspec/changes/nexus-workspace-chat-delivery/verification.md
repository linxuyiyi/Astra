# 实现与验收记录

## 变更范围

- Agent Service 的 workspace_attachments.py 通过官方 WorkspaceService 定位 Session，使用目录描述符保护本地流式 I/O。群原件绑定到专用资源 Session，复制使用稳定附件 ID。chat_attachments.py 按需读取文本、生成限额图片块或提取 PDF 指定页文本。schedule_workspace.py 将普通聊天的物理目录按用户、Agent 和 workspace ID 隔离。
- Nexus 的 main.py 和 chat_attachments.py 管理附件引用、私聊绑定、流式代理、权限和过期未发送文件回收。collaboration.py 保留群公开日志和讨论租约，以接收成员的 seq 查询增量，并只复制原发言派发对象的附件。agentscope.py 的真实用户消息统一经 trigger_inputs 调用原生 /chat/。group_security.py 保留讨论执行标识和工具授权，在调用任务创建工具时填入实际来源。
- 两套 Web 入口都使用同一附件 API。删除旧 multipart 上传、Nexus 原件文件目录写入、发送前读盘与内容拼装，以及通过 Nexus 读取工具反向传输附件原件的路径。
- Alembic 0002_workspace_attachments 建立附件引用、转换群送达游标，已存在原件时保留旧表供离线迁移。已应用的 Skill/MCP 记录重新核验，新同步记录保存 MCP 挂载名用于检查 Session 就绪。
- AgentScope 保持 2.0.10。官方 2.0.10 和 2.0.11 都有 Workspace 读取与 Skill 上传，通用聊天附件上传由 Agent Service 提供。
  [官方 2.0.10 Workspace 路由](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_workspace.py)
  [官方 2.0.11 Workspace 路由](https://github.com/agentscope-ai/agentscope/blob/v2.0.11/src/agentscope/app/_router/_workspace.py)

## API 示例

上传使用文件原始字节作为请求体。附件 ID 由客户端生成并在网络重试时保持一致。私聊与群组作用域二选一，Nexus 根据登录用户推导内部 Session。

~~~http
PUT /api/v1/attachments/att_0123456789abcdef0123456789abcdef?conversation_id=conv_example&name=report.txt
Content-Type: text/plain

原件字节
~~~

上传成功返回 201 及 id、name、type、size。下载和图片预览使用 GET /api/v1/attachments/{id}。群组上传用 channel_id 查询参数。群公开消息沿用 POST /api/v1/channels/{id}/messages，包含 client_id。

~~~json
{
  "client_id": "draft-0123456789",
  "content": [{"type": "text", "text": "请查看日志"}],
  "attachment_ids": ["att_0123456789abcdef0123456789abcdef"]
}
~~~

以下为 Nexus HTTP 回归捕获的实际 /chat/ 请求结构。标识已替换，响应端由测试替身接收请求。

~~~json
{
  "agent_id": "agent-1",
  "session_id": "session-chat",
  "input": {
    "id": "msg_example",
    "name": "anon-a",
    "role": "user",
    "content": [
      {"type": "text", "text": "看日志"},
      {
        "type": "text",
        "id": "nexus-attachments-msg_example",
        "text": "消息 msg_example 的会话附件（按需调用 read_chat_attachment）：\nattachments/att_11111111111111111111111111111111 \"run.log\" (application/octet-stream)"
      }
    ],
    "metadata": {
      "message_id": "msg_example",
      "verified_uid": "anon-a",
      "display_text": "看日志",
      "nexus_deduplicate": true,
      "attachments": [{
        "id": "att_11111111111111111111111111111111",
        "name": "run.log",
        "type": "application/octet-stream",
        "size": 12
      }]
    }
  }
}
~~~

## 存量操作与证据

从已发布 0001_init 升级时，先备份数据库和附件目录并停止 Nexus 写入。升级 schema 后启动新版 Agent Service，保持 Nexus 停止，使用已配置的内部服务地址、密钥和 AgentScope 用户执行 dry-run。完成带原生历史扫描的 dry-run 后，添加 --apply 导入原件并验证上传和回读 SHA-256。完成审核后可用 --apply --archive-legacy /path/to/archive 核验并归档旧原件，然后移除旧表。未引用原件一并保留在归档中。

~~~bash
cd services/nexus-api
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m scripts.migrate_workspace_attachments --legacy-root /path/to/legacy/files
~~~

本机开发库的只读盘点命令为：

~~~bash
.venv/bin/python -m scripts.migrate_workspace_attachments \
  --database-url sqlite:///data/nexus.db --legacy-root data/files
~~~

盘点找到 7 条旧记录及 7 个原件。4 个附件在 Nexus 消息表有引用并可定位群组，文件均存在且大小匹配。另 3 条在 Nexus 消息表中未发现引用，AgentScope 原生私聊历史未连接扫描。本机库 revision 为 0006_employee_avatars，早于本分支的 0001_init 基线，实际导入和清理未运行。隔离测试覆盖同一旧 ID 被两个群复用、引用重写、上传和回读哈希校验、重复执行及归档。

## 验收边界

- AgentScope 真实 SQL 存储和 Workspace 测试覆盖 20 MiB 原件、超限、空文件、幂等重传、群副本、未获授权的 Session、符号链接及上传期间目录替换。原件与回读均按 SHA-256 比对。
- 本地工具会在宿主执行命令。受管私聊和群聊停用宿主 Bash/PowerShell，原生文件工具限定在工作区，附件通过 read_chat_attachment 读取。控制台代理关闭原生 Workspace 文件读取、下载令牌和目录浏览入口。
- 当前登录实现使用临时 UID，缺少公司 SSO 的可信身份绑定。权限结果只证明已确定 UID 的作用域检查。
- 图片工具已验证能生成受限 DataBlock。真实 VL 推理、非 VL 主模型调用独立视觉模型、生产 MySQL 与 Redis、现网 SSO、全部 HITL 场景仍需验收。独立视觉模型路径尚未实现。

## 已执行检查

| 检查 | 结果 |
| --- | --- |
| Nexus 的 .venv/bin/pytest tests/nexus tests/platform -q --tb=short --disable-warnings | 832 通过、21 跳过。之后补充拦截控制台原生文件入口，单独重跑 test_agentscope_proxy.py，7 通过 |
| Agent Service 的 .venv-agentscope/bin/python -m pytest tests -q --ignore=tests/test_schedule_resources_runtime.py --tb=short --disable-warnings | 21 通过、1 跳过 |
| Agent Service 的 .venv-agentscope/bin/python -m pytest tests/test_schedule_resources_runtime.py -q --tb=short --disable-warnings | 2 通过，使用本机环回 MCP |
| 聊天岛 npm test、npm run type-check、npm run build | 17 个测试通过，类型检查和构建通过 |
| 主 Web 的 npm run type-check、npm run build | 通过 |
| Ruff、git diff --check、OpenSpec strict validate | 通过 |

真实浏览器使用 Chromium 156.0.8078.4，后端为临时环回 Nexus 与 AgentScope 2.0.10，数据库为独立 SQLite，消息总线为 AgentScope InMemoryMessageBus。浏览器一次上传两个同名二进制文件并发布无 @ 群消息。重载后两个附件均可下载，群外 UID 请求返回 403。Nexus 数据目录没有原件目录，群资源 Workspace 的两个原件与下载字节都匹配以下 SHA-256。

- 4becd9dfe37a7f53670416c434be5e21281f38e41735d502083a1528d23e31ff
- aee4986cfeae8fca943f4190ed523f524049d01389778c176f767fe4d8c5db41

浏览器运行命令为 python3 /tmp/nexus-workspace-browser.deUxjK/run.py，验证后关闭了两个临时服务和 Chromium。测试的群组为 ch_567fde96e560。

本地 API 保持每个 UID 与员工一条默认私聊记录。Workspace 用例独立创建同一 Agent 的多个 Session 验证物理隔离。
