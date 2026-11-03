## ADDED Requirements

### Requirement: 任务按分类派发
任务 SHALL 通过 category_id 选择工作分类，调度 SHALL 根据 Agent Worker 配置的分类与任务类型派发，不要求任务包含仓库 ID 或仓库地址，不比较任务仓库与 Worker 仓库。

#### Scenario: 按分类承接
- **WHEN** 有已启用且空闲、支持任务分类与类型的可运行 Worker
- **THEN** 没有仓库字段的任务可以被该 Worker 承接

#### Scenario: 创建任务
- **WHEN** 页面、任务 API 或 Task MCP 创建有效任务
- **THEN** 不加载仓库列表、不要求仓库参数，不提供仓库查询工具

### Requirement: Worker 直接维护 Git 配置
Worker SHALL 直接保存 Git 地址、目标分支及可选加密凭据，不依赖独立仓库台账或仓库 ID。平台 SHALL 不维护仓库登记、绑定、克隆 API。部署和更新 SHALL 使用 Worker 自己的 Git 配置，不校验仓库与任务分类是否匹配。

#### Scenario: 直接保存 Git 配置
- **WHEN** 用户在 Worker 页面填写 Git 地址与分支并保存
- **THEN** 配置直接写入该 Worker 的执行环境，不先创建仓库对象；响应不包含明文凭据

#### Scenario: 全新空库初始化
- **WHEN** 全新版本在空数据库中初始化
- **THEN** 直接创建 Worker Git 配置字段；不创建独立仓库台账和任务仓库字段，不执行旧库迁移、数据回填或兼容处理
