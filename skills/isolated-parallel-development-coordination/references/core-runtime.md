# CORE 入口与集成契约

首版是单账本内核和有限宿主回调，Python 3.11+ 标准库，无安装/模型调用/定时器。idle_wakeup=false；没有宿主空闲唤醒、全局 hook、重载或后台常驻。消息和文件 hash 不是宿主身份认证。实际调用方负责从原生受信上下文提供 actor、工具回调、当前授权和实际观察；禁止从报告里的工具名、命令或 actor 自证取得权限。

## 初始化、恢复、收口

```text
python -X utf8 -B scripts/core_runtime.py --state-root ABS_PROFILE_STATE --project-root ABS_PROJECT init --coordinator ACTUAL_MAIN_ID
python -X utf8 -B scripts/core_runtime.py --state-root ABS_PROFILE_STATE --project-root ABS_PROJECT recover
python -X utf8 -B scripts/core_runtime.py --state-root ABS_PROFILE_STATE --project-root ABS_PROJECT prepare SPEC.json --actor ACTUAL_MAIN_ID
python -X utf8 -B scripts/core_runtime.py --state-root ABS_PROFILE_STATE --project-root ABS_PROJECT cycle OBSERVATION.json --actor ACTUAL_DELIVERY_ID
python -X utf8 -B scripts/core_runtime.py --state-root ABS_PROFILE_STATE --project-root ABS_PROJECT resolve-next ANSWER.json --actor ACTUAL_MAIN_ID
python -X utf8 -B scripts/core_runtime.py --state-root ABS_PROFILE_STATE --project-root ABS_PROJECT record-main VERIFIED_RESULT.json --actor ACTUAL_DELIVERY_ID
python -X utf8 -B scripts/core_runtime.py --state-root ABS_PROFILE_STATE --project-root ABS_PROJECT complete-read-only VERIFIED_REVIEW.json --actor ACTUAL_DELIVERY_ID
```

state-root 是安装 profile 固定的一项配置，不接受工作消息临时另指定；不同隔离测试 profile 不共享权威。项目根 resolve+normcase 派生 project_id，唯一文件为 state-root/projects/<project_id>/ledger.json。初始化不覆盖，恢复不重建/删历史/抢锁。原 R7 apply/pack/read 工具兼容保留，不能另外维护 manifest/queue/tasks 状态。Runtime.recover 返回 ledger、project_id、revision、summary、handoff、idle_wakeup；未知/过期观察只给最后登记信息。

SPEC 包含 task_id、worktree 绝对路径、branch、scope、acceptance、user_reference，可给已知 owner_agent_id；未实际取得的执行者留 null，不伪造 native ID。prepare 仅登记并排队。直接 apply(dispatch/start/redispatch/rework/integration-start) 被公开入口拒绝，实际执行走 launch。开发者不写账本/授予权限/独审自己；原主会话解决人事项并授予范围权限，交付者处理技术收件和审核。

新代码派发还须通过 [需求收口与路由](demand-routing.md)：`routing` 保存 spec 自核对、用户拆合/派发摘要审批及唯一指令归属，`launch(..., route_id=...)` 在资源准入之外重新核验这些依据。`routing_status` 只读同一账本及 canonical 任务事实。`prepare` 拒绝新 read_only 开发者，普通查询/分析由主会话处理，已有只读记录继续原完成协议。外部实际 spawn 可由批准的主/集成调用 `bind_observed_dispatch` 登记真实派发与当前运行观察，不补造准入或重复调用宿主。

cycle 使用原 coordination-closeout/1 观察：先收件/drain，再验终态、当前报告、blocked 下一责任者、来源/收件者和保护依据；结果 revision 与已保存 ledger/summary 相同。fixture_only 不代表真实宿主收件完整。resolve_and_next 在一个写锁内关闭当前事项并激活下一合格事项，返回下一项、最终 revision/summary；delivered_to_user=false，必须由当前主会话实际呈现后再 wait。不会打断已有原子事项，不把外部资料协助变成业务语义授权。close 后尚未新对账的精确进度不得伪报。

## Python 宿主接口

```python
from core_runtime import Runtime
from host_adapter import HostAdapter
runtime = Runtime(profile_state_root, project_root)
adapter = HostAdapter("app_thread", available_tool_names, trusted_invoke, actual_delivery_id,
                      scope="host_verified")
# trusted_invoke(tool_name, arguments) 是宿主代码；模块不执行 JSON 内的代码。
```

