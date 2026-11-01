# 单一交付负责人：授权、消息与发布检查

按动作查本契约，不默认通读。现行角色与权限见[共同规则](current-rules.md)；下列 R 编号、旧案例及“本轮”是[历史修订](history/index.md)的上下文，不授予或撤销当前任务权限。低层命令仅在同一账本兼容/诊断，不另建生产状态。

用户原主会话仅管需求、业务决策、功能验收、范围和顺序授权。技术独审/CI/作者沟通/串行合 main 由唯一交付者闭环；常规技术阶段不请求 root 确认。真实功能修改归原作者，同一 MR 重送。部署仍是独立授权事项，不能从 merge 权限推导。

仍只有一个持久任务账本。`coordinator_id` 保留用户主会话身份；可选 `delivery` 记录唯一交付者及范围。文件锁串行保存，角色检查限制操作入口。现有批次的全局 state 写权不自动移交；只在明确选定的实例账本采用新机制，旧索引/journal 不与它并列作权威。本版本不改 live 账本、安装或接管实际发布。

## 最少结构化入口

原主会话 `apply` 一次具体委托（task 必须已登记，原 task Git rights 不改变）：

```json
{"op":"delegate","actor_id":"original-main","agent_id":"delivery-agent","task_ids":["IMPORT","EXPORT"],"merge_order":["IMPORT","EXPORT"],"required_ci_jobs":["backend-tests","frontend-build"],"user_reference":"真实用户范围/顺序授权","authority_evidence":{"path":"/absolute/authority.json","sha256":"actual"},"routes":{"author-import":{"target_id":"authorized-existing-work-agent","existing_work_agent":true,"evidence":{"path":"/absolute/route-authorization.json","sha256":"actual"}},"author-export":{"target_id":"another-authorized-existing-agent","existing_work_agent":true,"evidence":{"path":"/absolute/other-route-authorization.json","sha256":"actual"}}}}
```

不能重复 delegate 成另一个 main 写者；变更前由原主会话有据 `revoke-delivery`，且先释放 reservation。旧 ledger 可读/恢复，但 merge 现在必须有单一交付者授权，不能沿用“root 亲自最终合”的隐含身份。

交付者使用 `python -B P --state STATE --actor delivery-agent apply OP.json`，主会话的用户操作用 `--actor original-main`。技术 receive/drain/CI/review/rework 属交付者，next/wait/close/整体验收与授权属原主会话。`actor_id`/user_reference/文件 hash **不是宿主身份认证或权限赋予**；实际接入必须可信映射调用者身份/授权。不能靠伪造 JSON 越权宣称通过。

## 实际能力与消息阶段

不假设 send_input/resume 或其它 API 一定存在。用当前宿主实际可用能力验证**已授权既有工作 agent**的发送及回执；App thread 可用时可声明，不创建用户对话，不给无关任务发消息。路由授权绑定具体作者、现有 target 和证据。

`delivery-transport` 输入 `transport`：mode 为 `fixture` / `native_verified` / `app_thread_verified`，send_tool/receive_tool 写实际能力名，creates_conversation=false，authors 按已授权作者列 outbound/inbound artifact refs。可先登记 outbound，缺 inbound 时闭环仍未核证；乱序、另一关联 ID 或只 accepted 的反向消息不能算确认。原始工具返回/作者回复应保全，适配声明保留其来源。没有原生 message id 的来源只可用明确标注的内部 correlation_id 关联证据，不能伪称宿主生成了该 ID。

outbound 适配证据字段：sender_id=delivery、recipient_id=作者、target_id=已授权目标、status=accepted、mode、correlation_id。inbound：sender_id=作者、recipient_id=delivery、status=acknowledged、同 mode/correlation_id。这是**发送接受→对方确认**，不改变 task revision/phase，也不是任务完成。

最终回执仍使用现有 receive/drain：owner_agent_id 指原作者，recipient_agent_id 指交付者。channel=native_agent 或 app_thread；App thread 还需 transport_evidence 指向实际任务最终消息的保全适配记录（sender_id、recipient_id、sender_target_id、event_id、kind=task_final）。accepted/acknowledged 记录不能映射 task_final。仅工具 send 成功时，必须写“accepted only”，不可声称读到确认或完成。当前工具不会发送消息/查询宿主，也不验证闲置自动唤醒。

