# 安装及上手

本页用于首次尝试。先从[README](../README.md)了解用途与限制，再选择自己的测试项目；不要直接以客户环境或共享生产资源试运行。

## 先检查环境

- Python 3.12 或更新版本。运行时只依赖标准库；文档结构验证的可选依赖另见[验证说明](validation.md)。
- Git，及可用的项目仓库。
- 支持本地 marketplace 的 Codex 宿主。还须核对当前会话真实可用的原生派发、观察、消息及集成交接工具；安装插件不会新增这些宿主能力。
- Windows 受管进程需要 Job Object。其它平台的完整资源管理没有在本项目中得到同等验证。

先运行这些无安装副作用的检查：

```shell
python --version
git --version
codex --version
codex plugin marketplace add --help
```

Windows 若 `python` 只是应用商店别名或没有输出版本，改用本机实际 Python 解释器绝对路径。不要把作者机器上的路径当作安装前提。若 Codex 没有该子命令，先核对宿主版本及下方官方说明，不用复制文件冒充安装成功。

## 按官方 marketplace 路线安装

依据 [OpenAI 官方插件打包及 marketplace 说明](https://developers.openai.com/plugins/build/plugins#add-a-marketplace-from-the-cli)（核对日期：2026-11-01）。官方提供添加 Git 仓库来源的 CLI，安装及测试本地插件使用桌面宿主插件界面；账户及组织策略可能限制可用功能。

1. 在自己的终端添加真实仓库来源：

   ```shell
   codex plugin marketplace add suuny-ab/codex-parallel-coordination --ref main
   codex plugin marketplace list
   ```

   `main` 会继续变化，记录实际取到的提交。仅需检查固定修订时，按官方 `--ref` 支持选择已知 Git ref。不要用只取 `.agents/plugins` 的 sparse checkout：此仓库插件位于根目录，完整 `skills/` 和脚本也需要保留。

2. 重启桌面宿主，进入 Plugins Directory（插件目录），选择该仓库来源，找到 `codex-parallel-coordination` 并安装、启用。实际来源显示名及 ID 如下，沿用既有名称，不另建同名副本：

   | 项目 | 仓库中的真实值 |
   | --- | --- |
   | 仓库 | `suuny-ab/codex-parallel-coordination` |
   | marketplace ID | `codex-parallel-coordination-local` |
   | 来源显示名 | `Parallel coordination (local candidate)` |
   | 插件 ID | `codex-parallel-coordination` |
   | 技能 | `isolated-parallel-development-coordination` |
   | 清单版本 | `0.1.0` |

   来源来自 [.agents/plugins/marketplace.json](../.agents/plugins/marketplace.json)，`source.path` 为 `./`，相对 marketplace 根目录解析。正式清单为 [plugin.json](../plugin.json)，兼容清单为 [.codex-plugin/plugin.json](../.codex-plugin/plugin.json)。公开 Git 仓库是分发来源，不表示插件已被公共目录收录。

3. 如需仅在一个可信项目启用，按[官方项目启用说明](https://developers.openai.com/plugins/build/plugins#enable-or-disable-a-plugin-for-a-repo)，在该项目已有的 `.codex/config.toml` 合并下列配置，保留其它设置：

   ```toml
   [plugins."codex-parallel-coordination@codex-parallel-coordination-local"]
   enabled = true
   ```

   先确认自己有权修改该项目配置。宿主只加载受信项目配置，组织策略仍优先；不要覆盖整个配置文件或为了加载插件降低全局保护。

4. 开一个新会话，明确请求使用该技能，并核对实际加载路径、插件来源及当前工具能力。官方也要求[首次安装后用新聊天测试](https://developers.openai.com/learn/developers-codex-plugin#install-the-plugin)。先核查可用能力，再批准真实派发；缺任一必要能力就保留受阻原因。

## 最小使用示例

在自己的示例项目原主会话输入：

> 使用 isolated-parallel-development-coordination 技能。给任务列表增加按状态筛选，保留现有默认结果。先梳理范围和验收条件，呈现一项需求的派工摘要，等我批准后再派发。先不要提交、推送、合并或安装。

预期流程：

1. 主会话写清范围、非目标和验收，例如“默认全部、筛选后仅显示目标状态、清除后恢复全部”，并自核规格、呈现摘要。
2. 你确认摘要并明确本轮实际权限后，才创建专属开发者与工作树。提交、推送、PR、合并等权限分别授权；上面的示例没有授权它们。
3. 开发者实现、自测，保留失败和复跑证据；集成者核当前源码并独立审核。无真实宿主通路时不能用模拟身份继续。
4. 原主会话集中递交确需你回答的事项，并直接展示已核进度。你继续在原主会话沟通，无需管理派生聊天。
5. 获准合入且核实目标 main 包含已测成果后，才登记代码完成；保全、关闭及资源回收还需要各自证据和授权。

该例是自然语言用法，不是作者已在每种宿主实测的保证。标准派工的固定 profile、批准文件和回调接线属于受信集成配置，详见[标准派工入口](../skills/isolated-parallel-development-coordination/references/standard-dispatch.md)，不要把示例 JSON 当作授权或初始化第二份生产账本。

## 如何判断是否装好

| 看到的事实 | 能证明什么 |
| --- | --- |
| marketplace list 有来源 | 来源已登记；尚不证明插件已安装 |
| 插件界面显示已安装、启用 | 宿主安装状态；仍需新会话核对技能加载 |
| 新会话看到技能及正确来源 | 当前会话已加载；尚不证明派发、消息及回收全链路 |
| 一项获批真实需求完成并留证 | 仅证明该宿主、修订及授权范围的实际使用 |
| 构建 ZIP 或 profile status 成功 | 包/隔离文件副本有效；不证明宿主加载 |

包的 build/inspect 和开发者隔离副本工具见[验证与打包](validation.md)。本页不要求手动覆盖用户安装缓存或改全局设置；找不到插件时先核来源、完整仓库、身份、信任及策略，保留错误后按[贡献说明](../CONTRIBUTING.md)反馈。

本次公开文档维护只核对官方路线、真实清单及无副作用命令；未执行宿主安装、启用或跨环境任务验收。
