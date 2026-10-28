import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace as NS

import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "wallet_test_package"
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(f"{PACKAGE}.balance", ROOT / "balance.py")
balance = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = balance
spec.loader.exec_module(balance)

PAYLOAD = {
    "is_available": True,
    "balance_infos": [
        {
            "currency": "CNY",
            "total_balance": "110.00",
            "topped_up_balance": "100.00",
            "granted_balance": "10.00",
        }
    ],
}


def provider(base="https://api.deepseek.com/v1", key="test-key"):
    return NS(provider_config={"api_base": base}, get_current_key=lambda: key)


def context(prov=None):
    async def current(umo):
        assert umo == "session-a"
        return "selected"

    def by_id(provider_id):
        assert provider_id == "selected"
        return prov

    return NS(get_current_chat_provider_id=current, get_provider_by_id=by_id)


def resolve(config=None, prov=None):
    return asyncio.run(balance.resolve_target(context(prov), config or {}, "session-a"))


@pytest.mark.parametrize("path", ["", "/", "/v1", "/v1/", "/beta", "/anthropic"])
def test_current_official(path):
    p = provider("https://api.deepseek.com" + path)
    target = resolve(prov=p)
    assert target.url == balance.OFFICIAL_URL
    assert target.key == "test-key"
    assert "test-key" not in repr(target)


def test_fixed_and_current_key():
    p = provider()
    p.get_keys = lambda: ["unused-key", "other-key"]
    target = resolve({"source_mode": "provider", "provider_id": "selected"}, p)
    assert target.key == "test-key"
    assert "固定" in target.label


@pytest.mark.parametrize(
    "config",
    [
        {"source_mode": "provider"},
        {"source_mode": "invalid"},
        {},
    ],
)
def test_missing_provider(config):
    with pytest.raises(balance.BalanceError):
        resolve(config)


@pytest.mark.parametrize(
    "base",
    [
        "https://relay.example/v1",
        "https://api.deepseek.com.evil.example/v1",
        "https://api.deepseek.com:444/v1",
        "http://api.deepseek.com/v1",
        "https://api.deepseek.com/unknown",
        "",
    ],
)
def test_do_not_guess_wallet_from_model(base):
    with pytest.raises(balance.BalanceError):
        resolve(prov=provider(base))


def test_same_origin_custom_interface():
    target = resolve(
        {"balance_url": "https://relay.example/account/balance"},
        provider("https://relay.example/v1"),
    )
    assert target.url == "https://relay.example/account/balance"
    with pytest.raises(balance.BalanceError, match="同源"):
        resolve(
            {"balance_url": balance.OFFICIAL_URL}, provider("https://relay.example/v1")
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://name:secret@example.com/balance",
        "https://example.com?key=secret",
        "https://example.com/#fragment",
        "https://example.com:invalid",
        "https://example.com/ bad",
    ],
)
def test_invalid_urls(url):
    with pytest.raises(balance.BalanceError) as exc:
        balance.https_url(url)
    assert "secret" not in str(exc.value)


def test_manual_env(monkeypatch):
    monkeypatch.setenv("WALLET_TEST_KEY", "independent-key")
    target = resolve({"source_mode": "manual", "api_key_env": "WALLET_TEST_KEY"})
    assert target.key == "independent-key"
    assert target.url == balance.OFFICIAL_URL
    monkeypatch.delenv("WALLET_TEST_KEY")
    with pytest.raises(balance.BalanceError, match="环境变量"):
        resolve({"source_mode": "manual", "api_key_env": "WALLET_TEST_KEY"})


def test_valid_balance():
    assert "总余额：110.00" in balance.parse_balance(PAYLOAD)
    data = {
        "is_available": False,
        "balance_infos": [
            dict(PAYLOAD["balance_infos"][0], currency="USD", total_balance="-0.01")
        ],
    }
    text = balance.parse_balance(data)
    assert "API 可用：否" in text and "USD 总余额：-0.01" in text


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"is_available": "true"},
        {"is_available": True, "balance_infos": []},
    ],
)
def test_malformed_payload(data):
    with pytest.raises(balance.BalanceError):
        balance.parse_balance(data)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e9999", "bad", True, None])
