# 新任务资源闭环

RESOURCE-FLOW 候选的独立关闭、取消和逐项回收见[新资源入口](resource-flow.md)；新入口消费实际合入 ARCHIVE 的已登记结果；下文 preservation 路径保留原关口。

冻结测试结果或基线内的临时指针，须经[受信分类入口](preservation-boundary.md)作为完整捕获包保全；文件内部标签不授予依赖豁免。

本入口仅用于批准后的正常新代码任务，不扫描或接管旧目录。原主会话先准备批准、任务和路由，宿主按实际能力执行创建；集成者仍须独立审核、核对目标 main，完成外部保全后在当前具体授权内回收。作者上下文在此之前保留。长期集成者不作为开发资源登记。

使用同一个 Runtime、Controller、ArtifactCleanup 和 ResourceBridge。初始化时 RESOURCE policy 的项目根必须包含该 Runtime 的 cleanup_root，cleanup_roots 必须允许该根；两者不相交时在 Git 创建前拒绝。目录由宿主选择，不硬编码机器路径，不扩大到任意目录。

```python
# 调用者身份、真实批准、工具能力来自受信宿主；JSON 不能自授权。
# spec.worktree 是 runtime 的 cleanup_root 内尚未存在的专属路径。
bridge.prepare_development(spec, original_main, authority_ref=frozen_approved_dispatch_ref)
created = bridge.create_worktree(task_id, creation_operation_id, authority_ref,
                                 authorized_integrator_or_main, authorized=True)
# created.status 必须为 registered，才进入原批准/路由/准入/launch。
# launch 或 bind_observed_dispatch 在同一次事务登记实际 author/generation。
# 资源不足时保留原排队；未创建 agent 不填身份。
```

prepare_development 是正常受管入口。原 Runtime.prepare 仍可用于基础排队，但会明确返回 resource_registration=required、managed_closed_loop=false，且登记不全不能宣称 cleanup_completed。它不等于已创建受管资源，也不替代正常入口的创建能力。无实际受管创建能力时保留待核验结果。

后续专属环境使用 create_temporary(task_id, operation_id, resource_id, kind, path, authority_ref, actor, authorized=True)。kind 支持 environment（空目录）、database（空 SQLite 文件）、artifact（空文件），实际存在后才登记身份；应用继续通过自己的正常接口填入内容。父目录只是受管容器，必要未入库材料须列入保全 extra。任何创建异常先保存 unknown 和责任者；重复同一 operation 只读原结果，不再执行创建，也不补登记历史目录。

有限受管测试进程使用 run_process(task_id, spec_ref, authority_ref, actor, authorized=True)。RESOURCE 原入口先注册专属 Job、PID 和创建身份再 resume；CORE 的创建 intent 关联该请求，返回后绑定实际 process/nonce/attempt。未启动的请求仅为 queued，没有虚构 PID；排队或未知保留当前技术责任者，不能重新 run_process 来盲重启。RESOURCE 的实际观察/准入仍用其公开入口；明确 pump/观察后由 observe_process 只读实际请求身份并承接本次创建 intent，不重发进程；这不是轮询器或持久服务。当前桥接只在已证明整棵进程树退出时聚合清理，运行中或未知进程保持 pending_cleanup，交集成者按原 RESOURCE 协议处理。

正常开发允许 HEAD 前进，工作树登记使用仓库、路径、目录身份和专属 branch。回收同时核对当前 HEAD 与已测、已核验合 main 的提交，脏/锁定/换身份工作树仍拒绝；所有 Git 调用只对该命令使用 core.longpaths=true。

完成仍走既有 record_main_inclusion 或 real_use 入口，不从资源记录推断任务完成。不增加取消、验收、合入或部署权限。原清理状态契约保持 task_completed 与 cleanup_completed 分离，失败/未知保留 owner 和下一动作；同一动作不重复执行。

