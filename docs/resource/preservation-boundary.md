# 冻结捕获与必要依赖

遗漏捕获项可在正式归档和关闭意图之前受控追加：新声明带 `previous`（原声明的路径及SHA）与补正原因，调用 `classify_preservation(..., authorized=True, previous_declaration=原声明ref)`。旧清单、原字节、既有每项分类和失败记录均保留，禁止删改旧分类或把必要凭据降为捕获。每次读取重新核验整条声明链；重复提交也重验原件。已正式归档或进入关闭阶段后拒绝追加。

原带marker的归档失败可在追加分类后沿原操作继续，保留原marker和partial字节，另存不可变修订意图。没有marker的旧失败目录保留原状，使用新的明确归档目录，不冒充已知原操作。

`Runtime.classify_preservation(task_id, declaration_ref, actor_id, authorized=True)` 登记技术分类。它复用账本内该任务的 `resource_authority` 原批准 SHA、原需求关联和集成者角色，调用者须为原主会话或获批集成者，且不是作者。原授权须包含任务成果保全回收或 `bounded_new_task_resource_cleanup`；声明里的角色、用户引文和标签不授予权限。声明和原批准必须留在受管资源之外。

原批准解析器统一承接现行直接用户消息及明确异步用户批准两种精确来源，分类、质量承接与后续交付门禁使用相同身份校验，仍核原文件 SHA、主会话、原需求路径、任务/树/分支和独立角色。未知来源仍拒绝；`SOURCE` 规范常量、技术/合入指令契约及 bounded cleanup 权限保持不变，不改写旧批准。

集成者核对生产者契约与原字节后准备如下技术声明，无需用户逐包重新审批：

```json
{
  "schema": "preservation-classification/1",
  "project_id": "project-example",
  "task_id": "FEATURE",
  "delivery_revision": 2,
  "delivery": {"path": "/durable/final-report.json", "sha256": "exact-final-report-sha256"},
  "captures": [{
    "original": {"path": "/durable/test-result.json", "sha256": "exact-original-sha256"},
    "contract": "frozen-test-result/1",
    "reason": "完整测试运行捕获；内部回执属于已结束的模拟测试"
  }]
}
```

固定契约为 `frozen-test-result/1`（测试运行捕获）、`frozen-baseline/1`（冻结基线与历史失败构建数据）、`frozen-host-message/1`（宿主消息捕获）。每项必须给出准确的绝对原路径、SHA 和可复核理由；不用文件名、目录前缀或通配符分类，也不从 JSON 自填的 `kind/schema` 推定可信。未分类证据仍按声明字段递归核验。

分类适用于 `evidence/artifacts/extra/probe_evidence/raw_journals` 引用槽中的完整包；`result/probe` 槽仅接受 `frozen-test-result/1`，`real_task_evidence` 仅接受冻结运行快照 `frozen-baseline/1`。必要的当前报告、源码、审核、批准、合入、关闭等证明继续严格核验；同一文件同时出现在证据和 `report/review/proof/authority` 等必要槽时仍拒绝降类。旧快照不能替代当前报告 source 与 Git 来源证明。捕获包自身必须存在且 SHA 匹配，复制完整原字节；只停止解释其内部临时、模拟和跨任务指针。相邻必要材料不受影响。

分类写在原任务账本，保全清单保存原批准、声明和分类依据，绑定当前交付修订。`preserved_read`、cleanup eligibility 和 native 关闭证明复用同一复核函数，检查绑定、包副本与仍需核验的原包。原树经确认回收后从同一 SHA 副本读取；远端绑定完成任务沿用既有持久资料语义。包或副本损坏、身份/修订/声明/批准变化继续拒绝。无分类的既有合法保全保持原 binding 格式。

分类在保全与关闭意图前登记。对已经 `unknown` 的原保全操作，可分类后沿原 operation、request 和 destination 继续：旧 `preservation-intent.json` 不改写，追加 `preservation-classification-intent.json` 绑定旧 marker SHA 和当前分类。只允许分类增量，其它原绑定须完全一致；已有部分文件逐字节复核，旧失败保留。中断后沿同一意图继续，篡改 marker 或部分副本均拒绝。本入口不重置目录删除失败、不恢复派发超时、不发起宿主关闭。

阶段边界：分类前与首次复制时，原捕获包缺失或 SHA 变化均拒绝。完成远端 main 核证并写入 canonical 保全后，受管工作路径转为来源身份，资料始终从完整外部封存副本读取；即使原树尚未清理，原路径变化或消失也不替换封存内容。这沿用既有 cloud-bound 完成语义，不表示原路径的新字节通过核验。封存副本、分类记录/声明、原批准、旧 marker 或追加意图有任何变化仍拒绝。外部非受管原包仍持续核其原 SHA；合法回收原树后继续核封存副本，不追索已确认清理的旧路径。实际删除和关闭授权不由分类授予。

测试中的真实临时 Git/文件删除证明持久资料可读；native、CI 和审核夹具不证明真实旧 agent 已关闭、配额已释放或宿主已加载新代码。真实旧资源须由获批宿主另核，原报告、旧失败与源码在此之前保留。