| 通道 | 实际工具映射 | 边界 |
| --- | --- | --- |
| app_thread | mcp__codex_app__send_message_to_thread / read_thread | 只已授权既有聊天，禁止 create/fork/archive 代替 agent close |
| native_agent | spawn_agent / send_input / wait_agent / close_agent / resume_agent | 仅实际工具清单可用且当前授权允许时；缺能力明确 unsupported |

此前作者子上下文未暴露 native 工具，不能据此推断主控制面或整个桌面没有 native 能力。本版组件没有新建/关闭真实 agent。App read_thread 的 schemaVersion/thread/turns[].status 结构曾做本会话只读结构核对，只保留字段名，无会话正文/账号/ID；这不等于完整宿主 E2E。集成者按实际调用上下文的工具名和原始成功语义绑定 available_tool_names，未提供能力不会被猜测为存在。

bind_executor(executor_id, routes, main_actor, authority_ref) 仅绑定真实已控身份；route 按 channel 存 host_id/target_id。跨通道同一身份须明确授权证明，不能同名推断。混合通路 proof.mode=mixed_verified，routes[owner].channels 与 transport.authors[owner].channels 分别绑定每条发送接受/对端确认；最终消息另外绑定 sender/recipient/target/event。失效来源或旧收件者不能靠未变报告放行。

首次登记即校验所有 route 的非空字符串 host_id/target_id，不以注册表已有项目作为检查前提。launch 每次新复用动作前重读 hash 绑定的 observation 与 authority；缺失、变更、identity/generation 或缓存事实不一致立即拒绝，不发送消息、不记 running，也不保存新的尝试。

late-bind 同样遵守唯一任务责任：执行者拥有其它需求时拒绝新任务派工，即使旧任务已完成或该 agent 当前 idle。新需求创建专属开发者；同一未结束需求的技术返工继续原作者、工作树和 PR。

HostAdapter.final_receipt(state, task_id, payload, sender_target, recipient, proof_ref) 只接受宿主侧真实来源。Runtime.receive 校验终态报告的项目/责任人/修订/来源/收件者；不同通道同 event/报告仅在再次核证后去重，canonical 原包不重写，冲突拒绝。缺报告走原未核证缓冲，同修订恢复；只读观察不占 canonical。

## 资源准入与执行

launch(task_id, adapter, attempt_id=..., admission=REF, authorized=True, message=..., spawn=False, executor_id=..., route_id=...) 是一次有限调用。admission 是受信 RESOURCE bridge 生成的 granted 证明，包含 project_id、task_id、attempt_id、request_id、permit_id、launch_identity；后者是既有 executor_id 或尚待取得身份的 spawn:<attempt_id> 请求标识，绝不是伪造 agent ID。CORE 只留引用和关联键，RESOURCE 是许可权威。回调只能联系该任务授权执行者，不覆盖模型、推理或速度，不关闭运行者。同需求返工前核无 pending_work/active_children；原任务责任人不得被静默替换，新需求不接用其它需求的开发者。

外部副作用前先写 intent。满额/缺能力保留 queued 并立即返回 public_progress；发送接受还须一次真实运行观察，不能当已开始/完成。unknown 或 accepted_unconfirmed 不重发，等待新的实际观察；调用方不能用新 attempt 掩盖未核清的旧副作用。

若已接受的本次执行在首次观察前就结束，可以用来源/修订/报告全部核证的终态消费并记 reported，不伪造中间 running。reconcile_launch 仅接受实际未开始的观察证据来解除未知请求；reconcile_lifecycle 仅接受明确 closed 的宿主结果来核清未知 close，idle/completed 不够。

RESOURCE.capacity_release(request_id, permit_id, receipt_ref, verifier) 的 verifier 使用 Runtime.verify_release_receipt(context, receipt)。close 真实确认后或明确从未创建 agent 的未用准入才可 mint release_receipt；failed/unknown/idle/completed 报告均不释放。confirmed_closed 返回 task_kind=code|read_only、task_completion_verified=true；task_completed_on_main 只对 code 为真、read_only_completion_verified 只对 read_only 为真，另带 verified/preservation_valid/no_active_work/no_active_descendants/no_release_dependencies/exclusive_task_resource/cleanup_authorized=true、protected=false。confirmed_not_started 仅返回 verified/not_started。事实不另存在 RESOURCE 形成任务账本。

