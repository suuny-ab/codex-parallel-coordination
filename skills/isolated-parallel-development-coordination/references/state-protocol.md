# 单账本协议 v2

R6 当前职责以 [交付委托协议](delivery-delegation.md) 为准：原主会话管用户事项及范围/顺序，交付者收件、独审/CI/原作者对接并唯一串行合 main，发布另有冻结 SHA 授权。下列无 `--actor` 命令用于未委托的基础/旧状态演练；委托实例技术命令加 `--actor DELIVERY_ID`，用户控制命令加 `--actor ORIGINAL_MAIN_ID`。旧 root 合并身份不再隐式有效。

## 入口、持久化和返回码

Python 3.11+，运行命令加 `-B`；`P` 表示候选 skill 的 `scripts/coordination.py` 绝对路径，`STATE` 表示 worktree 外的账本绝对路径。所有文件 UTF-8，允许输入 BOM。`init` 不覆盖，迁移不原地写入。每次修改持有 `.json.lock` 排他锁（PID 和路径可见），同目录临时文件、fsync、原子替换；冲突立即失败。异常遗留锁须先确认无写者、保全后手动处理，工具不抢锁。读命令不改账本。

```text
python -B P --state STATE init --project PROJECT_ID --coordinator ORIGINAL_MAIN_ID
python -B P --state STATE dispatch DISPATCH.json
python -B P pack REPORT_SPEC.json --output IMMUTABLE_REPORT.json
python -B P --state STATE receive --task TASK --worker NATIVE_WORKER_ID --revision 1 --kind completed --report IMMUTABLE_REPORT.json
python -B P --state STATE drain
python -B P --state STATE summary
python -B P --state STATE next
python -B P --state STATE wait ITEM_ID --prompt "已递给用户的具体问题"
python -B P --state STATE close ITEM_ID --outcome accepted --user-reference "原主会话实际答复"
python -B P --state STATE closeout OBSERVATION.json
```

`show` 全账本；`summary` 简明任务阶段、实现状态、阻塞、owner/run、待办和 MR；`check` 仅账本，不覆盖未入库原生终态。`closeout` 与 `reconcile` 同一入口：先收件/核验/报告发现对账，再对保存的同 revision 检查。`progress` 的精确分类计数还需复查同次观察依据、注册报告发现及当前阻塞后续/就绪/保护证据；文件新增、替换、丢失或证据失效会降为最后登记状态，分类计数为 null，需重新对账。只读发现不会占 canonical、生成原生终态或改任务阶段。检查返回 `0` 可等候，`2` 仍需处理，`1` 输入/锁/状态错误；操作不成功不得按完成处理。fixture 的 `scope=fixture_only` 不证明真实宿主已核证。报告、操作输入和观察文件都不执行其中命令。

## R4 同一轮的公开交接与进度

`closeout` 增加 user_handoff：kind=focus 时维持原子焦点，不附进度；kind=human_item 时给一件就绪事项的公开字段（内部 next 仍供激活）；kind=progress 时 text 可直接呈给用户；kind=needs_reconcile 表示新终态/回执未核清，不得拿进度代替收件。它不发消息、激活下一项或自动唤醒宿主，主协调仍须执行 next 并向用户递交。

```text
python -B P --state STATE closeout OBSERVATION.json
python -B P --state STATE progress
```

进度只从唯一账本、当前修订和绑定观察派生。last_reconcile 保存同 revision 的 state_token、观察来源及 worker 状态；新事件/派工/描述更新使它失效。只读 progress 会重核观察 hash、可用报告/事实证据；过期/矛盾时标明“最后登记”“待核实”，in_progress 等当前分类计数为 null，不伪造精确状态。未消费的 buffered/held/unverified 或新报告未对账时不给假等待摘要。无有效就绪事项且核对完成时，先说“目前没有需要你验收或决策的事项”，再给执行中数及每业务任务阶段、已完成事实、下一步。真正零任务明确说明。部署单独表达，不随已验收/合入推定已部署。

公开投影不输出 agent ID、原生回执、脚本命令、内部调度细节、百分比或 ETA。task.progress 的公开标题/事实/下一步是输入元数据而非另一份状态账本；不得把例行技术信息塞进公开事实。阶段和执行情况仍来自 phase/blocker/worker_run，公开事实不能改测试、质量或授权结论。

