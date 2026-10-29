# Nexus V1.4.9 数字办公室首页重构

## 问题原因

原首页使用星系布局，无法直接表达数字员工在办公室工作的状态。现有员工详情主要按 Worker 配置绑定筛选，不能覆盖没有绑定员工但实际正在执行该员工任务的共享资源；首页全局任务预览也有截断，不能用作工作台完整事项来源。

## 修改内容

- 新首页为完整像素办公室，直接复用用户 V5 的六款可爱机器人和家具 PNG，保留标识墙、计划看板、木地板、窗户、绿植及沙发。三人一排、六人两排；更多员工每六位一间办公室，可切换和定位。
- 工位采用可访问按钮。点击自动聚焦并展开工作台；空白/Escape 还原全景。减少动效/后台暂停动画；动画只表达已确认的平台工作概况，不作为 AgentScope 或 OpenCode Session 状态事实。
- 工作台新增只读 `GET /api/v1/nebula/employees/{id}/office`，复用既有任务归属及健康/占用投影；不修改生命周期、调度、AgentScope 或 schema。
- Worker 按当前任务实际执行关系关联，现有绑定仅代表调度配置中的可用范围。未执行本员工任务且正执行其他员工任务的资源不列为本员工执行资源；共享 Worker 可通过实际任务同时关联不同员工，不建立新归属。
- 单列按等待人工/异常、执行中、空闲排序。共享 Worker 在其他任务上等待人工时，也优先并作解释。接口单页最多 20 条，界面每页 6 条，每个 Worker 最多 3 个当前员工任务摘要，支持查看其余任务。事项独立分页，不受全局预览 400 条影响。
- 待分发、阻塞、异常提供真实原因与现有 Task 入口；提供员工对话、Worker 详情、全部任务和原五个模块入口。接口清除其他用户的源会话引用。
- 请求取消与序号防止员工切换竞态；失败保留确认数据并显示时间和重试。完成提示只响应确认的完成计数增长。
- 首页客户端标识更新为 1.4.9，保留既有静态 dist，构建验收输出到独立目录，供审查后按正常流程部署。

## 验证结果

| 验证 | 结果与边界 |
| --- | --- |
| 首页聚合后端 | 9 passed，覆盖共享资源/多员工、排序、独立分页、全局截断、失败不冒充空态、私聊引用、HTTP 参数与 404 |
| 相关后端回归 | `tests/nexus` + `test_nebula.py` + 平台 `test_api.py` + `test_service_gateway.py`：重验 199 passed / 6 skipped |
| 前端 | type-check、41 项测试、生产构建通过；已有大 bundle 提示保留 |
| Ruff | `app nexus_api tests` 及新增夹具脚本通过 |
| OpenSpec | `nexus-pixel-office-home` strict validate、项目 config 检查通过，逐 Scenario verify 见 change 的 verification.md |
| 浏览器 | 1280×720 三/六人、390×844、真实 16 人目录、工位点击/聚焦/空白/Escape、快速切换、两种分页、人工入口、断线保留/重试、空态及完成反馈已核验 |
| 原模块 | 真实 API 下进入员工对话、任务、图书馆、网关、配置均成功；未发送模型消息或执行任务 |
| 视觉 | 根目录 `design-qa.md`：passed；含原型和实现并置及修正历史 |

证据位于 `docs/reviews/evidence/nexus-pixel-office-20261029/`。三/六人、人工异常和故障状态是明确隔离的 SQLite 夹具，通过正式路由查询；真实本地目录与模块导航单独核验，不能将夹具等同于生产 Worker 执行。

扩大到全量平台测试时，120 秒内记录 115 passed / 10 failed 后中断：包括人工等待文案断言不匹配、安装器受到本地既有配置影响，以及本机 OpenCode 配置 1.18.33 与测试桩 1.18.35 不匹配导致重复超时。使用基线 `4f203c65d` 原始 nebula.py 的独立导入目录对照，人工文案和 OpenCode 版本两项同样失败（`baseline-regression.txt`），未修改这些无关行为。相关回归首次出现一次协作并发测试失败，该用例修改前隔离重跑通过，完整相关集合再次重跑也 199 passed / 6 skipped；如后续出现须单独排查，不能据此声称全量回归通过。

## 截图

- `office-three.jpg`：三人全景；`office-six.jpg`：六款机器人与两排工位。
- `office-attention.jpg`：人工优先 Worker 工作台；`office-inbox.jpg`：事项原因与入口。
- `office-mobile.jpg`：窄屏聚焦；`office-completion.jpg`：确认后完成提示。
- `office-empty.jpg`、`office-offline-retained.jpg`：空态及失败保留。
- `comparison-overview.jpg`、`comparison-focus.jpg`、`comparison-panels.jpg`：视觉并置。

## 运行与交付

交付分支：`v1.4.9`。源码、素材、契约和验收证据一并提交，发布结果以 Git 提交与远端 SHA 核对为准；原 18520 服务仍保持原部署。

当前真实数据增量预览：`http://127.0.0.1:15149/`。既有业务 API 转发到正在运行的 Nexus 18520，新增只读办公室接口在独立 18549 查询同一数据库；不启动第二个执行调度器。对应脚本 `tools/e2e/nexus_pixel_office_live_preview.mjs`，可用 LIVE_API / OFFICE_API / PORT 指定端点。该脚本仅用于本地验收，正式部署仍为单个 nexus-api。

独立三人视觉验收：`http://127.0.0.1:15150/`；六人验收：`http://127.0.0.1:15151/`，均使用独立夹具，不执行真实模型或 Worker。

正常统一部署：使用包含本次 Python 代码的 nexus-api，进入 `services/nexus-api/web` 执行 `npm run build`；前端仍由同一服务托管，无迁移。最终构建核验已输出到 `/tmp/nexus-office-v149-build-final-20261029`，没有将临时 API 路径写进产品。

可复现夹具：

```bash
services/nexus-api/.venv/bin/python tools/e2e/nexus_pixel_office_fixture.py \
  --directory /tmp/nexus-office-my-review --employees 3
# --employees 支持 0 / 3 / 6，拒绝覆盖已有数据库
```

隔离 API 使用正式路由与聊天客户端初始化，省略平台执行/调度器生命周期：

```bash
cd services/nexus-api
ASTRA_DATABASE_URL=sqlite+aiosqlite:////tmp/nexus-office-my-review/office.db \
ASTRA_DATA_DIR=/tmp/nexus-office-my-review ASTRA_EXECUTOR_TYPE=worker \
.venv/bin/python -m uvicorn --app-dir ../../tools/e2e \
  nexus_pixel_office_fixture:create_preview_app --factory \
  --host 127.0.0.1 --port 18550 --timeout-graceful-shutdown 1
```

在 web 目录以 VITE_NEXUS_API_PROXY_TARGET 与 VITE_PROXY_TARGET 指向该 API 后运行 `npm run dev -- --host 127.0.0.1 --port 15150 --strictPort`。

## 剩余风险

公司 MySQL 8.0.36 的新增窗口查询、真实 Worker 原生人工请求答复与恢复、长时间负载和公司部署尚未验收。真实员工首页状态为平台任务工作概况，不代表 AgentScope 正在输出 token；任务/Session 仍由原服务负责。原有网关 Ollama 与 SenseVoice 节点不可用在原页面如实显示。本次不将全量测试的存量失败、6 个跳过或上述边界标记为已通过。
