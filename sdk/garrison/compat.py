"""Compatibility with the names this project had before 0.1.0, when it was
called Bastion. Running setups keep working while they move over:

- environment variables: ``GARRISON_*`` first, else the old ``BASTION_*``
  name, with a one-time deprecation warning per variable (:func:`getenv`);
- request headers: ``X-Garrison*`` first, else the old ``X-Bastion*`` name
  (:func:`header`).

These fallbacks will be removed in a later release.
"""

from __future__ import annotations

import logging
import os
import warnings
from typing import Mapping

log = logging.getLogger("garrison")

PREFIX = "GARRISON_"
OLD_PREFIX = "BASTION_"
HEADER_PREFIX = "x-garrison"
OLD_HEADER_PREFIX = "x-bastion"

_warned: set[str] = set()


def old_name(name: str) -> str | None:
    """The pre-rename name of a ``GARRISON_*`` variable, else None."""
    return OLD_PREFIX + name[len(PREFIX):] if name.startswith(PREFIX) else None


def getenv(name: str, default: str | None = None) -> str | None:
    """``os.environ[name]``, falling back to its old ``BASTION_*`` name."""
    value = os.environ.get(name)
    if value is not None:
        return value
    old = old_name(name)
    if old is not None and old in os.environ:
        if old not in _warned:
            _warned.add(old)
            message = f"{old} is deprecated; set {name} instead (the project was renamed to Garrison)."
            log.warning(message)
            warnings.warn(message, DeprecationWarning, stacklevel=2)
        return os.environ[old]
    return default


def header(headers: Mapping[str, str], name: str) -> str | None:
    """A request header by its ``X-Garrison*`` name, falling back to the old
    ``X-Bastion*`` name. *headers* may be any mapping; lookup ignores case."""
    lower = {k.lower(): v for k, v in headers.items()}
    key = name.lower()
    if key in lower:
        return lower[key]
    if key.startswith(HEADER_PREFIX):
        return lower.get(OLD_HEADER_PREFIX + key[len(HEADER_PREFIX):])
    return None
