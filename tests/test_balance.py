import asyncio
import importlib.util
import sys
from dataclasses import FrozenInstanceError
from decimal import Decimal
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
    message_api = ModuleType("astrbot.api.message_components")

    class Image:
        def __init__(self, data):
            self.data = data

        @classmethod
        def fromBytes(cls, data):
            return cls(data)

    message_api.Image = Image

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
        "astrbot.api.message_components": message_api,
    }.items():
        monkeypatch.setitem(sys.modules, name, value)
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.main", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_admin_declaration_and_cache_key_isolation(main, monkeypatch):
    assert main.WalletBalance.wallet.permission == "admin"
    p = provider()
    plugin = main.WalletBalance(context(p), {"output_mode": "text"})
    calls = []

    async def fetch(target, timeout):
        calls.append(target.key)
        return balance.parse_balance_snapshot(PAYLOAD)

    monkeypatch.setattr(main, "fetch_balance_snapshot", fetch)
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

    monkeypatch.setattr(main, "fetch_balance_snapshot", fetch)
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


def test_structured_snapshot_preserves_decimal_precision_and_is_immutable():
    payload = {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "CNY",
                "total_balance": "25.12345678",
                "topped_up_balance": "20.00000001",
                "granted_balance": "5.12345677",
            }
        ],
    }
    snapshot = balance.parse_balance_snapshot(payload)
    assert isinstance(snapshot, balance.BalanceSnapshot)
    assert isinstance(snapshot.balances, tuple)
    row = snapshot.balances[0]
    assert row.total == Decimal("25.12345678")
    assert row.topped_up == Decimal("20.00000001")
    assert row.granted == Decimal("5.12345677")
    assert snapshot.as_text() == balance.parse_balance(payload)
    assert "CNY 总余额：25.12" in snapshot.as_text()
    with pytest.raises(FrozenInstanceError):
        row.total = Decimal("0")
    with pytest.raises(FrozenInstanceError):
        snapshot.is_available = False


@pytest.mark.parametrize("value", ["0.00000000", "-0.00000001", "-123.87654321"])
def test_structured_snapshot_keeps_zero_and_negative_amounts(value):
    payload = {
        "is_available": False,
        "balance_infos": [dict(PAYLOAD["balance_infos"][0], total_balance=value)],
    }
    snapshot = balance.parse_balance_snapshot(payload)
    assert snapshot.is_available is False
    assert snapshot.balances[0].total == Decimal(value)


@pytest.mark.parametrize(
    "field", ["currency", "total_balance", "topped_up_balance", "granted_balance"]
)
@pytest.mark.parametrize("missing", [False, True])
def test_structured_snapshot_rejects_null_or_missing_fields(field, missing):
    row = dict(PAYLOAD["balance_infos"][0])
    if missing:
        del row[field]
    else:
        row[field] = None
    with pytest.raises(balance.BalanceError):
        balance.parse_balance_snapshot({"is_available": True, "balance_infos": [row]})


def test_text_fetch_wrapper_remains_compatible(monkeypatch):
    snapshot = balance.parse_balance_snapshot(PAYLOAD)
    target = balance.Target("source", balance.OFFICIAL_URL, "secret-test-key")
    calls = []

    async def fetch(actual_target, timeout):
        calls.append((actual_target, timeout))
        return snapshot

    monkeypatch.setattr(balance, "fetch_balance_snapshot", fetch)
    assert asyncio.run(balance.fetch_balance(target, 7)) == snapshot.as_text()
    assert calls == [(target, 7)]


def wallet_event():
    event = NS(
        unified_msg_origin="session-a",
        stopped=False,
        plain_result=lambda text: text,
        chain_result=lambda images: images,
    )
    event.stop_event = lambda: setattr(event, "stopped", True)
    return event


def install_renderer(monkeypatch, render=None):
    calls = []
    module = ModuleType(f"{PACKAGE}.balance_renderer")

    def record(**kwargs):
        calls.append(kwargs)
        return render(**kwargs) if render else kwargs["currency"].encode()

    module.render_balance_bubble = record
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return calls


