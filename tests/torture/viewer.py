"""A hosted-dashboard viewer process for AC-42's ``test_with_dashboards``:
``python viewer.py SPEC``, its own process as a browser is.

``SPEC`` is ``{"url", "port", "project", "token", "out", "stop": path,
"drain"?: seconds, "panels": [...]}``. The viewer logs in with *token*, holds a
session stream, queues every journal entry it receives, and refetches every
panel (``/p/<project><panel>``) once per entry with no coalescing, over one
keep-alive connection. When *stop* exists it drains the entries it already
received (within *drain* seconds) and writes one line to *out*:
``{"done": true, "received": n, "refetched": m}``, or
``{"crashed": "...", "received": n, "refetched": m}`` with every error.
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


def run(spec: dict) -> dict:
    received = refetched = 0
    errors: list[str] = []
    stop = Path(spec["stop"])
    drain = float(spec.get("drain", 120.0))
    web = WebClient(_Server(spec["url"], spec["port"]), keep_alive=True)  # type: ignore[arg-type]
    stream = None
    listener: threading.Thread | None = None
    try:
        if web.login(spec["token"]).status != 303:
            raise AssertionError("viewer login failed")
        stream = open_stream(
            spec["url"],
            spec["project"],
            None,
            headers={"Cookie": f"lattice_session={web.session}", "Origin": spec["url"]},
        )
        entries: queue.Queue[int] = queue.Queue()
        stopped = threading.Event()

        def listen() -> None:
            # The page's EventSource: every journal entry is queued, none dropped.
            nonlocal received
            try:
                while not stopped.is_set():
                    try:
                        message = stream.next(timeout=10)
                    except TimeoutError:
                        continue
                    if message is None:
                        if not stopped.is_set():
                            errors.append("viewer stream closed")
                        return
                    if message.event == "journal":
                        received += 1
                        entries.put(1)
            except BaseException as exc:  # noqa: BLE001 - reported
                if not stopped.is_set():
                    errors.append(f"listener: {exc!r}")

        listener = threading.Thread(target=listen, daemon=True)
        listener.start()
        deadline: float | None = None
        while not errors:
            if deadline is None and stop.exists():
                stopped.set()
                deadline = time.monotonic() + drain
            try:
                entries.get(timeout=0.5)
            except queue.Empty:
                if deadline is not None and not listener.is_alive():
                    break  # stopped, and every entry received is refetched
                continue
            if deadline is not None and time.monotonic() > deadline:
                errors.append("viewer could not refetch every entry before the drain deadline")
                break
            for panel in spec["panels"]:  # one refetch per entry, as the page does
                response = web.get(f"/p/{spec['project']}{panel}")
                if response.status != 200:
                    errors.append(f"viewer {panel}: {response.status}")
            refetched += 1
    except BaseException as exc:  # noqa: BLE001 - every viewer reports, whatever happens
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        if stream is not None:
            stream.close()
        web.close()
        if listener is not None:
            listener.join(timeout=15)
    if errors:
        return {"crashed": "; ".join(errors[:5]), "received": received, "refetched": refetched}
    return {"done": True, "received": received, "refetched": refetched}


def main() -> None:
    spec = json.loads(Path(sys.argv[1]).read_text())
    report = run(spec)
    Path(spec["out"]).write_text(json.dumps(report) + "\n")
    sys.exit(0 if report.get("done") else 1)


if __name__ == "__main__":
    main()
