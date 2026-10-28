"""DeepSeek balance adapter. Never include credentials or response bodies in errors."""

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import aiohttp

OFFICIAL_URL = "https://api.deepseek.com/user/balance"
BEIJING = timezone(timedelta(hours=8))


class BalanceError(Exception):
    """A safe, user-facing error."""


@dataclass(frozen=True)
class Target:
    label: str
    url: str
    key: str = field(repr=False)

    @property
    def cache_id(self):
        return hashlib.sha256(f"{self.url}\0{self.key}".encode()).hexdigest()


def https_url(value):
    """Validate administrator configuration without echoing it back."""
    try:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or any(c.isspace() for c in value)
        ):
            raise ValueError
        origin = (parts.scheme, parts.hostname.lower(), parts.port or 443)
    except (ValueError, TypeError):
        raise BalanceError("接口地址必须是无账号、无查询参数的 HTTPS 地址。") from None
    return parts, origin


def validated_key(key):
    if not isinstance(key, str) or not key.strip():
        raise BalanceError("没有可用的 API Key，请在 AstrBot 模型服务商中配置密钥。")
    key = key.strip()
    if any(c.isspace() for c in key):
        raise BalanceError("API Key 格式无效，请检查密钥配置。")
    return key


async def resolve_target(context, config, umo):
    mode = config.get("source_mode", "current")
    override = str(config.get("balance_url", "") or "").strip()
    if mode == "manual":
        name = str(config.get("api_key_env", "DEEPSEEK_API_KEY") or "").strip()
        key = os.environ.get(name, "")
        if not key.strip():
            raise BalanceError(
                "未读取到独立 Key，请设置 api_key_env 指定的环境变量并重启 AstrBot。"
            )
        url = override or OFFICIAL_URL
        https_url(url)
        return Target("独立配置的钱包", url, validated_key(key))

    if mode == "provider":
        provider_id = str(config.get("provider_id", "") or "").strip()
        if not provider_id:
            raise BalanceError("请在插件配置的 provider_id 中选择固定模型提供商。")
    elif mode == "current":
        try:
            provider_id = await context.get_current_chat_provider_id(umo=umo)
        except Exception:
            raise BalanceError(
                "无法获取当前会话模型，请先配置并启用聊天模型，或改用固定提供商模式。"
            ) from None
    else:
        raise BalanceError("source_mode 只能是 current、provider 或 manual。")

    provider = context.get_provider_by_id(provider_id)
    if provider is None:
        raise BalanceError("找不到模型提供商，请重新选择并确认它已启用。")
    provider_config = getattr(provider, "provider_config", {})
    base = str(provider_config.get("api_base", "") or "").strip()
    if not base:
        raise BalanceError(
            "该提供商未提供 api_base，无法判断钱包来源；可改用 manual 模式。"
        )
    parts, origin = https_url(base)
    if override:
        _, target_origin = https_url(override)
        if target_origin != origin:
            raise BalanceError(
                "余额接口必须与模型 api_base 同源；独立钱包请使用 manual 模式。"
            )
        url = override
    elif origin == ("https", "api.deepseek.com", 443) and parts.path.rstrip("/") in (
        "",
        "/v1",
        "/beta",
        "/anthropic",
    ):
        url = OFFICIAL_URL
    else:
        raise BalanceError(
            "当前服务商不是可自动识别的 DeepSeek 官方接口。"
            "第三方中转余额需该平台自己的接口；只有兼容 DeepSeek 余额格式时才能填写 balance_url。"
        )
    getter = getattr(provider, "get_current_key", None)
    if not callable(getter):
        raise BalanceError("该提供商不支持读取当前 API Key，可改用 manual 模式。")
    # Read the provider's current key without rotating or modifying its key pool.
    key = validated_key(getter())
    return Target(
        "固定模型的钱包" if mode == "provider" else "当前会话模型的钱包", url, key
    )


def parse_balance(data):
    try:
        if not isinstance(data, dict) or type(data["is_available"]) is not bool:
            raise ValueError
        infos = data["balance_infos"]
        if not isinstance(infos, list) or not 1 <= len(infos) <= 16:
            raise ValueError
        lines = ["API 可用：" + ("是" if data["is_available"] else "否")]
        for info in infos:
            currency = info["currency"]
            if currency not in ("CNY", "USD"):
                raise ValueError
            amounts = []
            for key in ("total_balance", "topped_up_balance", "granted_balance"):
                value = info[key]
                if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                    raise ValueError
                raw = str(value)
                if len(raw) > 40:
                    raise ValueError
                amount = Decimal(raw)
                if not amount.is_finite() or abs(amount) > Decimal("1e15"):
                    raise ValueError
                amounts.append(f"{amount:.2f}")
            lines.append(
                f"{currency} 总余额：{amounts[0]}\n"
                f"充值余额：{amounts[1]}\n赠送余额：{amounts[2]}"
            )
        return "\n".join(lines)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise BalanceError(
            "余额响应格式不兼容或字段无效；请确认平台支持 DeepSeek /user/balance 格式。"
        ) from None


async def fetch_balance(target, timeout):
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(
                target.url,
                headers={
                    "Authorization": f"Bearer {target.key}",
                    "Accept": "application/json",
                },
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    hints = {
                        401: "API Key 无效或已失效。",
                        403: "没有查询权限，请核对 Key 和平台权限。",
                        404: "平台没有该余额接口，请核对余额接口地址。",
                        429: "查询过于频繁，请稍后重试。",
                    }
                    hint = hints.get(response.status, "平台暂时无法完成余额查询。")
                    raise BalanceError(f"查询失败（HTTP {response.status}）：{hint}")
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise BalanceError("余额接口响应过大，已停止读取。")
                return parse_balance(json.loads(body))
    except BalanceError:
        raise
    except (asyncio.TimeoutError, TimeoutError):
        raise BalanceError("余额查询超时，请稍后重试或调整 timeout_seconds。") from None
    except (aiohttp.ClientError, OSError):
        raise BalanceError(
            "无法连接余额接口，请检查 AstrBot 所在环境的网络。"
        ) from None
    except (ValueError, UnicodeError):
        raise BalanceError("余额接口未返回有效 JSON，请确认地址及平台接口。") from None


def bounded_int(config, name, default, lower, upper):
    try:
        return min(upper, max(lower, int(config.get(name, default))))
    except (TypeError, ValueError, OverflowError):
        return default


def query_time():
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S 北京时间")
