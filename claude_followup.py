#!/usr/bin/env python3
"""Schedule follow-up prompts into a running Claude Code session.

Types a message into a zellij or tmux session at a time you choose -- a fixed
delay, a clock time, or automatically when your Claude usage limit resets.
Delivery is handed to a transient systemd --user timer, so the schedule
survives this process exiting.

Stdlib only. Linux only (systemd --user).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__version__ = "0.1.0"

CANONICAL_PROG = "claude-followup"


def prog_name(argv0: str | None = None) -> str:
    """Report the name we were actually invoked as, so `cf --help` says `cf`."""
    name = Path(argv0 if argv0 is not None else (sys.argv[0] or "")).name
    if not name or name.endswith(".py") or name.startswith("python"):
        return CANONICAL_PROG
    return name


PROG = prog_name()
# Unit names must stay stable across aliases or list/cancel would miss jobs.
UNIT_PREFIX = "claude-followup"
DEFAULT_MESSAGE = "continue"
DEFAULT_BUFFER_SEC = 30

# Env vars forwarded into the systemd timer so the multiplexer client can still
# find its server socket and its binary when it fires.
ENV_PASSTHROUGH = (
    "PATH",
    "HOME",
    "XDG_RUNTIME_DIR",
    "TMUX_TMPDIR",
    "ZELLIJ_CONFIG_DIR",
    "ZELLIJ_CONFIG_FILE",
    "CLAUDE_HOME",
)


class FollowupError(Exception):
    """Anything the user should see as a clean one-line error."""


class ParseError(FollowupError):
    pass


# --------------------------------------------------------------------------
# time parsing (pure functions -- these are what the tests cover)
# --------------------------------------------------------------------------

_DURATION_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}
_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

# "3:15pm", "15:00", "3pm" -- a bare "15" is rejected as ambiguous.
_TIME_OF_DAY = re.compile(
    r"^(?P<hour>\d{1,2})"
    r"(?::(?P<minute>\d{2}))?"
    r"\s*(?P<meridiem>am|pm|a\.m\.|p\.m\.)?$"
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Observed shapes of Claude Code's usage-limit notice:
#   "You've hit your session limit · resets 5pm (UTC)"
#   "You've hit your session limit · resets 3:30pm (UTC)"
#   "You've hit your weekly limit · resets Jul 8, 9am (UTC)"
# The weekly form carries a date, and dropping it would schedule days early.
_RESET = re.compile(
    r"resets?\b\s*(?:at\s+|on\s+)?"
    r"(?:(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+"
    r"(?P<day>\d{1,2})\s*,?\s+)?"
    r"(?P<time>\d{1,2}:\d{2}\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm))"
    r"\s*(?:\(\s*(?P<tz>[^)]+?)\s*\))?",
    re.IGNORECASE,
)


def parse_duration(text: str) -> int:
    """Relative duration -> whole seconds.

    Accepts '90m', '2h30m', '1h5m10s', '90 minutes', 'in 1 hour'.
    A bare number is rejected: '90' could mean seconds or minutes.
    """
    s = " ".join(text.strip().lower().split())
    s = re.sub(r"^in\s+", "", s)
    if not s:
        raise ParseError("empty duration")

    total = 0.0
    cursor = 0
    seen = False
    for match in _DURATION_TOKEN.finditer(s):
        gap = s[cursor:match.start()].strip(" ,+")
        if gap and gap != "and":
            raise ParseError(f"bad duration: {text!r}")
        unit = match.group(2)
        if unit not in _DURATION_UNITS:
            raise ParseError(f"unknown time unit {unit!r} in {text!r}")
        total += float(match.group(1)) * _DURATION_UNITS[unit]
        cursor = match.end()
        seen = True

    if not seen:
        if s.isdigit():
            raise ParseError(f"{text!r} is ambiguous -- write {s}m or {s}s")
        raise ParseError(f"bad duration: {text!r}")
    if s[cursor:].strip(" ,+"):
        raise ParseError(f"trailing junk in duration: {text!r}")
    if total <= 0:
        raise ParseError("duration must be greater than zero")
    return int(total)


def _time_of_day(text: str) -> tuple[int, int]:
    match = _TIME_OF_DAY.match(text.strip())
    if not match:
        raise ParseError(f"bad time of day: {text!r}")
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    meridiem = (match.group("meridiem") or "").replace(".", "")

    if meridiem:
        if not 1 <= hour <= 12:
            raise ParseError(f"bad 12-hour time: {text!r}")
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif match.group("minute") is None:
        raise ParseError(f"{text!r} is ambiguous -- write {text}:00 or {text}pm")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ParseError(f"time out of range: {text!r}")
    return hour, minute


def parse_clock(text: str, now: datetime | None = None) -> datetime:
    """Clock or calendar time -> aware datetime in the local zone.

    Accepts '15:00', '3:15pm', 'tomorrow 09:00', 'friday 8am',
    '2026-08-06 15:00'. Bare times that already passed roll to tomorrow.
    """
    now = now or datetime.now().astimezone()
    s = " ".join(text.strip().lower().split())
    if not s:
        raise ParseError("empty time")

    day: date | None = None
    rolls_over = True  # only bare times-of-day may roll to the next day

    iso = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[ t]+(.*))?$", s)
    weekday = re.match(r"^(?:next\s+)?([a-z]+)\s+(.*)$", s)
    relative = re.match(r"^(today|tomorrow|tmr|tonight)\s+(.*)$", s)

    if iso:
        try:
            day = date.fromisoformat(iso.group(1))
        except ValueError as exc:
            raise ParseError(f"bad date: {iso.group(1)!r}") from exc
        s = (iso.group(2) or "00:00").strip()
        rolls_over = False
    elif relative:
        offset = 0 if relative.group(1) == "today" else 1
        if relative.group(1) == "tonight":
            offset = 0
        day = now.date() + timedelta(days=offset)
        s = relative.group(2).strip()
        rolls_over = False
    elif weekday and weekday.group(1) in _WEEKDAYS:
        target = _WEEKDAYS[weekday.group(1)]
        ahead = (target - now.weekday()) % 7 or 7
        day = now.date() + timedelta(days=ahead)
        s = weekday.group(2).strip()
        rolls_over = False

    hour, minute = _time_of_day(s)
    when = datetime.combine(day or now.date(), datetime.min.time(), now.tzinfo)
    when = when.replace(hour=hour, minute=minute)

    if when <= now:
        if not rolls_over:
            raise ParseError(f"time already passed: {text!r}")
        when += timedelta(days=1)
    return when


def looks_like_duration(text: str) -> bool:
    try:
        parse_duration(text)
    except ParseError:
        return False
    return True


def looks_like_clock(text: str) -> bool:
    try:
        parse_clock(text)
    except ParseError:
        return False
    return True


def classify_when(text: str) -> str:
    """'90m' -> 'in', '3pm' -> 'at'. Duration wins ties (nothing parses as both)."""
    if looks_like_duration(text):
        return "in"
    if looks_like_clock(text):
        return "at"
    raise ParseError(
        f"cannot read {text!r} as a delay or a time "
        f"(try '90m', '2h30m', '15:00', '3pm', 'tomorrow 09:00')"
    )


def _resolve_zone(name: str | None, fallback: datetime):
    if not name:
        return fallback.tzinfo
    cleaned = name.strip()
    if cleaned.upper() in ("UTC", "GMT", "Z"):
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError):
        # Abbreviations like PST/CEST are not IANA zones; assume local and
        # let `detect` show the raw stamp so a wrong guess is visible.
        return fallback.tzinfo


def parse_reset(text: str, now: datetime | None = None) -> tuple[datetime, str] | None:
    """Find a Claude usage-limit reset stamp in arbitrary text.

    Returns (aware datetime, the raw stamp that matched), or None. Uses the
    last match in the text -- transcripts and pane dumps are append-ordered,
    so the last mention is the current one.
    """
    now = now or datetime.now().astimezone()
    matches = list(_RESET.finditer(text or ""))
    if not matches:
        return None
    match = matches[-1]

    raw_time = " ".join(match.group("time").split()).lower()
    raw_tz = match.group("tz")
    try:
        hour, minute = _time_of_day(raw_time)
    except ParseError:
        return None

    zone = _resolve_zone(raw_tz, now)
    local_now = now.astimezone(zone)
    when = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if match.group("month"):
        month = _MONTHS[match.group("month").lower()[:3]]
        day = int(match.group("day"))
        try:
            when = when.replace(year=local_now.year, month=month, day=day)
        except ValueError:
            return None  # e.g. Feb 30
        if when <= local_now:  # a December stamp read in January
            try:
                when = when.replace(year=local_now.year + 1)
            except ValueError:
                return None
        stamp = f"{match.group('month').title()} {day}, {raw_time}"
    else:
        # A bare "resets 7pm" has no date, and the text it came from was
        # written at some unknown point before we read it. Take the occurrence
        # NEAREST the anchor rather than the next one after it: reading "7pm"
        # at 19:04 means the reset happened four minutes ago, not in 23h56m.
        # Safe because undated stamps are session limits (hours, not days);
        # weekly limits carry an explicit date and are handled above.
        when = min(
            (when + timedelta(days=offset) for offset in (-1, 0, 1)),
            key=lambda candidate: abs(candidate - local_now),
        )
        stamp = raw_time

    if raw_tz:
        stamp += f" ({raw_tz})"
    return when.astimezone(now.tzinfo), stamp


# --------------------------------------------------------------------------
# multiplexer backends
# --------------------------------------------------------------------------


def _run(cmd: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd), capture_output=True, text=True, check=False, **kwargs
    )


_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


@dataclass(frozen=True)
class Backend:
    name: str

    @property
    def binary(self) -> str:
        return self.name

    def installed(self) -> bool:
        return shutil.which(self.binary) is not None

    def sessions(self) -> list[str]:
        raise NotImplementedError

    def has(self, target: str) -> bool:
        # A tmux target may be 'session:window.pane'; match on the session part.
        return target.split(":", 1)[0] in self.sessions()

    def send(self, target: str, message: str) -> None:
        raise NotImplementedError

    def capture(self, target: str) -> str:
        raise NotImplementedError


class Zellij(Backend):
    def sessions(self) -> list[str]:
        result = _run(["zellij", "list-sessions", "--short"])
        if result.returncode != 0:
            return []
        names = []
        for line in result.stdout.splitlines():
            name = _ANSI.sub("", line).strip()
            if name:
                names.append(name)
        return names

    def send(self, target: str, message: str) -> None:
        # write-chars only inserts text; Claude's TUI needs a real CR to submit.
        # `--` keeps a message starting with '-' from being read as a flag.
        for args in (["action", "write-chars", "--", message], ["action", "write", "13"]):
            result = _run(["zellij", "--session", target, *args])
            if result.returncode != 0:
                raise FollowupError(
                    f"zellij send failed for {target!r}: "
                    f"{result.stderr.strip() or 'unknown error'}"
                )

    def capture(self, target: str) -> str:
        result = _run(["zellij", "--session", target, "action", "dump-screen", "--full"])
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        # Older zellij writes to a path argument instead of stdout.
        scratch = Path(os.environ.get("TMPDIR", "/tmp")) / f".{PROG}-{os.getpid()}.dump"
        try:
            _run(["zellij", "--session", target, "action", "dump-screen",
                  "--full", str(scratch)])
            return scratch.read_text(errors="replace") if scratch.exists() else ""
        except OSError:
            return ""
        finally:
            scratch.unlink(missing_ok=True)


class Tmux(Backend):
    def sessions(self) -> list[str]:
        result = _run(["tmux", "list-sessions", "-F", "#{session_name}"])
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def send(self, target: str, message: str) -> None:
        for args in (["-l", "--", message], ["Enter"]):
            result = _run(["tmux", "send-keys", "-t", target, *args])
            if result.returncode != 0:
                raise FollowupError(
                    f"tmux send failed for {target!r}: "
                    f"{result.stderr.strip() or 'unknown error'}"
                )

    def capture(self, target: str) -> str:
        result = _run(["tmux", "capture-pane", "-p", "-t", target, "-S", "-2000"])
        return result.stdout if result.returncode == 0 else ""


BACKENDS = {"zellij": Zellij("zellij"), "tmux": Tmux("tmux")}


def resolve_backend(target: str, preferred: str = "auto") -> Backend:
    """Pick the backend that actually owns `target`."""
    if preferred != "auto":
        backend = BACKENDS[preferred]
        if not backend.installed():
            raise FollowupError(f"{preferred} is not installed")
        if not backend.has(target):
            raise FollowupError(
                f"no {preferred} session named {target!r} "
                f"(have: {', '.join(backend.sessions()) or 'none'})"
            )
        return backend

    owners = [b for b in BACKENDS.values() if b.installed() and b.has(target)]
    if len(owners) == 1:
        return owners[0]
    if not owners:
        known = {n: b.sessions() for n, b in BACKENDS.items() if b.installed()}
        detail = "; ".join(f"{n}: {', '.join(s) or 'none'}" for n, s in known.items())
        raise FollowupError(f"session not found: {target!r} ({detail or 'no multiplexer installed'})")
    # Same name in both -- prefer the one we are sitting in, else make them choose.
    current = current_backend_name()
    for backend in owners:
        if backend.name == current:
            return backend
    raise FollowupError(
        f"{target!r} exists in both zellij and tmux -- pass --backend to choose"
    )


def current_backend_name() -> str | None:
    if os.environ.get("ZELLIJ"):
        return "zellij"
    if os.environ.get("TMUX"):
        return "tmux"
    return None


def all_sessions() -> list[tuple[str, str]]:
    found = []
    for name, backend in BACKENDS.items():
        if backend.installed():
            found.extend((name, session) for session in backend.sessions())
    return found


# --------------------------------------------------------------------------
# reset detection: find the Claude transcript behind a multiplexer session
# --------------------------------------------------------------------------


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))


def _proc_env(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    env = {}
    for chunk in raw.split(b"\0"):
        if b"=" in chunk:
            key, _, value = chunk.partition(b"=")
            env[key.decode(errors="replace")] = value.decode(errors="replace")
    return env


def _tmux_session_of_pane(pane: str) -> str | None:
    result = _run(["tmux", "display-message", "-p", "-t", pane, "#{session_name}"])
    name = result.stdout.strip()
    return name or None


def claude_session_id(target: str, backend: Backend) -> str | None:
    """Map a multiplexer session to the Claude sessionId running inside it."""
    sessions_dir = claude_home() / "sessions"
    if not sessions_dir.is_dir():
        return None

    wanted = target.split(":", 1)[0]
    for meta_file in sorted(sessions_dir.glob("*.json"), key=_mtime, reverse=True):
        try:
            meta = json.loads(meta_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid, session_id = meta.get("pid"), meta.get("sessionId")
        if not pid or not session_id or not Path(f"/proc/{pid}").exists():
            continue

        env = _proc_env(int(pid))
        if backend.name == "zellij":
            if env.get("ZELLIJ_SESSION_NAME") == wanted:
                return session_id
        elif env.get("TMUX_PANE") and _tmux_session_of_pane(env["TMUX_PANE"]) == wanted:
            return session_id
    return None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def transcript_files(session_id: str | None) -> list[Path]:
    projects = claude_home() / "projects"
    if not projects.is_dir():
        return []
    if session_id:
        return sorted(projects.glob(f"*/{session_id}.jsonl"))
    recent = sorted(projects.glob("*/*.jsonl"), key=_mtime, reverse=True)
    return recent[:40]


def parse_iso(stamp: str | None) -> datetime | None:
    """Transcript timestamps look like '2026-07-08T13:54:17.138Z'."""
    if not stamp:
        return None
    try:
        # fromisoformat only learned to read a trailing 'Z' in 3.11.
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def rate_limit_texts(path: Path) -> Iterator[tuple[datetime | None, str]]:
    """Yield (when the entry was written, its text) for rate-limit entries.

    The write time matters: a reset stamp like 'resets 5pm' has no date, so it
    only means anything relative to when it was recorded.
    """
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                # Cheap prefilter -- transcripts get large and most lines miss.
                if "rate_limit" not in line and "429" not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("error") != "rate_limit" and entry.get("apiErrorStatus") != 429:
                    continue
                written = parse_iso(entry.get("timestamp"))
                content = (entry.get("message") or {}).get("content")
                if isinstance(content, str):
                    yield written, content
                elif isinstance(content, list):
                    yield written, "\n".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
    except OSError:
        return


@dataclass
class Detection:
    when: datetime
    stamp: str
    source: str


def detect_reset(target: str, backend: Backend) -> Detection:
    """Find the next usage-limit reset for a session. Raises if none is in the future."""
    now = datetime.now().astimezone()
    candidates: list[Detection] = []

    # The pane is live, so 'now' is the right anchor for whatever it shows.
    found = parse_reset(backend.capture(target), now)
    if found:
        candidates.append(Detection(found[0], found[1], f"{backend.name} pane"))

    session_id = claude_session_id(target, backend)
    for path in transcript_files(session_id):
        for written, text in rate_limit_texts(path):
            # Anchor to when the notice was written, not to now. 'resets 5pm'
            # recorded last month means 5pm THAT day -- long since reset.
            found = parse_reset(text, written or now)
            if found:
                label = "transcript" if session_id else f"transcript {path.stem[:8]}"
                candidates.append(Detection(found[0], found[1], label))
        if session_id:
            break

    future = [c for c in candidates if c.when > now]
    if not future:
        if candidates:
            latest = max(candidates, key=lambda c: c.when)
            raise FollowupError(
                f"that limit already reset at {fmt_time(latest.when)} "
                f"(from {latest.source}) -- nothing to wait for, just continue"
            )
        raise FollowupError(
            f"no usage-limit reset found for {target!r} -- "
            f"use --in or --at instead"
        )
    return min(future, key=lambda c: c.when)


# --------------------------------------------------------------------------
# systemd scheduling
# --------------------------------------------------------------------------


def require_systemd() -> None:
    if not shutil.which("systemd-run") or not shutil.which("systemctl"):
        raise FollowupError("systemd-run/systemctl not found; this tool needs systemd --user")


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9:_.-]", "_", name) or "session"


def unit_name(target: str, fire_at: int) -> str:
    return f"{UNIT_PREFIX}-{sanitize(target)}-{fire_at}-{secrets.token_hex(2)}"


@dataclass
class Job:
    unit: str
    target: str
    message: str
    fire_at: int | None
    state: str

    def as_dict(self) -> dict:
        return {
            "unit": self.unit,
            "session": self.target,
            "message": self.message,
            "fire_at": self.fire_at,
            "fire_at_local": fmt_time(datetime.fromtimestamp(self.fire_at).astimezone())
            if self.fire_at
            else None,
            "state": self.state,
        }


# Description doubles as our metadata store: systemctl hands it back verbatim.
_DESCRIPTION = re.compile(rf"^{UNIT_PREFIX}\s+(?P<backend>\S+)\s+(?P<target>.*?) :: (?P<message>.*)$")


def describe(backend: str, target: str, message: str) -> str:
    return f"{UNIT_PREFIX} {backend} {target} :: {message}"


def self_command() -> list[str]:
    """How to re-invoke this program from a systemd unit."""
    return [sys.executable, str(Path(__file__).resolve())]


def schedule(
    backend: Backend,
    target: str,
    message: str,
    fire_at: int,
    *,
    dry_run: bool = False,
) -> Job:
    require_systemd()
    delay = fire_at - int(time.time())
    if delay <= 0:
        raise FollowupError("that time is in the past")

    unit = unit_name(target, fire_at)
    cmd = [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        f"--description={describe(backend.name, target, message)}",
        f"--on-active={delay}s",
        "--timer-property=AccuracySec=1s",
    ]
    cmd += [
        f"--setenv={key}={os.environ[key]}"
        for key in ENV_PASSTHROUGH
        if os.environ.get(key)
    ]
    cmd += [*self_command(), "send", "--backend", backend.name, target, "--message", message]

    if not dry_run:
        result = _run(cmd)
        if result.returncode != 0:
            raise FollowupError(f"systemd-run failed: {result.stderr.strip()}")
    return Job(unit=unit, target=target, message=message, fire_at=fire_at, state="waiting")


def list_jobs() -> list[Job]:
    require_systemd()
    result = _run([
        "systemctl", "--user", "list-units", "--type=timer", "--all",
        "--plain", "--no-legend", "--no-pager", f"{UNIT_PREFIX}-*",
    ])
    jobs = []
    for line in result.stdout.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 5 or not fields[0].startswith(UNIT_PREFIX):
            continue
        unit = fields[0].removesuffix(".timer")
        parsed = _DESCRIPTION.match(fields[4].strip())
        target, message = (parsed.group("target"), parsed.group("message")) if parsed else ("?", "?")
        jobs.append(Job(unit, target, message, fire_at_of(unit), fields[3]))
    return sorted(jobs, key=lambda job: job.fire_at or 0)


def fire_at_of(unit: str) -> int | None:
    """Fire time is encoded in the unit name: prefix-session-<epoch>-<rand>."""
    parts = unit.split("-")
    return int(parts[-2]) if len(parts) >= 2 and parts[-2].isdigit() else None


def cancel_jobs(targets: Iterable[str], *, all_jobs: bool = False) -> list[Job]:
    jobs = list_jobs()
    if all_jobs:
        doomed = jobs
    else:
        wanted = set(targets)
        doomed = [
            job for job in jobs
            if job.target in wanted
            or job.unit in wanted
            or f"{job.unit}.timer" in wanted
        ]
    for job in doomed:
        _run(["systemctl", "--user", "stop", f"{job.unit}.timer", f"{job.unit}.service"])
        _run(["systemctl", "--user", "reset-failed", f"{job.unit}.timer", f"{job.unit}.service"])
    return doomed


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------


def fmt_time(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def fmt_delta(seconds: int) -> str:
    if seconds < 0:
        return "overdue"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def emit(data: object, as_json: bool, human: str) -> None:
    print(json.dumps(data, indent=2) if as_json else human)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def resolve_fire_time(args, backend: Backend, target: str) -> tuple[int, str]:
    """-> (epoch to fire at, how we got there)."""
    now = datetime.now().astimezone()

    if args.auto:
        found = detect_reset(target, backend)
        base = found.when
        how = f"auto: reset {found.stamp} from {found.source}"
    elif args.at:
        base = parse_clock(args.at, now)
        how = f"at {args.at}"
    elif args.in_:
        base = now + timedelta(seconds=parse_duration(args.in_))
        how = f"in {args.in_}"
    else:  # positional: 'schedule sess 90m ...' / 'schedule sess 3pm ...'
        when = args.when
        if classify_when(when) == "in":
            base = now + timedelta(seconds=parse_duration(when))
            how = f"in {when}"
        else:
            base = parse_clock(when, now)
            how = f"at {when}"

    return int(base.timestamp()) + args.buffer, how


def cmd_schedule(args) -> int:
    target = args.session
    backend = resolve_backend(target, args.backend)
    message = args.message or DEFAULT_MESSAGE
    fire_at, how = resolve_fire_time(args, backend, target)
    job = schedule(backend, target, message, fire_at, dry_run=args.dry_run)

    when = datetime.fromtimestamp(fire_at).astimezone()
    countdown = fmt_delta(fire_at - int(time.time()))
    payload = job.as_dict() | {"backend": backend.name, "how": how, "dry_run": args.dry_run}
    emit(
        payload,
        args.json,
        "\n".join([
            f"{'would queue' if args.dry_run else 'queued'} -> {backend.name}:{target}",
            f"  fires  {fmt_time(when)}  (in {countdown}, includes +{args.buffer}s buffer)",
            f"  via    {how}",
            f"  says   {message}",
            f"  unit   {job.unit}.timer",
            f"  cancel {PROG} cancel {target}",
        ]),
    )
    return 0


def cmd_send(args) -> int:
    backend = resolve_backend(args.session, args.backend)
    message = args.message or DEFAULT_MESSAGE
    if not args.dry_run:
        backend.send(args.session, message)
    verb = "would send" if args.dry_run else "sent"
    emit(
        {"backend": backend.name, "session": args.session, "message": message,
         "dry_run": args.dry_run},
        args.json,
        f"{verb} -> {backend.name}:{args.session}: {message}",
    )
    return 0


def cmd_detect(args) -> int:
    backend = resolve_backend(args.session, args.backend)
    found = detect_reset(args.session, backend)
    fire_at = int(found.when.timestamp()) + args.buffer
    emit(
        {
            "backend": backend.name,
            "session": args.session,
            "reset_at": int(found.when.timestamp()),
            "reset_at_local": fmt_time(found.when),
            "stamp": found.stamp,
            "source": found.source,
            "fire_at_local": fmt_time(datetime.fromtimestamp(fire_at).astimezone()),
        },
        args.json,
        "\n".join([
            f"session {backend.name}:{args.session}",
            f"  reset  {fmt_time(found.when)}  (in {fmt_delta(int(found.when.timestamp()) - int(time.time()))})",
            f"  stamp  {found.stamp}",
            f"  source {found.source}",
            f"  would fire {fmt_time(datetime.fromtimestamp(fire_at).astimezone())} (+{args.buffer}s)",
        ]),
    )
    return 0


def cmd_list(args) -> int:
    jobs = list_jobs()
    if args.json:
        print(json.dumps([job.as_dict() for job in jobs], indent=2))
        return 0
    if not jobs:
        print("no follow-ups queued")
        return 0
    now = int(time.time())
    print(f"{'WHEN':<21} {'IN':>8}  {'SESSION':<16} MESSAGE")
    for job in jobs:
        when = fmt_time(datetime.fromtimestamp(job.fire_at).astimezone()) if job.fire_at else "?"
        left = fmt_delta(job.fire_at - now) if job.fire_at else "?"
        print(f"{when:<21} {left:>8}  {job.target:<16} {job.message}")
    return 0


def cmd_cancel(args) -> int:
    if not args.targets and not args.all:
        raise FollowupError("name a session or unit to cancel, or pass --all")
    cancelled = cancel_jobs(args.targets, all_jobs=args.all)
    if not cancelled:
        raise FollowupError(f"nothing matched: {', '.join(args.targets) or '--all'}")
    emit(
        [job.as_dict() for job in cancelled],
        args.json,
        "\n".join(f"cancelled -> {job.target}: {job.message}" for job in cancelled),
    )
    return 0


def cmd_sessions(args) -> int:
    found = all_sessions()
    if args.json:
        print(json.dumps([{"backend": b, "session": s} for b, s in found], indent=2))
        return 0
    if not found:
        print("no zellij or tmux sessions running")
        return 0
    current = current_backend_name()
    for backend_name, session in found:
        here = "  <- you are here" if (
            backend_name == current
            and session in (os.environ.get("ZELLIJ_SESSION_NAME"), _current_tmux_session())
        ) else ""
        print(f"{backend_name:<7} {session}{here}")
    return 0


def _current_tmux_session() -> str | None:
    if not os.environ.get("TMUX"):
        return None
    return _run(["tmux", "display-message", "-p", "#{session_name}"]).stdout.strip() or None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

EPILOG = f"""\
examples:
  {PROG} schedule claude4 --in 90m
  {PROG} schedule claude4 --at 15:00 -m "run the test suite"
  {PROG} schedule claude4 --auto -m "resume the gate sweep"
  {PROG} schedule work 2h30m continue with the refactor
  {PROG} schedule work 'tomorrow 09:00' -m "morning standup summary"
  {PROG} detect claude4
  {PROG} list
  {PROG} cancel claude4