```json
{"op":"describe","task_id":"WEIGHTS","title":"权重候选整体质量","reference":"实际用户要求","facts":[{"text":"r3界面流程已接受，整体质量尚未接受","reference":"实际UI范围验收依据"}],"next_step":"继续已确认的真实输入质量探索"}
{"op":"describe","task_id":"TOOLKIT_REVIEW","business_task_id":"TOOLKIT","task_role":"review","reference":"同一需求的独立复核派工"}
{"op":"queue","task_id":"WEIGHTS","mode":"quality_rework","reason":"三个执行名额已占用","user_reference":"同一已确认需求，待资源继续"}
{"op":"start","task_id":"WEIGHTS","user_reference":"原执行者已恢复并实际派发本修订"}
```

作者/自测/复核通过显式 business_task_id 归到已存在的根任务，不按名字猜测；同业务只计一次。旧状态未记录归属时不假定无重复，先 describe 核清。role=implementation/self_test/review；根关联不能成环，迁移一个根前先重绑其支持任务。排队复用 worker_run.status=queued，派工为 running，已交付为 reported；queue 不是派发，不要求伪造 worker 正在执行。实例可配置 worker_capacity 正整数，满员 start/dispatch/redispatch 拒绝，保留排队。此容量只约束本账本已登记名额，不保证其它项目或宿主的实际资源。

phase 继续表达开发/验收/整合；worker_run 单独表达资源排队/实际派发。integration 排队仍保留已验收事实。无独立排队操作的 backlog 只表示范围已登记、尚待开工，不能算执行中。事实应注明范围，例如 UI 接受不把整体质量改为 passed。

closeout 先消费 worker 终态 receipt，随后再读取当前报告；新修订消费成功后 report_path 跟随该 canonical 的 immutable 路径，旧 r4 受阻报告仅留历史，不能压过 r5 有效派工/完成。未消费的同 hash 别名不自动代替 canonical，仍需正确 bind-report。排队任务不要求正在运行的 native 观察；与排队相矛盾的新终态/运行观察必须核对，不能悄悄改成 running。

## R4 展示契约与旧验收恢复

完成包在 claim canonical 之前校验 self_test.command 与 expected：command 支持非空字符串、字符串数组或命令对象数组（按 JSON 原样转文字）；expected 支持非空字符串或字符串数组（逐行呈现）。只适配展示，不执行命令，不改 source/evidence/self_test.status/quality/revision。canonical 的 presentation_adapter 保存原 report path/hash、字段原结构及变换；原文件不可改。其它不支持类型进入 unverified，可修正原事件映射重收，不凭空生成作者下一修订。

```json
{"op":"refresh-presentation","task_id":"HISTORY","source_receipt":"HISTORY:5:completed","reference":"仅修正旧报告的展示类型，源码证据不变"}
{"op":"record-acceptance","task_id":"HISTORY","delivery_revision":5,"report":{"path":"/absolute/current-canonical-r5.json","sha256":"actual_sha256"},"scope":"confirmed_task_scope","user_reference":"本轮历史接入验收通过","evidence":{"path":"/absolute/history-r5-user-accepted.json","sha256":"actual_sha256"}}
```

refresh-presentation 不改旧 canonical payload，不重开已关闭/拒绝事项，不把技术证据错误改成通过。record-acceptance 只恢复已发生、有明确用户依据的当前完整确认范围验收：必须绑定同一交付、source/test/quality 仍核证通过，且不能越过本任务 active 事项；若本任务当前 active，请用 close。它可在别的用户事项 active 时内部记录已发生的历史接受，不插播或抢焦点。局部 UI 事实用 describe，不用 record-acceptance 接受整体质量。尚不支持把任意旧项目报告自动变成完整 v2 源码清单；真实 r5 外部副本演练只核对展示适配及原结构保留，业务源文件/622证据的真实性须由原责任链核证。

