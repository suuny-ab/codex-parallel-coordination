# 统一任务状态与反馈入口

`Runtime.task_state(actual_host_actor, **trusted_callbacks)` 返回绑定已有 Runtime 的 `TaskState`。它复用该项目的唯一 `ledger.json`、修订、任务字段、用户事项及 stage_feedback 存储；构造失败不会初始化替代账本。项目根/state-root 来自受信安装上下文，不能以开发者当前工作树重新派生项目。此接口是现有 Python 插件接线，不新增服务或安装步骤。

身份、观察回调和真实用户来源由宿主侧提供。调用命令不能包含 actor、role、project_id、state_root、producer 或授权来源对象；文件和报告不赋权。与现有 HostAdapter 一样，这不是操作系统沙箱，不能让不受信 JSON 决定构造参数。

## 调用

```python
# runtime 固定在原项目；actual_actor 来自当前宿主，不取自任务文件。
state_tool = runtime.task_state(actual_actor)
view = state_tool.query(task_id)
observed = state_tool.observe(task_id)  # 显式 Git/源码观察，发生在账本写锁外。
result = state_tool.record(dict(
    action='self-test', task_id=task_id, event_id=task_id+':self-test:r1',
    expected_revision=view['revision'], version=observed['version'],
    status='passed', command=actual_test_command, evidence=actual_evidence_refs))
```

`query(task_id=None)`、`pending()` 只返回职责内最后登记，不读取完整测试报告，不执行宿主刷新；`freshness=last_registered`、`queried_at` 与任务 `last_registered_at`/`source_observed_at` 区分读取与实际观察。缺资源/归档事实保持未登记，不根据目录、idle 或阶段推断成功。

`observe()` 默认核实际任务分支、HEAD、固定项目的本地 main 和完整源码清单 digest；main 必须是 HEAD 的祖先。远端 main 及测试真实性仍由交付者核实。受信宿主可提供 `observe_source(task)`，返回 `head_sha/target_sha/source_digest`；报告里同名字段不能替代回调。默认 Git 观察在 `deliver-pr/review/technical-acceptance/user-acceptance/merge-verified` 晋升处要求没有已暂存、未暂存或未跟踪源码，并在源码清单读取前后核 Git 状态及 HEAD；普通进度和自测仍可记录开发中的工作树。提交后 HEAD 改变，须按新版本重新自测，不能把提交前的脏摘要直接交付。被 Git 忽略且未纳入源码清单的运行产物不阻止晋升。自定义受信宿主观察器同样负责已提交源码的真实性，不得把夹具或脏字节冒充提交版本。版本结果和 evidence 引用只登记本次结论及关联，不递归审批历史材料。测试与审核不会由状态工具自动执行。

## 职责及动作

所有写命令包含 `action/task_id/event_id/expected_revision`，版本相关动作另带 `version`。未知字段拒绝。新任务接受 `delivery_verified` 或 `user_required`，不能在后续事件中降级。

| 调用者 | 动作与必要业务字段 |
| --- | --- |
| 原主会话 | `register`: spec(path/hash)、scope、acceptance、acceptance_type、integrator_id、rights(bool字典)、user_reference、可选 deliverables(默认空的明确文件清单)；实际作者/工作树/分支均保持 null，直到生产者取得资源 |
| 原主会话 | `authorize`: rights、user_reference；`cancel`: reason、user_reference；`user-acceptance`: version、status(accepted/rejected)、user_reference |
| 原主会话 | `user-question`: title、prompt、options；只有需用户回复的事项进入原 items 缓冲池 |
| 原作者 | `progress/problem`: summary；`self-test`: version、status(passed/failed/not_run)、command、evidence；`deliver-pr`: version、pr(provider/id/url)、summary、evidence |
| 指定集成者 | `review/technical-acceptance`: version、status(passed/failed)、summary、evidence；`rework`: version、summary |
| 本任务职责者 | `source-changed`: version、summary，清空旧版适用结果，交原作者回归及原 PR 复审 |
| 指定集成者 | `merge-verified`: version、原 operation_id；还需宿主的实际合入观察及现有 commit/push/mr/merge 授权、当前审核/验收。此动作只登记，不执行 merge |

