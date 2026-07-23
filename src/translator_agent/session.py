from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any


class SessionState:
    def __init__(self, session_id: str, max_entries: int = 200) -> None:
        self.session_id = session_id
        self.max_entries = max_entries
        self.request_sequence = 0
        self.last_activity = time.monotonic()
        self._memory: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def begin_request(self) -> int:
        with self._lock:
            self.request_sequence += 1
            self.last_activity = time.monotonic()
            return self.request_sequence

    def touch(self) -> None:
        with self._lock:
            self.last_activity = time.monotonic()

    def relevant_memory(
        self, units: list[dict[str, Any]], targets: list[str], continue_context: bool
    ) -> list[dict[str, Any]]:
        if not continue_context:
            return []
        with self._lock:
            results = []
            for unit in units:
                cached = self._memory.get(str(unit.get("text", "")))
                if cached:
                    selected = {
                        locale: text
                        for locale, text in cached.get("targets", {}).items()
                        if locale in targets
                    }
                    if selected:
                        results.append({"source": unit.get("text", ""), "targets": selected})
            return results[:20]

    def remember(self, source: str, targets: dict[str, str]) -> None:
        with self._lock:
            self._memory[source] = {"targets": dict(targets)}
            self._memory.move_to_end(source)
            while len(self._memory) > self.max_entries:
                self._memory.popitem(last=False)
            self.last_activity = time.monotonic()
