# Codex parallel coordination

把多个开发需求交给独立开发者，同时保留一个用户沟通入口。

这是一个早期的 Codex 协调插件：主会话与用户确认需求，每项获批需求由专属开发者在独立 Git 工作树实现、自测，集成者核对证据、独立审核并在授权内合入 `main`。适合需要并行开发、逐项验收和可追溯交付的项目。单个简单修改通常不需要完整协调流程。

当前 manifest 版本为 `0.1.0`。源码已公开，仍处于早期验证阶段；公开仓库不代表已上架公共插件目录，也不保证每种 Codex 宿主都有完整工具能力。

## 安装与开始使用

需要 **Python 3.12+、Git，以及支持本地 marketplace 的 Codex 宿主**。运行时使用 Python 标准库；实际派发、消息和资源操作还需要获授权的宿主工具。

按官方 marketplace 路线添加本仓库，再在宿主插件界面安装、启用，最后用新会话核实加载：

```shell
codex plugin marketplace add suuny-ab/codex-parallel-coordination --ref main
```

添加来源只完成第一步。完整步骤、真实插件名称、启用方式和故障检查见[安装及上手](docs/getting-started.md)。该命令会登记安装来源，按自己的环境授权执行。

安装并确认加载后，可以在自己的项目主会话中说：

> 使用 isolated-parallel-development-coordination 技能。先梳理一个小需求：给任务列表增加按状态筛选，保留现有默认结果。请说明范围、验收条件和派工摘要，等我批准后再派发。先不要提交、推送、合并或安装。

主会话应先对齐需求及权限；缺少真实派发能力时明确说明受阻。一次小需求可先走完整流程，再增加并行数量。详细步骤及预期见[最小使用示例](docs/getting-started.md#最小使用示例)。

## 怎样分工

| 角色 | 负责什么 |
| --- | --- |
| 用户 | 在原主会话确认需求、授权，回答确需决定或验收的事项 |
| 主会话 | 唯一沟通入口；整理需求、自核规格、呈现派工摘要和用户事项 |
| 开发者 | 每项需求独立工作树、分支和一个 PR；实现、自测，返工沿用原作者和 PR |
| 集成者 | 技术收件、独立审核、核对最新 main，按授权串行合入及保全回收 |

[工作流程图](docs/workflow.md)展示完整路径：需要用户回答的事项进入缓冲池；已核实的阶段与资源进度直接通知主会话展示，不要求回复、不打断当前事项。**合并完成与资源清理完成分别记录。**

## 当前限制

- 空闲自动唤醒尚未实现。没有打包 hooks、后台轮询或 daemon；新通知依赖实际可执行的宿主通路及工作时机。
- 安装文件、宿主加载、真实任务运行、原生关闭及跨项目使用分别核验。包内测试或一次本机成功不证明其它环境可用。
- Git 工作树分离代码；原生 agent 不是操作系统安全沙箱。Windows 受管进程使用 Job Object，不能据此推断原生 agent 关闭、模型容量或 RAM 已释放。
- 资源保全识别仍有[已知问题](CHANGELOG.md#已知问题与待验证)；失败或结果未知保留证据和责任者，不宣称全部收尾。
- 组件提供账本、门禁和有限宿主接入；真实 push、merge、安装及部署由获授权的调用方执行，互不授予权限。

## 继续阅读与参与

| 需要什么 | 入口 |
| --- | --- |
| 安装、启用及第一次使用 | [安装及上手](docs/getting-started.md) |
| 完整流程及通知双通路 | [工作流](docs/workflow.md) |
| 反馈问题、提交改进 | [贡献说明](CONTRIBUTING.md) |
| 已合入的功能与已知限制 | [变更记录](CHANGELOG.md) |
| 本地链接、包内容及测试 | [验证与打包](docs/validation.md) |
| agent 执行时的角色指引 | [SKILL 入口](skills/isolated-parallel-development-coordination/SKILL.md)及[共同规则](skills/isolated-parallel-development-coordination/references/current-rules.md) |
| 开发接口 | [CORE 账本契约](skills/isolated-parallel-development-coordination/references/core-runtime.md)、[RESOURCE 资源 API](docs/resource/API.md)、[标准派工](skills/isolated-parallel-development-coordination/references/standard-dispatch.md) |
| 来源及旧失败轨迹 | [历史索引](skills/isolated-parallel-development-coordination/references/history/index.md) |

采用 [MIT License](LICENSE)，Copyright (c) 2026 suuny-ab。允许使用、修改、商业使用及再分发，包括闭源衍生；须保留版权和许可声明，软件按原样提供。便携包包含相同许可。

历史来源保留于 [baseline](provenance/baseline.json)、[CORE](provenance/core.json)、[RESOURCE](provenance/resource.json)。历史指纹绑定当时文件；历史实验和失败不是通用安装步骤，也不替代当前源码自测及独审。
