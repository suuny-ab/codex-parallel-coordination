# 需求收口与受管路由

入口是 `Runtime.routing(command, actor_id, user_origin=...)`，与任务共用唯一项目账本、写锁及 revision。记录位于 `runtime.intake`，不复制任务阶段。`routing_status()` / CLI `routing-status` 同时读取审批、指令、目标版本及 canonical owner/worktree/branch/PR/worker_run。每条命令要有稳定 `event_id`；相同事件幂等，冲突拒绝，业务拒绝保存原命令与原因。CLI `routing INPUT --actor MAIN` 拒绝时退出 1。

`actor_id` 和 `user_origin` 必须来自受信宿主上下文；JSON 和文件哈希仅绑定内容，不认证发言身份。worker 无权调用主会话入口。CLI 不从输入 JSON 获取 `user_origin`；审批和权限更新由已验证用户来源的宿主 Python 调用完成。未经插件接管的直接工具调用、低层离线 `coordination.py` 及任意文件写入不属于拦截证明。

## 新需求的有限步骤

1. `spec`：主会话记录 `requirement_id/name/scope/acceptance`、`discussion/spec/check/start_reference`。后三类证据使用绝对 `path/sha256` 引用。讨论 JSON 有 `confirmed=true`；核对 JSON 包含 `coordinator_id/requirement_id/spec/discussion/result=consistent`。由主会话对照业务讨论判断一致，工具不理解任意自然语言，也不让集成代做业务核对。spec 校验不改变功能验收、自测或 Git/部署权限。
2. `present`：传 `plan_id/version/items/presentation`。每个 item 有 `task_id/requirement_id/name/scope/acceptance/scope_digest/split_reason/assignment/executor_id/assignment_reason`；`scope_digest` 调用 `demand_routing.scope_digest(record)`。`assignment=new` 时 executor 留 null；`reuse` 只接受原未结束需求的实际责任者。呈现 JSON 正好为 `{conversation_id: MAIN, plan_id, version, items}`，作为在原主会话展示的依据，items 数量即 spec 数量。
3. `approve`：传 `plan_id/reference`，用户原文依据 JSON 含 `plan_id/presentation/task_ids/decision/quote`，decision 为 approved 或 rejected。宿主提供 `user_origin={role:'user', conversation_id: MAIN, reference: SAME_REF}`。按单项 item 内容绑定审批，可只批准部分任务；另一项不被阻塞或借权。新目标、拆合或派发安排改变，重新呈现并审批受影响项。纯技术 spec 变化重新自核对，摘要不变可保留审批。
4. `prepare` 登记任务和原 scope/acceptance；存在计划时自动绑定 requirement。只排队不代表派发；可以在审批前排队。没有计划的历史 code 记录可恢复，但不能因此跳过新 `launch` 门禁。已有 code 记录可在审批及 scope 一致后用 `routing(op='adopt', task_id=...)` 关联需求，这不证明发生过执行。
5. `bind_executor(..., task_id=TASK)` 绑定已经真实取得的新身份；主会话调用，核审批、唯一 task/agent/route，保留原 worktree/branch。新身份尚未取得时用 spawn 路由，不能预填假 ID。原需求返工继续原 owner；其它需求即使已结束，也不能把其 developer 当长期模块角色复用。
6. `instruction` 记录 `instruction_id/source/action/candidates/reason/changes`。source JSON 保存原始 `text`；候选都是已登记任务。模型先分解一条消息中的独立要求，明确引用/PR/当前讨论能唯一归属时只传一个候选；多个候选为 held。action 为 query/discuss 时主会话处理，new 为另立需求；implement/adjust/defect/authorize 才可能影响唯一任务。验收后 adjust/new 另立需求，defect 才能在原需求返工，已结束任务都另立需求。
7. `route` 传 `route_id/instruction_id/task_id/actions`。actions 必须恰好一个 `{kind:'spawn'|'send', task_id, executor_id, message}`；不接收批量、暂停、关闭、中断或优先级操作。主会话给出与唯一需求相符的消息，工具不宣称能证明其自然语言内容正确。
8. RESOURCE 取得准入后调用 `launch(..., route_id=ROUTE)`，与 route 的目标、消息、spawn/executor 必须一致。当前 spec、自核对、用户摘要审批、开工依据、原目标及权限/owner/route 版本在调用宿主前重新核验。重复 attempt 返回原结果，未知结果沿旧准入对账，不换 ID 盲发。资源不足沿原排队，不借用其它需求开发者。

