"""Exercise the real HTTP interface against isolated checkpoints and providers."""

import base64
import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from content_engine.archive import ArticleArchive
from content_engine.config import Settings
from content_engine.models import DraftPackage, EditorialBrief, IntakeDecision, NoveltyDecision, VisualPlan
from content_engine.runtime import WorkflowRuntime
from content_engine.studio import render_article, summary
from content_engine.tracing import RunTrace
from content_engine.visuals import VisualService
from content_engine.web import create_app
from content_engine.workflow import WorkflowNodes
from content_engine.writer import WritingNode
from test_workflow import BRIEF, DRAFTS, Evaluator, ScriptedDecisions, Search, report


HEADERS = {"X-Content-Engine": "studio"}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO5sAAAAASUVORK5CYII=")


class StudioFixture:
    def __init__(self, directory):
        self.settings = Settings(data_dir=Path(directory), nebius_api_key="fixture-key", nebius_model="fixture-model")
        self.decisions, self.search, self.evaluator = ScriptedDecisions(), Search(), Evaluator()
        self.drafts = DRAFTS
        self.decisions.scripts["resolve_brief"] = [IntakeDecision(status="ready", brief=EditorialBrief(**BRIEF)) for _ in range(10)]
        self.archive = ArticleArchive(self.settings.articles_db)

    def runtime(self, settings, trace=None):
        trace = trace or RunTrace()
        nodes = WorkflowNodes(settings, self.decisions, self.search, WritingNode(lambda _: self.drafts.model_dump_json(), trace),
                              self.evaluator, self.archive, VisualService(settings, trace), trace)
        return WorkflowRuntime(settings, trace, nodes)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = StudioFixture(self.temp.name)
        self.app = create_app(self.fixture.settings, self.fixture.runtime)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def post(self, path, body):
        return self.client.post(path, json=body, headers=HEADERS)

    def wait(self, key):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = self.client.get(f"/api/sessions/{key}").json()
            if value.get("job", {}).get("status") not in {"queued", "running"}:
                return value
            time.sleep(.01)
        self.fail("Controlled workflow did not finish")

    def start(self):
        result = self.post("/api/sessions", {"request": "Write about agent evaluation for software engineers"})
        self.assertEqual(result.status_code, 202, result.text)
        return self.wait(result.json()["id"])

    def test_browser_flow_uses_real_checkpoints_and_approval_exports_a_bundle(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertEqual(self.client.get("/api/sessions").json()["sessions"], [])
        current = self.start()
        key, version = current["thread_id"], current["pending"]["version"]
        self.assertEqual(current["pending"]["kind"], "final_review")
        self.assertIn("<p>", current["articles"]["medium"])
        self.assertTrue(any(e["event"] == "pause" for e in current["activity"]))
        self.assertEqual(self.client.get(f"/api/sessions/{key}/download").status_code, 409)
        bad = self.post(f"/api/sessions/{key}/respond", {"action": "approve", "version": "old"})
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(self.client.get(f"/api/sessions/{key}").json()["pending"]["version"], version)
        result = self.post(f"/api/sessions/{key}/respond", {"action": "approve", "version": version})
        self.assertEqual(result.status_code, 202)
        self.assertEqual(self.wait(key)["state"]["status"], "approved")
        download = self.client.get(f"/api/sessions/{key}/download")
        self.assertEqual(download.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(download.content)) as bundle:
            self.assertIn("medium.md", bundle.namelist())
            self.assertFalse(json.loads(bundle.read("manifest.json"))["published"])
        self.assertEqual(len(self.client.get("/api/history").json()["articles"]), 1)

    def test_upload_and_omission_create_new_reviews_without_implicit_approval(self):
        plan = VisualPlan(platform="medium", placement="End", description="Illustrate a claim", caption="A useful scene", alt_text="A scene")
        self.fixture.drafts = DRAFTS.model_copy(update={"visuals": [plan]})
        current = self.start()
        key, version = current["thread_id"], current["pending"]["version"]
        self.assertEqual(self.post(f"/api/sessions/{key}/respond", {"action": "approve", "version": version}).status_code, 409)
        result = self.post(f"/api/sessions/{key}/images/0", {"version": version, "content": base64.b64encode(PNG).decode(), "credit": "Controlled fixture"})
        self.assertEqual(result.status_code, 202, result.text)
        updated = self.wait(key)
        self.assertNotEqual(updated["pending"]["version"], version)
        asset = updated["pending"]["assets"][0]
        self.assertEqual(asset["status"], "ready")
        image = self.client.get(f"/api/sessions/{key}/assets/{Path(asset['path']).name}")
        self.assertEqual(image.content, PNG)
        self.assertIn(f'/api/sessions/{key}/assets/', updated["articles"]["medium"])
        self.assertEqual(self.fixture.archive.briefs(), [])
        result = self.post(f"/api/sessions/{key}/respond", {"action": "assets", "version": updated["pending"]["version"], "choices": {"0": {"action": "omit", "reason": "The text explains it well enough"}}})
        self.assertEqual(result.status_code, 202)
        self.assertEqual(self.wait(key)["pending"]["assets"][0]["status"], "omitted")

    def test_imported_history_is_checked_before_any_research(self):
        imported = self.post("/api/history", {"brief": BRIEF, "content": "A previous article", "source_url": "https://example.org/previous"})
        self.assertEqual(imported.status_code, 201)
        self.assertEqual(self.post("/api/history", {"brief": BRIEF, "content": "A previous article", "source_url": "https://example.org/previous"}).status_code, 201)
        rows = self.client.get("/api/history").json()["articles"]
        self.assertEqual(len(rows), 1)
        key = rows[0]["id"]
        self.assertIn("A previous article", self.client.get(f"/api/history/{key}").json()["articles"]["content"])
        self.fixture.decisions.scripts["compare_angles"] = [NoveltyDecision(duplicate=True, reason="Same thesis", matching_ids=[key], alternative_angles=["A distinct angle"])]
        current = self.start()
        self.assertEqual(current["pending"]["kind"], "angle_review")
        self.assertEqual(current["pending"]["matching_articles"][0]["id"], key)
        self.assertEqual(self.fixture.search.calls, [])

    def test_clarification_and_manual_edit_actions_resume_the_same_session(self):
        self.fixture.decisions.scripts["resolve_brief"].insert(0, IntakeDecision(status="clarify", question="Which direction?", choices=["Practical"]))
        current = self.start()
        self.assertEqual(current["pending"]["kind"], "clarification")
        key = current["thread_id"]
        self.assertEqual(self.post(f"/api/sessions/{key}/respond", {"answer": "A practical guide for software engineers"}).status_code, 202)
        current = self.wait(key)
        edited = DRAFTS.model_copy(update={"medium": "A manually edited article"})
        self.assertEqual(self.post(f"/api/sessions/{key}/respond", {"action": "edit", "version": current["pending"]["version"], "drafts": edited.model_dump()}).status_code, 202)
        updated = self.wait(key)
        self.assertEqual(updated["state"]["drafts"]["medium"], "A manually edited article")
        self.assertEqual(updated["thread_id"], key)

    def test_cross_site_actions_and_arbitrary_files_are_blocked(self):
        self.assertEqual(self.client.post("/api/sessions", json={"request": "A topic"}).status_code, 403)
        self.assertEqual(self.client.post("/api/sessions", json={"request": "A topic"}, headers={**HEADERS, "Origin": "https://elsewhere.example"}).status_code, 403)
        current = self.start()
        self.assertEqual(self.client.get(f'/api/sessions/{current["thread_id"]}/assets/.env').status_code, 404)
        self.assertEqual(self.client.get("/.env").status_code, 404)
        self.assertIn("frame-ancestors 'none'", self.client.get("/").headers["content-security-policy"])

    def test_preview_disables_html_scripts_and_unreviewed_images(self):
        output = render_article('<script>alert("x")</script>\n\n![remote](https://example.org/pixel.png)\n\n[bad](javascript:alert(1))')
        self.assertNotIn("<script>", output)
        self.assertNotIn('<img', output)
        self.assertNotIn('href="javascript:', output)
        self.assertIn("Image awaiting review", output)

    def test_terminal_state_is_visible_even_while_background_wrapper_finishes(self):
        snapshot = {"thread_id": "fixture", "status": "approved", "pending": None, "state": {"status": "approved"}, "next_nodes": []}
        self.assertEqual(summary(snapshot, {"status": "running"})["status"], "approved")

    def test_unchanged_picture_choices_survive_text_edits_and_are_reapproved(self):
        plan = VisualPlan(platform="medium", placement="End", description="Illustrate a claim", caption="A useful scene", alt_text="A scene")
        self.fixture.drafts = DRAFTS.model_copy(update={"visuals": [plan]})
        current = self.start()
        key = current["thread_id"]
        self.post(f"/api/sessions/{key}/respond", {"action": "assets", "version": current["pending"]["version"], "choices": {"0": {"action": "omit", "reason": "The article works without this scene"}}})
        resolved = self.wait(key)
        edited = self.fixture.drafts.model_copy(update={"medium": "The revised text"})
        self.post(f"/api/sessions/{key}/respond", {"action": "edit", "version": resolved["pending"]["version"], "drafts": edited.model_dump()})
        updated = self.wait(key)
        self.assertEqual(updated["pending"]["assets"][0]["status"], "omitted")
        self.assertNotEqual(updated["pending"]["version"], resolved["pending"]["version"])
        self.assertEqual(self.fixture.archive.briefs(), [])

    def test_picture_insertion_does_not_split_a_sentence_or_paragraph(self):
        article = "A paragraph starts here. It continues with an explanation.\n\nThe next paragraph."
        asset = {"path": "fixture.png", "caption": "An image", "alt_text": "Image"}
        rendered, location = VisualService._insert(article, "after 'A paragraph starts here.'", asset)
        self.assertIn("starts here. It continues with an explanation.\n\n![", rendered)
        self.assertEqual(location, "after matched paragraph")