## 完成与清理

原批准的集成者可以通过 `real-use` 入口承接同任务/精确源码的独审、真实 CLI 作者及已发生的合并事实。技术自动验收保持 user_acceptance 原值；明确需要人工验收的任务保留原关口。输入契约和历史默认编码的受控处理见[真实任务事实承接](real-use-closeout.md)。此入口不执行外部副作用，不替代下面的清理门禁。

record_main_inclusion(proof_ref, delivery_actor) 不执行 merge，只核对已经实际发生的合入：既有用户接受、集成、自测/独审/当前 CI/顺序及 Git 权限仍有效；proof 绑定 project/task/current revision、provider、source_head、previous_main、main_ref、main_head、contains_source 和实际证据。GitHub/GitLab 元数据都可登记。host_verified 来自实际宿主观察，fixture_only 仅隔离演练，不能授权真实回收。

生命周期 guard 需要合法 task.phase=completed：code 仍核 merge.verified 和目标 main 包含源码；read_only 核当前 canonical completion、交付委托、报告修订和只读输入，保全清单绑定 completion_verification。两者均核交付/未入库材料外部保全和知识交接、源/备份 hash 及 scope。executor generation、无运行/后代/未完动作、无共享保护/其它任务引用均检查。close 真正成功才记 lease=closed；仅 completed 或不明确成功记 unknown，继续占位。重复 operation_id 不重复调用。失效保全或旧 generation 拒绝。已完成任务不 resume；bug 由最新 main 新任务关联旧记录。外部未完成任务失联恢复仍需另行合法导入边界，本次没有新增该入口。

register_resource(task_id, resource, authority_ref, delivery_actor) 将明确资源归属存同一账本。文件资源须在该项目管理的 cleanup_root 内；默认为 ledger 同级 resources/，不接收任意系统目录。agent/process 需要准确 executor/generation 或受管 process_identity；不会扫描/杀全局进程或删别的工作树。

cleanup_task(task_id, operation_id, preservation_ref, trusted_invoke, delivery_actor, authorized=True, scope=...) 先校验对应 task_kind 的合法完成、保全、独占或共享已无引用、无工作后代/保护/依赖，再持锁逐资源登记 intent 和结果。trusted_invoke(resource) 是具体已授权宿主或 RESOURCE 动作，返回 status=cleaned/failed/unknown，cleaned 必须有可核证据；agent 另需 closed=true。不能把 task completed、termination_requested 或发送接受当 cleanup completed。

agent close 只走 Runtime.lifecycle 单一路径并 mint release_receipt；资源聚合入口只核证该 canonical close，不另调第二次 close。工作树/文件清理完成但 agent 尚未明确 closed 时，整体仍是 pending_cleanup。RESOURCE 的目录动作若返回组件级 pending_cleanup，bridge 须映射单资源 failed/unknown 并保留原 receipt，不把它改写为成功。

RESOURCE 文件联接示例：invoke 收到的 resource 已含 CORE 生成的 cleanup_proof（不可变 ref），调用 `artifact_cleanup.cleanup(resource['id'], resource['cleanup_proof'], runtime.verify_cleanup_receipt)`。CORE 的 verifier(context, proof) 只读 canonical cleanup intent、resource/project/task/attempt/path、同一 preservation/main/授权及共享/保护边界，返回与容量释放相同的瞬态 flags；不获取 CORE 写锁、不写账本、不做宿主 I/O，可安全在 RESOURCE 锁内调用。接收者不能自写一个总返回 true 的 echo verifier。process 回调同样保留精确 request_id/nonce 和受管 PID 创建身份，由 RESOURCE 执行物理核验。

部分失败保存 task.cleanup.status=pending_cleanup 和各资源已确认结果，task.phase 仍 completed。重复操作幂等，unknown/intent 不盲重发；reconcile_cleanup 先核实际已清/仍保留证据，再允许新的有限操作继续。原回合结果保留，清理失败不丢任务。后续无需长期保留开发环境：发布用独立冻结提交/制品任务；publication_dependencies 仅指其它活动任务还实际使用这些资源，不能只因“尚待部署”填入。

