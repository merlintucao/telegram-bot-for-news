from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from .models import SourcePost


@dataclass(slots=True)
class SourceTermRule:
    pattern: str
    terms: tuple[str, ...]


@dataclass(slots=True)
class FilterDecision:
    should_deliver: bool
    reason: str | None = None


class PostFilter:
    def __init__(
        self,
        keyword_rules: tuple[SourceTermRule, ...],
        category_rules: tuple[SourceTermRule, ...],
    ) -> None:
        self.keyword_rules = keyword_rules
        self.category_rules = category_rules

    def evaluate(self, post: SourcePost) -> FilterDecision:
        keyword_terms = self._terms_for_source(post.source_id, self.keyword_rules)
        category_terms = self._terms_for_source(post.source_id, self.category_rules)

        if keyword_terms is not None:
            haystack = "\n".join(
                value
                for value in (
                    post.body_text,
                    post.url,
                    post.account_handle,
                    post.source_name,
                )
                if value
            ).casefold()
            if not any(term in haystack for term in keyword_terms):
                return FilterDecision(
                    should_deliver=False,
                    reason="keyword filter",
                )

        if category_terms is not None:
            post_categories = {category.casefold() for category in post.categories}
            if not post_categories.intersection(category_terms):
                return FilterDecision(
                    should_deliver=False,
                    reason="category filter",
                )

        return FilterDecision(should_deliver=True)

    @staticmethod
    def _terms_for_source(
        source_id: str,
        rules: tuple[SourceTermRule, ...],
    ) -> tuple[str, ...] | None:
        for rule in rules:
            if fnmatch.fnmatchcase(source_id, rule.pattern):
                return rule.terms
        return None


def build_post_filter(
    raw_keyword_rules: tuple[str, ...],
    raw_category_rules: tuple[str, ...],
) -> PostFilter:
    return PostFilter(
        keyword_rules=_parse_rules(raw_keyword_rules, label="SOURCE_KEYWORD_FILTERS"),
        category_rules=_parse_rules(raw_category_rules, label="SOURCE_CATEGORY_FILTERS"),
    )


def _parse_rules(raw_rules: tuple[str, ...], label: str) -> tuple[SourceTermRule, ...]:
    parsed_rules: list[SourceTermRule] = []
    for raw_rule in raw_rules:
        if "=" not in raw_rule:
            raise ValueError(f"Invalid {label} rule '{raw_rule}'. Expected pattern=term.")

        pattern, raw_terms = raw_rule.split("=", 1)
        pattern = pattern.strip()
        terms = tuple(part.strip().casefold() for part in raw_terms.split("|") if part.strip())

        if not pattern:
            raise ValueError(f"Invalid {label} rule '{raw_rule}'. Pattern is empty.")
        if not terms:
            raise ValueError(f"Invalid {label} rule '{raw_rule}'. At least one term is required.")

        parsed_rules.append(SourceTermRule(pattern=pattern, terms=terms))

    return tuple(parsed_rules)
