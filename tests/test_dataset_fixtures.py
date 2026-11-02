"""Verify benchmark isolation and the three separately counted workflow checks."""

import base64
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import test_workflow as fixtures
from content_engine.config import PROJECT_DIR, Settings
from content_engine.evaluation_cases import DatasetCaseRunner, validate_fixtures
from content_engine.models import VisualPlan
from content_engine.visuals import digest


class FixtureValidationTests(unittest.TestCase):
    def setUp(self):
        self.dataset = json.loads((PROJECT_DIR / "evals/golden-v0.4.json").read_text())

    def test_all_32_cases_are_prepared_and_keep_approval_workflow_out_of_denominator(self):
        cases = self.dataset["cases"]
        self.assertEqual(len(cases), 32)
        self.assertEqual(len({c["id"] for c in cases}), 32)
        self.assertTrue({c["split"] for c in cases} == {"dev", "test"})
        self.assertFalse({"CE-038", "CE-039", "CE-041", "CE-027", "CE-028"} & {c["id"] for c in cases})
        validate_fixtures(cases, self.dataset["sources"])

    def test_changed_or_unapproved_fixtures_fail_before_model_calls(self):
        case = self.dataset["cases"][0]
        case["inputs"]["brief"]["goal"] = "Changed input"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate_fixtures([case], self.dataset["sources"])
        case["definition_approved"] = False
        with self.assertRaisesRegex(ValueError, "reference"):
            validate_fixtures([case], self.dataset["sources"])

    def test_changed_source_fails_before_model_calls(self):
        case = self.dataset["cases"][0]
        self.dataset["sources"][case["execution"]["source_ids"][0]]["source"]["content"] += " New evidence"
        with self.assertRaisesRegex(ValueError, "Source hash mismatch"):
            validate_fixtures([case], self.dataset["sources"])


class DatasetWorkflowChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkflowTests(methodName="test_persistent_review_requires_exact_approval_and_exports_once")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_ce038_rendered_diagram_is_bound_to_exact_review_version(self):
        f = self.fixture
        settings = replace(f.settings, node_binary=Settings.from_env().node_binary)
        f.nodes.visuals.settings = settings
        f.drafts = fixtures.DRAFTS.model_copy(update={"visuals": [VisualPlan(
            platform="medium", placement="End", description="Approval workflow", caption="Proposed flow", alt_text="Review precedes application changes",
            mermaid="flowchart LR\nA[Proposed change] --> B[Human review]\nB --> C[Apply approved version]")]})
        result = f.start()
        state = result["state"]
        asset = state["packet"]["snapshot"]["assets"][0]
        self.assertEqual(asset["status"], "ready")
        self.assertTrue(Path(asset["path"]).read_bytes().startswith(bytes.fromhex("89504e470d0a1a0a")))
        self.assertEqual(state["packet"]["version"], digest(state["packet"]["snapshot"]))
        f.nodes.visuals.verify(state["packet"])
        self.assertFalse((f.settings.data_dir / "exports").exists())

    def test_ce039_attachment_and_omission_create_fresh_review_without_approval(self):
        f = self.fixture
        f.drafts = fixtures.DRAFTS.model_copy(update={"visuals": [VisualPlan(platform="medium", placement="End", description="A scene", caption="A scene", alt_text="A scene")]})
        first = f.start()
        old = first["pending"]["version"]
        image = f.settings.data_dir / "fixture.png"
        image.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO5sAAAAASUVORK5CYII="))
        attached = f.runtime.resume("test", {"action": "assets", "version": old,
            "choices": {"0": {"action": "attach", "path": str(image), "credit": "Synthetic test image"}}})
        self.assertNotEqual(attached["pending"]["version"], old)
        self.assertEqual(attached["pending"]["assets"][0]["status"], "ready")
        omitted = f.runtime.resume("test", {"action": "assets", "version": attached["pending"]["version"],
            "choices": {"0": {"action": "omit", "reason": "Image unnecessary in this fixture"}}})
        self.assertEqual(omitted["pending"]["assets"][0]["status"], "omitted")
        self.assertNotEqual(omitted["pending"]["version"], attached["pending"]["version"])
        self.assertFalse(omitted["state"].get("approval"))
        self.assertFalse((f.settings.data_dir / "exports").exists())

    def test_ce041_quality_label_does_not_export_and_exact_approval_is_idempotent(self):
        f = self.fixture
        result = f.start()
        item = f.runtime.metrics.record(result["state"], "fixture")
        f.runtime.metrics.review(item["id"], item["version"], 1, "Synthetic quality label", "fixture")
        self.assertFalse((f.settings.data_dir / "exports").exists())
        with self.assertRaisesRegex(ValueError, "exact version"):
            f.runtime.resume("test", {"action": "approve", "version": "stale"})
        completed = f.runtime.resume("test", {"action": "approve", "version": result["pending"]["version"]})
        self.assertEqual(completed["status"], "approved")
        manifest = json.loads((Path(completed["state"]["export_path"]) / "manifest.json").read_text())
        self.assertFalse(manifest["published"])
        self.assertEqual(len(f.archive.briefs()), 1)
        with self.assertRaisesRegex(ValueError, "no pending"):
            f.runtime.resume("test", {"action": "approve", "version": result["pending"]["version"]})
        self.assertEqual(len(f.archive.briefs()), 1)
