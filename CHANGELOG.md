# 变更记录

按使用者可理解的功能记录已合入变化，不把每个 PR 当作发布版本。当前 manifest 仍为 `0.1.0`，没有因这次整理创建版本、tag 或稳定版发布。链接可追查实际提交和当时验证范围。

## 2026-11-01 已合入功能

本节以 `main@567aaa81514b8628531de91d8433632af8b8e582` 为文档整理基线，以下 PR 已在该提交中。

| 功能 | 对使用者的影响 | 来源 |
| --- | --- | --- |
| 按需求路由 | 指令归属到具体需求；范围内返工沿原作者、树和 PR，新能力另行对齐审批；受限真实终态可登记及收件 | [#13](https://github.com/suuny-ab/codex-parallel-coordination/pull/13) |
| 开发者生命周期 | 一项需求绑定专属执行者；代码完成、取消、保全及回收分开，未知结果保留待处理责任 | [#14](https://github.com/suuny-ab/codex-parallel-coordination/pull/14) |
| 真实交付事实承接 | 对齐 CLI 来源、当前报告、独审及目标 main 的真实事实，避免把退出或旧审核当作完成 | [#15](https://github.com/suuny-ab/codex-parallel-coordination/pull/15) |
| 统一工作入口 | 工作开始/结束用一次 work-cycle 获取同修订摘要和下一责任；主会话只读，集成者有限对账 | [#16](https://github.com/suuny-ab/codex-parallel-coordination/pull/16) |
| 按角色阅读指引 | 共同边界配合主会话、开发者和集成者的阶段指引，旧实验单独保留来源 | [#17](https://github.com/suuny-ab/codex-parallel-coordination/pull/17) |
| 资源闭环 | 正常新任务关联工作树、受管临时环境及进程；完成后核远端 main 并在树外保全，再按归属回收 | [#18](https://github.com/suuny-ab/codex-parallel-coordination/pull/18) |
| 标准派工 | 已批单项规格复用账本、资源准入及有界宿主桥接；迟到结果先留证再核对，不自动重派 | [#19](https://github.com/suuny-ab/codex-parallel-coordination/pull/19) |
| 独立进度反馈 | 需用户回复的事项与无需回复的阶段/资源通知分开；已收通知及时展示，不等当前事项结束 | [#20](https://github.com/suuny-ab/codex-parallel-coordination/pull/20) |

## 本次公开文档与许可整理

- README 增加用途、安装、最小示例、角色及限制入口，补充工作流图、上手和贡献说明。
- 添加 MIT License，署名 `Copyright (c) 2026 suuny-ab`；便携包包含相同 LICENSE、贡献说明及本记录。
- 通用化公开示例，保留历史来源及失败事实。仅修改文档、许可、必要包清单和测试，不改变运行行为、版本或插件身份。

## 已知问题与待验证

- **资源保全识别**：整理基线仍有保全引用识别问题，独立修复任务 `PRESERVATION-REF-FIX` 正在处理；未合入修复不计为本节已实现。本次文档不代修、不预合其它分支，也不因等待该任务阻塞文档交付。后续按实际已合提交和新证据更新状态。
- **标准派工真实使用**：已有新需求的资源准入及创建记录；运行确认与完整交接仍须核最终真实证据，不能从代码合入、发送接受或夹具通过推断。
- **宿主能力**：空闲自动唤醒尚未实现。跨环境/跨项目默认加载、原生关闭/恢复及真实远端原子合并没有由包内夹具普遍证明，见[平台边界](skills/isolated-parallel-development-coordination/references/platform-boundary.md)。
- **历史失败与测试范围**：早期断言、路径和协议兼容失败及复跑按[历史索引](skills/isolated-parallel-development-coordination/references/history/index.md)追查。测试数、文件副本安装、宿主加载及真实资源回收是不同证据，不能互相替代，也不能相加组件测试数冒充组合通过。
