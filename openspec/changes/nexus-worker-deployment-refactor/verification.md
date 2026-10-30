# 实现核验：nexus-worker-deployment-refactor

| 维度 | 结论 |
| --- | --- |
| 完整性 | 7/7 实施任务完成，3 项 capability 有实现及回归 |
| 正确性 | 当前可运行验证通过；真实公司 SSH/下载/启动验收未完成 |
| 一致性 | Nexus 服务端控制安装判断和 SSH 启停；删除远端安装脚本及 systemd 分支；不改变 Runtime 状态事实源 |

## 映射

- 来源与全局包配置：worker_deployment.py、remote_artifacts.py、remote_package_config.py；本地真实 HTTP 制品下载/摘要/损坏内容/不允许主机/重定向/版本失败保持旧安装，配置冲突与普通 pip/npm 命令读取持久化默认值。
- 系统与运行环境：remote_bootstrap.py；系统/架构/root、现有解释器复用、显式解释器错误、缺失依赖和构建失败测试。固定 Python/Node 上游制品实际下载并校验摘要，目标 Linux 编译未实际执行。
- 服务端编排及运行隔离：remote_provisioning.py、remote_workspace.py、remote_processes.py、ssh_client.py；真实本地 Git 事务/配置合并、短期凭据清理、持久化维护阶段失败、SSH 双锁 channel 保持及失败关闭、PID 复用/未知监听/忙碌/租约丢失拒绝测试。
- 部署/更新/重启入口均经 SSH；OpenCode 健康后启动 Worker，版本/环境变化才重启、token 与未变化安装复用。Worker 本身保留既有通信及本地配置重载原语。

相关服务全量回归 1429 passed、30 skipped，Worker 相关初次集中回归 280 passed、1 skipped；最新平台增量后完成 Ruff 和差异检查。没有发现未实现项（CRITICAL 0）。

WARNING：公司根域名已由用户确认，官方文档支持根路径，但具体版本文件后缀仍是明确标记的候选；本机 TLS 探测失败。真实 SSH 尝试发生 BadHostKeyException，未绕过校验，未执行目标安装。Linux /proc 进程组、公司 Worker 回连与容器重启恢复仍需实机验收。保留 change，不在本轮归档。

证据与限制见 docs/reviews/nexus-v1.4.9-deployment-refactor-20261030/README.md 和 docs/deploy/REMOTE_EMPLOYEES.md。
