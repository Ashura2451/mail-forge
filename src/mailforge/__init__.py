"""Mailforge's canonical package boundary."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mailforge")
except PackageNotFoundError:
    # Source-tree imports remain useful before the wheel metadata exists.
    __version__ = "0.1.0"
