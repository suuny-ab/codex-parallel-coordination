# 本地验证与打包

从仓库根执行，Python 3.12+，Windows 核实实际解释器并加 `-B`。测试/证据输出目录必须属于当前任务且新建；不要使用 live 账本、共享安装缓存或真实业务资源。只选择受改动影响的检查，保全失败、修复、复跑和跳过，不默认重跑重型资源 suite。

## 插件自身修复

用户明确授权主会话直接修复插件时，使用普通源码变更和独立审核，不以故障派发入口成功为前提。归档分类增补、已知容量拒绝接续的定向用例位于 `tests/test_closeout_recovery.py`；宿主即时/迟到返回的真实身份一致性由 `test_dispatch_host`、`test_dispatch_late` 和 `test_handoff_faults` 覆盖。按既有共享重型测试锁串行验证，不强停其它任务测试。

夹具只能证明同一任务、请求、工作树和许可的接续规则，不能证明真实名额已释放。真实回归另留存成果归档清单、宿主关闭与复核原件、许可释放结果，以及原任务接续的实际作者身份。等待接口返回空状态时保持 `accepted_unconfirmed`，后续用原身份实际观察；不得用测试通过代替真实运行事实。旧材料无法核证时保留失败和未回收状态，不伪造归档或放宽必要凭据检查。

## 文档与便携包

```powershell
python -X utf8 -B tools/check_guidance.py
python -X utf8 -B tools/package.py build --output C:/explicit-artifacts/candidate.zip
python -X utf8 -B tools/package.py inspect --archive C:/explicit-artifacts/candidate.zip
python -X utf8 -B tools/check_guidance.py --archive C:/explicit-artifacts/candidate.zip
```

文档检查只核本次现行入口的本地链接/锚点及包内同路径，不访问外部 URL，不证明全部自然语言等价。skill 结构另用当前可用的官方 `quick_validate.py` 检查 frontmatter、命名及未完成占位；验证依赖按 [requirements-validation.txt](../requirements-validation.txt) 在获准的隔离环境准备，不写共享解释器。规则语义由作者对应表自核、非作者独审及授权后真实流程复核。

本次公开入口与许可分发的受影响回归：

```powershell
python -X utf8 -B -m unittest discover -s tests -p test_public_docs.py -v
python -X utf8 -B -m unittest discover -s tests -p test_packaging.py -v
```

前者把新增公开文档和原有现行指引放在同一链接/锚点检查中，对源码及真实构建包分别核对，并验证标准 MIT 文本及包内字节。后者检查固定根文件分发及既有便携包边界；其中 install/upgrade 等用例只操作临时文件夹具，不安装宿主插件。`LICENSE`、`CONTRIBUTING.md`、`CHANGELOG.md` 必须随根文件清单分发，上手及工作流随 docs 目录分发。

外部链接和官方安装路线需另行核实；测试不会登记 marketplace、改全局配置或创建 agent。文档命令以仓库根为工作目录，示例输出路径是读者自行选择的任务目录。Windows 深目录如遇路径长度错误，可从较短的专属 checkout 验证，或显式使用扩展绝对脚本路径；Git 可仅对当前命令加 `-c core.longpaths=true`，无需改全局设置。

## 隔离副本工具

```powershell
python -X utf8 -B tools/profile.py init --profile C:/explicit-isolated-profile
python -X utf8 -B tools/profile.py install --profile C:/explicit-isolated-profile --archive C:/explicit-artifacts/candidate.zip
python -X utf8 -B tools/profile.py status --profile C:/explicit-isolated-profile
python -X utf8 -B tools/profile.py uninstall --profile C:/explicit-isolated-profile
```

这里只管理显式目录内插件文件副本与安装指针，不执行官方宿主安装，不修改全局配置、hooks 或运行会话。升级再次 install，rollback 恢复上个已保全副本；卸载保留用户内容、账本及历史版本。拒绝真实用户主目录和当前 Codex home。调用这些命令仍需对应范围授权，不是看到示例就获得安装权限。

## 运行逻辑改动时的回归入口

