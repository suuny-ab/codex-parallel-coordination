# 标准派工入口

仅用于主会话已经确认的一份新 spec。需求语义、拆合、批准和集成者由主会话与用户决定。返工继续走原作者、原工作树及原 PR；本入口不分配旧作者、不代替 WORK-CYCLE 收件或 RESOURCE 正常闭环。

## 固定输入

主会话保留原始呈现摘要、用户批准和自核证据。批准文件沿用 `approved-dispatch-plan/1` 的字段：`task_id/task_ids`、`plan_id/presentation`、`decision/quote/text`、`base_main`、`integration_agent`、`tasks[{id,worktree,branch}]`、`specs[{path,sha256}]`、本任务 `rights`。呈现摘要包含已确定的名称、scope、acceptance、requirement_id、拆合及新作者理由。`SPEC_CHECK` 绑定 spec、讨论、主会话和自核结论。

固定请求只有六个字段；下面是结构示例，不是授权：

```json
{
  "request_id": "dispatch-APPROVED-TASK-R1",
  "project_id": "project-from-the-existing-fixed-profile",
  "approval": {"path": "ABSOLUTE_APPROVAL.json", "sha256": "SHA256"},
  "spec_check": {"path": "ABSOLUTE_SPEC_CHECK.json", "sha256": "SHA256"},
  "report_path": "ABSOLUTE_EXTERNAL_REPORT.json",
  "acceptance_type": "delivery_verified"
}
```

`approved_request` 从冻结文件派生其余字段。`acceptance_type` 必须是当前任务实际确认的 `delivery_verified` 或 `user_required`。同一请求始终使用相同 id 和字节；不能换 id 绕过 unknown、accepted_unconfirmed 或已经存在的作者。当前入口每次接收单 spec 的批准；已有多项摘要仍使用正式 routing 接口，不能拆写或伪造原批准。

## 纯排队请求的技术重基线

已登记的原 RESOURCE queued 请求遇 main 前进时，原主会话经当前明确的同范围技术重基线授权，可单独调用 `StandardDispatch.rebase_queued(original_request, authorized=True)`。受信 stdio 宿主对应 `new DispatchSession({...ORIGINAL_TRUSTED_HOST_OPTIONS, request: SAME_BOUND_REQUEST_REF, requestMeta: nodeRepl.requestMeta, rebaseQueued: true})`，外层仍设 `timeout_ms: 50000`，只取一次 `next()` 的 result。固定宿主 options 不能来自 task JSON；该布尔值是本次真实授权 sideband，不是文件自授权限。重基线与 reconcileResult 不能组合。

此动作只检查并追加 `base_revisions` 指纹证据，返回原 queued 状态、effective_base_main 和空 host_actions，不创建树/分支、不调用 native、不申请或释放 permit。必需原 request digest、批准/spec/项目/路径/分支/作用域一致；CORE 原记录仍为 RESOURCE queued，尚无树/branch/资源/launch/host intent/作者；同一 RESOURCE 请求当前仍 queued 或已正常 granted；实际 local/remote main 一致，前一有效基线是当前 main 的祖先。任一条件不足即拒绝；unknown、已接受、已创建及活跃作者绝不适用。

原 APPROVED/DISPATCH 请求及旧失败不改写。后续原调用方明确恢复正常 `DispatchSession`（不设 rebaseQueued），继续同一 request/task/attempt 和 RESOURCE 请求；入口每次重新核证基线链及当前 main，再从有效基线创建唯一原树，仍按真实准入和 host 结果推进。main 又前进时只能在仍满足纯排队条件下再追加一次，不换 ID 或自动轮询。没有该当前技术授权时保留 stale 提示，由集成者给原主会话最小步骤。

检查期间不占用 CORE 长写锁等待 Git/网络；追加时用原 revision 核对，账本已变化则拒绝，保留并发变化及未登记的原证据。后续显式重新核实仍用原 request/task/attempt，新的观察证据带对应 revision，不覆盖先前文件。

## 当前宿主的有界接入

`src/codex_parallel_coordination/dispatch.py` 的 `StandardDispatch.run` 复用 Runtime.routing/launch、ResourceBridge.prepare_development/admit、GitWorktrees.create 及原 CORE/RESOURCE 登记入口。新任务沿用 new-task/1；已有明确预留的登记不伪改成新建。输入文件不能指定 actor、state-root、授予权限或选择宿主可用能力。只接受当前原主会话 actor；固定 profile 来自受信宿主配置，不能从 task JSON 加载。现有全局 delivery 委托如果排除了主会话 launch，应先交集成者核实其授权边界，入口不冒用交付者身份。

