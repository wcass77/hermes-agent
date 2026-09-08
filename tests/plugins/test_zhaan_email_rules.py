from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


rules = importlib.import_module("plugins.zhaan_orchestration.email_rules")
processor_mod = importlib.import_module("plugins.zhaan_orchestration.processor")


def write_rule(
    root: Path,
    *,
    rule_id: str = "brearley-bytes",
    priority: int = 100,
    sender: str = "brearley@myschoolemails.com",
    subject: str = r"^Brearley Bytes(?:[—-].*)?$",
) -> Path:
    path = root / "email-intake" / "rules" / f"{rule_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
schema_version: 1
id: {rule_id}
name: Brearley Bytes weekly newsletter
enabled: true
priority: {priority}
match:
  original_from:
    addresses:
      - {sender}
  original_subject:
    regex: '{subject}'
  original_to:
    addresses:
      - wcass77@gmail.com
    required: false
source: brearley-bytes-email
people:
  - rosie
reply:
  include_rule_link: true
---

Ignore middle and upper school events. Follow useful links.
""",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("prefix", ["", "Fwd: ", "FW: ", "Re: Fwd: "])
def test_plain_gmail_forward_matches_normalized_subject(tmp_path, prefix):
    path = write_rule(tmp_path)
    message = {
        "from": "Willy Cass <wcass77@gmail.com>",
        "subject": "outer transport subject",
        "text": (
            "---------- Forwarded message ---------\n"
            "From: Brearley Communications Office <brearley@myschoolemails.com>\n"
            "Date: Sun, Sep 6, 2026 at 7:05 AM\n"
            f"Subject: {prefix}Brearley Bytes—September 6, 2026\n"
            "To: Richard Cass <wcass77@gmail.com>\n\nNewsletter body"
        ),
    }
    decision = rules.select_rule(message, path.parent)
    assert decision.status == "matched"
    assert decision.rule.id == "brearley-bytes"
    assert decision.metadata.sender == "brearley@myschoolemails.com"
    assert decision.metadata.subject == "Brearley Bytes—September 6, 2026"
    assert decision.metadata.recipient == "wcass77@gmail.com"


def test_html_and_structured_forwards_are_supported(tmp_path):
    path = write_rule(tmp_path)
    html_message = {
        "html": (
            '<div class="gmail_quote"><div class="gmail_attr">'
            "---------- Forwarded message ---------<br>"
            "From: Brearley &lt;brearley@myschoolemails.com&gt;<br>"
            "Date: Sunday<br>Subject: Brearley Bytes—September 6, 2026<br>"
            "To: Willy &lt;wcass77@gmail.com&gt;</div></div>"
        )
    }
    assert rules.select_rule(html_message, path.parent).status == "matched"

    structured = {
        "forwarded_message": {
            "from": "Brearley <brearley@myschoolemails.com>",
            "to": "wcass77@gmail.com",
            "subject": "Fwd: Brearley Bytes—September 6, 2026",
        }
    }
    assert rules.select_rule(structured, path.parent).status == "matched"


def test_outer_headers_and_arbitrary_prose_cannot_manufacture_match(tmp_path):
    path = write_rule(tmp_path)
    message = {
        "from": "Brearley <brearley@myschoolemails.com>",
        "subject": "Brearley Bytes—September 6, 2026",
        "text": "Someone wrote From: brearley@myschoolemails.com in ordinary prose.",
    }
    decision = rules.select_rule(message, path.parent)
    assert decision.status == "unparseable"
    assert decision.rule is None


def test_unmatched_recipient_and_ambiguous_priority_use_generic_path(tmp_path):
    first = write_rule(tmp_path)
    message = {
        "text": (
            "Begin forwarded message:\n"
            "From: Brearley <brearley@myschoolemails.com>\n"
            "Date: Sunday\n"
            "Subject: Brearley Bytes—September 6, 2026\n"
            "To: other@example.com\n"
        )
    }
    assert rules.select_rule(message, first.parent).status == "unmatched"

    message["text"] = message["text"].replace("other@example.com", "wcass77@gmail.com")
    write_rule(tmp_path, rule_id="brearley-bytes-tie", priority=100)
    decision = rules.select_rule(message, first.parent)
    assert decision.status == "ambiguous"
    assert decision.rule is None
    assert decision.detail == "brearley-bytes, brearley-bytes-tie"


def test_invalid_rule_is_skipped_without_blocking_valid_match(tmp_path, caplog):
    valid = write_rule(tmp_path)
    invalid = valid.parent / "invalid.md"
    invalid.write_text("---\nschema_version: 1\nid: invalid\n---\nbody\n")
    message = {
        "text": (
            "---------- Forwarded message ---------\n"
            "From: brearley@myschoolemails.com\nDate: Sunday\n"
            "Subject: Brearley Bytes—September 6, 2026\nTo: wcass77@gmail.com\n"
        )
    }
    decision = rules.select_rule(message, valid.parent)
    assert decision.status == "matched"
    assert "Skipping invalid Zhaan email intake rule" in caplog.text


@pytest.mark.parametrize(
    "replacement",
    [
        "enabled: sometimes",
        "reply:\n  include_rule_link: false",
        "reply:\n  include_rule_link: true\nunknown: unsafe",
    ],
)
def test_runtime_loader_rejects_rules_that_violate_schema_contract(tmp_path, replacement):
    path = write_rule(tmp_path)
    text = path.read_text(encoding="utf-8")
    if replacement.startswith("enabled:"):
        text = text.replace("enabled: true", replacement)
    elif replacement.startswith("reply:"):
        text = text.replace("reply:\n  include_rule_link: true", replacement)
    path.write_text(text, encoding="utf-8")

    assert rules.load_rules(path.parent) == []


def test_processor_injects_rule_before_email_and_appends_canonical_footer(tmp_path, monkeypatch):
    rule_path = write_rule(tmp_path)
    message = {
        "inbox_id": "inbox", "thread_id": "thread", "message_id": "message",
        "from": "Willy <wcass77@gmail.com>", "subject": "Fwd: newsletter",
        "text": (
            "---------- Forwarded message ---------\n"
            "From: Brearley <brearley@myschoolemails.com>\nDate: Sunday\n"
            "Subject: Brearley Bytes—September 6, 2026\nTo: wcass77@gmail.com\n\nBody"
        ),
        "attachments": [],
    }

    class Client:
        def __init__(self):
            self.replies = []

        def get_message(self, *_args):
            return message

        def reply(self, *args, **kwargs):
            self.replies.append((args, kwargs))

    class DB:
        def resolve_session_id(self, _session_id):
            return True

    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="Interesting events summary.", stderr="")

    client = Client()
    monkeypatch.setattr(processor_mod, "SessionDB", DB)
    monkeypatch.setattr(processor_mod.subprocess, "run", run)
    service = processor_mod.Processor(
        client, tmp_path, hermes_command="/bin/hermes",
        rule_repository_url="https://github.com/example/family-logistics",
    )
    service({"event_id": "event", "inbox_id": "inbox", "message_id": "message"}, "session")

    prompt = captured["command"][captured["command"].index("-q") + 1]
    assert prompt.index("TRUSTED_EMAIL_INTAKE_ROUTING") < prompt.index("UNTRUSTED_EMAIL_CONTENT")
    assert '"id": "brearley-bytes"' in prompt
    assert "Ignore middle and upper school events" in prompt
    reply = client.replies[0][0][2]
    assert reply.startswith("Interesting events summary.")
    assert "Processing rule: Brearley Bytes weekly newsletter" in reply
    assert (
        "https://github.com/example/family-logistics/blob/main/"
        "email-intake/rules/brearley-bytes.md"
    ) in reply
    assert rule_path.is_file()


def test_generic_processor_footer_is_explicit(tmp_path):
    service = processor_mod.Processor(object(), tmp_path)
    decision = rules.RuleDecision(metadata=None, rule=None, status="unparseable")
    assert service._routing_footer(decision) == "Processing rule: none (generic email intake)"


def test_fingerprint_converges_across_forwarders_and_recipient_tracking():
    def forwarded(forwarder, recipient, token):
        return {
            "from": forwarder,
            "subject": "Fwd: Brearley Bytes",
            "text": (
                f"FYI from {forwarder}\n\n---------- Forwarded message ---------\n"
                "From: Brearley <brearley@myschoolemails.com>\n"
                "Date: Sunday\nSubject: Brearley Bytes—September 6, 2026\n"
                f"To: {recipient}\n\nNewsletter body\n"
                f"https://school.example/events?recipient={token}\n"
            ),
            "attachments": [],
        }

    willy = forwarded("Willy <wcass77@gmail.com>", "wcass77@gmail.com", "willy")
    wife = forwarded("Wife <wife@example.com>", "wife@example.com", "wife")

    assert rules.canonical_email_fingerprint(willy) == rules.canonical_email_fingerprint(wife)
    wife["text"] = wife["text"].replace("Newsletter body", "Different issue")
    assert rules.canonical_email_fingerprint(willy) != rules.canonical_email_fingerprint(wife)
