from __future__ import annotations

import socket
from collections.abc import Iterable
from urllib.parse import urlparse

from .config import AppConfig


def iter_probe_hosts(config: AppConfig) -> list[str]:
    hosts: list[str] = []
    seen: set[str] = set()

    def add_url(raw_url: str) -> None:
        parsed = urlparse(raw_url)
        host = parsed.hostname
        if not host or host in seen:
            return
        seen.add(host)
        hosts.append(host)

    normalized_sources = {name.lower() for name in config.enabled_sources}

    if normalized_sources.intersection({"truthsocial", "truthsocial_trump"}):
        add_url(config.truthsocial_base_url)
        for feed_url in config.truthsocial_fallback_feed_urls:
            add_url(feed_url)
    if "reuters_rss" in normalized_sources:
        add_url(config.reuters_rss_url)
    if "investing_rss" in normalized_sources:
        add_url(config.investing_rss_url)
    if "ap_world_rss" in normalized_sources:
        add_url(config.ap_world_rss_url)
    if "ft_rss" in normalized_sources:
        add_url(config.ft_rss_url)
    if "x_kobeissi_letter" in normalized_sources:
        add_url(config.x_kobeissi_url)

    for feed_url in config.rss_feed_urls:
        add_url(feed_url)

    return hosts


def looks_like_dns_resolution_failure(detail: str | None) -> bool:
    if not detail:
        return False
    normalized = detail.lower()
    return (
        "nodename nor servname provided" in normalized
        or "name or service not known" in normalized
        or "dns failed" in normalized
    )


def probe_hosts(config: AppConfig) -> tuple[bool, list[str]]:
    probe_hosts = iter_probe_hosts(config)
    if not probe_hosts:
        return True, ["- Network probes: no external hosts configured"]

    lines = ["- Network probes:"]
    ok = True
    dns_failures = 0
    timeout_seconds = max(1.0, min(float(config.request_timeout_seconds), 3.0))

    for host in probe_hosts:
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            lines.append(f"  {host}: dns failed ({exc})")
            dns_failures += 1
            ok = False
            continue

        try:
            with socket.create_connection((host, 443), timeout=timeout_seconds):
                lines.append(f"  {host}: dns ok, tcp ok")
        except OSError as exc:
            lines.append(f"  {host}: dns ok, tcp failed ({exc})")
            ok = False

    if dns_failures == len(probe_hosts):
        lines.append(
            "  likely cause: DNS resolution failure in this runtime or machine environment"
        )

    return ok, lines


def has_global_dns_outage(config: AppConfig) -> bool:
    probe_hosts = iter_probe_hosts(config)
    if not probe_hosts:
        return False

    for host in probe_hosts:
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            continue
        return False

    return True


def summarize_status_network_issue(details: Iterable[str | None]) -> str | None:
    detail_list = list(details)
    if not detail_list:
        return None
    if not all(looks_like_dns_resolution_failure(detail) for detail in detail_list):
        return None
    return (
        "- Health hint: all sources are currently failing with DNS resolution errors; "
        "this points to a runtime or machine networking issue rather than a single source bug"
    )
