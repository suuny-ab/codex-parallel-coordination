# 平台能力与未启用的适配原型

候选 v2 增补于 2026-10-30。下面 2026-10-29 的版本/能力是历史核查，不是本轮重新证明。R6 当前能力/职责见末节与 delivery-delegation；本轮作者不运行 Git、不安装或启用适配器，不沿用历史 CAS 通过代称新源码通过。

核实日期：2026-10-29。官方说明可能更新，复用时应核对目标宿主的当前版本、运行位置与事件覆盖。

- [OpenAI 官方 Hooks 文档](https://learn.chatgpt.com/docs/hooks)：同步 Stop 支持 `decision: block` + reason，但语义是创建新 continuation prompt 继续运行，**不是拒绝该 turn 或撤回已生成 final**。`stop_hook_active` 标明已经续跑。其它 Stop 返回 `continue: false` 可压过续跑决定。异步 hook 不能控制触发它的操作；SessionEnd/Interrupt 也不能代替这里的结束前检查。
- [官方配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)：`features.hooks`、inline `[hooks]`/hooks.json 与 notify 是不同接口。notify 只是通知命令，不能称为结束拦截。
- [官方插件打包说明](https://developers.openai.com/plugins/build/plugins)：插件可携带 hooks，但安装不等于信任。非托管 hook 须审核并信任当前定义，变更 hash 后会跳过直到重新信任。云端编排不支持普通本地/插件 command hooks，应核对宿主适用性。

本机只读检查得到随桌面应用安装的 `codex-cli 0.165.0-alpha.1.2`，`codex features list` 显示 hooks stable=true。本机生成的 app-server JSON schema 含 hook 列表/事件协议。这支持“存在 hook 接口”，不证明当前主会话已经信任/加载我们的适配器或真实完成过续跑。未修改全局配置、安装 hook/plugin、重启应用、启动守护进程或直接改内部数据库。

## 可测范围

`scripts/stop_adapter.py --state STATE` 读取 stdin 上的原生 Stop JSON fixture，只读状态：

- 未委托时仅 coordinator_id 的 Stop 执行旧检查。R6 委托后主会话只处理用户焦点/eligible，技术收尾归交付者，不将 CI/收件问题续跑到 root；SubagentStop/其它会话返回空 JSON。
- check 要求继续时，首轮返回官方 block/reason 续跑形状；待用户回答/操作或无可办事项返回 `{}`，不自动结束人事项。
- 已续跑仍未修复协议时只返回警告，避免无限续跑；状态丢失/损坏返回警告并明确未核证。这是有界、失效可见的协议适配原型，**不是 fail-closed 硬门禁**。
- fixture 已验证输入输出、跨线程排除、待用户与空闲放行、续跑上限、损坏状态告警及不修改状态。没有在真实 hook 宿主触发，也没有保证 final 展示之前运行、事件丢失后的自动唤醒或全故障监听。

## 如需真实启用的最小增量

由总控另行评估：在目标本地宿主核实身份/状态映射与原生子 agent 事件；在专用 fixture 主会话中加载一个**同步 Stop** handler 指向已测适配器和固定状态路径，审核信任当前定义，再观察 eligible→续跑→递上一项、active待用户→不续跑、工作子agent→不受阻、故障/多hook冲突的真实行为。使用已测的绝对解释器和脚本路径及 Windows 正确引号，限定主会话，不使用 notify/async 冒充门禁。

只有宿主实测后才能声称该目标环境具备有界续跑提醒；要保证所有 final 均不可静默结束，需要宿主提供真正 final 发送前、可拒绝并绑定状态 revision 的接口或插件接口增强。本轮未证明存在这种接口，不为它建立重型编排服务。

## v2：check 的能力缺口与一次 closeout

真实失败轨迹：HISTORY r3 报告 status=held、存在业务未决，但旧账本仍为 r2 completed/rejected，主协调只运行 check 就以 wait_internal 结束。用户发现 worker idle 催问后，主协调调用一次 wait_agent 才拿到 completed=held。文件副本证明报告/账本不一致；“是否 idle 时宿主已推送”“消息究竟未送达还是主协调漏消费”只有平台原生事件日志才能区分，不能根据 idle 或报告文件存在断言根因。

v2 closeout/reconcile 先消费责任者已收到的消息，收集在途 worker 的一次状态/终态观察，再按 report_path 对账，同 revision check。R6 技术责任者是单一交付 agent，主会话只接用户事项/整批结果/重要例外。check 只查 ledger；观察不足 reconcile_required，不伪造事件。工具不自己调用宿主 wait 或唤醒会话；宿主仅显式 wait 暴露终态时，由交付侧一次有界收集并记录覆盖缺口，不让 root 反复跟进。禁止定时循环或用户催收。

后续用户说“你自己对一下……所有缸字段应一致”，含义是进一步横向技术排查。原子 `redispatch` 同时关闭该 decision、清除人类阻塞、记录原 worker running/expected r4；不把 withdrawn 当完成或字段含义确认，不另建 manifest running 状态。再次升级业务问题前，主协调需核证真实数据横向比较、生产者和解析器、已有定义及替代解释；hash 只验证证据身份，不证明排查充分。

## 最小事件桥接方案（未接入宿主）

1. 派工时记录原生 worker handle、task/project、报告路径和修订分配；在主会话可接收的 native completion/blocked 边界取得原始 message ID、worker ID、终态和报告引用。若只有 one-shot wait 输出，明确 provenance=one_shot_wait；不得标为 unsolicited push。
2. 当前指定的交付收件者将确已收到的消息稳定映射成 receive envelope，串行持久入缓冲。host completed 只表示执行停止；held/blocked 映射 blocked。映射与原始观察一起保全后 drain，不把接受发送或对方确认升级成最终交付。
3. 结束前导出 coordination-closeout/1：inbox 已见消息覆盖证据、每 worker 的一次原生状态证据及 terminal receipt，`based_on_revision` 绑定刚读取账本；工具一次对账，给出漏件/未登记报告/held 和单一 next。收尾检查后再来新事件时旧结论失效。
4. 缺 native adapter 时明确 unavailable，不能用文件读数伪造原生送达保证。首版只提供这个结构契约、输入校验和进程 fixture；没有全局 hook、插件、平台内部数据库或常驻服务。

## 主协调另派独立审阅者的行为验收计划

本轮实现者不得另派 agent。主协调在隔离目录提供候选 skill 和 `demo.py --prepare-only --output NEW_ABSOLUTE_DIR` 的原始包，使用包内 REVIEW_REQUEST.md 派独立审阅者；不要先给预期缺陷、修复答案或本文验收结论。先离线检验是否能恢复、发现终态/报告漏件、处理一个问题并原子退回技术排查，无需 JLJ 业务知识。

之后由有授权的主协调做真实原生工作子agent E2E（不是独立用户对话）：

| 场景 | 必留证据 | 可判通过的行为 |
|---|---|---|
| worker 写报告并停止，主会话正在处理另一项 | 原生 handle、发送/接收时间和 message ID、两端终态、report hash、ledger 前后 revision | 已收到的消息被消费；active 不被打断；关闭当前项立即递交下一项 |
| worker host completed 但报告 held | 原生原文、报告、归一化 envelope、技术排查核证 | task blocked 与技术局部通过分开，明确业务问题进入单项队列 |
| 主会话不调用 wait，worker 停止 | 宿主原始事件/终态记录 | 观察是否真实自动送达；未送达记能力失败/限制，不能让用户发现 idle 才工作 |
| 收尾显式一次原生 wait 才暴露终态 | wait 调用/返回、之前消息覆盖、对账输入/结果 | closeout 消费终态并发现旧 ledger 漏件；不得声称已证明自动唤醒 |
| 用户要求先全量技术对照 | 用户原文、同 worker 重新派工、state 和 summary 重载 | withdrawn、developing/running、human blocker 清除、expected r4，旧范围和权限不变 |
| 重启/重复/迟到、active源码改变 | 持久 ledger、原文重放和指纹变化 | 幂等、旧修订不回退、证据失效不能接受、active 保留 |

自动推送不可用并不否定离线账本行为，但必须把宿主能力列为未通过/待接入；真实任务使用前必须验证至少一次可信原生观察→映射→入库→closeout→单项呈现链路。Stop fixture 只说明输入输出兼容；v2 要求当前 revision 的 native_snapshot 对账，fixture 不作为真实核证放行依据。它仍不是 final 硬门禁。

## R3 独立复测增补

原独立复核结论为 changes_requested，原文件保留；作者修复自测不等于同一审阅者复测通过。复测应重放 IR-001 错误 hash→原 ID 正确映射，以及 IR-002 技术证据在收件后、激活前、等待用户时的变更/删除/恢复；核对旧候选未消费 poisoned canonical 的副本恢复和已核证事件不可覆盖。

另有用户补充的真实 r4 漏接：报告 not_a_new_business_decision 仍依赖上游字段源码/说明；主协调在 UI 返工答复后 final，未安排外部资料协助。此轨迹来自用户叙述，本轮未读取新的 r4 报告或原生事件日志。离线演练须覆盖：r3 问题退回技术且无语义授权→全量横查后的两类差异（11/4 为该案例提供的数量线索，fixture 明确使用合成数据）→主协调审核→UI active 保持→UI 关闭→一个可转客户 IT 的 assistance 被 next 激活。普通技术 blocked 只要求责任/派工证据，不自动给用户；请求只有真实发起证据才记 external_requested。

真实宿主复测还须观察 UI 结束后的 closeout 是否核对每个 blocked 任务的下一责任者、动作和已派工/外部请求情况，不能只重放 queued JSON。工具不会实际发送客户请求、自动唤醒或启用定时器。由主协调安排原独立审阅者，不由作者另派 agent。

## 进度兜底 R4 的新增边界

本轮 R4 指作者交付修订4，与上节历史任务 r4 场景不是同一编号。R3 独立通过仅绑定 f5c01495... 的21文件，不能用作新源码通过结论；R3-U01/U02 限制继续保留，未启用或安装任何能力。

一次 closeout 的公开输出按 focus -> 已核清的一个 eligible -> 无事项进度分支；未消费新终态时必须先收件。执行/排队数量来自本账本显式派工及同轮观察，不保证宿主外部资源真实占用，也不能发现工具未见的消息或自动唤醒 idle 主会话。支持用 worker_capacity 约束本实例分配数，不查询其它项目或 OS 进程。

新增回放包括：HISTORY r4 依赖被用户收窄范围解除，同 worker r5 完成后形成验收；展示数组只归一化字段类型，不改 canonical 或虚构r6；用户明确接受后为待整合。权重 r3 UI接受与整体质量分开，先排队、后恢复r4执行；操作日志 owner 关闭但入口保留时仍维持当前页面验收焦点；MR修复及工具作者继续执行，作者和复核不重复计业务任务。最新主协调 revision84 情况仅按用户提供轨迹构造隔离数据，不代表作者重新读取/核实了live状态。

`progress_demo.py --scenario warehouse|portal --output NEW_ABSOLUTE_DIRECTORY` 用两种任务结构、有限进程实际小型断言与 CLI 演练生成用户摘要。所有 native 状态明确是 fixture，真实宿主事件投递、页面服务存活和客户排产正确性未由这些演练证明。真实r5外部报告只读副本可验证18源码/622证据引用结构保持，不读取这些真实业务文件或执行其命令，不重复声明客户测试通过。

## R6 实际通路与新职责的分层验证

交付闭环与字段契约见 [交付委托协议](delivery-delegation.md)。`delegation_demo.py --scenario warehouse|portal --output NEW_ABSOLUTE_DIRECTORY` 在两个不同实例用真实 CLI/有限 Python 子进程验证新角色收件、原主会话单项用户处理、技术/资料等待和无 root 接力。其传输/CI/ref/发布均是 fixture；R6初交时五项Git未执行，后续补跑在新证据目录保全，旧R6报告不改。R7按新明确授权重新完整运行含这五项的套件，不借用旧结果。

本批用户补充的 author-route-r1.json 只证明 App thread send accepted；direct-handoff-r1.json 另含对方确认，所以可证明那一次双向联系，不能推导任何后续执行完成。作者与原审阅者在当前 active turn 也实际通过已有 App thread 消息互发并收到明确 peer acknowledgment，无新对话；这不是 idle 自动唤醒、sender 身份认证、main/部署或 R6 独审通过证明。具体文件与 SHA 在工具 R6 外部报告，不写真实项目配置进 skill。

Kepler 的 R5 A 影子链路保留独立：parent-forwarded wait 返回的是原 worker 直接交付接管完成，provider_event_id=null，并非更早 CI 完成事件或主动 idle 推送。真实 MR 报告缺 task_id/project_id/delivery_revision，自测 passed_local_and_linux_ci，数组 source 指外部清单、entry 为字符串，缺质量结论和自测源码绑定。R5 实测 quarantined / canonical0 / unverified1 / needs_reconcile；R6 单独副本复跑保持该边界，不猜补 passed、业务接受或新作者修订，也不开发通用转换器。通路存在与报告契约就绪是两个结论。

## 作者 R7：额外边界修复与独立修订编号

review7 检验的是冻结 author6；其完整116（含5实际本地Git）通过不能覆盖额外陌生边界。两处P1为旧buffer在委托变化后越scope消费、以及App task_final凭据过期仍可验收。作者R7在新implementation-r7修复，不回写R6证据。新的同一审阅者复测必须绑定新source digest和新review编号，不能用已存在的review7报告冒充author7通过。

新补跑Git只在新一次性根init/commit/ref-CAS、仅本地file，无push/真实JLJ；进程argv guard需在本机实际观测命令路径，不能把Windows audit executable字段未命中的0计数包装为强制隔离。Popen包装必须保持class兼容asyncio继承。原始运行器错误、失败和修正复跑分别保留；这两项运行器设置错误不是产品源码缺陷。
