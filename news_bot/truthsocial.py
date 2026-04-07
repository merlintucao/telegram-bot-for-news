from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

from .config import AppConfig
from .cookies import load_cookie_jar
from .html_text import html_to_text
from .models import MediaAttachment, SourcePost
from .source_types import SourceError, SourceProbeResult

LOGGER = logging.getLogger(__name__)


class TruthSocialError(SourceError):
    pass


def _bool_query(value: bool) -> str:
    return "true" if value else "false"


def _build_error_hint(status_code: int | None, detail: str, using_cookie_auth: bool) -> str:
    lowered = detail.lower()
    if status_code == 403 or "cloudflare" in lowered or "you have been blocked" in lowered:
        if using_cookie_auth:
            return (
                "Truth Social rejected the request. The cookie export may be stale or "
                "Cloudflare may have challenged the session. Log into Truth Social in "
                "your browser again, export fresh cookies, and rerun `python3 -m news_bot doctor`."
            )
        return (
            "Truth Social rejected the anonymous request. The public endpoint may be "
            "temporarily blocked or Cloudflare may have challenged it. If this keeps "
            "happening, switch to cookie-backed mode and rerun `python3 -m news_bot doctor`."
        )
    if status_code == 401 or "unauthorized" in lowered:
        if using_cookie_auth:
            return (
                "Truth Social reported an unauthorized request. Refresh the logged-in "
                "browser session and export cookies again."
            )
        return (
            "Truth Social reported an unauthorized request on the public path. If this "
            "persists, switch to cookie-backed mode."
        )
    if "enable cookies" in lowered:
        if using_cookie_auth:
            return (
                "Truth Social responded with an anti-bot page instead of JSON. Make sure "
                "the poller is using a real logged-in browser cookie export."
            )
        return (
            "Truth Social responded with an anti-bot page instead of JSON. Anonymous "
            "polling is likely being challenged right now."
        )
    return ""


class TruthSocialClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.source_id = f"truthsocial:{self.config.truthsocial_handle}"
        self.source_name = "Truth Social"
        cookie_path = self._cookie_path()
        if cookie_path is not None and cookie_path.exists():
            self.cookie_jar = load_cookie_jar(cookie_path)
        else:
            self.cookie_jar = CookieJar()
        self.opener = self._build_opener()
        self._account_id = config.truthsocial_account_id
        self._cookie_signature = self._get_cookie_signature(cookie_path)

    def _cookie_path(self) -> Path | None:
        if self.config.truthsocial_auth_mode == "public":
            return None
        return self.config.truthsocial_cookies_file

    def _using_cookie_auth(self) -> bool:
        cookie_path = self._cookie_path()
        return cookie_path is not None and cookie_path.exists()

    def _build_opener(self) -> urllib.request.OpenerDirector:
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )

    def _get_cookie_signature(
        self, path: Path | None
    ) -> tuple[str, int, int] | tuple[str] | None:
        if path is None:
            return None
        if not path.exists():
            return ("missing",)
        stat = path.stat()
        return (str(path.resolve()), stat.st_mtime_ns, stat.st_size)

    def _reload_cookies_if_needed(self) -> None:
        cookie_path = self._cookie_path()
        if cookie_path is None or not self.config.truthsocial_reload_cookies:
            return

        signature = self._get_cookie_signature(cookie_path)
        if signature == self._cookie_signature:
            return

        self.cookie_jar = load_cookie_jar(cookie_path)
        self.opener = self._build_opener()
        self._cookie_signature = signature
        LOGGER.info("Reloaded Truth Social cookies from %s", cookie_path)

    def _request_json(self, path: str, params: dict[str, str]) -> Any:
        self._reload_cookies_if_needed()
        query = urllib.parse.urlencode(params)
        url = f"{self.config.truthsocial_base_url}{path}"
        if query:
            url = f"{url}?{query}"

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{self.config.truthsocial_base_url}/@{self.config.truthsocial_handle}",
                "User-Agent": self.config.user_agent,
            },
        )

        try:
            with self.opener.open(
                request,
                timeout=self.config.request_timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            hint = _build_error_hint(exc.code, detail, using_cookie_auth=self._using_cookie_auth())
            message = f"Truth Social HTTP {exc.code}: {detail}"
            if hint:
                message = f"{message} {hint}"
            raise TruthSocialError(message) from exc
        except urllib.error.URLError as exc:
            raise TruthSocialError(f"Truth Social request failed: {exc.reason}") from exc

        if raw[:1] not in {"{", "["} and "json" not in content_type.lower():
            snippet = raw[:300].replace("\n", " ")
            hint = _build_error_hint(
                None,
                raw[:1000],
                using_cookie_auth=self._using_cookie_auth(),
            )
            message = (
                "Truth Social did not return JSON. "
                f"Check the access mode, cookies, or Cloudflare state. Response snippet: {snippet}"
            )
            if hint:
                message = f"{message} {hint}"
            raise TruthSocialError(message)

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TruthSocialError(f"Invalid JSON from Truth Social: {exc}") from exc

    def get_account_id(self) -> str:
        if self._account_id:
            return self._account_id

        payload = self._request_json(
            "/api/v1/accounts/lookup",
            {"acct": self.config.truthsocial_handle},
        )
        account_id = str(payload.get("id", "")).strip()
        if not account_id:
            raise TruthSocialError("Account lookup succeeded but did not return an id.")
        self._account_id = account_id
        return account_id

    def fetch_posts(self, since_id: str | None = None, limit: int | None = None) -> list[SourcePost]:
        account_id = self.get_account_id()
        params = {
            "limit": str(limit or self.config.fetch_limit),
            "exclude_replies": _bool_query(self.config.exclude_replies),
            "exclude_reblogs": _bool_query(self.config.exclude_reblogs),
        }
        if since_id:
            params["since_id"] = since_id

        payload = self._request_json(
            f"/api/v1/accounts/{account_id}/statuses",
            params,
        )
        if not isinstance(payload, list):
            raise TruthSocialError("Statuses response was not a list.")

        return [self._parse_status(item) for item in payload if isinstance(item, dict)]

    def probe(self) -> SourceProbeResult:
        account_id = self.get_account_id()
        latest_posts = self.fetch_posts(limit=1)
        latest_id = latest_posts[0].id if latest_posts else "no posts returned"
        auth_detail = (
            f"auth mode: {self.config.truthsocial_auth_mode}"
            + (" (cookie-backed)" if self._using_cookie_auth() else " (anonymous/public)")
        )
        return SourceProbeResult(
            source_id=self.source_id,
            source_name=self.source_name,
            detail_lines=(
                auth_detail,
                f"account id: {account_id}",
                f"latest post id: {latest_id}",
            ),
        )

    def _parse_status(self, payload: dict[str, Any]) -> SourcePost:
        effective = payload.get("reblog") if isinstance(payload.get("reblog"), dict) else payload
        content_html = str(effective.get("content") or "")
        body_text = html_to_text(content_html)
        media_items = effective.get("media_attachments") or []
        media_attachments = []
        for item in media_items:
            if not isinstance(item, dict):
                continue
            media_url = str(item.get("url") or item.get("preview_url") or "").strip()
            if not media_url:
                continue
            media_attachments.append(
                MediaAttachment(
                    kind=str(item.get("type") or "unknown"),
                    url=media_url,
                    preview_url=str(item.get("preview_url") or "").strip() or None,
                    description=str(item.get("description") or "").strip() or None,
                )
            )

        account = payload.get("account") or {}
        account_handle = str(account.get("acct") or self.config.truthsocial_handle)

        return SourcePost(
            source_id=self.source_id,
            source_name=self.source_name,
            id=str(payload["id"]),
            account_handle=account_handle,
            created_at=str(payload.get("created_at") or ""),
            url=str(payload.get("url") or ""),
            body_text=body_text,
            is_reply=payload.get("in_reply_to_id") is not None,
            is_reblog=isinstance(payload.get("reblog"), dict),
            media_attachments=tuple(media_attachments),
            raw_payload=payload,
        )