没有全局 delivery 时，资源入口可复用当前任务已冻结的 approved-dispatch-plan/1：原 main、独立 integration_agent、任务/worktree/branch 必须准确匹配，且 authorized 明确含 bounded_new_task_resource_cleanup。正常 prepare 时保存 authority_ref；已通过原 observed/CLI 入口绑定的本次任务复用其现有冻结批准，不补写历史记录。每次资源创建/观察/保全/关闭/清理和许可核验重验该引用。task_id 明确传入 observe_executor 的任务观察调用；不获得普通 apply、其它任务、resume 或全局写权。原 _actor、验收和 CI 关口不变，无 CI 项目不为资源清理造 CI。

```python
preservation = runtime.preserve_task(
    task_id, new_absolute_external_directory, handoff_ref, delivery_actor,
    scope="host_verified", extra=[necessary_non_git_artifact_ref],
    remote_verification=bridge.observe_remote_completion(task_id, delivery_actor),
)
result = runtime.reclaim_development(
    task_id, cleanup_operation_id, preservation, actual_host_adapter,
    bridge.cleanup, authorized=True,
)
# 正式 close 经 CORE 确认后才由 bridge.release_agent_quota 释放本插件许可。
```

保全入口在同一账本绑定当前 task/revision、source、交付、独审、集成、PR 和 main 事实；递归保全必要交付材料引用的准确字节及交接，外部副本不改写原报告。宿主显式提供有价值且不能从 Git 恢复的 extra，确认必要材料完整；不把所有临时产物永久保存，也不据复制文件推断业务知识无损。清单及副本不在任何将被回收的资源中。

JSON 默认是数据：普通相对或绝对 `path/sha256` 清单、失败快照和绝对路径字符串不自动成为外部依赖。入口 handoff/extra、既有 `evidence/report/proof/authority` 等明确引用槽及 JSON 根层的绝对引用清单才按引用契约读取；引用槽内的相对、缺失或变更文件仍拒绝。需要跨文件保全的材料应声明在 evidence 或入口 extra 中，不能仅放在自定义数据字段。普通分组和列表继续递归查找明确引用槽，同一文件引用只保存一次，引用附加元数据中的证据也会遍历。

既有引用槽（包括 evidence、proof、report、authority）中的内联容器统一保留声明上下文，不能用 `kind=result`、宿主日志 schema 或再包一层普通分组隐藏其嵌套证据；普通清单数据仍不会因处于同一容器就自动成为引用。读取被引用文件的 JSON 内容时重新按该文件的数据契约判断，保留真正捕获快照的数据边界。

证据 JSON 内按当前 CORE schema 序列化的状态、`dispatch-host-result/1` 日志、`result/host_call/host_result` 宿主包及完整结构的标准派工请求是捕获数据，包含它们的证据文件仍逐字节保存，但不把包内测试状态和调用上下文提升为本次真实外部依赖。源码 inventory 的 root/files/extra/digest 保留原值，Git 已跟踪的源码补充身份不逐任务备份，必要非 Git extra 仍严格保全。保全副本的 original 是原身份，不要求已确认清理的原路径继续存在。以上按既有数据契约判断，不取决于文件是否存在；显式引用仍优先校验，即使引用附带宿主包元数据。保全失败保留资源和已写副本，不登记成功或执行回收；任务完成、云端恢复、无活动/共享引用、边界及授权门禁保持不变。

开发和审核期间，report_for 继续检查实际源清单与测试绑定。正常完成后，先由 observe_remote_completion 实际读取批准仓库 origin 的 main，并核本地已知提交图确实包含已测 head、对应 PR 身份及作者树无未提交成果；对象尚未同步、远端未包含、仓库不匹配或作者树脏都拒绝。不自动 fetch/push，不从本地 merge 推定云端完成，file remote 仅允许明确 fixture。

带该远端核验的 canonical preserve_task 只保存必要交付/测试/审核/非 Git 材料和源清单，Git 可恢复的正式代码不逐任务全量备份。其后 report_for 使用已核云端提交与同字节持久化材料，原工作树不存在、目录待清理或原路径被新目录复用都不撤销已完成事实；它们也不授予删除新对象的权限。保全损坏、当前交付/审核/main/授权绑定变化仍拒绝。读取原引用可用 evidence.preserved_read 的有限上下文。

