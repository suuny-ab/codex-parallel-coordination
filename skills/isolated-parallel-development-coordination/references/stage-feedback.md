# 主会话阶段与资源回收反馈

用户事项和只读通知共用 canonical ledger，走两条通路。决策、澄清、验收等需要用户回复的事项仍由 `active_human_item` 串行处理；`notifications` 只记录已核实的阶段变化，不是第二个任务队列或用户待办。普通技术收件仍由唯一集成者处理。

主会话实际收到并核实通知后立即展示简短文字，即使已有 active 用户事项、正在讨论需求或等待验收回复，也不等用户空闲、事项结束或追问。展示不切换焦点、不请求回复。可以合并同时已经收到的变化，不为凑批次等待。准备结束时先处理 `display_required`；不能用已有用户事项遮住未展示通知。已展示的相同状态不再作为新进度重复发布；`progress` 是按需查询的全量摘要。

## 形成与恢复

`Runtime` 的技术写入和 closeout 在原写锁内从当前任务、报告、独审、合入及资源事实形成 `runtime.stage_feedback`。只有当前委托集成者或已批准的任务收件者可以转交本范围通知，包含既有 observed/CLI 真实派发绑定；过期派发依据不授予发送权。没有已核技术责任者时保留原账本，不凭主会话身份替代集成者形成阶段结论。

`Runtime.work_cycle(actual_main_id)` 仍不写技术状态，返回同 revision 的 `notifications.items/text`、`display_required` 和 `allow_end`。通知与 `user_item` 并列，二者互不阻塞。有未展示通知时 `allow_end=false`、`all_work_finished=false`；这只是有限工作入口的待展示提示，不是宿主硬门禁。`cycle` 的 closeout 结果也包含相同通知通路，旧 `notify_main` 保留用户事项/批次/例外兼容语义。

阶段包括开发已交付转独审、当前源码独审通过、目标 main 合入、安装及实际验证、重要受阻和解除。作者 `quality_review=passed` 不等于独审；报告到达、worker completed/idle、不完整报告不等于已交付。不同修订的旧通知失效；旧回执不覆盖新状态。同任务、交付修订、实际结果及来源去重，纯工作周期/账本 revision 变化不会重新通知；部分变全部等真实变化产生新通知。

资源摘要只读原 `runtime.resources`、cleanup 和 canonical close 回执，不执行资源操作。分别列开发者上下文、进程/测试服务、工作树和临时数据的已处理及剩余项。待清理、正在回收、部分、全部、失败、未知分别显示，责任仍在集成者；未登记/未观察给未知，缺任一类别的证据不报全部。目录不存在、not_found、idle、code completed 都不证明关闭，不计算 RAM 或名额释放；不要求已关闭开发者回复。执行策略、共享/保护及具体清理授权不变。

沿最新已合 main 消费普通 `release_receipt` 或 ROOT 原生 `close_confirmation`；后者只证明已核当前开发者上下文关闭，不推断平台名额。已保全完成任务的报告沿原 `preserved_read` 读取外部/云端绑定证据，不能让旧工作树成为永久前提。通知不重新执行关闭、目录回收或保全。

最小状态是 `pending`、`sent_unconfirmed`、`displayed`，另保留失效的 `superseded`。它们和发送 attempt 存在同一账本，保留来源修订/证据引用与最小去重记录；通知不修改角色、审批、接受、任务阶段或清理状态。重启后下一次主会话工作周期补齐未展示变化。

## 有限转交与展示确认

集成者从当前入口取 `notifications.items`，在实际发送前登记 intent：

```python
intent = runtime.feedback(dict(op='prepare-send', notification_ids=ids,
                               attempt_id=stable_attempt_id), actual_integrator_id)
# 仅 send_allowed=True 时，经已授权既有宿主通路发送 intent['text'] 给 recipient_id。
# 回放同 attempt 返回 send_allowed=False，不能再发一次。
runtime.feedback(dict(op='record-send', notification_ids=ids,
                     attempt_id=stable_attempt_id, proof=send_result_ref), actual_integrator_id)
```

发送结果文件绑定 `attempt_id`、`notification_ids`、`recipient_id` 以及 `status=accepted/unknown/failed`。三种结果均保持 `sent_unconfirmed`，不能作为用户已看到的证据，也不能换 attempt 盲重发。主会话仍会在工作周期读到它们；缺通路保留待确认，并由当前集成者核对。这里不调用消息工具，不新增会话、轮询或后台服务。

主会话实际发布可见文字后，受信宿主可确认本次展示：

```python
runtime.feedback(dict(op='confirm-display', notification_ids=ids, proof=visible_message_ref),
                 actual_main_id, verify_display=trusted_display_verifier)
```

`trusted_display_verifier(context, proof)` 必须由宿主代码提供，实际核本原主会话已发布可见消息及其内容覆盖全部通知，不能从 JSON 加载代码或凭 actor 字符串自证。context 含 `project_id`、`coordinator_id`、`notification_ids`、待展示 `text`；返回同样字段，另有 `visible=true`、真实 `message_id`、`scope=host_verified`。证据/文本不匹配则拒绝。读报告、生成摘要、发送接受或内部 ack 都不满足该条件。

隔离测试只能返回 `scope=fixture_only`，不能声称真实宿主展示或资源释放。无受信展示能力时继续待确认；CLI 不接受一个 JSON 的 visible 声明冒充宿主回调。主会话读技术账本的限制不变，此确认只记录展示事实。相同确认可以安全重放。

## 已授权安装及真实验证的事实登记

沿现有冻结发布流程执行的 `release_plan` 完成 verify 后可直接形成通知。没有该发布计划的现有安装流程，可由当前集成者仅登记已发生的事实，不补造 tag 或执行安装：

```python
runtime.feedback(dict(op='record-installation', task_id=actual_task_id,
                     proof=verified_installation_ref), actual_integrator_id)
```

证据文件 schema 为 `stage-installation-verification/1`、`status=passed`，绑定 `project_id/task_id/delivery_revision`、作者已测 `source_head`、与 canonical merge.main_sha 相同的 `installed_sha`、`reviewer_id`、`environment`、`scope=host_verified/fixture_only`，并有非空 `backup_evidence`、`installation_evidence`、`real_task_evidence` 文件引用数组。只有已核 main 完成的任务可登记；这是既有真实执行结果的通知依据，不授予安装、合入或其它权限。fixture 通知明确标注隔离夹具。

## 能力边界

这个机制在实际可执行的主会话时机立即显示或补齐已收到变化。空闲时自动产生新 turn/唤醒仍未实现；不加 hook、daemon、后台 MCP、平台私有数据库操作或替代用户聊天。实际宿主的收到、发布及展示分别由受信事实验证；组件回归不替代集成独审、安装或真实任务验证。
