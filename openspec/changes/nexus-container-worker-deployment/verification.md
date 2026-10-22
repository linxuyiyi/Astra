# OpenSpec 实施核验

变更：nexus-container-worker-deployment。基线 a3f429e7c，分支 codex/worker-container-deploy。

| 维度 | 结果 |
| --- | --- |
| 完整性 | 16/16 tasks，12 个 Requirement，19 个 Scenario |
| 正确性 | 各 Requirement 均有实现与针对性场景验证；部署轮全套 1280 passed / 8 skipped；分配补充轮 Linux 全套 1303 passed / 8 skipped，恢复扫描最终修订另跑 150 passed；语音轮最终 Linux 全套 1322 passed / 8 skipped |
| 一致性 | 沿用维护租约/占用/原生空闲、完整快照 CAS、运行配置事实源、MCP 认证及任务仓库绑定 |

## Requirement 与证据

| Requirement | 实现 | Scenario 证据 |
| --- | --- | --- |
| 无 systemd 容器部署和重启 | remote_provisioning + process_manager | 容器 deploy/重复 deploy 替身；Linux 实际分离进程的 EOF/复用/重启；陈旧 PID 记录拒绝信号；无 systemd namespace 中真实 OpenCode 重启 |
| 统一入口与 TLS 验证 | transport/local_opencode + nginx-nexus.conf | 四种 scheme 映射、无效 URL 拒绝、真实自签 WSS 未信任拒绝/信任 CA 成功、精确代理模板检查 |
| 真实运行配置和模型约束 | channel_switch/process_env + remote_install.sh | 已有 listener 确实重启、端口释放等待、gate 失效阻止新启动、延迟/缺字段重试、磁盘不可冒充成功；真实 OpenCode /config 及主/小模型核验 |
| Python 引导 | settings + remote_install.sh/provisioner | 不存在目标解释器仍能读源码路径；源码包与 configure/make 替身走完整引导并复用实际解释器路径；Linux 安装器早期失败及 venv 检查 |
| 健康并发保护 | maintenance.status_detail_matches | MySQL 8.0.36 实际条件写入成功/陈旧快照拒绝；原有配置变更、维护占用与 Worker epoch 竞争测试 |
| 查询可用分类 | task_tools/task_mcp + 任务管理 Skill | 未识别员工拒绝、只返回启用项、排序/模板/当前禁用变更、工具注册；MCP 凭据与身份中间件测试 |

## 分级结论

- CRITICAL：0。没有未实现 Requirement 或未完成清单。
- WARNING：公司 EulerOS、真实 SSH 一键部署与 Gateway/MCP/模型推理整链路尚未验收；本地 Linux 与真实 OpenCode 不能代替该验收。
- WARNING：源码构建仅验证引导流程与替身构建，未实际编译 CPython。
- WARNING：Nginx 完成模板/路由检查，未在公司实例执行 nginx -t、reload 与真实 Upgrade。

本次执行仓库 openspec-verify-change 流程，按任务、Requirement、Scenario 和设计逐项核对；内置 CLI strict validate 通过。保留 change 供公司部署验收追踪，不修改 Astra 历史 specs。完整原因、处理与验证记录见 docs/reviews/nexus-v1.4.8-container-worker-fixes-20261022.md。


## 补充：任务分配核验

新增 nexus-task-worker-routing，3 个 Requirement、5 个 Scenario 已核对：

- 类型/数字员工及公共池：assignment.py 为后端唯一资格函数；自动分配、API/MCP 和启动共用。test_worker_assignment 覆盖双方配置/未配置、跨员工拒绝、类型拒绝、公共 Worker 创建及恢复队列不阻塞。
- 仓库与调度分离：Dispatcher 和启动能力门禁删除仓库要求；真实临时 Git 测试保留任务 repository_id 且快照/base commit 来自实际 Worker；启动模拟客户端覆盖任务无仓库与跨仓库。
- 入口一致与安全：API 编辑责任员工时复检现有 Worker，MCP 身份作用域保留；原有占用/维护/健康/父任务回归通过。前端 31 项测试、类型检查、生产构建及隔离模拟接口页面操作通过。

恢复扫描补强后的最终 Linux 相关回归 150 passed；完整路由轮为该补强前 1303 passed / 8 skipped，证据不混淆。公司 MySQL 并发调度与真实业务任务整链路仍未验收。前端/后端执行边界、公共池含义及截图见 docs/reviews/nexus-v1.4.8-task-worker-routing-20261022.md。


## 补充：语音网关核验

新增 nexus-gateway-speech-input 的 3 个 Requirement / 5 个 Scenario：

- 语音网关：路由类型、新音频入口和 multipart 别名复用统一请求生命周期；test_gateway_speech 覆盖授权/类型/格式/重复字段/大小/畸形请求、成功/失败租约释放及协议错误；ASR 专项探测使用真实 WAV 协议。
- 语音输入配置：数据库配置、真实身份审计、服务端 Key 与模型选择、禁用即失败；新应用实例/实际 API 重启读取同一配置。生产页面实际保存/刷新通过，输入实际组件 harness 覆盖私聊/群聊草稿、故障重试和不自动发送，设备为音频替身。
- 独立服务：真实离线 SenseVoice API、本地实际网关及统一服务链路识别成功，网关元数据确认别名/实际模型/节点/HTTP 200；独立 ASR 22 项测试通过。

语音轮 Linux 最终完整回归 1322 passed / 8 skipped；最后增强迁移场景另跑 Linux 6 passed，本机语音与迁移 23 passed。MySQL 8.0.36 实际空库初始化及已发布 init 升级保留路由成功，未修改 init。两端前端测试和构建、Ruff、diff 与 strict validate 通过。完整记录见 docs/reviews/nexus-v1.4.8-gateway-speech-input-20261022.md。

新增 WARNING：硬件麦克风、完整 AgentScope 私聊/群聊发送、公司 Nginx/跨机和正式托管未验收；本地部署是独立验收服务。既有混合模型池需核对路由类型。CRITICAL 仍为 0；没有降低既有 Requirement，也没有用本地证据替代公司验收。
