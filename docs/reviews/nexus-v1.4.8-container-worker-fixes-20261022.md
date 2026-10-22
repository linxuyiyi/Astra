# 远程 Worker 容器部署逐项核对与修复

日期：2026-10-22。基线：`a3f429e7c`，工作分支：`codex/worker-container-deploy`。公司粘贴的问题单作为复现线索，未直接套用其中代码。

## 问题原因与处理

| 编号 | 核对结论 | 本次处理 |
| --- | --- | --- |
| 1 无 systemd | 原实现部署、SSH 离线重启都硬依赖 systemctl | 按 PID 1 和 systemd 运行目录选择管理模式；容器使用受管进程记录 |
| 2 SSH channel 阻塞 | 原基线没有 nohup 分支，不能宣称复现公司新增函数的问题；新增容器启动必须避免同类问题 | 独立会话启动，stdin 为 DEVNULL，stdout/stderr 为私有日志，close_fds；以捕获标准流的子进程模拟 SSH EOF 验证 |
| 3 pgrep 误杀 | 原基线没有公司方案中的 pkill，未直接复现 | 新实现不使用 pkill；停止前核对 PID、starttime、boot ID、cwd、argv 和进程组，身份不符拒绝停止 |
| 4 WebSocket scheme/TLS | 原实现原样传 URL，HTTP(S) 确实不适配 WS(S) | HTTP→WS、HTTPS→WSS；拒绝无效地址，保留 TLS 验证，通过显式 CA 信任内部证书 |
| 5 Nginx Upgrade | 仓库模板缺少 Worker WebSocket Upgrade | 给控制连接增加精确 location，设置 HTTP/1.1、Upgrade、Connection 和超时，不改变普通 API/SSE |
| 6 channel_switch systemctl | 原实现仅凭 unit 非空决定 systemctl | 检查 PID 1 与 systemd 目录；容器走受管进程路径；不采用 isfile 检查目录 |
| 7 listener 停止后漏启动 | 代码确认：启动在 else 内，已有 listener 时只停止 | 统一停止后启动；受管模式还等待子进程释放端口，启动前复检 gate |
| 8 /config 隐藏 provider | 本地真实 OpenCode 1.18.35 返回 provider/MCP，未复现“安全策略省略” | 不添加磁盘 fallback；保持运行配置核验，缺字段持续存在时失败 |
| 9 健康先就绪 | 原实现仅请求一次运行配置 | reload 后有界重试，覆盖暂时不可读和缺字段；不把磁盘内容当运行结果 |
| 10 HTTP 数据通道 scheme/TLS | HTTPS 原来会被降为 HTTP | HTTPS/WSS→HTTPS，HTTP/WS→HTTP；httpx 保持默认 TLS 校验 |
| 11 配置环境丢失 | 白名单缺少两个配置变量；新容器路径也必须传配置 | 保留 OPENCODE_CONFIG/DIR；OpenCode 独立 0600 env，Worker token 不进入子进程；保留 Git helper 和 CA |
| 12 内置模型 | 原实现已设置 enabled_providers、model、small_model 指向网关 | 补齐禁用 opencode，保留其他禁用项并清除旧配置对 nexus-gateway 的禁用冲突 |
| 13 MySQL 健康不持久化 | 8.0.36 实测 JSON 对象与 SQL 字符串直接比较为 0，并非公司描述的 NULL；JSON 值比较为 1 | 两处共享条件显式 CAST 绑定值为 JSON；保留完整快照 CAS、维护隔离和 Worker epoch 围栏，不直接删条件 |
| 14 Python 尚未安装 | 当前基线没有 python_source_tgz 设置或源码构建；问题单与基线不同 | 补齐设置，早期用系统 Python≥3.8 解析，显式源码包在私有目录构建，校验实际 Python≥3.11/SSL/SQLite/venv 并将路径交给 Agent venv |
| 15 settings 缓存 | 属于进程启动配置约定，不是应删除缓存的缺陷 | 文档明确修改 .env 后重启 API/实际读取配置的常驻进程，再提交维护；不增加隐式热加载 |

分类工具：新增 `nexus_task_categories`，复用 TaskCategory，返回已启用分类的 id/name/description/task_prompt/question_prompt，按 sort_order/id 排序，沿用 MCP 认证；同步员工任务管理 Skill。没有硬编码公司示例中的三种分类。

任务与代码仓关系保留：未修改 Task/Repository 模型、绑定字段、绑定 API 或迁移。既有 deploy 不隐式更新仓库、update 只允许安全 fast-forward 的测试继续执行。

