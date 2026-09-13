"""AI Village Mass Exporter."""

__version__ = "0.2.0"

# Install the compatibility layer that enriches day exports with the live
# /api/computer-use-sessions payload and its real turn-level records.
from . import computer_use_patch as _computer_use_patch  # noqa: F401,E402
