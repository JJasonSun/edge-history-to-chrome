#!/usr/bin/env python3
"""Prepare a temporary Firefox history profile for Chrome's importer."""

from __future__ import annotations

import argparse
import calendar
import csv
import fcntl
import functools
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlsplit


VERSION = "0.1.0"
PROFILE_PREFIX = "edge-history-to-chrome-"
STATE_FILE = ".edge-history-to-chrome.json"
REQUIRED_COLUMNS = {"DateTime", "NavigatedToUrl", "PageTitle"}
HISTORY_WINDOW_DAYS = 90
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class UserError(Exception):
    """An error that can be fixed by the user."""


@contextmanager
def _operation_lock():
    lock_path = Path(tempfile.gettempdir()) / f"edge-history-to-chrome-{os.getuid()}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise UserError("Could not open the operation lock") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UserError("Another edge-history-to-chrome operation is running") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _locked(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with _operation_lock():
            return function(*args, **kwargs)

    return wrapper


@dataclass(frozen=True)
class Visit:
    url: str
    title: str
    visit_usec: int


@dataclass
class ParsedExport:
    visits: List[Visit]
    titles: Dict[str, str]
    counts: Counter
    skipped_non_web: int
    older_than_window: int

    @property
    def oldest_usec(self) -> int:
        return min(visit.visit_usec for visit in self.visits)

    @property
    def newest_usec(self) -> int:
        return max(visit.visit_usec for visit in self.visits)


def default_firefox_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "Firefox"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _timestamp_to_usec(raw: str, row_number: int) -> int:
    value = raw.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UserError(f"CSV row {row_number} has an invalid DateTime value") from exc
    if parsed.tzinfo is None:
        raise UserError(f"CSV row {row_number} has a DateTime without a timezone")
    utc = parsed.astimezone(timezone.utc)
    return calendar.timegm(utc.utctimetuple()) * 1_000_000 + utc.microsecond


def parse_export(path: Path, now: Optional[datetime] = None) -> ParsedExport:
    if not path.is_file():
        raise UserError(f"CSV file not found: {path}")

    visits: List[Visit] = []
    titles: Dict[str, str] = {}
    counts: Counter = Counter()
    title_times: Dict[str, int] = {}
    skipped_non_web = 0

    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise UserError(f"Could not open CSV: {exc}") from exc

    with handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        missing = sorted(REQUIRED_COLUMNS - fields)
        if missing:
            raise UserError("CSV is missing required columns: " + ", ".join(missing))

        for row_number, row in enumerate(reader, start=2):
            url = (row.get("NavigatedToUrl") or "").strip()
            try:
                parsed_url = urlsplit(url)
            except ValueError:
                skipped_non_web += 1
                continue
            if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.netloc:
                skipped_non_web += 1
                continue

            visit_usec = _timestamp_to_usec(row.get("DateTime") or "", row_number)
            title = (row.get("PageTitle") or "").strip()
            visits.append(Visit(url=url, title=title, visit_usec=visit_usec))
            counts[url] += 1
            if title and (url not in title_times or visit_usec > title_times[url]):
                titles[url] = title
                title_times[url] = visit_usec
            elif url not in titles:
                titles[url] = ""

    if not visits:
        raise UserError("CSV contains no valid HTTP or HTTPS visits")

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current.astimezone(timezone.utc) - timedelta(days=HISTORY_WINDOW_DAYS)
    cutoff_usec = int(cutoff.timestamp() * 1_000_000)
    older_than_window = sum(visit.visit_usec < cutoff_usec for visit in visits)

    return ParsedExport(
        visits=visits,
        titles=titles,
        counts=counts,
        skipped_non_web=skipped_non_web,
        older_than_window=older_than_window,
    )


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.edge-history-to-chrome.", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _write_exclusive(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _validate_root(root: Path) -> Path:
    expanded = root.expanduser()
    if expanded.exists() and expanded.is_symlink():
        raise UserError(f"Firefox data directory must not be a symlink: {expanded}")
    absolute = Path(os.path.abspath(str(expanded)))
    if absolute.exists() and not absolute.is_dir():
        raise UserError(f"Firefox data path is not a directory: {absolute}")
    return absolute


