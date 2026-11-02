"""Exercise the real persistent graph with controlled providers and human decisions."""

import json
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from content_engine.archive import ArticleArchive
from content_engine.config import Settings
from content_engine.evaluator import DraftEvaluator
from content_engine.models import DraftPackage, EditorialBrief, EvaluationReport, IntakeDecision, NoveltyDecision, Source, VisualPlan
from content_engine.runtime import WorkflowRuntime
from content_engine.tracing import RunTrace
from content_engine.visuals import VisualService
from content_engine.workflow import WorkflowNodes
from content_engine.writer import WritingNode


BRIEF = dict(topic="Agent evaluation", audience="Software engineers", angle="Verify final application state", goal="Explain a concrete approach")
SOURCES = [Source(title=f"Evidence {i}", url=f"https://example.org/{i}", content="Check the resulting application state.") for i in range(5)]
DRAFTS = DraftPackage(linkedin="A grounded draft", medium="A longer grounded draft")


def report(**overrides):
    return EvaluationReport.model_validate({**dict(claims=[], faithfulness=4, substance=4, voice=4, structure=4,
                                                needs_research=False, issues=[], followup_queries=[], fetch_urls=[]), **overrides})


class ScriptedDecisions:
    def __init__(self):
        self.scripts = {}
        self.calls = []

    def ask(self, name, instructions, schema, data):
        self.calls.append((name, data))
        if name == "resolve_brief" and name not in self.scripts and data.get("supplied_brief"):
            return IntakeDecision(status="ready", brief=EditorialBrief.model_validate(data["supplied_brief"]))
        return self.scripts[name].pop(0)


class Search:
    def __init__(self):
        self.calls, self.fetches = [], []
        self.sources = SOURCES

    def search(self, query):
        self.calls.append(query)
        return self.sources

    def fetch(self, source):
        self.fetches.append(str(source.url))
        return source.model_copy(update={"content": source.content + " More detailed evidence."})


