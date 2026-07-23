from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Mode = Literal[
    "translate_text",
    "translate_text_to_file",
    "translate_file_to_file",
    "custom",
    "translate_batch",
]


class SessionSpec(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    context_policy: Literal["isolated", "continue"] = "isolated"


class UnitSpec(BaseModel):
    id: str = Field(min_length=1, max_length=512)
    text: str
    context: str | None = None


class SourceSpec(BaseModel):
    kind: Literal["text", "units", "file", "bundle"]
    locale: str = "auto"
    text: str | None = None
    path: str | None = None
    units: list[UnitSpec] | None = None
    bundle: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "SourceSpec":
        required = {
            "text": self.text is not None,
            "units": self.units is not None,
            "file": self.path is not None,
            "bundle": self.bundle is not None or self.path is not None,
        }
        if not required[self.kind]:
            raise ValueError(f"source.kind={self.kind} is missing its payload")
        return self


class GlossaryEntry(BaseModel):
    source: str
    targets: dict[str, str]


class TranslationOptions(BaseModel):
    model_config = ConfigDict(extra="allow")

    domain: str = "general"
    audience: str = "general"
    tone: str = "natural"
    preserve: list[str] = Field(
        default_factory=lambda: ["placeholders", "markdown", "urls", "code"]
    )
    glossary: list[GlossaryEntry] = Field(default_factory=list)
    quality: Literal["standard", "review"] = "standard"


class OutputSpec(BaseModel):
    format: Literal["text", "json", "yaml", "csv", "source"] = "source"
    path: str | None = None
    metrics_path: str | None = None
    overwrite: bool = False


class CustomTask(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    deliverables: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class TranslationRequest(BaseModel):
    schema_version: Literal["translator-agent/v1"]
    request_id: str = Field(min_length=1, max_length=128)
    session: SessionSpec
    mode: Mode
    source: SourceSpec
    targets: list[str] = Field(min_length=1, max_length=16)
    options: TranslationOptions = Field(default_factory=TranslationOptions)
    output: OutputSpec = Field(default_factory=OutputSpec)
    task: CustomTask | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> "TranslationRequest":
        if self.mode in {"translate_text_to_file", "translate_file_to_file"}:
            if not self.output.path:
                raise ValueError("output.path is required for file-writing modes")
        if self.output.metrics_path and self.mode not in {
            "translate_text_to_file",
            "translate_file_to_file",
        }:
            raise ValueError("output.metrics_path is only valid for file-writing modes")
        if self.output.metrics_path and self.output.metrics_path == self.output.path:
            raise ValueError("output.metrics_path must differ from output.path")
        if self.mode == "custom" and self.task is None:
            raise ValueError("task is required for custom mode")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("targets must be unique")
        return self


class ExtractedUnit(BaseModel):
    id: str
    text: str
    context: str | None = None


class ProviderUsage(BaseModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    provider_cost: float | None = Field(default=None, ge=0)


class ProviderResult(BaseModel):
    payload: dict[str, Any]
    usage: ProviderUsage = Field(default_factory=ProviderUsage)
    attempt_usages: list[ProviderUsage] = Field(default_factory=list)
    model_calls: int = Field(default=1, ge=0)
    provider_requests: int = Field(default=1, ge=1)
    format_strategies: list[str] = Field(default_factory=list)
