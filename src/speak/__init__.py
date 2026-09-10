"""speak — a full-screen TUI for macOS's built-in `say`."""

from importlib.metadata import version

# Read from the installed metadata rather than repeating pyproject's version
# here. The two were separate strings, they drifted, and `speak --version`
# reported 0.1.0 from a 0.2.0 checkout.
__version__ = version("speak-tui")
