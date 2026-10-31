# 真实任务事实承接

`core_runtime.py ... real-use INPUT.json --actor ACTUAL_INTEGRATOR` 和
`Runtime.real_use(command, actor)` 使用原 project/state-root 的唯一账本、写锁和历史。
不执行派发、消息发送、merge、安装或资源清理。证据文件不是第二状态库。

调用者必须是原 `approved-dispatch-plan/1` 明确的 integration_agent，且不是作者或主会话。
批准的 task_id、已登记 worktree/branch、原 requirements.user_reference 必须一致。
旧批准未含 worktree/branch 时仍绑定原账本身份，CLI 原始 launch 另外核对路径和分支。
已有全局 delivery 委托时也须满足其当前身份和 task scope。
入口不把 JSON 中的 actor、文件 hash 或命令文字当作宿主认证。原批准和直接用户指令的
真实性由受信调用方核实；作者不能通过写报告取得协调权限。

所有输入含 `action`、`task_id`、`approval:{path,sha256}`。支持以下有限动作：

| action | 额外输入 | 结果 |
| --- | --- | --- |
| `bind-cli` | `launch`、`launch_log`、`log`、`thread_id`，后续进程另有 `continuation`，原批准文档副本放 `approved_inputs.spec/checklist` | 原始 CLI process/thread 对应到未归属任务；无 native ID、spawn、RESOURCE 准入或 launch 回填 |
| `receive-cli` | `report`、`observation`、`observation_kind=codex_exec\|app_thread`，默认 kind=completed | 核终态及最终消息提及的报告或完整 head/base 后，复用 canonical receipt/consume；只消费本项 |
| `quality` | `review` | 将同任务、修订、原报告及精确源码的独审附加到任务；原作者报告不改 |
| `technical-acceptance` | `authority` | 单独记录技术验收，保持 user_acceptance 原值；关闭 queued 验收项为 technical_verified |
| `record-merge` | 以下合并输入 | 只登记已发生且已核证的合入，task_completed 与 pending_cleanup 分开 |

CLI 日志必须是真实 JSONL，含同一 thread.started。turn.completed/failed 区别于 process exit；
缺终态记 unknown，并保留责任人。CLI 后续可来自同一 thread 的真实 App 终态；
仍为 `channel=codex_exec, origin_mode=observed_codex_exec`，不能换成 native/App 作者来源。
同任务相同报告的再次观察会重核并去重，不重写原 canonical 回执。
缺报告、无关联 final、失败进程被写成成功、错 thread、错误/过期源码均不能完成任务。

`development-delivery/2` 是一个明确支持的历史作者契约：适配器核原任务、作者 thread、
源码清单和测试结果，在账本目录保存注明 adapter 的不可变 canonical 报告。
保留 imported_author_report 和原 source manifest 引用；不把适配报告冒充作者原件。
其它契约须提交正常 canonical 报告，不能靠自由文字猜测成功。

独审新契约为 `independent-quality-review/1`：project_id、task_id、owner_agent_id、reviewer_id、
delivery_revision、report、source_digest、head_sha、target_sha、status=passed、command、evidence。
也支持已保全的 `independent-PR-review/1|2`，逐一核其原始作者、报告或完整源码清单及 Git 绑定。
独审不授合并权；source/revision/report 改变后重新提交独审，旧证据留在 history。

技术验收授权为 `technical-acceptance-authorization/1`，包含 project_id、task_id、main_conversation、
requirements_digest、acceptance_type=delivery_verified、source、user_quote、evidence。
source 固定为 `direct user message in original main conversation`；evidence 指向已核实的原指令。
新任务可以明确 prepare acceptance_type=delivery_verified；明确 user_required 的任务不能自动放宽。
历史工具只支持 user_required 编码时，只有显式原指令证明是编码限制、并含 legacy_encoding_only=true
才可承接；保持原 requirements/acceptance_type，不重新解释确认的业务 scope。
active 用户事项须先由主会话依真实结果处理，不允许自动技术验收跳过。

合并登记需 `merge_authority`（code-merge-authorization/1，project/task/main_conversation、
head_sha、target_sha、merge_allowed=true、source、user_quote、evidence，可含 merge_order）、
`integration`（精确源码的独立检查报告）、`ci`、`source_head`、`previous_main`、`main_ref`、
`main_head`、`merge_commit`、`git_ref`、`pr_url`、完整 `mr`、`scope` 和 `evidence`。
mr 含 provider/id/url/source_branch/target_branch/head_sha/target_sha，原 PR 身份不可换。
代码已 fully delivered、独审和技术或必要人工验收均满足后，才核已经发生的 Git 合入。
`scope=host_verified` 还要求 `merge_observation` 引用原 `verified-PR-merge/1` 及其真实原始证据。
登记不授予未来 commit/push/merge/deploy 权，也不把合并批准写成人工 accepted。

Git 默认核 project.local_main_repo 或 project.root 的目标 main；显式 git_repository=task 可核
作者仓库已捕获的目标 main。git_ref 仅允许该目标的 refs/heads/ 或 refs/remotes/origin/。
核当前 ref、测试 head/base、作者 clean branch/head，以及 base→source→merge→main 包含关系。
不执行 fetch/merge/update-ref。变基/源码过期必须原作者同步、重测并维护同一 PR。

CI 为 required 时沿用配置的必需任务门禁；not_configured 时须有原独审中绑定到该 head 的
真实空 statuses/workflow_runs 观察，保存“没有配置的 CI 结果”，不能称 CI passed。
首次完成保留包含关系及全部证据；后续无关 main 提交不撤销这项历史事实。
相同登记幂等，不重做资源动作。不同完结证明不能覆盖历史。

回收仍走已有生命周期/保全/资源权限入口。CLI 进程退出不等于作者上下文关闭，
也不释放 native 名额；缺实际 close 能力、保全、子进程或当前资源归属时保持 pending_cleanup。
本入口不增加自动唤醒或后台循环。live 对账和本机安装由独立集成审过精确 head 后执行。
