# Codex parallel coordination

独立维护的本地插件候选 `0.1.0`。用于多个已授权开发任务的收件、对账、单项人事项交接，以及受管执行者的资源准入。项目不含业务系统数据或真实任务账本。

原主会话是用户唯一入口。开发者各自隔离开发和自测；唯一交付者做非作者审核、协调原作者修复，并在获授权后串行核对 CI、合入 main。代码任务经必要验收、集成审核和目标 main 包含关系核对后结束；无代码改动的排查、审阅、探索任务经交付者核验当前成果后结束，明确要求用户验收的仍等用户通过。用户一次只处理一件事，用户指定优先，其余待解阻事项与就绪验收同级 FIFO；已授权自动验收按原授权完成。main 或证据过期则同步重测、维护同一 MR；部署另用冻结提交与制品，交付职责本身不增加 Git 或部署权限。

**初始私有 main 基线已提交；本轮生命周期增量仍为本地候选，未发布、未全局安装。首版不支持空闲自动唤醒。** 协调者进入或结束一次工作时显式收件、对账和收口；没有后台轮询。实际 CLI 的隔离插件注册和两项目技能解析、Desktop 对话自动使用、原生 executor 关闭/恢复分别验收，并绑定各自实测源码；目录夹具或旧源码通过不能替代新候选宿主验收。

组件按冻结清单逐文件取入，见 [CORE 来源](provenance/core.json)、[RESOURCE 来源](provenance/resource.json)。组合测试与非作者复核绑定后才可称为本地集成就绪；不能相加组件的旧测试数。真实宿主加载与回收仍是单列边界。

## 本地使用与验证

组合版需要 Python 3.12+；Windows 受管进程隔离另外依赖系统 Job Object 能力。常规文件工具只用标准库，无隐式依赖升级。开发时运行官方 skill 校验器，另在隔离验证环境安装 [固定验证依赖](requirements-validation.txt)，不写共享解释器或全局环境。

```powershell
python -B tools/package.py build --output C:/explicit-artifacts/candidate.zip
python -B tools/profile.py init --profile C:/explicit-isolated-profile
python -B tools/profile.py install --profile C:/explicit-isolated-profile --archive C:/explicit-artifacts/candidate.zip
python -B tools/profile.py status --profile C:/explicit-isolated-profile
python -B tools/profile.py uninstall --profile C:/explicit-isolated-profile
```

上述 `profile.py` 管理显式隔离目录内的插件文件副本和本工具的安装指针，不修改 Codex 的全局配置、hooks 或正在运行的会话，也不执行官方宿主安装。升级再次运行 `install`；`rollback` 恢复上一个已保全副本。卸载保留账本、用户内容及历史版本。所有命令都需要显式目录，拒绝真实用户主目录和当前 Codex home。

打包排除运行目录和缓存；选入的来源路径及其父目录不能是链接或重解析点。需要复用的源码先保全为普通文件副本，不通过别名带入数据。

任务入口和恢复用法见 [CORE 运行说明](skills/isolated-parallel-development-coordination/references/core-runtime.md)，资源实例、策略和动作见 [RESOURCE API](docs/resource/API.md)。同一安装 profile 固定使用一个 state-root；项目账本由规范化项目根派生，不按消息临时切换状态目录。资源实例由多个项目明确共用，资源状态不会替代任务阶段。

在受信宿主代码中，`ResourceBridge.admit` 从真实 RESOURCE 许可生成 CORE admission；只有 granted 才调用已授权的宿主执行入口。`cleanup` 联接 CORE 的当前清理 intent 和 RESOURCE 实际回执，`reconcile_cleanup` 对账未知结果，`release_agent_quota` 只接受 CORE 已确认的关闭/未启动事实。桥接器不另存任务或资源账本，不提供 JSON 自授权。

任务登记明确使用 `task_kind=code|read_only`；旧记录未声明时按代码任务保守处理。只读任务还须明确 `acceptance_type=delivery_verified|user_required`，由 canonical 完成入口核验当前修订、确认依据、成果与源快照，不伪造 MR/CI。不能事后改任务类型、冒认旧 owner 或直接改完成状态来绕过门禁。

