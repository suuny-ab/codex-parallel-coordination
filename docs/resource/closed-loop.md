# 新任务资源闭环

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
    scope="host_verified", extra=[necessary_untracked_artifact_ref],
)
result = runtime.reclaim_development(
    task_id, cleanup_operation_id, preservation, actual_host_adapter,
    bridge.cleanup, authorized=True,
)
# 正式 close 经 CORE 确认后才由 bridge.release_agent_quota 释放本插件许可。
```

保全入口在同一账本绑定当前 task/revision、source、交付、独审、集成、PR 和 main 事实；递归复制这些材料引用的准确字节及必要交接，外部副本不改写原报告。宿主显式提供 extra 并确认其必要材料完整，不能据复制文件推断业务知识无损。清单及副本不在任何将被回收的资源中。

report_for 在真实文件仍存在时继续检查原字节/源清单。仅当同任务专属工作树经原清理入口确认删除，且外部保全仍绑定当前完成事实时，读取已保全的源清单；缺失证据也必须位于已确认清理的原路径且有同 SHA256 的外部副本。无记录删除、源目录替换、原文件改字节、外部副本损坏、任务绑定变化一律不降级放行。需要读取原始引用的审阅代码可以在 evidence.preserved_read(state, task_id) 内继续使用 artifact/load_bound；此上下文只在当前调用有效，不改变报告路径或全局账本。

真实宿主 native spawn/close、模型容量、进程树退出和目录空间分别核证。单元用例的 host/PR/CI 事实明确为 fixture；实际 Git、Windows Job 与临时文件操作也只代表所列隔离资源。当前开发者不能关闭自己来证明产品验收；本任务真实作者回收留给合入、保全核验后的集成步骤。
