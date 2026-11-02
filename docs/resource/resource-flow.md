# RESOURCE-FLOW 资源入口

本候选把创建和归属登记集中到 ResourceBridge，提供独立作者关闭、插件许可核对、停止后的取消及逐项文件回收。继续使用 Runtime 的原任务记录与 Controller 的原资源请求，不增加任务队列、后台轮询、自动唤醒或替代聊天。

已从实际 main 合并提交 `9b5ea1b08db4a8bcda2dac14919450166e521631` 同步 ARCHIVE-TOOL。`archive_protection` 消费该工具经 STATE 登记的最小结果、原操作及当前任务绑定，保护固定归档根和未完成文件涉及目录；不再次执行归档或读取报告图。未开始归档时按最后确认清单保护原位置。当前候选的 34 项隔离场景结果、head、失败与复跑以原 PR 和作者报告为准；这些结果不表示本轮作者真实回收通过。旧 `preserve_task/reclaim_development` 保留原门禁，不自动迁移历史任务。

## 身份、创建与观察

身份和宿主回调来自受信入口，不能从命令、报告、JSON 角色字段或路径取得权限。原 spec、任务、分支、工作树、作者及 PR 持续绑定。STATE 已登记而未分配资源的任务由标准派发沿同一记录规划；StandardDispatch 调用 Bridge 创建与观察工作树，不另造 Git 资源。

`bridge.create_worktree(task, operation, authority, actor, authorized=True, base=observed_main)` 先记原意图，在账本锁外调用 Git，登记实际目录及 Git 身份。创建后登记失败保留 callback result；`runtime.observe_resource_creation` 或 `bridge.observe_worktree_creation` 只核原结果，不重新创建。错误带真实作者身份时仍保留作者；未知结果保持原请求。

`create_temporary` 支持 environment/database/artifact；`run_process` 和 `observe_process` 分别创建与观察原请求。开发者只可申请原批准任务的 `temporary_resources` 明确列出的 environment/database/artifact/process；进程 cwd 限本树或已登记的本任务环境。未记录类型授权时拒绝，不从报告文字推断权限。该能力不放开 Runtime._actor，不授予清理、其它任务、共享资源、子 agent、模型档位或资源政策写权。

进程沿用既有 CPU/内存准入和 OS Job。Controller 在 resume 前登记 Job/PID；CORE 不在等待进程时持锁，观察可承接原 Job。短暂账本争用保留实际结果和登记待办，不重启进程。重型测试使用原 `tools/heavy.py` 互斥；CLI 父进程的独立 OS Job 不占该测试队列。

## 独立关闭与许可

```python
result = bridge.close_developer(task_id, close_operation, actual_host_adapter, authorized=True)
# 仅 ROOT 有正式 native 工具时，集成者保存最小 intent，ROOT 领取并调用：
intent = runtime.prepare_native_close(task_id, close_operation, None,
                                      actual_integrator, authorized=True, scope="host_verified")
# 最后由原集成者 reconcile 同一 operation；不能把领取当作实际执行。
```

省略 preservation 的新入口消费已核实合入，或主会话取消与实际停止结论；核作者、generation、路由、无待办/活动后代、非共享/保护及环境依赖。文件归档失败和目录缺失不成为关闭前提。显式传 preservation 的旧入口继续执行原文件门禁。

idle、完成消息、请求被接受和单独 not_found 不释放许可。native 继续要求实际 close 及后续宿主确认。Controller 只在 canonical release receipt 核实后释放插件许可；独立 agent guard 不声称文件已归档。关闭、插件许可与目录结果分别登记；剩余 granted 许可保持整体 pending。未绑定的 granted 后端先观察，不能按从未创建作者释放。

`ManagedCLIExitAdapter` 由受信管理者提供原 Job 的 ACTIVE_PROCESS_ZERO、原 PID 创建身份和该 thread 的终态日志。它只核独立进程树退出，不调用 native 工具，不把 CLI 写成 App/native 关闭；原生总名额是否可用继续单独核实。

## 取消与逐项回收

