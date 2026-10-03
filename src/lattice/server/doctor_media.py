"""The media pass of ``lattice server project doctor`` (SPEC §8.2, §8.12).

``lattice doctor`` reads the board's tasks and issue logs; it never looks at
the files under ``issues/media``. This pass does, in two phases so it never
holds the project's work lock for a hash:

* :func:`scan_media` runs under the lock (or the owner flock). It reads the
  snapshots and walks ``issues/media`` with ``lstat`` and a 64-byte head read:
  existence, regular file, size and type for every original and frame, files
  no snapshot lists (orphans), and the staged objects.
* :func:`finish_media` runs after the lock is released. With ``--verify-media``
  it hashes each original against the snapshot's sha256. A frame's hash is not
  recorded anywhere (frames are derived), so frames are checked for existence,
  type and size only. Before a hash mismatch or a vanished file is reported the
  snapshot is read again: an issue detached meanwhile is not corrupt.

Findings use doctor's shape (``level``, ``check``, ``message``): missing and
corrupt media are errors; orphans, stale staged objects and videos with no
frames are warnings.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from lattice.core.ids import validate_id
from lattice.core.issue_media import MAX_FRAME_BYTES, parse_frame_name, sniff_media
from lattice.storage.issue_media import frames_dir, media_path, media_root
from lattice.storage.issues import list_issue_snapshots, read_issue_snapshot

STAGE_TTL_SECONDS = 24 * 60 * 60


@dataclass
class MediaScan:
    board: Path
    findings: list[dict] = field(default_factory=list)
    checked: int = 0
    missing: int = 0
    corrupt: int = 0
    orphans: int = 0
    staged: int = 0
    #: ``(issue_id, media_id, path, sha256)`` for the hash pass.
    to_hash: list[tuple[str, str, Path, str]] = field(default_factory=list)


def _finding(level: str, check: str, message: str) -> dict:
    return {"level": level, "check": check, "message": message, "task_id": None}


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _head(path: Path) -> bytes:
    with open(path, "rb") as handle:  # noqa: PTH123 - read-only; the caller lstat-ed it regular
        return handle.read(64)


def _rel(board: Path, path: Path) -> str:
    try:
        return path.relative_to(board).as_posix()
    except ValueError:
        return str(path)


def _check_original(scan: MediaScan, issue_id: str, entry: dict) -> None:
    board = scan.board
    path = media_path(board, issue_id, entry)
    scan.checked += 1
    label = f"{issue_id} {entry.get('id')}"
    if path is None:
        scan.corrupt += 1
        scan.findings.append(
            _finding("error", "issue_media_corrupt", f"media {label} has an unusable id or type.")
        )
        return
    info = _lstat(path)
    where = _rel(board, path)
    if info is None:
        scan.missing += 1
        scan.findings.append(
            _finding("error", "issue_media_missing", f"media {label} is missing: {where}")
        )
        return
    if not stat.S_ISREG(info.st_mode):
        scan.corrupt += 1
        scan.findings.append(
            _finding(
                "error", "issue_media_corrupt", f"media {label} is not a regular file: {where}"
            )
        )
        return
    if info.st_size != entry.get("size_bytes"):
        scan.corrupt += 1
        scan.findings.append(
            _finding(
                "error",
                "issue_media_corrupt",
                f"media {label} has the wrong size: {where} is {info.st_size} bytes, "
                f"the issue records {entry.get('size_bytes')}.",
            )
        )
        return
    try:
        sniffed = sniff_media(_head(path))
    except OSError:
        sniffed = None
    if sniffed != entry.get("content_type"):
        scan.corrupt += 1
        scan.findings.append(
            _finding(
                "error",
                "issue_media_corrupt",
                f"media {label} is not {entry.get('content_type')} on disk: {where}",
            )
        )
        return
    scan.to_hash.append((issue_id, str(entry.get("id")), path, str(entry.get("sha256"))))


def _check_frames(scan: MediaScan, issue_id: str, entry: dict) -> set[str]:
    """Check a video's frame sidecar; returns the frame names that are legitimate."""
    board = scan.board
    directory = frames_dir(board, issue_id, entry)
    legit: set[str] = set()
    label = f"{issue_id} {entry.get('id')}"
    info = _lstat(directory) if directory is not None else None
    if info is None or not stat.S_ISDIR(info.st_mode):
        if entry.get("kind") == "video":
            scan.findings.append(
                _finding(
                    "warning",
                    "issue_media_frames",
                    f"video {label} has no frames sidecar; agents cannot see its content.",
                )
            )
        return legit
    assert directory is not None
    names = sorted(os.listdir(directory))
    if not names and entry.get("kind") == "video":
        scan.findings.append(
            _finding(
                "warning",
                "issue_media_frames",
                f"video {label} has an empty frames sidecar; agents cannot see its content.",
            )
        )
    for name in names:
        path = directory / name
        where = _rel(board, path)
        if parse_frame_name(name) is None:
            continue  # not a frame name: reported as an orphan by the tree walk
        scan.checked += 1
        finfo = _lstat(path)
        problem = None
        if finfo is None or not stat.S_ISREG(finfo.st_mode):
            problem = "is not a regular file"
        elif finfo.st_size > MAX_FRAME_BYTES or finfo.st_size == 0:
            problem = f"has an unusable size ({finfo.st_size} bytes)"
        else:
            try:
                if sniff_media(_head(path)) != "image/jpeg":
                    problem = "is not a JPEG"
            except OSError:
                problem = "cannot be read"
        if problem is not None:
            scan.corrupt += 1
            scan.findings.append(
                _finding("error", "issue_media_corrupt", f"frame {where} {problem}.")
            )
        legit.add(name)
    return legit


