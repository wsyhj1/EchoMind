"""Explicit booleans, loaded per request so deployment rollback is immediate."""
import os


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized not in ("true", "false"):
        raise ValueError(f"{name} must be true or false")
    return normalized == "true"
