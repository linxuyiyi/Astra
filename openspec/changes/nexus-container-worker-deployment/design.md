## Context

基线 a3f429e7c。Task → Run → Employee/Worker → OpenCode Session 的执行、占用与展示事实源不变。公司问题单中的源码编译功能当前基线并不存在，需补齐引导；settings 的进程级缓存是既有约定。

## Goals / Non-Goals

支持无 systemd 的一键部署、重复部署和维护重启，逐项修复问题单。保留任务与仓库绑定，不复制 AgentScope 状态。Worker 修复不迁移 schema；补充的语音路由通过新 revision 增加配置。

## Decisions

- 按 PID 1 和 systemd 可用性选择明确的管理模式，不在 systemctl 失败后盲目切换。无 systemd 使用同一 Worker 包内的进程管理工具，以 PID、Linux starttime、cwd、argv 核对身份，完全重定向标准流并脱离会话。共享此工具供 SSH 部署与 Worker 重启调用，避免两套进程事实。
- 远端配置原子写入与私密权限沿用现有实现。OpenCode 独立环境文件不含 Worker token；Worker 环境文件不得直接作为 shell 代码 source。
- URL scheme 在传输边界统一规范化；TLS 用系统/显式 CA 信任，不采用公司建议的 CERT_NONE 或 verify=False。
- 运行配置仍以 /config 为核验依据，暂时不可读或缺字段时有界重试；磁盘文件不能证明实际加载成功。
- 健康写入保留原有短事务锁与完整快照 CAS；MySQL 显式将绑定参数 CAST 为 JSON，SQLite 使用既有 JSON 参数比较，避免隐式 SQL 字符串比较。两处环境锁与观测写入统一使用该条件。
- 早期 Python 用可用系统解释器解析 JSON；优先查找 3.11+，显式源码包只在私有目录编译，返回实际解释器路径供 venv 使用。
- 分类工具复用 TaskCategory，沿用 MCP 认证，保留 task_prompt/question_prompt 语义。

## Risks / Trade-offs

- 无 systemd 的进程在容器重启后需要重新部署/启动；本变更不安装 init 或守护服务。
- 实际 EulerOS/aarch64、MySQL 8.0.36、自签 CA、公司 Nginx 配置须真实环境验收；本地替身证据单独标注。
- 编译需要系统构建依赖和足够维护超时；失败保留旧解释器，不能替换系统 Python。

## 补充：任务分配

任务的 responsible_employee_id 与 Worker 的 digital_employee_id 表示数字员工关系；任一方未设置时按公共池处理，两边都有值才要求一致。类型必须受 Worker 支持；category_id 与 repository_id 只保留业务/来源语义，不参加接单匹配。统一资格函数供自动分配、手动 API/MCP 与启动复检调用；运行能力和健康门禁继续独立保留。

仓库绑定不迁移、不清空。工作区沿用实际 Worker 的现有 workspace/worktree，执行快照仍记录该 Worker 的仓库与 base commit；本次不按任务仓库隐式 clone、切仓或改写 Worker 配置。

## 补充：网关语音输入

路由增加 CHAT / EMBEDDING / ASR 类型；已发布 init 不改，通过新 revision 增加字段和单例语音配置表。原路由缺省 CHAT，现存纯 EMBEDDING 探测资源池的路由按明确节点配置一次性迁为 EMBEDDING，其他混合资源池由管理员核对。

语音输入配置只保存 route_id。浏览器保持同源 /api/asr/transcribe，但由统一服务验证用户身份、读取配置和服务端网关 Key 后调用 /v1/audio/transcriptions；不再由 Vite/Nginx 转发到 19899。网关在既有请求生命周期中有界读取 multipart、重写模型名，复用调度/租约/错误/元数据，音频与文字不落日志。独立 ASR 兼容端点共享现有识别引擎及上传、时长、并发限制，只支持非流式 JSON 转写；ASR 探测上传短静音 WAV 并检查 text 字段，不用 chat 探测冒充语音能力。
