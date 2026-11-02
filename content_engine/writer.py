"""The writing node: prompt, generation call, validated state update."""

import json
from collections.abc import Callable

from .models import DraftPackage
from .prompts import WRITING_INSTRUCTIONS, build_writing_prompt
from .state import ContentState
from .tracing import RunTrace


class WritingNode:
    """Accept a text-generation function so the provider stays separate."""

    def __init__(self, generate: Callable[[str], str], trace: RunTrace | None = None) -> None:
        self.generate = generate
        self.trace = trace or RunTrace()

    def __call__(self, state: ContentState) -> dict[str, DraftPackage]:
        inputs = {"brief": state["brief"], "sources": state.get("sources", [])}
        with self.trace.span("write_drafts", "node", inputs=inputs) as event:
            with self.trace.span("build_writing_prompt", "prompt") as prompt_event:
                prompt = build_writing_prompt(state["brief"], state.get("sources"))
                if state.get("previous_drafts"):
                    prompt += "\n\nREVISION CONTEXT (data):\n" + json.dumps({
                        "previous_drafts": state["previous_drafts"],
                        "evaluation": state.get("evaluation"), "human_feedback": state.get("feedback", ""),
                    }, ensure_ascii=False)
                    prompt += "\nRevise the package to address this feedback. Preserve grounded claims and return the complete JSON package."
                prompt_event["outputs"] = {"system": WRITING_INSTRUCTIONS, "user": prompt}
            response = self.generate(prompt)
            with self.trace.span("validate_drafts", "validation", inputs={"response": response}) as validation:
                drafts = DraftPackage.model_validate_json(response)
                validation["outputs"] = drafts
                validation["metadata"] = {
                    "word_counts": {
                        "linkedin": len(drafts.linkedin.split()),
                        "medium": len(drafts.medium.split()),
                    },
                    "visual_count": len(drafts.visuals),
                    "research_review_needed": bool(drafts.research_gaps),
                }
            event["outputs"] = {"drafts": drafts}
            return {"drafts": drafts}