def snapshot_fetch(monkeypatch, main, payload=PAYLOAD):
    snapshot = balance.parse_balance_snapshot(payload)
    calls = []

    async def fetch(target, timeout):
        calls.append((target, timeout))
        return snapshot

    monkeypatch.setattr(main, "fetch_balance_snapshot", fetch)
    return snapshot, calls


def wallet_results(plugin, event):
    async def run():
        return [item async for item in plugin.wallet(event)]

    return asyncio.run(run())


def test_default_image_uses_full_precision_total_and_minimal_fields(main, monkeypatch):
    payload = {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "USD",
                "total_balance": "25.12345678",
                "topped_up_balance": "20.00000001",
                "granted_balance": "5.12345677",
            }
        ],
    }
    snapshot, fetch_calls = snapshot_fetch(monkeypatch, main, payload)
    render_calls = install_renderer(monkeypatch)
    monkeypatch.setattr(main, "query_time", lambda: "原始查询时间 北京时间")
    plugin = main.WalletBalance(context(provider(key="secret-test-key")), {})
    event = wallet_event()
    results = wallet_results(plugin, event)
    assert event.stopped
    assert len(results) == 1 and len(results[0]) == 1
    assert isinstance(results[0][0], main.Image)
    assert results[0][0].data == b"USD"
    assert render_calls == [
        {
            "currency": "USD",
            "total": Decimal("25.12345678"),
            "is_available": True,
            "query_stamp": "原始查询时间 北京时间",
            "cached": False,
            "font_path": None,
        }
    ]
    assert "secret-test-key" not in repr(render_calls)
    assert len(fetch_calls) == 1
    assert next(iter(plugin._cache.values()))[1] is snapshot


def test_multiple_currencies_produce_independent_images(main, monkeypatch):
    payload = {
        "is_available": False,
        "balance_infos": [
            dict(PAYLOAD["balance_infos"][0], total_balance="0.00000000"),
            {
                "currency": "USD",
                "total_balance": "-0.00000001",
                "topped_up_balance": "0",
                "granted_balance": "-0.00000001",
            },
        ],
    }
    snapshot_fetch(monkeypatch, main, payload)
    calls = install_renderer(monkeypatch)
    plugin = main.WalletBalance(context(provider()), {"font_path": "custom-font.ttf"})
    results = wallet_results(plugin, wallet_event())
    assert [image.data for image in results[0]] == [b"CNY", b"USD"]
    assert [call["currency"] for call in calls] == ["CNY", "USD"]
    assert [call["total"] for call in calls] == [Decimal("0"), Decimal("-0.00000001")]
    assert all(
        not {"topped_up", "granted", "source_label"}.intersection(call)
        for call in calls
    )
    assert all(call["is_available"] is False for call in calls)
    assert all(call["font_path"] == "custom-font.ttf" for call in calls)


def test_cached_images_keep_original_query_time_and_expire(main, monkeypatch):
    _, fetch_calls = snapshot_fetch(monkeypatch, main)
    calls = install_renderer(monkeypatch)
    now = [100.0]
    monkeypatch.setattr(main, "time", NS(monotonic=lambda: now[0]))
    stamps = iter(["第一次查询 北京时间", "第二次查询 北京时间"])
    monkeypatch.setattr(main, "query_time", lambda: next(stamps))
    plugin = main.WalletBalance(context(provider()), {"cache_seconds": 30})

    async def run():
        assert len([item async for item in plugin.wallet(wallet_event())]) == 1
        now[0] = 120.0
        assert len([item async for item in plugin.wallet(wallet_event())]) == 1
        now[0] = 131.0
        assert len([item async for item in plugin.wallet(wallet_event())]) == 1

    asyncio.run(run())
    assert len(fetch_calls) == 2
    assert [call["cached"] for call in calls] == [False, True, False]
    assert [call["query_stamp"] for call in calls] == [
        "第一次查询 北京时间",
        "第一次查询 北京时间",
        "第二次查询 北京时间",
    ]


