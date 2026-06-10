from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from bookwiki.schemas import SCHEMA_VERSION

# Content is now authored as YAML frontmatter + raw MDX body (see
# ``bookwiki.agents.document``). ``yaml.safe_load`` parses bare scalars like a quiz
# ``answer: 3`` as ``int``, but the field is ``str``; coerce numbers to strings so YAML
# typing does not reject otherwise-valid content. This only affects ``str``-typed fields;
# ``int``/``float`` fields are unaffected.
_COERCE_NUMBERS = ConfigDict(coerce_numbers_to_str=True)


class VersionedModel(BaseModel):
    model_config = _COERCE_NUMBERS

    schema_version: str = Field(default=SCHEMA_VERSION)


class Citation(BaseModel):
    model_config = _COERCE_NUMBERS

    ref_id: str
    quote: str

    @field_validator("ref_id", "quote")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            msg = "citation fields must be non-empty"
            raise ValueError(msg)
        return value

    @field_validator("ref_id")
    @classmethod
    def ref_id_in_allowed_context(cls, value: str, info: ValidationInfo) -> str:
        allowed = _allowed_refs(info.context)
        if allowed and value not in allowed:
            msg = f"citation ref_id {value!r} is not in allowed source_refs"
            raise ValueError(msg)
        return value


def _allowed_refs(context: Any) -> set[str]:
    if not isinstance(context, dict):
        return set()
    raw_refs = context.get("allowed_citation_refs")
    if raw_refs is None:
        return set()
    if isinstance(raw_refs, str):
        return {raw_refs}
    return {str(item) for item in raw_refs}
