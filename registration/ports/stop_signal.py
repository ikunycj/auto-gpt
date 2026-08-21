"""Framework-neutral cancellation signal for registration drivers."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol


class StopRequested(RuntimeError):
    """Raised when a registration attempt was cancelled by its owner."""


class StopSignal(Protocol):
    def is_requested(self) -> bool:
        ...


@dataclass(frozen=True, slots=True)
class CallbackStopSignal:
    callback: Callable[[], bool]

    def is_requested(self) -> bool:
        return bool(self.callback())


_CURRENT: ContextVar[StopSignal | None] = ContextVar("registration_stop_signal", default=None)


def current_stop_signal() -> StopSignal | None:
    return _CURRENT.get()


@contextmanager
def bind_stop_signal(signal: StopSignal | None) -> Iterator[Token[StopSignal | None]]:
    token = _CURRENT.set(signal)
    try:
        yield token
    finally:
        _CURRENT.reset(token)


def check_stop_requested() -> None:
    signal = current_stop_signal()
    if signal is not None and signal.is_requested():
        raise StopRequested("注册任务已被用户手动停止")

