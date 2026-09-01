"""Deterministic, context-bound rule layering for Director decisions.

Technical rules are immutable hard constraints.  Creative rules replace one
another by stable ``key`` using this precedence (high to low)::

    current_shot > episode_temporary > project_series > system_base

Technical hard rules are evaluated after that creative stack and are always
the final winner for a colliding key.

The resolver deliberately does not concatenate values for the same key: one
key has one effective value and an auditable source.  Project and episode
bindings are checked before applicability so a mixed rule store cannot leak a
rule into another production merely because that rule would otherwise be
filtered out.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


RULE_SCOPE_SCHEMA = "aifos.rule-scope/v1"

CREATIVE_PRECEDENCE = (
    "current_shot",
    "episode_temporary",
    "project_series",
    "system_base",
)

_CREATIVE_APPLICATION_ORDER = tuple(reversed(CREATIVE_PRECEDENCE))
_APPLICABILITY_FIELDS = (
    "stage",
    "modality",
    "shot_no",
    "scene_no",
    "story_phase",
    "era",
    "active_realm_id",
    "event_id",
)
_APPLICABILITY_ALIASES = {
    "stages": "stage",
    "modalities": "modality",
    "shot_nos": "shot_no",
    "scene_nos": "scene_no",
    "story_phases": "story_phase",
    "active_story_phase": "story_phase",
    "active_story_phases": "story_phase",
    "eras": "era",
    "era_context": "era",
    "era_contexts": "era",
    "active_realm_ids": "active_realm_id",
    "active_realms": "active_realm_id",
    "realm_id": "active_realm_id",
    "realm_ids": "active_realm_id",
    "realms": "active_realm_id",
    "event_ids": "event_id",
    "events": "event_id",
    "scene_event_id": "event_id",
    "scene_event_ids": "event_id",
    "story_event_ids": "event_id",
}
_EPISODE_BOUND_LAYERS = {"episode_temporary", "current_shot"}
_PROTECTED_TECHNICAL_PREFIXES = (
    "technical.", "provider.", "quality.gate.", "quality_gate.")

_EXCEPTION_ALIASES = {
    "time_travel": {
        "time_travel", "time-travel", "time travel", "travel_in_time",
        "穿越", "时空穿越", "时间旅行",
    },
    "dream": {
        "dream", "dream_sequence", "dream-sequence", "梦", "梦境", "梦中",
    },
    "play_within_play": {
        "play_within_play", "play-within-play", "play within play",
        "戏中戏", "剧中剧",
    },
}


class RuleScopeError(ValueError):
    """Base exception for invalid rule-scope input."""


class ScopeBindingError(RuleScopeError):
    """A project- or episode-scoped rule does not belong to the context."""


class DuplicateRuleError(RuleScopeError):
    """Two simultaneously applicable rules share a key in one layer."""


def _required_identifier(name: str, value: Any) -> str:
    if isinstance(value, bool) or value is None:
        raise RuleScopeError(f"{name} must be a non-empty string")
    normalized = str(value).strip()
    if not normalized:
        raise RuleScopeError(f"{name} must be a non-empty string")
    return normalized


def _optional_identifier(name: str, value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _required_identifier(name, value)


@dataclass(frozen=True)
class RuleContext:
    """The exact production location for which rules are being resolved."""

    project_id: str
    episode_id: str
    stage: str | None = None
    modality: str | None = None
    shot_no: int | str | None = None
    scene_no: int | str | None = None
    story_phase: str | None = None
    active_story_phase: str | None = None
    era: str | None = None
    active_realm_id: str | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_id", _required_identifier("project_id", self.project_id))
        object.__setattr__(
            self, "episode_id", _required_identifier("episode_id", self.episode_id))
        story_phase = self.story_phase
        active_story_phase = self.active_story_phase
        if story_phase is not None and active_story_phase is not None:
            if _normalized_phase(story_phase) != _normalized_phase(
                    active_story_phase):
                raise RuleScopeError(
                    "story_phase conflicts with active_story_phase")
        normalized_phase = _normalized_phase(
            active_story_phase if active_story_phase is not None
            else story_phase)
        object.__setattr__(self, "story_phase", normalized_phase)
        object.__setattr__(self, "active_story_phase", normalized_phase)
        object.__setattr__(self, "active_realm_id", _optional_identifier(
            "active_realm_id", self.active_realm_id))
        object.__setattr__(self, "event_id", _optional_identifier(
            "event_id", self.event_id))

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "episode_id": self.episode_id,
            **{name: getattr(self, name) for name in _APPLICABILITY_FIELDS},
            "active_story_phase": self.active_story_phase,
        }


@dataclass(frozen=True)
class Rule:
    """A keyed rule value, optionally narrowed to a production context."""

    key: str
    value: Any
    applicability: Mapping[str, Any] = field(default_factory=dict)
    project_id: str | None = None
    episode_id: str | None = None
    source: str | None = None
    exception_kind: str | None = None


@dataclass(frozen=True)
class RuleBundle:
    """Bind several rules once instead of repeating IDs on every rule."""

    rules: Sequence[Rule | Mapping[str, Any]]
    project_id: str | None = None
    episode_id: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class _Candidate:
    key: str
    value: Any
    layer: str
    source: str
    project_id: str | None
    episode_id: str | None
    applicability: Mapping[str, tuple[Any, ...]]
    exception_kind: str | None

    def source_record(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "source": self.source,
            "technical_hard": self.layer == "technical_hard",
            "project_id": self.project_id,
            "episode_id": self.episode_id,
            "applicability": {
                name: list(values)
                for name, values in sorted(self.applicability.items())
            },
            "exception_kind": self.exception_kind,
        }


class ResolvedRuleSet(Mapping[str, Any]):
    """Resolved values plus the provenance and replacement audit trail."""

    def __init__(
        self,
        *,
        context: RuleContext,
        final_rules: Mapping[str, Any],
        sources: Mapping[str, Mapping[str, Any]],
        overridden: Sequence[Mapping[str, Any]],
        fingerprint: str,
    ) -> None:
        self.context = context
        self.final_rules = copy.deepcopy(dict(final_rules))
        self.sources = copy.deepcopy(dict(sources))
        self.overridden = copy.deepcopy(list(overridden))
        self.fingerprint = fingerprint

    @property
    def rules(self) -> dict[str, Any]:
        """A copy of effective key/value rules (convenient for Director)."""

        return copy.deepcopy(self.final_rules)

    @property
    def effective_rules(self) -> dict[str, Any]:
        """Compatibility name used by the rule-stack API."""

        return self.rules

    @property
    def suppressed(self) -> list[dict[str, Any]]:
        """Compatibility name for overridden/suppressed audit records."""

        return copy.deepcopy(self.overridden)

    def __getitem__(self, key: str) -> Any:
        return self.final_rules[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.final_rules)

    def __len__(self) -> int:
        return len(self.final_rules)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": RULE_SCOPE_SCHEMA,
            "context": self.context.as_dict(),
            "final_rules": copy.deepcopy(self.final_rules),
            "effective_rules": copy.deepcopy(self.final_rules),
            "sources": copy.deepcopy(self.sources),
            "overridden": copy.deepcopy(self.overridden),
            "suppressed": copy.deepcopy(self.overridden),
            "fingerprint": self.fingerprint,
        }


def _context(value: RuleContext | Mapping[str, Any]) -> RuleContext:
    if isinstance(value, RuleContext):
        return value
    if not isinstance(value, Mapping):
        raise RuleScopeError("context must be RuleContext or a mapping")
    aliases = {
        "realm_id": "active_realm_id",
        "scene_event_id": "event_id",
        "era_context": "era",
    }
    allowed = {
        "project_id", "episode_id", "active_story_phase",
        *_APPLICABILITY_FIELDS, *aliases,
    }
    unknown = set(value) - allowed
    if unknown:
        raise RuleScopeError(
            f"unknown context fields: {', '.join(sorted(map(str, unknown)))}")
    normalized = dict(value)
    for alias, canonical in aliases.items():
        if alias not in normalized:
            continue
        if canonical in normalized:
            left = _normalized_applicability_value(
                canonical, normalized[canonical])
            right = _normalized_applicability_value(
                canonical, normalized[alias])
            if left != right:
                raise RuleScopeError(
                    f"context {alias} conflicts with {canonical}")
        else:
            normalized[canonical] = normalized[alias]
        normalized.pop(alias, None)
    try:
        return RuleContext(**normalized)
    except TypeError as exc:
        raise RuleScopeError(str(exc)) from exc


def _normalized_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return value.strip().casefold()


def _normalized_phase(value: Any) -> Any:
    text = _normalized_text(value)
    if not isinstance(text, str):
        return text
    for canonical, aliases in _EXCEPTION_ALIASES.items():
        if text in {alias.casefold() for alias in aliases}:
            return canonical
    return text


def _normalized_applicability_value(field_name: str, value: Any) -> Any:
    if value == "*":
        return value
    if field_name in {"shot_no", "scene_no"}:
        if isinstance(value, bool):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if field_name == "story_phase":
        return _normalized_phase(value)
    if field_name in {"active_realm_id", "event_id"}:
        if value is None:
            return None
        return str(value).strip().casefold()
    return _normalized_text(value)


def _value_list(value: Any) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(
            value, (list, tuple, set, frozenset)):
        return [value]
    return list(value)


def _validated_applicability_values(
        field_name: str, value: Any) -> list[Any]:
    """Validate one selector before normalization.

    Applicability is a routing contract.  Treating malformed JSON as a value
    that merely does not match is unsafe because callers may later coerce the
    whole contract to ``{}``, accidentally turning a local rule into a global
    one.  Selectors therefore accept only one scalar or a non-empty collection
    of scalars; mappings, nested collections, booleans and null are invalid.
    """
    if isinstance(value, Mapping):
        raise RuleScopeError(
            f"applicability selector {field_name} must be a scalar or list")
    values = _value_list(value)
    if not values:
        raise RuleScopeError(
            f"applicability selector {field_name} cannot be empty")
    validated = []
    for item in values:
        if (item is None or isinstance(item, bool)
                or isinstance(item, Mapping)
                or (not isinstance(item, (str, bytes))
                    and isinstance(item, (list, tuple, set, frozenset)))):
            raise RuleScopeError(
                f"applicability selector {field_name} contains invalid value")
        if field_name in {"shot_no", "scene_no"}:
            if item == "*":
                validated.append(item)
                continue
            if isinstance(item, int):
                number = item
            elif isinstance(item, str) and item.strip().isdigit():
                number = int(item.strip())
            else:
                raise RuleScopeError(
                    f"applicability selector {field_name} must contain "
                    "positive integers or *")
            if number <= 0:
                raise RuleScopeError(
                    f"applicability selector {field_name} must contain "
                    "positive integers or *")
            validated.append(number)
            continue
        if not isinstance(item, str) or not item.strip():
            raise RuleScopeError(
                f"applicability selector {field_name} must contain "
                "non-empty strings")
        validated.append(item)
    return validated


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _stable_data(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _normalized_applicability(
        applicability: Mapping[str, Any] | None) -> dict[str, tuple[Any, ...]]:
    if applicability is None:
        return {}
    if not isinstance(applicability, Mapping):
        raise RuleScopeError("applicability must be a mapping")
    non_string_fields = [
        key for key in applicability if not isinstance(key, str)]
    if non_string_fields:
        raise RuleScopeError("applicability field names must be strings")
    unknown = (
        set(applicability)
        - set(_APPLICABILITY_FIELDS)
        - set(_APPLICABILITY_ALIASES)
    )
    if unknown:
        raise RuleScopeError(
            "unknown applicability fields: "
            + ", ".join(sorted(map(str, unknown))))

    result: dict[str, tuple[Any, ...]] = {}
    for supplied_name in sorted(applicability):
        field_name = _APPLICABILITY_ALIASES.get(
            supplied_name, supplied_name)
        values = [
            _normalized_applicability_value(field_name, item)
            for item in _validated_applicability_values(
                field_name, applicability[supplied_name])
        ]
        unique = {_canonical_json(item): item for item in values}
        normalized_values = tuple(
            unique[token] for token in sorted(unique))
        if (field_name in result
                and result[field_name] != normalized_values):
            raise RuleScopeError(
                f"applicability supplies conflicting selectors for "
                f"{field_name}")
        result[field_name] = normalized_values
    return result


def normalize_rule_applicability(
        applicability: Mapping[str, Any] | None) -> dict[str, list[Any]]:
    """Validate and canonicalize a public rule applicability contract.

    The returned mapping uses canonical selector names and JSON-safe lists.
    API and Director callers share this entry point so invalid stored data is
    rejected consistently instead of being widened to a global rule.
    """
    normalized = _normalized_applicability(applicability)
    return {
        name: list(values)
        for name, values in sorted(normalized.items())
    }


def _canonical_exception(value: Any) -> str | None:
    if value is None:
        return None
    normalized = _normalized_phase(value)
    if normalized not in _EXCEPTION_ALIASES:
        raise RuleScopeError(
            "exception_kind must be time_travel, dream, or play_within_play")
    return normalized


def _validate_exception(
        exception_kind: str | None,
        applicability: Mapping[str, tuple[Any, ...]],
) -> None:
    if exception_kind is None:
        return
    phases = applicability.get("story_phase")
    if not phases or "*" in phases:
        raise RuleScopeError(
            f"{exception_kind} exception requires an explicit story_phase")
    if any(phase != exception_kind for phase in phases):
        raise RuleScopeError(
            f"{exception_kind} exception may only target its matching story_phase")


def _rule_from_mapping(value: Mapping[str, Any]) -> Rule:
    if "key" not in value:
        raise RuleScopeError("each rule requires key")
    if "value" not in value and "text" not in value:
        raise RuleScopeError("each rule requires value or text")
    if "applicability" in value and "applies_to" in value:
        raise RuleScopeError("use only one of applicability and applies_to")
    applicability = value.get("applicability", value.get("applies_to", {}))
    exception_kind = value.get("exception_kind", value.get("exception"))
    binding = value.get("binding", {})
    if binding is None:
        binding = {}
    if not isinstance(binding, Mapping):
        raise RuleScopeError("rule binding must be a mapping")
    return Rule(
        key=value["key"],
        value=(value["value"] if "value" in value else value["text"]),
        applicability=applicability,
        project_id=value.get("project_id", binding.get("project_id")),
        episode_id=value.get("episode_id", binding.get("episode_id")),
        source=value.get("source", value.get("source_id")),
        exception_kind=exception_kind,
    )


def _effective_setting(
    name: str,
    rule_value: Any,
    bundle_value: Any,
) -> Any:
    if rule_value is not None and bundle_value is not None:
        normalized_rule = str(rule_value).strip()
        normalized_bundle = str(bundle_value).strip()
        if normalized_rule != normalized_bundle:
            raise ScopeBindingError(
                f"rule {name}={rule_value!r} conflicts with bundle "
                f"{name}={bundle_value!r}")
    return rule_value if rule_value is not None else bundle_value


def _payload_parts(
    payload: RuleBundle | Mapping[str, Any] | Iterable[Rule | Mapping[str, Any]] | None,
) -> tuple[list[Rule | Mapping[str, Any]], dict[str, Any]]:
    if payload is None:
        return [], {}
    if isinstance(payload, Rule):
        return [payload], {}
    if isinstance(payload, RuleBundle):
        return list(payload.rules), {
            "project_id": payload.project_id,
            "episode_id": payload.episode_id,
            "source": payload.source,
        }
    if isinstance(payload, Mapping):
        if "rules" in payload and "key" not in payload:
            raw_rules = payload["rules"]
            if isinstance(raw_rules, (str, bytes, Mapping)) or not isinstance(
                    raw_rules, Iterable):
                raise RuleScopeError("bundle rules must be an iterable of rules")
            binding = payload.get("binding", {}) or {}
            if not isinstance(binding, Mapping):
                raise RuleScopeError("bundle binding must be a mapping")
            return list(raw_rules), {
                "project_id": payload.get(
                    "project_id", binding.get("project_id")),
                "episode_id": payload.get(
                    "episode_id", binding.get("episode_id")),
                "source": payload.get("source", payload.get("source_id")),
            }
        return [payload], {}
    if isinstance(payload, (str, bytes)) or not isinstance(payload, Iterable):
        raise RuleScopeError("rule layer must be a rule, bundle, or iterable")
    return list(payload), {}


def _suppression_candidates(payload, *, layer: str,
                            context: RuleContext) -> list[dict[str, Any]]:
    """Return applicable high-layer removals of lower creative defaults."""
    if not isinstance(payload, Mapping) or "key" in payload:
        return []
    raw_rows = payload.get("suppressions") or []
    if not isinstance(raw_rows, list):
        raise RuleScopeError("bundle suppressions must be a list")
    binding = payload.get("binding") or {}
    if not isinstance(binding, Mapping):
        raise RuleScopeError("bundle binding must be a mapping")
    project_id = payload.get("project_id", binding.get("project_id"))
    episode_id = payload.get("episode_id", binding.get("episode_id"))
    project_id = _optional_identifier("project_id", project_id)
    episode_id = _optional_identifier("episode_id", episode_id)
    if layer == "project_series" and project_id is None:
        raise ScopeBindingError("project_series suppression requires project_id")
    if layer in _EPISODE_BOUND_LAYERS and (
            project_id is None or episode_id is None):
        raise ScopeBindingError(
            f"{layer} suppression requires project_id and episode_id")
    if project_id is not None and project_id != context.project_id:
        raise ScopeBindingError(
            f"suppression belongs to project {project_id!r}, not "
            f"{context.project_id!r}")
    if episode_id is not None and episode_id != context.episode_id:
        raise ScopeBindingError(
            f"suppression belongs to episode {episode_id!r}, not "
            f"{context.episode_id!r}")
    result = []
    for index, raw in enumerate(raw_rows):
        if isinstance(raw, str):
            key = raw.strip()
            applicability = {}
            source = str(payload.get("source") or layer)
            item_project_id = project_id
            item_episode_id = episode_id
        elif isinstance(raw, Mapping):
            key = str(raw.get("target") or raw.get("key")
                      or raw.get("rule_key") or raw.get("id") or "").strip()
            applicability = _normalized_applicability(
                raw.get("applicability", raw.get("applies_to", {})))
            source = str(raw.get("source") or payload.get("source")
                         or layer).strip()
            raw_binding = raw.get("binding") or {}
            if not isinstance(raw_binding, Mapping):
                raise RuleScopeError(
                    f"suppression {index} binding must be a mapping")
            item_project_id = _effective_setting(
                "project_id",
                raw.get("project_id", raw_binding.get("project_id")),
                project_id)
            item_episode_id = _effective_setting(
                "episode_id",
                raw.get("episode_id", raw_binding.get("episode_id")),
                episode_id)
        else:
            raise RuleScopeError(
                f"suppression {index} must be a string or mapping")
        if not key:
            raise RuleScopeError(f"suppression {index} requires a target key")
        if key.startswith(_PROTECTED_TECHNICAL_PREFIXES):
            raise RuleScopeError(
                f"technical hard rule {key!r} cannot be suppressed")
        item_project_id = _optional_identifier(
            "project_id", item_project_id)
        item_episode_id = _optional_identifier(
            "episode_id", item_episode_id)
        if layer == "project_series" and item_episode_id is not None:
            raise ScopeBindingError(
                "project_series suppression cannot bind an episode_id")
        if layer in _EPISODE_BOUND_LAYERS and (
                item_project_id is None or item_episode_id is None):
            raise ScopeBindingError(
                f"{layer} suppression requires project_id and episode_id")
        if item_project_id is not None and item_project_id != context.project_id:
            raise ScopeBindingError(
                f"suppression belongs to project {item_project_id!r}, not "
                f"{context.project_id!r}")
        if item_episode_id is not None and item_episode_id != context.episode_id:
            raise ScopeBindingError(
                f"suppression belongs to episode {item_episode_id!r}, not "
                f"{context.episode_id!r}")
        if _applies(applicability, context):
            result.append({
                "key": key, "layer": layer, "source": source,
                "project_id": item_project_id,
                "episode_id": item_episode_id,
                "applicability": {
                    name: list(values)
                    for name, values in sorted(applicability.items())
                },
            })
    return sorted(result, key=lambda item: (item["key"], item["source"]))


def _candidate_rules(
    payload: RuleBundle | Mapping[str, Any] | Iterable[Rule | Mapping[str, Any]] | None,
    *,
    layer: str,
    context: RuleContext,
) -> list[_Candidate]:
    rows, defaults = _payload_parts(payload)
    candidates: list[_Candidate] = []
    for raw_rule in rows:
        if isinstance(raw_rule, Rule):
            rule = raw_rule
        elif isinstance(raw_rule, Mapping):
            if raw_rule.get("enabled") is False:
                continue
            rule = _rule_from_mapping(raw_rule)
        else:
            raise RuleScopeError("rules must be Rule instances or mappings")

        if not isinstance(rule.key, str) or not rule.key.strip():
            raise RuleScopeError("rule key must be a non-empty string")
        key = rule.key.strip()
        project_id = _effective_setting(
            "project_id", rule.project_id, defaults.get("project_id"))
        episode_id = _effective_setting(
            "episode_id", rule.episode_id, defaults.get("episode_id"))
        # Bundle source is a fallback label; unlike identity bindings, an
        # individual rule may name a more precise source within that bundle.
        source = rule.source if rule.source is not None else defaults.get("source")
        project_id = _optional_identifier("project_id", project_id)
        episode_id = _optional_identifier("episode_id", episode_id)
        if source is None:
            source = layer
        source = _required_identifier("source", source)

        if layer == "project_series" and project_id is None:
            raise ScopeBindingError(
                f"project_series rule {key!r} requires project_id")
        if layer == "project_series" and episode_id is not None:
            raise ScopeBindingError(
                f"project_series rule {key!r} cannot bind an episode_id")
        if layer in _EPISODE_BOUND_LAYERS and (
                project_id is None or episode_id is None):
            raise ScopeBindingError(
                f"{layer} rule {key!r} requires project_id and episode_id")
        if project_id is not None and project_id != context.project_id:
            raise ScopeBindingError(
                f"rule {key!r} belongs to project {project_id!r}, not "
                f"{context.project_id!r}")
        if episode_id is not None and episode_id != context.episode_id:
            raise ScopeBindingError(
                f"rule {key!r} belongs to episode {episode_id!r}, not "
                f"{context.episode_id!r}")

        applicability = _normalized_applicability(rule.applicability)
        exception_kind = _canonical_exception(rule.exception_kind)
        _validate_exception(exception_kind, applicability)
        if not _applies(applicability, context):
            continue
        candidates.append(_Candidate(
            key=key,
            value=copy.deepcopy(rule.value),
            layer=layer,
            source=source,
            project_id=project_id,
            episode_id=episode_id,
            applicability=applicability,
            exception_kind=exception_kind,
        ))

    candidates.sort(key=lambda item: (
        item.key, item.source, _canonical_json(item.value),
        _canonical_json(item.source_record())))
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.key in seen:
            raise DuplicateRuleError(
                f"multiple applicable {layer} rules use key {candidate.key!r}")
        seen.add(candidate.key)
    return candidates


def _applies(
    applicability: Mapping[str, tuple[Any, ...]],
    context: RuleContext,
) -> bool:
    for field_name, expected_values in applicability.items():
        if not expected_values:
            return False
        if "*" in expected_values:
            continue
        actual = _normalized_applicability_value(
            field_name, getattr(context, field_name))
        if actual not in expected_values:
            return False
    return True


def _stable_data(value: Any) -> Any:
    """Return JSON-canonical data or raise instead of hashing unstable reprs."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuleScopeError("rule values cannot contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuleScopeError("rule value mapping keys must be strings")
            result[key] = _stable_data(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_stable_data(item) for item in value]
    if isinstance(value, (set, frozenset)):
        rows = [_stable_data(item) for item in value]
        return sorted(rows, key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    raise RuleScopeError(
        f"rule values must be JSON-compatible, got {type(value).__name__}")


def resolve_rules(
    context: RuleContext | Mapping[str, Any],
    *,
    technical_hard: RuleBundle | Mapping[str, Any]
    | Iterable[Rule | Mapping[str, Any]] | None = None,
    system_base: RuleBundle | Mapping[str, Any]
    | Iterable[Rule | Mapping[str, Any]] | None = None,
    project_series: RuleBundle | Mapping[str, Any]
    | Iterable[Rule | Mapping[str, Any]] | None = None,
    episode_temporary: RuleBundle | Mapping[str, Any]
    | Iterable[Rule | Mapping[str, Any]] | None = None,
    current_shot: RuleBundle | Mapping[str, Any]
    | Iterable[Rule | Mapping[str, Any]] | None = None,
) -> ResolvedRuleSet:
    """Resolve one Director context into a deterministic effective rule set.

    Layer arguments accept a sequence of :class:`Rule`, rule mappings, or a
    :class:`RuleBundle`.  A mapping bundle with ``rules`` and optional
    ``project_id``/``episode_id``/``source`` is accepted as well.
    """

    resolved_context = _context(context)
    inputs = {
        "system_base": system_base,
        "project_series": project_series,
        "episode_temporary": episode_temporary,
        "current_shot": current_shot,
        "technical_hard": technical_hard,
    }
    candidates = {
        layer: _candidate_rules(
            inputs[layer], layer=layer, context=resolved_context)
        for layer in (*_CREATIVE_APPLICATION_ORDER, "technical_hard")
    }

    winners: dict[str, _Candidate] = {}
    overridden: list[dict[str, Any]] = []
    for layer in _CREATIVE_APPLICATION_ORDER:
        for candidate in candidates[layer]:
            previous = winners.get(candidate.key)
            if previous is not None:
                overridden.append({
                    "key": previous.key,
                    "value": copy.deepcopy(previous.value),
                    "source": previous.source_record(),
                    "overridden_by": candidate.source_record(),
                    "replacement_value": copy.deepcopy(candidate.value),
                    "reason": (
                        "technical_hard_constraint"
                        if layer == "technical_hard"
                        else "higher_creative_priority"
                    ),
                })
            winners[candidate.key] = candidate
        for suppression in _suppression_candidates(
                inputs[layer], layer=layer, context=resolved_context):
            previous = winners.pop(suppression["key"], None)
            if previous is not None:
                overridden.append({
                    "key": previous.key,
                    "value": copy.deepcopy(previous.value),
                    "source": previous.source_record(),
                    "overridden_by": {
                        **copy.deepcopy(suppression),
                        "technical_hard": False,
                    },
                    "replacement_value": None,
                    "reason": "higher_creative_suppression",
                })
    for candidate in candidates["technical_hard"]:
        previous = winners.get(candidate.key)
        if previous is not None:
            overridden.append({
                "key": previous.key,
                "value": copy.deepcopy(previous.value),
                "source": previous.source_record(),
                "overridden_by": candidate.source_record(),
                "replacement_value": copy.deepcopy(candidate.value),
                "reason": "technical_hard_constraint",
            })
        winners[candidate.key] = candidate

    final_rules = {
        key: copy.deepcopy(winners[key].value)
        for key in sorted(winners)
    }
    sources = {
        key: winners[key].source_record()
        for key in sorted(winners)
    }
    fingerprint_payload = {
        "schema": RULE_SCOPE_SCHEMA,
        "context": resolved_context.as_dict(),
        "final_rules": final_rules,
        "sources": sources,
        "overridden": overridden,
    }
    fingerprint = "sha256:" + hashlib.sha256(
        _canonical_json(fingerprint_payload).encode("utf-8")).hexdigest()
    return ResolvedRuleSet(
        context=resolved_context,
        final_rules=final_rules,
        sources=sources,
        overridden=overridden,
        fingerprint=fingerprint,
    )


def _compat_bound_payload(
    payload: Rule | RuleBundle | Mapping[str, Any]
    | Iterable[Rule | Mapping[str, Any]] | None,
    *,
    layer: str,
    context: RuleContext,
) -> RuleBundle | Mapping[str, Any] | Iterable[Rule | Mapping[str, Any]] | None:
    """Inject caller-owned scope IDs while preserving conflicting IDs to fail."""

    if payload is None:
        return None
    project_id = (
        context.project_id
        if layer in {"project_series", "episode_temporary", "current_shot"}
        else None
    )
    episode_id = (
        context.episode_id if layer in _EPISODE_BOUND_LAYERS else None)
    if isinstance(payload, RuleBundle):
        return RuleBundle(
            rules=payload.rules,
            project_id=(
                payload.project_id
                if payload.project_id is not None else project_id),
            episode_id=(
                payload.episode_id
                if payload.episode_id is not None else episode_id),
            source=payload.source or layer,
        )
    if isinstance(payload, Rule):
        rows: list[Rule | Mapping[str, Any]] = [payload]
        bundle: dict[str, Any] = {"rules": rows}
    elif isinstance(payload, Mapping):
        if "key" in payload:
            bundle = {"rules": [copy.deepcopy(dict(payload))]}
        else:
            bundle = copy.deepcopy(dict(payload))
            scope = str(bundle.get("scope") or "").strip()
            if scope and scope != layer:
                raise ScopeBindingError(
                    f"{layer} input declares incompatible scope {scope!r}")
            bundle.setdefault("rules", [])
    else:
        if isinstance(payload, (str, bytes)) or not isinstance(
                payload, Iterable):
            raise RuleScopeError(
                f"{layer} rules must be a rule pack or iterable")
        bundle = {"rules": list(payload)}
    if project_id is not None:
        bundle.setdefault("project_id", project_id)
    if episode_id is not None:
        bundle.setdefault("episode_id", episode_id)
    if not bundle.get("source"):
        version = bundle.get("version")
        bundle["source"] = (
            f"{layer}:v{version}" if version not in (None, "") else layer)
    return bundle


def resolve_rule_stack(
    *,
    context: RuleContext | Mapping[str, Any],
    technical_rules: Rule | RuleBundle | Mapping[str, Any]
    | Iterable[Rule | Mapping[str, Any]] | None = None,
    base_rules: Rule | RuleBundle | Mapping[str, Any]
    | Iterable[Rule | Mapping[str, Any]] | None = None,
    project_rules: Rule | RuleBundle | Mapping[str, Any]
    | Iterable[Rule | Mapping[str, Any]] | None = None,
    episode_rules: Rule | RuleBundle | Mapping[str, Any]
    | Iterable[Rule | Mapping[str, Any]] | None = None,
    shot_rules: Rule | RuleBundle | Mapping[str, Any]
    | Iterable[Rule | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compatibility facade for storage/Web callers using friendly pack names.

    Project and episode bindings omitted from a pack are supplied from the
    required context.  Explicit conflicting bindings remain intact and are
    rejected by :func:`resolve_rules`.
    """

    resolved_context = _context(context)
    resolved = resolve_rules(
        resolved_context,
        technical_hard=_compat_bound_payload(
            technical_rules, layer="technical_hard", context=resolved_context),
        system_base=_compat_bound_payload(
            base_rules, layer="system_base", context=resolved_context),
        project_series=_compat_bound_payload(
            project_rules, layer="project_series", context=resolved_context),
        episode_temporary=_compat_bound_payload(
            episode_rules,
            layer="episode_temporary",
            context=resolved_context,
        ),
        current_shot=_compat_bound_payload(
            shot_rules, layer="current_shot", context=resolved_context),
    )
    return resolved.as_dict()


class RuleResolver:
    """Small reusable facade for Director's persistent hard/base rules."""

    def __init__(self, *, technical_hard=None, system_base=None) -> None:
        self.technical_hard = technical_hard
        self.system_base = system_base

    def resolve(
        self,
        context: RuleContext | Mapping[str, Any],
        *,
        project_series=None,
        episode_temporary=None,
        current_shot=None,
    ) -> ResolvedRuleSet:
        return resolve_rules(
            context,
            technical_hard=self.technical_hard,
            system_base=self.system_base,
            project_series=project_series,
            episode_temporary=episode_temporary,
            current_shot=current_shot,
        )


__all__ = [
    "CREATIVE_PRECEDENCE",
    "DuplicateRuleError",
    "ResolvedRuleSet",
    "RULE_SCOPE_SCHEMA",
    "Rule",
    "RuleBundle",
    "RuleContext",
    "RuleResolver",
    "RuleScopeError",
    "ScopeBindingError",
    "resolve_rule_stack",
    "resolve_rules",
]
