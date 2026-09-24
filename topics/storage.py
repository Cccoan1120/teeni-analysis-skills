import os
import shutil
from pathlib import Path, PurePosixPath
from uuid import UUID

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation


def storage_root() -> Path:
    return Path(settings.TEENI_TOPIC_STORAGE_ROOT).resolve()


def job_directory(job_id: UUID | str, *, create: bool = False) -> Path:
    UUID(str(job_id))
    directory = storage_root() / "jobs" / str(job_id)
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    return directory


def safe_job_path(job_id: UUID | str, relative_name: str, *, create_parent: bool = False) -> Path:
    candidate = PurePosixPath(relative_name)
    if not relative_name or candidate.is_absolute() or ".." in candidate.parts or "\\" in relative_name:
        raise SuspiciousFileOperation("unsafe topic job path")
    base = job_directory(job_id, create=create_parent)
    resolved = (base / Path(*candidate.parts)).resolve()
    if base.resolve() not in resolved.parents:
        raise SuspiciousFileOperation("topic job path escapes storage root")
    if create_parent:
        resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return resolved


def upload_path(job_id: UUID | str, *, create: bool = False) -> Path:
    return safe_job_path(job_id, "input/upload.csv.part", create_parent=create)


def disk_has_capacity(expected_bytes: int) -> bool:
    root = storage_root()
    root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(root)
    reserve = int(usage.total * settings.TEENI_TOPIC_DISK_RESERVE_RATIO)
    return expected_bytes >= 0 and usage.free - expected_bytes >= reserve
