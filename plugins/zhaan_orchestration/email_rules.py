from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

logger = logging.getLogger(__name__)

_FORWARD_MARKER = re.compile(
    r"(?im)^\s*(?:-{2,}\s*forwarded message\s*-{2,}|begin forwarded message:?)\s*$"
)
_HEADER = re.compile(r"(?im)^\s*(from|to|subject)\s*:\s*(.+?)\s*$")
_SUBJECT_PREFIX = re.compile(r"(?i)^\s*(?:re|fw|fwd)\s*:\s*")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, _attrs) -> None:
        if tag in {"br", "div", "p", "tr", "li", "blockquote"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"div", "p", "tr", "li", "blockquote"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return html.unescape("".join(self.parts))


@dataclass(frozen=True)
class ForwardedMetadata:
    sender: str
    subject: str
    recipient: str | None = None


@dataclass(frozen=True)
class EmailRule:
    id: str
    name: str
    priority: int
    sender_addresses: frozenset[str]
    subject_regex: re.Pattern[str]
    recipient_addresses: frozenset[str]
    recipient_required: bool
    instructions: str
    path: Path


@dataclass(frozen=True)
class RuleDecision:
    metadata: ForwardedMetadata | None
    rule: EmailRule | None
    status: str
    detail: str = ""


def normalize_subject(value: str) -> str:
    result = str(value or "").strip()
    while True:
        stripped = _SUBJECT_PREFIX.sub("", result, count=1)
        if stripped == result:
            return re.sub(r"\s+", " ", result).strip()
        result = stripped


def _address(value: Any) -> str:
    return parseaddr(str(value or ""))[1].casefold()


def _structured_forward(message: dict[str, Any]) -> ForwardedMetadata | None:
    """Read only explicitly forwarded/original nested metadata, never outer headers."""
    for key in ("forwarded_message", "forwarded", "original_message"):
        candidate = message.get(key)
        if not isinstance(candidate, dict):
            continue
        sender = _address(candidate.get("from") or candidate.get("sender"))
        subject = normalize_subject(str(candidate.get("subject") or ""))
        recipient = _address(candidate.get("to") or candidate.get("recipient")) or None
        if sender and subject:
            return ForwardedMetadata(sender=sender, subject=subject, recipient=recipient)
    return None


def _plain_text_from_html(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text()


def _header_block(text: str) -> ForwardedMetadata | None:
    marker = _FORWARD_MARKER.search(text)
    if marker:
        candidate = text[marker.end():]
    else:
        # Outlook-style forwards may omit a marker but put From/Sent/To/Subject
        # together. Restrict the fallback to the beginning of a quoted block so
        # arbitrary prose cannot manufacture a match.
        blocks = re.split(r"\n\s*\n", text)
        candidate = next(
            (
                block for block in blocks
                if re.search(r"(?im)^\s*from\s*:", block)
                and re.search(r"(?im)^\s*to\s*:", block)
                and re.search(r"(?im)^\s*subject\s*:", block)
                and re.search(r"(?im)^\s*(?:sent|date)\s*:", block)
            ),
            "",
        )
    if not candidate:
        return None
    headers: dict[str, str] = {}
    for match in _HEADER.finditer("\n".join(candidate.splitlines()[:40])):
        headers.setdefault(match.group(1).casefold(), match.group(2).strip())
    sender = _address(headers.get("from"))
    subject = normalize_subject(headers.get("subject", ""))
    recipient = _address(headers.get("to")) or None
    if not sender or not subject:
        return None
    return ForwardedMetadata(sender=sender, subject=subject, recipient=recipient)


def extract_forwarded_metadata(message: dict[str, Any]) -> ForwardedMetadata | None:
    structured = _structured_forward(message)
    if structured:
        return structured
    candidates = [message.get("text"), message.get("extracted_text")]
    if message.get("html"):
        candidates.append(_plain_text_from_html(str(message["html"])))
    for value in candidates:
        if value and (metadata := _header_block(str(value))):
            return metadata
    return None


def _frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing opening YAML front matter delimiter")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError("missing closing YAML front matter delimiter") from exc
    metadata = yaml.safe_load("\n".join(lines[1:end]))
    if not isinstance(metadata, dict):
        raise ValueError("YAML front matter must be a mapping")
    body = "\n".join(lines[end + 1:]).strip()
    if not body:
        raise ValueError("instruction body is empty")
    return metadata, body


def load_rule(path: Path) -> EmailRule:
    metadata, instructions = _frontmatter(path)
    if metadata.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    match = metadata.get("match")
    if not isinstance(match, dict):
        raise ValueError("match must be a mapping")
    sender_cfg = match.get("original_from")
    subject_cfg = match.get("original_subject")
    if not isinstance(sender_cfg, dict) or not isinstance(subject_cfg, dict):
        raise ValueError("match requires original_from and original_subject mappings")
    senders = frozenset(_address(value) for value in sender_cfg.get("addresses", []))
    if not senders or "" in senders:
        raise ValueError("original_from.addresses must contain valid mailbox addresses")
    pattern = subject_cfg.get("regex")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("original_subject.regex must be a non-empty string")
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"invalid original_subject.regex: {exc}") from exc
    recipient_cfg = match.get("original_to") or {}
    if not isinstance(recipient_cfg, dict):
        raise ValueError("original_to must be a mapping")
    recipients = frozenset(_address(value) for value in recipient_cfg.get("addresses", []))
    if "" in recipients:
        raise ValueError("original_to.addresses contains an invalid mailbox address")
    rule_id = metadata.get("id")
    name = metadata.get("name")
    priority = metadata.get("priority")
    if not isinstance(rule_id, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", rule_id):
        raise ValueError("id must be a lowercase kebab-case stable ID")
    if path.stem != rule_id:
        raise ValueError("filename must match id")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ValueError("priority must be an integer")
    return EmailRule(
        id=rule_id,
        name=name.strip(),
        priority=priority,
        sender_addresses=senders,
        subject_regex=compiled,
        recipient_addresses=recipients,
        recipient_required=bool(recipient_cfg.get("required", False)),
        instructions=instructions,
        path=path,
    )


def load_rules(directory: Path) -> list[EmailRule]:
    rules: list[EmailRule] = []
    for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
        try:
            metadata, _ = _frontmatter(path)
            if metadata.get("enabled") is False:
                continue
            rules.append(load_rule(path))
        except Exception as exc:
            logger.error("Skipping invalid Zhaan email intake rule %s: %s", path, exc)
    return rules


def _matches(rule: EmailRule, metadata: ForwardedMetadata) -> bool:
    if metadata.sender not in rule.sender_addresses:
        return False
    if rule.subject_regex.fullmatch(metadata.subject) is None:
        return False
    if rule.recipient_required and not metadata.recipient:
        return False
    if metadata.recipient and rule.recipient_addresses and metadata.recipient not in rule.recipient_addresses:
        return False
    return True


def select_rule(message: dict[str, Any], directory: Path) -> RuleDecision:
    metadata = extract_forwarded_metadata(message)
    if metadata is None:
        return RuleDecision(metadata=None, rule=None, status="unparseable")
    matches = [rule for rule in load_rules(directory) if _matches(rule, metadata)]
    if not matches:
        return RuleDecision(metadata=metadata, rule=None, status="unmatched")
    priority = max(rule.priority for rule in matches)
    winners = [rule for rule in matches if rule.priority == priority]
    if len(winners) != 1:
        return RuleDecision(
            metadata=metadata,
            rule=None,
            status="ambiguous",
            detail=", ".join(sorted(rule.id for rule in winners)),
        )
    return RuleDecision(metadata=metadata, rule=winners[0], status="matched")


def rule_url(rule: EmailRule, workspace: Path, repository_url: str) -> str:
    relative = rule.path.resolve().relative_to(workspace.resolve())
    encoded = "/".join(quote(part, safe="") for part in relative.parts)
    return f"{repository_url.rstrip('/')}/blob/main/{encoded}"