def test_text_mode_never_loads_renderer(main, monkeypatch):
    snapshot_fetch(monkeypatch, main)
    monkeypatch.setitem(sys.modules, f"{PACKAGE}.balance_renderer", None)
    plugin = main.WalletBalance(context(provider()), {"output_mode": "text"})
    results = wallet_results(plugin, wallet_event())
    assert len(results) == 1 and isinstance(results[0], str)
    assert "CNY 总余额：110.00" in results[0]
    assert "气泡图片暂不可用" not in results[0]


@pytest.mark.parametrize("failure", ["render", "import"])
def test_image_failure_falls_back_to_actual_balance_without_secret(
    main, monkeypatch, failure
):
    snapshot_fetch(monkeypatch, main)
    warnings = []
    monkeypatch.setattr(main, "logger", NS(warning=warnings.append))
    if failure == "import":
        monkeypatch.setitem(sys.modules, f"{PACKAGE}.balance_renderer", None)
    else:

        def broken(**kwargs):
            raise RuntimeError("Bearer secret-test-key")

        install_renderer(monkeypatch, broken)
    plugin = main.WalletBalance(context(provider(key="secret-test-key")), {})
    results = wallet_results(plugin, wallet_event())
    assert len(results) == 1 and isinstance(results[0], str)
    assert "气泡图片暂不可用" in results[0]
    assert "CNY 总余额：110.00" in results[0]
    assert "充值余额：100.00" in results[0] and "赠送余额：10.00" in results[0]
    assert "查询时间：" in results[0]
    assert warnings and "secret-test-key" not in repr(warnings + results)


def test_second_currency_render_failure_returns_complete_text_no_partial_image(
    main, monkeypatch
):
    payload = {
        "is_available": True,
        "balance_infos": [
            PAYLOAD["balance_infos"][0],
            dict(PAYLOAD["balance_infos"][0], currency="USD", total_balance="12.34"),
        ],
    }
    snapshot_fetch(monkeypatch, main, payload)

    def render(**kwargs):
        if kwargs["currency"] == "USD":
            raise RuntimeError("unavailable")
        return b"first image"

    calls = install_renderer(monkeypatch, render)
    results = wallet_results(
        main.WalletBalance(context(provider()), {}), wallet_event()
    )
    assert len(calls) == 2
    assert len(results) == 1 and isinstance(results[0], str)
    assert "CNY 总余额：110.00" in results[0] and "USD 总余额：12.34" in results[0]


@pytest.mark.parametrize("failure", ["safe", "unexpected", "malformed"])
def test_query_errors_never_render_or_cache_or_disclose_credentials(
    main, monkeypatch, failure
):
    calls = install_renderer(monkeypatch)
    warnings = []
    monkeypatch.setattr(main, "logger", NS(warning=warnings.append))

    async def fetch(target, timeout):
        if failure == "safe":
            raise balance.BalanceError("safe error")
        if failure == "unexpected":
            raise RuntimeError("Bearer secret-test-key https://private.example")
        return balance.parse_balance_snapshot({"private": "secret-test-key"})

    monkeypatch.setattr(main, "fetch_balance_snapshot", fetch)
    plugin = main.WalletBalance(context(provider(key="secret-test-key")), {})
    event = wallet_event()
    results = wallet_results(plugin, event)
    assert event.stopped
    assert len(results) == 1 and isinstance(results[0], str)
    assert not calls and not plugin._cache
    assert "secret-test-key" not in repr(results + warnings)
    assert "private.example" not in repr(results + warnings)


def test_rejected_provider_does_not_query_or_render(main, monkeypatch):
    _, fetch_calls = snapshot_fetch(monkeypatch, main)
    calls = install_renderer(monkeypatch)
    plugin = main.WalletBalance(context(provider("https://relay.example/v1")), {})
    results = wallet_results(plugin, wallet_event())
    assert len(results) == 1 and isinstance(results[0], str)
    assert "第三方中转" in results[0]
    assert not fetch_calls and not calls and not plugin._cache
