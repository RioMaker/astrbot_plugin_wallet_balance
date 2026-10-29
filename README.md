# API 钱包余额

查询 **API Key 所属账户的钱包余额**。默认对接 DeepSeek 官方 `GET https://api.deepseek.com/user/balance`，把总余额和查询时间写入角色图片的气泡内，发送静态 PNG。请求仅查询余额，不调用聊天模型。

## 静态气泡图

管理员使用 `/钱包余额`、`/余额查询` 或 `/wallet` 即可获取图片。气泡展示“钱包余额”、蓝紫渐变金额和查询时间，群聊增加一行“本群今日估算”；缓存或 API 不可用时额外提示状态。余额保留两位小数；人民币显示 `¥`，美元标明 `USD`，不同币种分别生成图片，不换算或相加。底图使用本次提供的角色图片，文字只写在气泡内部，角色和原图尺寸保持不变。

- 默认 `output_mode=image`；设为 `text` 可查看原文字回复，包括充值余额、赠金、API 状态及查询来源等明细。
- 查询结果内部保留 `Decimal` 精度，显示时才格式化；缺失或非法金额按原规则报错，不当作零余额。
- 缓存结果明确标注“缓存”，展示原查询时间；本地重渲染不会更新余额获取时间。
- 图片或字体不可用时自动降级为文字，保留已查到的金额。接口查询失败时返回错误文字，不生成假余额图。
- 第一版为静态图，不包含动画或余额自动轮询。群消耗来自实际调用返回的 Token；不会用账户余额下降来猜测某个群的支出。缓存仅影响余额，群今日统计每次重新读取。

## 群消耗与持久存档

启用插件后，在群消息处理期间观察 Provider 返回的用量，记录输入、缓存命中、输出和总 Token。只采集上游实际报告的数据，不按文本长度推算；未返回用量、模型无法识别、缺少缓存明细或使用中转时，仍保留可读取的 Token，并标记缺失或未估价，不伪造零消耗。

管理员指令（AstrBot 管理员，非普通群管理员）：

| 指令 | 内容 |
| --- | --- |
| `/群消耗汇总` 或 `/群消耗` | 全部已记录平台/机器人的今日总计、累计总计及各群累计列表 |
| `/群消耗汇总 今日` | 所有群今日用量与费用估算 |
| `/群消耗汇总 累计` | 所有群累计汇总 |
| `/群消耗汇总 2026-10-29` | 指定北京日期的历史存档 |
| `/群消耗汇总 今日 2` | 查看第 2 页，每页 10 群，总计不受分页影响 |

群身份按“平台实例 + 机器人 + 群号”隔离，避免不同平台相同群号混合。气泡中的本群费用汇总该群所有已观察到的官方 DeepSeek 调用；它与本次查询的单个 Key 钱包不是相同范围，不能据此核对账户扣款。私聊调用不进入群账本，私聊余额图不显示群行。管理员在私聊也能查询全群汇总。

存档路径为 AstrBot 数据目录下 `plugin_data/astrbot_plugin_wallet_balance/consumption.db`。首次创建时间作为统计起点，汇总中显示北京时间；重启/重载后保留。每日数据不会被新的一天覆盖，也不自动删除。记录按日期、群、提供商、模型、价表版本和峰谷时段聚合，不保存消息正文、用户 ID、API Key 或逐次调用日志。费用用 Decimal 文本保存，升级价表不会重新计算旧记录。

默认数据库上限 64 MiB，`max_archive_mib` 可设 1～1024。达到上限时整次写入回滚并标记记录不完整，不删除历史数据；先备份，再调整上限、重载。文件备份建议停止 AstrBot 后复制数据库，避免事务期间仅复制主文件。未就绪/读取失败时明确提示，不生成假的零统计。

统计覆盖正在处理的群消息内、经已接入 Provider 的普通和流式调用。流式只取最终完整用量，多轮工具调用按各次实际 Provider 请求计入；取消或失败但无用量的请求标记缺失。SDK 内部未暴露的重试、直接通过 HTTP/SDK 发出的调用、消息处理结束后的后台任务及停用插件期间的用量无法恢复。跟踪采用 AstrBot 内部 PipelineScheduler/Provider 接口，已对照 4.29.1 接口并做模拟兼容测试；接入不兼容时显示未就绪或记录不完整，余额查询仍可用。

## DeepSeek 费用估算规则

