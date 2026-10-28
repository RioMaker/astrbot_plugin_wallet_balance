# API 钱包余额

查询 **API Key 所属账户的钱包余额**。默认对接 DeepSeek 官方 `GET https://api.deepseek.com/user/balance`，显示 API 是否可用、总余额、充值余额、赠送余额和北京时间。请求仅查询余额，不调用聊天模型。

## 安装

目前为本地插件，尚无远程仓库或市场发布。将本目录复制到运行中的 AstrBot 的 `data/plugins/astrbot_plugin_wallet_balance`，不要多套一层目录，也不要复制 `.git`、缓存或测试环境。在 AstrBot 使用的 Python 环境安装 `requirements.txt`，然后重启 AstrBot 或在插件管理中重载。

要求 AstrBot `>=4.5.7,<5`、Python >=3.10。已核对当前官方源码的提供商读取接口；该版本范围不是逐版本实机测试声明。查询机必须能通过 HTTPS 访问目标 API。

## 最常用配置：直接查询当前 DeepSeek 钱包

1. 在 AstrBot 的模型服务商配置中填入你自己的 **DeepSeek 官方 API Key**；`api_base` 使用 `https://api.deepseek.com` 或 `https://api.deepseek.com/v1`，并将该模型设为当前会话的聊天模型。余额接口与具体模型名称无关。
2. 打开插件配置，`source_mode` 保持 `current`，`balance_url` 留空。其他字段保持默认，无须重复填写 Key。
3. 在 AstrBot 的管理员配置中添加你自己的平台用户 ID。这里指 AstrBot 管理员，群管理员身份本身不会获得权限。
4. 用管理员账号向机器人发送 `/钱包余额`，也可以用 `/余额查询` 或 `/wallet`。在群里执行时会将余额回复到群里，如需私下查看，请在私聊中查询。

配置示例（插件配置，不包含模型服务商的 Key）：

```json
{
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
- 这里查询账户钱包，不是统计 Token 用量，也不是订阅套餐额度。一个账户的多个 Key 可能共享同一钱包。
- 成功查询默认缓存 30 秒，按接口和实际 Key 隔离，标注原查询时间；`cache_seconds=0` 可关闭缓存。错误不缓存。缓存仅存在内存中，重载时清空。
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
```

测试覆盖来源选择、官方域名识别、同源约束、当前 Key、金额字段、HTTP 错误/重定向/超时、权限声明及缓存隔离。HTTP 测试使用本地模拟服务，不使用真实密钥；未完成真实 AstrBot、QQ 或 DeepSeek 钱包联调。

## 官方参考

- [DeepSeek 余额接口](https://api-docs.deepseek.com/api/get-user-balance/)
- [DeepSeek 接入与 API Key](https://api-docs.deepseek.com/)
- [AstrBot 当前会话模型接口](https://docs.astrbot.app/dev/star/guides/ai.html)
- [AstrBot 插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)
- [AstrBot 提供商源码](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/provider/provider.py)

接口资料核对日期：2026-10-28。