主会话先经 STATE 记录 cancellation_request。集成者用 `runtime.confirm_cancellation`，提供只观察原作者停止和原 PR/main 的回调。取消决定不等于停止；已有串行 merge reservation 先核实。main 未知继续同一 operation 观察，实际已合入转正常收尾，不回退 main。没有作者或 PR 时不补造。

停止后 `runtime.close_stopped_pr` 只处理原 PR，不要求保存已放弃代码或完整文件图；未知 close 先观察。合法关闭后用 `bridge.reclaim_resources(task, operation, actor, authorized=True, scope=..., cleanup_remote_branch=...)`，只消费登记资源与真实归档结果，不扫描报告或整机。专属进程树退出确认后才处理文件；main、固定归档位置、共享/保护项、其它使用者、换身份对象和待归档目录保留。

取消只默认保留归档工具要求的最后 spec；任务代码及专属额外输出按原授权丢弃，缺失输出不补做。Git 只处理原树和任务分支，取消可丢弃脏代码，仍拒绝锁定或含链接的树；远端删除使用原 ref 与已观察 head 的 lease，保护已改变的分支。没有取消备份提交、备份分支或代码包。尚未启动的原 CPU/agent 队列项可核实取消，不生成 PID 或作者关闭事实。

每项成功、保留、失败和未知独立登记；成功不撤销，未知不重做。实际删除后登记失败从原 callback capture 接续。工具故障可沿原授权使用正常文件/Git/宿主通路，再观察登记实际结果。通知由 STATE 单独接续，不重复业务操作。

## 已失败 native 转获权 CLI

`bridge.bind_cli(command, actual_integrator, user_origin=..., observe_cli=...)` 要求原任务无作者、native 明确未创建、实际追加用户后端授权、原 Job/PID/thread 观察及当前 Controller 准入。未知、错误带真实身份、已有作者或其它任务作者均不能走此路径。原 native/request/tree/PR 保留，只新增实际观察的 `codex_exec` 操作及作者；channel=codex_exec、origin=independent_codex_process。

原批次批准和后续开工批准分别核对；后续依据须绑定同一 batch、任务、主会话及当前开工来源。只要求明确约定的 checklist，原 task spec 仍核批准字节。文件自称身份不代替 user_origin 或真实宿主观察。

## 验证边界

沿[验证说明](../validation.md)使用原互斥 runner。`test_resource_flow_capture` 只写原任务 ledger/Controller 的隔离副本；原任务、原失败与真实 launch 原件不动。捕获只读取指定输入并核原 Job/PID 和 CLI thread，stdout 路径映射单独声明；重放中的集成调用者是角色夹具，不冒称实际集成者调用。

| 场景 | 主要用例 | 证明边界 |
| --- | --- | --- |
| D01-D02 | STATE 原任务派发、dispatch 幂等 | 临时 Git 实际效果；宿主夹具 |
| D03-D05 | dispatch/closeout_recovery/late、原失败转 CLI 副本 | 原意图与返回身份保留；不证明 native 名额释放 |
| D06-D07 | 临时环境/SQLite/Job、只观察、未取得作者许可释放 | 实际文件、进程及本地许可 |
| R01-R03 | 独立关闭、ROOT 包、unknown、目录失败 | 宿主关闭为夹具；真实归档部分成功后的独立关闭及接续另有联测 |
| R04-R05 | 专属 Job 停止、共享/身份/待归档保护 | 实际受管进程及临时文件 |
| R06-R07 | 登记失败重放、成功不重复、临时资源越权拒绝 | 隔离账本与实际文件 |
| C01-C03 | 未取得资源取消、脏树/本地模拟远端/输出丢弃 | 临时 Git/文件实际；PR 和停止结论夹具 |
| C04 | 原 merge reservation、未知再观察、已合入保护 | 同操作接续；main 结论夹具 |

同一候选的联测包含 STATE 的 S01-S08/F01-F03、ARCHIVE 的 A01-A05 与本项 18 项；34 个场景分别关联实际运行用例，交付报告按隔离宿主夹具、真实临时 Git/文件/进程及未执行的生产闭环区分。本轮作者实际关闭、原 spec 移动、云端任务分支回收及安装发布均不属于这些隔离用例的通过结论。