def _orphans(scan: MediaScan, expected: dict[str, dict[str, set[str]]], skip: set[str]) -> None:
    """Files under ``issues/media`` that no present snapshot entry lists.

    *expected* maps issue id -> {original file name, ``<med>.frames`` -> frame names}."""
    board = scan.board
    root = media_root(board)
    info = _lstat(root)
    if info is None:
        return
    if not stat.S_ISDIR(info.st_mode):
        scan.orphans += 1
        scan.findings.append(
            _finding("warning", "issue_media_orphan", f"{_rel(board, root)} is not a directory.")
        )
        return

    def orphan(path: Path, why: str) -> None:
        scan.orphans += 1
        scan.findings.append(
            _finding("warning", "issue_media_orphan", f"orphaned media {_rel(board, path)}: {why}")
        )

    for issue_dir in sorted(root.iterdir()):
        if issue_dir.name in skip:
            continue
        files = expected.get(issue_dir.name)
        dinfo = _lstat(issue_dir)
        if files is None or dinfo is None or not stat.S_ISDIR(dinfo.st_mode):
            orphan(issue_dir, "no present issue lists it")
            continue
        for child in sorted(issue_dir.iterdir()):
            wanted = files.get(child.name)
            cinfo = _lstat(child)
            if wanted is None:
                orphan(child, "no snapshot lists it")
            elif child.name.endswith(".frames"):
                if cinfo is None or not stat.S_ISDIR(cinfo.st_mode):
                    orphan(child, "a frames sidecar must be a directory")
                    continue
                for frame in sorted(child.iterdir()):
                    if frame.name not in wanted:
                        orphan(frame, "not a frame of this video")
            elif cinfo is None or not stat.S_ISREG(cinfo.st_mode):
                pass  # already reported as corrupt by the original's check


