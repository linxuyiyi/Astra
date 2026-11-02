# 真实 Linux Worker 部署验收

日期：2026-11-02。分支：`v1.4.9`。本报告基于真实验收时的工作区源码，发布提交以 Git 记录为准；本报告补充同目录 `verification.md` 的本地测试阶段，不代表公司生产验收。

## 环境与清理范围

- 使用用户此前提供的 CentOS 8.2 x86_64 测试机。SSH 主机密钥变化经用户明确确认可信后，核验并更新本机已知主机记录。
- 先通过进程命令行、工作目录和 Worker 环境标识确认旧测试部署归属，再终止两条旧 DSH Worker 进程并删除对应部署目录。无关的协作测试目录保留。
- 本机运行独立 Nexus API、网关、Library MCP、Redis 和真实 Git HTTP 服务；数据库由空 SQLite 运行 Alembic 至 `0007` 初始化。没有修改原有服务数据库。
- SSH 反向转发与远端临时 HTTP relay 仅用于让远端访问隔离控制面，属于验收网络设施，不是产品新增通道或 fallback。
- 远端从系统 Python 3.6 环境构建私有 Python 3.11.17，安装 Node 22.23.3、OpenCode 和当前源码 Worker。Worker 不接收厂商模型凭据，只通过 Nexus 网关调用实际模型。
- 凭据、机器地址、原始响应及日志仅保存在本机权限受限的 `/tmp/nexus-worker-real-e2e-20261102/`，不写入仓库。

## 暴露的问题与修复

1. 生成的端口归属检查脚本循环缩进错误：修正缩进，并执行生成脚本覆盖本进程与其他进程交错场景。
2. 原默认镜像域名在目标机无法解析：改用实际下载验证过的华为云公共镜像路径，保留 TLS 与校验和检查，更新环境示例与部署文档。既有显式配置不会被静默覆盖。
3. npm 镜像元数据返回仓库相对 tarball 路径：按配置的 registry 解析，仍限制协议、允许的源和 SHA512，不引入其他下载源 fallback。
4. 首次 SFTP 原子写配置或 Skill 时父目录可能不存在：只在首次写入时创建父目录，目标父目录权限为 `0700`，保留既有目录权限；新增真实本地文件系统回归测试，确认修复前失败、修复后通过。
5. 机器镜像自带 HTTP pip 源及 `[install] trusted-host` 导致既有安全检查拒绝部署：备份后调整机器的 pip 配置为 HTTPS 公共源并移除旧 TLS 绕过设置。没有修改安装器去绕过冲突检查。
6. 隔离验收配置误覆盖 OpenCode 为 1.18.33，而仓库正式版本为 1.18.35：纠正测试配置并重新部署，保留版本校验。该项属于验收环境错误。

首次冷安装经过上述失败与修复后重试成功；重试复用了已下载或已编译的依赖，不将本轮描述为一次点击无重试的冷安装成功。

## 已核验场景

| 场景 | 结果与证据 |
| --- | --- |
| 部署验收 | Worker 报告 READY、可调度，OpenCode、Worker Agent、模型、MCP、Skill、Git 六项装备均验证通过；单守护、单 OpenCode 子进程，独占锁有效 |
| 改展示字段 | 仍 READY，守护与子进程 PID 不变 |
| 改 Skill 正文 | 立即 DRIFTED、不可调度，保存不触发同步，进程不变 |
| 手动部署同步 | 完成后 READY；本次耗时 109.37 秒 |
| 相同配置重复部署 | READY，守护和子进程 PID 均不变；本次耗时 75.04 秒 |
| 删除最后一个 Skill | 保持 DRIFTED，直到管理员手动部署后 READY；102.51 秒 |
| 在线深度检查 | 实际 Git 与网关检查通过；4.07 秒 |
| 在线更新仓库 | 真实远端仓库快进到新提交；3.06 秒 |

## 自动化回归

- `.venv/bin/pytest tests/platform tests/nexus`：1361 passed、32 skipped，217.97 秒。保留现有弃用等警告。
- 定向安装/部署回归：44 passed。
- Ruff、OpenSpec validate、`git diff --check` 通过。
- 本轮重跑前端：79 个测试、Web 与 chat-island 的 type-check/build 通过（保留现有 chunk 大小及 path 模块 externalized 提示）。本轮真实浏览器没有 mock API，已确认列表页不额外请求逐节点 `/health`，并通过真实按钮提交部署。

## 真实执行与恢复结果

| 场景 | 结果与证据 |
| --- | --- |
| 正式 OpenCode 版本重新部署 | 1.18.35 部署成功，READY；96.21 秒 |
| 任务占用门禁 | Task 运行期间请求 restart，实际 HTTP 409 |
| 真实远端模型任务 | Task `T-20261102-08C8A3` 在远端 OpenCode Session 中执行，`read` 工具 completed，实际输出包含 README 中的证明值；人工确认接口完成后 Task 为 SUCCEEDED。网关记录两次 `deepseek-flash` 推理均 HTTP 200，分别输出 75/18 tokens。验收条件没有告知预期证明字符串 |
| 定向强杀 OpenCode | 原守护 PID 不变，新子进程恢复；已经完成的 Task 保持 SUCCEEDED。本轮采样仅观察到 READY，未捕获短暂非就绪窗口，不据此声称已验证错误 UI 展示 |
| 定向强杀守护进程 | 节点标为 offline，锁释放；OpenCode 孤儿存在，随后唤醒时被清理 |
| 离线轻量唤醒 | 真实 SSH `ctl.sh start`，9.12 秒恢复；新守护、新子进程均单实例，独占锁有效 |
| 在线重启 | 通过现有 Worker Gateway，6.08 秒完成并恢复 READY |
| enabled 同步开关 | API PATCH 停止/恢复派单立即生效；真实浏览器点击开关验证两次 PATCH、零次异步维护 POST |
| 最终状态 | READY、schedulable=true；真实浏览器列表逐节点健康请求为 0，无 pageerror |

原始证据包括 `scenarios.json`、`actual-read-proof.json`、`gateway-inference-proof.json`、`browser-final-result.json`、`final-health.json` 与 `real-final-ready.png`，位于上述私有临时目录。临时控制面与网络转发需要保持运行才能维持此隔离环境在线。

## 边界

- 没有重启整台远端宿主机，没有验证系统启动钩子；守护终止后的恢复依赖管理员唤醒或部署方接入的启动入口。
- 控制面使用隔离 SQLite，未重跑公司 MySQL 8.0.36、AgentScope 聊天、多副本或长时间稳定性验收。
- 远端访问控制面经过测试转发网络，延迟受该拓扑影响；重复部署与配置同步仍为分钟级，不能声称秒级完成。
- 本次发布仅包含 Worker 部署与健康收敛相关改动；`0001`–`0006`、无关目录和历史 Specs 不修改。
