"""Posit **pins** data source.

Reads a pinned dataset from a `pins <https://posit-dev.github.io/pins-python/>`_ board
and returns ``(header, rows)`` so :func:`cx_connectors.reshape.rows_to_cx` can turn it
into a CanvasXpress object. A pin written from **R or Python** reads the same here — the
Python ``pins`` package is cross-compatible — so an analyst's ``pin_write()`` board becomes
a live dashboard source with no export step.

Four board backends, matching the ``pins`` constructors:

* ``"connect"`` — a Posit Connect server. ``server_url`` + ``api_key`` are **both
  required** on a user-registered source. The server's ``CONNECT_SERVER`` /
  ``CONNECT_API_KEY`` environment is **never** used as a fallback: on Posit Connect those
  carry the content owner's identity, so falling back would let any signed-in user read
  every pin the owner can reach. Pin name is usually ``"owner/name"``.
* ``"folder"`` — a local or mounted directory (``board_folder("~/Dropbox/my-pins")``).
  **Disabled unless** the operator sets an allowlist of roots in
  ``CXC_PINS_FOLDER_ROOTS`` (``os.pathsep``-separated); the configured path is resolved
  with ``realpath`` and must sit inside an allowed root, which also blocks ``..`` and
  symlink escapes.
* ``"s3"`` — an S3 bucket (``board_s3("my-bucket", region=...)``); needs the ``[pins-s3]``
  extra. The source config must carry its own AWS credentials **unless** the operator opts
  in to the server's ambient credentials with ``CXC_PINS_S3_SERVER_CREDENTIALS=on``.
* ``"url"`` — a read-only board published over HTTP(S). ``board_url`` addresses a pin by an
  exact version path, so the config carries ``url`` (board root) and ``path`` (the pin's
  versioned sub-path); see the pins "web-hosted boards" guide.

The source holds no credential of its own beyond what the stored config supplies; like the
other SaaS connectors the server reads the board and the browser only sees the reshaped
result, never the board credentials. A source **never** borrows the server's own ambient
credentials (Connect env, AWS env, arbitrary server paths) on behalf of a user — those
checks run at both registration and read time, so a source stored before this policy cannot
keep using server credentials.

A pin need not be tabular (pins can store models, plots, arbitrary objects). Only a
DataFrame-like pin (``.columns`` + row iteration) can become a chart, so a non-tabular pin
raises a clear error rather than a cryptic reshape failure.
"""

from __future__ import annotations

import os
from typing import Any, List, Mapping, Optional, Sequence, Tuple

#: The board backends this source understands (the ``board_type`` config values).
BOARD_TYPES = ("connect", "folder", "s3", "url")

#: Operator allowlist for ``folder`` boards: a ``os.pathsep``-separated list of directory
#: roots a user-registered folder board may read under (e.g.
#: ``CXC_PINS_FOLDER_ROOTS=/srv/pins:/data/pins``). Unset ⇒ folder boards are disabled.
_FOLDER_ROOTS_ENV = "CXC_PINS_FOLDER_ROOTS"

#: Operator opt-in for letting an ``s3`` board fall back to the server's ambient AWS
#: credentials (``CXC_PINS_S3_SERVER_CREDENTIALS=on``). Off ⇒ the source config must carry
#: its own credentials.
_S3_SERVER_CREDS_ENV = "CXC_PINS_S3_SERVER_CREDENTIALS"

#: Config keys that count as the source carrying its own S3 credentials.
_S3_CRED_KEYS = ("key", "secret", "token",
                 "aws_access_key_id", "aws_secret_access_key", "aws_session_token",
                 "profile")


def _folder_roots() -> List[str]:
    """Return the operator-configured allowlist of folder-board roots (realpath'd).

    :returns: Absolute, symlink-resolved directory roots from :data:`_FOLDER_ROOTS_ENV`;
        empty when the variable is unset or blank (folder boards then disabled).
    """
    raw = os.environ.get(_FOLDER_ROOTS_ENV) or ""
    return [os.path.realpath(os.path.expanduser(part))
            for part in raw.split(os.pathsep) if part.strip()]


def _s3_server_creds_allowed() -> bool:
    """Whether the operator opted in to ambient AWS credentials for S3 boards.

    :returns: ``True`` only when :data:`_S3_SERVER_CREDS_ENV` is an explicit on value.
    """
    return (os.environ.get(_S3_SERVER_CREDS_ENV) or "").strip().lower() in (
        "on", "1", "true", "yes")


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
        self._check_credentials()

    def _check_credentials(self) -> str:
        """Enforce the credential-escalation policy for this source's backend.

        Guarantees a user-registered source can never borrow the server's own ambient
        credentials. Runs at both registration (:meth:`_require_params`) and read
        (:meth:`_open_board`), so a source stored before this policy cannot keep using
        server credentials either.

        :returns: For a ``folder`` board, the validated absolute (realpath'd) directory;
            an empty string for every other backend.
        :raises ValueError: When the backend's security requirements are not met.
        """
        cfg = self.config
        if self.board_type == "connect":
            # Require an explicit server + key; never fall back to CONNECT_SERVER /
            # CONNECT_API_KEY (the content owner's identity on Posit Connect).
            if not (cfg.get("server_url") or "").strip() or not (cfg.get("api_key") or "").strip():
                raise ValueError(
                    "A pins Connect board requires an explicit 'server_url' and 'api_key'; "
                    "the server's CONNECT_SERVER / CONNECT_API_KEY environment is not used.")
            return ""
        if self.board_type == "folder":
            path = (cfg.get("path") or "").strip()
            if not path:
                raise ValueError("A pins folder board requires a 'path'")
            roots = _folder_roots()
            if not roots:
                raise ValueError(
                    "Folder pins boards are disabled: set %s to an allowlist of roots."
                    % _FOLDER_ROOTS_ENV)
            resolved = os.path.realpath(os.path.expanduser(path))
            for root in roots:
                if resolved == root or resolved.startswith(root + os.sep):
                    return resolved
            raise ValueError(
                "Folder path %r is outside the allowed roots (%s)." % (path, _FOLDER_ROOTS_ENV))
        if self.board_type == "s3":
            has_creds = any((cfg.get(k) or "") for k in _S3_CRED_KEYS)
            if not has_creds and not _s3_server_creds_allowed():
                raise ValueError(
                    "A pins S3 board requires credentials in its config unless the operator "
                    "sets %s=on to allow the server's ambient AWS credentials." % _S3_SERVER_CREDS_ENV)
            return ""
        return ""

    def _open_board(self):
        """Construct the ``pins`` board for this source's ``board_type`` + config.

        :returns: An open ``pins`` board object.
        :raises ValueError: When a required parameter for the chosen backend is absent.
        """
        import pins  # lazy: importing this module must not require the pins package

        cfg = self.config
        # Re-run the security policy at read time too, so a source stored before this
        # policy (or with a since-removed allowlist/opt-in) cannot keep using server creds.
        resolved_folder = self._check_credentials()
        if self.board_type == "connect":
            # Pass server_url/api_key explicitly (required, validated above) so
            # board_connect() can never fall back to the server's CONNECT_SERVER /
            # CONNECT_API_KEY environment (the content owner's identity).
            return pins.board_connect(server_url=cfg["server_url"], api_key=cfg["api_key"])
        if self.board_type == "folder":
            return pins.board_folder(resolved_folder)
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
