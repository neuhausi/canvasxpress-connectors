"""A dependency-free simulated live feed — the demo :class:`LiveSource` for the SSE transport.

This proves the streaming path end-to-end without any upstream credential or network: each
:meth:`poll` advances a bounded random walk one step per variable and returns a
CanvasXpress-shaped tick (one new sample) ready to relay to ``pushData(tick)`` in the browser.
Seed it for a deterministic sequence (used in tests); leave the seed ``None`` for a live-looking
random walk (used by the demo endpoint). Needs only the standard library.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Optional, Sequence

from .base import Tick


class SimulatedLiveSource:
    """A bounded random-walk metric feed emitting one new sample per :meth:`poll`."""

    def __init__(
        self,
        variables: Sequence[str] = ("metric",),
        interval: float = 1.0,
        start: float = 50.0,
        step: float = 5.0,
        low: float = 0.0,
        high: float = 100.0,
        digits: int = 3,
        seed: Optional[int] = None,
        label_prefix: str = "t",
    ):
        """
        :param variables: The variable (series) names; one random walk is kept per name.
        :param interval: Seconds between ticks — the cadence the SSE transport polls at.
        :param start: Initial level every walk starts from.
        :param step: Maximum absolute change per tick (uniform in ``[-step, +step]``).
        :param low: Lower clamp for every level (the walk is reflected/held at the bound).
        :param high: Upper clamp for every level.
        :param digits: Decimal places each emitted value is rounded to.
        :param seed: Seed for a reproducible sequence; ``None`` for a fresh random walk.
        :param label_prefix: Prefix for the auto-incremented sample names (``t1``, ``t2``, ...).
        """
        if not variables:
            raise ValueError("variables must be a non-empty sequence")
        self.variables = list(variables)
        self.interval = float(interval)
        self.step = float(step)
        self.low = float(low)
        self.high = float(high)
        self.digits = int(digits)
        self.label_prefix = label_prefix
        self._rng = random.Random(seed)
        self._levels = {name: float(start) for name in self.variables}
        self._count = 0

    def poll(self) -> Optional[Tick]:
        """Advance every series one step and return a one-sample :data:`Tick`.

        :returns: A CanvasXpress-shaped increment carrying the new sample: its name, one value
            per variable (the new level), and a ``time`` annotation (UTC ISO-8601 timestamp).
        """
        self._count += 1
        sample = self.label_prefix + str(self._count)
        data = []
        for name in self.variables:
            level = self._levels[name] + self._rng.uniform(-self.step, self.step)
            level = max(self.low, min(self.high, level))
            self._levels[name] = level
            data.append([round(level, self.digits)])
        return {
            "y": {"vars": list(self.variables), "smps": [sample], "data": data},
            "x": {"time": [datetime.now(timezone.utc).isoformat()]},
        }
