"""Profile-scoped, DNS-bound exceptions for a configured loopback CDP browser.

This is a browser-tool URL policy, not page-script/network egress isolation.
The global URL classifier remains unchanged for web/terminal/other consumers.
"""

import ipaddress
import re
import socket
from datetime import date
from urllib.parse import parse_qsl, unquote, urlsplit

from tools import browser_tool_cdp as cdp
from tools.browser_tool_origin import origin_module


def _canonical_path(path: str) -> str | None:
    """Accept encoded filenames but reject ambiguous separators and traversal."""
    if re.search(r"%(?![0-9a-fA-F]{2})|%2f|%5c", path, re.I):
        return None
    decoded = unquote(path, errors="strict")
    if any(c in decoded for c in ("%", "\\")) or "//" in decoded:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in decoded):
        return None
    if any(part in (".", "..") for part in decoded.split("/")):
        return None
    return decoded or "/"


def _matches_paths(path: str, paths: list) -> bool:
    for prefix in paths:
        if not isinstance(prefix, str) or not prefix.startswith("/"):
            return False
        if _canonical_path(prefix) != prefix:
            return False
    return any(path == prefix or (prefix != "/" and prefix.endswith("/")
               and (path == prefix[:-1] or path.startswith(prefix))) for prefix in paths)


def _bound_loopback_browser(endpoint: str) -> bool:
    parsed = urlsplit(endpoint)
    return (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
            and parsed.port is not None and parsed.username is None
            and parsed.password is None and parsed.path in ("", "/")
            and not parsed.query and not parsed.fragment
            and cdp._get_cdp_override_raw() == endpoint)


def _dns_matches(host: str, addresses: list) -> bool:
    expected = {str(ipaddress.ip_address(value)) for value in addresses}
    actual = {str(ipaddress.ip_address(answer[4][0]))
              for answer in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    return bool(actual) and actual <= expected


def private_url_verdict(url: str) -> bool | None:
    """True: explicit grant; False: configured host refused; None: ordinary URL.

    A configured host stays denied on DNS drift even if it resolves publicly.
    Read on every call through the existing profile-aware config reader; no
    global cache or launch-profile environment policy can leak across profiles.
    """
    bt = origin_module()
    rule = bt._browser_cfg("private_url_exceptions", {}, lambda v: v,
                           "browser.private_url_exceptions")
    if not isinstance(rule, dict) or not rule.get("host"):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.hostname != rule["host"]:
            return None
        return _matches_rule(parsed, url, rule)
    except (ValueError, TypeError, UnicodeError, OSError):
        return False


def _matches_rule(parsed, url: str, rule: dict) -> bool:
    if (parsed.scheme != "https" or parsed.port not in (None, 443)
            or parsed.username is not None or parsed.password is not None
            or any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in url)):
        return False
    paths, addresses = rule.get("paths"), rule.get("addresses")
    if not isinstance(paths, list) or not isinstance(addresses, list) or not addresses:
        return False
    path = _canonical_path(parsed.path)
    return (path is not None and _matches_paths(path, paths)
            and _valid_hours_selector(path, parsed.query)
            and isinstance(rule.get("cdp_url"), str)
            and _bound_loopback_browser(rule["cdp_url"])
            and _dns_matches(parsed.hostname, addresses))


def _valid_hours_selector(path: str, query: str) -> bool:
    if path.rstrip("/") not in ("/hours", "/timecard") or not query:
        return True
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    return (len(pairs) == 1 and pairs[0][0] == "period"
            and date.fromisoformat(pairs[0][1]).isoformat() == pairs[0][1])
