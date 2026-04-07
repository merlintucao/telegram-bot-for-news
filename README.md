# Telegram Bot for News

This bot polls Donald Trump's Truth Social account and can also ingest RSS/Atom news feeds, then forwards new items to a Telegram chat.

## What this version does

- Polls Donald Trump's public Truth Social account API without requiring login by default.
- Can optionally reuse an authenticated Truth Social browser session as a fallback when anonymous polling is blocked.
- Deduplicates deliveries with a local SQLite database.
- Sends each new post to Telegram, including photo/video attachments when Telegram can fetch them.
- Formats each message with a Vietnamese translation, the original text, and the original source link.
- Uses media captions so image and video posts show a visible Telegram preview when possible.
- Boots safely on first run by recording the latest post instead of replaying the whole backlog.
- Includes a `doctor` command to validate cookies, config, and live Truth Social access.
- Reloads the cookie export automatically when the cookie file changes on disk.
- Uses a source registry so future news/social providers can plug into the same polling pipeline.
- Supports RSS/Atom feeds as the second real source type.
- Supports per-source Telegram routing so different feeds can go to different chats.
- Supports per-source keyword and category filters for selective delivery.
- Stores run history and source activity so you can inspect status from the CLI.
- Can alert an operator chat when a source keeps failing for multiple polls in a row.
- Retries flaky source polls and keeps healthy sources running when one source is down.

## Quick start

1. Copy `.env.example` to `.env`.
2. Create a Telegram bot with BotFather and fill in `TELEGRAM_BOT_TOKEN`.
3. Leave `ENABLED_SOURCES=truthsocial_trump` for Truth Social only, or use `ENABLED_SOURCES=truthsocial_trump,rss` to also ingest RSS feeds.
4. Set `TELEGRAM_CHAT_ID` to a chat ID or channel username like `@my_channel`.
5. Leave `TRUTHSOCIAL_ACCOUNT_ID=107780257626128497` unless you are tracking a different Truth Social account.
6. If the public endpoint gets blocked in your environment, optionally export Truth Social cookies to `truthsocial_cookies.json` or `truthsocial_cookies.txt` and keep `TRUTHSOCIAL_AUTH_MODE=auto`.
7. If you enabled RSS, set `RSS_FEED_URLS` to one or more feed URLs separated by commas.
8. Run a dry run:

```bash
python3 -m news_bot once --dry-run
```

9. If the output looks correct, start the loop:

```bash
python3 -m news_bot run
```

If you want `once` to behave like a deployment smoke test, it now exits non-zero when any source fails.

## Truth Social access modes

The bot supports three Truth Social access modes:

- `auto`: default; use cookies when a cookie file is present, otherwise poll anonymously
- `public`: always poll anonymously and ignore cookies
- `cookies`: require a cookie file and always use it

For the current Trump-only MVP, `.env.example` includes the known public account id `107780257626128497`, so the bot can skip the account lookup call on first run.

## Cookie file formats

Cookies are optional in `auto` and `public` mode. When you use them, the poller accepts either:

- a Netscape/Mozilla cookie file such as one exported by a browser extension
- a JSON file containing either a top-level `cookies` array or a plain array of cookies

Supported JSON cookie fields are `name`, `value`, `domain`, `path`, `expires`, `httpOnly`, and `secure`.

## Sources

The bot now loads sources through `ENABLED_SOURCES`.

- `truthsocial_trump`: polls Donald Trump's Truth Social account
- `rss`: polls one or more RSS/Atom feeds listed in `RSS_FEED_URLS`

Additional providers can be added later without changing the delivery or state-tracking pipeline.

## Routing

By default, every source posts to `TELEGRAM_CHAT_ID`.
If every source is covered by `SOURCE_CHAT_ROUTES`, the default chat can be left empty.

To override destinations per source, set `SOURCE_CHAT_ROUTES` using semicolon-separated rules:

```bash
SOURCE_CHAT_ROUTES=truthsocial:*=@trump_news;rss:*=@news_feeds
```

Rules are checked from left to right, and the first match wins. Pattern matching uses shell-style wildcards.

Examples:

- route all Truth Social sources to one chat: `truthsocial:*=@trump_news`
- route all RSS feeds to one chat: `rss:*=@rss_news`
- route one specific source to multiple chats: `rss:example-com-feed=@breaking_news|@all_news`

Use `python3 -m news_bot doctor --skip-network` to see the configured source IDs and where they will route.

## Filtering

You can selectively deliver posts with source-specific keyword and category filters.

Rules are semicolon-separated and use the same source pattern matching as routing:

```bash
SOURCE_KEYWORD_FILTERS=rss:*=election|market;truthsocial:*=tariff|campaign
SOURCE_CATEGORY_FILTERS=rss:*=politics|world
```

Behavior:

