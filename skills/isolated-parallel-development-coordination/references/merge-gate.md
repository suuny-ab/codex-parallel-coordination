# GitLab MR 离线门禁与本地 CAS 范围

支持 GitLab MR 的身份和证据绑定，不调用 GitLab、gh 或网络。其它 provider 目前拒绝；扩展时需实现对应原子条件并独立验证，不能仅改 provider 字符串。

每个需求由原工作 agent 独立维护一个 branch/MR，只获取并对齐最新 main、解决冲突、重跑自测及本需求对 main 的集成回归。不提前整合其它未合任务，不等待它们，不创建多需求组合 MR 或要求子代理互相对齐。功能验收后仍由原作者维护同一 MR；main 落后时退回原作者同步重测并更新同一 MR 重新送审。

主会话只与用户确定范围/顺序并授权；交付者独审最新 main、冲突、源 CI、源码和验收证据，唯一串行合 main。失败直接交原提交者，不代修功能、不另建修复 MR；同事 MR 同理。必要版本元数据仅限明确发布范围且需非作者独审。门禁只复核离线绑定，不查真实 GitLab CI、不代替实际源 CI 审核。发布另有独立准确 SHA/范围授权，不解除既有禁令。见 [交付委托协议](delivery-delegation.md)。

```json
{"op":"mr","task_id":"IMPORT","mr":{"provider":"gitlab","id":"该任务同一个MR的完整URL或稳定标识","source_branch":"authorized-task-branch","target_branch":"main","head_sha":"actual_task_head","target_sha":"actual_tested_main"}}
{"op":"integration-start","task_id":"IMPORT"}
{"op":"integration-result","task_id":"IMPORT","report":{"path":"/absolute/integration.json","sha256":"actual_sha256"}}
{"op":"review","actor_id":"authorized-delivery-agent","task_id":"IMPORT","evidence":{"path":"/absolute/delivery-review.txt","sha256":"actual_sha256"}}
```

功能接受、source/evidence、自测/quality、MR head/source branch/tested main、集成、交付独审/同 revision 源 CI、单一委托/顺序及四项 Git rights 都满足后才 gate。工作树须干净，实际 HEAD/branch 匹配且包含 tested target；真实 main ref 仍等于 target。main 变化直接交同 worker rework、更新同 MR 并复测。门禁是一次观察，不保证稍后远端 merge 安全。

```text
python -B P --state STATE --actor DELIVERY gate IMPORT
python -B P --state STATE --actor DELIVERY reserve IMPORT --token UNIQUE_LOCAL_TOKEN --expected-revision JUST_CHECKED_REVISION
python -B P --state STATE --actor DELIVERY release-reservation --token UNIQUE_LOCAL_TOKEN --reason "新消息或main变化，重新核证"
```

ledger 只容一项 merge_reservation，写锁使争抢可见。reservation 固定 task/revision/source_digest/head/target/MR id；后续 ledger 修改导致本地 merge 拒绝，必须释放重查。另一账本或外部写者不受这个本地预留控制。

## 仅本地临时裸库演练

初始化显式给 `--main-repo ABS_BARE_REPO --local-cas-root ABS_SANDBOX_ROOT`，bare repo 必须在这个专门 sandbox 内；先在隔离 fixture 中准备 commit objects（工具不 fetch/push）。以下命令只更新本地 bare main ref，要求 task Git rights；不能指向真实项目或解释为远端授权。

```text
python -B P --state STATE --actor DELIVERY local-cas-merge --token UNIQUE_LOCAL_TOKEN
```

命令重新检查所有门禁后执行 `git update-ref refs/heads/main HEAD EXPECTED_OLD_MAIN`，expected-old-value 是实际 ref 的原子 CAS；检查与更新之间另一个写者先更新了 main 时，Git 拒绝本次更新。工具核对 main 后才 phase=completed；deploy 权限不改变。实际测试使用两个本地任务从相同 main 出发，A 先入后 B 旧 target 被拒；B 同 worker/MR rebase、重测、新验收后可合；两个独立 update-ref 进程同时竞争只能一个成功。

Git ref 和 JSON 不是一个跨文件事务。如果 CAS 已发生、账本写入前进程崩溃，保留 reservation，先保全，再 `recover-merge --token TOKEN`：只在实际 main 精确等于该已测 head，且原门禁/证据仍有效时确认完成。其它 ref 变化或证据失效不自动恢复。工具不回退 main。部署从不随 merge 自动执行或授权。

**真实 GitLab 原子门禁未实现/未验证。** 本地 CAS 仅证明本地 ref，不证明 GitLab MR API 能以同样方式比较 target main 与 source/head。需要另行验证 provider 原子条件、保护分支/合并队列或等效服务端门禁，再接入真实合并；不能把 `sha=head` 校验当成同时校验 target 的证明。本次不发真实 GitLab 请求、不合 JLJ。