可复跑的本地检查：

```powershell
python -X utf8 -B -m unittest discover -s tests -p test_*.py -v
python -X utf8 -B skills/isolated-parallel-development-coordination/scripts/run_core_tests.py --test-root C:/explicit-tests/core --evidence-root C:/explicit-evidence/core
python -X utf8 -B tools/run_resource_tests.py --output C:/explicit-evidence/resource
```

输出目录必须新建。测试只使用自己创建的合成账本、临时进程、文件和无提交的 orphan worktree；原生 actor、main 合入证明为明确夹具，不是宿主验收。CORE 默认不运行创建 Git commit 的用例；包外真实案例明确跳过。RESOURCE 批次使用共享测试互斥，忙或 abandoned 就不启动，不抢占他人资源。隔离插件副本的卸载保护与任务合并后的资源回收分别验证。

正式插件入口是根目录 `plugin.json` 和 `skills/`，兼容声明位于 `.codex-plugin/plugin.json`。本地 marketplace 清单随源码和压缩包提供；测试注册只发生于明确授权的隔离 home。格式来源：[OpenAI 插件打包说明](https://developers.openai.com/plugins/build/plugins)。实际安装、刷新和宿主加载测试按各自有界方案执行。

## 四种资源分别记录

| 资源 | 释放条件与保留内容 |
| --- | --- |
| Agent 执行名额 | 达到本任务合法结束点前保留执行者上下文。代码任务核对目标 main，只读任务核对当前成果及必要用户验收；报告与必要知识外部交接，且无运行工作、活动后代或其它任务仍使用资源后，才评估任务专属执行者。在具体授权范围内正式 close 成功才算释放。完成、idle、消息接受、超时均不足以证明关闭。失败或未知保持占用；恢复必须重新准入。 |
| 模型请求容量 | 宿主或服务端容量限制，与 agent thread limit 分开诊断；关闭 agent 不保证模型请求立即可用。 |
| 受管进程 CPU/内存 | 仅管理明确登记的进程树及资源许可，不接管其它项目进程；agent 名额释放不等于全部 RAM 释放。 |
| 验收环境与存储 | 合法结束点前保留。完成核对与必要保全后，按登记归属回收专属工作树、临时库及验收环境；共享资源先核对无其它任务引用。归档保留代码提交或只读输入快照、关键决策、验收结论与最小复现证据。 |

任务交付状态、actor 的正式关闭状态和资源许可互相引用，任务账本只有一份。资源存储不能再维护一套任务阶段。当前仍在工作的作者不能因上一项任务完成而被回收。共享、受保护或仍由其它任务使用的资源排除在回收范围之外。agent 记忆不能假定无损恢复，结束前须写下必要知识。本轮回收实验仅限本插件登记的隔离临时测试资源，真实历史资源与当前四名执行者不动。

`task_completed` 与 `cleanup_completed` 分开记录；清理部分失败为 `pending_cleanup`，保留任务责任、已完成步骤和恢复依据。未知结果先核对，不盲重试，不虚报名额或内存已释放。后续 bug 从最新 main 建新任务并关联旧记录；部署是独立任务，使用冻结提交与制品，不依赖旧开发环境。

先前“暂停或等待验收时默认提前关闭 agent”的草案已于 2026-10-31 撤回，见 [生命周期边界与撤回记录](docs/lifecycle.md)；不作为可执行策略。

## 来源与授权

初始内核是 `2026-10-30/implementation-r7` 的冻结源码，独审为 R8；旧插件 R6 及其独审另行绑定，见 [来源清单](provenance/baseline.json)。历史只读，既有失败证据不改。CORE、RESOURCE 各自清单绑定实际取入文件；组合回归与独审以最终源码和包的哈希为准，旧通过结论不继承到新源码。

真实执行、消息发送、Git 提交/推送/合并、部署、模型配置、全局安装与数据修改各守其授权边界。消息送达仅表示接受，执行完成须核对终态和成果。没有正式宿主适配能力时如实报告，不创建用户聊天、计时器或新 worker 充当替代。