派发反馈范围与 prehost 恢复的定向批次：`python -X utf8 -B tests/run_dispatch_scope_recovery.py --output C:/explicit-evidence/dispatch-scope --batch focused test_dispatch_scope_recovery test_dispatch_prehost_transport`。runner 使用现有 heavy 互斥、独占 TEMP、源码前后指纹，保留红测、失败及复跑；Windows 输出根过长时另选当前任务的较短专属目录，不改全局 longpaths。宿主测试复制原 host 模块字节，只在隔离 Python 端点注入 intent 后强退；覆盖原子进程终态、重复恢复、832 旧版真实实例接入新版 prototype 和实际 stdio 调用回执。直接 helper 调用配合普通 session/child 描述必须被拒绝，并观测到 0 新子进程、0 host 动作。native 返回仍是 fixture，不能据此宣称原生资源关闭/释放。

真实捕获比较用 `tests/profile_dispatch_capture.py --input ABSOLUTE_CAPTURE.json --output ABSOLUTE_NEW_OUTPUT --baseline COMMIT --request ORIGINAL_REQUEST_ID`，保全整份输入字节及比较实际读取的不可变证据，分别报告原/新保存组件耗时。`tests/capture_dispatch_state.py --output ABSOLUTE_NEW_OUTPUT INPUT...` 仅冻结输入及直接绑定工件，不递归扩展历史包。设置 `COORDINATION_REAL_CAPTURE` 为 profile 输出、`COORDINATION_REAL_DISPATCH_INPUT` 为原请求/RESOURCE/profile 冻结目录后，在同一 runner 中增加 `test_dispatch_real_capture`，保留全部历史任务和通知，记录路径/项目/文件及宿主身份的隔离映射；缺输入明确 skip。原 dispatcher 在同一捕获副本仍停在 intent，新入口以原任务/许可走到 host_call 并记录 fixture 原始回复及完整接续耗时。临时模拟路径不是长期凭据；原始捕获、来源字节和重现命令须在树外保全。实际 native 最终验收由获权 ROOT/集成者使用当前既有任务另核。

保全边界定向回归使用 `python -X utf8 -B tools/run_preservation_tests.py --output C:/explicit-evidence/preservation --batch focused test_preservation_boundary test_preservation_refs test_historical_artifacts test_resource_closed_loop test_native_control test_cleanup_runtime`。runner 使用专属 TEMP 和既有 heavy 互斥，保留完整输出、失败/跳过及前后源码指纹。对单个新任务用例可加 `--baseline COMMIT`，只替换原 `preserve` 函数并保留 Git 原始源码字节，在相同批准/文件输入上比较红绿；不切换工作树。旧3案例材料另做有界只读复制，原报告和失败不改。夹具不代表实际旧资源关闭。

派工超时修复使用 `python -X utf8 -B tests/run_dispatch_timeout.py --output C:/explicit-evidence/dispatch-timeout --batch focused test_dispatch_timeout test_dispatch_late test_callback_lock_contention test_dispatch_queued_base`。runner 将临时根登记在该证据目录内，使用现有 heavy 互斥，保留命令、源码前后指纹、失败及跳过。`--baseline COMMIT` 只用于所选原函数的隔离红测，另保留 Git 原始源码字节；不切换或修改工作树。

新用例覆盖 Windows 原进程强退、树创建前后、native 前后、真实返回结构夹具的 ID 保留、活跃 CORE 争用、PID 复用、换锁/账本变化和来源拒绝。精确复制的 host 模块配合隔离故障端点及可控45秒时钟验证强退宽限和两个自有锁恢复，不长睡凑时限；夹具没有创建真实 agent。旧现场材料只读比对，缺创建身份的旧锁继续拒绝自动恢复，三项包外历史材料缺失仍按原测试 skip。

轻量收件定向回归：`python -X utf8 -B -m unittest discover -s skills/isolated-parallel-development-coordination/scripts -p test_main_brief.py -v`。覆盖原完整结果对照、焦点/优先级、内部待办、合入与各资源状态、通知未确认恢复、来源变化、Unicode 详情和分页。均使用隔离账本及宿主夹具；真实项目大小/耗时须另取同一冻结输入测量并保留并发变化和范围限制。固定调用及兼容入口见[轻量收件](../skills/isolated-parallel-development-coordination/references/main-brief.md)。

