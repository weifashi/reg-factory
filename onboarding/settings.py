"""Strict local-only configuration boundary for the P1b test database."""
from dataclasses import dataclass, field, fields
import json
import os
from pathlib import Path
import re
import stat

from .errors import ErrorCode, ServiceError


BASE = Path("/workspace/reg-factory/.local/onboarding-p1b")
SOCKET = str(BASE / "socket")
ROLES = {"app": "rf_onboarding_app", "migrator": "rf_onboarding_migrator"}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False)
    instance_marker: str
    schema: str


def validate_settings(settings: Settings, *, role: str = "app") -> None:
    """Recheck even directly constructed/replaced records before connecting."""
    if (type(settings) is not Settings or role not in ROLES
            or settings.host != SOCKET
            or type(settings.port) is not int or settings.port != 55433
            or settings.dbname != "rf_onboarding_test"
            or settings.user != ROLES[role]
            or type(settings.password) is not str or not settings.password
            or len(settings.password) > 4096 or "\x00" in settings.password
            or type(settings.instance_marker) is not str
            or not re.fullmatch(r"rf-onboarding-p1b-v1:[A-Za-z0-9_-]{1,128}",
                                settings.instance_marker)
            or type(settings.schema) is not str
            or not re.fullmatch(r"rf_onboarding|rf_p1b_test_[0-9a-f]{32}", settings.schema)):
        raise ServiceError(ErrorCode.INVALID_INPUT)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _read_private_file(path: Path) -> bytes:
    """Open each directory by descriptor so symlink swaps cannot redirect reads."""
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(BASE):
        raise ValueError("outside dedicated base")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    traversed = Path("/")
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=fd)
            os.close(fd)
            fd = next_fd
            traversed /= component
            if traversed == BASE or traversed.is_relative_to(BASE):
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError("unsafe directory")
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=fd)
        with os.fdopen(file_fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or info.st_size > 16384):
                raise ValueError("unsafe file")
            value = source.read(16385)
            if len(value) > 16384:
                raise ValueError("oversized file")
            return value
    finally:
        os.close(fd)


def load_settings(path, *, role: str = "app") -> Settings:
    """Load only owner-private JSON beneath the dedicated non-DEMO base."""
    try:
        data = json.loads(_read_private_file(Path(path)), object_pairs_hook=_unique_object)
        if type(data) is not dict or set(data) != {item.name for item in fields(Settings)}:
            raise ValueError("invalid configuration fields")
        settings = Settings(**data)
        validate_settings(settings, role=role)
        return settings
    except (OSError, TypeError, ValueError, UnicodeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None
