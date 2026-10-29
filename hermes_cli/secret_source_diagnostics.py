"""Bounded, value-free source outcomes, including loads before worker adoption."""
import re
import threading
from collections import OrderedDict, deque
from copy import deepcopy
from pathlib import Path

_HISTORY = OrderedDict()
_LOCK = threading.RLock()
_OUTCOMES = frozenset({"cached", "config_error", "no_config", "hydration_error",
                       "fetched", "no_sources", "registry_unavailable"})


def observe_hydration(home, outcome, report=None):
    """Project reports onto an allowlist; free-form errors, labels and values never escape."""
    from agent.secret_sources.base import ErrorKind

    sources = []
    for source in (report.sources if report is not None else ())[:32]:
        name = source.name
        name = name if isinstance(name, str) and re.fullmatch(r"[a-z0-9_]{1,64}", name) else "invalid"
        result = source.result
        category = "success" if result.ok else (
            result.error_kind.value if isinstance(result.error_kind, ErrorKind) else "unknown")
        sources.append({"source": name, "category": category})
    home_key = str(Path(home).resolve())
    event = {"home": home_key, "outcome": outcome if outcome in _OUTCOMES else "unknown", "sources": sources}
    with _LOCK:
        history = _HISTORY.setdefault(home_key, deque(maxlen=32))
        # Import-time cache reads must not erase the fetch that established the cache.
        if not history or history[-1] != event:
            history.append(event)
        _HISTORY.move_to_end(home_key)
        while len(_HISTORY) > 128:
            _HISTORY.popitem(last=False)


def hydration_observations(home):
    """Return this home's safe outcomes; never another profile's report or credentials."""
    with _LOCK:
        return deepcopy(list(_HISTORY.get(str(Path(home).resolve()), ())))
