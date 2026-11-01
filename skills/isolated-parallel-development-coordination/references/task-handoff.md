# 已派发任务的有限交接

`Runtime.handoff` / `core_runtime.py handoff` 复用唯一项目账本。只接续已经绑定的实际作者、generation、request/operation 或 observed dispatch；不 spawn、不发消息、不合并、不回收、不授予全局角色。`WORK-CYCLE` 仍负责本轮摘要和既有收件对账。本入口供受信宿主处理一次新观察，不是后台采集器。

原 launch 已由真实证据核为 not_started 后，ROOT 通过现有公开 bind_executor 登记的手工原生作者也可接续；逐次核原未开始证明、批准、现任务 agent 资源及 generation。该来源标为 registered_executor，原 launch/失败记录保持不动，不补造 managed spawn、准入或 running。未知 launch 且无确定原绑定仍拒绝接续。

## 一次观察与收件

1. 用固定 profile 的现有 state-root/project-root、真实 caller 身份和 task 调用 plan。只把宿主当前实际暴露的工具名传入 `--tool`；返回一项 `host_call` 及 `binding`。主会话可读 plan，实际写入只能由批准的集成者执行。
2. 受信宿主调用返回的那一项工具，原样保存工具名、实参、原始返回和原 `binding` 到树外不可变观察文件。原生 wait 空返回继续 unknown；App 必须返回同一实际 thread ID 和明确最新 turn，截断/无 final 路径不能代替终态来源。不猜工具参数、不创建替代聊天。
3. 集成者用 `--observation` 和当前不可变 `--report` 再调用同一入口；也可用任务已登记的精确 report_path，不遍历寻找报告。明确 running 只接续原作者和资源；明确终态才检查最终报告，进入既有 admit/consume 收件、质量及集成关口。没有实际工具能力时返回 `capability_unavailable`；ROOT 专属能力只需执行 plan 中一项最小调用，把原观察交回集成者，无需逐任务 Python 收件脚本。

示例命令的占位值由真实宿主固定，actor 字符串和 JSON 文件自身不证明身份或授权。Windows 使用已核 Python 3.12+ 的绝对路径、`-X utf8 -B`，每个命令显式定位工作目录。

```powershell
& ABSOLUTE_PYTHON -X utf8 -B ABSOLUTE_CORE_RUNTIME --state-root FIXED_STATE_ROOT --project-root FIXED_PROJECT_ROOT handoff --task TASK --actor ACTUAL_CALLER --tool mcp__codex_app__read_thread
& ABSOLUTE_PYTHON -X utf8 -B ABSOLUTE_CORE_RUNTIME --state-root FIXED_STATE_ROOT --project-root FIXED_PROJECT_ROOT handoff --task TASK --actor ACTUAL_INTEGRATOR --observation ABSOLUTE_CAPTURE --report ABSOLUTE_REPORT
```

观察包结构固定；`result` 必须保留实际工具返回（包括 MCP content），不能只写自己判断的 running/completed：

```javascript
const capture = {
  schema: 'task-handoff-observation/1',
  binding: plan.binding,
  tool: plan.host_call.tool,
  arguments: plan.host_call.arguments,
  result: actualUnchangedToolResult
};
```

当前支持实际 `mcp__codex_app__read_thread({threadId,turnLimit:1,includeOutputs:false,maxOutputCharsPerItem:1800})` 或 `multi_agent_v1__wait_agent({targets:[原target],timeout_ms:10000})`，只选择确实暴露的一项。App 使用紧凑正式参数省略工具/命令输出，保留最新 turn 状态及原 final，避免单个巨大 turn 超出工具响应限制；保存的仍是该次实际调用的完整原始返回，不是自行选择或删字段后的对象。App 完成必须有最新 turn 的 `agentMessage` final/final_answer；原生完成必须有对应 target 的 completed 文本。该文本须包含这份完整绝对报告路径，路径被截断或缺失仍拒绝。isError 超限拒绝保留原件，不映射为完成，也不能把旧大返回改参数重包成紧凑观察。没有可靠终态来源时保留 unknown，不用 idle、发送接受或宿主 completed 代替作者交付。

## 报告门禁与修订

完成报告必须用 [canonical pack](task-and-report-templates.md#完成报告规格与自检)，包含实际 owner/project/task/revision、worktree/branch、`git.pr_url/head_sha/target_sha`、完整 source（含 extra）、源码绑定的真实自测及证据。Git 来源核当前 HEAD/分支与报告；PR 地址及已测 target 是作者报告事实，远端 PR/CI/target 新鲜性仍由集成者独审，不因字段齐全宣称已联网核实或 CI 通过。

缺项、held、测试失败或源码/证据失效返回 `needs_author` 和原作者，不进入成功收件；实际失败 turn 只能配带明确原因的 blocked 报告。quality pending 继续走原有质量关口，收件本身不证明质量、用户接受、独审、合入或部署。

缺元数据时保持 R1 字节，作者提交 R2。核 source 和自测 evidence/digest 不变可复用已测绑定，不能借补字段改 passed、验收或独审结论。同一 PR 已有记录或已交付时不允许换 PR。重复 receipt 保留原 canonical payload/来源；迟到 running/旧报告不能回退已有交付，冲突同修订拒绝；宿主重启后用原身份、报告和观察文件接续。原报告、源事件及所绑定批准在后续取用时仍重新核证。

无终态/原始副作用 unknown 时不重派、不换 id、不释放资源。原生明确容量拒绝与 RESOURCE 准入分开，标准宿主的外层预算及中断见 [标准派工入口](standard-dispatch.md#当前宿主的有界接入)。本入口不修旧事故记录或真实锁，也不安装 hook、守护进程、轮询、自动唤醒。