class Evaluator:
    def __init__(self):
        self.reports = []
        self.calls = 0

    def evaluate(self, *args):
        self.calls += 1
        return self.reports.pop(0) if self.reports else report()


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.decisions, self.search, self.evaluator = ScriptedDecisions(), Search(), Evaluator()
        self.generated = []
        self.drafts = DRAFTS

        def generate(prompt):
            self.generated.append(prompt)
            return self.drafts.model_dump_json()

        self.archive = ArticleArchive(self.settings.articles_db)
        self.visuals = VisualService(self.settings)
        self.nodes = WorkflowNodes(self.settings, self.decisions, self.search, WritingNode(generate),
                                   self.evaluator, self.archive, self.visuals, RunTrace())
        self.runtime = WorkflowRuntime(self.settings, nodes=self.nodes)

    def start(self):
        return self.runtime.start("test", brief=BRIEF)

    def test_persistent_review_requires_exact_approval_and_exports_once(self):
        pending = self.start()
        self.assertEqual(pending["pending"]["kind"], "final_review")
        self.assertEqual(self.archive.briefs(), [])
        self.assertFalse((self.settings.data_dir / "exports").exists())
        restarted = WorkflowRuntime(self.settings, nodes=self.nodes)
        self.assertEqual(restarted.status("test")["pending"], pending["pending"])
        with self.assertRaisesRegex(ValueError, "exact version"):
            restarted.resume("test", {"action": "approve", "version": "stale"})
        completed = restarted.resume("test", {"action": "approve", "version": pending["pending"]["version"]})
        self.assertEqual(completed["status"], "approved")
        target = Path(completed["state"]["export_path"])
        self.assertFalse(json.loads((target / "manifest.json").read_text())["published"])
        self.assertEqual(len(self.archive.briefs()), 1)
        self.assertEqual(len(self.generated), 1)
        with self.assertRaisesRegex(ValueError, "no pending"):
            restarted.resume("test", {"action": "approve", "version": pending["pending"]["version"]})

    def test_topic_only_waits_and_uses_actual_answer_without_researching_early(self):
        self.decisions.scripts["resolve_brief"] = [
            IntakeDecision(status="clarify", question="Which direction?", choices=["Practical", "Research"]),
            IntakeDecision(status="ready", brief=EditorialBrief(**BRIEF), assumptions=["User delegated the angle"]),
        ]
        pending = self.runtime.start("test", request="Write about meditation")
        self.assertEqual(pending["pending"]["choices"][-1], "Choose for me")
        self.assertEqual(self.search.calls, [])
        self.assertEqual(self.generated, [])
        with self.assertRaises(ValueError):
            self.runtime.resume("test", {})
        self.assertEqual(self.runtime.status("test")["pending"]["kind"], "clarification")
        result = self.runtime.resume("test", {"answer": "You choose the audience and angle"})
        self.assertEqual(result["pending"]["kind"], "final_review")
        self.assertEqual(self.decisions.calls[-1][1]["conversation"][-1]["content"], "You choose the audience and angle")

    def test_rewrites_are_bounded_and_never_imply_quality_pass(self):
        self.evaluator.reports = [report(structure=2) for _ in range(3)]
        result = self.start()
        self.assertEqual(len(self.generated), 3)
        self.assertEqual(result["state"]["revision_count"], 2)
        self.assertIn("REVISION CONTEXT", self.generated[-1])
        self.assertIn('"measured_article_word_counts": {"linkedin": 3, "medium": 4}', self.generated[-1])
        version = result["pending"]["version"]
        with self.assertRaisesRegex(ValueError, "Unresolved"):
            self.runtime.resume("test", {"action": "approve", "version": version})
        approved = self.runtime.resume("test", {"action": "approve_with_warnings", "version": version, "reason": "Fixture-only override to test explicit acceptance."})
        self.assertEqual(approved["status"], "approved")

    def test_material_gaps_trigger_one_deeper_pass_then_human_review(self):
        self.evaluator.reports = [report(needs_research=True, followup_queries=["focused evidence"], fetch_urls=[str(SOURCES[0].url)]) for _ in range(2)]
        result = self.start()
        self.assertEqual(result["pending"]["kind"], "research_review")
        self.assertEqual(len(self.search.calls), 2)
        self.assertEqual(len(self.generated), 2)
        self.assertEqual(self.search.fetches, [str(SOURCES[0].url)])
        self.assertIn("More detailed evidence", result["state"]["sources"][0]["content"])

    def test_insufficient_sources_pause_without_writer_or_judge_call(self):
        self.search.sources = SOURCES[:4]
        result = self.start()
        self.assertEqual(result["pending"]["kind"], "research_review")
        self.assertEqual(self.generated, [])
        self.assertEqual(self.evaluator.calls, 0)
        with self.assertRaisesRegex(ValueError, "five sources"):
            self.runtime.resume("test", {"action": "accept_scope", "feedback": "Use less"})
        self.assertEqual(self.runtime.resume("test", {"action": "cancel"})["status"], "cancelled")

    def test_duplicate_angle_is_reviewed_before_search(self):
        self.archive.save("prior", "version", BRIEF, DRAFTS.model_dump(), "fixture-export")
        identifier = self.archive.briefs()[0]["id"]
        self.decisions.scripts["compare_angles"] = [NoveltyDecision(duplicate=True, reason="Same argument", matching_ids=[identifier])]
        result = self.start()
        self.assertEqual(result["pending"]["kind"], "angle_review")
        self.assertEqual(self.search.calls, [])
        result = self.runtime.resume("test", {"action": "allow_repeat"})
        self.assertEqual(result["pending"]["kind"], "final_review")

    def test_visual_omission_requires_fresh_review_and_stale_approval_is_rejected(self):
        self.drafts = DRAFTS.model_copy(update={"visuals": [VisualPlan(platform="medium", placement="End", description="A meaningful scene", caption="A scene", alt_text="A scene")]})
        result = self.start()
        first_version = result["pending"]["version"]
        with self.assertRaisesRegex(ValueError, "Resolve every visual"):
            self.runtime.resume("test", {"action": "approve", "version": first_version})
        result = self.runtime.resume("test", {"action": "assets", "version": first_version, "choices": {"0": {"action": "omit", "reason": "No useful image for this fixture."}}})
        self.assertNotEqual(first_version, result["pending"]["version"])
        with self.assertRaisesRegex(ValueError, "exact version"):
            self.runtime.resume("test", {"action": "approve", "version": first_version})
        self.assertEqual(self.runtime.resume("test", {"action": "approve", "version": result["pending"]["version"]})["status"], "approved")

    def test_modified_review_file_cannot_be_approved_as_original(self):
        result = self.start()
        path = Path(result["pending"]["review_directory"]) / "medium.md"
        path.write_text("A changed article")
        with self.assertRaisesRegex(ValueError, "review article file changed"):
            self.runtime.resume("test", {"action": "approve", "version": result["pending"]["version"]})
        self.assertFalse((self.settings.data_dir / "exports").exists())

    def test_human_edited_drafts_are_reevaluated_before_another_approval(self):
        result = self.start()
        edited = DRAFTS.model_copy(update={"medium": "A human-edited draft"})
        updated = self.runtime.resume("test", {"action": "edit", "version": result["pending"]["version"], "drafts": edited.model_dump()})
        self.assertEqual(len(self.generated), 1)
        self.assertEqual(self.evaluator.calls, 2)
        self.assertNotEqual(updated["pending"]["version"], result["pending"]["version"])
        self.assertEqual(updated["state"]["drafts"]["medium"], "A human-edited draft")

    def test_invalid_brief_and_duplicate_session_are_rejected(self):
        with self.assertRaises(ValueError):
            self.runtime.start("bad", brief={"topic": "Meditation"})
        self.assertEqual(self.search.calls, [])
        self.start()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.runtime.start("test", brief=BRIEF)

    def test_claim_quotes_and_citation_urls_are_checked_independently(self):
        self.decisions.scripts["evaluate"] = [report(claims=[dict(claim="An unsupported claim", verdict="supported", source_url=str(SOURCES[0].url), supporting_quote="A quote that does not exist", explanation="Claimed support")])]
        judged = DraftEvaluator(self.decisions).evaluate(EditorialBrief(**BRIEF), DRAFTS, SOURCES)
        self.assertEqual(judged.claims[0].verdict, "unsupported")
        self.assertTrue(judged.needs_revision)
        self.assertEqual(judged.faithfulness, 3)

    def test_evidence_ids_copy_exact_text_and_reject_unknown_ids_or_wrong_urls(self):
        self.decisions.scripts["evaluate"] = [report(claims=[
            dict(claim="Check application state", verdict="supported", evidence_id="S1P1", explanation="Source supports it"),
            dict(claim="Invented reference", verdict="supported", evidence_id="S99P1", explanation="Invalid ID"),
            dict(claim="Wrong source", verdict="supported", evidence_id="S1P1", source_url=str(SOURCES[1].url), explanation="Wrong URL"),
        ])]
        judged = DraftEvaluator(self.decisions).evaluate(EditorialBrief(**BRIEF), DRAFTS, SOURCES)
        self.assertEqual(judged.claims[0].supporting_quote, SOURCES[0].content)
        self.assertEqual(judged.claims[0].source_url, str(SOURCES[0].url))
        self.assertEqual([c.verdict for c in judged.claims], ["supported", "unsupported", "unsupported"])
        self.assertEqual(judged.faithfulness, 3)

    def test_vague_structured_brief_waits_for_clarification_and_retains_original(self):
        vague = dict(topic="AI agents", audience="Everyone", angle="An interesting angle", goal="Inform")
        self.decisions.scripts["resolve_brief"] = [
            IntakeDecision(status="clarify", question="Who is this for and what should it explain?", choices=["Engineers building agents", "Nontechnical readers"]),
            IntakeDecision(status="ready", brief=EditorialBrief(**BRIEF)),
        ]
        pending = self.runtime.start("vague", brief=vague)
        self.assertEqual(pending["pending"]["kind"], "clarification")
        self.assertEqual(self.search.calls, [])
        self.assertEqual(self.generated, [])
        updated = self.runtime.resume("vague", {"answer": "Explain application-state checks to software engineers"})
        self.assertEqual(updated["pending"]["kind"], "final_review")
        payload = self.decisions.calls[-1][1]
        self.assertEqual(payload["supplied_brief"], vague)
        self.assertEqual(payload["conversation"][-1]["content"], "Explain application-state checks to software engineers")

    def test_attributed_inaccurate_quote_is_rejected_but_hypothetical_dialogue_is_not(self):
        self.decisions.scripts["evaluate"] = [report(), report()]
        evaluator = DraftEvaluator(self.decisions)
        quote = 'As one guide puts it: *“A source quotation that does not exist anywhere in the evidence.”* [Evidence](https://example.org/0)'
        bad = evaluator.evaluate(EditorialBrief(**BRIEF), DRAFTS.model_copy(update={"medium": quote}), SOURCES)
        self.assertEqual(bad.faithfulness, 3)
        self.assertTrue(any(c.verdict == "unsupported" and c.claim.startswith("Direct quotation") for c in bad.claims))
        scene = 'Imagine a user typing: “Please update the household chore schedule for next week.” [Evidence](https://example.org/0)'
        hypothetical = evaluator.evaluate(EditorialBrief(**BRIEF), DRAFTS.model_copy(update={"medium": scene}), SOURCES)
        self.assertEqual(hypothetical.faithfulness, 4)
        self.assertEqual(hypothetical.claims, [])

    def test_broad_audience_writer_fallback_discloses_scope_in_both_articles(self):
        brief = EditorialBrief(topic="AI", audience="Everyone", angle="Cover everything important", goal="Make it useful")
        result = self.nodes.writer({"brief": brief, "sources": SOURCES})["drafts"]
        for text in (result.linkedin, result.medium):
            self.assertTrue(text.startswith("Scope note:"))
            self.assertIn("working interpretation", text)
        self.assertIn(DRAFTS.medium, result.medium)

    def test_nonempirical_findings_do_not_require_scientific_evidence(self):
        self.decisions.scripts["evaluate"] = [report(claims=[dict(
            claim="A clearly proposed conversation checklist", verdict="not_applicable", evidence_id=None,
            explanation="A suggestion without a claimed empirical outcome")])]
        evaluated = DraftEvaluator(self.decisions).evaluate(EditorialBrief(**BRIEF), DRAFTS, SOURCES)
        self.assertEqual(evaluated.faithfulness, 4)
        self.assertFalse(any(i.kind == "evidence" and i.severity == "error" for i in evaluated.issues))

    def test_unknown_fetch_request_is_removed_and_report_remains_reviewable(self):
        self.decisions.scripts["evaluate"] = [report(needs_research=True, fetch_urls=["https://untrusted.example/", str(SOURCES[0].url)],
            claims=[dict(claim="An unresolved fact", verdict="unsupported", explanation="No supplied evidence")])]
        evaluated = DraftEvaluator(self.decisions).evaluate(EditorialBrief(**BRIEF), DRAFTS, SOURCES)
        self.assertEqual(evaluated.fetch_urls, [str(SOURCES[0].url)])
        self.assertTrue(evaluated.needs_research)
        self.assertEqual(evaluated.faithfulness, 3)
        self.assertTrue(any("Ignored evaluator fetch" in i.message for i in evaluated.issues))

    def test_failed_search_is_reported_and_retried_from_its_checkpoint(self):
        self.search.search = Mock(side_effect=[RuntimeError("Controlled provider failure"), SOURCES])
        with self.assertRaisesRegex(RuntimeError, "Controlled provider failure"):
            self.start()
        failed = self.runtime.status("test")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["next_nodes"], ["research"])
        self.assertEqual(self.generated, [])
        resumed = self.runtime.retry("test")
        self.assertEqual(resumed["pending"]["kind"], "final_review")
        self.assertEqual(len(self.generated), 1)

    def test_refresh_rebuilds_review_without_model_calls_or_approval(self):
        pending = self.start()
        medium = Path(pending["pending"]["review_directory"]) / "medium.md"
        medium.write_text("An unsaved external change")
        refreshed = self.runtime.refresh("test")
        self.assertEqual(medium.read_text(), DRAFTS.medium)
        self.assertEqual(len(self.generated), 1)
        self.assertEqual(self.evaluator.calls, 1)
        self.assertEqual(refreshed["pending"]["kind"], "final_review")
        self.assertFalse(refreshed["state"].get("approval"))
        self.assertEqual(self.archive.briefs(), [])

    def test_invalid_human_feedback_does_not_consume_review(self):
        result = self.start()
        for feedback in [None, 42, [], {}]:
            with self.subTest(feedback=feedback), self.assertRaises(ValueError):
                self.runtime.resume("test", {"action": "revise", "version": result["pending"]["version"], "feedback": feedback})
        self.assertEqual(self.runtime.status("test")["pending"], result["pending"])

    def test_human_revisions_are_bounded_without_disabling_cancel(self):
        self.settings = Settings(data_dir=self.settings.data_dir, max_human_revisions=1)
        self.nodes.settings = self.settings
        self.runtime.settings = self.settings
        result = self.start()
        result = self.runtime.resume("test", {"action": "revise", "version": result["pending"]["version"], "feedback": "Use a clearer example"})
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            self.runtime.resume("test", {"action": "revise", "version": result["pending"]["version"], "feedback": "Again"})
        self.assertEqual(self.runtime.resume("test", {"action": "cancel", "version": result["pending"]["version"]})["status"], "cancelled")

    def test_changed_attachment_is_rejected_before_approval_or_archive_write(self):
        self.drafts = DRAFTS.model_copy(update={"visuals": [VisualPlan(platform="medium", placement="End", description="Useful image", caption="A scene", alt_text="A scene")]})
        pending = self.start()
        image = Path(self.temp.name) / "fixture.png"
        image.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO5sAAAAASUVORK5CYII="))
        attached = self.runtime.resume("test", {"action": "assets", "version": pending["pending"]["version"],
                                               "choices": {"0": {"action": "attach", "path": str(image), "credit": "Test fixture"}}})
        asset = attached["pending"]["assets"][0]
        self.assertEqual(asset["status"], "ready")
        Path(asset["path"]).write_bytes(b"A changed asset")
        with self.assertRaisesRegex(ValueError, "image changed"):
            self.runtime.resume("test", {"action": "approve", "version": attached["pending"]["version"]})
        self.assertEqual(self.archive.briefs(), [])
        self.assertFalse((self.settings.data_dir / "exports").exists())

    def test_imported_history_participates_without_claiming_workflow_approval(self):
        self.archive.save("imported", "version", BRIEF, {"content": "Existing writing"}, "fixture.json", status="imported")
        self.archive.save("imported", "version", BRIEF, {"content": "Existing writing"}, "fixture.json", status="imported")
        self.assertEqual(len(self.archive.briefs()), 1)
        with self.archive.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM articles").fetchone()[0], "imported")
        identifier = self.archive.briefs()[0]["id"]
        self.decisions.scripts["compare_angles"] = [NoveltyDecision(duplicate=True, reason="Repeated thesis", matching_ids=[identifier])]
        pending = self.start()
        self.assertEqual(pending["pending"]["matching_articles"][0]["angle"], BRIEF["angle"])
        self.assertEqual(self.generated, [])

    def test_durable_human_pause_has_a_distinct_trace_event(self):
        trace = RunTrace(Path(self.temp.name) / "traces", console=False)
        self.nodes.trace = trace
        self.start()
        events = [json.loads(line) for line in trace.path.read_text().splitlines()]
        self.assertTrue(any(e["event"] == "pause" and e["name"] == "final_review" for e in events))
        self.assertFalse(any(e["event"] == "error" for e in events))


if __name__ == "__main__":
    unittest.main()