timing:
  --in   delay:  90m, 45s, 2h30m, '90 minutes', '1 hour'
  --at   clock:  15:00, 3:15pm, 'tomorrow 09:00', 'friday 8am', 2026-08-06 15:00
  --auto reads the usage-limit reset out of the session's pane and transcript

environment:
  CLAUDE_FOLLOWUP_BUFFER  seconds added after the target time (default {DEFAULT_BUFFER_SEC})
  CLAUDE_HOME             Claude Code state dir (default ~/.claude)
"""


def build_parser() -> argparse.ArgumentParser:
    default_buffer = int(os.environ.get("CLAUDE_FOLLOWUP_BUFFER", DEFAULT_BUFFER_SEC))

    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Schedule follow-up prompts into a running Claude Code session.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")

    session_opts = argparse.ArgumentParser(add_help=False)
    session_opts.add_argument(
        "--backend", choices=["auto", "zellij", "tmux"], default="auto",
        help="which multiplexer owns the session (default: auto-detect)",
    )
    session_opts.add_argument(
        "--dry-run", action="store_true", help="show what would happen, change nothing",
    )

    timing = argparse.ArgumentParser(add_help=False)
    timing.add_argument(
        "--buffer", type=int, default=default_buffer, metavar="SEC",
        help=f"seconds to wait past the target time (default: {default_buffer})",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sched = sub.add_parser(
        "schedule", aliases=["add"], parents=[common, session_opts, timing],
        help="queue a message for later", description="Queue a message for later delivery.",
    )
    sched.add_argument("session", help="zellij or tmux session name")
    when = sched.add_mutually_exclusive_group()
    when.add_argument("--in", dest="in_", metavar="DURATION", help="fire after a delay, e.g. 90m")
    when.add_argument("--at", metavar="TIME", help="fire at a clock time, e.g. 15:00")
    when.add_argument("--auto", action="store_true", help="fire when the usage limit resets")
    sched.add_argument("-m", "--message", help="message to type (default: %(default)s)",
                       default=None)
    sched.add_argument("rest", nargs="*", metavar="[WHEN] [MESSAGE...]",
                       help="bare timing and/or message, when not using flags")
    sched.set_defaults(func=cmd_schedule)

    send = sub.add_parser(
        "send", aliases=["now"], parents=[common, session_opts],
        help="type a message into a session right now",
    )
    send.add_argument("session")
    send.add_argument("-m", "--message", default=None)
    send.add_argument("rest", nargs="*", metavar="[MESSAGE...]")
    send.set_defaults(func=cmd_send)

    detect = sub.add_parser(
        "detect", parents=[common, session_opts, timing],
        help="show the detected usage-limit reset without scheduling",
    )
    detect.add_argument("session")
    detect.set_defaults(func=cmd_detect)

    listing = sub.add_parser("list", aliases=["ls"], parents=[common],
                             help="show queued follow-ups")
    listing.set_defaults(func=cmd_list)

    cancel = sub.add_parser("cancel", aliases=["rm"], parents=[common],
                            help="cancel queued follow-ups")
    cancel.add_argument("targets", nargs="*", metavar="SESSION|UNIT")
    cancel.add_argument("--all", action="store_true", help="cancel every queued follow-up")
    cancel.set_defaults(func=cmd_cancel)

    sessions = sub.add_parser("sessions", parents=[common],
                              help="list zellij and tmux sessions")
    sessions.set_defaults(func=cmd_sessions)

    return parser


def absorb_rest(args) -> None:
    """Fold the bare `[WHEN] [MESSAGE...]` positionals into --in/--at/-m."""
    rest = list(getattr(args, "rest", []) or [])
    if args.command in ("schedule", "add") and rest:
        flagged = bool(args.in_ or args.at or args.auto)
        if not flagged:
            args.when = rest.pop(0)
    if rest:
        if args.message:
            raise FollowupError("pass the message with -m or as bare words, not both")
        args.message = " ".join(rest)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0

    args.when = None
    try:
        absorb_rest(args)
        if args.command in ("schedule", "add") and not (
            args.in_ or args.at or args.auto or args.when
        ):
            raise FollowupError("say when: --in, --at, --auto, or a bare delay/time")
        return args.func(args)
    except FollowupError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
