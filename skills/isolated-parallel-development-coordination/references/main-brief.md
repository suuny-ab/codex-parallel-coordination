# 主会话轻量收件

从插件根调用现有入口，CLI 默认是四块简报，预算默认 8192 UTF-8 字节（含 JSON 编码和末尾换行），可显式设置 4096–65536。不加载完整报告、历史或日志到默认返回；所有来源、就绪、报告、收件角色校验仍执行，不能据此推断计算提速。

```text
python -X utf8 -B skills/isolated-parallel-development-coordination/scripts/core_runtime.py --state-root ABS_PROFILE_STATE --project-root ABS_PROJECT work-cycle --actor ACTUAL_MAIN_ID
```

调用者身份及 state-root 继续由[原受信宿主](core-runtime.md#一轮工作入口)提供。当前已批准集成者可沿原入口带 observation 有限收件；主会话不能借此写入技术事实。Python `Runtime.work_cycle` 保留旧默认完整结构，避免破坏在途宿主；请求相同简报时传 `view='brief'`。CLI 的 `--view full` 保留原诊断结构及全部内容。

## 四块内容

| 字段 | 含义 |
| --- | --- |
| `user_items` | 当前可递交的一项及摘要、尚未关闭的用户事项总数。焦点、优先级、FIFO、就绪条件均沿用原算法；总数可能包含尚不就绪项。 |
| `notifications` | 本次已收到且原通路校验仍有效的待展示通知。普通开发/独审成功按任务及责任者聚合到最新阶段；合并、安装、阻塞、失败、清理各自保留。`received_total` 是原通知数，`total` 是聚合后的行数。 |
| `tasks` | 未结束任务及结束后回收尚未核清的任务，逐项给阶段、阻塞、下一责任者及动作。业务归属相同的子任务也保留，避免丢失其阻塞或责任。合 main、安装、agent 与其它资源状态各自表示。 |
| `next_action` | 原入口选出的用户决策、内部处理或等待动作。没有用户事项仍可能需要收件、审核、核查或回收，不能解释为全部结束。任务范围给总数，完整清单按引用读取。 |

`source` 包含同一账本 revision、规范化账本指纹、快照引用、核对时间和观察来源。没有新观察标为 `last_recorded`，不把账本中的旧观察当作本轮宿主现状。`verification_scope=snapshot_evidence_only` 说明验证的是该快照证据，不是宿主身份或未来事件。重放已观察快照时，`snapshot_replay=true`、`current=false`，仍明确为最后登记。

## 续读与详情

`page.remaining`、分块剩余数及 `remaining_attention` 明确未展示数量。任务与通知交替入页，未结束任务先于收尾，异常通知先于普通通知；不为凑批次等待新通知。只要还有未展示行，`allow_end` 和 `all_work_finished` 都为 false。用返回的游标继续，不重新提交 observation：

```text
python -X utf8 -B skills/isolated-parallel-development-coordination/scripts/core_runtime.py --state-root ABS_PROFILE_STATE --project-root ABS_PROJECT work-cycle --actor ACTUAL_MAIN_ID --cursor RETURNED_CURSOR
```

每项 `detail` 是选择器，与该页的 `source.snapshot` 一起使用。可选 `user`、`notifications`、`tasks`、`observation`、`full`，或行内 `task/N`、`notification/N`。聚合通知的全部稳定事件 ID、各自 pending/sent_unconfirmed 状态、证据和原文均在该行详情中；聚合行的 `statuses` 保留各状态数量。查询不标已读，不确认实际展示，不清空待办，不切换焦点，也不制造或重写事件。

```text
python -X utf8 -B skills/isolated-parallel-development-coordination/scripts/core_runtime.py --state-root ABS_PROFILE_STATE --project-root ABS_PROJECT work-cycle --actor ACTUAL_MAIN_ID --snapshot RETURNED_SNAPSHOT --detail notification/0
python -X utf8 -B skills/isolated-parallel-development-coordination/scripts/core_runtime.py --state-root ABS_PROFILE_STATE --project-root ABS_PROJECT work-cycle --actor ACTUAL_MAIN_ID --cursor RETURNED_DETAIL_CURSOR --detail notification/0
```

详情同样有界，以完整 JSON 页返回 `encoding=json-text` 的 `content`。按顺序拼接同一选择器的 content 后再解析 JSON；字符切分不破坏 Unicode。长摘要有 `…[detail]`，列表给准确数量和详情入口；极大单行会显式 `summary_in_detail=true`。没有静默截断原 JSON 或把未展示项当作已处理。

快照/游标绑定项目、调用者、任务过滤范围、完整账本、已校验投影和详情内容；游标还绑定所读分块。续读时重复相同 actor、`--task` 范围和详情选择器。账本或可见证据变化即报来源变化，须重新取首页，不能拼接不同 revision；没有第二份任务/通知队列或持久分页缓存。每页都重新校验证据，页数多时计算和总传输成本会增加。快照是读取引用，不是身份、授权、已读或验收凭据。

实际展示后仍须沿[原通知确认通路](stage-feedback.md)执行受信确认；收到简报、发送接受、读取详情都不能代替实际展示。安装、派发、资源回收、合并及既有门禁算法未由本入口执行。异常超过首页时，必须续读核清后再按原门禁收尾。
