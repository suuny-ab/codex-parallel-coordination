# RESOURCE 1.1：隔离受管进程组件

复用昨日 plugin-r6 的 Controller、Windows Job/IOCP、命名互斥，不改变业务计算参数。唯一写入范围为本 resource 目录；昨日源码、唤醒证据及真实项目全部只读。

组件只存资源请求、许可、实际进程身份、等待原因和资源历史；不存业务任务 stage/验收/决策。project_id/task_id/attempt_id/launch_identity 是 CORE 提供的关联身份，资源状态不等于任务状态。

保持显式策略及授权证据：机器配置 max_parallel、物理/提交安全余量、允许项目根/执行文件、资源独占键。新增 resource_kind：temporary_auxiliary 才可能获准 idle 回收；compute/acceptance/regression/shared_tool 即使 caller 声明 reclaim 也保护。既有外部 PID 不提供任意 attach/stop 接口。

进程由唯一启动入口在 suspended 状态加入新命名 Job，写入 PID+creation time、nonce 后才 resume。后台计算只有本次调用的有限前台控制器/Job 事件等待，不建立服务、定时器或闲置唤醒。内存不足保留 FIFO，不修改 peak_bytes、argv 或业务搜索预算。

一致性：命名互斥串行多进程访问，加原有显式写锁及原子文件替换；遗留锁和损坏状态拒绝，不自动抢锁或初始化。未确定启动、丢失树证明及 PID 身份变化保留许可，不超时重启。

核验点：物理/提交分别不足；两个项目并发抢独占许可；重复申请/释放；父退出子存活；异常退出；monitor 崩溃；PID 复用；运行/待验收/共享工具保护；实际树去重；损坏/锁/恢复；只读 snapshot。实际低内存用明确注入，不逼机器 OOM；实际受管进程只用本轮临时夹具。

接口：自包含 component 包，Controller.initialize/submit/pump/claim/recover/idle/reclaim/snapshot；CLI 同名。submit(run=False) 只入队，claim 是许可预留而非进程已运行；pump 是统一启动及前台事件监控。CORE 只读 snapshot，不直接改 resource state。资源 schema 升为 coordination-resources/2，旧 v1 文件只读拒绝，不能被静默覆盖；本轮均新建隔离实例，不迁移 live。

边界：仅管理通过本入口新启动、明示登记的临时资源；不提供对绕开插件进程的全机硬内存限制。普通 Job IOCP 通知无必达保证，缺失时下一显式恢复入口核实事实；缺少可靠外部内存事件不承诺自动接续。真实宿主组合与安装由唯一集成者重新验证，不能叠加组件测试计数。

## 用户新增：native agent 名额

真实轨迹：三次 spawn 被 thread limit 拒绝，已交付执行者仍占位；四名完成者随后已被复用，本轮全部有工作，不可关闭。此轨迹只作脱敏事实夹具，不存真实对话转录。

资源分类明确拆分为 agent 席位、模型请求容量、受管进程 CPU/物理与提交内存、验收环境存储。close_agent 不等于杀 Codex/MCP 或释放所有 RAM；工作树/历史/DB/验收环境不删不归档。

经与CORE对齐，native agent 身份、lease/关闭/恢复意图和结果只在CORE权威账本。RESOURCE不实现第二份AgentSeats生命周期表、不调用host close/resume。本轮只增加agent_execution_slot容量许可/FIFO，保存request/attempt/permit与资源grant/release。CORE负责在实际操作前互斥新派工，核无运行后代/未完工作、保全与用户焦点保护；确认正式close或明确spawn未发生后，先持久化canonical结果，再调用RESOURCE release。

正式 side-effect adapter 归 REPOSITORY bridge/CORE生命周期入口：observe/close_agent/resume_agent 必须实际确认 native 身份与终态。发送成功不算关闭；失败/未知保持许可，不盲重试。RESOURCE release须经注入的canonical receipt verifier核对精确project/task/attempt/permit及已确认结果，不接受裸JSON自授权限。重复release幂等，resume新attempt重新准入，释放事件推进容量FIFO。fixture在stub CORE中模拟身份变化/后代/保全等，RESOURCE验证不能凭idle或completed字段释放，实际host仍未验证。不新派测试agent，不关当前四名作者。

## 2026-10-31 用户纠正：旧提前关闭草案已撤回

上文“本轮已交付即可关闭”的早期讨论/接口草案撤回，保留为历史，不作为现行授权。暂停、等待验收、报告completed或App聊天idle均不足以提前关闭agent；agent记忆不得假定可无损resume。

现行规则：任务合入目标main且核对后才结束。CORE在唯一账本核对实际合并目标/SHA、必要代码/报告/未入库证据外部保全及知识交接、无运行工作/活动后代/发布依赖，且资源明确专属该任务、无共享或保护使用；然后才可能在另有清理授权下执行正式回收。用户讨论阶段没有新增真实清理许可。

RESOURCE的confirmed_closed许可释放仅接受CORE verifier瞬态结果同时证实上述条件，不能用旧bool True通过；不把这些事实保存成第二份任务阶段。confirmed_not_started只是释放经核证从未启动的准入预留，不关闭上下文或环境。工作树/历史/DB/验收存储不自动归档删除，实际清理仍待范围与验证。本轮只有注入fixture，当前四执行者均有任务保护。

## 2026-10-31 用户确认：合并完成后的专属资源清理

用户已确认开发自测、验收、整合审核、合main核对、必要记录保全、专属资源回收归档的顺序。task_completed与cleanup_completed仍只在CORE；RESOURCE只记登记资源的单次操作事实及pending_cleanup，不因清理失败丢失任务。后续bug从最新main开新任务，部署独立使用冻结制品，不依赖旧开发环境。

本轮允许实现/隔离验证，仅对自己登记的可丢弃夹具目录、临时库、Job进程回收；不授权任意真实历史资源清理。共享引用必须清空、保护资源不得直接回收。未知操作结果只观察对账，不盲重试；重复调用不重复删除/终止。持久任务环境的回收通过同一CORE完成证明门禁；短时计算结束释放其进程许可仍与持久agent/工作树/环境清理分离。真正Git工作树/宿主归档由REPOSITORY适配，RESOURCE不以任意目录删除冒充。
