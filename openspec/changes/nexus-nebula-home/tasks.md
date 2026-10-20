## 1. 后端只读投影

- [x] 1.1 `GET /api/v1/nebula/snapshot`：员工、Worker、任务统计、执行光路、协作边、待绑定/待分配、数据冲突与降级
- [x] 1.2 员工星系详情、员工任务分页、Worker 光路任务、职责协作、单任务投影
- [x] 1.3 责任员工/Worker 归属冲突显式标记；不随机归属、不重写任务
- [x] 1.4 今日完成按配置时区与 `completed_at` 唯一任务计数
- [x] 1.5 职责请求去重、双向合并、私聊来源 owner 校验

## 2. 前端首页

- [x] 2.1 正式 `/` 路由与既有导航入口；`/chat` 保持可用
- [x] 2.2 确定性星系布局、Worker 轨道、任务卫星、执行光路、协作线
- [x] 2.3 Canvas 2D 透视投影、聚焦、旋转/缩放/平移/巡航/重置、命中检测
- [x] 2.4 全局指标、待处理入口、点击节点后的轻量提示
- [x] 2.5 节点提示跳转既有员工对话、任务中心与 Worker 详情；首版不内嵌对话
- [x] 2.6 SSE 失效通知、15 秒快照、断线重连、降级提示、路由恢复选中
- [x] 2.7 减少动态效果、后台暂停、窄屏降级

## 3. 验证

- [x] 3.1 后端聚合/冲突/时区/协作去重/分页/降级/HTTP 契约测试
- [x] 3.2 `ruff check app nexus_api tests`
- [x] 3.3 `pytest tests/nexus` 113 passed / 4 skipped；`pytest tests/platform/test_nebula.py` 6 passed
- [ ] 3.3.1 全量 `tests/platform`：OpenCode 安装版本 1.18.35 与测试期望 1.18.33 不一致导致 `test_opencode_server` / `test_employee_serve` 失败，另在 69% 处出现与本变更无关的 SIGSYS；需在匹配版本的环境复跑
- [x] 3.4 `npm run type-check && npm run build`；`npm test` 21 passed（含星系布局稳定性与队列上限）
- [x] 3.5 Chromium 真实浏览器验证：1440×900、1600×960、1920×1080、1024×768、820×900；全屏无站点导航、员工/Worker/任务提示、跳转聊天页与任务中心、协作详情、未关联任务、刷新恢复与路由恢复
- [ ] 3.6 生产多用户身份接入后的权限验收（当前 `/api/v1/me` 仍为占位身份）