## 新任务资源入口

未设置全局 delivery 时，资源操作仅可复用本任务原批准。正常 prepare_development 的 authority_ref 冻结 approved-dispatch-plan/1；已观察派发继续引用其现有 proof.approval。每次重验原 main、独立 integration_agent、task/worktree/branch 及 bounded_new_task_resource_cleanup 范围。只允许该任务的资源创建、observe_executor(..., task_id=...)、保全及清理/确认/许可回执；不修改 _actor，不得到普通任务写权、其它任务或 resume 权限，不为无 CI 项目编造 required_ci_jobs。当前调用者身份仍须由真实宿主提供。

完整插件宿主以同一 Runtime/Controller/ArtifactCleanup 构造 ResourceBridge，先 `prepare_development(spec, main_actor)`，再 `create_worktree(task_id, operation_id, authority_ref, actor, authorized=True)`。spec.worktree 必须是 CORE cleanup_root 内的未创建专属目录，且 RESOURCE cleanup_roots 同样允许。任一范围或能力不匹配在 Git 前拒绝；同操作未知不重发。只有实际创建和两组件登记成功为 registered，才走原批准路由、RESOURCE 准入和 launch。取得实际执行者时同事务登记 task:agent、generation 和归属。基础 prepare 不提供创建能力，返回 managed_closed_loop=false，登记缺口不能成为 cleanup_completed。

按需调用 bridge.create_temporary 创建并登记 environment/database/artifact，bridge.run_process 复用 RESOURCE 的有限 Job/PID 创建和整树退出证据；没有实际 PID 的排队请求不冒充已运行。后续资源同样专属于原需求，不建立新任务队列。详细 API、外部保全和真实宿主边界见插件 docs/resource/closed-loop.md。

合入核验后由当前唯一交付者调用 `preserve_task(task_id, new_external_directory, handoff_ref, actor, scope=..., extra=[...])`。保全准确的交付/审核/合入引用图、当前修订及必要材料到回收范围外，随后沿用 reclaim_development。原报告路径和字节不改。report_for 只有在同任务 worktree 确认清理及保全绑定仍有效时读取已保全源清单；缺失证据必须有已确认清理归属和同 hash 副本。源/证据存在但改字节、非正常丢失、保全损坏均拒绝。原始 artifact/load_bound 审阅可用 evidence.preserved_read(state, task_id) 的有限上下文。

本入口不扫描、迁移、补登记历史任务，不重写旧完成状态，不增加原 main/交付者角色或 Git/清理权限。缺能力或未知仍归当前集成者处理；fixture 成功不代表 live 账本接线或 native 关闭可用。

## 测试与事实范围

Windows 的 cleanup-proofs/receipts 文件可能因完整 SHA 文件名超过普通 Win32 路径上限。共享证据 I/O 的 write_atomic/load/digest/artifact/ref/load_bound 在达到 260 字符时采用扩展绝对路径；两类生成工件也在存在性/不可变检查前转换。保留 64 位 SHA、原 atomic replace、后续 hash/read 和幂等校验；短路径和其它平台保持原有路径。不会改系统长路径开关、缩短 TEMP 或放宽绝对路径/资源归属/清理授权。只涵盖本模块调用这些入口的证据 I/O，不宣称全局任意长路径支持。测试夹具仅为已核归属的独占临时根销毁使用扩展别名，传给被测入口的路径仍是普通路径。

使用 scripts/run_core_tests.py --test-root ABS_NEW_TEST_ROOT --evidence-root ABS_NEW_EVIDENCE_DIR。所有夹具和子进程输出仅进指定隔离测试根。默认排除 5 个会创建临时 Git commit 的旧用例，本轮未获 Git 提交授权，不套用旧 R8 130 数字；显式额外授权后才用 --allow-disposable-git。缺便携包外真实旧案例会明确 skip，新的匿名最小轨迹在 fixtures/redacted-trajectories.json 实际回归。

本组件测试包括真实一次性目录/SQLite 文件回收，native close/resume/源 main/许可均为明确隔离夹具，不冒充真实宿主清理、远端 Git/CI、全局安装或两个真实项目默认加载。完整插件集成和非作者复核由集成者/独审者另绑定，组件通过数不能相加充当组合通过。
