"""Pydantic response schemas for all LLM calls.

Every schema uses extra="forbid" so the generated JSON schema carries
additionalProperties: false, as required by OpenAI strict structured outputs.
strict_json_schema() additionally marks every property required (strict mode
rejects schemas with optional properties) — optional-by-meaning fields are
therefore declared as nullable-but-required (e.g. ``c: float | None``).
"""
from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict


class TranslationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    i: int
    t: str
    c: float | None = None  # model-reported confidence 0–1, null when unsure


class TranslateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    translations: list[TranslationItem]


class RepairItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    i: int
    t: str


class RepairResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repairs: list[RepairItem]


class PolishEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    i: int
    t: str
    reason: str


class PolishIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    i: int
    severity: str
    category: str
    comment: str


class PolishResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edits: list[PolishEdit]
    issues: list[PolishIssue]


FinalAuditCategory = Literal[
    "meaning",
    "grammar",
    "context",
    "cross_event",
    "formality",
    "ambiguity",
    "other",
]
FinalAuditSeverity = Literal["warning", "info"]

# Runtime validators and persistence code use these values too.  Deriving
# them from the Literal aliases keeps the Python validation contract and the
# generated structured-output schema in one place.
FINAL_AUDIT_CATEGORIES = frozenset(get_args(FinalAuditCategory))
FINAL_AUDIT_SEVERITIES = frozenset(get_args(FinalAuditSeverity))


class FinalAuditIssue(BaseModel):
    """Read-only finding produced by the post-review semantic QA pass."""
    model_config = ConfigDict(extra="forbid")

    i: int
    category: FinalAuditCategory
    severity: FinalAuditSeverity
    explanation: str
    # Nullable-but-required in the strict JSON schema.  The auditor may be
    # certain that a defect exists without being certain of the best wording.
    suggestion: str | None = None


class FinalAuditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issues: list[FinalAuditIssue]


class GlossaryTermOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    category: str            # name|place|technique|item|honorific|catchphrase|other
    gender: str | None = None
    vocative: str | None = None
    note: str | None = None


class CharacterVoiceOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    voice_note: str
    register: str


class AddressPairOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker: str
    addressee: str
    mode: str                # tykani|vykani|mixed


class SceneSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_line: int
    to_line: int
    summary: str
    setting: str


class TrickyLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    i: int
    note: str


class AnalyzeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    synopsis: str
    scenes: list[SceneSummary]
    tricky_lines: list[TrickyLine]
    address_pairs: list[AddressPairOut]
    suggested_terms: list[GlossaryTermOut]


class StyleBibleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tone_summary: str
    register_notes: str
    honorific_policy: str
    terms: list[GlossaryTermOut]
    character_voices: list[CharacterVoiceOut]
    address_pairs: list[AddressPairOut]


class SpeakerMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker: str
    character_external_id: str | None = None  # null = no matching character
    confidence: float                          # 0–1
    inferred_gender: str | None = None         # male|female|null
    rationale: str


class MappingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matches: list[SpeakerMatch]


class StyleBibleUpdateResponse(BaseModel):
    """Additive per-episode update — new terms/voices/pairs only."""
    model_config = ConfigDict(extra="forbid")

    terms: list[GlossaryTermOut]
    character_voices: list[CharacterVoiceOut]
    address_pairs: list[AddressPairOut]


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """model_json_schema() adjusted for OpenAI strict mode: every property
    in every object node is listed as required."""
    schema = model.model_json_schema()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"].keys())
                node.setdefault("additionalProperties", False)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(schema)
    return schema
