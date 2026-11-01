# 交付收尾与原请求续派

已派发任务由当前集成者使用 `Runtime.continue_delivery`；需要实际宿主工具时使用随包分发的 `scripts/continuation_host.mjs`。只处理指定任务和一个明确获批的原续派请求，不选择队列优先级、不提高配额、不创建新任务账本、不唤醒空闲会话。主会话只承接实际不能下放的最小宿主包。

## 冻结请求与当前证据

请求是一个外部、带 SHA-256 的 JSON 工件。以下字段之外的字段会被拒绝，actor、工具及授权不能放在请求里自授：

```json
{
  "schema": "delivery-continuation/1",
  "project_id": "ACTUAL_PROJECT_ID",
  "task_id": "EXISTING_TASK",
  "request_id": "STABLE_CONTINUATION_ID",
  "approval": {"path": "ABS_ORIGINAL_APPROVAL", "sha256": "ACTUAL_HASH"},
  "preservation_destination": "ABS_NEW_EXTERNAL_DIRECTORY",
  "handoff": {"path": "ABS_HANDOFF", "sha256": "ACTUAL_HASH"},
  "installation_required": false,
  "successor": {"path": "ABS_ORIGINAL_APPROVED_DISPATCH_REQUEST", "sha256": "ACTUAL_HASH"}
}
```

`successor`、安装步骤以及尚不需要的保全字段可省略；声明续派不等于授权续派。每任务只绑定一份请求。恢复不更换 request/attempt/operation，不改原批准。收到报告后固定其修订、作者、工作树、分支、需求和报告哈希；同任务源变化返回冲突，其它任务推进 revision 不被旧快照覆盖。

每次新增的事实以 `inputs` 回接：`observation_ref`、`report_ref` 交原 handoff；`review` 交原 quality；`acceptance_authority` 交原 technical-acceptance；`merge` 指向原 `real_use(action='record-merge')` 完整命令工件；`installation_proof` 交原 record-installation；`preservation` 可采用已核外部保全；`native_observation` 回原关闭操作。真实核证与独立判断仍由原责任者提供，工具不生成审核、CI 通过或用户接受。

```python
result = runtime.continue_delivery(
    request_ref, actual_actor_id, bridge=resource_bridge,
    inputs=current_bound_evidence, max_steps=8, scope='host_verified',
    reclaim_authorized=current_cleanup_authority,
    install_authorized=current_local_update_authority,
    successor_authorized=explicit_original_request_continuation_authority,
    successor_origin=actual_original_user_origin,
    dispatch=existing_standard_dispatch_or_none,
    available_tools=actual_available_tools,
)
```

`bridge`、`dispatch`、可选的 Python `install(context)` 回调是受信代码对象，不从任务 JSON 导入代码、shell 或工具。集成者缺 ROOT 权限时返回 ROOT 包，不冒认主会话。旧冻结授权缺 cleanup grant 时仍保留缺口，由真实主会话沿合法入口完成。

## 已安装的固定宿主驱动

宿主从其已核实的安装 profile 引入一次 `ContinuationHost`，后续只更换绑定请求/证据。`requestMeta` 必须是实际 `nodeRepl.requestMeta`；profile、permissions、工具清单和回调由当前受信宿主分别提供，不展开任务 JSON 作为配置。

```javascript
const {ContinuationHost} = await import(installedContinuationModuleUrl);
const host = new ContinuationHost({
  python: verifiedAbsolutePython, profile: trustedInstalledProfile,
  requestMeta: nodeRepl.requestMeta, availableTools: actualTools,
  permissions: {reclaim: scopedCleanupApproved, install: scopedUpdateApproved,
                successor: originalSuccessorContinuationApproved},
  successorOrigin: actualOriginalApprovalOrigin, scope: 'host_verified',
});
const result = await host.run(boundRequest, {
  inputs: currentBoundEvidence, invoke: actualTrustedToolCallback,
  update: existingApprovedUpdaterOrNull,
});
nodeRepl.write(result);
```

