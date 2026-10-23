# nexus-experience-quality 验证

2026-10-23。按 openspec-verify-change 核实 proposal、design、tasks 和 delta spec；运行证据见 `docs/reviews/nexus-experience-quality-20261023/`。

| 维度 | 结果 |
| --- | --- |
| 完整性 | 6/6 任务；3 条 Requirement、6 个 Scenario 有实现与验证 |
| 正确性 | 通常一条但上限仍为 3；复用可以零条；实际已读参照受来源、预算与脱敏约束 |
| 一致性 | 复用现有 Map/Reduce 和最终比较；没有新 Agent/Skill 运行框架、自动重跑、自动编辑或发布 |

## Scenario 对应

| Scenario | 代码与证据 |
| --- | --- |
| 方法与测试属于同一结论 | reduce_analyzer 的方法/验证归纳与多条合并；旧策略同输入 5 次均 3 条，新策略 5 次各 1 条；test_overlapping_lessons_are_merged_below_the_configured_cap |
| 常规成功没有新发现 | Map/Reduce 区分观察和经验；常规 Skill 要求但无使用证据的模型场景返回零条；真实 Worker Task 成功后 Job NO_FINDINGS |
| 读取后原样应用 | coordinator 只查该 Task/来源 Session 的 read 关联知识；单条候选也与已有知识最终比较；复用样本 5 次最终均零条；真实 Task62873B 不新增知识但 read 记录保留 |
| 新的边界发现 | 当前完整归纳器保留“按事件时间而非到达顺序”的具体增量候选，执行对照有实际 Python 结果；明确这不等于原方法在原合同下错误，仍需人工审阅措辞 |
| 没有实际使用证据 | 新 Map/Reduce 规则及 Skill 未加载/多因素混杂两项补充模型场景均返回零条，不把用户指令或成功当收益 |
| 可定位实际差异 | 同一 Bash 管道实际仅改 pipefail，退出码0→1；当前归纳器输出具体脚本改进候选，未自动应用或发布 |

多条独立值的结构要求另有双向单测：缺说明拒绝，有说明允许；实际双问题补充场景可输出两条，数量没有硬改成 1。结构验证不证明解释本身正确，候选中的因果或隐含前提表述仍需人工核实。

## 验证

- 后端全量 1311 passed、32 skipped；相关集合 35 PASS；Ruff 通过。
- web 30 PASS、type-check/build 通过；真实页面显示“无新经验”，不显示重复的 no_findings；无 pageerror。
- 私有 MySQL 8.0.36 新库 Alembic head、JSON/Text DISTINCT 参照查询和去重通过，实例已停止。
- 原规则反例包括低于上限不合并、多条缺独立说明仍接受、已读参照未传入、单条不比较和旧版本标识；参照测试夹具补真实 observation 后另对旧 coordinator 确认失败，未用空输入测试误差冒充产品反例。
- Strict 校验见 `evidence/openspec-validation.txt`。

## CRITICAL

无未完成的本 change 实施项。

## WARNING

- 数量和重复有改善，不能据此承诺所有文字与因果解释正确；中间试验和最终候选的措辞局限已保留在报告，仍须人工审核。
- 参照是当前 read 关联知识的有界当前记录，非历史内容版本快照，不是全库语义去重；原生执行事实以 Session 证据为准。
- 只有一个网关模型和少量重复样本，未证明长期成功率、其他模型或公司特定 Skill 的执行收益。
- 真实 Worker 流程仍在隔离 SQLite；MySQL 查询专项不等于生产全链部署。公司第三方 MCP 与 DSML 上游根因未新增验收。

保持 change 可审阅，不同步覆盖历史 Astra Spec，不自动部署生产。
