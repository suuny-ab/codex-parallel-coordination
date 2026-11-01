# 本地验证与打包

从仓库根执行，Python 3.12+，Windows 核实实际解释器并加 `-B`。测试/证据输出目录必须属于当前任务且新建；不要使用 live 账本、共享安装缓存或真实业务资源。只选择受改动影响的检查，保全失败、修复、复跑和跳过，不默认重跑重型资源 suite。

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

任务交接修复的定向回归为 `python -X utf8 -B -m unittest discover -s tests -p test_handoff_faults.py -v` 和 `-p test_task_handoff.py`，另运行既有 dispatch/host/late、CORE 和组合检查。新增用例验证实际 Node/Python EOF退出及原锁释放、容量拒绝/unknown、原身份观察、报告门禁及重复/迟到恢复；native 回复仍是 fixture。实际 host 的有限观察与最终收件需使用已获授权的既有任务，由受信宿主及集成者另核，不为验证另造任务或重复 spawn。

```powershell
python -X utf8 -B -m unittest discover -s tests -p test_*.py -v
python -X utf8 -B skills/isolated-parallel-development-coordination/scripts/run_core_tests.py --test-root C:/explicit-tests/core --evidence-root C:/explicit-evidence/core
python -X utf8 -B tools/run_resource_tests.py --output C:/explicit-evidence/resource
```

CORE 默认排除创建临时 Git commit 的五个旧用例；确有临时 Git 授权才加 `--allow-disposable-git`，仅限独占验证根、不 push。包外真实案例缺失明确 skip，不沿用历史数字。RESOURCE 批次有共享测试互斥，忙或 abandoned 不抢占。

进程、orphan worktree、目录/SQLite、native/CI/ref fixture 均按各自测试范围说明，不能冒充真实宿主验收。打包排除运行目录和缓存；来源文件及父目录不能是链接/重解析点。来源与包 hash、测试输出和实际范围绑定后才可交独审，组件测试数不能相加作为组合通过。
