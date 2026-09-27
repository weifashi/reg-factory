"""Explicit fixture key versions from owner-private, descriptor-checked files.

No creation, environment lookup, fallback key or HTTP-controlled path exists.
The existing private-file reader walks every directory with O_NOFOLLOW and
checks owner/mode/regular-file/link-count on the opened descriptors each time.
"""
from dataclasses import dataclass, field
from pathlib import Path
import re

from .errors import ErrorCode, ServiceError
from .settings import BASE, _read_private_file

_VERSION = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_DIRECTORY = re.compile(r'fixture-[0-9a-f]{32}\Z')


@dataclass(frozen=True, repr=False)
class Keyring:
    directory: Path = field(repr=False)
    active_version: str = field(repr=False)

    def __post_init__(self):
        try:
            directory = Path(self.directory)
            if (directory.parent != BASE / 'keyrings'
                    or not _DIRECTORY.fullmatch(directory.name)
                    or type(self.active_version) is not str
                    or not _VERSION.fullmatch(self.active_version)):
                raise ValueError
            object.__setattr__(self, 'directory', directory)
            self.key(self.active_version)
        except (OSError, TypeError, ValueError):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE) from None

    def __repr__(self):
        return '<Keyring>'

    def key(self, version):
        try:
            if type(version) is not str or not _VERSION.fullmatch(version):
                raise ValueError
            value = _read_private_file(self.directory / (version + '.key'))
            if len(value) != 32:
                raise ValueError
            return value
        except (OSError, TypeError, ValueError):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE) from None
