## 1. 作业底座

- [x] 1.1 0054 迁移：Job/Segment 表、Evidence 扩展字段
- [x] 1.2 确认完成事务内创建 Job；重复确认幂等
- [x] 1.3 Celery `nexus.experience-review.scan/process`、队列、租约与恢复
- [x] 1.4 配置开关、预算与 Gateway Key

## 2. 轨迹与证据

- [x] 2.1 分页 TraceReader（本地与 Worker，复用现有 messages_page）
- [x] 2.2 标准化、因果切片、Token 预算
- [x] 2.3 证据索引与服务端校验（伪造/跨 Session/水位）

## 3. 复盘与写入

- [x] 3.1 独立 ReviewLLMClient（JSON + 有限重试）
- [x] 3.2 Map / Reduce / Harness 双轨输出
- [x] 3.3 CandidateWriter + KnowledgeService 原子写入
- [x] 3.4 NO_FINDINGS、去重、失败分类

## 4. 接口与界面

- [x] 4.1 复盘查询/详情/证据/重试 API
- [x] 4.2 任务详情复盘状态与候选入口
- [ ] 4.3 数字图书馆改进前/后/证据展示

## 5. 验证

- [x] 5.1 单元测试：幂等、租约、切片、证据、Map/Reduce、写入
- [ ] 5.2 大轨迹/重启/旧结果晚到测试（已有片段/租约/重试单测；远程大响应与真实长轨迹待补）
- [x] 5.3 ruff、前端 type-check/build、真实服务冒烟
