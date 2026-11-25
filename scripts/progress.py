"""
Лёгкий индикатор прогресса для консоли.
Без внешних зависимостей.
"""
import sys
import time


class Progress:
    def __init__(self, label: str, unit: str = "строк", interval: float = 0.2):
        self.label = label
        self.unit = unit
        self.interval = interval
        self.count = 0
        self.t0 = time.perf_counter()
        self._last = 0.0
        self._render(force=True)

    def _line(self) -> str:
        el = time.perf_counter() - self.t0
        sp = self.count / el if el > 0 else 0
        return f"  {self.label}: {self.count:,} {self.unit}  ({sp:,.0f}/сек)  {el:,.0f}с"

    def _render(self, force: bool = False):
        now = time.perf_counter()
        if not force and now - self._last < self.interval:
            return
        self._last = now
        sys.stdout.write("\r" + self._line().ljust(78))
        sys.stdout.flush()

    def update(self, n: int = 1):
        self.count += n
        self._render()

    def finish(self):
        sys.stdout.write("\r" + self._line().ljust(78) + "\n")
        sys.stdout.flush()


class PercentProgress:
    def __init__(self, label: str, total: float, unit: str = "MB", interval: float = 0.2):
        self.label = label
        self.total = total if total > 0 else 1
        self.unit = unit
        self.interval = interval
        self.done = 0.0
        self.t0 = time.perf_counter()
        self._last = 0.0
        self._render(force=True)

    def _line(self) -> str:
        el = time.perf_counter() - self.t0
        pct = self.done / self.total * 100
        sp = self.done / el if el > 0 else 0
        filled = int(pct / 100 * 30)
        bar = "█" * filled + "░" * (30 - filled)
        return (f"  {self.label}: [{bar}] {pct:5.1f}%  "
                f"{self.done:,.0f}/{self.total:,.0f} {self.unit}  {sp:,.1f}/с")

    def _render(self, force: bool = False):
        now = time.perf_counter()
        if not force and now - self._last < self.interval:
            return
        self._last = now
        sys.stdout.write("\r" + self._line().ljust(90))
        sys.stdout.flush()

    def update(self, n: float = 1):
        self.done += n
        self._render()

    def finish(self):
        sys.stdout.write("\r" + self._line().ljust(90) + "\n")
        sys.stdout.flush()