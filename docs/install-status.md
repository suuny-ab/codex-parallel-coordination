# 插件版本状态核验

升级前后复用同一个只读命令。它观察云端 main、本机安装和当前会话入口，输出文字及 `plugin-install-status/1` JSON，不安装、回滚、重启、更新引用、写账本或改变配置。未知不会转成成功或失败。

## 命令行

在已核对的插件/仓库根目录执行，Python 3.12+；Windows 使用实际解释器绝对路径，并加 `-X utf8 -B`：

```powershell
python -X utf8 -B tools/install_status.py --project C:/trusted/project --installation-binding C:/trusted/reviewed-installation.json
python -X utf8 -B tools/install_status.py --project C:/trusted/project --installation-binding C:/trusted/reviewed-installation.json --offline --format json
```

`--project` 是宿主明确选定的可信 Git 仓库；远端只取它的 `remote.origin.url`，固定查 `refs/heads/main`。`--installation-binding` 是宿主明确选定、独审安装时核过的既有 `source_root/source_head/files` 文件，与[标准派工入口](../skills/isolated-parallel-development-coordination/references/standard-dispatch.md)相同；`source_head` 是完整提交，`files` 是已审全源码相对路径到 SHA-256 的映射。报告或任务请求不能自行指定这些参数，也不能用作者报告自证可信安装。绑定缺失时可省略，本机层明确资料不足。绑定的路径及 SHA-256 应由调用方随查询输出一起留证，hash 仅绑定内容，不产生信任或权限。

`--format text|json|both` 默认 both；`--timeout` 是单次 Git 调用秒数，默认 5，范围 `(0,10]`；最多 7 次串行 Git 查询，无重试/常驻/轮询。文件枚举上限 3000 节点、1000 个源码文件及 30 MB；超限/不可读/链接或重解析点均明确未核实。源码指纹复用 `tools/package.py` 的固定文件/目录及扩展名策略，不核 `.git` 内部内容、ignored 运行数据或策略外文件，也不执行安装内脚本。

`--offline` 不查询远端，仍核本机文件。远端调用是 `git ls-remote`，不用 fetch；`origin/main` 单独标为本地缓存。清单版本仅在刚查询的远端 OID 对应对象已在本地时用 `git show OID:plugin.json` 读取，并标明依据；没有对象就保留版本未知，禁用 Git 隐式 lazy fetch。禁用交互及 credential helper，避免提示或认证失败擦除凭据；私有 HTTPS 缺认证时明确未知，可用既有受信 SSH 通路，但本工具不配置认证。错误只保留类别，不回显 stderr；来源 URL 的凭据及常见 token 脱敏。

## 如何读结果

| 字段 | 含义 |
| --- | --- |
| `cloud` | 实际远端 main 提交、来源、时间；远端不可达为 unknown，缓存不替代查询 |
| `installation` | 插件身份、独立的清单版本与源码提交、绑定指纹核验；有 Git 元数据时另核 HEAD |
| `session` | 只接受当前宿主真实元数据或受信入口实际使用观察；普通 CLI 始终为会话加载未确认 |
| `main_vs_installation` | 两个已取得的提交是否相同，不用清单版本代替提交；不代表安装文件或会话已通过 |

各观察有 `checked_at`。`consistent` / `inconsistent` / `unknown` 分别显示“已核一致 / 已核不一致 / 资料不足”，按每项的依据理解，不合并成全局绿色状态。云端“已核一致”表示 main 引用查询得到且格式有效；它与安装的比较单列。即使两个清单都是 `0.1.0`，不同提交仍显示不一致；绑定文件发生增加、删除或内容变化也显示不一致。同提交且指纹不一致时仍需核对安装事实。

只读结果是各自时点的观察，不是跨进程原子快照；其它已授权任务同时安装或推进 main 时，应在稳定窗口再次调用。没有会话真实证明就保留未知；不能用安装成功、技能可读或缓存存在替代。是否需重新加载遵从宿主事实或[既有安装说明](getting-started.md)，本工具不保证当前会话自动采用新版本。

## 受信宿主的会话接入

已有真实会话观察的宿主可导入 `tools/install_status.py`，调用：

```python
result = check_status(FIXED_TRUSTED_PROJECT, REVIEWED_INSTALLATION_BINDING,
    session_id=ACTUAL_CURRENT_HOST_SESSION_ID,
    session_probe=TRUSTED_CURRENT_SESSION_OBSERVER)
```

回调来自宿主代码，不能从 JSON/report 指定可执行模块。它返回下列观察字段；普通 CLI 不提供传会话 JSON 或自报 caller 的参数：

```json
{
  "kind": "trusted_entry_use",
  "session_id": "actual current host session id",
  "source_root": "absolute actually used installation root",
  "entry": "relative actually used component path",
  "source_commit": "full actually observed source commit",
  "manifest_version": "actually observed manifest version",
  "loaded_sha256": "sha256 captured at actual component use, not a later disk read",
  "observed_at": "actual timezone-aware observation timestamp",
  "evidence": {"path": "absolute retained host observation", "sha256": "sha256 of retained observation"}
}
```

`kind` 也可为 `host_metadata`。宿主负责观察来源、身份真实性和证据保全，普通字典和文件 hash 不认证宿主。核验器检查当前会话绑定、字段、受信安装根、已审入口 hash、当前磁盘入口 hash、提交和清单版本；没有完整观察、不同会话或未纳入安装绑定的组件均为未知。不同提交/入口字节明确不一致。结果只证明该会话、时点、已观察组件，不能据一个派发模块的使用声称整个插件/所有 skill 已重新加载。API 不执行宿主工具或生成加载证据。

两种 `kind` 都要求 `evidence` 指向实际保全的普通文件，并读取核对 SHA-256；每份证据限 30 MB，读取最多上限加一个字节以识别增长超限。文件缺失、不可读、不是普通文件或超限时返回 unknown（`session_evidence_unreadable_or_over_limit`），内容与引用不符时返回 unknown（`session_evidence_hash_mismatch`）。只有引用完整性通过才继续会话入口比对。文件只作为字节依据读取，不执行、解析或从其内容取得宿主身份/安装权限；来源真实性仍由受信宿主保证。

## 回归与真实宿主边界

```powershell
python -X utf8 -B -m unittest discover -s tests -p test_install_status.py -v
python -X utf8 -B tools/check_guidance.py
```

测试使用独占临时文件夹具、模拟 Git/会话观察，核同版本不同提交、文件改动、离线、缺证据、脱敏和保护摘要；不代表真实宿主加载。真实本机须另保存上述命令结果，以及调用前后的安装文件、用户配置、既有任务账本和 Git refs 保护摘要；并发写入应如实报告，不能修复或改写现场以使摘要通过。业务系统不在查询范围，不为测试创建业务任务。