正常同树返工的 R1 报告可能引用现已变为 R2 的跟踪文件。首次 `preserve_task` 遍历已接受的原任务报告时，若原路径字节不再匹配，只能从该不可变报告绑定的 Git head 解析历史字节：重新核对 canonical 回执、作者/项目/任务/修订、原 source 清单及 SHA、注册工作树与可信项目仓库的共同 Git 身份、报告 target→原 head→任务合入 head→main 的祖先关系，以及准确的跟踪相对路径和普通 blob 类型。原始 blob 字节必须匹配原 SHA；不应用 checkout/filter/换行转换，不用任意外部备份或报告里自声明的仓库。

历史项仍进入同一 `preservation.json.files`，保留原 `original.path/sha256`，并增加 `git_source`（原 report 引用、receipt_id、项目/任务、可信 repository、commit、relative 和 blob）。只复制实际被引用的历史工件，不全量复制旧源码。普通 Git source 清单不是额外外部依赖；必要非 Git 证据缺失、未跟踪文件、跨任务/仓库/提交/路径/SHA 不符仍拒绝。旧报告、回执和当前 R2 工作树均不改写。后续 eligibility 和有限 `preserved_read` 对历史项复用同一解析入口，重新核来源和保存副本的准确字节；`git_source` 字段本身不授予例外。保全不完整、活跃/共享/受保护资源仍不能回收。

旧报告的 `null+extra` 长路径源码补充，只有在原 canonical 报告上下文中经同一 Git 解析器核证后才省去源码复制，并在同一清单的 `git_sources` 保存 original 和已核证来源，eligibility 再核证。跳过源码补充不进入已复制集合，同文件后续出现在 `entry.artifact` 或显式证据时仍实际保全。null/extra 字段本身不代表已跟踪；无法核证、未跟踪或非 Git 的必要补充继续核原字节并复制，缺失/篡改仍拒绝。此兼容仅承接既有源码清单，不迁移或改写原报告。

中断后的保全沿原 continuation `operation_id` 和同一 destination 恢复，核对原 intent 的完整绑定，并逐字节检查已有部分副本；不能另造 operation、覆盖损坏副本或重做关闭/释放/删除。已有普通路径字节仍匹配时继续原有复制规则；工作树消失只能沿已确认的本任务工作树清理事实读取保全历史。可信 Git 对象或原报告来源无法核证时保留阻断，不提供通用历史迁移或放宽回收授权。隔离 R1→R2 与部分恢复回归不代替真实宿主正式保全和回收验收。

native 关闭与目录清理分开：按[ROOT 原生关闭控制接线](../../skills/isolated-parallel-development-coordination/references/native-control.md)保存 intent、ROOT 一次领取并正式调用、集成者 reconcile。交接和安全关口完成后，目录清理失败仍保留 pending_cleanup，不能拖住无活动工作的原生作者。无本地 permit 不补造许可，平台容量数仍未知。

## 受信 RESOURCE profile

RESOURCE 实例必须由受信项目入口显式引用，不能按消息猜路径、私建准入状态或将旧 actor 补记成已获许可。当前正式接线字段为 explicit-resource-instance-profile/1：state（绝对路径）、policy/authority（path+sha256）、project_id、cleanup_root、external_evidence_root；profile 自身由受信 CURRENT_ENTRY 引用。它只标识现有实例，不是第二份任务队列。

宿主先核该 profile hash、项目和 CORE cleanup_root，再用 Controller(profile["state"]).read() 核现有 state.policy 与 profile.policy、policy.authority 与 profile.authority 一致。之后构造 ArtifactCleanup 与 ResourceBridge(runtime, controller, artifacts, external_evidence_root)。未声明或不一致时停止准入，交集成者完成必要受信配置；不以 fixture 生产身份替代，也不从空实例推定旧 actor 已准入。

真实宿主 native spawn/close、模型容量、进程树退出和目录空间分别核证。单元用例的 host/PR/CI 事实明确为 fixture；实际 Git、Windows Job 与临时文件操作也只代表所列隔离资源。当前开发者不能关闭自己来证明产品验收；本任务真实作者回收留给合入、保全核验后的集成步骤。
