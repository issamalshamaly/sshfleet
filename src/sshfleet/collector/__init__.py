"""Remote data collection: one script, one round-trip, one snapshot per host."""

from .alerts import evaluate, transitions
from .model import Alert, ContainerInfo, DiskInfo, HostSnapshot, ProbeResult, Severity
from .parse import parse_collect
from .remote import collect, probe_specs, script, unreachable

__all__ = [
    "Alert",
    "ContainerInfo",
    "DiskInfo",
    "HostSnapshot",
    "ProbeResult",
    "Severity",
    "collect",
    "evaluate",
    "parse_collect",
    "probe_specs",
    "script",
    "transitions",
    "unreachable",
]
