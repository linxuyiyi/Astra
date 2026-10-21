# Nexus v1.4.8 DeepSeek 改动复核

日期：2026-10-21。复核对象：`9671aa51e`、`9367e3002`、`968fa732e`；它们之前的 MySQL 初始化实现已合入。开始本轮复核时，原目录 `v1.4.8` 与远端都在 `968fa732e`，没有受 Git 管理的未提交改动；未纳入无关 untracked 目录。

结论：方向基本正确，部署链路尚未闭环。员工聊天本就已有网关路由主路径；删除无效的旧 credential/bootstrap 配置是合理收敛。远程 Worker 模式跳过控制机 OpenCode 检查也合理，但安装器没有完整传递新配置，不能据此认定公司全新部署可用。本轮只修正自有测试的 lint 标记并记录审查，没有修改 DeepSeek 的业务实现。

## 应修复的问题

### P1：安装器未持久化 worker 模式，服务仍可能要求本机 OpenCode

- `ops/deploy/install_nexus.sh:153` 未配置时按 `worker` 跳过本机 OpenCode 安装。
- `step_runenv:202` 没有写入 `ASTRA_EXECUTOR_TYPE`；systemd 只从该运行文件和 secrets 文件加载环境（`:281-282`）。
- `services/nexus-api/app/config/settings.py:115` 默认仍为 `opencode_server`。
- 因此在全新目标目录、通过 `--env-file` 配置、采用 systemd 部署时，安装器按 worker 执行，服务却按本机执行模式启动；依然会校验 UI bundle、检查或启动控制机 OpenCode。无 systemd 部署也依赖安装进程临时继承的环境，重启不可靠。

修复：把选定执行模式写入实际服务加载的环境，针对生成环境文件后的干净进程测试；远程 Worker 自身的 OpenCode 版本仍应校验。另需核实新目录 `ops/env/.env` 的生成路径，因为 `astra_service.validate_start` 仍要求它存在。

### P1：网关 key/base 没有进入服务环境，员工 Session 创建失败

- 环境模板新增 `NEXUS_GATEWAY_API_KEY` 和 `NEXUS_GATEWAY_OPENAI_BASE`，但安装器的 `step_secrets`、`step_runenv` 均未写入。
- `nexus_api/model_route.py:32-35,91-99` 从进程环境取得 key；缺少时明确报“网关访问密钥未配置”。
- `step_gateway_route:356-372` 只建池、节点和路由，没有签发可用的客户端 key。

修复：明确提供或签发网关调用 key、验证其权限/模型范围、持久化 key 与 base，并保证实际启动服务可读。新安装验收应包含创建员工和 Session，不能只看服务 health。安装器仍强制 `DEEPSEEK_API_KEY` 并探测供应商 `/models`（`:98-101`），与“平台部署屏蔽具体模型供应商”的目标也不一致；供应商凭据应属于网关上游配置。

### P1：接入已有 llm_wiki 时仍生成随机 token/project_id

- `step_llm_wiki:375-386` 现在只连接已有服务器、检查 health、触发 resync。
- `step_secrets:195-198` 在未填配置时却继续生成随机 token 和项目 ID；原来的远端登记/项目创建过程已经删除。
- `app/llm_wiki.py:82-83,115-121` 用这些值鉴权并定位项目。随机值在既有服务器上无效，通常导致 401/404；health 成功不说明项目可用。

修复：接入已有服务时要求明确的真实 token/project_id，并进行一次经过鉴权的项目验证；不要自动生成无法注册的凭据。核实远端模型端点使用 Nexus 可达的网关地址，不能只留注释。

### P1：Worker 网关地址模板缺少 /v1，部署探测路径错误

此项基线已有，此次“统一模型网关”改动仍未解决。

- `ops/env/.env.example:140-141` 指导地址为 `http://<内网>:18110`。
- `remote_provisioning.py:203,544-548` 原样写进 OpenAI compatible provider；`:585` 再拼 `/models`。
- `gateway/server.py:110-111,1387-1393` 的入口是 `/v1/models` 与 `/v1/chat/completions`。

照模板只替换内网主机后，Worker 请求 `/models`，收到 404。应统一规范化 `/v1`，测试配置生成、真实模型列表和聊天转发的完整路径，不仅检查 host 非回环。

### 需求未完整覆盖：embedding 仍可直连供应商

新增 `NEXUS_KB_EMBEDDING_JSON` 直接引用 AgentScope credential，`app/library_kb.py` 将它传给官方知识库。官方 2.0.10 据此构造 embedding 客户端；当前网关没有 `/v1/embeddings`。

如果“全部模型 API 走自有代理”包含 embedding，目前未满足。需要补代理能力并配置对应路由，或明确暂不启用该调用。不能把生成式聊天已走网关说成所有模型调用都已收敛。外部 llm_wiki 的模型连接也仅有文档要求，本次代码没有自动配置或验证它。

## 核验与证据

安装器函数在临时目录使用虚假配置执行，未访问外部服务、未读写真实凭据。即使输入 worker 与网关配置，生成结果仍为：

| 配置 | 写入运行文件或 secrets |
| --- | --- |
| ASTRA_EXECUTOR_TYPE | 否 |
| NEXUS_GATEWAY_API_KEY | 否 |
| NEXUS_GATEWAY_OPENAI_BASE | 否 |
| llm_wiki token/project_id 留空 | 自动生成，与既有服务器没有关联 |

最新代码的独立回归结果：

| 检查 | 结果与边界 |
| --- | --- |
| Nexus platform + nexus 回归 | 1203 passed / 18 skipped / 3 deselected；另整模块排除 Linux `test_worker_agent_files.py`。命令见下方 |
| 真实 MySQL 8.0.36 双服务进程冒烟 | 1 passed；员工长文本、Agent/Session、私聊去重、团队、任务取消、双服务重启、16 条 SSE、IM 并发认领、大小写判重与无本地业务 .db |
| Agent Service 全部测试 | 13 passed，包含真实 MySQL 的同秒消息顺序、游标分页和存储重新打开 |
| Nexus Ruff、shell 语法与 OpenSpec strict validate | 通过 |

```bash
cd services/nexus-api
pytest tests/platform tests/nexus \
  --ignore=tests/platform/test_worker_agent_files.py \
  -k 'not test_install_marks_inside_the_operation_and_serves_files and not test_install_script_rejects_python_below_3_11 and not test_install_script_installs_venv_package_for_the_selected_python' \
  --maxfail=3 -q --disable-warnings
```

排除的是 macOS 无 `os.O_PATH` / `remote_install.sh` 要求 Linux 的用例；不能把这些用例计为通过。真实 MySQL 冒烟手动注入了网关测试 key 并直接启动进程，因此不会覆盖上面安装器遗漏环境变量的问题。未执行真实模型推理、远程 Worker 安装、公司 MySQL 权限/TLS验收、外部 llm_wiki 项目写入与检索。

本机 MySQL 8.4 已卸载，常驻服务为官方 8.0.36（127.0.0.1:3306）；临时实例单独清理。Nexus 业务初始化为一个 `0001_init`（53 张业务表加 Alembic 版本表），AgentScope 继续使用官方独立 schema 和部署期的消息时间精度适配。旧库不升级、不导入。