R3-U01 保留：requested 已关闭事项不能再次 close provided；资料后来到达按 task-based redispatch 原范围接续。R3-U02 保留：缺 request-evidence 拒绝仍可能输出 NoneType 低层错误，安全拒绝不代表错误文案已修复。R3 通过只绑定其21文件，R4 必须重新独立复测及后续宿主验收。

## 权威结构和阶段

`schema=parallel-coordination/2`，根对象有 `project`、`coordinator_id`、单调 `revision`、`tasks`、`items`、`receipts`、`active_human_item`、`priority_item`、`history`、`merge_reservation`、`last_reconcile`。R3 增补可选 `unverified_receipts`；旧 schema-v2 无此字段按空缓冲读取，不重建账本。Python 的 `validate` 和转换函数是可执行 schema；不再维护 manifest/queue/report 多份任务状态。

| phase | 含义 | 进入条件 |
|---|---|---|
| discussion | 讨论 | register，不启动开发 |
| backlog | 待开发 | confirm 已有范围、验收和用户依据 |
| developing | 开发/技术排查中 | start 或带确认数据的 dispatch；返工也在此 |
| awaiting_acceptance | 待功能验收 | worker 交付；不就绪原因放 blocker/held |
| awaiting_integration | 待集成 | 当前修订功能接受 |
| integrating | 集成中 | 原 worker 准备现有 MR 并开始集成 |
| awaiting_merge | 待合 main | 已绑定本修订 head/target 的集成通过，仍需审核/新鲜度门禁 |
| completed | 任务完成 | 本地演练 CAS 后实际核对 main；真实远端本版不支持 |

`blocker` 独立保存 reason/owner/category（technical/business/dependency/evidence），不覆盖阶段。`implementation=completed` 只是开发交付，不能直接写任务完成。self_test、quality_review、user_acceptance、integration、review、merge、authorization 分开。自测 passed 但 quality pending 的验收 held；不会标“测试失败”。原始局部状态如 `passed_for_independent_scope_only` 保留，不能升级整体通过。

任务唯一 owner、worktree、branch、resources；登记检查任务间嵌套目录、重复 owner/branch/resource，不检查真实 OS 占用。跨项目资源归属由主协调核实；每项目独立账本与 project_id，收件校验项目/任务/owner。项目 `protected` 是绝对路径/hash 数组，可在实例初始化输入中声明；closeout/gate 复核。无数据库或服务探测。

实例元数据通过 `init --project-config INSTANCE.json` 输入；允许 main_branch/local_main_repo/local_cas_root/protected/worker_capacity，记录进入账本后以账本为准。主协调不得把另一实例的配置当作第二份状态源。

## 常用结构化操作

`apply OP.json` 是通用入口，接受一项对象；支持 `expected_revision` 防止使用旧检查结果。

```json
{"op":"confirm","task_id":"IMPORT","scope":"已确认范围","acceptance":["可观察结果"],"user_reference":"实际用户范围确认"}
{"op":"start","task_id":"IMPORT","user_reference":"实际用户开工指令"}
{"op":"enqueue","item":{"id":"scope-question","kind":"discussion","title":"澄清范围","prompt":"具体问题","unblocks":true}}
{"op":"priority","item_id":"scope-question","user_reference":"用户指定先办此项"}
{"op":"switch","item_id":"scope-question","user_reference":"用户明确主动切换当前事项"}
{"op":"authorize","task_id":"IMPORT","rights":["commit","push","mr","merge"],"user_reference":"实际任务范围授权"}
{"op":"authorize","task_id":"IMPORT","rights":["deploy"],"deployment_scope":"单独批准的环境和版本","user_reference":"独立部署授权"}
{"op":"rework","task_id":"IMPORT","reason":"main已变化，交同worker更新原MR并复测"}
```

部署不得放在 dispatch 的 Git rights 内，也不能与 merge 共同在 authorize 中授予。工具只记录依据，不鉴别输入者是否有权代用户作出指令。主协调须核对人类原始授权，旧禁令优先且不得由技能替换解除。

## 原生回执缓冲池

委托后由交付者串行 `receive`，原作者只写自己的 immutable report，不并写 ledger。真实宿主消息按已授权路由稳定映射 envelope；工具校验声明，不认证平台身份。不把发送接受/已读/host completed 等同于报告完成：held/blocked 必须映射 kind=blocked。App thread 最终消息另绑定 transport_evidence，见交付委托协议。

