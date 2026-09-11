"""Transport-agnostic DTMF keypad number collector for Kavya calls.

Dialog sends clean ``dtmf`` events, so keypad entry bypasses STT entirely and is
100% accurate for phone/reference numbers. The collector buffers the digits and
resolves an :class:`asyncio.Future` when the entry terminates (``#``, an
inter-digit pause, an overall timeout, the max-length guard, or teardown). It
accepts keys before :meth:`start`, because guests key in over the prompt that
asks them to; the timeouts only begin at :meth:`start`. It holds no transport or
session state, so the same core can drive any provider.
"""

from __future__ import annotations

import asyncio
from typing import Any


_DIGITS = frozenset("0123456789")


class DtmfCollector:
    """Collect one keypad number entry and resolve a future with the result."""

    def __init__(
        self,
        *,
        loop: Any,
        interdigit_timeout: float = 6.0,
        overall_timeout: float = 30.0,
        max_digits: int = 15,
    ) -> None:
        self._loop = loop
        self._interdigit_timeout = interdigit_timeout
        self._overall_timeout = overall_timeout
        self._max_digits = max_digits
        self._buffer: list[str] = []
        self._done = False
        self._started = False
        self._future: asyncio.Future = loop.create_future()
        self._interdigit_handle: Any = None
        self._overall_handle: Any = None

    @property
    def future(self) -> asyncio.Future:
        return self._future

    def start(self) -> None:
        """Open the entry window: arm the timeouts that measure the guest's typing.

        Called after the keypad instruction has been spoken, so the guest gets
        the whole window to key in. Digits pressed over the prompt are buffered
        before this, but they are not timed until here — an entry begun early
        must not expire against speech the guest is still listening to. An entry
        the guest already ended with '#' over the prompt is left alone: arming a
        timer for a finished collection would leave a handle nothing cancels.
        """
        if self._done or self._started:
            return
        self._started = True
        if self._buffer:
            # Digits tapped over the instruction: their pause window opens now,
            # so listening to the rest of the prompt cannot cut the entry short.
            self._arm_interdigit()
        self._overall_handle = self._loop.call_later(
            self._overall_timeout, self._on_overall_timeout
        )

    def feed(self, key: str) -> None:
        """Consume one DTMF key. No-op once the entry has terminated."""
        if self._done:
            return
        if key in _DIGITS:
            self._buffer.append(key)
            if self._started:
                self._arm_interdigit()
            if len(self._buffer) >= self._max_digits:
                self._finalize_collected()
        elif key == "#":
            # A lone '#' before any digit is a mis-press; keep waiting.
            if self._buffer:
                self._finalize_collected()
        elif key == "*":
            # Clear the current entry and let the guest start over.
            self._buffer.clear()
            self._cancel_interdigit()
        # Any other key (A-D, letters) is ignored.

    def cancel(self, reason: str = "cancelled") -> None:
        """Teardown (hangup): resolve the future so the awaiter unblocks cleanly."""
        self._resolve({"status": "cancelled", "reason": reason})

    # -- internals ----------------------------------------------------------

    def _arm_interdigit(self) -> None:
        self._cancel_interdigit()
        self._interdigit_handle = self._loop.call_later(
            self._interdigit_timeout, self._on_interdigit_timeout
        )

    def _cancel_interdigit(self) -> None:
        if self._interdigit_handle is not None:
            self._interdigit_handle.cancel()
            self._interdigit_handle = None

    def _on_interdigit_timeout(self) -> None:
        self._interdigit_handle = None
        if self._buffer:
            self._finalize_collected()

    def _on_overall_timeout(self) -> None:
        self._overall_handle = None
        # A runaway guard: an ordinary entry finalizes on '#' or the inter-digit
        # pause long before this. Reaching it means no usable input arrived.
        self._resolve({"status": "no_input", "reason": "overall_timeout"})

    def _finalize_collected(self) -> None:
        digits = "".join(self._buffer)
        self._resolve({
            "status": "collected",
            "digits": digits,
            "readback": " ".join(digits),
            "length": len(digits),
        })

    def _resolve(self, result: dict[str, Any]) -> None:
        if self._done:
            return
        self._done = True
        self._cancel_interdigit()
        if self._overall_handle is not None:
            self._overall_handle.cancel()
            self._overall_handle = None
        if not self._future.done():
            self._future.set_result(result)
