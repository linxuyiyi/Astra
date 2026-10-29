# Nexus V1.4.9 员工形象统一验收

## 问题与实现

配置页原来保存任意 Emoji，首页另按员工 ID 和职责选择机器人，导致同一员工有两套形象。现在复用已有 avatar 字段保存稳定 ID，唯一目录为 `services/nexus-api/nexus_api/employee_avatars.json`；Python 校验、Vue 和聊天岛读取同一个目录。

保留 V5 六款机器人，增加六款 BotPixel 像素人物。素材固定来自 `@botharness/pixel-avatar@0.10.2` 的官方预设，使用透明 body/head SVG，不加入工具变形、Agent 内部执行动作或远程素材依赖。固定预设索引、重建脚本和 MIT 许可随代码提供：

- 来源：[BotHarness/BotPixel](https://github.com/BotHarness/BotPixel)、[Avatar Studio](https://tobs.app/en/avatar)。
- 生成器：`tools/e2e/generate_employee_avatar_assets.mjs`，使用 `BOTPIXEL_ROOT` 指定已安装的固定版本。
- 许可与资源：`services/nexus-api/web/public/avatars/`。

配置页提供图片选择、标签、选中态和预览；保存后统一用于员工列表/详情、办公室工位/工作台、聊天联系人、群组成员/消息，以及 Worker 配置的员工引用。改名、职责、状态与排序不再决定人物。原表单保存、脏数据退出及错误重试机制继续复用。

API 只接受目录 ID，拒绝 Emoji、未知 ID、URL、HTML 和 null。创建省略头像使用默认机器人；更新省略头像保留原值。此为显式契约收紧，旧 API 客户端须使用目录 ID。

## 数据迁移与运行

新数据迁移 `0006_employee_avatars` 基于已发布 `0005_dsh_runtime`，没有修改 init。迁移冻结升级前首页算法，含中文/非 BMP ID 的旧 JS hash 语义，旧值一次性变成对应机器人；已保存的合法角色保留。业务运行不再推测旧 Emoji。迁移不可恢复原 Emoji，回退需恢复升级前数据库备份。

本次仅对从旧办公室验收夹具备份得到的隔离 SQLite 库执行正式 `alembic upgrade head`，确认 revision 为 `0006_employee_avatars`。实际运行服务与真实业务库未迁移。预览：`http://127.0.0.1:15159/`，API 为隔离验收服务 18559；不启动 Worker 或模型。

部署顺序：备份数据库；在已配置目标数据库的 `services/nexus-api` 运行 `.venv/bin/alembic upgrade head`；先在 `chat-island` 执行 `npm run build`，再在 `web` 执行 `npm run build`，最后重启统一服务。聊天岛构建资源随本次提交更新；主 Web 编译产物在本地临时目录验证，部署仍须按上述顺序构建。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| `pytest tests/nexus tests/platform/test_employee_avatar_migration.py tests/platform/test_fresh_sql_storage.py tests/platform/test_nebula.py -q --disable-warnings` | 188 passed，6 skipped |
| 最终 API/数据迁移复核 | 50 passed |
| Web `npm test` | 43 passed |
| Web type-check/build | 通过，输出到 `/tmp/nexus-avatar-identity-web-build` |
| 聊天岛 type-check/build | 通过 |
| Ruff：app、nexus_api、tests、新迁移及 Alembic env | 通过 |
| OpenSpec strict validation、`git diff --check` | 通过 |
| Python wheel 打包 | 成功，包内含完整 12 项统一形象目录 |

后端验证包含保存重载、改名/改职责且省略 avatar 保留、默认形象、拒绝非法值且无写入，以及旧外观迁移/重复执行/既有任务不变。前端验证覆盖全部本地资产、唯一 ID、可用工位姿态、未知或不安全 ID 不解析、版本和许可。

浏览器验证使用真实页面与正式 API：选择耳机眼镜并保存，列表/详情重载一致；首页工位与右侧工作台同一角色；私聊联系人、群组配置、成员及员工消息同一素材。取消选择不写入；重复员工名称触发 409，仍保留当前形象草稿，放弃后首页保持已保存形象。群聊消息仅通过现有消息写入函数加入隔离夹具，已标记完成，不代表真实 AgentScope 执行。

截图与并置比较位于 `docs/reviews/evidence/nexus-avatar-identity-20261029/`。视觉结论见根目录 `design-qa.md` 追加章节。OpenSpec Scenario 核验见对应 change 的 `verification.md`。

## 剩余边界

- 公司 MySQL 迁移、真实 AgentScope/Worker 执行未验证；本次不改变它们的调度或状态。
- 新人物采用 BotPixel 原生 chibi figure，保留静态人物和平台状态提示，没有伪造敲键盘或工具执行动画。
- 六款机器人继续作为默认选择，人物为可选项。既有客户自定义 Emoji 统一迁移为升级前首页机器人，原 Emoji 不继续显示。
- 构建保留现有大 chunk 与 mime-types/path 提示，未增加新的构建失败；不以此声称全平台或长期运行已验收。

## 后续调整：形象统一列表

按用户反馈，去掉“机器人员工 / 像素同事”的分组和分类标题，全部12款形象在同一个三列网格中选择。删除无用分组变量和样式，不改变保存值或跨页面形象一致性。Web 类型检查/构建通过；浏览器确认只有1个形象列表、12个选项，当前保存形象保持选中。最新截图 `09-unified-avatar-picker.jpg` 取代旧截图中的分类展示；本次不涉及 API 或数据库变更。