后续补充的遗漏项已处理：移除分配/启动要求任务与 Worker 仓库 ID 一致的限制，改为任务类型与数字员工关系；分类不限制接单，任务未指定数字员工时按支持类型领取；任务指定员工时只匹配绑定该员工的 Worker。任务仓库字段仍保留。详见 [任务分配仓库限制修复](nexus-v1.4.8-task-worker-routing-20261022.md)；下方 1280 项为前一轮部署修复验证，最新路由修复验证见该专项报告。

## 验证

证据仅属于本地隔离环境，不是公司部署验收。

- macOS 最终针对性回归：**279 passed / 5 skipped**（含独立 MySQL 条件写入与真实 WSS CA 测试）。Ruff 对 app/nexus_api/tests 全量通过，bash -n、git diff --check、OpenSpec strict validate 通过。macOS 全套运行到 Linux openat2 系统调用时被 SIGSYS 终止，不计为全套通过；整套结果以下方 Linux 为准。
- aarch64 Linux（Ubuntu 24.04 / Python 3.12）完整服务回归：**1280 passed / 8 skipped**，155.51 秒；命令为 `ASTRA_REDIS_URL=<专用测试 Redis> ASTRA_TEST_REDIS_URL=<同地址> python -m pytest -q --disable-warnings tests/platform tests/nexus`。跳过项未计为通过；可选真实 OpenCode/MySQL 用例另行执行。
- 无 systemd 的 PID 命名空间：**22 passed / 1 skipped**（无 MySQL socket），包括真实 OpenCode 1.18.35 和 TLS。命令由 `sudo unshare --pid --fork --mount-proc` 启动，显式设置 `NEXUS_TEST_NO_SYSTEMD=1` 与 `NEXUS_TEST_OPENCODE_BIN=/usr/local/bin/opencode`，用例断言实际不存在 systemd 并带非空 unit 验证重启路径。
- Linux 首轮失败分别是测试替身未声明 systemd、资源打包遗漏，以及全量顺序触发 Key 吊销时缺少 Redis。已补齐测试模式/完整原仓库资源，并以专用临时 Redis 复验；没有降低运行态依赖或跳过失败测试。
- 真实 OpenCode 1.18.35：独立目录、HOME 和端口，验证 /config 地址、主/小模型、同进程复用、重启后配置及受管身份。
- MySQL 8.0.36：独立 socket 实例，原表达式结果 0，显式 JSON CAST 结果 1；SQLAlchemy 实际条件写入匹配成功，快照改变后写入被拒绝。未操作已有 MySQL 实例。
- TLS：临时自签证书默认被拒绝，SSL_CERT_FILE 配置信任 CA 后 WSS 建连成功；没有 CERT_NONE/verify=False。
- Python 构建：用临时源码包和 configure/make 替身验证引导、解释器路径与重复调用复用；未真实编译 CPython。

## 剩余风险与部署动作

- 公司 EulerOS/aarch64 容器、公司 Nginx、真实 Gateway/Library MCP、模型推理与 SSH 一键部署整链路尚未验收。真实 OpenCode 测试使用假 key 和禁用的测试 MCP，不宣称外部服务连接成功。
- 无 systemd 模式负责受控启动/重启；容器重启或进程意外退出后的自动守护仍由容器启动管理承担，Worker 离线也可通过 SSH 维护恢复。
- 公司若已手工启动 OpenCode 而无本实现进程记录，部署会拒绝接管其监听端口；先人工核对并停止旧进程，再部署，不能猜 PID 或删除不确定记录。
- 源码包必须预先放到远端，构建依赖由管理员准备；实际源码编译耗时可能需要调高维护超时。
- 更新仓库后同步 Nginx 精确路由并执行 nginx -t，再 reload。配置 CA、Python 源码路径后重启读取配置的常驻进程；然后在空闲 Worker 上重新部署以更新 Worker wheel/env。

本次没有改动公司服务器或已有本地服务。临时 MySQL、Redis/转发在验收后关闭；该轮验收时尚未提交；本次随修复分支交付。

部署细节见 [Linux 远程员工部署](../deploy/REMOTE_EMPLOYEES.md)。变更契约与唯一任务清单位于 `openspec/changes/nexus-container-worker-deployment/`。


补充的网关语音输入修复与本地真实模型联调见 [语音网关核验](nexus-v1.4.8-gateway-speech-input-20261022.md)。本轮新增 schema 只通过 0002_gateway_speech 演进，原 Worker 修复仍不改变 schema。
