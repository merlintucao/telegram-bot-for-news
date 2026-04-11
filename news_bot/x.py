from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import re
import sqlite3
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

from .config import AppConfig
from .cookies import load_cookie_jar
from .models import MediaAttachment, SourcePost
from .source_types import SourceError, SourceProbeResult

LOGGER = logging.getLogger(__name__)

_STATUS_URL_RE = re.compile(r"https?://(?:www\.)?x\.com/([^/]+)/status/(\d+)")
_X_USER_BY_SCREEN_OP = "IGgvgiOx4QZndDHuD3x9TQ/UserByScreenName"
_X_USER_TWEETS_OP = "x3B_xLqC0yZawOB7WQhaVQ/UserTweets"
_X_USER_FEATURES: dict[str, Any] = {
    "hidden_profile_subscriptions_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}
_X_USER_FIELD_TOGGLES: dict[str, Any] = {
    "withPayments": False,
    "withAuxiliaryUserLabels": True,
}
_X_TWEETS_FEATURES: dict[str, Any] = {
    "rweb_video_screen_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_jetfuel_frame": False,
    "responsive_web_grok_share_attachment_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}
_X_TWEETS_FIELD_TOGGLES: dict[str, Any] = {
    "withArticleRichContentState": True,
    "withArticlePlainText": False,
    "withGrokAnalyze": False,
    "withDisallowedReplyControls": False,
}


def _cookie_jar_to_playwright_cookies(jar: CookieJar) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for cookie in jar:
        payload: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
        }
        if cookie.expires is not None:
            payload["expires"] = float(cookie.expires)
        cookies.append(payload)
    return cookies


def _cookie_jar_to_twscrape_cookies(jar: CookieJar) -> str:
    payload = [{"name": cookie.name, "value": cookie.value} for cookie in jar]
    return json.dumps(payload, ensure_ascii=True)


def _normalize_x_created_at(raw_value: str) -> str:
    value = (raw_value or "").strip()
    if not value:
        return value
    if "T" in value:
        return value
    try:
        return email.utils.parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError, IndexError, OverflowError):
        return value


def _get_by_path(obj: Any, path: str) -> Any:
    current = obj
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _build_x_body_text(main_text: str, quote_text: str) -> str:
    main = main_text.strip()
    quote = quote_text.strip()
    if main and quote:
        return f"{main}\n\nQuoted context: {quote}"
    return main or quote


def _canonical_status_url(url: str, fallback_handle: str) -> tuple[str, str]:
    match = _STATUS_URL_RE.search(url or "")
    if not match:
        raise SourceError(f"X post is missing a canonical status URL: {url!r}")
    handle, post_id = match.group(1), match.group(2)
    return (f"https://x.com/{handle}/status/{post_id}", post_id)


def _normalize_media_attachments(items: list[dict[str, Any]]) -> tuple[MediaAttachment, ...]:
    attachments: list[MediaAttachment] = []
    for item in items:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        attachments.append(
            MediaAttachment(
                kind=str(item.get("kind") or "image").strip().lower() or "image",
                url=url,
                preview_url=str(item.get("preview_url") or "").strip() or None,
                description=str(item.get("description") or "").strip() or None,
            )
        )
    return tuple(attachments)


def _unwrap_x_tweet_result(result: Any) -> dict[str, Any] | None:
    current = result
    while isinstance(current, dict):
        typename = current.get("__typename")
        if typename == "Tweet":
            return current
        next_result = current.get("tweet") or current.get("result")
        if next_result is current:
            break
        current = next_result
    return current if isinstance(current, dict) else None


def _screen_name_from_x_user_result(user_result: Any, default_handle: str) -> str:
    user = user_result if isinstance(user_result, dict) else {}
    return (
        str(
            _get_by_path(user, "core.screen_name")
            or user.get("screen_name")
            or _get_by_path(user, "legacy.screen_name")
            or default_handle
        ).strip()
        or default_handle
    )


def _tweet_text_from_x_result(tweet: dict[str, Any]) -> str:
    return str(
        _get_by_path(tweet, "note_tweet.note_tweet_results.result.text")
        or _get_by_path(tweet, "legacy.full_text")
        or tweet.get("full_text")
        or tweet.get("text")
        or ""
    ).strip()


