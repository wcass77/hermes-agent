"""Verify descriptor identity before using its Linux fdinfo lock records.

Btrfs can report a subvolume device through stat() and the superblock device in
/proc/locks. Matching inode numbers alone would confuse different subvolumes.
"""

import os
from pathlib import Path


def _parse_descriptor_locks(text: str, sidecar: str) -> list:
    from hermes_state_lockowners import parse_proc_locks

    found = []
    for line in text.splitlines():
        if not line.startswith("lock:"):
            continue
        payload = line.partition(":")[2].strip()
        fields = payload.split()
        if len(fields) < 8 or fields[1] == "->":
            continue
        try:
            major, minor, inode = fields[5].split(":")
            identity = (os.makedev(int(major, 16), int(minor, 16)), int(inode))
        except ValueError:
            continue
        found.extend(parse_proc_locks(payload, {identity: sidecar}))
    return found


def _descriptor_locks(pid: int, fd: str, inodes: dict) -> list:
    path = f"/proc/{pid}/fd/{fd}"
    try:
        before = os.stat(path)
        identity = (before.st_dev, before.st_ino)
        sidecar = inodes.get(identity)
        if sidecar is None:
            return []
        with open(f"/proc/{pid}/fdinfo/{fd}", encoding="ascii", errors="replace") as handle:
            text = handle.read()
        after = os.stat(path)
        if (after.st_dev, after.st_ino) != identity:
            return []  # Descriptor closed/reused while its metadata was read.
    except OSError:
        return []  # Processes and descriptors can disappear during a diagnostic scan.
    return _parse_descriptor_locks(text, sidecar)


def descriptor_write_locks(base: str, inodes: dict) -> list:
    from hermes_state_holders import foreign_state_db_holders

    pids = {pid for pid, _ in foreign_state_db_holders(Path(base)) if pid > 0}
    pids.add(os.getpid())
    held = []
    for pid in pids:
        try:
            descriptors = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in descriptors:
            held.extend(entry for entry in _descriptor_locks(pid, fd, inodes) if entry not in held)
    return held
