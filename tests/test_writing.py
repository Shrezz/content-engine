"""Exercise the real graph with controlled responses, without a provider."""

import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from content_engine.graph import build_graph
from content_engine.models import DraftPackage, EditorialBrief, VisualPlan
from content_engine.state import ContentState
from content_engine.writer import WritingNode


PROJECT = Path(__file__).resolve().parent.parent


class WritingWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.brief = EditorialBrief.model_validate_json(
            (PROJECT / "examples/brief.json").read_text(encoding="utf-8")
        )
        self.state = ContentState(brief=self.brief, revision_count=0)
        self.response = (PROJECT / "examples/drafts.json").read_text(encoding="utf-8")

    def test_graph_calls_generator_and_merges_only_validated_drafts(self) -> None:
        prompts = []

        def generate(prompt: str) -> str:
            prompts.append(prompt)
            return self.response

        result = build_graph(WritingNode(generate)).invoke(self.state)

        self.assertEqual(len(prompts), 1)
        self.assertIn(self.brief.angle, prompts[0])
        self.assertIn('"additionalProperties": false', prompts[0])
        self.assertEqual(result["brief"], self.brief)
        self.assertEqual(result["revision_count"], 0)
        self.assertEqual(result["drafts"], DraftPackage.model_validate_json(self.response))
        self.assertNotIn("drafts", self.state)

    def test_bad_output_fails_instead_of_entering_state(self) -> None:
        responses = [
            "not JSON",
            json.dumps({"linkedin": "Missing the other platform"}),
            json.dumps({"linkedin": "   ", "medium": "An article"}),
            json.dumps({"linkedin": 42, "medium": "An article"}),
            json.dumps({"linkedin": "A post", "medium": "An article", "published": True}),
        ]
        for response in responses:
            with self.subTest(response=response):
                graph = build_graph(WritingNode(lambda _prompt: response))
                with self.assertRaises(ValidationError):
                    graph.invoke(self.state)
                self.assertNotIn("drafts", self.state)

    def test_generation_failure_is_not_replaced_with_sample_content(self) -> None:
        def fail(_prompt: str) -> str:
            raise RuntimeError("Generation unavailable")

        with self.assertRaisesRegex(RuntimeError, "Generation unavailable"):
            build_graph(WritingNode(fail)).invoke(self.state)

    def test_older_draft_pair_defaults_to_empty_visuals_and_gaps(self) -> None:
        drafts = DraftPackage.model_validate({"linkedin": "An article", "medium": "A fuller article"})
        self.assertEqual(drafts.visuals, [])
        self.assertEqual(drafts.research_gaps, [])

    def test_visual_plan_rejects_missing_fields_invalid_platform_and_blank_text(self) -> None:
        plan = {
            "platform": "medium",
            "placement": "After the design section",
            "description": "Illustrate the design.",
            "caption": "How the components interact.",
            "alt_text": "A diagram of the system components.",
        }
        invalid_plans = [
            {key: value for key, value in plan.items() if key != "alt_text"},
            {**plan, "platform": "other"},
            {**plan, "description": " "},
            {**plan, "image_url": "https://example.org/invented.png"},
        ]
        for invalid in invalid_plans:
            with self.subTest(plan=invalid), self.assertRaises(ValidationError):
                VisualPlan.model_validate(invalid)

    def test_empty_research_gap_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            DraftPackage(linkedin="An article", medium="An article", research_gaps=[" "])


if __name__ == "__main__":
    unittest.main()
