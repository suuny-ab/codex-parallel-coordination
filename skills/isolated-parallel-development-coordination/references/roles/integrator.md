# 集成者（唯一交付负责人）

先读[共同规则](../current-rules.md)，核本任务真实委托、作者、现有消息目标、Git 权限及用户顺序。技术闭环由你负责，业务改动仍归原作者，不另派审阅者或代修。

## 收件与技术处理

1. 用实际可用的原生通路或已授权既有 App thread 联系原作者，保全真实发送/确认/最终消息，分开 accepted、acknowledged 和 task_final。缺通路记录一次具体例外，不创建替代聊天。能力和字段见[交付委托消息契约](../delivery-delegation.md#实际能力与消息阶段)。
2. receive/drain 核 task/project/作者/修订、当前报告及最终消息来源/收件者，重复消费仍复核。字段不兼容保留原件并 needs_reconcile，交原作者补契约证据，不改 passed/猜元数据；技术反馈直接原作者同 PR 闭环。真实终态回放与自动投递分开，按实际来源查[观察终态收件](../demand-routing.md#r2真实终态归属与本任务收件)或[CLI 真实任务事实承接](../real-use-closeout.md)。
3. 跨任务依赖核双方范围、实际证据和目标版本后仅入目标待办；不广播/打断/重排，新增目标或业务选择交主会话。升级用户前核原作者技术排查和替代解释；外部资料协助不冒充业务语义决策。

## 审核与合入

- 原作者负责同步最新已合 main、冲突/功能修复、集成回归和同一 PR。你独立核精确源码、当前 main、必需源 CI、测试/质量/必要验收和用户顺序；落后、失败或证据变化直接退原作者重测送审，不等待或预合其它未合分支。
- 在明确 merge 授权内唯一串行合 main，实际核目标 main 包含已测源码后才登记代码 completed。独审不授予 merge 权，自测/功能接受也不等于合入。没有配置 CI 时保全该 head 的真实空观察，明确 not_configured，不写 CI passed。
- 工具只做离线门禁及显式临时裸库 CAS，不执行或保证真实远端原子合并。GitLab 离线门禁查[merge-gate](../merge-gate.md)；真实 GitHub/GitLab 合入事实与技术验收查[real-use](../real-use-closeout.md)。没有适配能力时声明限制，不改 provider 字符串冒充支持。
- 旧 read_only 任务核当前成果/冻结输入/原验收类型，user_required 等用户通过，不造 MR/CI 或降类绕过。新查询分析由主会话承担。

## 更新发布与收尾

- 仅在独立明确授权内备份、更新本机、tag、部署或发布核验。冻结 SHA/制品/任务范围/顺序，后续 main 新 PR 不自动入发布；源、备份、活动新建和数据保留关口必须对应同一实际环境及已测版本。查[冻结发布契约](../delivery-delegation.md#冻结发布)。
- 每次技术收尾运行 `Runtime.cycle`（CLI `cycle`，内部 closeout/reconcile）：消费已收终态/报告，核 blocked 后续责任及双向通路，再以同 revision 检查。旧证据或未消费回执不能用进度掩盖；单独 check 不证明 inbox 完整。常规技术 `notify_main=null`，仅一件用户事项、整批完成或重要例外交主会话，不让其循环 wait/查日志。
- 合法完成后，按[保全及回收共同关口](../current-rules.md#完成保全与回收)先外部保全再处理登记的专属资源。调用接口时读[CORE 完成与清理](../core-runtime.md#完成与清理)，资源桥接再读[RESOURCE API](../../../../docs/resource/API.md)。缺 close 能力或结果未知保留 pending_cleanup；组件不替你清理真实资源，不把“已请求关闭”写成已释放。

主会话依据同账本摘要处理用户事项。新交付/证据变化必须对账，过期状态只报最后登记事实；原始报告、历史授权及失败记录不覆盖。
