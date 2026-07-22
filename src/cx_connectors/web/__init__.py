"""Web serving helpers (requires the ``web`` extra)."""

__all__ = ["create_byo_app", "create_sheets_app"]


def __getattr__(name):
    if name == "create_byo_app":
        from .byo_app import create_byo_app
        return create_byo_app
    if name == "create_sheets_app":
        from .sheets_app import create_sheets_app
        return create_sheets_app
    raise AttributeError(name)
