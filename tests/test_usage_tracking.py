"""Local execution-contract checks; no model requests or credentials are used."""

import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

SPEC = importlib.util.spec_from_file_location(
    "wallet_usage_tracking_tests",
    Path(__file__).resolve().parents[1] / "usage_tracking.py",
)
tracking = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tracking
SPEC.loader.exec_module(tracking)


def response(tokens=7, *, chunk=False):
    return NS(usage=NS(input_other=tokens, input_cached=0, output=0), is_chunk=chunk)


@pytest.fixture
def runtime(monkeypatch):
    class Scheduler:
        async def execute(self, event):
            return await event.action()

    providers, calls, errors, trackers = [], [], [], []
    context = NS(get_all_providers=lambda: providers)
    modules = {"astrbot.core.pipeline.scheduler": NS(PipelineScheduler=Scheduler)}
    monkeypatch.setattr(tracking.importlib, "import_module", modules.__getitem__)
    original_execute = Scheduler.execute

    def make(callback=None):
        async def emit(event, provider, result, failed, started_at):
            calls.append((event, provider, result, failed, started_at))

        tracker = tracking.ProviderUsageTracker(
            context, callback or emit, errors.append
        )
        trackers.append(tracker)
        return tracker

    r = NS(
        scheduler=Scheduler(),
        cls=Scheduler,
        context=context,
        providers=providers,
        calls=calls,
        errors=errors,
        make=make,
        original_execute=original_execute,
        modules=modules,
    )
    yield r
    for tracker in reversed(trackers):
        tracker.uninstall()


def provider(runtime, tokens=7):
    class Provider:
        async def text_chat(self, *args, **kwargs):
            return response(tokens)

        async def text_chat_stream(self):
            yield response(2, chunk=True)
            yield response(tokens)

    p = Provider()
    runtime.providers.append(p)
    return p


def execute(runtime, action):
    return runtime.scheduler.execute(NS(action=action))


def test_message_scope_tool_rounds_and_no_background_attribution(runtime):
    p = provider(runtime)
    original = type(p).text_chat
    t = runtime.make()
    assert t.install() and t.install()

    async def run():
        assert (await p.text_chat()).usage.input_other == 7  # no message scope
        release = asyncio.Event()
        late_task = None

        async def late():
            await release.wait()
            return await p.text_chat()

        async def action():
            nonlocal late_task
            for _ in range(3):  # tool loop: each actual Provider request counts
                await p.text_chat()
            late_task = asyncio.create_task(late())
            return "same-result"

        assert await execute(runtime, action) == "same-result"
        release.set()
        await late_task
        assert len(runtime.calls) == 3
        assert all(row[4].tzinfo is timezone.utc for row in runtime.calls)

    asyncio.run(run())
    t.uninstall()
    assert type(p).text_chat is original
    assert runtime.cls.execute is runtime.original_execute
    assert t.status()["installed"] is False


def test_concurrent_events_keep_their_own_identity(runtime):
    p = provider(runtime)
    assert runtime.make().install()

    async def run():
        async def action():
            await asyncio.sleep(0)
            await p.text_chat()

        events = [NS(action=action, name=n) for n in range(6)]
        await asyncio.gather(*(runtime.scheduler.execute(e) for e in events))
        assert {row[0].name for row in runtime.calls} == set(range(6))

    asyncio.run(run())


def test_same_provider_super_nesting_and_different_provider_count_separately(runtime):
    other = provider(runtime, 3)

    class Base:
        async def text_chat(self):
            return response(8)

        async def text_chat_stream(self):
            yield response(8)

    class Child(Base):
        async def text_chat(self):
            await other.text_chat()
            return await super().text_chat()

        async def text_chat_stream(self):
            yield await self.text_chat()

    base, child = Base(), Child()
    runtime.providers.extend([base, child])
    assert runtime.make().install()

    async def run():
        await execute(runtime, child.text_chat)
        assert [row[1] for row in runtime.calls] == [other, child]
        runtime.calls.clear()

        async def stream():
            return [part async for part in child.text_chat_stream()]

        await execute(runtime, stream)
        assert [row[1] for row in runtime.calls] == [other, child]

    asyncio.run(run())


