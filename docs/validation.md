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

## 隔离副本工具

```powershell
python -X utf8 -B tools/profile.py init --profile C:/explicit-isolated-profile
python -X utf8 -B tools/profile.py install --profile C:/explicit-isolated-profile --archive C:/explicit-artifacts/candidate.zip
python -X utf8 -B tools/profile.py status --profile C:/explicit-isolated-profile
python -X utf8 -B tools/profile.py uninstall --profile C:/explicit-isolated-profile
```

这里只管理显式目录内插件文件副本与安装指针，不执行官方宿主安装，不修改全局配置、hooks 或运行会话。升级再次 install，rollback 恢复上个已保全副本；卸载保留用户内容、账本及历史版本。拒绝真实用户主目录和当前 Codex home。调用这些命令仍需对应范围授权，不是看到示例就获得安装权限。

## 运行逻辑改动时的回归入口

```powershell
python -X utf8 -B -m unittest discover -s tests -p test_*.py -v
python -X utf8 -B skills/isolated-parallel-development-coordination/scripts/run_core_tests.py --test-root C:/explicit-tests/core --evidence-root C:/explicit-evidence/core
python -X utf8 -B tools/run_resource_tests.py --output C:/explicit-evidence/resource
```

CORE 默认排除创建临时 Git commit 的五个旧用例；确有临时 Git 授权才加 `--allow-disposable-git`，仅限独占验证根、不 push。包外真实案例缺失明确 skip，不沿用历史数字。RESOURCE 批次有共享测试互斥，忙或 abandoned 不抢占。

进程、orphan worktree、目录/SQLite、native/CI/ref fixture 均按各自测试范围说明，不能冒充真实宿主验收。打包排除运行目录和缓存；来源文件及父目录不能是链接/重解析点。来源与包 hash、测试输出和实际范围绑定后才可交独审，组件测试数不能相加作为组合通过。
