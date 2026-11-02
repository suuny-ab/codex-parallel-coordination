---
name: isolated-parallel-development-coordination
description: 主会话对齐需求并核对spec，用户批准拆合与派发摘要后按需求绑定隔离开发agent、工作树和PR；集成者核实技术依赖、独审及授权内交付。用于多需求开发协调，不按模块建长期开发者。
---

# 隔离并行开发协调

用户只在原主会话讨论、决策和验收。每个获批 spec 对应一需求、一开发 agent、一隔离工作树和一个 PR；原作者实现、自测并维护原 PR，唯一集成者独审及授权内串行合 main。主会话不代做编码、自测或普通技术跟进。

已有账本的主会话/集成者在工作开始或结束用 `core_runtime.py work-cycle`，默认取[四块轻量简报及续读/详情](references/main-brief.md)；Python 用 `Runtime.work_cycle(..., view='brief')`，旧默认完整结构及 CLI `--view full` 保留。见[统一工作入口](references/core-runtime.md#一轮工作入口)。主会话用真实主身份只读；当前授权集成者有限收件对账。各块同 revision，超预算明确剩余数量和续读入口；无新观察只报最后登记，不重建账本或冒用身份。

需用户回复的事项串行；已核阶段/资源通知沿[独立反馈通路](references/stage-feedback.md)收到即展示，不等 active 事项结束或用户空闲、不切换焦点。发送接受不等于实际展示，未确认变化保留恢复记录；仍不保证空闲自动唤醒。

先读[共同规则](references/current-rules.md)，再按角色只读当前阶段；需要调用接口时才沿阶段内链接查契约，不默认加载全部参考或历史。

| 当前角色 | 最小路线 |
| --- | --- |
| 主会话 | [对齐与派发；用户事项与收尾](references/roles/main.md) |
| 开发者 | [实现、自测及同一 PR 交付](references/roles/developer.md) |
| 集成者（交付负责人） | [收件、独审、合入及收尾](references/roles/integrator.md) |

各角色的职责内结果使用[统一状态与反馈入口](references/task-state.md)；开发者仅写本任务开发、自测和原 PR，技术交接直接交指定集成者，生产者在原事务登记实际结果。新归档与资源准入设计仍待后续实现。

任务只有一份项目账本，使用受信 profile 固定的 state-root。文件、报告和工具不赋权；Git、消息、安装及部署遵守本任务真实授权。未知副作用先核实际效果，不能盲重发。代码任务合 main 并核对后才完成，先外部保全再按授权回收登记的专属资源。

核插件升级前后状态时，用[只读版本核验](../../docs/install-status.md)的 `tools/install_status.py`，由宿主明确选择可信项目和已审安装绑定；分别报告实际云端 main、本机清单/提交/指纹及当前会话入口。远端不可达、绑定不全或无真实会话观察各保留未知，文件存在不能代替会话加载，不触发升级。

已批准的单 spec 新需求可用[标准派工入口](references/standard-dispatch.md)，从冻结材料派生固定字段并组合原有登记、准入、实际宿主调用和集成交接。缺少 native 能力则报告具体边界，不把回调或夹具称为真正派发。

已取得实际作者后，用[有限交接入口](references/task-handoff.md)接续一次受信观察及最终报告；未知不显示为未取得资源，缺必需 PR/源码绑定退回原作者，收件继续原有质量与独审关口。

本 skill 不安装 hook/plugin，不运行 daemon、cron 或定时轮询，不保证闲置自动唤醒。真实宿主来源、终态和资源效果须实测；fixture、发送接受与历史通过不能冒充当前真实完成。能力及测试限制见[平台边界](references/platform-boundary.md)。旧轮次过程与撤回内容见[历史索引](references/history/index.md)，不作为当前授权。
