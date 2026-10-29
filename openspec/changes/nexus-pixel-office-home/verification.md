# nexus-pixel-office-home 实施核验

2026-10-29，按 openspec-verify-change 的完整性、正确性和一致性逐项核对。

| 维度 | 结果 |
| --- | --- |
| 完整性 | 6/6 tasks；3 项 Requirement；8 个 Scenario 均有实现/核验依据 |
| 正确性 | 共享 Worker 通过当前任务关联，状态沿用现有投影，列表分页/排序在服务端完成；无调度、生命周期或 schema 改动 |
| 一致性 | 原有快照与 SSE 更新继续使用；Vue/Element Plus 分页/既有导航保留；单列资源列表按用户明确要求实现 |

## Scenario 核对

| 场景 | 实现与证据 |
| --- | --- |
| 三人和六人办公室 | `OfficeScene.vue`、`model.ts:108`；浏览器 `office-three.jpg`、`office-six.jpg`，六款机器人可辨；office-model 测试覆盖桌面及窄屏工位边界 |
| 聚焦与返回 | `PixelOfficeHomeView.vue:222`、OfficeScene 工位按钮；直接点击、定位、空白、Escape、减少动态及 390×844 浏览器验证 |
| 共享执行资源 | `nebula.py:1276`；`test_nebula.py:456` 验证未绑定 Worker 为两个员工执行各自任务、无其他员工任务混入、不写归属及私聊源引用 |
| 介入与异常优先 | Worker 排序涵盖健康异常、全局等待人工、该员工连接/活动异常；每个 Worker 三条摘要也优先问题；测试及 `office-attention.jpg`、`office-inbox.jpg`；共享资源其他事项只提示状态不泄露会话 |
| 大量数据 | `test_nebula.py:492` 将全局预览限制至 1，独立办公室分页仍获取全部计数/人工事项；浏览器 23 Worker / 28 事项的第二页验证；HTTP 校验页码与 20 条上限 |
| 切换与失败 | `PixelOfficeHomeView.vue:239` AbortController + 序号；浏览器快速三次切换最终员工数据一致。关闭隔离 API 后 `office-offline-retained.jpg` 保留 23 个资源计数、任务和确认时间；恢复后重试正常；降级源测试返回 503 而非空成功 |
| 继续工作与原模块 | Worker 展开详情与任务入口、真实员工对话及五模块导航已核验，记录 `module-navigation.json`。任务夹具无真实 Session，未声称完成原生人工恢复 E2E |
| 空目录与后台 | 空目录 `office-empty.jpg`，无虚构员工；visibilitychange/减少动态暂停，状态和导航保留；完成提示由 SQL 确认计数增长触发，证据 `office-completion.jpg` |

上述 8 个 Scenario 均已核对；“切换与失败”同时覆盖请求竞态及断线失败，“继续工作与原模块”同时覆盖入口和私聊引用。

## 验证

- 首页后端 9 passed；相关后端集合最终 199 passed / 6 skipped。
- 前端 type-check、41 项测试和生产构建通过，输出 `/tmp/nexus-office-v149-build-final-20261029`。
- Ruff 全量 `app nexus_api tests` 和新增夹具脚本通过。
- OpenSpec strict validate 与 config 检查通过；视觉 QA `design-qa.md` 为 passed。
- `git diff --check` 通过；保留原已跟踪 dist 与原工作区其他未提交内容。

## 问题与风险

CRITICAL：本次首页范围内未发现未完成项。

WARNING：全量平台回归不是通过状态。中断前 115 passed / 10 failed；基线 `4f203c65d` 代码对照复现人工文案断言及 OpenCode 版本配置失败，见 `baseline-regression.txt`。相关集合首次一次协作测试失败，基线隔离和本次完整相关集合重验均通过，记录保留，没有将首次失败隐藏。6 项跳过不计为验收通过。

WARNING：公司 MySQL 8.0.36、真实 Worker 原生人工请求恢复、长时间负载与正式部署尚未验证。现有 AgentScope / OpenCode 行为未修改。

SUGGESTION：后续如调整窄屏全景密度，应保持完整团队与定位入口；当前聚焦可读，不阻断本次交付。

结论：本次首页实现与契约一致，可交付本地审查；保留以上生产和全量回归边界，change 未归档以便审查。

## 2026-10-30 复核：比例、十二人分页与取消单员工放大

按本 change 更新后的契约重新核对，取代2026-10-29关于六人分页与聚焦的对应结论。

| 维度 | 结果 |
| --- | --- |
| 完整性 | 9/9 tasks，3项Requirement，9个Scenario |
| 本次正确性 | 三至十二人、超过十二位、选择与关闭均有代码/单元测试/浏览器证据 |
| 一致性 | 唯一12人分页常量；不传员工索引给镜头；保留原状态事实源，无API/数据库变更 |

- 三至十二人：model.ts按整页布局并限制素材大小；office-model测试覆盖1/3/4/6/7/9/12人及1280×720、1920×1080、1024×768、390×844；截图04/05/10/12。
- 超过十二位：PixelOfficeHomeView统一使用OFFICE_ROOM_SIZE=12，翻页/定位/路由均复用；隔离16人目录实测12+4、跨页选人身份正确；截图08/09。
- 选择与关闭：OfficeScene镜头只接收工作台是否打开，不再接收员工索引；3人页人物打开前后均62.72px；12人页切换第12位与第1位，镜头style完全一致。Escape、空白和关闭按钮通过，截图06/07/14。
- 原完成反馈：在多排姓名牌显示且不覆盖人工异常；隔离库通过已完成任务触发真实投影计数变化，截图13。未调用执行器或模型。
- Web44项测试、type-check/build、Ruff夹具检查、OpenSpec strict validation与diff检查均通过。其他业务代码未变更，本次不把旧版后端回归结果宣称为新跑的结果。

CRITICAL：0。WARNING：0（未发现本次需求/设计偏差）。隔离数据、窄屏底部覆盖与无障碍测试边界见 `docs/reviews/nexus-office-density-20261030.md`。原生产MySQL、真实Worker与长期运行边界继续保留。

发布前已接入远端既有任务运行修复（202c67838、bb9ddf049），重新运行Web44项测试及type-check/build，并同步追踪的Web静态产物。本次提交不覆盖这些运行修复。
