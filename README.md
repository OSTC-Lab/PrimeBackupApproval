# PrimeBackupApproval

PrimeBackupApproval 是 PrimeBackup 的 MCDR 附属插件，
为具有申请资格的玩家提供回档审批。玩家在游戏内提交申请，
审批人员通过 [ApprovalCenter](https://github.com/OSTC-Lab/ApprovalCenter) 的 Discord 消息作出决定，
通过后由玩家再次执行 PB 回档命令。

插件支持申请、查询、取消，以及批准的有效期和执行次数限制。
审批记录与执行次数保存在审批中心，插件重载后恢复处理。
已有 PB 回档权限的用户继续直接使用 PB。

## 使用流程

1. 玩家提交目标备份和申请理由：

   ```text
   !!pba apply 456 误操作破坏了地形，需要恢复到修改前
   ```

   备份参数支持编号、`latest` 和 `~N`。创建时会确定具体备份，
   并返回审批单号、目标编号和审批截止时间。

2. 审批人员在 Discord 卡片上同意或拒绝。玩家收到结果通知，
   也可以使用 `!!pba show <单号>` 查询。

3. 通过后，玩家在有效期内使用申请对应的备份编号回档：

   ```text
   !!pb back 456
   ```

4. 按 PB 提示确认，后续倒计时和回档过程由 PB 执行。

默认审批窗口为 **3 小时**；通过后有效期为 **30 分钟**，**执行次数上限为 10 次**。
这些规则均可配置。

## 安装部署

| 组件           | 版本要求                            |
|----------------|-------------------------------------|
| Python         | ≥ 3.11                              |
| MCDReforged    | ≥ 2.15                              |
| PrimeBackup    | ≥ 1.14                              |

1. 部署 ApprovalCenter，为当前 Minecraft 服务器配置独立的普通 client。
   每个 client 对应一个插件实例。
2. 在 MCDR 使用的 Python 环境中安装 [requirements.txt](requirements.txt) 中的依赖。
   从本项目目录执行：

   ```bash
   python -m pip install -r requirements.txt
   ```

3. 将打包的 `.mcdr` 文件放入 MCDR 插件目录并加载。
   首次加载会自动生成 `config/prime_backup_approval/config.json`。
4. 在生成的配置中填写服务器名称、中心地址、`client_id` 和 `client_secret`，
   重载插件后通过 `!!pba status` 检查运行状态。

配置修改后重载插件生效。审批人员、Discord 频道等设置由 ApprovalCenter 管理。

## 命令

默认命令前缀为 `!!pba`。申请和单据操作面向具有申请资格的玩家，
单据查询与取消限定为玩家自己的申请。控制台可查看帮助和运行状态。

| 命令                        | 功能                                     |
|-----------------------------|------------------------------------------|
| `!!pba` / `!!pba help`      | 查看帮助                                 |
| `!!pba apply <备份> <理由>` | 申请回档，理由可直接包含空格             |
| `!!pba show <单号>`         | 查看申请内容、审批结果、有效期和执行次数 |
| `!!pba list [页码]`         | 按创建时间倒序列出申请，每页 10 条       |
| `!!pba status`              | 查看运行状态和有效审批                   |
| `!!pba cancel <单号>`       | 取消待审批申请                           |

取消成功后释放有效审批额度，记录继续保留供查询。
已同意、拒绝或超时的申请保留原有结果。

游戏内单号可点击查看详情，列表支持点击翻页。
申请、取消和回档入口会将命令预填到聊天输入框，玩家补充或确认后发送。
截止时间按 MCDR 所在系统的本地时区显示。
详情始终展示执行次数；列表和通知等提示在剩余执行次数 ≤3 时展示。

## 批准使用规则

- **限定玩家和目标。** 批准用于申请玩家恢复指定备份。
  使用申请反馈中的固定编号；`latest` 等动态输入指向其他备份时，需要针对该目标申请。
  备份编号对应的文件集发生变化时，原批准失效。
- **按指定命令回档。** 批准适用于 `!!pb back <备份>`。
- **从决定时间计算有效期。** 通知延迟和插件重载保持原有截止时间。
  每张申请使用创建时保存的有效期和执行次数规则。
- **执行次数按放行计算。** 每次交给 PB 处理计一次，包括确认超时、玩家中止、任务繁忙及回档失败。
  使用记录写入成功后发生中断，也保留该次计数。
- **允许重复申请。** 相同目标再次申请时提示已有单号，并继续创建。
  多份批准分别计数，执行时优先使用截止时间最早的一份。

每个玩家默认最多持有 **20 张有效审批单**：审批窗口内的待审批单，
以及使用期内仍有剩余执行次数的批准。达到上限后，可等待申请结束或取消待审批单。

## 配置

配置示例见 [config.example.json](config.example.json)。时间配置的单位均为秒。
首次生成的 client 凭据为空，填写后重载即可启用审批连接。

| 配置项                                    | 默认值                  | 说明                     |
|-------------------------------------------|-------------------------|--------------------------|
| `enabled`                                 | `true`                  | 启用审批扩展             |
| `server_name`                             | `Minecraft Server`      | 审批卡片上的服务器名称   |
| `approval_center.base_url`                | `http://127.0.0.1:8731` | 审批中心地址             |
| `approval_center.client_id`               | 空，需填写              | 当前服务器专用的接入身份 |
| `approval_center.client_secret`           | 空，需填写              | 接入密钥                 |
| `approval_center.request_timeout_seconds` | `2`                     | HTTP 请求超时            |
| `approval.request_permission`             | `1`                     | 玩家申请权限下限         |
| `approval.decision_timeout_seconds`       | `10800`                 | 申请的审批窗口           |
| `approval.grant_validity_seconds`         | `1800`                  | 通过后的使用窗口         |
| `approval.max_uses`                       | `10`                    | 每张批准的执行次数上限   |
| `approval.max_active_per_player`          | `20`                    | 每个玩家的有效审批上限   |
| `polling.interval_seconds`                | `1`                     | 待审批结果查询间隔       |
| `polling.retry_interval_seconds`          | `10`                    | 中心访问失败后的重试间隔 |
| `command.prefix`                          | `!!pba`                 | 附属插件命令前缀         |

时长、次数和额度应为正值。申请权限应满足 PB 的 `root` 和 `confirm` 权限要求。
PB 命令前缀、原始回档权限和恢复策略沿用 PB 配置，附属插件前缀应与 PB 前缀区分。

## 运行与故障处理

插件启动后恢复审批记录，再开放申请和批准使用。
待审批结果默认每秒查询一次，网络请求耗时会影响实际间隔。
玩家离线时保留近期结果，上线后补充通知。

中心暂时无法访问时，插件等待恢复，需要审批的回档也等待结果确认。
具有 PB 原始权限的操作继续由 PB 处理。
申请提交超时后，可先查询已有单据，再按正常流程申请。

执行次数写入失败或结果待确认时，本次回档调用结束；
下一次操作读取中心当前记录。运行日志位于 MCDR 日志中，
包含审批单号、玩家、状态变化和错误信息，可用于排查连接或配置问题。

## 开发

在项目目录、对应 Python 环境中执行：

```bash
python -m pip install -r requirements.dev.txt
python -m unittest discover -s tests -v
python -m mypy
python -m mcdreforged pack -o package
```

打包结果位于 `package/`。
