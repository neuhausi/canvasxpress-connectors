"""Hide the secrets inside a connection URL, and put them back when the URL is saved again.

A stored password or token should be replaceable but never shown again. These helpers let a
source's edit form display ``postgresql://app:********@db.example/sales`` and still save it:
when the form is sent back with the mask in it, :func:`unmask_conn_url` swaps the stored secret
back in.

What counts as a secret: the password in ``user:password@host``, any query parameter whose name
contains ``pass``, ``secret`` or ``token`` (or ends in ``key``), and a ``PWD=`` / ``Password=``
setting inside a query value (an ODBC connect string). Usernames, hosts, paths and the other
parameters stay readable so the owner can still see where a source points.

The swap is only done when the scheme, host and port are unchanged. Otherwise a stolen session
could register ``postgresql://user:********@attacker.example/db`` and have the server connect
there with the real password; pointing a source somewhere new means typing the password again.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple
from urllib.parse import quote, unquote

MASK = "********"

_SECRET_NAME = re.compile(r"pass|secret|token|(^|_)key$", re.IGNORECASE)
_EMBEDDED = re.compile(r"(pwd|password|passwd)\s*=\s*[^;&]*", re.IGNORECASE)

_Parts = Tuple[str, str, str, str, str, str, str]


def _split(url: str) -> Optional[_Parts]:
    """Cut a URL into ``(scheme, authority, path, "?" or "", query, "#" or "", fragment)``.

    Done by hand rather than with ``urlsplit``/``geturl`` because re-assembling through
    ``urllib`` drops the empty ``//`` of ``sqlite:///file.db``. ``None`` when it has no ``://``.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return None
    end = len(rest)
    for char in "/?#":
        found = rest.find(char)
        if found != -1:
            end = min(end, found)
    authority, tail = rest[:end], rest[end:]
    path, question, after = tail.partition("?")
    if question:
        query, hash_mark, fragment = after.partition("#")
    else:
        query, hash_mark, fragment = "", "", ""
    return scheme, authority, path, question, query, hash_mark, fragment


def _join(parts: _Parts) -> str:
    scheme, authority, path, question, query, hash_mark, fragment = parts
    return scheme + "://" + authority + path + question + query + hash_mark + fragment


def _userinfo(authority: str) -> Tuple[str, str, str]:
    """``authority`` as ``(user, password, host_and_port)``; the password is ``""`` when absent."""
    userinfo, _, hostport = authority.rpartition("@")
    user, _, password = userinfo.partition(":")
    return user, password, hostport


def _with_password(authority: str, password: str) -> str:
    user, _, hostport = _userinfo(authority)
    return user + ":" + password + "@" + hostport


def _mask_item(item: str) -> str:
    """One ``name=value`` query item with its secret (if any) masked."""
    name, equals, value = item.partition("=")
    if not equals or not value:
        return item
    if _SECRET_NAME.search(unquote(name)):
        return name + "=" + MASK
    decoded = unquote(value)
    hidden = _EMBEDDED.sub(lambda match: match.group(1) + "=" + MASK, decoded)
    # Keep "*" literal: the mask must still read as MASK when the form sends it back.
    return item if hidden == decoded else name + "=" + quote(hidden, safe="*")


def mask_conn_url(url: Optional[str]) -> str:
    """``url`` with its password and secret-looking parameters shown as ``********``.

    A string that is not a ``scheme://`` URL is returned unchanged (nothing to hide in a bare
    path). A URL that cannot be taken apart is hidden entirely rather than risk showing a secret.
    """
    if not url or "://" not in url:
        return url or ""
    parts = _split(url)
    if parts is None:
        return ""
    scheme, authority, path, question, query, hash_mark, fragment = parts
    user, password, hostport = _userinfo(authority)
    if password:
        authority = user + ":" + MASK + "@" + hostport
    if query:
        query = "&".join(_mask_item(item) for item in query.split("&"))
    return _join((scheme, authority, path, question, query, hash_mark, fragment))


def unmask_conn_url(url: str, stored_url: Optional[str]) -> str:
    """Replace every ``********`` in ``url`` with the matching secret from ``stored_url``.

    :param url: A URL from an edit form; it may still carry the mask.
    :param stored_url: The real stored URL of the source being edited or copied.
    :returns: ``url`` unchanged when it has no mask, else ``url`` with the stored secrets.
    :raises ValueError: If a masked value has no stored secret to take, if the scheme, host or
        port differs from the stored source (the password must be typed again), or if the URL
        was edited so the mask can no longer be matched.
    """
    if MASK not in url:
        return url
    retype = "Re-enter the password: it is never shown, so it cannot be reused for %s."
    if not stored_url:
        raise ValueError("Re-enter the password: there is no stored source to take it from.")
    if url == mask_conn_url(stored_url):
        return stored_url                                    # nothing was changed
    new, old = _split(url), _split(stored_url)
    if new is None or old is None:
        raise ValueError(retype % "this URL")
    if new[0] != old[0] or _userinfo(new[1])[2] != _userinfo(old[1])[2]:
        raise ValueError(retype % "a different server")

    scheme, authority, path, question, query, hash_mark, fragment = new
    if _userinfo(authority)[1] == MASK:
        old_password = _userinfo(old[1])[1]
        if not old_password:
            raise ValueError("Re-enter the password: the stored source has none to reuse.")
        authority = _with_password(authority, old_password)

    if MASK in query:
        stored_items = {unquote(item.partition("=")[0]): item for item in old[4].split("&") if item}
        rebuilt = []
        for item in query.split("&"):
            if MASK in item:
                original = stored_items.get(unquote(item.partition("=")[0]))
                if original is None or _mask_item(original) != item:
                    raise ValueError(retype % "this parameter")
                item = original
            rebuilt.append(item)
        query = "&".join(rebuilt)

    result = _join((scheme, authority, path, question, query, hash_mark, fragment))
    if MASK in result:
        raise ValueError(retype % "this URL")
    return result
