#!/usr/bin/env python3
"""Keep AvianVisitors' persistent data below a bounded physical allocation."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable


DATE_DIRECTORY = re.compile(r"\d{4}-\d{2}-\d{2}")
AUDIO_SUFFIXES = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"})
DEFAULT_STOP_COMMAND = "/usr/local/bin/stop_core_services.sh"
CORE_SERVICES = (
    "birdnet_recording.service",
    "custom_recording.service",
    "birdnet_analysis.service",
    "chart_viewer.service",
    "spectrogram_viewer.service",
)


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"{timestamp} Avian storage cap: {message}", flush=True)


def format_bytes(value: int) -> str:
    return f"{value / 1_000_000_000:.3f} GB"


def environment_integer(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from error
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    root: Path
    recordings: Path
    protection_file: Path
    status_file: Path
    blocked_file: Path
    lock_file: Path
    cap_bytes: int
    cleanup_at_bytes: int
    target_bytes: int
    check_seconds: int
    minimum_age_seconds: int
    minimum_per_species: int
    stop_command: str

    @classmethod
    def from_environment(cls) -> "Settings":
        root = Path(os.environ.get("AVIAN_STORAGE_ROOT", "/config"))
        cap_bytes = environment_integer("AVIAN_STORAGE_CAP_BYTES", 20_000_000_000, 1)
        cleanup_at_bytes = environment_integer(
            "AVIAN_STORAGE_CLEANUP_AT_BYTES", 19_000_000_000, 1
        )
        target_bytes = environment_integer(
            "AVIAN_STORAGE_TARGET_BYTES", 18_000_000_000, 1
        )
        if not target_bytes < cleanup_at_bytes < cap_bytes:
            raise ValueError(
                "storage thresholds must satisfy target < cleanup-at < cap"
            )

        return cls(
            root=root,
            recordings=root / "BirdSongs" / "By_Date",
            protection_file=root / "disk_check_exclude.txt",
            status_file=root / ".avian-storage-cap-status.json",
            blocked_file=Path(
                os.environ.get(
                    "AVIAN_STORAGE_BLOCKED_FILE", "/run/avian-storage-cap-blocked"
                )
            ),
            lock_file=Path(
                os.environ.get(
                    "AVIAN_STORAGE_LOCK_FILE", "/run/avian-storage-cap.lock"
                )
            ),
            cap_bytes=cap_bytes,
            cleanup_at_bytes=cleanup_at_bytes,
            target_bytes=target_bytes,
            check_seconds=environment_integer(
                "AVIAN_STORAGE_CHECK_SECONDS", 60, 1
            ),
            minimum_age_seconds=environment_integer(
                "AVIAN_STORAGE_MIN_AGE_SECONDS", 600
            ),
            minimum_per_species=environment_integer(
                "AVIAN_STORAGE_MIN_PER_SPECIES", 30
            ),
            stop_command=os.environ.get(
                "AVIAN_STORAGE_STOP_COMMAND", DEFAULT_STOP_COMMAND
            ),
        )


@dataclass(frozen=True)
class MediaFile:
    path: Path
    device: int
    inode: int
    modified_ns: int
    allocated_bytes: int


@dataclass
class RecordingGroup:
    key: tuple[str, str, str]
    files: list[MediaFile] = field(default_factory=list)

    @property
    def species(self) -> str:
        return self.key[1]

    @property
    def newest_modified_ns(self) -> int:
        return max(member.modified_ns for member in self.files)

    @property
    def allocated_bytes(self) -> int:
        return sum(member.allocated_bytes for member in self.files)


class ProtectionFileError(RuntimeError):
    pass


def allocated_bytes(path: Path) -> int:
    """Return physical blocks below path, without crossing mounts or symlinks."""
    root_stat = path.lstat()
    if not stat.S_ISDIR(root_stat.st_mode):
        raise RuntimeError(f"storage root is not a directory: {path}")

    root_device = root_stat.st_dev
    seen: set[tuple[int, int]] = set()

    def visit(current: Path, current_stat: os.stat_result) -> int:
        identity = (current_stat.st_dev, current_stat.st_ino)
        if current_stat.st_dev != root_device or identity in seen:
            return 0
        seen.add(identity)
        total = current_stat.st_blocks * 512
        if not stat.S_ISDIR(current_stat.st_mode):
            return total

        try:
            entries = list(os.scandir(current))
        except FileNotFoundError:
            return total
        for entry in entries:
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            total += visit(Path(entry.path), entry_stat)
        return total

    return visit(path, root_stat)


def logical_key(relative: PurePosixPath) -> tuple[str, str, str] | None:
    parts = relative.parts
    if len(parts) == 4 and parts[0] == "shifted":
        parts = parts[1:]
    if len(parts) != 3 or DATE_DIRECTORY.fullmatch(parts[0]) is None:
        return None

    filename = parts[2]
    if filename.endswith(".png"):
        filename = filename[:-4]
    if PurePosixPath(filename).suffix.lower() not in AUDIO_SUFFIXES:
        return None
    return parts[0], parts[1], filename


def normalized_protection_path(raw: str) -> PurePosixPath | None:
    value = raw.strip()
    if not value or value.startswith("#"):
        return None

    prefixes = (
        "/home/pi/BirdSongs/Extracted/By_Date/",
        "/config/BirdSongs/By_Date/",
        "BirdSongs/By_Date/",
        "./",
    )
    for prefix in prefixes:
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break

    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ProtectionFileError(f"unsafe protected path: {raw!r}")
    return relative


def protected_keys(path: Path) -> set[tuple[str, str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()

    protected: set[tuple[str, str, str]] = set()
    invalid: list[str] = []
    for raw in lines:
        relative = normalized_protection_path(raw)
        if relative is None:
            continue
        key = logical_key(relative)
        if key is None:
            invalid.append(raw)
        else:
            protected.add(key)
    if invalid:
        samples = ", ".join(repr(item) for item in invalid[:3])
        raise ProtectionFileError(f"unrecognized protected path(s): {samples}")
    return protected


def directory_entry_on_device(entry: os.DirEntry[str], device: int) -> bool:
    try:
        entry_stat = entry.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(entry_stat.st_mode) and entry_stat.st_dev == device


def recording_directories(
    root: Path, device: int
) -> Iterable[tuple[str, str, Path]]:
    """Yield exact date/species directories, including optional shifted copies."""
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_dev != device:
        raise RuntimeError(f"recordings path is not a local, real directory: {root}")
    try:
        top_entries = list(os.scandir(root))
    except FileNotFoundError:
        return

    for top in top_entries:
        if not directory_entry_on_device(top, device):
            continue
        if DATE_DIRECTORY.fullmatch(top.name):
            date_entries = [(top.name, Path(top.path))]
        elif top.name == "shifted":
            date_entries = []
            for shifted_date in os.scandir(top.path):
                if directory_entry_on_device(
                    shifted_date, device
                ) and DATE_DIRECTORY.fullmatch(shifted_date.name):
                    date_entries.append(
                        (f"shifted/{shifted_date.name}", Path(shifted_date.path))
                    )
        else:
            continue

        for relative_date, date_path in date_entries:
            for species in os.scandir(date_path):
                if directory_entry_on_device(species, device):
                    yield relative_date, species.name, Path(species.path)


def recording_groups(root: Path, device: int) -> list[RecordingGroup]:
    groups: dict[tuple[str, str, str], RecordingGroup] = {}
    for relative_date, species, species_path in recording_directories(root, device):
        for entry in os.scandir(species_path):
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (
                entry_stat.st_dev != device
                or not stat.S_ISREG(entry_stat.st_mode)
                or entry_stat.st_nlink != 1
            ):
                continue
            relative = PurePosixPath(relative_date, species, entry.name)
            key = logical_key(relative)
            if key is None:
                continue
            group = groups.setdefault(key, RecordingGroup(key=key))
            group.files.append(
                MediaFile(
                    path=Path(entry.path),
                    device=entry_stat.st_dev,
                    inode=entry_stat.st_ino,
                    modified_ns=entry_stat.st_mtime_ns,
                    allocated_bytes=entry_stat.st_blocks * 512,
                )
            )
    return list(groups.values())


def deletion_candidates(
    groups: list[RecordingGroup],
    protected: set[tuple[str, str, str]],
    minimum_per_species: int,
    cutoff_ns: int,
) -> list[RecordingGroup]:
    unprotected: dict[str, list[RecordingGroup]] = {}
    for group in groups:
        if group.key not in protected:
            unprotected.setdefault(group.species, []).append(group)

    reserved: set[tuple[str, str, str]] = set()
    for species_groups in unprotected.values():
        species_groups.sort(key=lambda group: group.newest_modified_ns, reverse=True)
        reserved.update(group.key for group in species_groups[:minimum_per_species])

    candidates = [
        group
        for group in groups
        if group.key not in protected
        and group.key not in reserved
        and group.newest_modified_ns <= cutoff_ns
    ]
    candidates.sort(key=lambda group: (group.newest_modified_ns, group.key))
    return candidates


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class StorageGuard:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.capture_stopped = False

    def write_status(self, state: str, used: int | None, detail: str) -> None:
        atomic_json(
            self.settings.status_file,
            {
                "cap_bytes": self.settings.cap_bytes,
                "cleanup_at_bytes": self.settings.cleanup_at_bytes,
                "detail": detail,
                "state": state,
                "target_bytes": self.settings.target_bytes,
                "timestamp": int(time.time()),
                "used_bytes": used,
            },
        )

    def core_service_states(self) -> dict[str, str]:
        states: dict[str, str] = {}
        for service in CORE_SERVICES:
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", service],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired):
                states[service] = "unknown"
            else:
                states[service] = result.stdout.strip() or "unknown"
        return states

    def existing_stopped_services(self) -> set[str]:
        try:
            payload = json.loads(
                self.settings.blocked_file.read_text(encoding="utf-8")
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return set()
        services = payload.get("stopped_services", [])
        if not isinstance(services, list):
            return set()
        return {service for service in services if service in CORE_SERVICES}

    def all_core_services_inactive(self) -> bool:
        inactive_states = {"failed", "inactive"}
        return all(
            state in inactive_states for state in self.core_service_states().values()
        )

    def stop_capture(self, reason: str, used: int | None) -> None:
        if (
            self.capture_stopped
            and self.settings.stop_command != DEFAULT_STOP_COMMAND
        ):
            return

        if self.settings.stop_command == DEFAULT_STOP_COMMAND:
            states_before = self.core_service_states()
            if self.capture_stopped and all(
                state in {"failed", "inactive"}
                for state in states_before.values()
            ):
                return
            services = self.existing_stopped_services()
            services.update(
                service
                for service, state in states_before.items()
                if state not in {"failed", "inactive"}
            )
        else:
            states_before = {}
            services = set()

        payload: dict[str, object] = {
            "cap_bytes": self.settings.cap_bytes,
            "reason": reason,
            "stopped_services": sorted(services),
            "timestamp": int(time.time()),
            "used_bytes": used,
        }
        try:
            atomic_json(self.settings.blocked_file, payload)
        except OSError as error:
            # A genuinely full filesystem may not have room for the marker.
            # Capture still has to stop; process liveness also makes health fail
            # if the guard itself cannot continue safely.
            log(f"ERROR: could not write blocked marker: {error}")
        log(f"BLOCKED: {reason}; stopping capture before the 20 GB ceiling")
        stopped = False
        if self.settings.stop_command == DEFAULT_STOP_COMMAND:
            for service, state in states_before.items():
                if state in {"failed", "inactive"}:
                    continue
                try:
                    result = subprocess.run(
                        ["systemctl", "stop", service],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=45,
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    log(f"ERROR: could not stop {service}: {error}")
                else:
                    if result.returncode != 0:
                        log(
                            f"ERROR: stopping {service} exited {result.returncode}"
                        )
            stopped = self.all_core_services_inactive()
            if not stopped:
                states_after = self.core_service_states()
                unsafe = ", ".join(
                    f"{service}={state}"
                    for service, state in states_after.items()
                    if state not in {"failed", "inactive"}
                )
                log(
                    "ERROR: capture shutdown was not verified; the guard will "
                    f"retry ({unsafe or 'unknown state'})"
                )
        else:
            try:
                result = subprocess.run(
                    shlex.split(self.settings.stop_command),
                    check=False,
                    timeout=90,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                log(f"ERROR: capture stop command failed: {error}")
            else:
                stopped = result.returncode == 0
                if not stopped:
                    log(f"ERROR: capture stop command exited {result.returncode}")
        self.capture_stopped = stopped

    def resume_capture(self) -> None:
        try:
            payload = json.loads(
                self.settings.blocked_file.read_text(encoding="utf-8")
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            payload = {}

        services = payload.get("stopped_services", [])
        if isinstance(services, list) and self.settings.stop_command == DEFAULT_STOP_COMMAND:
            for service in services:
                if service not in CORE_SERVICES:
                    continue
                result = subprocess.run(
                    ["systemctl", "start", service],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"could not restart {service}")
        self.settings.blocked_file.unlink(missing_ok=True)
        self.capture_stopped = False
        if services:
            log("storage recovered; resumed the services stopped by the guard")

    def delete_group(self, group: RecordingGroup, cutoff_ns: int) -> int:
        # The web UI can update protections at any time. Re-read immediately
        # before touching each recording group.
        if group.key in protected_keys(self.settings.protection_file):
            return 0

        current: list[MediaFile] = []
        for member in group.files:
            try:
                current_stat = member.path.lstat()
            except FileNotFoundError:
                return 0
            if (
                not stat.S_ISREG(current_stat.st_mode)
                or current_stat.st_nlink != 1
                or current_stat.st_dev != member.device
                or current_stat.st_ino != member.inode
                or current_stat.st_mtime_ns > cutoff_ns
            ):
                return 0
            current.append(member)

        reclaimed = 0
        parents: set[Path] = set()
        for member in sorted(current, key=lambda item: item.path.name.endswith(".png"), reverse=True):
            try:
                member.path.unlink()
            except FileNotFoundError:
                continue
            parents.add(member.path.parent)
            reclaimed += member.allocated_bytes

        self.prune_empty_directories(parents)
        return reclaimed

    def prune_empty_directories(self, parents: set[Path]) -> None:
        shifted_root = self.settings.recordings / "shifted"
        for starting_path in parents:
            current = starting_path
            while current not in {self.settings.recordings, shifted_root}:
                try:
                    current.rmdir()
                except (FileNotFoundError, OSError):
                    break
                current = current.parent

    def cleanup(self, initial_used: int) -> tuple[int, int]:
        used = initial_used
        deleted_groups = 0
        cutoff_ns = time.time_ns() - self.settings.minimum_age_seconds * 1_000_000_000
        device = self.settings.root.lstat().st_dev

        for _pass in range(4):
            protected = protected_keys(self.settings.protection_file)
            groups = recording_groups(self.settings.recordings, device)
            candidates = deletion_candidates(
                groups,
                protected,
                self.settings.minimum_per_species,
                cutoff_ns,
            )
            progress = False
            estimated = used
            for group in candidates:
                if estimated <= self.settings.target_bytes:
                    break
                reclaimed = self.delete_group(group, cutoff_ns)
                if reclaimed:
                    progress = True
                    deleted_groups += 1
                    estimated = max(0, estimated - reclaimed)

            used = allocated_bytes(self.settings.root)
            if used <= self.settings.target_bytes or not progress:
                return used, deleted_groups
        return used, deleted_groups

    def sweep(self) -> bool:
        """Run one sweep. Return True when capture remains quota-blocked."""
        try:
            used = allocated_bytes(self.settings.root)
        except Exception as error:
            reason = f"cannot measure {self.settings.root}: {error}"
            log(f"ERROR: {reason}")
            try:
                self.stop_capture(reason, None)
                self.write_status("blocked", None, reason)
            except Exception as status_error:
                log(f"ERROR: could not record blocked state: {status_error}")
            return True

        already_blocked = self.settings.blocked_file.exists()
        if already_blocked and used <= self.settings.target_bytes:
            try:
                self.resume_capture()
            except Exception as error:
                reason = f"usage recovered, but capture could not resume: {error}"
                log(f"ERROR: {reason}")
                self.write_status("blocked", used, reason)
                return True
            self.write_status("ok", used, "storage recovered below target")
            return False

        if used < self.settings.cleanup_at_bytes and not already_blocked:
            self.write_status("ok", used, "below cleanup threshold")
            return False

        if used >= self.settings.cap_bytes:
            self.stop_capture(
                f"allocation reached {format_bytes(used)}", used
            )

        log(
            f"allocation is {format_bytes(used)}; removing oldest unlocked media "
            f"toward {format_bytes(self.settings.target_bytes)}"
        )
        try:
            used_after, deleted_groups = self.cleanup(used)
        except Exception as error:
            reason = f"cleanup failed safely: {error}"
            log(f"ERROR: {reason}")
            self.stop_capture(reason, used)
            self.write_status("blocked", used, reason)
            return True

        if used_after <= self.settings.target_bytes:
            if self.settings.blocked_file.exists():
                try:
                    self.resume_capture()
                except Exception as error:
                    reason = f"cleanup succeeded, but capture could not resume: {error}"
                    log(f"ERROR: {reason}")
                    self.write_status("blocked", used_after, reason)
                    return True
            detail = f"removed {deleted_groups} recording group(s)"
            log(f"{detail}; allocation is now {format_bytes(used_after)}")
            self.write_status("ok", used_after, detail)
            return False

        reason = (
            f"only reached {format_bytes(used_after)} after removing "
            f"{deleted_groups} group(s); protected, recent, or minimum-retention "
            "data prevents further safe cleanup"
        )
        self.stop_capture(reason, used_after)
        self.write_status("blocked", used_after, reason)
        return True


def emergency_stop(message: str) -> None:
    log(f"ERROR: {message}")
    command = os.environ.get("AVIAN_STORAGE_STOP_COMMAND", DEFAULT_STOP_COMMAND)
    try:
        subprocess.run(shlex.split(command), check=False, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"ERROR: emergency capture stop failed: {error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one sweep and exit")
    args = parser.parse_args()

    try:
        settings = Settings.from_environment()
    except ValueError as error:
        emergency_stop(f"invalid storage-cap configuration: {error}")
        return 2

    if not settings.root.is_dir():
        emergency_stop(f"storage root does not exist: {settings.root}")
        return 2

    settings.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with settings.lock_file.open("w", encoding="ascii") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another guard owns the quota lock; exiting")
            return 0

        guard = StorageGuard(settings)
        log(
            f"guard active: cap {format_bytes(settings.cap_bytes)}, cleanup at "
            f"{format_bytes(settings.cleanup_at_bytes)}, target "
            f"{format_bytes(settings.target_bytes)}"
        )
        if args.once:
            try:
                return 2 if guard.sweep() else 0
            except Exception as error:
                guard.stop_capture(f"unexpected guard failure: {error}", None)
                log(f"ERROR: unexpected guard failure: {error}")
                return 2

        while True:
            try:
                guard.sweep()
            except Exception as error:
                guard.stop_capture(f"unexpected guard failure: {error}", None)
                log(f"ERROR: unexpected guard failure: {error}")
            time.sleep(settings.check_seconds)


if __name__ == "__main__":
    sys.exit(main())
