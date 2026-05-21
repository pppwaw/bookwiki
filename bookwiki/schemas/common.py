from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from bookwiki.schemas import SCHEMA_VERSION


class VersionedModel(BaseModel):
    schema_version: str = Field(default=SCHEMA_VERSION)


class Citation(BaseModel):
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
