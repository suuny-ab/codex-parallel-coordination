# 派工与 worker 报告模板

## 派工

主协调给原生 worker 一份完整任务单，常规技术问题由其自行解决。资源与 Git 权限必须来自实际用户授权；没有相应授权时 authorized_rights=[]。以下是个人项目无凭据模板，绝对路径按实例填写：

```json
{"task_id":"IMPORT","owner_agent_id":"native-worker-id","worktree":"/absolute/isolated-import-worktree","branch":"authorized-task-branch","resources":["database:project-import-copy","port:project-import"],"scope":"只将已确认格式导入既有表单；不改数量含义","acceptance":["正常输入成功","错误输入显示已确认的业务提示"],"user_reference":"原主会话真实开工及本任务Git权限依据","authorized_rights":[],"report_path":"/absolute/external-delivery/report-current.json"}
```

`dispatch` 只登记，不创建分支/agent/环境。回执直接交授权交付者，按实际宿主通路声明能力，既有工作 agent App thread 可用但不得创建替代对话。委托、路由和 accepted/acknowledged/task_final 的模板见 [交付委托协议](delivery-delegation.md)。报告发现路径可指当前交付指针；正式证据包不可变。由交付者 bind-report 记录新位置，不让主会话搜索目录。instance config 可用 init --project-config：

```json
{"main_branch":"main","local_main_repo":"/absolute/local-main-ref-repo","protected":[{"path":"/absolute/frozen-input","sha256":"actual_sha256"}]}
```

原生派工消息应覆盖：确认范围、非目标、验收条件；基线/隔离 worktree/分支；保护数据/服务/资源与共享文件 owner；必须测试及报告路径；只读参考材料；事件路由；本任务真实权限。worker 自行编码、准备环境、自测、生成报告、检查证据，再一次性原生交付。真实业务未决先横向检查数据和解析调用链，带技术证据交主协调核定是否确需用户。任何报告 status=held 不得以宿主 completed 消息升级为完成。

## 完成报告规格与自检

```json
{"project_id":"warehouse-demo","task_id":"IMPORT","delivery_revision":1,"summary":"导入结果可验证的简明说明","source_root":"/absolute/isolated-import-worktree","extra_sources":[],"evidence":["/absolute/test-attempt-1.log","/absolute/test-rerun.log"],"self_test":{"status":"passed","command":"实际已经运行的完整命令"},"quality_review":{"status":"passed","scope":"本次已确认验收条件"},"entry":{"kind":"file","target":"/absolute/review-entry.html"},"steps":["打开入口","执行关键操作"],"expected":"用户能观察到的具体结果","limitations":[],"attempts":[{"command":"实际命令","exit_code":1,"evidence":"/absolute/test-attempt-1.log","repair":"真实修复说明"},{"command":"实际复跑命令","exit_code":0,"evidence":"/absolute/test-rerun.log"}],"git":{"head_sha":"actual_task_head","target_sha":"actual_tested_main_sha"}}
```

运行 `pack SPEC --output IMMUTABLE_REPORT` 自动枚举完整源码和证据 hash，绑定 self_test.source_digest。Git 根枚举所有 tracked 和 nonignored untracked 文件、删除标记；ignored 必需资产/输入放 extra_sources。非 Git 根枚举所有文件；报告、日志、ledger 放源码根外，避免生成后指纹自变。符号链接和 submodule 暂不支持。pack 拒绝覆盖报告，不运行测试；worker 先真实执行测试，再 pack 并复核 source/evidence。测试覆盖完整性和报告真实性仍需审核。

URL 入口用 entry.kind=url、target=http(s) 及 probe_evidence artifact（绝对 path/hash）；工具不联网证明 URL 现在存活。没有可运行环境时说明限制，不造验收就绪。self_test 与 quality_review 独立；quality pending 不得写 self_test failed，局部通过按实际范围记录且不会放行整体验收。

收件 CLI 能自动构造简单完成 envelope；受阻带决策、native ID 或技术核证时保存完整 receive 对象再 apply，见状态协议。相同交付重放不得重造 event_id/revision/字段以躲开去重。

## 受阻报告

```json
{"project_id":"warehouse-demo","task_id":"IMPORT","delivery_revision":3,"status":"held","summary":"已完成独立检查，仍有明确未决","self_test":{"status":"passed_for_independent_scope_only","command":"实际检查命令"},"blocker":{"reason":"一个具体未决问题","category":"business","owner":"human"},"technical_investigation":{"summary":"已比较原始数据、生产者和解析器；哪些解释被排除、哪些仍未确认","evidence":[{"path":"/absolute/investigation.log","sha256":"actual_sha256"}]},"open_questions":[{"question":"一个具体问题","options":["已讨论选项一","已讨论选项二"]}]}
```

交付者核证技术排查并绑定 report hash 后才把确需用户的人事项交原主会话；不把 worker 的“缺数据”诊断自动变为客户补数需求。常规技术问题直接对接原作者；用户要求进一步技术核查时用 redispatch 原子回原 worker，保持 scope/权限。

## 集成报告与同一 MR

```json
{"project_id":"warehouse-demo","task_id":"IMPORT","delivery_revision":2,"source_digest":"packed_source_digest","head_sha":"actual_source_head","target_sha":"actual_tested_main","status":"passed","command":"实际集成测试命令","evidence":[{"path":"/absolute/integration.log","sha256":"actual_sha256"}]}
```

每需求一个独立 MR，原 worker 仅对齐最新 main，解决冲突并重跑功能自测/本需求集成，更新同一 MR 送审。不提前整合或等待其它未合需求，不建组合 MR、不要求子代理互相对齐。主会话与用户授权范围/顺序，交付者独审及串行合 main；落后或失败直接交原作者，不代修。`mr`、`integration-start`、integrated 回执、交付者 review/delivery-check 见 merge-gate。新修订清空旧接受/集成/审核，MR id 和 owner 保留。各自通过不证明合入后的实际组合结果；按已合 main 核对，部署独立冻结 SHA/范围授权。
