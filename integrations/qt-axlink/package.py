#!/usr/bin/env python3
"""Install and select an opt-in Qt/KWin plugin pair for a private test session."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

RPM_NAMES = ("kwin", "qt6-qtbase", "qt6-qtwayland")
# The compiler headers, KWin internal ABI, and Qt private API used by this build.
BUILD_ABI = dict(
    zip(
        RPM_NAMES,
        ("6.8.3-1.fc45.x86_64", "6.11.2-3.fc45.x86_64", "6.11.2-1.fc45.x86_64"),
        strict=True,
    )
)
PLUGIN_FILES = (
    "generic/libjamesosaxlink.so",
    "kwin/plugins/kwin-axlink.so",
)
RELEASE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}\Z")
PRIVATE_RUNTIME = re.compile(r"/tmp/kax-[A-Za-z0-9]{6}/r\Z")


def trusted_dir(path: Path) -> Path:
    path = path.absolute()
    info = path.lstat()
    if path.resolve() != path or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Refusing a symlink or indirect directory: {path}")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(f"Directory must be owned by the current user and private: {path}")
    return path


def regular(path: Path) -> Path:
    info = path.lstat()
    if path.resolve() != path or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Expected regular file: {path.name}")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise ValueError(f"Expected an owned file not writable by others: {path.name}")
    return path


def digest(path: Path) -> str:
    with regular(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def release_id(value: object) -> str:
    if not isinstance(value, str) or not RELEASE_ID.fullmatch(value):
        raise ValueError("Invalid release ID")
    return value


def rpm_abi() -> dict[str, str]:
    out = subprocess.check_output(
        ["/usr/bin/rpm", "-q", "--qf", "%{NAME} %{VERSION}-%{RELEASE}.%{ARCH}\n", *RPM_NAMES],
        text=True,
        timeout=5,
    )
    records = [line.split() for line in out.splitlines()]
    if len(records) != len(RPM_NAMES) or any(len(pair) != 2 for pair in records):
        raise ValueError("Expected one installed RPM for each supported component")
    return dict(records)


def verify_abi(expected: dict) -> None:
    actual = rpm_abi()
    if set(actual) != set(RPM_NAMES) or set(expected) != set(RPM_NAMES):
        raise ValueError("Incomplete KWin/Qt compatibility manifest")
    for name in RPM_NAMES:
        if actual[name] != expected[name]:
            raise ValueError(
                f"Incompatible {name}: expected {expected[name]}, found {actual[name]}"
            )
    if expected != BUILD_ABI:
        raise ValueError("Manifest does not match the compiler SDK pins")


def manifest(directory: Path) -> dict:
    record = json.loads(regular(directory / "manifest.json").read_text())
    if not isinstance(record, dict) or record.get("format") != 1:
        raise ValueError("Unsupported package manifest")
    release_id(record.get("id"))
    if not isinstance(record.get("rpm"), dict) or not isinstance(record.get("sha256"), dict):
        raise ValueError("Invalid package manifest")
    verify_abi(record["rpm"])
    if set(record["sha256"]) != set(PLUGIN_FILES):
        raise ValueError("Unexpected plugin file set")
    for name in PLUGIN_FILES:
        if digest(directory / "plugins" / name) != record["sha256"][name]:
            raise ValueError(f"Plugin checksum mismatch: {name}")
    return record


def write_json(path: Path, record: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(record, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def state(root: Path) -> dict:
    path = root / "selection.json"
    if not path.exists() and not path.is_symlink():
        return {"active": None, "previous": None}
    data = json.loads(regular(path).read_text())
    if not isinstance(data, dict) or set(data) != {"active", "previous"}:
        raise ValueError("Invalid package selection")
    for value in data.values():
        if value is not None:
            release_id(value)
    return data


def build(build_plugins: Path, target: Path, label: str) -> None:
    verify_abi(BUILD_ABI)
    label = release_id(label)
    target = trusted_dir(target)
    if any(target.iterdir()):
        raise ValueError("Package output directory must be empty")
    hashes = {}
    for name in PLUGIN_FILES:
        source = regular(build_plugins.resolve() / name)
        destination = target / "plugins" / name
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        hashes[name] = digest(destination)
    write_json(
        target / "manifest.json", {"format": 1, "id": label, "rpm": BUILD_ABI, "sha256": hashes}
    )


def install(root: Path, source: Path) -> str:
    source = trusted_dir(source)
    record = manifest(source)
    current = state(root)
    label = record["id"]
    destination = root / label
    if label == "selection.json":
        raise ValueError("Reserved release ID")
    if destination.exists() or destination.is_symlink():
        if manifest(trusted_dir(destination)) != record:
            raise ValueError("Release ID already exists with different contents")
    else:
        with tempfile.TemporaryDirectory(dir=root, prefix=".incoming-") as temporary:
            staged = Path(temporary) / "release"
            staged.mkdir(mode=0o700)
            for name in PLUGIN_FILES:
                place = staged / "plugins" / name
                place.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(source / "plugins" / name, place)
            write_json(staged / "manifest.json", record)
            if manifest(staged) != record:
                raise ValueError("Copied release failed verification")
            staged.rename(destination)
    if current["active"] != label:
        write_json(root / "selection.json", {"active": label, "previous": current["active"]})
    return label


def rollback(root: Path) -> str:
    current = state(root)
    previous = current["previous"]
    if previous is None:
        raise ValueError("No previous release selected")
    manifest(trusted_dir(root / previous))
    write_json(root / "selection.json", {"active": previous, "previous": current["active"]})
    return previous


def plugin_dir(root: Path, runtime: str) -> Path:
    if not PRIVATE_RUNTIME.fullmatch(runtime):
        raise ValueError("This package may only launch in a private KWin test runtime")
    path = trusted_dir(Path(runtime))
    trusted_dir(path.parent)
    selected = state(root)["active"]
    if not selected:
        raise ValueError("No opt-in release selected")
    directory = trusted_dir(root / selected)
    manifest(directory)
    return directory / "plugins"


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    make = sub.add_parser("build")
    make.add_argument("plugins", type=Path)
    make.add_argument("output", type=Path)
    make.add_argument("release")
    for command in ("install", "rollback", "disable", "status", "plugin-dir"):
        args = sub.add_parser(command)
        args.add_argument("root", type=Path)
        if command == "install":
            args.add_argument("package", type=Path)
        if command == "plugin-dir":
            args.add_argument("runtime")
    args = parser.parse_args()
    try:
        if args.action == "build":
            build(args.plugins, args.output, args.release)
            return
        root = trusted_dir(args.root)
        fd = os.open(root / ".selection.lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
        with os.fdopen(fd, "r+b") as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise ValueError("Selection lock is not a regular file")
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.action == "install":
                print(install(root, args.package))
            elif args.action == "rollback":
                print(rollback(root))
            elif args.action == "disable":
                write_json(root / "selection.json", {"active": None, "previous": None})
            elif args.action == "status":
                print(json.dumps(state(root), sort_keys=True))
            else:
                print(plugin_dir(root, args.runtime))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
