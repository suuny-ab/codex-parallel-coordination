# 生命周期规则与历史入口

现行完成、保全和回收边界统一维护在[共同规则](../skills/isolated-parallel-development-coordination/references/current-rules.md#完成保全与回收)，执行阶段见[集成者指引](../skills/isolated-parallel-development-coordination/references/roles/integrator.md)。接口仍查 [CORE](../skills/isolated-parallel-development-coordination/references/core-runtime.md#完成与清理) 和 [RESOURCE](resource/API.md)。

早期“暂停/等待验收提前关闭 agent”的草案已经撤回，不作执行策略。2026-10-31 本页原文及当时临时实验范围完整保留于[历史原字节快照](../skills/isolated-parallel-development-coordination/references/history/guidance-6f7b866.json)，原 hash 不变；其中个人环境、四名执行者和实验许可仅属于当时范围。

下列设计/验收文件保留其原修订，按需追查；不代替当前源码验证、用户审批或独审：

- [开发者生命周期设计](design/development-agent-lifecycle-spec.md)及[验收清单](acceptance/development-agent-lifecycle-checklist.md)。
- [需求路由设计](design/demand-driven-routing-spec.md)及[验收清单](acceptance/demand-driven-routing-checklist.md)。
- [真实收尾修复设计](design/real-use-closeout-fix-spec.md)及[验收清单](acceptance/real-use-closeout-fix-checklist.md)。
