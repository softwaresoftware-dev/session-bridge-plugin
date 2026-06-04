"""Discover Claude Code sessions from ~/.claude/sessions/*.json."""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
YAKUAKE_SERVICE = "org.kde.yakuake"


@dataclass
class DiscoveredSession:
    session_id: str
    pid: int
    cwd: str
    started_at: int
    tab_title: str = ""


def _is_pid_alive(pid: int) -> bool:
    # Signal 0 probes existence without delivering a signal — portable across
    # Linux and macOS, unlike reading /proc (which doesn't exist on macOS).
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ppid_map() -> dict[int, int]:
    """Map every pid -> ppid via a single `ps` call (portable: Linux + macOS)."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    parents: dict[int, int] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            parents[int(parts[0])] = int(parts[1])
    return parents


def _name_from_path(path: str) -> str:
    if not path:
        return "unknown"
    return os.path.basename(path.rstrip("/")) or "unknown"


def _qdbus(path: str, method: str) -> str | None:
    result = subprocess.run(
        ["qdbus", YAKUAKE_SERVICE, path, method],
        capture_output=True, text=True, timeout=5,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _qdbus_call(path: str, method: str, *args: str) -> str | None:
    result = subprocess.run(
        ["qdbus", YAKUAKE_SERVICE, path, method, *args],
        capture_output=True, text=True, timeout=5,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _build_shell_to_tab_title() -> dict[int, str]:
    """Map Konsole session shell PIDs to Yakuake tab titles.

    Yakuake terminal IDs and Konsole session IDs are different namespaces with
    no direct API bridge. When the counts are equal, sorted positional matching
    works reliably (both are monotonically increasing, created in sequence).
    When counts differ (stale terminals or sessions), falls back to direct
    matches only (terminal IDs that happen to equal Konsole session IDs).
    """
    shell_to_title: dict[int, str] = {}

    # Get sorted Konsole session IDs and their shell PIDs
    result = subprocess.run(
        ["qdbus", YAKUAKE_SERVICE], capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        return {}

    konsole_ids = sorted(
        int(line.split("/")[-1])
        for line in result.stdout.strip().splitlines()
        if line.startswith("/Sessions/") and line != "/Sessions"
        and line.split("/")[-1].isdigit()
    )

    konsole_shells: dict[int, int] = {}
    for kid in konsole_ids:
        spid = _qdbus(f"/Sessions/{kid}", "org.kde.konsole.Session.processId")
        if spid:
            try:
                konsole_shells[kid] = int(spid)
            except ValueError:
                pass

    # Get sorted Yakuake terminal IDs
    term_list = _qdbus_call("/yakuake/sessions", "org.kde.yakuake.terminalIdList")
    if not term_list:
        return {}

    terminal_ids = sorted(int(t.strip()) for t in term_list.split(",") if t.strip().isdigit())

    # Build terminal ID → tab title
    term_to_title: dict[int, str] = {}
    for tid in terminal_ids:
        ysid = _qdbus_call("/yakuake/sessions", "org.kde.yakuake.sessionIdForTerminalId", str(tid))
        if ysid and ysid != "-1":
            title = _qdbus_call("/yakuake/tabs", "org.kde.yakuake.tabTitle", ysid)
            if title:
                term_to_title[tid] = title

    if len(konsole_ids) == len(terminal_ids):
        # Sorted positional match — reliable when counts are equal
        for kid, tid in zip(konsole_ids, terminal_ids):
            if kid in konsole_shells and tid in term_to_title:
                shell_to_title[konsole_shells[kid]] = term_to_title[tid]
    else:
        # Fallback: only use direct matches (terminal ID == Konsole session ID)
        for kid in konsole_ids:
            if kid in term_to_title and kid in konsole_shells:
                shell_to_title[konsole_shells[kid]] = term_to_title[kid]

    return shell_to_title


def _find_tab_title(pid: int, shell_to_title: dict[int, str], ppid_map: dict[int, int]) -> str:
    """Walk up the process tree to find a shell PID with a known tab title."""
    visited = set()
    current = pid
    while current and current > 1 and current not in visited:
        visited.add(current)
        if current in shell_to_title:
            return shell_to_title[current]
        current = ppid_map.get(current)
    return ""


def discover_sessions() -> list[DiscoveredSession]:
    if not SESSIONS_DIR.is_dir():
        return []

    shell_to_title = _build_shell_to_tab_title()
    # Tab titles only exist on KDE/Yakuake; skip the process-table snapshot
    # entirely when there's nothing to match (e.g. macOS).
    ppid_map = _ppid_map() if shell_to_title else {}
    sessions = []

    for path in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        pid = data.get("pid")
        session_id = data.get("sessionId")
        if not pid or not session_id:
            continue

        if not _is_pid_alive(pid):
            continue

        tab_title = _find_tab_title(pid, shell_to_title, ppid_map)

        sessions.append(DiscoveredSession(
            session_id=session_id,
            pid=pid,
            cwd=data.get("cwd", ""),
            started_at=data.get("startedAt", 0),
            tab_title=tab_title,
        ))

    return sessions


def assign_names(sessions: list[DiscoveredSession]) -> dict[str, str]:
    names: dict[str, str] = {}
    name_counts: dict[str, int] = {}

    for s in sorted(sessions, key=lambda s: s.started_at):
        if s.tab_title and s.tab_title.lower() not in ("default", "shell", "bash", "zsh"):
            base = s.tab_title.lower().replace(" ", "-")
        else:
            base = _name_from_path(s.cwd)

        count = name_counts.get(base, 0) + 1
        name_counts[base] = count
        names[s.session_id] = base if count == 1 else f"{base}-{count}"

    return names