def test_parallel_same_provider_child_task_is_a_separate_call(runtime):
    class Provider:
        async def text_chat(self, child=False):
            if not child:
                await asyncio.create_task(self.text_chat(child=True))
            return response()

        async def text_chat_stream(self):
            yield response()

    p = Provider()
    runtime.providers.append(p)
    assert runtime.make().install()
    asyncio.run(execute(runtime, p.text_chat))
    assert len(runtime.calls) == 2


def test_stream_final_only_snapshot_and_no_context_across_yield(runtime):
    p = provider(runtime)
    final = response(11)
    final.model = "actual-response-model"
    raw = NS(usage={"total_tokens": 11}, model="test-model", text="not copied")
    final.raw_completion = raw

    async def stream(self):
        yield response(100, chunk=True)
        yield response(9)
        yield final
        final.usage.input_other = 999
        raw.usage["total_tokens"] = 999

    type(p).text_chat_stream = stream
    assert runtime.make().install()

    async def action():
        iterator = p.text_chat_stream()
        first = await anext(iterator)
        assert first.is_chunk
        await p.text_chat()  # caller outside yield is a separate actual call
        remaining = [part async for part in iterator]
        assert remaining[-1] is final

    asyncio.run(execute(runtime, action))
    assert len(runtime.calls) == 2
    observed = runtime.calls[-1][2]
    assert observed.usage.input_other == 11
    assert observed.model == "actual-response-model"
    assert observed.raw_completion.usage["total_tokens"] == 11
    assert not hasattr(observed.raw_completion, "text")
    assert runtime.calls[-1][3] is False


@pytest.mark.parametrize("finish", ["close", "raise", "cancel", "exhaust"])
def test_chunk_only_stream_unknown_and_failure_semantics(runtime, finish):
    p = provider(runtime)
    marker = RuntimeError("secret-in-provider-error")

    async def stream(self):
        yield response(123, chunk=True)
        if finish == "raise":
            raise marker
        if finish == "cancel":
            raise asyncio.CancelledError

    type(p).text_chat_stream = stream
    assert runtime.make().install()

    async def action():
        iterator = p.text_chat_stream()
        await anext(iterator)
        if finish == "close":
            await iterator.aclose()
        elif finish == "raise":
            with pytest.raises(RuntimeError) as exc:
                await anext(iterator)
            assert exc.value is marker
        elif finish == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await anext(iterator)
        else:
            with pytest.raises(StopAsyncIteration):
                await anext(iterator)

    asyncio.run(execute(runtime, action))
    assert len(runtime.calls) == 1
    assert runtime.calls[0][2] is None
    assert runtime.calls[0][3] is (finish != "exhaust")
    assert "secret-in-provider-error" not in str(runtime.errors)


def test_failed_stream_after_final_keeps_usage(runtime):
    p = provider(runtime)

    async def stream(self):
        yield response(21)
        raise RuntimeError("secret")

    type(p).text_chat_stream = stream
    assert runtime.make().install()

    async def action():
        with pytest.raises(RuntimeError):
            async for _ in p.text_chat_stream():
                pass

    asyncio.run(execute(runtime, action))
    assert runtime.calls[0][2].usage.input_other == 21
    assert runtime.calls[0][3] is True


@pytest.mark.parametrize("cancel", [False, True])
def test_nonstream_failures_and_cancellation_preserved(runtime, cancel):
    p = provider(runtime)
    marker = asyncio.CancelledError() if cancel else RuntimeError("secret")

    async def chat(self):
        raise marker

    type(p).text_chat = chat
    assert runtime.make().install()

    async def run():
        with pytest.raises(type(marker)) as exc:
            await execute(runtime, p.text_chat)
        assert exc.value is marker

    asyncio.run(run())
    assert runtime.calls[0][2] is None and runtime.calls[0][3] is True
    assert not runtime.errors