register/authorize/cancel/user-acceptance 另需可信调用参数 `user_origin={role:'user', conversation_id:原主会话, reference:本次user_reference}`，不能在命令或自签文件里提供。技术交接面向指定集成者，返工面向原作者，保留任务/作者/PR。源码或 main 改变须重新自测交付；审核失败也须作者重新交付，不能直接覆盖成通过。

`cancel` 只登记真实取消请求并阻止新开发结果，不冒充作者已停止或资源已删除；实际取消/停止/回收仍由原生产工具及后续 RESOURCE-FLOW 负责。`merge-verified` 的 `observe_merge(task, operation_id)` 由受信宿主提供项目/任务/原操作、source_head、previous_main、main_head、contains_source、scope(host_verified/fixture_only)。代码 completed、archive、逐项资源及通知独立显示，接口不会因此获得合入或回收权限。

## 并发及反馈接续

修订过期或已有写锁返回 `conflict`，保留当前修订和接续说明，不抢锁。相同 event_id/业务输入重放返回原结果；改变输入拒绝。冲突后可读取当前记录并以同一逻辑事件更新 expected_revision，其它业务输入保持一致。未知外部效果先核实际对象，不换编号重做。

技术交接和无需回复的通知复用同一 feedback 存储，由 `pending()` 分别返回 technical、notifications、human_items；没有第二任务队列，也没有自动唤醒或轮询。

1. 实际发送者登记 `send-intent(notification_id, attempt_id)`；仅 `send_allowed=true` 才执行一次获权消息发送。重复原意图不再放行。
2. 发送者登记 `send-result(notification_id, attempt_id, status, evidence)`。accepted/failed/unknown 均不代表用户已看到；只有已核实未发送的 not_sent 才允许另次发送。业务结果不因通知失败回退。
3. 实际收件者登记 `notification-received(notification_id)`；技术收件即离开该技术待办，主会话已收到但未展示仍保持待展示。
4. 主会话的 `notification-displayed(notification_id)` 需要宿主 `verify_display(event)` 返回对应 notification_id/recipient_id、visible=true 和实际 message_id。文本或发送接受不能证明展示。已展示同事项不重复提醒，新版本变化可产生新通知。

合入/展示的实际观察复用现有 callback-results 留存；保存冲突或失败返回 actual_result 与恢复定位，重启复用原观察，只补登记。宿主/Git/消息/测试回调都不在账本写锁内运行。

## 生产者接入及范围

`record_producer_result(state, capability, task_id, operation_id, result)` 是生产者已有事务内的稳定登记接点。能力为内部对象 `_DISPATCH/_RESOURCE/_ARCHIVE`，不接受命令里的角色字符串；实际工具负责自身准入、操作、观察和结果核实。登记不重复调用业务工具，不代做第二次业务审批。

当前 CORE `_save` 接入已发生的 launch/bind、资源登记/创建/回收对账结果。普通 `Runtime.launch` 的 handoff 同时核原 attempt、许可、路由、实际 callback-result、作者和 generation，明确标识 core_launch 来源；不补造 StandardDispatch 或 observed_dispatch。旧的标准派发、外部观察和报告收件检查继续保留。

独立 [ARCHIVE-TOOL 原件移动](archive-tool.md)通过本接口的固定 `_ARCHIVE` 事务接点登记意图、结果和当前文件定位；原件移动不沿旧报告图。RESOURCE-FLOW 的集中资源创建及关闭/文件回收准入拆分仍待后续任务。本项没有改变那些实际准入，没有迁移旧任务，不声明34项或真实完整闭环通过。当前自测、指定集成者独审、用户验收、实际合入和关闭分别记结论。

验证命令见 [STATE runner](../../../tests/run_task_state.py)，覆盖 [11项主责场景](../../../docs/acceptance/tool-workflow-checklist.md)。Windows 使用 Python 3.12+ 的 `-X utf8 -B`，`--temp-root` 选新建、短路径的专属临时目录，`--output` 保存本轮源码绑定及失败/复测。纯 fixture 不证明真实宿主关闭或远端合入。
