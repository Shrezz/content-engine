"""Check label semantics, version isolation and undefined metric denominators."""
import tempfile
import unittest
from pathlib import Path

from content_engine.config import Settings
from content_engine.metrics import ReviewMetrics, assess, classification_metrics
from content_engine.visuals import VisualService
from test_workflow import BRIEF, DRAFTS, SOURCES, report


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.store = ReviewMetrics(self.settings.metrics_db)
        self.state = {"thread_id": "example", "brief": BRIEF, "drafts": DRAFTS.model_dump(),
                      "sources": [s.model_dump(mode="json") for s in SOURCES], "evaluation": report().model_dump()}
        self.state["packet"] = VisualService(self.settings).prepare("example", DRAFTS, self.state["sources"], self.state["evaluation"], {})

    def test_confusion_matrix_and_unlabelled_exclusion(self):
        rows = [{"automatic_result": p, "human_result": h} for p, h in [(1,1),(1,0),(0,1),(0,0),(1,None)]]
        metrics = classification_metrics(rows)
        self.assertEqual([metrics[k] for k in ("tp","fp","fn","tn")], [1,1,1,1])
        self.assertEqual(metrics["precision"], .5)
        self.assertEqual(metrics["recall"], .5)
        self.assertEqual(metrics["f1"], .5)
        self.assertEqual(metrics["accuracy"], .5)
        self.assertEqual(metrics["coverage"], .8)

    def test_asymmetric_counts_distinguish_precision_from_recall(self):
        pairs = [(1,1)]*2 + [(1,0)] + [(0,1)]*3 + [(0,0)]*4
        m = classification_metrics([{"automatic_result": p, "human_result": h} for p,h in pairs])
        self.assertAlmostEqual(m["precision"], 2/3)
        self.assertAlmostEqual(m["recall"], 2/5)
        self.assertEqual(m["f1"], .5)
        m = classification_metrics([{"automatic_result":0,"human_result":1}])
        self.assertIsNone(m["precision"])
        self.assertEqual(m["recall"],0)
        self.assertEqual(m["f1"],0)

    def test_empty_denominators_are_not_zero_or_a_perfect_score(self):
        for rows in ([], [{"automatic_result":1,"human_result":None}], [{"automatic_result":0,"human_result":0}]):
            m = classification_metrics(rows)
            self.assertIsNone(m["precision"])
            self.assertIsNone(m["recall"])
        self.assertIsNone(classification_metrics([])["f1"])

    def test_gate_uses_all_requirements_and_only_current_packet(self):
        self.assertEqual(assess(self.state)["automatic_result"], 1)
        self.state["evaluation"] = report(faithfulness=3).model_dump()
        result = assess(self.state)
        self.assertEqual(result["automatic_result"], 0)
        self.assertIn("Faithfulness is below 4/5.", result["reasons"])
        self.assertTrue(any("not been prepared" in r for r in result["reasons"]))
        self.state["sources"] = self.state["sources"][:2]
        self.assertTrue(any("five" in r for r in assess(self.state)["reasons"]))

    def test_human_label_is_bound_to_version_and_has_an_audit(self):
        old = self.store.record(self.state)
        self.assertIsNone(old["human_result"])
        self.store.review(old["id"], old["version"], 1, "Reviewed")
        self.state["drafts"]["medium"] += " Changed argument."
        new = self.store.record(self.state)
        self.assertNotEqual(old["id"],new["id"])
        self.assertIsNone(new["human_result"])
        self.assertEqual(self.store.get(old["id"])["human_result"],1)
        self.store.review(old["id"], old["version"], 0, "Missed an unsupported claim")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM review_labels").fetchone()[0],2)
        self.assertEqual(self.store.overview()["metrics"]["fp"],1)

    def test_invalid_labels_and_versions_are_rejected(self):
        item = self.store.record(self.state)
        for result, notes in [(True,"boolean"),(2,"invalid"),(0,"")]:
            with self.assertRaises(ValueError): self.store.review(item["id"], item["version"], result, notes)
        with self.assertRaises(ValueError): self.store.review(item["id"], "old", 1)
        self.assertIsNone(self.store.get(item["id"])["human_result"])

    def test_recording_and_repeated_approval_are_idempotent(self):
        item = self.store.record(self.state)
        self.state["approval"] = {"version":item["version"],"action":"approve"}
        self.store.capture(self.state, approved=True)
        self.store.capture(self.state, approved=True)
        self.assertEqual(len(self.store.rows()),1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM review_labels").fetchone()[0],1)
        # A later explicit correction must survive server restart/backfill.
        self.store.review(item["id"], item["version"], 0, "Found a missed problem")
        self.assertEqual(self.store.capture(self.state, approved=True)["human_result"],0)
