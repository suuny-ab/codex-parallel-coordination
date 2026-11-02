# 明确清单原件归档

`Runtime.archive_tool(actual_host_actor, archive_root=..., spec_root=..., authorize=..., observe_stop=...)` 是独立 Python 插件入口。项目和已有 state-root 固定在原 Runtime；身份、固定目录及授权/停止观察器来自受信宿主，不从 JSON、报告或 spec 文本授予权限。此接口与 STATE 一样不是操作系统沙箱。它不会在 merge 或 cleanup 中自动调用。

```python
archive = runtime.archive_tool(
    actual_integrator, archive_root=trusted_archive_root,
    spec_root=trusted_project_materials_root,
    authorize=verify_actual_archive_grant, observe_stop=observe_actual_task_stop)
view = runtime.task_state(actual_integrator).query(task_id)
result = archive.execute(task_id, original_operation_id, view['revision'])
```

调用者必须是该任务指定的独立集成者，且 `authorize(task, actor, plan)` 必须按本次真实宿主授权返回 `True`。返回自称身份/权限的字典无效。`archive_root/spec_root` 仅是受信构造参数，首次意图登记后固定在同一账本 `project.archive_policy`；之后不能换根。不存在账本时不自动初始化。

## 清单与准入

STATE 的 `register` 可选 `deliverables=[{path,sha256}, ...]`，默认 `[]`；它与已确认 `state_contract.spec` 一起保存。同一原主会话及真实 user-origin 规则不变。工具不解析报告路径，也不根据 spec 文本猜额外交付物。清单应在用户确认 spec 时按明确约定登记；既有公开 routing + prepare 路径使用同账本唯一 `task.routing.requirement_id` 的已批准 canonical requirement（`runtime.intake.requirements`），额外交付同样默认空，不再 register 一份任务。两种绑定均缺失的旧任务不会自动迁移，旧 `resource_lifecycle.preserve` 仍是原复制图语义。 已合法结束（completed/cancelled）的绑定任务不能经公开 `routing(spec)` 用同一 scope 替换最终 spec 引用或额外交付清单；相同声明重报保留 STATE 已登记的 `current_spec/location_history`，归档工具的正常定位更新仍走原受限事务。

正常归档只消费 `phase=completed`、与当前 source_version（既有路径为原 mr 版本）相符的已核实 merge；既有 `record_main_inclusion/real_use` 路径只核同记录绑定的最小合入 proof，不重读报告图。不会重新执行测试、独审或 Git 合入。取消消费同记录的真实 `cancellation_request`，还需 `observe_stop(task)` 返回对应 `project_id/task_id/owner_agent_id`、`stopped=True`、`merge_status=not_merged`、`scope=host_verified` 或明确标为 `fixture_only` 的测试观察。未知停止、未解决 merge reservation、实际合入或结果未知都拒绝按未完成取消归档。已经合法合入并核对的任务继续正常归档。

取消默认只归档最后确认 spec；已放弃的额外文件即使缺失也不要求补做。不关闭 PR、不备份代码、不改 agent 运行或许可事实；实际取消阶段及其它资源收尾仍由原生产者负责登记。

## 文件效果与同账本状态

归档根和活动 spec 根必须位于主仓库、所有登记开发工作树及受管清理范围之外，归档根也不能落入任一已登记资源。目标为 `archive_root/task-TASK/spec/原文件名` 及 `deliverables/序号-原文件名`，不能指定任意目标、覆盖已有文件或借新操作编号绕过原操作。

只接受内容 hash 一致的普通独占文件；拒绝链接、junction/reparse point、hardlink、Git 已跟踪文件、跨任务/共享/受保护/客户仍使用的已登记资源。spec 必须在受信资料根内；额外交付物必须在该任务工作树或登记的专属资源内。宿主仍负责不把未登记的共享/客户文件误授为专属资料，文件自身的身份声明不赋权。

`execute(task_id, operation_id, expected_revision)` 先通过 STATE 的固定 `_ARCHIVE` 事务接点登记意图，再在项目写锁外移动。Windows 使用不允许覆盖的文件移动 API（允许跨卷）；非 Windows 采用不覆盖的 link/unlink，同卷可用，跨卷失败交手动接手。内容及源/目的实际位置会在移动后复核。受管资源清理不得把本归档目录纳入范围。

结果 `schema=task-archive/1` 最小包含 `project_id/task_id/operation_id/mode/status/files/completed/pending/protected_directories`；`files` 逐项列 `kind/original/destination/current/status` 和观察事实，定位含 SHA-256；位置冲突时 `current=null`。`admission` 单独标明正常合入核对或取消及停止核实依据和 host/fixture 范围。`status` 区分 `completed/partial/pending/needs_reconcile`。返回的 `state_registration.status` 单独区分 `recorded/conflict/registration_pending`；文件移动完成不等于状态已登记。结果不包含“资源可删除/已释放”的结论，`protected_directories=[]` 也不授予删除权限。

STATE 原子更新 `task.archive`、归档生产者结果、`task.spec_location` 和已移动文件的当前定位及 `location_history`。STATE 注册路径更新 `state_contract.spec/deliverables`；routing 路径更新 canonical requirement 的 `current_spec/deliverables`，冻结 `spec/check` 仍保留原引用及字节身份。存在两种绑定时必须一致并同事务更新；不为旧路径虚构 `state_contract`。旧审核历史不改写，原 spec 旧路径不要求永久存在。不会扫描或归档另一套报告、日志或完整代码树。`query(task_id)` 只读最后登记，不探测文件或重跑历史门禁。

## 中断恢复与手动接手

- 多文件部分移动、移动成功但登记失败：继续同一任务/操作，以当前修订调用 `observe`，核实源/目的后补登记；`execute` 也先观察，仅对源内容一致且目标不存在的未完成项接续移动。
- 目标已有其它内容、源目的同时存在、两边均缺失、内容变化：保留 `needs_reconcile` 和相关源目录（必要时包括归档任务目录），不重复复制、覆盖或删除。换操作号无效。
- 归档 mover 故障：`prepare(task_id, operation_id, revision)` 可先登记同范围原意图；集成者用正常文件工具按返回清单移动原件，再调用 `observe`。`observe` 从不调用 mover，独立核实真实文件效果并登记。权限、共享保护及停止准入仍有效，手动接手不能绕过。
- 项目账本写锁或修订冲突：返回冲突或登记待接续；按返回定位读取原操作并观察，不夺锁、不换 attempt。归档只使用有限操作锁防止同操作并发移动，进程异常留下的锁也不自动抢占，先核实原进程和实际文件位置。

操作意图已持久化，故状态登记故障后可从相同两个位置重建实际观察，不要求另存完整报告图。宿主停止/授权观察和文件操作均不长期占用项目账本写锁。

## 验证范围

[隔离测试](../scripts/test_archive_tool.py)覆盖 A01—A05、取消清单及负向边界，复用 [STATE runner](../../../tests/run_task_state.py) 的重型互斥入口：

```text
ABSOLUTE_PYTHON -X utf8 -B tests/run_task_state.py --output ABSOLUTE_EXTERNAL_OUTPUT --temp-root NEW_SHORT_OWNED_TEMP --batch archive-r1 test_archive_tool test_task_state
```

真实临时 Git、文件移动及进程中断与宿主角色/取消/独审观察夹具分别计证；本项测试不证明实际原生 agent 关闭、许可释放、远端 main 合入或 RESOURCE 最终闭环。实现入 Git 不表示已安装到当前会话。
