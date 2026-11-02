"""Simulated OPC UA devices with behaviour, NE 107 health, real alarms and fault injection."""
__version__ = "0.1.0"

from typing import Any  # noqa: E402

from .profile import ProfileError, bundled_profiles, load_profile  # noqa: E402

__all__ = ["Bench", "BenchError", "ProfileError", "__version__", "bundled_profiles",
           "load_profile"]


def __getattr__(name: str) -> Any:
    # The server needs asyncua; devices, models and profiles also run without it (browser demo).
    if name in ("Bench", "BenchError"):
        from . import bench
        return getattr(bench, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