Python 子进程不能直接调用当前 agent 的 native 工具。`scripts/dispatch_host.mjs` 提供当前 `node_repl` + `functions.exec` 的实际 stdio 通路：

1. 在 `node_repl` 导入受信插件内的 `DispatchSession`，传入固定 profile、明确 Python 绝对路径、绑定请求和 **`nodeRepl.requestMeta`**。actor 使用实际 `threadId`，不使用可能属于父会话的 `sessionId`。`approvalOrigin` 是主会话核对直接用户批准后给出的受信 sideband `{role:'user',conversation_id:实际主会话,reference:原批准ref}`，不能从请求文件自取。
2. Python 先完成准入核验，再输出一次 host_call；functions 宿主只调用真实暴露的工具并返回未经伪造的原始结果。下面的通用驱动可反复使用，不生成逐任务 Python 脚本。
3. 最多执行 spawn、observe、integration-send 三个 host_call，宿主会话45秒截止。截止时关闭输入，给当前子进程至多1秒退出宽限；必要时仅结束本会话子进程，再用至多1秒的只恢复子进程核对原请求自有锁。结果保留恢复证据或明确拒绝；没有常驻服务、后台队列、自动唤醒或自动重发。

初始化示例（参数由受信主会话提供，省略项目特定值）：

```javascript
// 宿主先核对已审已装 binding 及 dispatch_entry.mjs 自身 hash，再从该安装路径导入。
var {loadInstalledDispatch} = await import(VERIFIED_INSTALLED_ENTRY_URL);
var {DispatchSession, serializeHostError} = await loadInstalledDispatch(REVIEWED_INSTALLATION_BINDING);
var dispatchSession = new DispatchSession({
  python: FIXED_PYTHON_ABSOLUTE_PATH,
  profile: FIXED_HOST_PROFILE,
  request: BOUND_REQUEST_REF,
  requestMeta: nodeRepl.requestMeta,
  testingScope: 'host_verified', // 仅实际宿主通路；fixture默认fixture_only
  approvalOrigin: VERIFIED_USER_ORIGIN,
  availableTools: ACTUAL_FUNCTIONS_TOOL_NAMES
});
nodeRepl.write(JSON.stringify(await dispatchSession.next()));
```

上面初始化以及下方每次 `next/reply/verifyHostResult/interrupt` 的外层 Node 工具调用均显式使用 `timeout_ms: 50000`（模块 `NODE_CALL_TIMEOUT_MS` / `session.callTimeoutMs`）。它覆盖45秒会话截止、1秒子进程退出宽限、1秒锁恢复及返回余量；不要用20秒或默认30秒调用等待这个会话。主动取消时显式 `await dispatchSession.interrupt()`。硬 kernel reset 无法保证 finally 执行；下一次原请求通过同一入口先核对固定项目的原自有锁，不能抢占来源不足或仍活跃的 owner。

安装 binding 是集成者独审、安装核对后给受信宿主的 `source_root/source_head/files`，其中 files 是已审全源码的相对路径到 SHA256 映射。`dispatch_entry.mjs` 核对自身所在安装根、每个真实文件及指纹后才加载同一安装内的 host 模块；不回退到作者工作树。binding 不能来自任务 JSON，复制测试也不代表正式安装完成。

固定 profile 的字段为 `state_root/project_root/resource_state/evidence_root/integration_channel/integration_target`；必须指向已有唯一账本、原 RESOURCE 实例、永久外部证据目录及受信的已授权集成者。不能初始化新的生产账本来绕过现有权限。当前 ROOT 传输按冻结契约精确调用 `multi_agent_v1__spawn_agent({message})`、`multi_agent_v1__wait_agent({targets:[实际spawn身份],timeout_ms:10000})`；集成交接使用已暴露的 `send_input({id,message,interrupt:false})` 或 `mcp__codex_app__send_message_to_thread({threadId,prompt})`。前两项的 canonical 别名仅限 Python HostAdapter 内部，stdio 记录并下发真实工具名/实参，原始返回值不改写。不会调用 App create_thread，也不会启动独立 Codex CLI。

