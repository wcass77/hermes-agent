"""Publish already-redacted terminal captures inside their originating container."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import uuid


# Isolated Python avoids shell snapshots, cwd changes and another output collector.
# Payload bytes travel on stdin, never in a command argument or a host bind mount.
_PUBLISH_SCRIPT = r"""
import hashlib, json, os, re, stat, sys, tempfile, time
root = sys.argv[1] or os.path.join(tempfile.gettempdir(), 'hermes-terminal-output')
name = sys.argv[2]
if not re.fullmatch(r'out-[0-9a-f]{32}\.log', name):
    raise ValueError('invalid capture name')
try:
    os.mkdir(root, 0o700)
except FileExistsError:
    pass
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    info = os.fstat(directory)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError('capture directory is not private')
    cutoff = time.time() - 7 * 24 * 60 * 60
    for old in os.listdir(directory):
        if not re.fullmatch(r'out-[0-9a-f]{32}\.log', old):
            continue
        try:
            info = os.stat(old, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mtime < cutoff:
                os.unlink(old, dir_fd=directory)
        except FileNotFoundError:
            pass
    data = sys.stdin.buffer.read()
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, 'w+b') as capture:
            capture.write(data)
            capture.flush()
            capture.seek(0)
            saved = capture.read()
        if saved != data:
            raise OSError('capture readback mismatch')
    except BaseException:
        os.unlink(name, dir_fd=directory)
        raise
    print(json.dumps({'path': os.path.join(root, name), 'bytes': len(saved),
                      'sha256': hashlib.sha256(saved).hexdigest()}))
finally:
    os.close(directory)
"""


def publish_output_file(docker_exe: str, container_id: str, path: Path,
                        *, client_env: dict[str, str] | None = None) -> str:
    """Return a container path only after exact-byte readback has succeeded."""
    if not container_id:
        raise OSError("terminal container is unavailable")
    data = path.read_bytes()
    name = f"out-{uuid.uuid4().hex}.log"
    result = subprocess.run(
        [docker_exe, "exec", "-i", container_id, "python3", "-I", "-c",
         _PUBLISH_SCRIPT, "", name],
        input=data, capture_output=True, timeout=30, check=True, env=client_env,
    )
    receipt = json.loads(result.stdout)
    published = receipt["path"]
    if (receipt.get("bytes") != len(data)
            or receipt.get("sha256") != hashlib.sha256(data).hexdigest()
            or not isinstance(published, str) or not published.startswith("/")
            or Path(published).name != name):
        raise OSError("terminal capture publication was not verified")
    return published
