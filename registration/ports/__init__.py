"""Ports used by registration application and adapters."""

from .stop_signal import (
    CallbackStopSignal,
    StopRequested,
    bind_stop_signal,
    check_stop_requested,
    current_stop_signal,
)

__all__ = [
    "CallbackStopSignal",
    "StopRequested",
    "bind_stop_signal",
    "check_stop_requested",
    "current_stop_signal",
]

