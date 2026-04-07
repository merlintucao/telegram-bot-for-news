from __future__ import annotations

import json
import logging
from http.cookiejar import Cookie, CookieJar, MozillaCookieJar
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


def _make_cookie(record: dict[str, Any]) -> Cookie:
    domain = str(record["domain"])
    path = str(record.get("path") or "/")
    secure = bool(record.get("secure", False))
    expires = record.get("expires")
    if expires in {None, "", -1, 0}:
        expires = None
    else:
        expires = int(float(expires))

    return Cookie(
        version=0,
        name=str(record["name"]),
        value=str(record["value"]),
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=secure,
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": bool(record.get("httpOnly", False))},
        rfc2109=False,
    )


def _load_json_cookie_jar(path: Path) -> CookieJar:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records: list[dict[str, Any]]
    if isinstance(payload, dict):
        records = list(payload.get("cookies", []))
    elif isinstance(payload, list):
        records = list(payload)
    else:
        raise ValueError(f"Unsupported cookie JSON structure in {path}")

    jar = CookieJar()
    for record in records:
        if not isinstance(record, dict):
            continue
        if "name" not in record or "value" not in record or "domain" not in record:
            continue
        jar.set_cookie(_make_cookie(record))
    return jar


def load_cookie_jar(path: Path | None) -> CookieJar:
    if path is None:
        LOGGER.warning("No cookie file configured; requests may be blocked.")
        return CookieJar()

    if not path.exists():
        LOGGER.warning("Cookie file %s does not exist; using an empty jar.", path)
        return CookieJar()

    if path.suffix.lower() == ".json":
        return _load_json_cookie_jar(path)

    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    return jar