R7 修正使用时序：旧 buffered/held 和 unverified 恢复，在 admission/实际 consume 重新核对当前项目/作者/事件、委托 task scope 和 recipient，closeout 内部 drain 同样检查。不在范围或旧/缺 recipient 的包只 held/unverified，不修改任务阶段/修订或新增验收；不重写 canonical payload/fingerprint。此类权限交接缺口给主会话一次重要例外；不是普通CI阶段确认，也不自动转交或虚造作者新修订。当前不提供已核证 canonical 跨收件人的自动接管；须保全后按真实授权/原始来源做显式对账，不能用改旧字段解除。

已 processed 的 App task_final 也要持续复核：report_for/readiness/next/wait/accepted/业务 answered/集成及 gate 绑定该修订原 canonical 来源。最终消息证据变更/删除时就绪失效，即使源码、自测、report、握手均未变；原自测 passed/已记录用户结果不被篡改为失败。恢复同一原始证据字节可重查原修订，active 焦点保留，不伪造新消息或新修订。返技术操作只给 item_id/source_receipt 也须检查其真实目标 scope；涉及人事项撤回/返技术的控制由原主会话执行。

真实报告缺 task_id/project_id/delivery_revision 或自测采用自定义结论时，可信派工登记和真实 author_event 可以作为**待对账来源**，不代替报告契约。字段别名只使用显式 bind-report 的已知映射；缺少完整 v2 绑定、无法证明语义对应时进入 unverified，canonical 不占键，closeout=needs_reconcile。原字节/自定义自测状态保持，交原作者提供受控的契约说明/新证据副本，不猜补字段、改 passed 或编造下一修订。尚未 canonical 的原 ID 可按既有修复流程重收。

## CI 与合并

原作者自测/用户接受/MR/集成完成后，交付者 `review`，再 `delivery-check` 绑定本轮观察 artifact：project_id、task_id、delivery_revision、based_on_revision、source_digest、head_sha、main_sha、ci。CI 含 head_sha、pipeline_id、status=passed、jobs 状态表、evidence；委托内 required_ci_jobs 每项都要 passed。工具不查真实 CI，执行者必须捕获实际最新源 pipeline/job 证据，不能用这些声明代替实际核对。

`gate` / `reserve` / `local-cas-merge` 只认委托交付者、原 task Git rights 和用户顺序。delivery-check 可先独立核证其它 MR，不意味着允许越序合入；合并门禁必须前序已完成。新 ledger 事件使旧同 revision 检查失效，需重做 delivery-check；reserve 到执行间仍复核原 source/head/target/证据，保留本地 CAS 竞态保护。source/main/CI 失败直接 `rework` 原作者，不改需求/权限、不自动建用户项或新 MR。工具没有真实 GitLab 原子门禁或 OS main 写权限隔离；其它进程/账本不会被此声明锁住。

## 冻结发布

原主会话独立 `authorize-release`：user_reference，加 plan（release_sha、included_task_ids、before_tasks、environment、tag、artifact、manifest、authority_evidence、actions=[tag,backup,deploy,verify]）。included 需已合 main 并核对，manifest 绑定 release_sha、included_task_ids、artifact_sha256；before_tasks 是用户要求发布验收后才可合的后续任务。

交付者 `release-step` 只记录核验过的实际动作结果，不执行发布。需 action、observation/result artifact refs；观察绑定 frozen release_sha/checkout_sha/artifact/environment 和该 SHA 的 CI，不能用未来 current_main_sha 取代冻结源。deploy 前明确无活动新建、已核对数据保留并有 safety_evidence。结果绑定相同 SHA/制品/环境，tag 名必须匹配；原 tag/备份证据失效则不允许后续部署，阶段不能跳过。全部技术核验通过不代替新功能的用户验收。

一批一个冻结发布计划；批次范围/顺序改变需原主会话重新授权并保全旧计划，不擅自覆盖。部署、tag、备份及实际 main 操作仍由交付者在已授权真实环境完成，本工具演练不执行它们。

## 收尾与上报

交付侧 closeout 的 observation 仍标原 coordinator_id，同时 recipient_agent_id=交付者；先对账再 check。普通技术工作 action_owner=delivery，routine_root_confirmation_required=false，notify_main=null；主会话不循环 wait/查 CI。仅单个 eligible 用户项、整批完成或重要通路例外生成给主会话的通知，同一通知摘要去重；active 不插队。用户新决策/验收只在原主会话关闭。

已收到的报告/原生终态未 drain/reconcile 不能用进度掩盖。无事项时沿用 R5 的同 revision 公开进度兜底。只检查 ledger 仍不证明宿主 inbox 完整，声明路由/fixture 更不等于真实双向确认；实际宿主边界和保全结果须独立报告。
