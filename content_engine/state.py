"""Shared state passed between LangGraph nodes."""

from typing import NotRequired, TypedDict

from .models import DraftPackage, EditorialBrief, Source


class ContentState(TypedDict):
    """Nodes will read this state and return updates to its fields."""

    brief: EditorialBrief
    revision_count: int
    research_query: NotRequired[str]
    sources: NotRequired[list[Source]]
    drafts: NotRequired[DraftPackage]


class WorkflowState(TypedDict, total=False):
    """Persist plain JSON values; validate models at node boundaries."""

    thread_id: str
    request: str
    conversation: list[dict]
    brief: dict | None
    submitted_brief: dict
    intake: dict
    assumptions: list[str]
    novelty: dict
    repeat_allowed: bool
    search_query: str
    research_rounds: int
    sources: list[dict]
    research_warnings: list[str]
    drafts: dict
    evaluation: dict
    revision_count: int
    human_revisions: int
    feedback: str
    scope_accepted: bool
    visual_choices: dict
    packet: dict
    approval: dict
    review_action: str
    export_path: str
    status: str
