"""Observe message-bound Provider calls without changing AstrBot responses.

Only PipelineScheduler.execute and Provider chat methods are wrapped. This module
has no dependency on another plugin and never reads keys or retains call logs.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import inspect
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from types import SimpleNamespace
from typing import Any, Callable


@dataclass
class _Installation:
    active: bool = True


@dataclass
class _Scope:
    event: Any
    active: bool = True


@dataclass
class _Invocation:
    provider_id: int
    task: Any
    active: bool = True


@dataclass
class _Patch:
    target: Any
    name: str
    original: Any
    wrapper: Any
    owned: bool


def _field(value: Any, name: str, default: Any = None) -> Any:
    return (
        value.get(name, default)
        if isinstance(value, Mapping)
        else getattr(value, name, default)
    )


class ProviderUsageTracker:
    """Callbacks: async on_llm(event, provider, response, failed, started_at).

    started_at is an aware UTC datetime captured before the Provider call starts.
    on_error(message) is an optional synchronous, safe-summary notification.
    install() requires the asynchronous PipelineScheduler interface and a
    synchronous Context.get_all_providers(). Unsupported individual methods are
    left untouched and reported in status()["errors"]. Newly loaded providers are
    discovered at each message boundary. Calls after that boundary closes cannot
    be attributed, even in tasks inheriting the original ContextVar.
    """

    def __init__(
        self, context: Any, on_llm: Callable, on_error: Callable | None = None
    ):
        self.context = context
        self.on_llm = on_llm
        self.on_error = on_error
        self.active = False
        self._installation: _Installation | None = None
        self._scope: ContextVar[_Scope | None] = ContextVar(
            f"wallet_usage_scope_{id(self)}", default=None
        )
        self._calls: ContextVar[tuple[_Invocation, ...]] = ContextVar(
            f"wallet_usage_calls_{id(self)}", default=()
        )
        self._patches: list[_Patch] = []
        self._provider_classes: set[type] = set()
        self._errors: list[str] = []

    def _warn(self, message: str) -> None:
        # Only fixed descriptions and exception *types* reach this bounded list.
        if message in self._errors or len(self._errors) >= 20:
            return
        self._errors.append(message)
        if self.on_error:
            try:
                result = self.on_error(message)
                if inspect.iscoroutine(result):
                    result.close()
            except (Exception, asyncio.CancelledError):
                pass

    def _patch(self, target: Any, name: str, wrapper: Any) -> None:
        original = getattr(target, name)
        owned = name in vars(target)
        setattr(target, name, wrapper)
        self._patches.append(_Patch(target, name, original, wrapper, owned))

    def _enabled(self, installation: _Installation) -> bool:
        return (
            self.active and installation.active and self._installation is installation
        )

    def install(self) -> bool:
        if self.active:
            return True
        try:
            scheduler_cls = importlib.import_module(
                "astrbot.core.pipeline.scheduler"
            ).PipelineScheduler
            if not inspect.iscoroutinefunction(scheduler_cls.execute):
                raise TypeError
            if not callable(getattr(self.context, "get_all_providers", None)):
                raise TypeError
            providers = self._providers()
            self._installation = installation = _Installation()
            self.active = True
            self._patch(
                scheduler_cls,
                "execute",
                self._wrap_execute(scheduler_cls.execute, installation),
            )
            self._refresh(providers, installation)
            return True
        except (Exception, asyncio.CancelledError) as exc:
            self.uninstall()
            self._warn(f"Provider 用量接入失败，未启用统计（{type(exc).__name__}）。")
            return False

    def _providers(self) -> list[Any]:
        providers = self.context.get_all_providers()
        if inspect.isawaitable(providers):
            if inspect.iscoroutine(providers):
                providers.close()
            raise TypeError
        return list(providers)

    def _refresh(self, providers: list[Any], installation: _Installation) -> None:
        for provider in providers:
            cls = type(provider)
            # Instance overrides bypass a class wrapper, including new instances
            # of a class seen earlier. Do not rewrite the provider's own state.
            for name in ("text_chat", "text_chat_stream"):
                if name in getattr(provider, "__dict__", {}):
                    self._warn(f"Provider.{name} 被实例覆盖，该实例路径无法统计。")
            if cls in self._provider_classes:
                continue
            patched = False
            for name, stream in (("text_chat", False), ("text_chat_stream", True)):
                original = getattr(cls, name, None)
                descriptor = inspect.getattr_static(cls, name, None)
                compatible = inspect.isfunction(descriptor) and (
                    inspect.isasyncgenfunction(original)
                    if stream
                    else inspect.iscoroutinefunction(original)
                )
                if not compatible:
                    self._warn(f"Provider.{name} 接口不兼容，该路径无法统计。")
                    continue
                wrapper = (
                    self._wrap_stream(original, installation)
                    if stream
                    else self._wrap_chat(original, installation)
                )
                self._patch(cls, name, wrapper)
                patched = True
            if patched:
                self._provider_classes.add(cls)

    def refresh(self) -> int:
        if not self.active or self._installation is None:
            return 0
        try:
            self._refresh(self._providers(), self._installation)
        except (Exception, asyncio.CancelledError) as exc:
            self._warn(f"Provider 用量接入不完整（{type(exc).__name__}）。")
        return len(self._provider_classes)

    def uninstall(self) -> None:
        self.active = False
        if self._installation is not None:
            self._installation.active = False
        for patch in reversed(self._patches):
            try:
                # Another plugin may own the outer wrapper. Leave its chain
                # intact; this installation is permanently inert after unloading.
                if getattr(patch.target, patch.name, None) is not patch.wrapper:
                    continue
                if patch.owned:
                    setattr(patch.target, patch.name, patch.original)
                else:
                    delattr(patch.target, patch.name)
            except (Exception, asyncio.CancelledError) as exc:
                self._warn(f"Provider 用量包装恢复失败（{type(exc).__name__}）。")
        self._patches.clear()
        self._provider_classes.clear()
        self._installation = None

    def status(self) -> dict[str, Any]:
        return {
            "installed": self.active,
            "provider_classes": len(self._provider_classes),
            "errors": list(self._errors),
        }

    def _live_scope(self, installation: _Installation) -> _Scope | None:
        scope = self._scope.get()
        return scope if self._enabled(installation) and scope and scope.active else None

    def _nested(self, provider: Any) -> bool:
        task = asyncio.current_task()
        return any(
            call.active and call.provider_id == id(provider) and call.task is task
            for call in self._calls.get()
        )

    async def _emit(
        self,
        installation: _Installation,
        scope: _Scope,
        provider: Any,
        response: Any,
        failed: bool,
        started_at: datetime,
    ) -> None:
        if not self._enabled(installation) or not scope.active:
            return
        pending = None
        try:
            # A separate, shielded callback distinguishes cancellation raised by
            # the observer from cancellation of the message task on Python 3.10.
            # The callback is always awaited, or cancelled and drained below.
            pending = asyncio.create_task(
                self.on_llm(scope.event, provider, response, failed, started_at)
            )
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            if pending is not None and pending.done() and pending.cancelled():
                self._warn("Provider 用量写入回调取消，记录可能不完整。")
                return
            if pending is not None:
                pending.cancel()
                try:
                    await pending
                except (Exception, asyncio.CancelledError):
                    pass
            raise
        except Exception as exc:
            self._warn(f"Provider 用量写入失败（{type(exc).__name__}）。")

    def _wrap_execute(
        self, original: Callable, installation: _Installation
    ) -> Callable:
        @wraps(original)
        async def execute(scheduler: Any, event: Any, *args: Any, **kwargs: Any):
            if not self._enabled(installation):
                return await original(scheduler, event, *args, **kwargs)
            self.refresh()
            scope = _Scope(event)
            token = self._scope.set(scope)
            try:
                return await original(scheduler, event, *args, **kwargs)
            finally:
                scope.active = False
                self._scope.reset(token)

        return execute

    def _wrap_chat(self, original: Callable, installation: _Installation) -> Callable:
        @wraps(original)
        async def text_chat(provider: Any, *args: Any, **kwargs: Any):
            scope = self._live_scope(installation)
            if scope is None or self._nested(provider):
                return await original(provider, *args, **kwargs)
            call = _Invocation(id(provider), asyncio.current_task())
            token = self._calls.set(self._calls.get() + (call,))
            response, failed = None, True
            started_at = datetime.now(timezone.utc)
            try:
                response = await original(provider, *args, **kwargs)
                failed = False
                return response
            finally:
                call.active = False
                self._calls.reset(token)
                await self._emit(
                    installation, scope, provider, response, failed, started_at
                )

        return text_chat

    def _snapshot(self, response: Any) -> Any:
        """Freeze usage and model metadata only, never prompts or response bodies."""
        try:
            raw = _field(response, "raw_completion")
            raw_snapshot = None
            if raw is not None:
                missing = object()
                fields = {}
                for name in ("usage", "usage_metadata", "usageMetadata", "model"):
                    value = _field(raw, name, missing)
                    if value is not missing:
                        fields[name] = copy.deepcopy(value)
                raw_snapshot = SimpleNamespace(**fields)
            return SimpleNamespace(
                usage=copy.deepcopy(_field(response, "usage")),
                raw_completion=raw_snapshot,
                role=_field(response, "role"),
                model=_field(response, "model"),
                is_chunk=False,
            )
        except Exception as exc:
            self._warn(
                f"Provider 流式用量无法读取，该次记为未知（{type(exc).__name__}）。"
            )
            return None

    def _wrap_stream(self, original: Callable, installation: _Installation) -> Callable:
        @wraps(original)
        async def text_chat_stream(provider: Any, *args: Any, **kwargs: Any):
            scope = self._live_scope(installation)
            observe = scope is not None and not self._nested(provider)
            call = _Invocation(id(provider), asyncio.current_task())
            calls = self._calls.get() + (call,)
            final_response, failed = None, True
            iterator = original(provider, *args, **kwargs)
            # Async-generator construction does not perform the request. Capture
            # its start immediately before the first actual iterator advancement.
            started_at = datetime.now(timezone.utc)
            try:
                while True:
                    token = self._calls.set(calls)
                    try:
                        response = await iterator.__anext__()
                    except StopAsyncIteration:
                        failed = False
                        break
                    finally:
                        self._calls.reset(token)
                    if observe:
                        try:
                            if not _field(response, "is_chunk", False):
                                final_response = self._snapshot(response)
                        except Exception as exc:
                            final_response = None
                            self._warn(
                                f"Provider 流式响应无法读取，该次记为未知（{type(exc).__name__}）。"
                            )
                    # Never leave ContextVar tokens installed across a yield.
                    yield response
            finally:
                token = self._calls.set(calls)
                try:
                    await iterator.aclose()
                finally:
                    call.active = False
                    self._calls.reset(token)
                    if observe:
                        await self._emit(
                            installation,
                            scope,
                            provider,
                            final_response,
                            failed,
                            started_at,
                        )

        return text_chat_stream
