from __future__ import annotations

import fnmatch
from dataclasses import dataclass


@dataclass(slots=True)
class SourceRouteRule:
    pattern: str
    chat_ids: tuple[str, ...]


class SourceRouter:
    def __init__(self, default_chat_id: str, rules: tuple[SourceRouteRule, ...]) -> None:
        self.default_chat_id = default_chat_id
        self.rules = rules

    def destinations_for_source(self, source_id: str) -> tuple[str, ...]:
        for rule in self.rules:
            if fnmatch.fnmatchcase(source_id, rule.pattern):
                return rule.chat_ids

        if self.default_chat_id:
            return (self.default_chat_id,)

        return ()


def build_router(default_chat_id: str, raw_rules: tuple[str, ...]) -> SourceRouter:
    rules: list[SourceRouteRule] = []
    for raw_rule in raw_rules:
        if "=" not in raw_rule:
            raise ValueError(
                f"Invalid SOURCE_CHAT_ROUTES rule '{raw_rule}'. Expected pattern=chat_id."
            )

        pattern, destinations = raw_rule.split("=", 1)
        pattern = pattern.strip()
        chat_ids = tuple(part.strip() for part in destinations.split("|") if part.strip())

        if not pattern:
            raise ValueError(
                f"Invalid SOURCE_CHAT_ROUTES rule '{raw_rule}'. Pattern is empty."
            )
        if not chat_ids:
            raise ValueError(
                f"Invalid SOURCE_CHAT_ROUTES rule '{raw_rule}'. Destination chat id is empty."
            )

        rules.append(SourceRouteRule(pattern=pattern, chat_ids=chat_ids))

    return SourceRouter(default_chat_id=default_chat_id, rules=tuple(rules))