def _extract_x_media_items(tweet: dict[str, Any]) -> list[dict[str, Any]]:
    entities = _get_by_path(tweet, "legacy.extended_entities.media") or _get_by_path(
        tweet, "legacy.entities.media"
    ) or []
    items: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        kind = str(entity.get("type") or "photo").strip().lower()
        media_url = str(entity.get("media_url_https") or entity.get("media_url") or "").strip()
        expanded_url = str(entity.get("expanded_url") or "").strip()
        preview_url = media_url or expanded_url
        if kind == "photo":
            if preview_url:
                items.append({"kind": "image", "url": preview_url})
            continue

        video_info = entity.get("video_info") if isinstance(entity.get("video_info"), dict) else {}
        variants = video_info.get("variants") if isinstance(video_info.get("variants"), list) else []
        best_variant_url = ""
        best_bitrate = -1
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            variant_url = str(variant.get("url") or "").strip()
            if not variant_url:
                continue
            bitrate = int(variant.get("bitrate") or 0)
            if bitrate >= best_bitrate:
                best_variant_url = variant_url
                best_bitrate = bitrate
        if best_variant_url or preview_url:
            items.append(
                {
                    "kind": "video",
                    "url": best_variant_url or preview_url,
                    "preview_url": preview_url or None,
                }
            )
    return items


def _normalize_x_tweet_result(
    tweet_result: Any,
    *,
    default_handle: str,
) -> dict[str, Any] | None:
    tweet = _unwrap_x_tweet_result(tweet_result)
    if not isinstance(tweet, dict):
        return None

    user_result = _get_by_path(tweet, "core.user_results.result") or {}
    handle = _screen_name_from_x_user_result(user_result, default_handle)
    post_id = str(tweet.get("rest_id") or tweet.get("id_str") or tweet.get("id") or "").strip()
    if not post_id:
        return None

    text = _tweet_text_from_x_result(tweet)
    quoted_result = _unwrap_x_tweet_result(_get_by_path(tweet, "quoted_status_result.result"))
    quote_text = _tweet_text_from_x_result(quoted_result) if isinstance(quoted_result, dict) else ""
    quote_handle = (
        _screen_name_from_x_user_result(_get_by_path(quoted_result, "core.user_results.result"), default_handle)
        if isinstance(quoted_result, dict)
        else default_handle
    )
    quote_id = (
        str(quoted_result.get("rest_id") or quoted_result.get("id_str") or "").strip()
        if isinstance(quoted_result, dict)
        else ""
    )
    legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else {}
    is_reply = (
        legacy.get("in_reply_to_status_id_str") is not None
        or tweet.get("in_reply_to_status_id_str") is not None
    )
    is_reblog = bool(tweet.get("retweeted_status_result")) and not text
    created_at = str(
        legacy.get("created_at")
        or tweet.get("created_at")
        or ""
    ).strip()

    if not text and not quote_text:
        return None

    return {
        "url": f"https://x.com/{handle}/status/{post_id}",
        "handle": handle,
        "text": text,
        "quote_text": quote_text,
        "quote_url": f"https://x.com/{quote_handle}/status/{quote_id}" if quote_id else "",
        "created_at": created_at,
        "media": _extract_x_media_items(tweet),
        "is_reply": is_reply,
        "is_reblog": is_reblog,
        "is_ad": False,
    }


def _extract_x_timeline_items_from_graphql(
    payload: dict[str, Any],
    *,
    default_handle: str,
) -> list[dict[str, Any]]:
    instructions = (
        _get_by_path(payload, "data.user.result.timeline.timeline.instructions")
        or _get_by_path(payload, "data.user.result.timeline_v2.timeline.instructions")
        or []
    )
    items: list[dict[str, Any]] = []
    for instruction in instructions:
        if not isinstance(instruction, dict):
            continue
        entries = list(instruction.get("entries") or [])
        single_entry = instruction.get("entry")
        if isinstance(single_entry, dict):
            entries.append(single_entry)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content") if isinstance(entry.get("content"), dict) else {}
            item_content = content.get("itemContent") if isinstance(content.get("itemContent"), dict) else {}
            tweet_result = _get_by_path(item_content, "tweet_results.result")
            normalized = _normalize_x_tweet_result(tweet_result, default_handle=default_handle)
            if normalized is not None:
                items.append(normalized)
    return items