def test_real_task_cancellation_records_once(runtime):
    p = provider(runtime)
    started = asyncio.Event()

    async def chat(self):
        started.set()
        await asyncio.Event().wait()

    type(p).text_chat = chat
    assert runtime.make().install()

    async def run():
        task = asyncio.create_task(execute(runtime, p.text_chat))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(runtime.calls) == 1 and runtime.calls[0][3] is True


def test_started_at_is_request_start_not_completion(runtime, monkeypatch):
    p = provider(runtime)
    start = datetime(2026, 10, 29, 0, 59, 59, tzinfo=timezone.utc)
    clock = [start]

    class Clock:
        @staticmethod
        def now(tz):
            assert tz is timezone.utc
            return clock[0]

    async def chat(self):
        clock[0] += timedelta(minutes=2)
        return response()

    async def stream(self):
        clock[0] += timedelta(minutes=2)
        yield response()

    type(p).text_chat = chat
    type(p).text_chat_stream = stream
    monkeypatch.setattr(tracking, "datetime", Clock)
    assert runtime.make().install()

    async def run():
        await execute(runtime, p.text_chat)
        assert runtime.calls[0][4] == start
        iterator = p.text_chat_stream()  # creation is not request start
        clock[0] += timedelta(minutes=3)
        first_iteration_time = clock[0]

        async def action():
            return [part async for part in iterator]

        await execute(runtime, action)
        assert runtime.calls[-1][4] == first_iteration_time

    asyncio.run(run())


@pytest.mark.parametrize("first", [0, 1])
def test_two_trackers_coexist_and_unload_in_either_order(runtime, first):
    p = provider(runtime)
    records = [[], []]

    def callback(index):
        async def emit(*args):
            records[index].append(args)

        return emit

    trackers = [runtime.make(callback(0)), runtime.make(callback(1))]
    assert all(t.install() for t in trackers)

    async def action():
        await p.text_chat()
        return [part async for part in p.text_chat_stream()]

    asyncio.run(execute(runtime, action))
    assert [len(x) for x in records] == [2, 2]
    trackers[first].uninstall()
    asyncio.run(execute(runtime, action))
    assert len(records[first]) == 2
    assert len(records[1 - first]) == 4
    # Reload the tracker while its old wrappers may be retained inside the other.
    assert trackers[first].install()
    asyncio.run(execute(runtime, action))
    assert len(records[first]) == 4
    assert len(records[1 - first]) == 6
    for tracker in trackers:
        tracker.uninstall()
    asyncio.run(execute(runtime, action))
    assert [len(x) for x in records] == ([4, 6] if first == 0 else [6, 4])


def test_new_provider_discovered_at_next_message(runtime):
    t = runtime.make()
    assert t.install()
    p = provider(runtime)

    async def action():
        return await p.text_chat()

    asyncio.run(execute(runtime, action))
    assert len(runtime.calls) == 1 and t.status()["provider_classes"] == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_callback_failure_safe_and_model_result_unchanged(runtime, cancel):
    p = provider(runtime)

    async def failing(*args):
        if cancel:
            raise asyncio.CancelledError("secret-callback")
        raise RuntimeError("secret-callback")

    t = runtime.make(failing)
    assert t.install()
    result = asyncio.run(execute(runtime, p.text_chat))
    assert result.usage.input_other == 7
    assert t.status()["errors"]
    assert "secret-callback" not in str(t.status())


def test_incompatible_scheduler_or_provider_is_safe(runtime):
    original = runtime.cls.execute
    runtime.cls.execute = lambda *_: None
    t = runtime.make()
    assert not t.install()
    assert "TypeError" in str(t.status())
    assert not t.status()["installed"]
    runtime.cls.execute = original
    incompatible = NS(text_chat=lambda: "not async")
    runtime.providers.append(incompatible)
    assert t.install()
    assert t.status()["provider_classes"] == 0
    assert "不兼容" in str(t.status())


