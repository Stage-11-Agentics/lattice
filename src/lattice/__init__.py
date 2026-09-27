"""Lattice: file-based, agent-native task tracker with an event-sourced core."""


def __getattr__(name: str) -> str:
    # ``__version__`` resolves on first use: ``importlib.metadata`` costs about
    # 0.1 s at import, which the server's stage process would pay per commit.
    if name == "__version__":
        from importlib.metadata import version

        value = version("lattice-tracker")
        globals()["__version__"] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
