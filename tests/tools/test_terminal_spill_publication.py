"""Overflow handles must name a readable, redacted file in the task environment."""

from pathlib import Path
import json
import os
import stat
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from tools.terminal_tool_result import finalize_foreground_result


def finish_spill(path, env, **extra):
    return json.loads(finalize_foreground_result(
        command="printf synthetic-output", result={
            "output": "head ... tail", "returncode": 7,
            "full_output_path": str(path), "output_total_chars": 60_000, **extra,
        }, env=env, env_type="docker", effective_task_id="spill-test",
        task_id="spill-test", session_id=None, session_key="spill-test",
        workdir=None, command_cwd=None, approval_note=None,
    ))


@pytest.mark.parametrize("failed_copy", [False, True])
def test_foreground_handle_is_published_only_after_redaction(tmp_path, failed_copy):
    fake = "sk-proj-" + "SyntheticFixture123" * 4
    host = tmp_path / "host.log"
    host.write_text("head\n" + fake + "\nmiddle\ntail\n")
    sandbox = tmp_path / "sandbox.log"

    class Environment:
        def publish_output_file(self, path):
            text = Path(path).read_text()
            assert fake not in text
            if failed_copy:
                raise OSError("synthetic publication failure")
            sandbox.write_text(text)
            return str(sandbox)

    result = finish_spill(host, Environment())
    assert result["exit_code"] == 7
    assert result["output"] == "head ... tail"
    if failed_copy:
        assert "full_output_path" not in result
        assert "unavailable" in result["truncation_note"]
    else:
        assert result["full_output_path"] == str(sandbox)
        assert "middle" in sandbox.read_text()
        assert fake not in sandbox.read_text()


def test_saved_json_excludes_the_shell_cwd_protocol(tmp_path):
    marker = "__HERMES_CWD_synthetic__"
    host = tmp_path / "host.log"
    data = {"payload": "x" * 60_000, "marker": "middle"}
    host.write_text(json.dumps(data) + f"\n\n{marker}/workspace{marker}\n")

    class Environment:
        _cwd_marker = marker

    result = finish_spill(host, Environment())
    assert json.loads(Path(result["full_output_path"]).read_text()) == data


def test_capped_capture_is_not_described_as_full_output(tmp_path):
    from tools.environments.base_output import _BoundedOutputCollector, _finalize_wait_result
    host = tmp_path / "host.log"
    collector = _BoundedOutputCollector(10, spill_path=host)
    collector._SPILL_CAP_CHARS = 80
    collector.append("x" * 40)
    collector.append("y" * 100)
    captured = _finalize_wait_result(collector, collector.render(), 7)
    result = finish_spill(host, object(), **{
        "output_capture_complete": captured["output_capture_complete"],
    })
    assert result["output_capture_complete"] is False
    assert "incomplete" in result["truncation_note"]
    assert "Full output" not in result["truncation_note"]


def publish(directory, name, data=b"synthetic capture"):
    from tools.environments.docker_output import _PUBLISH_SCRIPT
    return subprocess.run(
        [sys.executable, "-I", "-c", _PUBLISH_SCRIPT, str(directory), name],
        input=data, capture_output=True, timeout=10,
    )


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("obstacle", ["directory_link", "file_link", "public_directory", "invalid_name"])
def test_container_capture_refuses_unsafe_destinations(tmp_path, obstacle):
    directory = tmp_path / "captures"
    name = f"out-{uuid.uuid4().hex}.log"
    victim = tmp_path / "victim"
    victim.write_text("unchanged")
    if obstacle == "directory_link":
        directory.symlink_to(tmp_path, target_is_directory=True)
    elif obstacle == "invalid_name":
        name = "../victim"
    else:
        directory.mkdir(mode=0o700)
        if obstacle == "file_link":
            (directory / name).symlink_to(victim)
        else:
            directory.chmod(0o755)
    result = publish(directory, name)
    assert result.returncode != 0
    assert victim.read_text() == "unchanged"
    assert not result.stdout


@pytest.mark.platforms("linux")
def test_parallel_captures_roundtrip_long_unicode_lines_and_prune_only_old_captures(tmp_path):
    directory = tmp_path / "captures"
    directory.mkdir(mode=0o700)
    stale = directory / f"out-{uuid.uuid4().hex}.log"
    stale.write_text("old capture")
    unrelated = directory / "user.log"
    unrelated.write_text("keep")
    link = directory / f"out-{uuid.uuid4().hex}.log"
    link.symlink_to(unrelated)
    os.utime(stale, (1, 1))
    os.utime(unrelated, (1, 1))
    data = ("head" + "é" * 160_000 + "middle" + "x" * 160_000 + "tail").encode()

    def capture(_):
        result = publish(directory, f"out-{uuid.uuid4().hex}.log", data)
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        path = Path(receipt["path"])
        assert path.read_bytes() == data
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert receipt["bytes"] == len(data)
        return path

    with ThreadPoolExecutor(max_workers=3) as workers:
        paths = list(workers.map(capture, range(3)))
    assert len(set(paths)) == 3
    assert not stale.exists()
    assert unrelated.read_text() == "keep"
    assert link.is_symlink()