随插件提供 `deepseek-2026-10-29` 价表，来自 2026-10-29 核对的 [DeepSeek 人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)和[美元价格](https://api-docs.deepseek.com/quick_start/pricing/)。仅官方 HTTPS `api.deepseek.com` 的可识别模型计价，优先取响应中的模型名；中转模型即使同名也不套用官方费率。

以下单价均为每百万 Token，依次为缓存命中输入 / 未命中输入 / 输出：

| 模型 | CNY 闲时 | CNY 峰时 | USD 闲时 | USD 峰时 |
| --- | --- | --- | --- | --- |
| deepseek-flash | 0.02 / 1 / 4 | 0.04 / 2 / 8 | 0.003 / 0.15 / 0.6 | 0.006 / 0.30 / 1.2 |
| deepseek-v4-pro | 0.15 / 4.5 / 13.5 | 0.30 / 9 / 27 | 0.022 / 0.66 / 1.98 | 0.044 / 1.32 / 3.96 |

明确的 Flash 旧别名 `deepseek-v4-flash`、`deepseek-v4-flash-vision-exp` 和价表列出的实际版本名可匹配；未知旧型号不猜价格。金额按 `(缓存命中 × 命中价 + 未命中 × 未命中价 + 输出 × 输出价) / 1000000` 计算，人民币与美元分别套用官方价表，不做汇率换算。

以**本地请求开始的北京时间**确定日期和时段：周一至周五（中国法定节假日除外）09:00～12:00、14:00～18:00 为峰时，右端点不包含；其他时间及周末全天为闲时，周末调休上班也按闲时。官网未明确服务端扣费采用请求到达还是完成时间，跨时段请求可能与账单有差异，所以始终标记“估算”。

内置 2026 年国务院放假安排，来源为[官方转载通知](https://www.beijing.gov.cn/cs/gncs/zcwj/202603/t20260327_4568275.html)。未提供的年份、早于本版核价日期的调用或未知模型，保留 Token 并标记未估价。当前版本不会联网自动改价；官网调价或新年度假期公布后需更新插件价表及日历。历史记录保留原估算。

费用不含独立按次计费的 Search/Fetch、其他插件直接发出的请求或未暴露的重试。群汇总显示已记录 Token 与估算费用，并区分缺失用量、未估价和写入失败；费用不是账户实扣账单。金额显示四位小数，非零且小于 0.0001 时显示 `<0.0001`，避免隐藏微小消耗。

## 安装

将本目录复制到运行中的 AstrBot 的 `data/plugins/astrbot_plugin_wallet_balance`，不要多套一层目录，也不要复制 `.git`、缓存或测试环境。在 AstrBot 使用的 Python 环境安装 `requirements.txt`，然后重启 AstrBot 或在插件管理中重载。

要求 AstrBot `>=4.5.7,<5`、Python >=3.10。已核对当前官方源码的提供商读取接口；该版本范围不是逐版本实机测试声明。查询机必须能通过 HTTPS 访问目标 API。

静态图使用 Pillow，依赖包含在 `requirements.txt`。中文字体默认检测微软雅黑、Noto Sans CJK、文泉驿等系统字体，也可在插件配置 `font_path` 指定字体文件。Linux/Docker 没有中文字体时会退回文字；可在容器内安装 Noto Sans CJK，或挂载字体文件后填写其容器内路径。本插件不自动下载字体。

## 最常用配置：直接查询当前 DeepSeek 钱包

1. 在 AstrBot 的模型服务商配置中填入你自己的 **DeepSeek 官方 API Key**；`api_base` 使用 `https://api.deepseek.com` 或 `https://api.deepseek.com/v1`，并将该模型设为当前会话的聊天模型。余额接口与具体模型名称无关。
2. 打开插件配置，`source_mode` 保持 `current`，`balance_url` 留空。其他字段保持默认，无须重复填写 Key。
3. 在 AstrBot 的管理员配置中添加你自己的平台用户 ID。这里指 AstrBot 管理员，群管理员身份本身不会获得权限。
4. 用管理员账号向机器人发送 `/钱包余额`，也可以用 `/余额查询` 或 `/wallet`。在群里执行时会将余额回复到群里，如需私下查看，请在私聊中查询。

配置示例（插件配置，不包含模型服务商的 Key）：

```json
{
  "output_mode": "image",
  "font_path": "",
  "max_archive_mib": 64,
  "source_mode": "current",
  "provider_id": "",
  "api_key_env": "DEEPSEEK_API_KEY",
  "balance_url": "",
  "timeout_seconds": 10,
  "cache_seconds": 30
}
```

## 固定查询一个 DeepSeek 钱包

如果日常聊天使用别的服务商，但想固定查看 DeepSeek 钱包：把 `source_mode` 改为 `provider`，在 `provider_id` 下拉框中选择已配置的 DeepSeek 聊天模型，`balance_url` 留空。这样无需切换当前会话模型，也不用单独存一份 Key。

## 独立 Key

不想依赖 AstrBot 模型配置时，将 `source_mode` 设为 `manual`。`api_key_env` 填变量名，默认 `DEEPSEEK_API_KEY`，并在 **运行 AstrBot 的同一个进程环境或容器中**设置该环境变量，然后重启 AstrBot。密钥无需发给机器人或写入本插件代码。

Docker Compose 可以通过服务的 `environment` 注入变量，例如 `DEEPSEEK_API_KEY: ${DEEPSEEK_API_KEY}`，变量值在你自己的部署环境中设置。配置修改后需重新创建容器，只有宿主机环境变量而未传入容器时插件无法读取。

独立模式且 `balance_url` 留空时也使用 DeepSeek 官方接口。插件配置中 `api_key_env` 填的是变量名，不是 `sk-...` 密钥。

## 第三方平台与边界

- **使用中转平台的 DeepSeek 模型，不等于使用 DeepSeek 官方钱包。** 插件不会根据模型名称猜测钱包，也不会把第三方 Key 自动发送给 DeepSeek。
- 当前只实现 **DeepSeek 余额响应格式**。第三方平台明确提供兼容格式的接口时，可以在 `balance_url` 填写其完整 HTTPS 地址；复用模型 Key 时必须与模型 `api_base` 同源（协议、域名、端口相同）。自定义路径完全按所填地址请求，不自动拼接 `/v1`。
- New API、One API、硅基流动等平台的余额接口和鉴权可能不同，不能只换地址就保证兼容；需针对其官方余额 API 增加适配。
- 接口地址不能带查询参数、用户名、密码或片段。不会跟随 HTTP 重定向，以免把 Key 带到其他地址。
- 多 Key 轮询时，只读取提供商 `get_current_key()` 返回的当前 Key，不修改轮询状态，不遍历、不累加。并发聊天可能改变提供商的当前 Key；需要稳定查询特定账户时使用独立模式或单 Key 的固定模型。
- 钱包余额属于账户，不是订阅套餐额度。一个账户的多个 Key 可能共享同一钱包；群 Token 账本单独采集，两者范围不同。
- 成功查询默认缓存 30 秒，按接口和实际 Key 隔离，标注原查询时间；`cache_seconds=0` 可关闭缓存。错误不缓存。缓存仅存在内存中，重载时清空。
- 图片仅在内存生成并串行渲染，不保留余额截图或逐次查询日志。多币种各自显示，不隐去非人民币余额。
- 默认超时 10 秒，限 1～60 秒。不输出完整 Key、Key 片段、原始响应或上游异常详情到消息和日志。

## 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 无法获取当前模型 | 当前会话是否已配置并启用聊天模型；或改用 `provider` |
| 不是可识别的 DeepSeek 官方接口 | 检查实际 `api_base`；第三方平台需自己的余额 API 和适配 |
| HTTP 401 / 403 | 检查 Key 是否属于该平台、是否失效、是否有权限 |
| HTTP 404 / 格式不兼容 | 该地址可能仅支持聊天，不支持 DeepSeek 余额接口 |
| 超时或无法连接 | 检查运行 AstrBot 的服务器/容器网络，必要时调整超时 |
| 独立模式未读取到 Key | 环境变量是否传给实际 AstrBot 进程/容器，修改后是否重启 |
| 指令不可用 | 检查插件加载日志、AstrBot 管理员 ID、命令前缀及平台唤醒规则 |

## 开发验证

```text
python -m pip install -r requirements.txt pytest ruff
python -m ruff format .
python -m ruff check .
python -m pytest -q
python local_test/preview.py
```

测试覆盖来源选择、域名与同源约束、金额、HTTP 错误、权限、缓存、图片与文字降级，以及 Provider 流式/取消/重载、群隔离、持久存档与容量回滚、真实用量归一化、峰谷计价和管理汇总。HTTP 与 AstrBot 事件测试使用本地模拟，不使用真实密钥；未完成真实 AstrBot、QQ 或 DeepSeek 账户联调。

预览脚本在 `local_test/output/` 生成图片，显著标记“模拟数据 · 非真实余额”，仅用于版式验收，不读取真实密钥。

## 参考与素材

余额展示参考 [DeepSeek Balance Whale Widget](https://github.com/MeteorNOX/DeepSeek-Balance-Whale-Widget) 的余额/充值/赠金拆分、两位小数与时间/缓存标记，阅读版本固定为提交 `5247dd6813d6e4430728f39ccc63d43cda046022`。本插件独立实现 Python 渲染，不安装参考项目，也不包含其鲸鱼、动图、声音等资源；参考项目说明代码与这些资源的许可范围不同。

本插件底图来源见 [素材说明](assets/README.md)。

## 官方参考

- [DeepSeek 余额接口](https://api-docs.deepseek.com/api/get-user-balance/)
- [DeepSeek 接入与 API Key](https://api-docs.deepseek.com/)
- [AstrBot 当前会话模型接口](https://docs.astrbot.app/dev/star/guides/ai.html)
- [AstrBot 插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)
- [AstrBot 提供商源码](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/provider/provider.py)

接口资料核对日期：2026-10-29。

版本 `0.2.0`：新增角色气泡静态余额图、群真实 Token 日存档、DeepSeek 峰谷费用估算及管理员所有群汇总；保留原钱包指令、管理员权限、密钥来源和缓存规则。
