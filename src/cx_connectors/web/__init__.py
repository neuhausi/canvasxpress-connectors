"""Web serving helpers (requires the ``web`` extra)."""

__all__ = ["create_byo_app"]


def __getattr__(name):
    if name == "create_byo_app":
        from .byo_app import create_byo_app
        return create_byo_app
    raise AttributeError(name)
