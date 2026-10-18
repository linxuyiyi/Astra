## ADDED Requirements

### Requirement: 顶部主导航只有五个直达入口

顶部主导航 SHALL 固定显示数字员工、任务大厅、数字图书馆、API 网关、配置中心五个入口，点击后直接进入对应页面；MUST NOT 使用下拉菜单、悬浮子菜单或二级导航弹层。

#### Scenario: 五个入口直接切换
- **WHEN** 用户依次点击顶部五个入口
- **THEN** 每次直接进入对应页面，不出现展开式子菜单

#### Scenario: 当前入口高亮
- **WHEN** 用户位于 `/chat`、`/tasks`、`/library`、`/gateway/*` 或 `/settings/*`
- **THEN** 对应顶部入口显示选中状态，其它入口不显示选中状态

### Requirement: 路由刷新与历史导航保持稳定

五个主入口 SHALL 使用可刷新、可前进后退的浏览器路由；根路径 MUST 进入数字员工统一聊天页。

#### Scenario: 根路径与刷新
- **WHEN** 用户访问 `/` 或在任一主入口刷新页面
- **THEN** `/` 显示统一聊天页，刷新后仍停留在当前路由并保持选中状态

#### Scenario: 浏览器前进后退
- **WHEN** 用户在多个主入口之间前进或后退
- **THEN** 页面与顶部导航高亮跟随历史记录更新

### Requirement: 旧路径映射到新位置

旧入口 SHALL 保留可达性，并映射到具有相同定位上下文的新位置。

#### Scenario: 旧数字员工管理入口
- **WHEN** 用户访问 `/employees`
- **THEN** 进入配置中心的员工管理，而不是统一聊天页

#### Scenario: 旧团队入口
- **WHEN** 用户访问 `/team` 或 `/team?channel=<id>`
- **THEN** 进入统一聊天页的群组视图；带群组 ID 时定位该群组

#### Scenario: 旧 Worker 与待处理入口
- **WHEN** 用户访问 `/workers`、`/workers/:id` 或 `/interventions`
- **THEN** 分别进入配置中心的 Worker 列表、Worker 详情或任务大厅的待处理视图