每次 `spec` 的核对绑定新文件指纹。验收前范围改变还需新摘要审批，再 `sync-spec` 同步 canonical requirements；验收后冻结 scope。`apply-instruction` 仅合并该指令明确列出的 `conditions` 项；`rights` 必须是 authorize 指令及绑定真实用户来源的 `{task_id, rights}`，仅更新明确 Git 权限，部署仍走已有独立授权入口。`bind-pr(task_id,mr)` 复用既有 MR 元数据门禁；一个需求维护原 PR，URL/ID 不得改成另一个 PR，也不能跨任务复用。MR 元数据不声明已验收或合入。

## 跨任务技术依赖

开发者只能为自己任务调用 `dependency`，提供 `dependency_id/source_task/target_task/contract/actual_change/required_action/evidence`。集成者调用 `verify-dependency`，证据 JSON 精确绑定五项依赖内容，并含 `verifier/basis/disposition`；basis 是实际技术证据引用，disposition 为 within_scope、needs_user 或 unsubstantiated。请求和核实间目标版本变化必须重新请求/核实。

within_scope 只把 dependency ID 加入目标任务 `routing.dependency_todos`，不发送、不打断、不改变优先级/运行阶段；新增目标或改用户优先级归主会话确认。第三任务不继承该证明，须独立请求并核实下一段依赖。集成凭证只能证明所登记的技术判断及证据绑定，工具不自动证明自然语言技术依赖正确。

## 登记已经发生的原生派发

真实项目反馈：主控先登记 pending task，再在受管入口外取得真实 native ID 时，不能伪造一次 RESOURCE 准入或 `launch`。使用下面的有限登记入口；它无任何宿主回调，也不修改主/集成角色的生命周期或释放资源。

```python
status = runtime.routing_status()
proof = {
    'project_id': runtime.project_id, 'task_id': task_id,
    'based_on_revision': status['revision'],
    'target_version': status['target_versions'][task_id],
    'worktree': status['tasks'][task_id]['worktree'],
    'branch': status['tasks'][task_id]['branch'],
    'executor_id': actual_native_id, 'host_id': actual_host_id,
    'observer_id': actual_caller,
    'approval': approval_ref, 'dispatch_result': original_dispatch_result_ref,
    'observation': original_read_thread_ref,
    'spec_at_dispatch': approved_original_spec_ref,
    'checklist_at_dispatch': approved_original_checklist_ref,
}
# Save proof as immutable JSON, obtain evidence.ref(path), then:
result = runtime.bind_observed_dispatch(task_id, proof_ref, actual_caller)
```

该适配器仅接受 `approved-dispatch-plan/1`：approval=approved、source 为 direct user message in original main conversation、main_conversation 是 canonical 主会话，tasks[].id 含目标，integration_agent 是实际审核者。caller 只可为该主会话或该批准的 integration_agent；observer_id 绑定实际取证者。登记任务的 requirements.user_reference 必须引用同一批准文件路径。

`dispatch_result` 为原始批次 JSON，authority_file 引用同批准文件，dispatch_attempts 中本任务恰好一个 fulfilled/value，含 task_id/worktree/branch/agent_id/spec/checklist。两份 dispatch 时文档的 basename/hash 必须对应 approval.specs；不得拿开发后修改的文档冒充原批准快照。`observation` 保存实际 read_thread 原始 JSON（也支持 MCP content 包装），thread.id 必须对应实际 agent_id，且状态 active/running/inProgress 或当前 turn inProgress。接受/idle/completed 不冒认为 running，拒绝后保留原账本。内容哈希仍不替代受信宿主的真实来源认证。

成功仅将已排队、owner=null 的任务与真实身份、运行观察和审核者依据关联。`runtime.observed_dispatches` 记录证明，runtime.operations 不补造准入/launch/close。完全相同证明幂等；改身份、其它任务/控制角色占用、旧 revision/版本、错误 worktree/branch/审批/观察均拒绝。后续真实交付仍经 canonical receive/drain；此入口不给完成、合并、回收或全局安装授权。既有 delivery 委托仍由主会话按独立权限维护。
