"""Simulated OPC UA devices with behaviour, NE 107 health, real alarms and fault injection."""
__version__ = "0.1.0"

from .bench import Bench, BenchError  # noqa: E402
from .profile import ProfileError, bundled_profiles, load_profile  # noqa: E402

__all__ = ["Bench", "BenchError", "ProfileError", "__version__", "bundled_profiles",
           "load_profile"]
