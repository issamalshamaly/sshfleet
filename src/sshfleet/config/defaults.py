"""Built-in defaults.

The tool ships with no knowledge of any particular fleet. What runs on a host is discovered from
its container runtime, and what the operator wants watched is written per host under ``hosts:``.
What lives here are the generic knobs and their defaults.
"""

from __future__ import annotations

from typing import Any

from .schema import HostRole

#: Roles drive colouring and which hosts offer nginx log sources.
ROLES: tuple[str, ...] = tuple(r.value for r in HostRole)

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 3,
    "projects": [],
    "environments": [],
    "probes": {"discovered_path": "/"},
    "frontends": {"web_root": "/var/www/html"},
    "hosts": {},
    "refresh": {
        "fast_s": 120,
        "stale_after_s": 300,
        "collector_timeout_s": 25,
        "connect_timeout_s": 10,
    },
    "concurrency": {"global": 16, "per_bastion": 8, "heavy_logs_per_bastion": 6, "heavy_logs_global": 10},
    "thresholds": {
        "disk_warn_pct": 80,
        "disk_crit_pct": 90,
        "mem_warn_pct": 85,
        "mem_crit_pct": 95,
        "load_per_cpu_warn": 1.5,
        "container_log_warn_bytes": 2147483648,
        "restart_count_warn": 1,
        "vanished_memory_s": 21600,
    },
    "ssh": {
        "control_dir": "auto",
        "control_persist": "10m",
        "connect_timeout_s": 8,
        "server_alive_interval_s": 15,
        "strict_host_key_checking": "accept-new",
        "compression": True,
        "include_user_config": False,
    },
    "tunnels": {},
    "files": {
        "download_dir": "auto",
        "deny_extra": [],
        "preview_max_bytes": 65536,
    },
    "ui": {
        "open_ssh": "auto",
        # Browser sounds per event; names are the built-in patterns of the web interface.
        "sounds": {"enabled": False, "crit": "alarm", "warn": "chime", "done": "soft", "desktop": False},
    },
    "logs": {
        "default_before": 60,
        "default_after": 0,
        "default_window": "30m",
        "tail_lines": 50000,
        "max_bytes": 20971520,
        "max_line_bytes": 16384,
        "max_file_bytes": 209715200,
        "display_tz": "local",
        "timeout_s": 600,
        "follow_max_sources": 12,
        "allowed_paths": [],
        "seq": {},
        "elastic": {},
    },
}