def _normalize_x_item(
    item: dict[str, Any],
    *,
    source_id: str,
    source_name: str,
    default_handle: str,
) -> SourcePost | None:
    if item.get("is_ad"):
        return None
    if item.get("is_reply"):
        return None
    if item.get("is_reblog"):
        return None

    canonical_url, post_id = _canonical_status_url(str(item.get("url") or ""), default_handle)
    main_text = str(item.get("text") or "").strip()
    quote_text = str(item.get("quote_text") or "").strip()
    body_text = _build_x_body_text(main_text, quote_text)
    if not body_text:
        return None

    handle = str(item.get("handle") or default_handle).strip() or default_handle
    return SourcePost(
        source_id=source_id,
        source_name=source_name,
        id=post_id,
        account_handle=handle,
        created_at=_normalize_x_created_at(str(item.get("created_at") or "")),
        url=canonical_url,
        body_text=body_text,
        is_reply=False,
        is_reblog=False,
        media_attachments=_normalize_media_attachments(list(item.get("media") or [])),
        raw_payload={
            "id": post_id,
            "url": canonical_url,
            "text": main_text,
            "quote_text": quote_text,
            "is_quote": bool(quote_text),
            "quoted_url": str(item.get("quote_url") or "").strip() or None,
        },
    )


def _filter_newer_posts(posts: list[SourcePost], since_id: str | None) -> list[SourcePost]:
    if not since_id:
        return posts
    if since_id.isdigit():
        since_value = int(since_id)
        return [
            post
            for post in posts
            if not post.id.isdigit() or int(post.id) > since_value
        ]
    return [post for post in posts if post.id != since_id]


def _dedupe_and_sort_posts(posts: list[SourcePost]) -> list[SourcePost]:
    deduped: dict[str, SourcePost] = {}
    for post in posts:
        if post.id not in deduped:
            deduped[post.id] = post
    return sorted(deduped.values(), key=lambda post: post.sort_key, reverse=True)


