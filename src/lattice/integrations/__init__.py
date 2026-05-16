"""Optional, environment-aware spawning backends for the agent_spawn primitive.

Each module in this package provides a concrete ``Backend`` implementation
that drives an agent runner via something other than ``subprocess.run``:

- ``terminal`` opens macOS Terminal.app / iTerm2 windows or
  ``gnome-terminal`` / ``xterm`` on Linux.
- ``c11`` opens a dedicated workspace and 2x2 pane grid inside the c11
  multiplexer.

Backends are loaded lazily by ``lattice.core.agent_spawn.select_backend``
so that the absence of c11 or a usable terminal launcher never breaks
import time.
"""