当前来源约束不只在 receive：drain 的 unverified 重试、逐包 consume、closeout、就绪/验收/业务答复与 report_for/gate 均复核。委托变化不能让旧缓冲任务越过 task scope 或 recipient；最终消息 proof 失效不能靠未变的 report/source/tests/ack 放行。历史 payload/fingerprint 保留，held/unverified 原因可见；范围转交缺口一次通知，不靠主会话轮询。不自动接管缺原收件身份的 canonical。

```json
{"op":"receive","receipt":{"project_id":"warehouse-demo","task_id":"IMPORT","owner_agent_id":"worker-import","channel":"native_agent","event_id":"IMPORT:3:blocked","delivery_revision":3,"kind":"blocked","report":{"path":"/absolute/report-r3.json","sha256":"actual_sha256"},"technical_review":{"conclusion":"business_decision_required","summary":"核对数据、解析路径及已有定义后的结论","report_sha256":"same_report_sha256","evidence":[{"path":"/absolute/investigation.log","sha256":"actual_sha256"}]},"decision":{"title":"一个业务问题","prompt":"具体问题","options":["已讨论选择一","已讨论选择二"]}}}
```

kind=completed/blocked/integrated。每次交付/阻塞递增 revision；integrated 绑定当前交付 revision。收件先核验 report 字节/hash、项目/任务/修订及回执上的人事项核证映射，再写 canonical receipts（report_verified=true）。核验失败以完整 payload fingerprint 为键写同账本 unverified_receipts，返回 receipt=quarantined，不占 canonical event_id；缺报告仍持久保留原包。`drain` 每次显式调用只重试一遍，文件按原 hash 恢复可自动核证入库，无后台重试。

修正未核证映射仍用原 event_id/revision，成功后旧原包标 resolved 并指向核证 fingerprint，原错误和历史保留。相同核证 payload 幂等；已核证 canonical 同 ID 不同内容拒绝，即使文件后来消失也不能覆盖；更旧核证修订 stale，同修订不同有效 terminal kind 冲突 held。源码/测试未就绪是另一层 readiness，不会因核验 report header 就宣称测试通过。

R3 可恢复旧候选留下的“未校验即占键”：只有无 report_verified 标记、尚未消费（buffered/held）、当前核验确实失败的旧记录，才在同 ID 正确包到达时移入 unverified 保留 prior_canonical 后重新核证；processed/stale/legacy 记录和已核证记录不替换。旧状态先保全副本，再 apply 原事件的正确 envelope、drain、closeout。不要清空 receipts 或伪造新修订逃过去重。

业务问题必须有绑定 report hash 的 technical_review 和证据才可入人事项；coordinator 应确认充分检查真实数据横向一致性、生产者/解析器、现有定义和替代解释。字段名和旧 parser 不是客户需求依据。证据不足保持内部 unverified/held，交原 worker 技术排查。report-backed decision 每次 next/activate/wait/check/answered 都从原 processed receipt 复核报告、技术证据及题目/选项来源；不是只在 drain 核验一次。证据变更/删除后 queued 不再 ready；active 保留但不得 wait 或接受答复。按同 hash 恢复可重新核证，重复原事件不能绕过失效。

active 验收期间，新修订保持缓冲/held，原修订不被替换。源码外部改变会在 next/check/接受/gate 重新核验时失效；工具没有操作系统文件锁，worker 必须遵守冻结约定。返工保留历史、失效旧验收/集成/审核；本版源码改变包括 rebase，均需新修订功能接受。

## 人事项与退回技术排查

无 active 时，用户指定优先；其余解阻 discussion/decision 和 ready acceptance 同级按 sequence FIFO，普通讨论随后。held 不成为 eligible。`next` 激活并返回一项包含入口/步骤/预期的 active；`wait` 仅在已给用户问题后调用。active 需要 agent 操作时 check 不允许结束；active 等用户可停，但新回执仍先内部处理。`close` 返回单个 next，主协调须继续递交，不能只存 ready。