def _staged(scan: MediaScan, project_dir: Path, now: float) -> None:
    staging = project_dir / ".runtime" / "issue-media" / "staging"
    manifests = project_dir / ".runtime" / "issue-media" / "manifests"
    if not staging.is_dir():
        return
    referenced: set[str] = set()
    for path in manifests.glob("op_*.json"):
        try:
            for item in json.loads(path.read_text(encoding="utf-8")).get("objects", []):
                referenced.add(str(item.get("sha256")))
        except (OSError, ValueError, AttributeError):
            continue
    for path in sorted(staging.glob("*.json")):
        scan.staged += 1
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            sha = str(raw.get("sha256"))
            age = now - float(raw.get("created_at", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            scan.findings.append(
                _finding(
                    "warning", "issue_media_staged", f"staged object {path.name} is unreadable."
                )
            )
            continue
        if sha not in referenced and age >= STAGE_TTL_SECONDS:
            scan.findings.append(
                _finding(
                    "warning",
                    "issue_media_staged",
                    f"staged object {sha[:12]} is {age / 3600:.0f} h old and unreferenced "
                    "(the server removes it on its next upload or load).",
                )
            )


def scan_media(board: Path, project_dir: Path, *, now: float | None = None) -> MediaScan:
    """Phase one, under the lock: everything that needs only ``lstat`` and a head read."""
    scan = MediaScan(board=Path(board))
    unreadable: set[str] = set()
    snapshots = list_issue_snapshots(
        scan.board, on_unreadable=lambda path, _exc: unreadable.add(Path(path).stem)
    )
    expected: dict[str, dict[str, set[str]]] = {}
    for snapshot in snapshots:
        issue_id = snapshot.get("id")
        if not validate_id(issue_id, "iss"):
            continue
        files: dict[str, set[str]] = {}
        for entry in snapshot.get("media", []):
            if entry.get("removed"):
                continue
            path = media_path(scan.board, issue_id, entry)
            _check_original(scan, issue_id, entry)
            if path is not None:
                files[path.name] = set()
                frames = _check_frames(scan, issue_id, entry)
                files[f"{entry['id']}.frames"] = frames
        expected[issue_id] = files
    _orphans(scan, expected, unreadable)
    _staged(scan, Path(project_dir), time.time() if now is None else now)
    return scan


def _hash(path: Path) -> str:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("not a regular file")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _still_listed(board: Path, issue_id: str, media_id: str) -> bool:
    try:
        snapshot = read_issue_snapshot(board, issue_id)
    except Exception:  # noqa: BLE001 - an unreadable snapshot is doctor's own finding
        return False
    return any(
        entry.get("id") == media_id and not entry.get("removed")
        for entry in (snapshot or {}).get("media", [])
    )


def finish_media(scan: MediaScan, *, verify: bool) -> dict:
    """Phase two, outside the lock: the optional hash pass. Returns the report:
    ``{"findings": [...], "summary": {...}}``."""
    if verify:
        for issue_id, media_id, path, expected in scan.to_hash:
            try:
                actual = _hash(path)
            except OSError:
                if _still_listed(scan.board, issue_id, media_id):
                    scan.missing += 1
                    scan.findings.append(
                        _finding(
                            "error",
                            "issue_media_missing",
                            f"media {issue_id} {media_id} is missing: {_rel(scan.board, path)}",
                        )
                    )
                continue
            if actual != expected and _still_listed(scan.board, issue_id, media_id):
                scan.corrupt += 1
                scan.findings.append(
                    _finding(
                        "error",
                        "issue_media_corrupt",
                        f"media {issue_id} {media_id} is corrupt: {_rel(scan.board, path)} "
                        f"hashes to {actual[:12]}, the issue records {expected[:12]}.",
                    )
                )
    return {
        "findings": scan.findings,
        "summary": {
            "media_checked": scan.checked,
            "media_missing": scan.missing,
            "media_corrupt": scan.corrupt,
            "media_orphans": scan.orphans,
            "staged_objects": scan.staged,
            "media_hash_verified": verify,
        },
    }


def merge_media(data: dict, report: dict) -> dict:
    """Fold the media report into doctor's ``data`` (findings and summary)."""
    findings = [*data.get("findings", []), *report["findings"]]
    summary = dict(data.get("summary") or {})
    summary.update(report["summary"])
    summary["warnings"] = sum(1 for f in findings if f["level"] == "warning")
    summary["errors"] = sum(1 for f in findings if f["level"] == "error")
    return {**data, "findings": findings, "summary": summary}


__all__ = ["MediaScan", "finish_media", "merge_media", "scan_media"]
