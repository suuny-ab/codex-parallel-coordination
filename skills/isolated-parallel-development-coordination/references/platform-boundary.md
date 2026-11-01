# 平台能力边界

本页描述可用组件及证据界限；不声明任何安装者当前已加载或未安装。旧轮次宿主调查、失败轨迹、复测编号和未启用方案保留在[历史索引](history/index.md)，不作为当前任务授权或通用执行步骤。

| 能力 | 当前边界与核验责任 |
| --- | --- |
| 任务账本与角色门禁 | CORE 有唯一项目账本、有限调用、修订/身份/来源绑定。hash 和 actor 字段不认证宿主身份；绕过公开入口的直接工具/文件操作不受本组件强制拦截。 |
| 派发与消息 | 宿主提供实际可用且获授权的回调。原生工具和既有 App thread 按实际调用上下文映射；发送接受、反向确认、终态和入账分别验证，不创建替代用户聊天。 |
| 工作时收件/收尾 | 已配置项目显式 work-cycle 读取/有限对账，recover/cycle 保留底层用途。一次 work-cycle/closeout 不保证看到工具不可见的消息；不保证闲置主会话自动唤醒。 |
| 后台与 Stop | 不打包或安装 hooks，不运行 daemon/cron/定时轮询。Stop 原型只验证过夹具输入输出，是有界续跑提醒，不是阻止或撤回 final 的硬门禁。 |
| Git | 低层 merge-gate 是 GitLab 离线门禁/显式临时裸库 CAS；真实 GitHub/GitLab 合入可由 CORE/real-use 核已发生事实。本组件不执行网络 merge/tag/push，也不保证远端无竞态。 |
| 资源 | Windows Job Object 管理明确登记的进程，RESOURCE 许可不证明 agent 已启动。实际 agent close/resume、共享引用解除和逐资源回收须对应真实宿主证据。 |
| 安装与真实使用 | 包、profile 文件副本、宿主注册、会话 skill 加载、跨项目默认使用、原生关闭/恢复分别核验；某台机器一次加载成功不能证明其它环境或两个真实项目通过。 |

接口按动作查 [CORE](core-runtime.md)、[消息/交付契约](delivery-delegation.md)、[真实任务事实承接](real-use-closeout.md)。测试与打包入口见[验证说明](../../../docs/validation.md)。进程、目录、CI/ref fixture 仅证明所测协议；旧通过和组件数量不能替代当前源码的组合验证或独审。