def _find_managed_profiles(root: Path) -> List[Path]:
    profiles_dir = root / "Profiles"
    if not profiles_dir.exists():
        return []
    if profiles_dir.is_symlink() or not profiles_dir.is_dir():
        raise UserError(f"Unsafe Firefox Profiles directory: {profiles_dir}")
    managed = []
    for child in profiles_dir.iterdir():
        if not child.name.startswith(PROFILE_PREFIX):
            continue
        run_id = child.name[len(PROFILE_PREFIX) :]
        if not RUN_ID_RE.fullmatch(run_id):
            continue
        state_path = child / STATE_FILE
        if state_path.is_symlink():
            raise UserError(f"Managed profile state must not be a symlink: {state_path}")
        if child.is_dir() and not child.is_symlink() and state_path.is_file():
            managed.append(child)
    return sorted(managed)


def _next_profile_number(content: str) -> int:
    numbers = [int(value) for value in re.findall(r"(?mi)^\[Profile(\d+)\][ \t]*\r?$", content)]
    if len(numbers) != len(set(numbers)):
        raise UserError("profiles.ini contains duplicate Profile sections")
    if not numbers:
        return 0
    ordered = sorted(numbers)
    expected = list(range(ordered[-1] + 1))
    if ordered != expected:
        raise UserError("profiles.ini contains non-contiguous Profile sections")
    return ordered[-1] + 1


