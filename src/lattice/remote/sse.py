"""A minimal Server-Sent Events parser (the subset SPEC §8.9 uses).

Pure: feed it decoded lines (without their line terminators) and it returns
each event when its blank line arrives. Comment lines (``:``) are skipped,
``data`` lines join with ``\\n``, and an event without data is not dispatched,
as the SSE specification says.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: str
    id: str | None


class SSEParser:
    def __init__(self) -> None:
        self._event = ""
        self._data: list[str] = []
        self._id: str | None = None

    def feed(self, line: str) -> SSEEvent | None:
        """Consume one line; return the event it completes, if any."""
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        elif name == "id" and "\0" not in value:
            self._id = value
        return None

    def _dispatch(self) -> SSEEvent | None:
        event, data, event_id = self._event, self._data, self._id
        self._event, self._data, self._id = "", [], None
        if not data:
            return None
        return SSEEvent(event=event or "message", data="\n".join(data), id=event_id)
