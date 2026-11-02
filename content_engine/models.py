"""Validated input and output for the writing workflow."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class EditorialBrief(BaseModel):
    """Describe the subject, reader, specific argument, and intended outcome."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        str_min_length=1,
    )

    topic: str
    audience: str
    angle: str
    goal: str


class VisualPlan(BaseModel):
    """A requested illustration or diagram, resolved separately from drafting."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        str_min_length=1,
    )

    platform: Literal["linkedin", "medium"]
    placement: str
    description: str
    caption: str
    alt_text: str
    mermaid: str | None = None


class DraftPackage(BaseModel):
    """Platform drafts, planned visuals, and evidence gaps for review."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        str_min_length=1,
    )

    linkedin: str
    medium: str
    visuals: list[VisualPlan] = Field(default_factory=list, max_length=4)
    research_gaps: list[str] = Field(default_factory=list)


class Source(BaseModel):
    """A search excerpt with its original URL; not an independently verified fact."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        str_min_length=1,
    )

    title: str
    url: HttpUrl
    content: str


class DecisionModel(BaseModel):
    """Strict contracts for model decisions; never executable instructions."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, str_min_length=1)


class IntakeDecision(DecisionModel):
    status: Literal["clarify", "ready"]
    brief: EditorialBrief | None = None
    question: str | None = None
    choices: list[str] = Field(default_factory=list, max_length=4)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_decision(self):
        if self.status == "ready" and self.brief is None:
            raise ValueError("A ready decision requires a complete brief")
        if self.status == "clarify" and (not self.question or self.brief is not None):
            raise ValueError("Clarification requires a question, not a finalized brief")
        return self


class NoveltyDecision(DecisionModel):
    duplicate: bool
    reason: str
    matching_ids: list[str] = Field(default_factory=list)
    alternative_angles: list[str] = Field(default_factory=list, max_length=3)


class ClaimCheck(DecisionModel):
    claim: str
    verdict: Literal["supported", "unsupported", "conflicting", "not_applicable"]
    evidence_id: str | None = None
    source_url: str | None = None
    supporting_quote: str | None = None
    explanation: str


class ReviewIssue(DecisionModel):
    kind: Literal["evidence", "editorial", "visual"]
    severity: Literal["error", "warning"]
    message: str


class EvaluationReport(DecisionModel):
    claims: list[ClaimCheck]
    faithfulness: int = Field(ge=1, le=5)
    substance: int = Field(ge=1, le=5)
    voice: int = Field(ge=1, le=5)
    structure: int = Field(ge=1, le=5)
    needs_research: bool
    issues: list[ReviewIssue]
    followup_queries: list[str] = Field(default_factory=list, max_length=2)
    fetch_urls: list[str] = Field(default_factory=list, max_length=2)

    @property
    def needs_revision(self) -> bool:
        return any(i.severity == "error" for i in self.issues) or min(
            self.faithfulness, self.substance, self.voice, self.structure
        ) < 4


class EvidenceFinding(DecisionModel):
    """The model selects evidence; code copies the quotation and URL."""

    claim: str
    verdict: Literal["supported", "unsupported", "conflicting", "not_applicable"]
    evidence_id: str | None
    explanation: str


class EvidenceEvaluation(EvaluationReport):
    claims: list[EvidenceFinding]