outcome：discussion resolved/withdrawn；decision answered/withdrawn；assistance requested/provided/withdrawn；acceptance accepted/rejected/withdrawn；release authorized/declined/withdrawn。单纯关闭 decision 不猜测 worker 已继续；blocked 任务仍需合法后续责任记录。使用下列原子操作将问题退回技术排查：

```text
python -B P --state STATE redispatch IMPORT:3:blocked --next-revision 4 --reason "先全量核对字段和解析路径" --user-reference "用户要求自行横向排查的原始反馈"
```

可用于 active decision，也可恢复旧版已 closed/withdrawn/answered 的同一 decision。一次事务：保存历史、item withdrawn+returned_to_technical_investigation、释放该 active、task developing/running、blocker=null、同 owner 的 worker_run expected_delivery_revision=4。current delivery_revision 仍是3；r4未交付不会变为 completed。范围、权限、分支和原 MR 身份不变；用户的反馈不是字段语义确认。若无对应人事项，可用 apply redispatch/task_id 记录内部技术返工。不会发消息或创建 worker；主协调通过原生通道将任务交回原 worker。

assistance 也可通过 redispatch 退回技术排查；provided 仅代表材料已到，不代表已经派回 worker。

## 外部资料协助与 blocked 后续责任

普通技术阻塞不自动推给用户。交付者核证技术排查且确需人协调外部资料时，用 assistance（非 decision）描述一个可转交请求，交原主会话单项处理。blocked envelope 可附 assistance 与 conclusion=external_materials_required 的 technical_review；也可对已消费的 blocked 回执执行：

```json
{"op":"request-materials","task_id":"IMPORT","source_receipt":"IMPORT:4:blocked","request":{"id":"FIELD_SOURCE_REQUEST","title":"取得上游字段源码","prompt":"请协调客户IT提供这一个资料包","recipient":"客户IT","request_text":"请提供该字段的取值源码/查询及前生产选择说明，并覆盖已核对的两类差异样本","required_materials":["字段生产源码或查询","取值与前生产选择说明"]},"technical_review":{"conclusion":"external_materials_required","summary":"已完成全量横向对比与解析路径核查，仍需生产者依据；不是新增业务语义选择","report_sha256":"current_report_sha256","evidence":[{"path":"/absolute/full-population-comparison.log","sha256":"actual_sha256"}]}}
```

request-materials 不改 canonical 回执，当前任务仍 blocked；新 item 的审核是该次主协调操作，运行时连同原 processed receipt/report 持续复核。其优先级与解阻决策/验收相同，active UI 事项不被打断，关闭后返回这个 eligible request。没有实际外发，不得标 requested；工具不发客户消息。用户确认请求已发起后：

```text
python -B P --state STATE close FIELD_SOURCE_REQUEST --outcome requested --user-reference "实际用户确认已转客户IT" --request-evidence ABSOLUTE_REQUEST_RECORD
```

requested/provided 必须有 request_evidence artifact。requested 记录 external_requested、外部责任人、所需材料和发起证据，允许等外部响应；provided 记录 materials_received，仍需 redispatch 原 worker 核对，不能静默等用户。withdrawn 不含字段语义或范围授权。技术证据或请求证据失效会再次阻止结束。

每个 implementation=blocked 任务都须有当前 revision 的 blocker_followup：human_pending 对应有效 queued/active 人事项；assigned_technical 有责任者/动作/实际派工证据；external_requested 对应已关闭 requested 资料事项及发起证据。内部实际派工可记录：

```json
{"op":"route-blocker","task_id":"IMPORT","delivery_revision":4,"owner":"实际技术执行者","action":"核对生产者代码及解析分支","evidence":{"path":"/absolute/native-assignment-record.json","sha256":"actual_sha256"}}
```

工具只记录实际派工事实，不代发消息。缺后续责任、技术/请求证据失效、已关闭问题但未派工或材料已到尚未派回，check 返回 route_blocked_work/allow_end=false，closeout 也列为问题；不能仅因 queued 为空就 wait_internal。scope/authorization 不随这些操作改变。

## 强制 closeout 对账输入