def test_async_provider_enumeration_is_reported_and_not_patched(runtime):
    async def providers():
        return []

    runtime.context.get_all_providers = providers
    t = runtime.make()
    assert not t.install()
    assert runtime.cls.execute is runtime.original_execute


def test_stream_snapshot_copy_failure_never_changes_chunks(runtime):
    p = provider(runtime)

    class BadUsage:
        def __deepcopy__(self, memo):
            raise RuntimeError("secret-copy")

    item = NS(usage=BadUsage(), is_chunk=False)

    async def stream(self):
        yield item

    type(p).text_chat_stream = stream
    assert runtime.make().install()

    async def action():
        assert [x async for x in p.text_chat_stream()] == [item]

    asyncio.run(execute(runtime, action))
    assert runtime.calls[0][2] is None
    assert "secret-copy" not in str(runtime.errors)


def test_cancellation_while_callback_waits_still_cancels_message(runtime):
    p = provider(runtime)
    callback_started = asyncio.Event()
    callback_closed = []

    async def callback(*args):
        callback_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            callback_closed.append(True)

    assert runtime.make(callback).install()

    async def run():
        task = asyncio.create_task(execute(runtime, p.text_chat))
        await callback_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert callback_closed == [True]

    asyncio.run(run())


@pytest.mark.parametrize("legacy_first", [False, True])
@pytest.mark.parametrize("unload_legacy_first", [False, True])
def test_actual_local_usage_stats_tracker_coexistence(
    runtime, legacy_first, unload_legacy_first
):
    # Optional source-contract check in this development workspace; the plugin
    # and all other tests remain independent of a sibling plugin installation.
    legacy_path = (
        Path(__file__).resolve().parents[2]
        / "astrbot_plugin_usage_stats"
        / "instrumentation.py"
    )
    if not legacy_path.is_file():
        pytest.skip("Optional sibling source for coexistence contract is unavailable")
    spec = importlib.util.spec_from_file_location(
        "wallet_legacy_tracker_contract", legacy_path
    )
    legacy_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = legacy_module
    spec.loader.exec_module(legacy_module)

    async def handler(event, callback):
        yield await callback(event)

    runtime.modules["astrbot.core.pipeline.process_stage.method.star_request"] = NS(
        call_handler=handler
    )
    runtime.modules["astrbot.core.star.filter.command"] = NS(
        CommandFilter=type("CommandFilter", (), {})
    )
    p = provider(runtime)
    legacy_calls = []

    async def on_command(*args):
        raise AssertionError("wallet tracker must not wrap or invoke command handlers")

    async def on_llm(*args):
        legacy_calls.append(args)

    old = legacy_module.Tracker(runtime.context, on_command, on_llm)
    new = runtime.make()
    order = [old, new] if legacy_first else [new, old]
    try:
        assert all(item.install() for item in order)

        async def action():
            await p.text_chat()
            return [part async for part in p.text_chat_stream()]

        asyncio.run(execute(runtime, action))
        assert len(runtime.calls) == len(legacy_calls) == 2
        first, second = (old, new) if unload_legacy_first else (new, old)
        first.uninstall()
        asyncio.run(execute(runtime, action))
        assert len(runtime.calls) == (4 if unload_legacy_first else 2)
        assert len(legacy_calls) == (2 if unload_legacy_first else 4)
        second.uninstall()
        before = (len(runtime.calls), len(legacy_calls))
        asyncio.run(execute(runtime, action))
        assert before == (len(runtime.calls), len(legacy_calls))
    finally:
        old.uninstall()
        new.uninstall()


def test_static_provider_methods_are_not_rebound(runtime):
    class Provider:
        @staticmethod
        async def text_chat():
            return response(17)

        @staticmethod
        async def text_chat_stream():
            yield response(17)

    p = Provider()
    runtime.providers.append(p)
    original = Provider.text_chat
    assert runtime.make().install()
    assert Provider.text_chat is original
    assert asyncio.run(execute(runtime, p.text_chat)).usage.input_other == 17
    assert not runtime.calls and runtime.errors
