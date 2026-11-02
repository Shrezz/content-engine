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
                assumptions = list(state.get("assumptions", []))
                brief = state["brief"]
                if brief.audience.casefold().strip() in {"everyone", "anyone", "all", "people", "general audience"}:
                    assumptions.append("The audience is broad. Any narrower technical example is an illustrative working scope, not a confirmed user preference.")
                prompt = build_writing_prompt(brief, state.get("sources"), assumptions)
                prompt += "\n\nDELIVERY PLAN: Write the full articles, not summaries. For LinkedIn, develop six substantive paragraphs of roughly 65-75 words. For Medium, develop twelve substantive paragraphs of roughly 80-90 words, grouped under clear headings. Each paragraph should develop a distinct part of the explanation, example, tradeoffs or limitations. Do not repeat yourself or add facts beyond the supplied evidence."
                if state.get("previous_drafts"):
                    previous = state["previous_drafts"]
                    counts = {platform: len(previous[platform].split()) for platform in ("linkedin", "medium")}
                    targets = {"linkedin": {"minimum": 300, "maximum": 500, "target": 420},
                               "medium": {"minimum": 800, "maximum": 1200, "target": 1050}}
                    additions = {p: max(0, targets[p]["target"] - counts[p]) for p in counts}
                    prompt += "\n\nREVISION CONTEXT (data):\n" + json.dumps({
                        "previous_drafts": previous,
                        "evaluation": state.get("evaluation"), "human_feedback": state.get("feedback", ""),
                        "measured_article_word_counts": counts, "article_word_targets": targets,
                        "additional_words_needed_toward_target": additions,
                    }, ensure_ascii=False)
                    prompt += "\nRevise the complete package. Correct any measured word-count deficit by adding useful detail toward the target, not just the minimum. Preserve grounded sections and the other platform's substance. Resolve the listed factual/editorial issues without inventing evidence."
                prompt_event["outputs"] = {"system": WRITING_INSTRUCTIONS, "user": prompt}
            response = self.generate(prompt)
            with self.trace.span("validate_drafts", "validation", inputs={"response": response}) as validation:
                drafts = DraftPackage.model_validate_json(response)
                if brief.audience.casefold().strip() in {"everyone", "anyone", "all", "people", "general audience"}:
                    note = "Scope note: the brief names a broad audience. The examples below are a working interpretation for review; more specific audience and purpose details would help refine it."
                    for platform in ("linkedin", "medium"):
                        text = getattr(drafts, platform)
                        if not text.startswith(note):
                            setattr(drafts, platform, note + "\n\n" + text)
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