```json
{"schema":"coordination-closeout/1","project_id":"warehouse-demo","coordinator_id":"main-session","based_on_revision":7,"inbox":{"mode":"native_snapshot","complete":true,"evidence":{"path":"/absolute/received-native-messages.json","sha256":"actual_sha256"},"messages":[]},"workers":[{"task_id":"IMPORT","owner_agent_id":"worker-import","status":"completed","last_delivery_revision":3,"evidence":{"path":"/absolute/one-shot-wait-result.json","sha256":"actual_sha256"},"receipt":{"project_id":"warehouse-demo","task_id":"IMPORT","owner_agent_id":"worker-import","channel":"native_agent","event_id":"IMPORT:3:blocked","delivery_revision":3,"kind":"blocked","report":{"path":"/absolute/report-r3.json","sha256":"actual_sha256"}}}]}
```

示例 receipt 省略了业务 decision/technical_review，实际业务受阻需完整 envelope。inbox.mode=native_snapshot/fixture/unavailable；complete 仅当此次确实收集了可见原生消息。workers 覆盖所有未完成的已派工任务；status=running/completed/blocked/unknown，evidence 是一次真实原生观察或明确标注 fixture 的记录。worker.receipt 可携带本次 one-shot wait 得到的终态，不能用文件存在冒充消息已送达。

对账先消费 inbox.messages，再按 task.report_path 一次读取报告（不扫描项目），比较终态修订/指纹和已消费消息；可消费 worker.receipt 后复核。缺观察、未知能力、未登记新报告、原生终态/报告不一致或 held receipt 均可见，不能按完整审计放行。r4已重新派工时旧r3终态不能证明当前 worker 状态。结果附原生观察证据、报告发现、消费事件及同一 revision；任一后续修改清空 last_reconcile。输出只是该次观察，不覆盖返回后的新事件或宿主 idle。

未核证原包及 blocked_followup_issues 也会阻止完整 closeout。批量消息逐项收件：后一个错误的身份/冲突映射会记录 issue，不回滚之前已核证收到的消息；原观察文件与 issue 一并绑定最终 revision。

主协调收集观察可调用原生状态/一次有界 wait，不能定时循环等事件。如果宿主只有 wait 才暴露终态，记录这一缺口；不能期待用户发现 idle 或催收。最小事件桥接和独立宿主验收见 platform-boundary。

## v1 副本迁移、恢复、回滚

```text
python -B P --state NEW_V2_PATH migrate --from EXTERNAL_V1_COPY --project PROJECT_ID
python -B P --state NEW_V2_PATH show
python -B P --state NEW_V2_PATH summary
```

保留 entire legacy、原 receipt 指纹/状态、accepted/rejected、queued/held、sequence 和焦点。v1 completed 只作 implementation；任务不会迁移为 completed。新 rights 全 denied，原 release/no-Git 限制留 legacy。旧业务质量 pending 不改成测试失败。旧报告不自动变成 v2 就绪证据；需 worker 新包。

若 v1 active 指向 closed，保留旧历史，将该项显式 active+迁移冲突；`reconcile-focus ITEM --user-reference ACTUAL_REFERENCE` 后再按真实决定关闭，不自动失焦。对历史任务继续收件前，`apply adopt-legacy` 记录原已确认 scope/acceptance/start user_reference；此操作不授 Git 权。需要报告别名时 `apply bind-report` 提供 task_id/path/report_task_id/format/reference；legacy-held 仅读取明确 status=held+open_questions，不能把旧完成报告当新验收证据。

回滚不写回 v1：退出候选路径，继续使用保全的原版本和原 state；候选期间新事件须由主协调显式回放，不能静默丢弃。真实替换前暂停唯一写者、外部保全完整 skill+state+hash、迁移副本并独立验收，再安排路径切换。候选不热替换安装目录、不改真实 state。

## 保全核对

`preservation_check.py MANIFEST.json` 保留原行为。manifest 包含 backup_root、全部 managed_roots（绝对路径）、base、scope、credential_review=completed、ignored_data=included/not_needed、files。每项 source、相对 copy、kind=change/patch/evidence/ignored_data、sha256；change/patch/evidence 三类必须齐全。工具只读检查来源与外部副本哈希、路径和声明的覆盖，不能代替遗漏检查、离线恢复或清理授权。
