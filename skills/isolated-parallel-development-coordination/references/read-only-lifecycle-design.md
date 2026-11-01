# 只读任务合法完成入口

用户已确认：没有代码改动的排查、审阅、探索由交付者核验成果后结束；明确要求用户验收的仍等用户通过。代码任务继续合 main 并核对后完成。完成与资源清理分开。

`task_kind` 为 `code` 或 `read_only`。旧数据缺省保守按 code；不能将代码任务改成只读绕过 Git 门槛。新只读登记必须明确 `acceptance_type=delivery_verified|user_required`，随确认需求和 user_reference 绑定，确认时冻结被审源码 snapshot。报告和证据写在被审输入树外；只读输入冻结前后不变，不按文件后缀猜测有没有改代码。

同任务重新确认或重试仍校验并沿用首次 source_baseline，不允许通过重抓基线把已发生的源码改动洗成只读。

`Runtime.complete_read_only(proof_ref, delivery_actor)` 和 `complete-read-only` CLI 是新的公开入口，不制造 MR/CI。核当前非作者交付者及任务委托、原作者收件来源、当前报告修订和 hash、源码与初始 snapshot、确认授权和验收类型、通过的核验依据。user_required 还核当前修订真正由主会话记录的用户接受。重复同一已核结果幂等；旧修订、变更证据、被撤授权、kind/mode 调包、源变更、作者自验均拒绝。worker completed 报告本身不等于任务完成。

只读完成写同一账本的 completion 记录及 phase=completed，不造 merge.verified。code 的 record-main/MR/CI/授权/验收/集成独审门禁不变。只读不要求用户的交付核验不进入用户验收队列，进度显示待交付核验；需用户验收仍沿用原单项人事项接续。

close/cleanup/release 验证统一引用此 canonical completion。只读保全清单绑定 `completion_verification`；代码继续绑定 `merge_verification`。保全、原owner/generation、没有运行/后代/未完动作、共享/受保护/实际依赖、未知结果不重试等原保护不变。源码已被确认清理时，仅允许同一账本明确成功的归属资源及外部保全支持后续核验，不能将文件消失当清理成功。

RESOURCE 瞬态 flags：task_kind=code|read_only；task_completion_verified；task_completed_on_main 仅 code 为真；read_only_completion_verified 仅 read_only 为真。其它共同安全 flags 沿用。RESOURCE 不另记任务阶段。缺失新字段的旧 flags 只允许保守 code+原 main 门槛。

验证包括完整公开只读完成→保全→夹具 canonical close→真正临时文件清理/许可核验，双验收类型、源/报告/依据/授权/kind/修订变更、重复与失败恢复、代码与旧数据不降级。native/main 为明确夹具，本轮不新建或关闭真实 actor，不跑模型 turn，不提交推送或改全局。

## 接口及可复跑示例

`prepare` JSON 必须含 `task_kind: read_only`、`acceptance_type: delivery_verified|user_required`。确认时 `requirements` 保存这两个字段、user_reference 和 `source_baseline`。作者的报告也必须含相同 kind/mode，source 与冻结输入完全相同；成果文档、self_test/quality_review 和证据沿用原报告格式，放输入树外。只读-only 的交付委托可声明 `required_ci_jobs: []`；只要包含 code 任务仍要求原非空 CI 策略，不能用只读完成走 MR/main 入口。

交付者核验 JSON（下面每个 REF 均为实际绝对文件路径与 SHA 的引用，不是裸文本授权）由当前权威状态取值：

```python
proof = dict(project_id=state['project']['id'], task_id=key,
    based_on_revision=state['revision'], task_kind='read_only',
    acceptance_type=task['acceptance_type'], owner_agent_id=task['owner_agent_id'],
    reviewer_id=actual_delivery_id, delivery_revision=task['delivery_revision'],
    report=task['delivery'], source_digest=report['source']['digest'],
    source_baseline_digest=task['requirements']['source_baseline']['digest'],
    requirements_reference=task['requirements']['user_reference'],
    authority=state['delivery']['authority_evidence'], status='passed',
    code_changes=False, scope=actual_proof_scope, evidence=[actual_review_ref])
runtime.complete_read_only(proof_ref, actual_delivery_id)
```

user_required 在完成前使用主会话控制的 `record-acceptance`：scope=confirmed_task_scope、acceptance_type=user_required、当前 delivery_revision/report、真实 user_reference 与 evidence；或关闭当前原子验收事项时提供同样的用户依据。交付者不能代用户记录。完成不代表 close 获授权，也不代表资源释放。

保全 JSON 为 `executor_id/generation`、`completion_verification=task['completion']['proof']`、`files=[{path,sha256,original:task['delivery']}]`、`handoff: REF`、`unregistered_evidence_preserved:true`。报告副本和交接必须在被清理树外，scope 与实际完成依据一致。现有 lifecycle(close) 后 release_receipt/RESOURCE verifier 使用这个 canonical 结果，不由文件副本自证完成。源码树只有在同一账本已有成功资源清理记录且外部证据有效时才可消失；其它丢失或变更仍拒绝。

最小可运行夹具（无 MR、CI 或直接修改完成字段）：

```powershell
python -X utf8 -B -m unittest discover -s skills/isolated-parallel-development-coordination/scripts -p test_read_only_completion.py -v
```

其中 `test_current_cli_entry_finishes_without_direct_ledger_edit` 实际调用 CLI；`test_complete_preserve_canonical_close_and_real_owned_file_cleanup` 走公开入口到真实临时目录清理。所有 native 返回都明确本地夹具，不能代真实宿主验收。报告保留实际命令、原失败和新源码 hash。