class XKobeissiLetterSource:
    source_id = "x:kobeissiletter"
    source_name = "X | Kobeissi Letter"
    account_handle = "KobeissiLetter"

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.profile_url = config.x_kobeissi_url

    def probe(self) -> SourceProbeResult:
        posts = self.fetch_posts(limit=1)
        latest = posts[0].id if posts else "no posts returned"
        auth_detail = self._auth_detail()
        return SourceProbeResult(
            source_id=self.source_id,
            source_name=self.source_name,
            detail_lines=(
                f"profile url: {self.profile_url}",
                f"auth: {auth_detail}",
                f"latest post id: {latest}",
            ),
        )

    def fetch_posts(
        self,
        since_id: str | None = None,
        limit: int | None = None,
    ) -> list[SourcePost]:
        raw_items = self._fetch_timeline_items(max(limit or self.config.x_poll_limit, self.config.x_poll_limit))
        posts = [
            post
            for item in raw_items
            if (post := _normalize_x_item(
                item,
                source_id=self.source_id,
                source_name=self.source_name,
                default_handle=self.account_handle,
            ))
            is not None
        ]
        posts = _dedupe_and_sort_posts(posts)
        posts = _filter_newer_posts(posts, since_id)
        if limit is not None:
            posts = posts[:limit]
        return posts

    def _auth_detail(self) -> str:
        if self.config.x_backend == "twscrape":
            return (
                f"twscrape:{self.config.x_twscrape_db_path}"
                f" ({self.config.x_twscrape_account_username})"
            )
        if self.config.x_profile_dir:
            return f"profile:{self.config.x_profile_dir}"
        if self.config.x_cookies_file:
            return f"cookies:{self.config.x_cookies_file}"
        return self.config.x_auth_mode

    def _fetch_timeline_items(self, max_items: int) -> list[dict[str, Any]]:
        if self.config.x_backend == "twscrape":
            return self._fetch_timeline_items_twscrape(max_items)
        return self._fetch_timeline_items_playwright(max_items)

    def _fetch_timeline_items_twscrape(self, max_items: int) -> list[dict[str, Any]]:
        if self.config.x_auth_mode == "profile":
            raise SourceError("twscrape backend does not support X_PROFILE_DIR; use X_AUTH_MODE=cookies.")
        if self.config.x_cookies_file is None:
            raise SourceError("X_COOKIES_FILE is required when X_BACKEND=twscrape.")

        try:
            from twscrape import API
            from twscrape.accounts_pool import parse_cookies
            from twscrape.utils import encode_params
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise SourceError(
                "twscrape is required for X_BACKEND=twscrape. Install it with "
                "`python3 -m pip install twscrape`."
            ) from exc
        try:
            import bs4
            import httpx
            from x_client_transaction import ClientTransaction
            from x_client_transaction.utils import Math
            from twscrape.xclid import get_tw_page_text
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise SourceError(
                "X_BACKEND=twscrape also requires beautifulsoup4, httpx, and xclienttransaction."
            ) from exc

        async def _run() -> list[dict[str, Any]]:
            from functools import reduce

            db_file = self._prepare_twscrape_db_file()
            db_path = str(db_file)
            api = API(db_path, raise_when_no_account=True)
            await self._ensure_twscrape_account(api, parse_cookies=parse_cookies)

            home_page_text = await get_tw_page_text(self.profile_url)
            soup = bs4.BeautifulSoup(home_page_text, "html.parser")
            hrefs = [tag.get("href") for tag in soup.find_all("link", href=True)]
            main_js_url = next(
                (
                    href
                    for href in hrefs
                    if isinstance(href, str) and "/main." in href and href.endswith(".js")
                ),
                None,
            )
            if not main_js_url:
                raise SourceError("Could not locate X main.js while preparing twscrape request headers.")

            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as tmp_client:
                main_js_text = (await tmp_client.get(main_js_url)).text

            original_get_animation_key = ClientTransaction.get_animation_key

            def _patched_get_animation_key(self: Any, key_bytes: bytes, home_page_response: Any) -> str:
                total_time = 4096
                row_index = key_bytes[self.row_index] % 16
                values = [key_bytes[index] % 16 for index in self.key_bytes_indices]
                frame_time = reduce(lambda a, b: a * b, values, 1)
                frame_time = Math.round(frame_time / 10) * 10
                arr = self.get_2d_array(key_bytes=key_bytes, home_page_response=home_page_response)
                frame_row = arr[row_index]
                target_time = float(frame_time) / total_time
                return self.animate(frames=frame_row, target_time=target_time)

            ClientTransaction.get_animation_key = _patched_get_animation_key
            try:
                transaction_client = ClientTransaction(soup, main_js_text)
                account = await api.pool.get_for_queue_or_wait("UserByScreenName")
                client = account.make_client()
                try:
                    user_path = f"/i/api/graphql/{_X_USER_BY_SCREEN_OP}"
                    client.headers["x-client-transaction-id"] = (
                        transaction_client.generate_transaction_id("GET", user_path)
                    )
                    user_response = await client.get(
                        f"https://x.com{user_path}",
                        params=encode_params(
                            {
                                "variables": {
                                    "screen_name": self.account_handle,
                                    "withGrokTranslatedBio": True,
                                },
                                "features": _X_USER_FEATURES,
                                "fieldToggles": _X_USER_FIELD_TOGGLES,
                            }
                        ),
                    )
                    if user_response.status_code != 200:
                        raise SourceError(
                            f"Failed to resolve X user via twscrape backend: HTTP {user_response.status_code}"
                        )
                    rest_id = str(
                        _get_by_path(user_response.json(), "data.user.result.rest_id") or ""
                    ).strip()
                    if not rest_id:
                        raise SourceError("Failed to resolve X rest_id from UserByScreenName response.")

                    tweets_path = f"/i/api/graphql/{_X_USER_TWEETS_OP}"
                    client.headers["x-client-transaction-id"] = (
                        transaction_client.generate_transaction_id("GET", tweets_path)
                    )
                    tweets_response = await client.get(
                        f"https://x.com{tweets_path}",
                        params=encode_params(
                            {
                                "variables": {
                                    "userId": rest_id,
                                    "count": max_items,
                                    "includePromotedContent": False,
                                    "withQuickPromoteEligibilityTweetFields": True,
                                    "withVoice": True,
                                    "withV2Timeline": True,
                                },
                                "features": _X_TWEETS_FEATURES,
                                "fieldToggles": _X_TWEETS_FIELD_TOGGLES,
                            }
                        ),
                    )
                    if tweets_response.status_code != 200:
                        raise SourceError(
                            f"Failed to fetch X timeline via twscrape backend: HTTP {tweets_response.status_code}"
                        )
                    return _extract_x_timeline_items_from_graphql(
                        tweets_response.json(),
                        default_handle=self.account_handle,
                    )
                finally:
                    await client.aclose()
            finally:
                ClientTransaction.get_animation_key = original_get_animation_key

        try:
            return asyncio.run(_run())
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(
                f"Failed to read X timeline via twscrape for {self.profile_url}: {exc}"
            ) from exc

    def _prepare_twscrape_db_file(self) -> Path:
        db_file = Path(self.config.x_twscrape_db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        # Reset clearly broken DBs, but preserve healthy shared state across polls.
        if db_file.exists():
            should_reset = False
            if db_file.stat().st_size == 0:
                should_reset = True
            else:
                try:
                    with sqlite3.connect(db_file) as connection:
                        row = connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'"
                        ).fetchone()
                    should_reset = row is None
                except sqlite3.Error:
                    should_reset = True
            if should_reset:
                db_file.unlink()
        return db_file

    async def _ensure_twscrape_account(self, api: Any, parse_cookies: Any | None = None) -> None:
        if self.config.x_cookies_file is None:
            raise SourceError("X_COOKIES_FILE is required when X_BACKEND=twscrape.")

        jar = load_cookie_jar(self.config.x_cookies_file)
        cookies = list(jar)
        if not cookies:
            raise SourceError(
                f"X cookies are empty in {self.config.x_cookies_file}; "
                "re-export a logged-in X session."
            )

        username = self.config.x_twscrape_account_username
        cookies_json = _cookie_jar_to_twscrape_cookies(jar)
        account = await api.pool.get_account(username)
        if account is None:
            await api.pool.add_account(
                username=username,
                password="unused",
                email="unused@example.com",
                email_password="unused",
                user_agent=self.config.user_agent,
                cookies=cookies_json,
            )
            return

        account.user_agent = self.config.user_agent
        account.cookies = (
            parse_cookies(cookies_json) if parse_cookies is not None else json.loads(cookies_json)
        )
        account.active = "ct0" in account.cookies
        account.locks = {}
        account.last_used = None
        account.error_msg = None
        await api.pool.save(account)

    def _fetch_timeline_items_playwright(self, max_items: int) -> list[dict[str, Any]]:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise SourceError(
                "Playwright is required for X scraping. Install it with "
                "`python3 -m pip install playwright && playwright install chromium`."
            ) from exc

        if self.config.x_auth_mode == "profile" and not self.config.x_profile_dir:
            raise SourceError("X_PROFILE_DIR is required when X_AUTH_MODE=profile.")
        if self.config.x_auth_mode == "cookies" and not self.config.x_cookies_file:
            raise SourceError("X_COOKIES_FILE is required when X_AUTH_MODE=cookies.")
        if (
            self.config.x_auth_mode == "auto"
            and self.config.x_profile_dir is None
            and self.config.x_cookies_file is None
        ):
            raise SourceError("X source requires X_PROFILE_DIR or X_COOKIES_FILE.")

        timeout_ms = max(1000, self.config.request_timeout_seconds * 1000)
        with sync_playwright() as playwright:  # pragma: no cover - browser path
            if self.config.x_profile_dir:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.config.x_profile_dir),
                    headless=self.config.x_headless,
                    viewport={"width": 1440, "height": 2400},
                )
                browser = None
            else:
                browser = playwright.chromium.launch(headless=self.config.x_headless)
                context = browser.new_context(viewport={"width": 1440, "height": 2400})
                if self.config.x_cookies_file:
                    context.add_cookies(
                        _cookie_jar_to_playwright_cookies(
                            load_cookie_jar(self.config.x_cookies_file)
                        )
                    )

            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(self.profile_url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_selector("article[data-testid='tweet']", timeout=timeout_ms)

                previous_count = -1
                stable_rounds = 0
                collected: list[dict[str, Any]] = []
                for _ in range(8):
                    collected = page.evaluate(_TIMELINE_EXTRACTION_SCRIPT)
                    if len(collected) >= max_items:
                        break
                    if len(collected) == previous_count:
                        stable_rounds += 1
                        if stable_rounds >= 2:
                            break
                    else:
                        stable_rounds = 0
                    previous_count = len(collected)
                    page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                    page.wait_for_timeout(750)
                return list(collected)
            except Exception as exc:
                raise SourceError(f"Failed to read X timeline for {self.profile_url}: {exc}") from exc
            finally:
                context.close()
                if browser is not None:
                    browser.close()


def _normalize_twscrape_tweet(tweet: Any, *, default_handle: str) -> dict[str, Any] | None:
    user = getattr(tweet, "user", None)
    handle = getattr(user, "username", None) or default_handle
    url = getattr(tweet, "url", "") or f"https://x.com/{handle}/status/{getattr(tweet, 'id', '')}"
    text = str(getattr(tweet, "rawContent", "") or "").strip()
    quoted = getattr(tweet, "quotedTweet", None)
    quote_text = str(getattr(quoted, "rawContent", "") or "").strip() if quoted else ""
    in_reply_to = getattr(tweet, "inReplyToTweetId", None)
    retweeted = getattr(tweet, "retweetedTweet", None)

    media_items: list[dict[str, Any]] = []
    media = getattr(tweet, "media", None)
    if media is not None:
        for photo in getattr(media, "photos", []) or []:
            media_items.append({"kind": "image", "url": str(getattr(photo, "url", "") or "").strip()})
        for video in getattr(media, "videos", []) or []:
            media_items.append(
                {
                    "kind": "video",
                    "url": str(getattr(video, "thumbnailUrl", "") or "").strip(),
                    "preview_url": str(getattr(video, "thumbnailUrl", "") or "").strip(),
                }
            )
        for animated in getattr(media, "animated", []) or []:
            media_items.append(
                {
                    "kind": "video",
                    "url": str(getattr(animated, "videoUrl", "") or "").strip(),
                    "preview_url": str(getattr(animated, "thumbnailUrl", "") or "").strip(),
                }
            )

    if not text and not quote_text:
        return None

    return {
        "url": url,
        "handle": handle,
        "text": text,
        "quote_text": quote_text,
        "quote_url": str(getattr(quoted, "url", "") or "").strip() if quoted else "",
        "created_at": getattr(tweet, "date", None).isoformat() if getattr(tweet, "date", None) else "",
        "media": media_items,
        "is_reply": in_reply_to is not None,
        "is_reblog": retweeted is not None and not text,
        "is_ad": False,
    }


_TIMELINE_EXTRACTION_SCRIPT = """
() => {
  const statusIdFromUrl = (value) => {
    if (!value) return null;
    const match = value.match(/\\/status\\/(\\d+)/);
    return match ? match[1] : null;
  };

  const normalizeUrl = (href) => {
    if (!href) return "";
    try {
      const url = new URL(href, window.location.origin);
      return url.toString();
    } catch (_err) {
      return href;
    }
  };

  return Array.from(document.querySelectorAll("article[data-testid='tweet']")).map((article) => {
    const timeLink = article.querySelector("time")?.closest("a");
    const canonicalUrl = normalizeUrl(timeLink?.href || "");
    const statusId = statusIdFromUrl(canonicalUrl);
    const textNodes = Array.from(article.querySelectorAll("div[data-testid='tweetText']"))
      .map((node) => (node.innerText || "").trim())
      .filter(Boolean);
    const socialContext = (article.querySelector("[data-testid='socialContext']")?.innerText || "").trim();
    const allText = (article.innerText || "").trim();
    const statusLinks = Array.from(article.querySelectorAll("a[href*='/status/']"))
      .map((anchor) => normalizeUrl(anchor.href))
      .filter((href) => !!statusIdFromUrl(href));
    const uniqueStatusLinks = Array.from(new Set(statusLinks));
    const quoteUrl = uniqueStatusLinks.find((href) => href !== canonicalUrl) || "";
    const quoteText = textNodes.length > 1 ? textNodes.slice(1).join("\\n\\n") : "";
    const images = Array.from(article.querySelectorAll("[data-testid='tweetPhoto'] img"))
      .map((img) => ({ kind: "image", url: img.currentSrc || img.src || "" }))
      .filter((item) => item.url);
    const videos = Array.from(article.querySelectorAll("video"))
      .map((video) => ({
        kind: "video",
        url: video.currentSrc || video.src || "",
        preview_url: video.getAttribute("poster") || "",
      }))
      .filter((item) => item.url || item.preview_url);

    return {
      id: statusId,
      url: canonicalUrl,
      created_at: article.querySelector("time")?.getAttribute("datetime") || "",
      text: textNodes[0] || "",
      quote_text: quoteText,
      quote_url: quoteUrl,
      handle: (canonicalUrl.match(/x\\.com\\/([^/]+)\\/status\\//) || [null, ""])[1],
      is_reply: /replying to/i.test(allText),
      is_reblog: /reposted/i.test(socialContext),
      is_ad: !statusId || /promoted/i.test(allText),
      media: images.concat(videos),
    };
  });
}
"""