functions 宿主驱动（`packet` 是上一步真实 JSON；`tools` 与 `ALL_TOOLS` 是宿主提供的真实绑定）：

```javascript
const names = new Set(ALL_TOOLS.map(t => t.name));
const permitted = new Set(['multi_agent_v1__spawn_agent', 'multi_agent_v1__wait_agent', 'send_input',
  'mcp__codex_app__send_message_to_thread']);
let packet = INITIAL_PACKET;
for (let count = 0; packet.kind === 'host_call' && count < 3; count++) {
  if (!permitted.has(packet.tool) || !names.has(packet.tool)) {
    throw new Error('Capability unavailable; observe original intent, do not substitute another tool');
  }
  let result, error = null;
  try { result = await tools[packet.tool](packet.arguments); }
  catch (failure) {
    error = {name: failure?.name ?? null, message: failure?.message ?? String(failure),
      code: failure?.code ?? null, string: String(failure)};
  }
  const reply = await tools.mcp__node_repl__js({
    code: 'nodeRepl.write(JSON.stringify(await dispatchSession.reply(' +
      JSON.stringify(packet.tool) + ',' + JSON.stringify(result ?? null) + ',' +
      JSON.stringify(error) + ')))',
    timeout_ms: 50000,
    title: 'Continue the same approved dispatch'
  });
  packet = JSON.parse(reply.content.find(b => b.type === 'text').text);
}
text(packet);
```

如果原生工具只作为单独的宿主工具暴露，主会话按同一 packet 调用那一个真实工具，再把原始返回值交给 `dispatchSession.reply`。工具参数、id、消息和原始返回值不得改写；这仍是固定入口的有界宿主步骤，不是新编排脚本。工具清单、真实返回结构和这条通路必须在目标主会话核实。开发子 agent 没有 native spawn 时，结果是具体能力缺口；不能拿 Python callback、该段示例或测试夹具称为实际原生派发成功。

## 状态、证据和恢复

属于已核任务的派发保存带原 `task_id`；routing 从已验证的命令/记录推导 `task_ids`。单任务保存仅采集当前任务，批量摘要、批准及依赖采集所有受影响任务；拒绝的元数据命令不采集任务。真正全局保存仍保留全范围采集。其它任务已有状态及未展示通知不因单任务保存被刷新或确认。

### intent 后、callback 前的显式恢复

原受信宿主仍持有原 `DispatchSession` 对象时，可以显式执行 `var resumed = await dispatchSession.resumePrehost(nodeRepl.requestMeta)`，再用上面的固定驱动调用 `resumed.next/reply`。外层继续使用 `timeout_ms: 50000`，新的有限接续仍为原 45 秒上限；不会自动循环重启。并发/重复调用原对象的恢复方法返回同一接续对象。旧版本真实实例可由已审新模块执行 `DispatchSession.prototype.resumePrehost.call(originalSession, nodeRepl.requestMeta)`；必须先核对原对象、原请求/批准/profile及两版源码来源，不接受同形 plain object 或任务文件替代。

恢复仅适用于原子进程已实际退出、原会话已结束、已暴露 host packet 数为零，且没有 pending/reply/retained result 的情况。原对象将实际终态写入永久 evidence_root，并以私有宿主 sideband 传给接续入口；单独一份声称零调用的 JSON 不授予恢复权限。CORE 在锁内再次核对原请求、actor、owner journal 的 PID/创建身份及死亡状态、原 intent/route/许可/任务版本、当前 RESOURCE granted，以及不存在 callback/host intent/result/实际 owner。缺 marker 只是必要条件，不能单独证明未调用。

通过后复用原 request/task/attempt、工作树和许可，保存原 intent 快照及恢复证据；不删除原失败或申请/释放许可。已有 callback、真实身份、丢失/迟到返回、仍活跃/未知进程、PID复用、错误请求/项目/任务、变更路由/权限、缺失或冲突来源均拒绝恢复，不重发。原对象丢失或来源不足时保留未知，由获权 ROOT/集成者核证，不以新文件重建宿主对象。一次接续若再次中断，不自动或反复重置 intent。

同一 canonical `runtime.operations` 保存请求指纹、工作树意图、RESOURCE 许可、CORE launch 及交接意图；没有第二份任务队列。分支创建前核对本地与远端 main、真实路径和当前资源策略；只使用本次命令的 `-c core.longpaths=true`。已有工作树只有经原公开资源入口登记且与本次批准、路径和分支一致才可使用。