- rules are checked from left to right and the first matching rule wins
- keyword filters match against the post body, URL, source name, and publisher name
- category filters match RSS/Atom categories when the feed provides them
- if both a keyword rule and a category rule apply to a source, a post must satisfy both

Examples:

- only send political or world RSS stories: `SOURCE_CATEGORY_FILTERS=rss:*=politics|world`
- only send Trump posts mentioning a topic: `SOURCE_KEYWORD_FILTERS=truthsocial:*=border|trade`
- only send one feed when it mentions AI: `SOURCE_KEYWORD_FILTERS=rss:example-com-feed=ai|artificial intelligence`

## Useful commands

```bash
python3 -m unittest discover -s tests -v
python3 -m news_bot once --dry-run
python3 -m news_bot run
python3 -m news_bot doctor
python3 -m news_bot notify --notify-target both
python3 -m news_bot notify --notify-target routed
python3 -m news_bot status
python3 -m news_bot status --json
```

## Translation and message format

By default, the bot translates post bodies to Vietnamese before delivery and keeps the original text underneath it.

Relevant settings:

```bash
TRANSLATION_TARGET_LANGUAGE=vi
TRANSLATION_ENDPOINT=https://translate.googleapis.com/translate_a/single
```

Delivered post format:

- `Vietnamese:` translated text
- `Original:` source text
- `Original link:` canonical Truth Social or article URL

For image and video posts, the bot also adds a short caption to the media message so Telegram can show a thumbnail/preview when supported.

## Failure alerts

To notify an operator chat when a source repeatedly fails, set:

```bash
TELEGRAM_ALERT_CHAT_ID=@ops_channel
SOURCE_FAILURE_ALERT_THRESHOLD=3
```

Behavior:

- alerts are sent after a source reaches the configured number of consecutive failures
- only one alert is sent per failure streak
- a successful poll resets the streak and re-enables future alerts
- if a failure alert was sent, the bot sends a recovery alert when that source starts succeeding again

## Retry and recovery

The bot retries temporary source failures before counting the poll as failed:

```bash
SOURCE_RETRY_ATTEMPTS=3
SOURCE_RETRY_BACKOFF_SECONDS=2
CONTINUE_ON_SOURCE_ERROR=true
```

Behavior:

- `SOURCE_RETRY_ATTEMPTS` is the total number of tries per source in one poll cycle
- backoff doubles on each retry attempt
- with `CONTINUE_ON_SOURCE_ERROR=true`, one failing source does not block the others
- with `CONTINUE_ON_SOURCE_ERROR=false`, the poll cycle stops on the first unrecovered source failure

## Status

The bot stores run summaries and per-source events in the SQLite database at `STATE_DB_PATH`.

Use the status command to inspect:

- recent runs with fetched, sent, and filtered counts
- the latest checkpoint per source
- the last delivered item per source
- the latest source error, if a poll failed
- the current failure streak and latest success time per source
- recent filtered skips per source

```bash
python3 -m news_bot status
python3 -m news_bot status --status-limit 5
python3 -m news_bot status --json
```

## Telegram test messages

Use the notify command to confirm the bot can post to the configured Telegram chats:

```bash
python3 -m news_bot notify --notify-target main
python3 -m news_bot notify --notify-target alert
python3 -m news_bot notify --notify-target both --notify-message "news_bot deployment test"
python3 -m news_bot notify --notify-target routed
python3 -m news_bot notify --notify-target routed --notify-source "truthsocial:*"
```

`--notify-target routed` sends a test message to the actual Telegram destination chats resolved from `SOURCE_CHAT_ROUTES` and `TELEGRAM_CHAT_ID`.
Use `--notify-source` to narrow routed tests to one source id or wildcard pattern.

## Cookie refresh workflow

If the Truth Social session expires:

1. Log back into Truth Social in your browser.
2. Export fresh cookies to the same file path.
3. Leave the bot running. It reloads the cookie file automatically on the next poll cycle.

## Docker

Build and run with Docker Compose:

```bash
cp .env.example .env
docker compose up --build -d
docker compose logs -f
```

For Docker, keep `TRUTHSOCIAL_COOKIES_FILE` as a relative path inside the project, for example `truthsocial_cookies.json` or `cookies/truthsocial_cookies.txt`, if you want cookie fallback inside the container.

The sample `compose.yaml` mounts `./data` for SQLite state and can also mount a cookie file when you use one.

## systemd

A sample unit file is available at `deployment/systemd/news-bot.service`.

Typical install flow on Linux:

```bash
sudo cp deployment/systemd/news-bot.service /etc/systemd/system/news-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now news-bot
sudo systemctl status news-bot
```

## Notes

- Truth Social can change its anti-bot behavior at any time. This build now starts with the public account flow first and can fall back to cookies if anonymous polling is challenged.
- If you track `realDonaldTrump`, the config now defaults `TRUTHSOCIAL_ACCOUNT_ID` to `107780257626128497`. You can still override it manually in `.env`.
