"""Resolve secret references. Secrets are never stored in the database.

A reference names where a secret lives. Supported today:

    env:NAME   -> os.environ["NAME"]

Further backends (Vault, file mounts) can be added here without touching
callers.
"""
import os
import re

_ENV_REF = re.compile(r"^env:([A-Z][A-Z0-9_]{2,127})$")


class SecretError(ValueError):
    """Invalid or unresolvable secret reference."""


def validate_ref(ref: str) -> str:
    if not isinstance(ref, str) or not _ENV_REF.match(ref.strip()):
        raise SecretError("secret references must look like env:UPPER_CASE_NAME")
    return ref.strip()


def resolve(ref: str) -> str:
    name = _ENV_REF.match(validate_ref(ref)).group(1)
    value = os.environ.get(name)
    if not value:
        raise SecretError(f"secret {ref} is not set")
    return value
