"""Posit **pins** data source.

Reads a pinned dataset from a `pins <https://posit-dev.github.io/pins-python/>`_ board
and returns ``(header, rows)`` so :func:`cx_connectors.reshape.rows_to_cx` can turn it
into a CanvasXpress object. A pin written from **R or Python** reads the same here — the
Python ``pins`` package is cross-compatible — so an analyst's ``pin_write()`` board becomes
a live dashboard source with no export step.

Four board backends, matching the ``pins`` constructors:

* ``"connect"`` — a Posit Connect server. ``server_url`` + ``api_key`` (or the
  ``CONNECT_SERVER`` / ``CONNECT_API_KEY`` environment, as ``board_connect()`` uses when
  they are omitted). Pin name is usually ``"owner/name"``.
* ``"folder"`` — a local or mounted directory (``board_folder("~/Dropbox/my-pins")``).
* ``"s3"`` — an S3 bucket (``board_s3("my-bucket", region=...)``); needs the ``[pins-s3]``
  extra and AWS credentials in the server environment.
* ``"url"`` — a read-only board published over HTTP(S). ``board_url`` addresses a pin by an
  exact version path, so the config carries ``url`` (board root) and ``path`` (the pin's
  versioned sub-path); see the pins "web-hosted boards" guide.

The source holds no credential of its own beyond what the stored config supplies; like the
other SaaS connectors the server reads the board and the browser only sees the reshaped
result, never the board credentials.

A pin need not be tabular (pins can store models, plots, arbitrary objects). Only a
DataFrame-like pin (``.columns`` + row iteration) can become a chart, so a non-tabular pin
raises a clear error rather than a cryptic reshape failure.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence, Tuple

#: The board backends this source understands (the ``board_type`` config values).
BOARD_TYPES = ("connect", "folder", "s3", "url")


class PinsBoardSource:
    """Read one pin from a pins board as ``(header, rows)``.

    :param board_type: One of :data:`BOARD_TYPES` — which ``pins`` board to open.
    :param pin_name: The pin to read (``"owner/name"`` on Connect, else ``"name"``).
    :param config: Backend parameters (per ``board_type``):
        ``connect`` → ``server_url``, ``api_key``; ``folder`` → ``path``;
        ``s3`` → ``path`` (bucket) and optional ``region`` / other storage options;
        ``url`` → ``url`` (board root) and ``path`` (the pin's versioned sub-path).
    :raises ValueError: On an unknown ``board_type``, a missing required parameter, or a
        non-tabular pin.
    """

    def __init__(self, board_type: str, pin_name: str, config: Optional[Mapping[str, Any]] = None):
        board_type = (board_type or "").strip().lower()
        if board_type not in BOARD_TYPES:
            raise ValueError("Unknown pins board_type %r (expected one of %s)"
                             % (board_type, ", ".join(BOARD_TYPES)))
        if not (pin_name or "").strip():
            raise ValueError("pin_name is required")
        self.board_type = board_type
        self.pin_name = pin_name.strip()
        self.config = dict(config or {})

    def _require_params(self) -> None:
        """Validate that the config has the required fields for this ``board_type``.

        Pure field-presence checks — imports no package and opens no connection — so a
        registration form can reject a bad config with a clear 400 before anything tries to
        reach the board.

        :raises ValueError: When a required parameter for the chosen backend is absent.
        """
        cfg = self.config
        if self.board_type == "folder" and not (cfg.get("path") or "").strip():
            raise ValueError("A pins folder board requires a 'path'")
        if self.board_type == "s3" and not (cfg.get("path") or "").strip():
            raise ValueError("A pins S3 board requires a 'path' (bucket name)")
        if self.board_type == "url" and not ((cfg.get("url") or "").strip()
                                             and (cfg.get("path") or "").strip()):
            raise ValueError("A pins URL board requires 'url' (board root) and 'path' "
                             "(the pin's versioned sub-path)")
        # "connect" needs no stored field: server_url/api_key may come from the environment.

    def _open_board(self):
        """Construct the ``pins`` board for this source's ``board_type`` + config.

        :returns: An open ``pins`` board object.
        :raises ValueError: When a required parameter for the chosen backend is absent.
        """
        import pins  # lazy: importing this module must not require the pins package

        cfg = self.config
        if self.board_type == "connect":
            # server_url/api_key optional: board_connect() falls back to the
            # CONNECT_SERVER / CONNECT_API_KEY environment, as the R/Python docs show.
            return pins.board_connect(server_url=cfg.get("server_url") or None,
                                      api_key=cfg.get("api_key") or None)
        if self.board_type == "folder":
            path = (cfg.get("path") or "").strip()
            if not path:
                raise ValueError("A pins folder board requires a 'path'")
            return pins.board_folder(path)
        if self.board_type == "s3":
            path = (cfg.get("path") or "").strip()
            if not path:
                raise ValueError("A pins S3 board requires a 'path' (bucket name)")
            storage = {k: v for k, v in cfg.items()
                       if k not in ("path",) and v not in (None, "")}
            return pins.board_s3(path, **storage)
        # url
        url = (cfg.get("url") or "").strip()
        version_path = (cfg.get("path") or "").strip()
        if not (url and version_path):
            raise ValueError("A pins URL board requires 'url' (board root) and 'path' "
                             "(the pin's versioned sub-path)")
        return pins.board_url(url, {self.pin_name: version_path})

    def read(self) -> Tuple[Sequence[str], Sequence[Sequence[Any]]]:
        """Read the pin and return ``(header, rows)``.

        :returns: Column names and row values of the pinned table.
        :raises ValueError: When the pin is not a tabular (DataFrame-like) object.
        """
        board = self._open_board()
        obj = board.pin_read(self.pin_name)
        return _frame_to_rows(obj, self.pin_name)


def _frame_to_rows(obj: Any, pin_name: str) -> Tuple[List[str], List[List[Any]]]:
    """Turn a pinned object into ``(header, rows)``, or fail with a clear message.

    Accepts a pandas DataFrame (the usual pin payload) or a list-of-dicts/records; anything
    else — a model, a plot, a scalar — cannot become a chart and raises.

    :param obj: The object returned by ``pin_read``.
    :param pin_name: The pin name, for the error message.
    :returns: ``(header, rows)``.
    :raises ValueError: When ``obj`` is not tabular.
    """
    # pandas DataFrame (duck-typed so pandas isn't a hard import here).
    if hasattr(obj, "columns") and hasattr(obj, "itertuples"):
        header = [str(c) for c in obj.columns]
        rows = [list(t) for t in obj.itertuples(index=False, name=None)]
        return header, rows
    # A list of records (dicts): header is the union of keys, first-seen order.
    if isinstance(obj, (list, tuple)) and obj and isinstance(obj[0], Mapping):
        header = []
        for record in obj:
            for key in record:
                if key not in header:
                    header.append(str(key))
        rows = [[record.get(col) for col in header] for record in obj]
        return header, rows
    raise ValueError(
        "Pin %r is not a tabular dataset (got %s); only a data-frame pin can feed a chart."
        % (pin_name, type(obj).__name__))