def _build_places_database(path: Path, parsed: ParsedExport) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE moz_places (
              id INTEGER PRIMARY KEY,
              url TEXT NOT NULL,
              title TEXT,
              visit_count INTEGER NOT NULL DEFAULT 0,
              hidden INTEGER NOT NULL DEFAULT 0,
              typed INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX moz_places_url_uniqueindex ON moz_places(url);
            CREATE TABLE moz_historyvisits (
              id INTEGER PRIMARY KEY,
              from_visit INTEGER NOT NULL DEFAULT 0,
              place_id INTEGER NOT NULL,
              visit_date INTEGER NOT NULL,
              visit_type INTEGER NOT NULL DEFAULT 1,
              session INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX moz_historyvisits_placedateindex
              ON moz_historyvisits(place_id, visit_date);
            """
        )

        place_ids: Dict[str, int] = {}
        for place_id, url in enumerate(parsed.counts, start=1):
            place_ids[url] = place_id
            connection.execute(
                "INSERT INTO moz_places(id, url, title, visit_count, hidden, typed) "
                "VALUES (?, ?, ?, ?, 0, 0)",
                (place_id, url, parsed.titles.get(url, ""), parsed.counts[url]),
            )

        connection.executemany(
            "INSERT INTO moz_historyvisits"
            "(id, from_visit, place_id, visit_date, visit_type, session) "
            "VALUES (?, 0, ?, ?, 1, 0)",
            (
                (visit_id, place_ids[visit.url], visit.visit_usec)
                for visit_id, visit in enumerate(parsed.visits, start=1)
            ),
        )
        connection.commit()
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise UserError("Generated places.sqlite failed its integrity check")
    finally:
        connection.close()
    os.chmod(path, 0o600)


def _format_usec(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc).isoformat()


@_locked
def prepare(
    source: Path,
    firefox_root: Path,
    *,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> dict:
    source = source.expanduser()
    parsed = parse_export(source, now=now)
    root = _validate_root(firefox_root)

    summary = {
        "visits": len(parsed.visits),
        "unique_urls": len(parsed.counts),
        "skipped_non_web": parsed.skipped_non_web,
        "older_than_window": parsed.older_than_window,
        "oldest": _format_usec(parsed.oldest_usec),
        "newest": _format_usec(parsed.newest_usec),
    }
    if dry_run:
        return summary

    if _find_managed_profiles(root):
        raise UserError("A managed import profile already exists; run cleanup first")

    root_existed = root.exists()
    profiles_ini = root / "profiles.ini"
    profiles_ini_existed = profiles_ini.is_file()
    if profiles_ini.exists() and not profiles_ini.is_file():
        raise UserError(f"profiles.ini is not a regular file: {profiles_ini}")
    if profiles_ini.is_symlink():
        raise UserError(f"profiles.ini must not be a symlink: {profiles_ini}")

    original_bytes = profiles_ini.read_bytes() if profiles_ini_existed else b""
    try:
        original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UserError("profiles.ini is not valid UTF-8") from exc
    original_mode = stat.S_IMODE(profiles_ini.stat().st_mode) if profiles_ini_existed else None

    base_bytes = original_bytes
    if not profiles_ini_existed:
        base_bytes = b"[General]\nStartWithLastProfile=1\nVersion=2\n"
    profile_number = _next_profile_number(base_bytes.decode("utf-8"))

    created = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{created}-{secrets.token_hex(3)}"
    profile_name = PROFILE_PREFIX + run_id
    relative_profile = f"Profiles/{profile_name}"
    profile_dir = root / "Profiles" / profile_name

    separator = b"\n" if base_bytes.endswith((b"\n", b"\r")) else b"\n\n"
    section = (
        f"[Profile{profile_number}]\n"
        f"Name=Edge-History-To-Chrome-{run_id}\n"
        "IsRelative=1\n"
        f"Path={relative_profile}\n"
    ).encode("utf-8")
    appended_block = separator + section
    installed_bytes = base_bytes + appended_block

    backup_path: Optional[Path] = None
    profiles_dir_created = False
    profile_created = False
    backup_created = False
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root_existed:
        os.chmod(root, 0o700)
    profiles_dir = root / "Profiles"
    if profiles_dir.exists() and (profiles_dir.is_symlink() or not profiles_dir.is_dir()):
        raise UserError(f"Unsafe Firefox Profiles directory: {profiles_dir}")
    if not profiles_dir.exists():
        profiles_dir.mkdir(mode=0o700)
        profiles_dir_created = True

    try:
        if profiles_ini_existed:
            backup_path = root / f"profiles.ini.edge-history-to-chrome.{run_id}.bak"
        profile_dir.mkdir(mode=0o700)
        profile_created = True
        state = {
            "tool_version": VERSION,
            "phase": "preparing",
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "profile_name": profile_name,
            "profile_number": profile_number,
            "relative_profile": relative_profile,
            "profiles_ini_existed": profiles_ini_existed,
            "root_existed": root_existed,
            "profiles_dir_created": profiles_dir_created,
            "backup_name": backup_path.name if backup_path else None,
            "original_profiles_mode": original_mode,
            "original_profiles_sha256": _sha256_bytes(original_bytes),
            "installed_profiles_sha256": _sha256_bytes(installed_bytes),
            "appended_block": appended_block.decode("utf-8"),
            **summary,
        }
        _atomic_write(
            profile_dir / STATE_FILE,
            (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            0o600,
        )

        if backup_path:
            _write_exclusive(backup_path, original_bytes, 0o600)
            backup_created = True

        compatibility = b"[Compatibility]\nLastVersion=152\n"
        _atomic_write(profile_dir / "compatibility.ini", compatibility, 0o600)
        _build_places_database(profile_dir / "places.sqlite", parsed)

        if profiles_ini_existed:
            if (
                not profiles_ini.is_file()
                or profiles_ini.is_symlink()
                or profiles_ini.read_bytes() != original_bytes
                or stat.S_IMODE(profiles_ini.stat().st_mode) != original_mode
            ):
                raise UserError("profiles.ini changed during prepare; no profile was registered")
        elif profiles_ini.exists():
            raise UserError("profiles.ini appeared during prepare; no profile was registered")

        ini_mode = original_mode if original_mode is not None else 0o600
        _atomic_write(profiles_ini, installed_bytes, ini_mode)
        state["phase"] = "prepared"
        _atomic_write(
            profile_dir / STATE_FILE,
            (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            0o600,
        )
        return state
    except BaseException:
        safe_to_remove = False
        try:
            if profiles_ini.exists():
                if profiles_ini.is_file() and not profiles_ini.is_symlink():
                    current_bytes = profiles_ini.read_bytes()
                    if current_bytes == installed_bytes:
                        if profiles_ini_existed:
                            _atomic_write(
                                profiles_ini,
                                original_bytes,
                                original_mode or 0o600,
                            )
                        else:
                            profiles_ini.unlink()
                        safe_to_remove = True
                    elif profiles_ini_existed and current_bytes == original_bytes:
                        safe_to_remove = True
                    else:
                        identity_fragments = (
                            f"Name=Edge-History-To-Chrome-{run_id}".encode("utf-8"),
                            f"Path={relative_profile}".encode("utf-8"),
                        )
                        safe_to_remove = not any(
                            fragment in current_bytes for fragment in identity_fragments
                        )
            elif not profiles_ini_existed:
                safe_to_remove = True
        except Exception:
            safe_to_remove = False

        if safe_to_remove:
            if profile_created and profile_dir.exists() and not profile_dir.is_symlink():
                shutil.rmtree(profile_dir)
            if backup_created and backup_path and backup_path.exists():
                if (
                    backup_path.is_file()
                    and not backup_path.is_symlink()
                    and _sha256_bytes(backup_path.read_bytes())
                    == _sha256_bytes(original_bytes)
                ):
                    backup_path.unlink()
            if (
                profiles_dir_created
                and profiles_dir.exists()
                and not any(profiles_dir.iterdir())
            ):
                profiles_dir.rmdir()
            if not root_existed and root.exists() and not any(root.iterdir()):
                root.rmdir()
        raise


def _load_state(profile_dir: Path) -> dict:
    state_path = profile_dir / STATE_FILE
    if state_path.is_symlink():
        raise UserError(f"Managed profile state must not be a symlink: {state_path}")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UserError(f"Could not read managed profile state: {state_path}") from exc
    run_id = state.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise UserError(f"Managed profile has an invalid run ID: {profile_dir}")
    expected_profile = PROFILE_PREFIX + run_id
    if state.get("profile_name") != expected_profile or profile_dir.name != expected_profile:
        raise UserError(f"Managed profile state does not match its directory: {profile_dir}")
    profile_number = state.get("profile_number")
    if (
        isinstance(profile_number, bool)
        or not isinstance(profile_number, int)
        or profile_number < 0
    ):
        raise UserError(f"Managed profile has an invalid profile number: {profile_dir}")
    expected_relative = f"Profiles/{expected_profile}"
    if state.get("relative_profile") != expected_relative:
        raise UserError(f"Managed profile has an invalid relative path: {profile_dir}")
    for key in ("profiles_ini_existed", "root_existed", "profiles_dir_created"):
        if not isinstance(state.get(key), bool):
            raise UserError(f"Managed profile has an invalid {key} value: {profile_dir}")
    expected_backup = (
        f"profiles.ini.edge-history-to-chrome.{run_id}.bak"
        if state["profiles_ini_existed"]
        else None
    )
    if state.get("backup_name") != expected_backup:
        raise UserError(f"Managed profile has an invalid backup name: {profile_dir}")
    for key in ("original_profiles_sha256", "installed_profiles_sha256"):
        value = state.get(key)
        if not isinstance(value, str) or not HASH_RE.fullmatch(value):
            raise UserError(f"Managed profile has an invalid {key} value: {profile_dir}")
    original_mode = state.get("original_profiles_mode")
    if state["profiles_ini_existed"]:
        if (
            isinstance(original_mode, bool)
            or not isinstance(original_mode, int)
            or not 0 <= original_mode <= 0o7777
        ):
            raise UserError(f"Managed profile has no valid original file mode: {profile_dir}")
    elif original_mode is not None:
        raise UserError(f"Managed profile has an unexpected original file mode: {profile_dir}")
    expected_section = (
        f"[Profile{profile_number}]\n"
        f"Name=Edge-History-To-Chrome-{run_id}\n"
        "IsRelative=1\n"
        f"Path={expected_relative}\n"
    )
    block = state.get("appended_block")
    if block not in {"\n" + expected_section, "\n\n" + expected_section}:
        raise UserError(f"Managed profile has an invalid profiles.ini block: {profile_dir}")
    if state.get("phase") not in {"preparing", "prepared"}:
        raise UserError(f"Managed profile has an invalid phase: {profile_dir}")
    return state


def status(firefox_root: Path) -> List[dict]:
    root = _validate_root(firefox_root)
    results = []
    for profile_dir in _find_managed_profiles(root):
        state = _load_state(profile_dir)
        if state["phase"] != "prepared":
            raise UserError(
                f"An interrupted prepare operation exists for {profile_dir.name}; run cleanup"
            )
        database = profile_dir / "places.sqlite"
        if not database.is_file() or database.is_symlink():
            raise UserError(f"Managed profile has no safe places.sqlite: {profile_dir}")
        uri = f"file:{quote(str(database))}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            visits = connection.execute("SELECT COUNT(*) FROM moz_historyvisits").fetchone()[0]
            unique_urls = connection.execute("SELECT COUNT(*) FROM moz_places").fetchone()[0]
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            oldest, newest = connection.execute(
                "SELECT MIN(visit_date), MAX(visit_date) FROM moz_historyvisits"
            ).fetchone()
        finally:
            connection.close()
        results.append(
            {
                "run_id": state["run_id"],
                "profile": profile_dir.name,
                "visits": visits,
                "unique_urls": unique_urls,
                "oldest": _format_usec(oldest),
                "newest": _format_usec(newest),
                "integrity": integrity,
            }
        )
    return results


@_locked
def cleanup(firefox_root: Path) -> dict:
    root = _validate_root(firefox_root)
    managed = _find_managed_profiles(root)
    if not managed:
        raise UserError("No managed import profile was found")
    if len(managed) > 1:
        raise UserError("More than one managed profile exists; remove them one at a time")

    profile_dir = managed[0]
    state = _load_state(profile_dir)
    expected_parent = root / "Profiles"
    if profile_dir.parent != expected_parent or profile_dir.is_symlink():
        raise UserError(f"Refusing to remove an unsafe profile path: {profile_dir}")

    known_files = {
        STATE_FILE,
        "compatibility.ini",
        "places.sqlite",
        "places.sqlite-journal",
        "places.sqlite-shm",
        "places.sqlite-wal",
    }
    unknown_entries = []
    for entry in profile_dir.iterdir():
        name = entry.name
        known_temp = name.startswith(f".{STATE_FILE}.") or name.startswith(
            ".compatibility.ini."
        )
        if name not in known_files and not known_temp:
            unknown_entries.append(name)
        elif entry.is_dir() and not entry.is_symlink():
            unknown_entries.append(name)
    if unknown_entries:
        raise UserError("Managed profile contains unexpected files; nothing was removed")

    profiles_ini = root / "profiles.ini"
    block = str(state.get("appended_block", "")).encode("utf-8")
    backup_name = state.get("backup_name")
    backup_path = root / backup_name if backup_name else None
    root_temp_files = [
        path
        for path in root.iterdir()
        if path.name.startswith(".profiles.ini.edge-history-to-chrome.")
    ]
    if any(path.is_symlink() or not path.is_file() for path in root_temp_files):
        raise UserError("Firefox data directory contains an unsafe temporary profiles file")
    if backup_path and backup_path.exists():
        if (
            not backup_path.is_file()
            or backup_path.is_symlink()
            or _sha256_bytes(backup_path.read_bytes())
            != state["original_profiles_sha256"]
        ):
            raise UserError("Managed backup changed; nothing was removed")

    if profiles_ini.exists():
        if not profiles_ini.is_file() or profiles_ini.is_symlink():
            raise UserError("profiles.ini is unsafe; managed files were left in place")
        current_mode = stat.S_IMODE(profiles_ini.stat().st_mode)
        current_bytes = profiles_ini.read_bytes()
        occurrences = current_bytes.count(block)
        if occurrences > 1:
            raise UserError("profiles.ini contains more than one managed block")
        if occurrences == 1:
            updated = current_bytes.replace(block, b"", 1)
            generated_base = b"[General]\nStartWithLastProfile=1\nVersion=2\n"
            if not state["profiles_ini_existed"] and updated == generated_base:
                profiles_ini.unlink()
            else:
                _atomic_write(profiles_ini, updated, current_mode)
        else:
            identity_fragments = (
                f"[Profile{state['profile_number']}]".encode("utf-8"),
                f"Name=Edge-History-To-Chrome-{state['run_id']}".encode("utf-8"),
                f"Path=Profiles/{profile_dir.name}".encode("utf-8"),
            )
            if any(fragment in current_bytes for fragment in identity_fragments):
                raise UserError(
                    "profiles.ini contains a modified managed section; files were left in place"
                )
    else:
        if state["profiles_ini_existed"]:
            raise UserError("profiles.ini is missing; managed files were left in place")

    for entry in list(profile_dir.iterdir()):
        if entry.name == STATE_FILE:
            continue
        if entry.is_dir() and not entry.is_symlink():
            raise UserError(f"Refusing to remove unexpected directory: {entry}")
        entry.unlink()
    if backup_path and backup_path.exists():
        backup_path.unlink()
    for temp_path in root_temp_files:
        if temp_path.exists():
            temp_path.unlink()
    (profile_dir / STATE_FILE).unlink()
    profile_dir.rmdir()

    profiles_dir = root / "Profiles"
    if (
        state.get("profiles_dir_created")
        and profiles_dir.exists()
        and not any(profiles_dir.iterdir())
    ):
        profiles_dir.rmdir()
    if not state.get("root_existed") and root.exists() and not any(root.iterdir()):
        root.rmdir()
    return {"run_id": state["run_id"], "profile": state["profile_name"]}


def _root_from_argument(value: Optional[str]) -> Path:
    if value:
        return Path(value)
    if sys.platform != "darwin":
        raise UserError(
            "The default Firefox path is available on macOS only; "
            "pass --firefox-root for tests"
        )
    return default_firefox_root()


def _print_summary(summary: dict) -> None:
    print(f"Visits: {summary['visits']}")
    print(f"Unique URLs: {summary['unique_urls']}")
    print(f"Oldest visit: {summary['oldest']}")
    print(f"Newest visit: {summary['newest']}")
    print(f"Skipped non-web rows: {summary['skipped_non_web']}")
    if summary.get("older_than_window"):
        print(
            f"Warning: {summary['older_than_window']} visits are older than "
            f"{HISTORY_WINDOW_DAYS} days; Chrome's local history backend may expire them."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare Edge history for Chrome's built-in Firefox importer."
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser(
        "prepare", help="create and register a temporary profile"
    )
    prepare_parser.add_argument("csv", type=Path, help="Edge history CSV")
    prepare_parser.add_argument("--firefox-root", help=argparse.SUPPRESS)
    prepare_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print counts without writing files",
    )

    status_parser = subparsers.add_parser(
        "status", help="check a managed profile without printing URLs"
    )
    status_parser.add_argument("--firefox-root", help=argparse.SUPPRESS)

    cleanup_parser = subparsers.add_parser(
        "cleanup", help="unregister and remove the managed profile"
    )
    cleanup_parser.add_argument("--firefox-root", help=argparse.SUPPRESS)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        root = _root_from_argument(getattr(args, "firefox_root", None))
        if args.command == "prepare":
            result = prepare(args.csv, root, dry_run=args.dry_run)
            _print_summary(result)
            if not args.dry_run:
                print("\nOpen chrome://settings/importData")
                print("Choose Mozilla Firefox and select Browsing history only.")
                print(
                    "After Chrome shows the visits, run: "
                    "python3 edge_history_to_chrome.py cleanup"
                )
        elif args.command == "status":
            results = status(root)
            if not results:
                print("No managed import profile found.")
            for result in results:
                print(f"Profile: {result['profile']}")
                print(f"Visits: {result['visits']}")
                print(f"Unique URLs: {result['unique_urls']}")
                print(f"Oldest visit: {result['oldest']}")
                print(f"Newest visit: {result['newest']}")
                print(f"Integrity: {result['integrity']}")
        elif args.command == "cleanup":
            result = cleanup(root)
            print(f"Removed managed profile: {result['profile']}")
        return 0
    except UserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
