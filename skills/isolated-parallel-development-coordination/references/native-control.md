# ROOT 原生关闭控制接线

仅用于正常已登记的新代码任务。代码已合目标 main 并核验、必要验收结束、作者 idle/completed 且无活动后代/工作/依赖，先外部保全。主会话和长期集成者仍受保护。本协议不承接旧空 resources 任务，不事后补造 intent、许可、lease 或关闭事实。

当前原生控制能力由真实 ROOT 持有；Ram 无工具时不宣称有 close 能力，不冒用 ROOT 调用普通写入口。宿主调用者身份必须由各自实际控制面提供，文件与 actor 字符串不赋权。以下接口仍在原唯一账本和写锁内，不新增服务、队列、轮询或唤醒。

## 1. 集成者生成当前 intent

```python
intent = runtime.prepare_native_close(
    task_id, stable_operation_id, preservation_ref, actual_integrator_id,
    authorized=True, scope="host_verified",
)
request_ref = intent["native_request"]
```

入口重验当前任务级批准或既有全局委托、准确注册作者/工作树、generation、路由、保全、main 及安全关口。先持久化 operation=close/status=intent 和 lease=closing，再返回不可变请求。请求固定 ROOT controller_id、目标及 close/wait 参数；尚未调用任何 native 工具。无记录旧资源、未合入、保护/共享/活动项不生成请求。

把同一 operation_id/request_ref 通过已授权现有通路交 ROOT。重复 prepare 返回原 intent；新 operation 不能绕过已有 closing 状态。

## 2. 真正 ROOT 一次领取并执行

ROOT 在自己的真实上下文、同一受信 profile 下调用：

```python
claim = runtime.claim_native_close(stable_operation_id, actual_original_main_id, authorized=True)
```

仅原主会话身份可领取。调用前重验当前 intent、作者 generation/路由/登记、批准、任务与保全；只有 dispatch_allowed=true 才可执行工具。领取先持久化 native_dispatch，重复领取返回 false，不能据未知结果重发 close。Ram 不调用这个入口冒认 ROOT。

ROOT 保全原始实际返回值，按 claim.plan 的**准确参数**各调用一次正式工具：

```javascript
const closeResult = await tools.multi_agent_v1__close_agent({target: targetId});
const waitResult = await tools.multi_agent_v1__wait_agent({
  targets: [targetId], timeout_ms: 10000
});
```

targetId 必须是已领取 plan.close.arguments.target；tool 名称及 wait 参数在 CORE 中固定验证。不是旧 close_agent({id}) 或 wait_agent({ids, timeout_ms:0}) 契约。close 返回 previous_status，只是关闭前状态；wait 的 not_found/shutdown 不能独立证明关闭或平台剩余名额。调用报错保留原错误，不填成功返回、不重复 close；丢失回执后先检查原效果。

原始 close/wait 返回和下面观察文件必须落在工作树、项目及所有回收根之外。不要只在作者目录保存：

```json
{
  "operation_id": "the-original-operation",
  "executor_id": "the-registered-executor",
  "generation": 1,
  "request": {"path": "/absolute/native-request.json", "sha256": "exact-request-hash"},
  "controller_id": "actual-original-main",
  "close": {
    "tool": "multi_agent_v1__close_agent",
    "arguments": {"target": "exact-target"},
    "result": {"path": "/absolute/raw-close.json", "sha256": "actual-raw-close-hash"}
  },
  "wait": {
    "tool": "multi_agent_v1__wait_agent",
    "arguments": {"targets": ["exact-target"], "timeout_ms": 10000},
    "result": {"path": "/absolute/raw-wait.json", "sha256": "actual-raw-wait-hash"}
  }
}
```

示例是字段契约，不是实际执行证据。ROOT 不直接写 ledger/lease，App 发送接受也不是执行结果。

## 3. 集成者沿原入口消费真实结果

```python
closed = runtime.reconcile_lifecycle(stable_operation_id, actual_observation_ref, actual_integrator_id)
```

入口只认原先已领取 intent，重验 controller/目标/代次/路由、任务、保全及原始工具结果。正式 close 有有效 previous_status、且准确有界 post-wait 返回 not_found/shutdown 才确认 closed。previous_status=not_found、只有 wait、只有 closed=true、超时、错误及其它目标均不变成关闭成功。真实未知保存具体观察、原因、下一技术责任者与 unknown；仍不可再次领取 close。后续可以用同一原 close 及新的明确有界观察 reconcile，不重新 close。

确认后持久化独立 close_confirmation，准确相同观察重复消费不写账本；保留原始证据。随后用原 reclaim_development/cleanup_task 聚合清理；Ram 可传 available_tools=[] 的 HostAdapter，使其不持有也不伪称 native 能力，已确认 close 不再调用宿主。

存在准确 RESOURCE request_id/permit_id 时，沿原 release_receipt/ResourceBridge.release_agent_quota 释放该本地许可。真实 observed 派发没有本地许可时，只核 native 关闭确认，不重建或伪造 permit；回收返回 release_receipt=null、plugin_quota=not_reserved。模型容量、平台总名额、进程 RAM 都不据此声称已释放。

真实 ROOT 的原始关闭及 post-wait 仍须在合入、保全后实际执行。包内 fixture 只验证契约和门禁，不是这些真实动作的替代。