原生关闭只 claim 一次原操作，保留原始 close 返回，再作一次限定 wait。close 超时/结果未知不重发；同一次 run 不反复观察。迟到的实际结果可用 `host.captureNativeResult(boundRequest, nativeRequestRef, {close: actualRawClose, wait: actualRawWait})` 构造固定回接，再作为 `inputs.native_observation` 继续。它不执行关闭、不将 not_found 或 completed 当关闭事实。没有实际工具能力就返回包和下一责任者。

迟到 spawn 只从原 host journal 原子绑定原 actor、generation、attempt 和已准入 permit，不推断 running。错误 request/task/permit/generation、改派、取消或其它已有 actor 均拒绝，原返回仍保全。账本已知有本地许可却无法合法关联当前作者时，关闭和释放前返回 needs_reconcile 并保持占用；不能退化成 not_reserved，也不补造历史手工作者的派发。

驱动复用 `DispatchSession` 执行原 StandardDispatch，保持原请求、原用户来源、准入和 main 核验；没有宿主派发能力则停止。原排队基线需要更新时，使用原 `rebaseQueued` 门禁另行显式核证，不能自动改原批准。已登记成功的后继请求只读复核，不再 spawn/send。许可确认释放后即可接续，目录失败不重新占用许可。

安装仅在当前授权和原批准都允许时进入。`update` 是宿主已有的受信备份/更新流程，须返回现有 `stage-installation-verification/1` 工件；没有 updater 就返回安装包。先保存安装 intent，异常/中断后只接受原操作的核证，禁止盲重装。已合并、安装证据、会话加载未知、agent 关闭、许可释放与目录回收分别表达，安装状态的只读检查继续使用 INSTALL-STATUS。

## 中断、锁与证据

副作用在唯一账本 `runtime.operations` 先记 intent，再执行。native、安装回调及清理回调不持 CORE 写锁；返回重读当前状态。清理重入同一轮可汇总迟到核证，intent/unknown 的删除不重做。保全采用原操作标记和逐文件字节复核接续原目录，标记或副本变化即阻断。

硬退出遗留锁不是重派理由。实际 ROOT 可显式调用：

```python
runtime.recover_call_locks(exact_lock_refs, absolute_external_backup, actual_main_id,
                          request_ref=original_bound_request)
```

固定驱动对应 `host.recoverLocks(originalRequest, exactLockRefs, absoluteExternalBackup)`。只允许本 runtime 的 `ledger.json.lock` 与 `standard-dispatch-driver.lock`，先验证并外部保存原字节，再对比 inode/hash 与实际死亡 PID 后移除。StandardDispatch 必须保留本次实际登记的 driver PID、原请求 digest，且尚无 native attempt/owner。局部保全中断只允许原 continuation 的已登记死亡 copy PID、原标记、当前已完成任务及无活动/未知 native 操作，恢复 CORE 锁后仍逐文件核证。不追补历史 PID 登记。活动 PID（包括复用）、权限拒绝/死亡未知、其它锁、不同请求、活动后代及未核 native 结果均拒绝，绝不自动抢锁。

返回值包括 step、confirmed、next_owner、next_action、reason、task_completed、merge、installation、session_loaded、cleanup 及必要 host_package；同账本阶段事实继续供 work_cycle/stage_feedback 读取，不新增通知或人事项队列。技术处理归集成者；审批和真实用户验收仍走原主会话。

定向用例见 `tests/test_delivery_continuation.py`：临时 Git/文件/真实本地 RESOURCE 与 Node/Python 通路是真执行；native 工具、安装、独审判断为明确夹具。生产任务必须在获批安装后，经安装入口、原作者和最小宿主驱动另验。作者自测、独审和真实宿主效果分别报告，夹具不冒充真实通过。
