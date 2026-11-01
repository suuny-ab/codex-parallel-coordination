# Codex parallel coordination

私有协调插件 `0.1.0`，用于已授权开发需求的隔离派发、收件、对账和单项用户交接。原主会话是用户唯一入口；每个获批需求由专属开发者实现和自测，唯一集成者独审并在授权内串行合入 main。仓库不包含真实任务账本或业务数据。

首次使用先确认 Python 3.12+；Windows 受管进程隔离还需要 Job Object。运行时使用标准库。插件不提供空闲自动唤醒，不安装 hook 或运行后台轮询。

| 你现在要做什么 | 阅读入口 |
| --- | --- |
| agent 执行协调任务 | [SKILL：共同边界及角色选择](skills/isolated-parallel-development-coordination/SKILL.md)，然后只读本角色当前阶段 |
| 了解主会话、开发者、集成者的分工 | [共同规则](skills/isolated-parallel-development-coordination/references/current-rules.md) |
| 构建、验证或管理隔离文件副本 | [本地验证与打包](docs/validation.md) |
| 调用账本或资源接口 | [CORE 运行契约](skills/isolated-parallel-development-coordination/references/core-runtime.md)、[RESOURCE API](docs/resource/API.md)，按动作查阅 |
| 追查旧版本、撤回策略或失败轨迹 | [历史索引](skills/isolated-parallel-development-coordination/references/history/index.md)，按需读取 |

从仓库根可构建和检查便携包；输出使用自己获准的新路径：

```powershell
python -X utf8 -B tools/package.py build --output C:/explicit-artifacts/candidate.zip
python -X utf8 -B tools/package.py inspect --archive C:/explicit-artifacts/candidate.zip
```

正式入口为 `plugin.json` 和 `skills/`，兼容声明为 `.codex-plugin/plugin.json`；本地 marketplace 清单随包提供。包内不含 hooks。打包、隔离副本安装、宿主注册、当前会话加载及真实任务使用是不同事实：应在目标环境核对插件来源、源码/包 hash、加载的 skill 路径和当前工具能力，不能从本机或旧轮次的通过结论推定其它安装者状态。`profile.py status` 只检查本工具管理的文件副本，不证明宿主已加载。

本组件提供离线协议、有限宿主回调和受管资源接口；实际 Git、消息、安装和部署由获授权的调用方执行。原生关闭/恢复、两个真实项目默认加载和远端原子合并没有由包内 fixture 证明；实际能力见 [平台边界](skills/isolated-parallel-development-coordination/references/platform-boundary.md)。

来源按 [初始基线](provenance/baseline.json)、[CORE](provenance/core.json)、[RESOURCE](provenance/resource.json) 保留。历史 hash 绑定当时文件，旧结果不继承到新源码；组合通过与独审必须绑定最终源码和包，不能相加组件测试数。
