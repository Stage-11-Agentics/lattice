"""The Lattice server: one process that owns and serves many project boards (SPEC §8).

Only ``app``, ``serve``, and ``testing`` import the ``server`` extra's
libraries (Starlette, uvicorn); everything else here is standard library, so
the ``lattice server`` admin commands work without the extra. Nothing here
imports ``lattice.cli`` or ``lattice.integrations``: the server runs no hooks,
spawns no agents, and never touches c11 (G-5, G-10).
"""
