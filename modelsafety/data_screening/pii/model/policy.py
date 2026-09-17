from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PolicyDecision:
    eligible: bool
    threshold: float | None
    excluded_reason: str | None


@dataclass(frozen=True)
class CandidatePolicy:
    version: int
    tier_thresholds: dict[str, float | None]
    label_thresholds: dict[str, float | None]
    control_markup_names: tuple[str, ...]
    ignored_exact_values: tuple[str, ...]

    @cached_property
    def exact_markup_pattern(self) -> re.Pattern[str]:
        names = "|".join(re.escape(name) for name in self.control_markup_names)
        return re.compile(rf"</?(?:{names})>?", flags=re.IGNORECASE)

    @cached_property
    def tag_pattern(self) -> re.Pattern[str]:
        names = "|".join(re.escape(name) for name in self.control_markup_names)
        return re.compile(
            rf"<\s*/?\s*(?:{names})\b[^>\n]*>",
            flags=re.IGNORECASE,
        )

    @classmethod
    def from_path(cls, path: Path) -> "CandidatePolicy":
        value = json.loads(path.read_text(encoding="utf-8"))
        tier_thresholds = {
            str(tier): _optional_threshold(threshold, f"tier {tier}")
            for tier, threshold in value["tier_thresholds"].items()
        }
        label_thresholds = {
            str(label): _optional_threshold(threshold, f"label {label}")
            for label, threshold in value.get("label_thresholds", {}).items()
        }
        names = tuple(
            sorted(
                {
                    str(name).strip().casefold()
                    for name in value.get("control_markup_names", [])
                    if str(name).strip()
                }
            )
        )
        if not names:
            raise ValueError("Policy must define at least one control markup name")
        return cls(
            version=int(value["version"]),
            tier_thresholds=tier_thresholds,
            label_thresholds=label_thresholds,
            control_markup_names=names,
            ignored_exact_values=tuple(
                sorted(
                    {
                        str(item).strip().casefold()
                        for item in value.get("ignored_exact_values", [])
                        if str(item).strip()
                    }
                )
            ),
        )

    def threshold_for(self, label: str, tier: str) -> float | None:
        if label in self.label_thresholds:
            return self.label_thresholds[label]
        if tier not in self.tier_thresholds:
            raise ValueError(f"Policy has no threshold for tier: {tier}")
        return self.tier_thresholds[tier]

    def evaluate(
        self,
        *,
        label: str,
        tier: str,
        confidence: float,
        span_text: str,
        source_text: str,
        start: int,
        end: int,
    ) -> PolicyDecision:
        threshold = self.threshold_for(label, tier)
        if threshold is None:
            return PolicyDecision(False, None, "contextual_tier")
        if self.is_control_markup(span_text, source_text, start, end):
            return PolicyDecision(False, threshold, "control_markup")
        if span_text.strip().casefold() in self.ignored_exact_values:
            return PolicyDecision(False, threshold, "known_non_pii_token")
        if confidence < threshold:
            return PolicyDecision(False, threshold, "below_confidence_threshold")
        return PolicyDecision(True, threshold, None)

    def is_control_markup(
        self,
        span_text: str,
        source_text: str,
        start: int,
        end: int,
    ) -> bool:
        compact = re.sub(r"\s+", "", span_text).casefold()
        if self.exact_markup_pattern.fullmatch(compact):
            return True
        return any(
            match.start() < end and start < match.end()
            for match in self.tag_pattern.finditer(source_text)
        )


def _threshold(value: Any, description: str) -> float:
    threshold = float(value)
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"Invalid confidence threshold for {description}: {value}")
    return threshold


def _optional_threshold(value: Any, description: str) -> float | None:
    if value is None:
        return None
    return _threshold(value, description)

