"""New memory is a sanitized derivative; history and frozen prompts are untouched."""

import json

import pytest

from tools.memory_tool import MemoryStore, get_memory_dir


FIXTURE = "sk-proj-" + "SyntheticFixture123" * 3


@pytest.mark.parametrize("operation", ["add", "patch", "replace", "batch", "batch-patch"])
def test_memory_creation_sanitizes_all_mutation_routes(operation):
    store = MemoryStore(memory_char_limit=5000)
    store.load_from_disk()
    assert store.add("memory", "useful ordinary note")["success"]
    frozen = store.format_for_system_prompt("memory")
    operations = {
        "add": lambda: store.add("memory", "fixture " + FIXTURE),
        "patch": lambda: store.patch("memory", "ordinary", FIXTURE),
        "replace": lambda: store.replace("memory", "ordinary", "fixture " + FIXTURE),
        "batch": lambda: store.apply_batch("memory", [{"action": "add", "content": "fixture " + FIXTURE}]),
        "batch-patch": lambda: store.apply_batch("memory", [
            {"action": "patch", "old_text": "ordinary", "content": FIXTURE}]),
    }
    result = operations[operation]()
    assert result["success"] and result["redacted"]
    assert FIXTURE not in json.dumps(result)
    assert FIXTURE not in (get_memory_dir() / "MEMORY.md").read_text()
    assert store.format_for_system_prompt("memory") == frozen


def test_sanitized_memory_respects_character_limit():
    from agent.redact import register_vault_redaction_value, clear_vault_redaction_values
    register_vault_redaction_value("tiny")
    try:
        store = MemoryStore(memory_char_limit=10)
        store.load_from_disk()
        result = store.add("memory", "tiny")
        assert not result["success"]
        assert store.memory_entries == []
        assert not (get_memory_dir() / "MEMORY.md").exists()
    finally:
        clear_vault_redaction_values()
