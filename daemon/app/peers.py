"""Tailscale-aware peer discovery for the cross-host mesh.

The daemon learns its own canonical hostname (the first DNS label from
`tailscale status --json`) and resolves peers' Tailscale IPs the same way.
Peers are listed by hostname in `peers.json`; URLs are derived at runtime so
IP changes (rare on Tailscale, but possible) don't require config edits.

If Tailscale isn't installed or the daemon isn't logged in, this module
degrades gracefully: self_host is `None`, peers is empty, the daemon stays
single-host and no FQDN forwarding happens.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PEERS_FILE = Path(
    os.environ.get(
        "SESSION_BRIDGE_PEERS_FILE",
        str(Path.home() / ".config" / "session-bridge" / "peers.json"),
    )
)
DEFAULT_PEER_PORT = 8910

# On hosts without the tailscale CLI (e.g. Termux on Android, where Tailscale
# runs as a system VPN), set SESSION_BRIDGE_HOST_NAME to the canonical name
# the rest of the mesh should use.
SELF_HOST_OVERRIDE = os.environ.get("SESSION_BRIDGE_HOST_NAME") or None


@dataclass
class Peer:
    host: str  # canonical short hostname, e.g. "pixel-7-pro"
    ip: str  # Tailscale IP, e.g. "100.74.17.91"
    port: int = DEFAULT_PEER_PORT
    capabilities: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"http://{self.ip}:{self.port}"


@functools.lru_cache(maxsize=1)
def _run_tailscale_status() -> dict | None:
    """Cached subprocess call. get_self_host() and load_peers() both need
    the result; caching avoids running the CLI twice per request path."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _canonical_host(dns_name: str) -> str:
    """First label of a Tailscale MagicDNS name. 'foo.tailX.ts.net.' → 'foo'."""
    return dns_name.split(".")[0]


def get_self_host() -> str | None:
    """Return the canonical Tailscale hostname for this machine, or None.

    Honors SESSION_BRIDGE_HOST_NAME first so hosts without the tailscale CLI
    (Android/Termux) can still join the mesh.
    """
    if SELF_HOST_OVERRIDE:
        return SELF_HOST_OVERRIDE
    status = _run_tailscale_status()
    if not status or "Self" not in status:
        return None
    return _canonical_host(status["Self"].get("DNSName", "")) or None


def get_self_ip() -> str | None:
    """Return the Tailscale IPv4 address for this machine, or None.

    Used to bind the daemon so peers can reach it without exposing 0.0.0.0
    on whatever local network the host is attached to.
    """
    status = _run_tailscale_status()
    if not status or "Self" not in status:
        return None
    for ip in status["Self"].get("TailscaleIPs", []):
        if ":" not in ip:  # filter out IPv6
            return ip
    return None


def _index_peer_ips(status: dict) -> dict[str, str]:
    """Map canonical hostname → Tailscale IPv4 for every peer in the tailnet."""
    out: dict[str, str] = {}
    for entry in (status or {}).get("Peer", {}).values():
        canonical = _canonical_host(entry.get("DNSName", ""))
        if not canonical:
            continue
        for ip in entry.get("TailscaleIPs", []):
            if ":" not in ip:
                out[canonical] = ip
                break
    return out


def _load_config(path: Path) -> dict:
    """Read peers.json once and return the parsed config (or {} on miss)."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_self_capabilities(path: Path = DEFAULT_PEERS_FILE) -> list[str]:
    """Capabilities this host advertises. Read from the `self` block in peers.json.

    Used by the resolver and /hosts endpoint to know what local hardware /
    services the host claims to provide. Missing block or missing field
    means the host advertises nothing — it's a routing participant only.
    """
    config = _load_config(path)
    self_block = config.get("self") or {}
    caps = self_block.get("capabilities") or []
    return list(caps)


def load_peers(path: Path = DEFAULT_PEERS_FILE) -> list[Peer]:
    """Read peers.json and resolve each named host to its Tailscale IP.

    File format (every field except `host` is optional):
      {"peers": ["pixel-7-pro"]}
      {"peers": [{"host": "pixel-7-pro", "port": 8911}]}
      {"peers": [{"host": "local-yocal", "ip": "100.99.44.89"}]}
      {"peers": [{"host": "pixel-7-pro", "capabilities": ["sms-send", "gps"]}]}

    If `ip` is given, it's used verbatim — needed on hosts that can't run
    `tailscale status` (e.g. Termux). Otherwise the IP is resolved via
    Tailscale and the entry is silently skipped if the host isn't in the
    tailnet right now (peers can come and go).
    """
    config = _load_config(path)
    raw_peers = config.get("peers", [])
    status = _run_tailscale_status()
    ip_index = _index_peer_ips(status) if status else {}

    peers: list[Peer] = []
    for raw in raw_peers:
        if isinstance(raw, str):
            host, port, ip, caps = raw, DEFAULT_PEER_PORT, None, []
        elif isinstance(raw, dict):
            host = raw.get("host", "")
            port = int(raw.get("port", DEFAULT_PEER_PORT))
            ip = raw.get("ip") or None
            caps = list(raw.get("capabilities") or [])
        else:
            continue
        if not host:
            continue
        ip = ip or ip_index.get(host)
        if not ip:
            continue
        peers.append(Peer(host=host, ip=ip, port=port, capabilities=caps))
    return peers
