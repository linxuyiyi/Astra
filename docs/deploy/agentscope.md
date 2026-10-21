# 部署 AgentScope 2.0.10 与 Redis

Nexus 不内嵌 AgentScope。本目录的启动脚本调用已安装的 `agentscope==2.0.10`。

## 版本

见 `agentscope.version`。PyPI 与 GitHub tag `v2.0.10` 都是这个版本，Python `>=3.11`。接口记录在 `../architecture/agentscope-2.0.10.md`。

## Redis

有 Docker 时：

```bash
docker compose -f services/agent-service/docker-compose.yml up -d
```

没有 Docker 时，`services/agent-service/redis.sh` 会在本机 `redis-server` 上监听 `127.0.0.1:6379`。两者都没有时，脚本退出并说明原因。

Agent Service 的 `RedisMessageBus` 连接这里；官方 SQL 存储使用独立 MySQL 数据库，不把 Agent/Session/消息保存到 Redis。

## Agent Service

另建一个虚拟环境，安装官方包和它声明的 extras（`service`、`storage-sql`、`model-ollama`、`vdb-qdrant`）：

```bash
python3 -m venv services/agent-service/.venv-agentscope
services/agent-service/.venv-agentscope/bin/pip install -r services/agent-service/requirements.txt
```

`agentscope.version` 的内容仍是 `agentscope==2.0.10`。`requirements.txt` 使用同一版本，并带上官方 extras。然后：

```bash
set -a
source ops/env/.env
set +a
export NEXUS_API_BASE=http://127.0.0.1:18120
bash services/agent-service/redis.sh
services/agent-service/.venv-agentscope/bin/python services/agent-service/agentscope_sql.py init
services/agent-service/.venv-agentscope/bin/python services/agent-service/agent_service.py
```

脚本会：

- 用官方 `AsyncSQLAlchemyStorage` 与 `RedisMessageBus` 调用 `agentscope.app.create_app`。
- 把 `LocalWorkspaceManager` 的隔离设为 `IsolationPolicy.PER_SESSION`。这只给每个 Session 一个独立工作目录，不是沙箱。Bash 仍可能读到该目录以外的文件。模型 API 密钥放在 Agent Service 的 credential 里，调用模型时由这个进程持有，不要在仓库或页面里再放一份。
- 私聊、邮箱和团队讨论 Session 在创建或发消息前都设为 `dont_ask`：没有人能回答的权限询问会被拒绝，运行不会停在 asking。绑定 MCP 的会话改用 `bypass`，否则 MCP Tool 的 `PASSTHROUGH` 权限会被转成 `DENY`。
- 注册 `contact_employee`，并允许它执行。工具把对方姓名和原文交给 Nexus 解析，再向目标 Session 调用 `deliver_to_inbox`，不生成对方的回复。解析返回 4xx 时，工具把说明交回模型并停止投递。
- 不注册 Agent Team。官方 `get_toolkit` 仍会把任务规划、`ToolStop`、定时任务和 Agent Team 放进工具组。`agent_service.py` 在 `create_app` 之前替换聊天和实时服务使用的 `get_toolkit`：先调用官方组装，再 `Toolkit.remove_tool` 卸掉 `TaskCreate`、`TaskGet`、`TaskList`、`TaskUpdate`、`ToolStop`、`TeamCreate`、`AgentCreate`、`TeamSay`、`TeamDelete`、`AgentInvite`、`ScheduleCreate`、`ScheduleDelete`、`ScheduleList`、`ScheduleView`。空的非 basic 工具组也会去掉，避免模型再去激活它们。`contact_employee` 和工作区的 Bash、Read、Write 等仍然保留。没有改 AgentScope 安装包。
- 传入 `knowledge_base_manager`：`CollectionPerKbManager` 使用独立 SQL 库中的知识库元数据和本地 `QdrantStore`。向量目录由 `AGENTSCOPE_QDRANT_PATH` 指定（默认 `services/agent-service/data/qdrant`），文件由 `AGENTSCOPE_BLOB_DIR` 指定。`GET /knowledge_bases/` 等路由可用。图书馆发布使用 `NEXUS_KB_EMBEDDING_JSON={"model_route_id":"实际嵌入路由ID","dimensions":1024}`，自动登记指向 Nexus 网关的 credential；模型实际经 `/v1/embeddings` 调用，不使用独立厂商 credential。`requirements-embedding.txt` 和 `embedding_smoke.py` 只用于本机嵌入冒烟，不装进 Agent Service 虚拟环境。技能库为空时也不写入示例技能。

服务默认监听 `0.0.0.0:18000`；对外部署建议改成 `127.0.0.1` 或加反向代理。健康检查：

```bash
bash services/agent-service/healthcheck.sh
```

它要求 Redis 返回 `PONG`，并且 `GET /health` 带 `X-User-ID` 成功。`X-User-ID` 只是 Agent Service 的占位身份，对外发布前要换成真实认证。

## 聊天模型

Nexus 页面没有模型或密钥字段。模型统一经 Nexus API 网关调用，不再由部署脚本向 Agent Service 预登记厂商 credential：

- 员工在配置中心选择已启用的网关路由（`model_route_id`）。
- Nexus 用 `NEXUS_GATEWAY_OPENAI_BASE` 和 `NEXUS_GATEWAY_API_KEY` 在 Agent Service 登记/复用指向网关的 `openai_credential`（见 `nexus_api/model_route.py`）。
- 创建 Session 时带上该路由的模型名；没有路由会直接报错，不会回退到部署环境里的厂商模型。

因此 `NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG` 和 `bootstrap_chat_model.py` 已移除。需要新增模型时，在网关新增节点与路由，不需要改 Agent Service 的 credential。

## 本地嵌入冒烟

未配置嵌入路由时图书馆映射保持关闭。独立实验若要确认 `all-MiniLM-L6-v2` 能在本机编码：

```bash
python3 -m venv services/agent-service/.venv-embedding
services/agent-service/.venv-embedding/bin/pip install -r services/agent-service/requirements-embedding.txt
services/agent-service/.venv-embedding/bin/python services/agent-service/embedding_smoke.py
```

脚本打印维度，以及两句相近英文和一句不相近英文的余弦。`all-MiniLM-L6-v2` 是英文模型。相近的一对必须更高。这个结果不写入 Agent Service。

## 任务 6.1

服务都起来之后：

```bash
NEXUS_LIVE_AGENTSCOPE=1 PYTHONPATH=services/nexus-api \
  services/nexus-api/.venv/bin/pytest services/nexus-api/tests/nexus/test_live_agentscope.py -s
```

未设置 `NEXUS_LIVE_AGENTSCOPE=1` 时，这条测试会跳过，避免在没有 Agent Service 的环境里失败。