`queued` 只表示准入等待；没有工作树或 host 调用。Git 创建丢响应后只观察已记录路径，核实实际分支和 HEAD 后继续，不重新 add。spawn 前落 launch intent；spawn 接受后在单独 observe 前持久化实际身份。`unknown/accepted_unconfirmed` 保留原操作和资源，不重发、不换 id，投影显示“已派发，执行状态待核实”。已知身份的后续观察及报告走[有限交接](task-handoff.md)。原入口实际 running 才向已授权集成者发一次交接；接受交接不代表独审或最终交付。交接未知也不自动重发。

RESOURCE `granted` 只证明本实例准入，不保证原生宿主名额，也不推导全局容量。原生明确 `agent thread limit reached` / 对应容量代码保留原始异常和回执，返回 `capacity`；响应丢失等未知副作用仍为 `unknown`。两者均不自动重派、换 attempt 或释放资源；由集成者按真实事实核对下一动作。

raw host intent/result 和准入、交接回执保存于 profile 的永久外部 evidence_root，带 SHA256 引用。CORE 的作者、任务阶段与资源归属仍为唯一权威。半途故障保留现场，使用同一请求恢复可确定步骤；需要观察的状态交集成者核实，正常保全/回收继续用现有生命周期入口。没有自动删除、回滚或历史修复。

`reply` 对匹配的 pending packet **先持久化原始结果**，再判断会话是否截止或子进程是否退出。原子外部 journal 绑定原请求、任务/项目、操作、工具/实参、原始返回和已知实际 agent ID；相同迟到结果复用原文件，冲突结果拒绝覆盖。正常回复也先落同样证据，才继续 Python。截止或退出后只返回 `needs_reconcile`、结果指纹及核证入口，不续派、不重发，不据迟到观察宣称 running 或交接完成。

原受信 caller 可显式调用 `await dispatchSession.verifyHostResult(nodeRepl.requestMeta)`：使用已保留请求/结果，等当前子进程退出后，通过 `StandardDispatch.reconcile_host_result` 核对原 canonical intent。当前未知 spawn 的真实 ID 经原 `bind_executor/register_resource` 入口保留归属，仍保持 `needs_reconcile` 和执行未知；核证不调用任何 native 工具。同一请求再次 run 也不续派。原对象不可用时，受信 host 用同一 profile/request 加 `reconcileResult: 返回的host_result指纹` 创建有限核证会话，不重新填写任务材料。

每次有限入口先通过现有 `Runtime.recover_call_locks` 检查本项目固定的 ledger/driver 两个锁路径；仅额外转发受信 profile 的 `dispatch_evidence_root`，内部复用 `recover_dispatch_locks` 核验。新派工的锁引用外部 owner journal，绑定原 project/task/request digest、受信 actor、PID 和进程创建时间；先保全原锁字节、当前账本指纹和进程观察，再双检死亡、进程身份、账本字节及锁文件身份。Windows 使用禁止并发写入/替换的删除句柄，只释放核对过的文件，并留下恢复回执。正常 finally 也只释放自己创建的锁，不删除替换者。Git 预检和创建树的等待释放共享 driver 锁，重新进入时核对 revision。旧派工恢复不能绕过创建身份门槛；原 delivery-continuation 保全恢复分支保持其既有边界。

活跃写者、PID复用、错请求/项目/任务、未知进程、并发换锁/账本变化、旧锁缺少创建身份或来源均拒绝，保留原结果及集成者责任。自动死锁恢复目前限 Windows；其它平台保留拒绝，不以 PID 不存在猜测身份。树意图已存在但树结果未知时只观察原路径，不重建；CORE 原 intent 默认去重，只有上述受信零调用终态允许显式接续。没有 CORE launch intent、没有 host intent 且仍为原 launch_pending 的明确间隙保持既有原 attempt 接续语义。RESOURCE 许可保持原样，不申请或释放另一许可；后续观察和交付继续 handoff/work-cycle。

验证分开报告：保存组件耗时、真实 Git/RESOURCE 与 stdio 桥接、synthetic native fixtures、实际 native 验收。完整 native 验收由获权 ROOT/集成者按已审源码与当前既有需求执行；不得为验工具自造任务。作者当前受信外部派发也不证明 StandardDispatch SDK 已通过。
