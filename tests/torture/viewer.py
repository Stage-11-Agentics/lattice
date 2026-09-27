"""A hosted-dashboard viewer process for AC-42's ``test_with_dashboards``:
``python viewer.py SPEC``, its own process as a browser is.

``SPEC`` is ``{"url", "port", "project", "token", "ready": path, "out": path,
"stop": path, "drain"?: seconds, "panels": [...]}``. The viewer logs in with
*token* and opens a session stream. At the stream's first heartbeat it writes
``{"head_seq": S}`` to *ready*: every journal entry after ``S`` reaches it
live. From then on it queues every journal entry it receives and refetches
every panel (``/p/<project><panel>``) once per entry, with no coalescing, over
one keep-alive connection.

The rig ends the run by writing ``{"final_seq": F}`` to *stop*. The viewer then
keeps going until it has received and refetched every entry through ``F``
(within *drain* seconds), and writes one line to *out*: ``{"done": true,
"ready_seq": S, "last_seq": ..., "received": n, "refetched": m}``, or the same
with ``"crashed": "..."`` in place of ``done``.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the repo root, for ``tests``

from lattice.server.testing import open_stream  # noqa: E402
from tests.test_server.web_client import WebClient  # noqa: E402


class _Server:
    """What ``WebClient`` needs of a ``ServerHandle``."""

    def __init__(self, url: str, port: int) -> None:
        self.url, self.port = url, port


def _seq(event_id: str | None) -> int:
    """The ``seq`` of a journal entry's SSE id (``<epoch>:<seq>:<hash>``)."""
    assert event_id is not None, "a journal entry without an id"
    return int(event_id.split(":")[1])


def _final_seq(stop: Path) -> int | None:
    try:
        return int(json.loads(stop.read_text())["final_seq"])
    except (OSError, ValueError, KeyError):
        return None  # absent, or not fully written yet


def run(spec: dict) -> dict:
    state = {"ready_seq": None, "last_seq": None, "received": 0, "refetched": 0}
    errors: list[str] = []
    stop = Path(spec["stop"])
    drain = float(spec.get("drain", 120.0))
    web = WebClient(_Server(spec["url"], spec["port"]), keep_alive=True)  # type: ignore[arg-type]
    stream = None
    listener: threading.Thread | None = None
    finished = threading.Event()
    try:
        if web.login(spec["token"]).status != 303:
            raise AssertionError("viewer login failed")
        stream = open_stream(
            spec["url"],
            spec["project"],
            None,
            headers={"Cookie": f"lattice_session={web.session}", "Origin": spec["url"]},
        )
        if stream.status != 200:
            raise AssertionError(f"viewer stream refused: {stream.status}")
        entries: queue.Queue[int] = queue.Queue()

        def listen() -> None:
            # The page's EventSource: every journal entry after the ready head is
            # queued, none dropped.
            try:
                while not finished.is_set():
                    try:
                        message = stream.next(timeout=10)
                    except TimeoutError:
                        continue
                    if message is None:
                        if not finished.is_set():
                            errors.append("viewer stream closed")
                        return
                    if message.event == "heartbeat" and state["ready_seq"] is None:
                        state["ready_seq"] = int(message.data["head_seq"])
                        Path(spec["ready"]).write_text(
                            json.dumps({"head_seq": state["ready_seq"]}) + "\n"
                        )
                    elif message.event == "journal" and state["ready_seq"] is not None:
                        seq = _seq(message.id)
                        if seq > state["ready_seq"]:
                            state["received"] += 1
                            state["last_seq"] = seq
                            entries.put(seq)
                    elif message.event == "reset":
                        errors.append("the journal was reset during the run")
            except BaseException as exc:  # noqa: BLE001 - reported
                if not finished.is_set():
                    errors.append(f"listener: {exc!r}")

        listener = threading.Thread(target=listen, daemon=True)
        listener.start()
        final: int | None = None
        deadline = float("inf")
        refetched_through = None
        while not errors:
            if final is None and (final := _final_seq(stop)) is not None:
                deadline = time.monotonic() + drain
            if final is not None and refetched_through is not None and refetched_through >= final:
                break  # every entry through the final one is refetched
            if (
                final is not None
                and state["ready_seq"] is not None
                and final <= state["ready_seq"]
            ):
                break  # nothing was written after this viewer became ready
            if time.monotonic() > deadline:
                errors.append(
                    f"viewer drained only through {refetched_through} of {final} in {drain:.0f}s"
                )
                break
            try:
                seq = entries.get(timeout=0.5)
            except queue.Empty:
                continue
            for panel in spec["panels"]:  # one refetch per entry, as the page does
                response = web.get(f"/p/{spec['project']}{panel}")
                if response.status != 200:
                    errors.append(f"viewer {panel}: {response.status}")
            state["refetched"] += 1
            refetched_through = seq
    except BaseException as exc:  # noqa: BLE001 - every viewer reports, whatever happens
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        finished.set()
        if stream is not None:
            stream.close()
        web.close()
        if listener is not None:
            listener.join(timeout=15)
    report: dict = dict(state)
    if errors:
        report["crashed"] = "; ".join(errors[:5])
    else:
        report["done"] = True
    return report


def main() -> None:
    spec = json.loads(Path(sys.argv[1]).read_text())
    report = run(spec)
    Path(spec["out"]).write_text(json.dumps(report) + "\n")
    sys.exit(0 if report.get("done") else 1)


if __name__ == "__main__":
    main()