def test_bad_amounts(value):
    data = {
        "is_available": True,
        "balance_infos": [dict(PAYLOAD["balance_infos"][0], total_balance=value)],
    }
    with pytest.raises(balance.BalanceError):
        balance.parse_balance(data)


@pytest.mark.parametrize("status", [200, 401, 403, 404, 429, 500, 302])
def test_http_contract(status):
    async def run():
        requests = []

        async def handler(request):
            requests.append((request.path, request.headers.get("Authorization")))
            return web.json_response(
                PAYLOAD, status=status, headers={"Location": "/should-not-follow"}
            )

        app = web.Application()
        app.router.add_get("/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            # HTTP is used only for this local transport test; configured URLs require HTTPS.
            target = balance.Target(
                "test", f"http://127.0.0.1:{port}/user/balance", "hidden-key"
            )
            if status == 200:
                assert "110.00" in await balance.fetch_balance(target, 2)
            else:
                with pytest.raises(balance.BalanceError) as exc:
                    await balance.fetch_balance(target, 2)
                assert str(status) in str(exc.value)
                assert "hidden-key" not in str(exc.value)
            assert requests == [("/user/balance", "Bearer hidden-key")]
        finally:
            await runner.cleanup()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["html", "oversize", "timeout"])
def test_bad_http_responses(kind):
    async def run():
        async def handler(request):
            if kind == "timeout":
                await asyncio.sleep(0.05)
            return web.Response(text="x" * (70000 if kind == "oversize" else 8))

        app = web.Application()
        app.router.add_get("/", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            target = balance.Target("test", f"http://127.0.0.1:{port}/", "hidden-key")
            with pytest.raises(balance.BalanceError) as exc:
                await balance.fetch_balance(target, 0.01 if kind == "timeout" else 2)
            assert "hidden-key" not in str(exc.value)
            assert {"html": "JSON", "oversize": "过大", "timeout": "超时"}[kind] in str(
                exc.value
            )
        finally:
            await runner.cleanup()

    asyncio.run(run())


@pytest.fixture
def main(monkeypatch):
    def command(*args, **kwargs):
        return lambda func: func

    def permission(value):
        def decorate(func):
            func.permission = value
            return func

        return decorate

    api = ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = NS(warning=lambda text: None)
    event_api = ModuleType("astrbot.api.event")
    event_api.AstrMessageEvent = object
    event_api.filter = NS(
        command=command, permission_type=permission, PermissionType=NS(ADMIN="admin")
    )
    star_api = ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    star_api.Star = Star
    star_api.Context = object
    for name, value in {
        "astrbot": ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event_api,
        "astrbot.api.star": star_api,
    }.items():
        monkeypatch.setitem(sys.modules, name, value)
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.main", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_admin_declaration_and_cache_key_isolation(main, monkeypatch):
    assert main.WalletBalance.wallet.permission == "admin"
    p = provider()
    plugin = main.WalletBalance(context(p), {})
    calls = []

    async def fetch(target, timeout):
        calls.append(target.key)
        return balance.parse_balance(PAYLOAD)

    monkeypatch.setattr(main, "fetch_balance", fetch)
    event = NS(
        unified_msg_origin="session-a",
        stop_event=lambda: None,
        plain_result=lambda x: x,
    )

    async def run():
        first = [x async for x in plugin.wallet(event)][0]
        second = [x async for x in plugin.wallet(event)][0]
        assert "（缓存）" not in first and "（缓存）" in second
        p.get_current_key = lambda: "new-key"
        third = [x async for x in plugin.wallet(event)][0]
        assert "（缓存）" not in third
        assert calls == ["test-key", "new-key"]
        assert "new-key" not in third
        await plugin.terminate()
        assert not plugin._cache

    asyncio.run(run())


def test_failed_queries_are_not_cached(main, monkeypatch):
    plugin = main.WalletBalance(context(provider()), {})
    calls = []

    async def fetch(target, timeout):
        calls.append(1)
        raise balance.BalanceError("safe error")

    monkeypatch.setattr(main, "fetch_balance", fetch)
    event = NS(
        unified_msg_origin="session-a",
        stop_event=lambda: None,
        plain_result=lambda x: x,
    )

    async def run():
        for _ in range(2):
            assert [x async for x in plugin.wallet(event)] == ["safe error"]
        assert len(calls) == 2
        assert not plugin._cache

    asyncio.run(run())