交付接续定向回归：`python -X utf8 -B -m unittest discover -s tests -p test_delivery_continuation.py -v`。CORE launch/cleanup 及保全接点变更另跑 CORE、相关 handoff/Node、dispatch 和 RESOURCE 组合回归；批次按 `tools/heavy.py` 的 `lease()` 互斥。该用例实际执行临时 Git/文件、本地许可、Node/Python、强退和固定恢复入口，native/安装/独审判断仍为夹具。真实任务经获批安装入口另验，不写 live 台账或造任务凑通过。

任务交接修复的定向回归为 `python -X utf8 -B -m unittest discover -s tests -p test_handoff_faults.py -v`、`-p test_task_handoff.py` 及 `-p test_dispatch_queued_base.py`，另运行既有 dispatch/host/late、CORE 和组合检查。新增用例验证实际 Node/Python EOF退出及原锁释放、容量拒绝/unknown、原身份观察、报告门禁及重复/迟到恢复；纯排队重基线使用专属临时 Git/RESOURCE，覆盖原字节/身份不变、祖先/远端/归属拒绝、无 host 调用及追加时 revision 竞争；native 回复仍是 fixture。实际 host 的有限观察与最终收件需使用已获授权的既有任务，由受信宿主及集成者另核，不为验证另造任务或重复 spawn。

```powershell
python -X utf8 -B -m unittest discover -s tests -p test_*.py -v
python -X utf8 -B skills/isolated-parallel-development-coordination/scripts/run_core_tests.py --test-root C:/explicit-tests/core --evidence-root C:/explicit-evidence/core
python -X utf8 -B tools/run_resource_tests.py --output C:/explicit-evidence/resource
```

CORE 默认排除创建临时 Git commit 的五个旧用例；确有临时 Git 授权才加 `--allow-disposable-git`，仅限独占验证根、不 push。包外真实案例缺失明确 skip，不沿用历史数字。RESOURCE 批次有共享测试互斥，忙或 abandoned 不抢占。

进程、orphan worktree、目录/SQLite、native/CI/ref fixture 均按各自测试范围说明，不能冒充真实宿主验收。打包排除运行目录和缓存；来源文件及父目录不能是链接/重解析点。来源与包 hash、测试输出和实际范围绑定后才可交独审，组件测试数不能相加作为组合通过。

## RESOURCE-FLOW 候选

定向入口使用同一 heavy 互斥：`python -X utf8 -B tools/run_preservation_tests.py --output ABSOLUTE_SHORT_OWNED_OUTPUT --batch NAME test_resource_flow.ResourceFlowTests test_resource_flow_capture test_dispatch test_dispatch_timeout test_delivery_continuation`。原任务副本以 `RESOURCE_FLOW_CAPTURE=ABSOLUTE_CASE_JSON` 明确指定，否则该捕获用例 skip。

`tests/capture_resource_flow_case.py --ledger ACTUAL_READONLY_LEDGER --controller ACTUAL_READONLY_RESOURCE_STATE --launch ACTUAL_MANAGED_LAUNCH --process-created ACTUAL_CREATED_RECEIPT --output NEW_OWNED_CAPTURE` 只读取指定原件、核原 Job/PID 并冻结副本，不做 live adoption；副本重放中的集成角色和用户 sideband 明确是夹具。命令、源码与输入指纹保留在测试结果，不能据此宣称正式收件或关闭。

`tools/run_resource_tests.py` 继续重放原冻结 RESOURCE suite；原 provenance 字段保留，RESOURCE-FLOW 仅追加绑定前后源码 hash 的 amendment，runner 核继续关系及实际字节。已同步实际合 main 的 ARCHIVE 并加入真实移动结果的资源接续联测。34 项场景映射及每次结果保存在作者报告；归档故障、角色及原生宿主夹具不升级为生产闭环通过。入口与场景映射见[资源生命周期](resource/resource-flow.md)。
