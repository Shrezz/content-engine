"""Verify evaluation isolation, bounded revisions, and honest failure reporting."""

import json
import tempfile
import unittest
from pathlib import Path

from content_engine.config import Settings
from content_engine.evals import GoldenEvaluator, ReferenceGrade
from content_engine.models import DraftPackage, EditorialBrief, EvaluationReport, IntakeDecision
from content_engine.tracing import RunTrace
from content_engine.writer import WritingNode
from content_engine.decisions import DecisionClient
from pydantic import ValidationError


ROOT = Path(__file__).resolve().parent.parent


class Decisions:
    def __init__(self):
        self.calls = []
        self.omit_criterion = False

    def ask(self, name, system, schema, data):
        self.calls.append((name, data))
        if name == "golden_intake":
            return IntakeDecision(status="clarify", question="Which direction?", choices=["Practical", "Scientific"])
        criteria = data["reference_criteria"]
        return ReferenceGrade(checks=[dict(criterion=c, verdict="met", reason="Controlled test grader") for c in (criteria[:1] if self.omit_criterion else criteria)])


class Evaluator:
    def __init__(self, scores):
        self.scores = iter(scores)

    def evaluate(self, *args):
        return EvaluationReport(claims=[], faithfulness=next(self.scores), substance=4, voice=4, structure=4,
                                needs_research=False, issues=[], followup_queries=[], fetch_urls=[])


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.runner = GoldenEvaluator.__new__(GoldenEvaluator)
        self.runner.settings = Settings(max_revisions=1)
        self.runner.trace = RunTrace()
        self.runner.decisions = Decisions()
        self.prompts = []

        def generate(prompt):
            self.prompts.append(prompt)
            return DraftPackage(linkedin="A draft", medium="A deeper draft").model_dump_json()

        self.runner.writer = WritingNode(generate)
        self.runner.evaluator = Evaluator([3, 4])
        self.dataset = json.loads((ROOT / "evals/golden.json").read_text())

    def test_frozen_references_never_enter_writer_and_judge_sees_evidence_without_scores(self):
        case = next(c for c in self.dataset["cases"] if c["id"] == "WRITE-001")
        case["criteria"] = ["EVALUATOR_ONLY_SENTINEL", "Second criterion"]
        result = self.runner._case(case, self.dataset["sources"], True)
        self.assertTrue(result["passed"])
        self.assertFalse(result["baseline"]["passed"])
        self.assertEqual(len(self.prompts), 2)
        self.assertTrue(all("EVALUATOR_ONLY_SENTINEL" not in p for p in self.prompts))
        for name, data in self.runner.decisions.calls:
            self.assertEqual(name, "reference_grade")
            self.assertEqual(len(data["original_input"]["sources"]), 6)
            self.assertNotIn("evaluation", data["actual_output"])

    def test_exhausting_revision_budget_is_a_failure_even_when_reference_judge_passes(self):
        self.runner.evaluator = Evaluator([2, 3])
        case = next(c for c in self.dataset["cases"] if c["id"] == "WRITE-002")
        result = self.runner._case(case, self.dataset["sources"], False)
        self.assertFalse(result["passed"])
        self.assertFalse(result["runtime_quality_passed"])
        self.assertEqual(result["output"]["revisions"], 1)

    def test_reference_grader_must_return_every_criterion(self):
        self.runner.decisions.omit_criterion = True
        with self.assertRaisesRegex(ValueError, "omitted"):
            self.runner._grade(["One", "Two"], {}, {})

    def test_critic_grader_receives_role_separately_from_flawed_input(self):
        inputs = {"drafts": {"medium": "Deliberately unsupported claim"}}
        self.runner._grade(["Flags the unsupported claim"], {"claims": [{"verdict": "unsupported"}]}, inputs,
                           evaluation_target="critic")
        data = self.runner.decisions.calls[-1][1]
        self.assertEqual(data["evaluation_target"], "critic")
        self.assertEqual(data["original_input"], inputs)
        self.assertNotIn("evaluation", data["actual_output"])

    def test_invalid_decision_gets_one_schema_repair_without_coercing_verdict(self):
        calls = []
        responses = iter(['{"checks":[{"criterion":"One","reason":"Unsupported","verdict":"partially met"}]}',
                          '{"checks":[{"criterion":"One","reason":"Unsupported","verdict":"not_met"}]}'])
        def complete(system, prompt, *, name):
            calls.append((name, prompt))
            return next(responses)
        grade = DecisionClient(complete).ask("grade", "Use supplied evidence", ReferenceGrade, {})
        self.assertEqual(grade.checks[0].verdict, "not_met")
        self.assertEqual([c[0] for c in calls], ["grade", "grade_repair"])
        self.assertIn("validation_errors", calls[1][1])

    def test_invalid_decision_does_not_retry_indefinitely(self):
        calls = []
        def complete(system, prompt, *, name):
            calls.append(name)
            return '{}'
        with self.assertRaises(ValidationError):
            DecisionClient(complete).ask("grade", "Use supplied evidence", ReferenceGrade, {})
        self.assertEqual(calls, ["grade", "grade_repair"])

    def test_split_and_case_filters_do_not_run_development_examples(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "golden.json"
            path.write_text(json.dumps(self.dataset))
            result = self.runner.run(path, split="test", case_ids=["INT-001", "INT-002"])
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["cases"][0]["id"], "INT-002")
            self.assertEqual(result["cases"][0]["output"]["choices"][-1], "Choose for me")
            self.assertIn("candidate", result["reference_status"])
            with self.assertRaisesRegex(ValueError, "No dataset cases"):
                self.runner.run(path, split="test", case_ids=["missing"])

    def test_fewer_than_five_frozen_sources_is_rejected_before_writing(self):
        case = next(c for c in self.dataset["cases"] if c["id"] == "WRITE-001")
        case["inputs"]["source_ids"] = ["workflows"]
        with self.assertRaisesRegex(ValueError, "five unique"):
            self.runner._case(case, self.dataset["sources"], False)
        self.assertEqual(self.prompts, [])
