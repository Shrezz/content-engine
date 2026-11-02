"""Evaluate HTTP semantics using temporary data and controlled providers only."""
import json
import time
import unittest
from pathlib import Path

from content_engine.evaluation_runs import EvaluationRuns
from content_engine.models import VisualPlan
from threading import Event
import test_web as web_fixtures


class EvaluationUITests(unittest.TestCase):
    setUp = web_fixtures.WebTests.setUp
    post = web_fixtures.WebTests.post
    wait = web_fixtures.WebTests.wait
    start = web_fixtures.WebTests.start

    def test_new_article_is_scored_without_fabricated_human_label(self):
        current = self.start()
        assessment = current["assessment"]
        self.assertEqual(assessment["automatic_result"], 1)
        self.assertIsNone(assessment["human_result"])
        d = self.client.get("/api/evals").json()
        self.assertEqual(d["metrics"]["total"],1)
        self.assertIsNone(d["metrics"]["precision"])
        self.assertEqual(len(d["dataset"]["cases"]),7)
        self.assertEqual(d["runs"],[])

    def test_manual_quality_rating_updates_metrics_without_approving_export(self):
        current = self.start(); a = current["assessment"]; key = current["thread_id"]
        reviewed = self.post(f'/api/evals/assessments/{a["id"]}/review', {"version":a["version"],"result":0,"notes":"The argument is thin"})
        self.assertEqual(reviewed.status_code,200,reviewed.text)
        self.assertEqual(self.client.get("/api/evals").json()["metrics"]["fp"],1)
        self.assertEqual(self.client.get(f"/api/sessions/{key}/download").status_code,409)
        self.assertEqual(self.client.get(f"/api/sessions/{key}").json()["pending"]["version"],a["version"])
        self.assertEqual(self.client.get(f'/api/evals/assessments/{a["id"]}').json()["snapshot"]["drafts"],current["state"]["drafts"])

    def test_wrong_version_boolean_and_unexplained_zero_cannot_label_a_draft(self):
        a = self.start()["assessment"]
        for body in ({"version":"stale","result":1}, {"version":a["version"],"result":True}, {"version":a["version"],"result":0}):
            self.assertIn(self.post(f'/api/evals/assessments/{a["id"]}/review',body).status_code,(409,422))
        self.assertEqual(self.client.get("/api/evals").json()["metrics"]["reviewed"],0)

    def test_approval_records_positive_and_cancel_stays_unlabelled(self):
        current = self.start(); key = current["thread_id"]
        self.post(f"/api/sessions/{key}/respond", {"action":"cancel","version":current["pending"]["version"]})
        self.assertIsNone(self.wait(key)["assessment"]["human_result"])
        current = self.start(); key = current["thread_id"]
        self.post(f"/api/sessions/{key}/respond", {"action":"approve","version":current["pending"]["version"]})
        self.assertEqual(self.wait(key)["assessment"]["human_result"],1)
        m = self.client.get("/api/evals").json()["metrics"]
        self.assertEqual((m["tp"],m["reviewed"],m["total"]),(1,1,2))

    def test_edit_records_old_version_zero_but_new_version_needs_review(self):
        current = self.start(); key = current["thread_id"]
        drafts = dict(current["state"]["drafts"]); drafts["medium"] += " A human improvement."
        self.post(f"/api/sessions/{key}/respond", {"action":"edit","version":current["pending"]["version"],"drafts":drafts})
        revised = self.wait(key)
        self.assertNotEqual(revised["assessment"]["id"],current["assessment"]["id"])
        self.assertIsNone(revised["assessment"]["human_result"])
        self.assertEqual(self.app.state.metrics.get(current["assessment"]["id"])["human_result"],0)

    def test_eval_run_uses_selection_and_persists_a_report(self):
        calls = []
        class FakeEvaluator:
            def __init__(self, settings, trace): pass
            def run(self, path, **selection):
                calls.append(selection)
                return {"dataset_version":"fixture", "reference_status":"candidate", "model":"fake", "split":selection["split"], "total":1,"passed":1,"pass_rate":1,"cases":[{"id":selection["case_ids"][0],"passed":True}]}
        self.app.state.benchmarks.factory = FakeEvaluator
        case = next(c for c in self.client.get("/api/evals").json()["dataset"]["cases"] if c["split"] == "dev")
        response = self.post("/api/evals/runs", {"split":"dev","case_ids":[case["id"]],"compare_baseline":True})
        self.assertEqual(response.status_code,202,response.text)
        key = response.json()["id"]
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            run = self.client.get(f"/api/evals/runs/{key}").json()
            if run["status"] not in {"running","queued"}: break
            time.sleep(.01)
        self.assertEqual(run["status"],"completed",run)
        self.assertEqual(run["report"]["passed"],1)
        self.assertTrue(calls[0]["compare_baseline"])
        self.assertTrue((self.fixture.settings.data_dir / "evaluations" / f"{key}.json").is_file())
        self.assertEqual(self.client.get("/api/evals").json()["metrics"]["reviewed"],0)

    def test_unknown_or_mismatched_split_cases_do_not_start_paid_jobs(self):
        result = self.post("/api/evals/runs", {"split":"dev","case_ids":["not-a-case"]})
        self.assertEqual(result.status_code,409)
        self.assertEqual(self.client.get("/api/evals").json()["runs"],[])

    def test_saved_cli_report_import_is_idempotent_and_survives_restart(self):
        runs = self.app.state.benchmarks
        path = self.fixture.settings.data_dir / "saved.json"
        path.write_text(json.dumps({"total":1,"passed":0,"pass_rate":0,"split":"test","cases":[{"id":"example","passed":False}]}))
        runs.import_report(path); runs.import_report(path)
        self.assertEqual(len(runs.rows()),1)
        restarted = EvaluationRuns(self.fixture.settings, legacy_paths=[])
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.rows()[0]["passed"],0)

    def test_cross_site_human_review_is_blocked(self):
        a = self.start()["assessment"]
        result = self.client.post(f'/api/evals/assessments/{a["id"]}/review',json={"version":a["version"],"result":1},headers={"X-Content-Engine":"studio","Origin":"https://elsewhere.invalid"})
        self.assertEqual(result.status_code,403)
        self.assertEqual(self.client.get("/api/evals").json()["metrics"]["reviewed"],0)


    def test_pending_picture_returns_zero_and_omission_gets_a_new_assessment(self):
        plan = VisualPlan(platform="medium", placement="End", description="Illustrate the argument", caption="A relevant illustration", alt_text="A scene")
        self.fixture.drafts = self.fixture.drafts.model_copy(update={"visuals":[plan]})
        current = self.start(); key = current["thread_id"]; a = current["assessment"]
        self.assertEqual(a["automatic_result"],0)
        self.post(f'/api/evals/assessments/{a["id"]}/review', {"version":a["version"],"result":0,"notes":"Resolve the illustration"})
        self.post(f"/api/sessions/{key}/respond", {"action":"assets","version":current["pending"]["version"],"choices":{"0":{"action":"omit","reason":"The text explains it clearly"}}})
        revised = self.wait(key)["assessment"]
        self.assertEqual(revised["automatic_result"],1)
        self.assertIsNone(revised["human_result"])
        self.assertNotEqual(revised["id"],a["id"])

    def test_concurrent_benchmarks_are_rejected_and_provider_errors_are_redacted(self):
        entered, release = Event(), Event()
        secret = self.fixture.settings.nebius_api_key
        class FailingEvaluator:
            def __init__(self, settings, trace): pass
            def run(self, path, **selection):
                entered.set(); release.wait(2)
                raise RuntimeError("Provider rejected " + secret)
        runs = self.app.state.benchmarks
        runs.factory = FailingEvaluator
        job = runs.launch(split="dev")
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaises(ValueError): runs.launch(split="dev")
        finally: release.set()
        deadline = time.monotonic()+3
        while time.monotonic()<deadline and runs.get(job["id"])["status"] == "running": time.sleep(.01)
        result = runs.get(job["id"])
        self.assertEqual(result["status"],"failed")
        self.assertNotIn(secret,result["error"])
        self.assertIn("[redacted]",result["error"])

    def test_restart_marks_unfinished_benchmark_interrupted_without_running_it(self):
        runs = self.app.state.benchmarks
        with runs.store.connect() as db:
            db.execute("INSERT INTO benchmark_runs(id,status,created_at,selection) VALUES('unfinished','running','2026-11-01','{}')")
        restarted = EvaluationRuns(self.fixture.settings, legacy_paths=[])
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.get("unfinished")["status"],"interrupted")


    def test_explicit_approval_replaces_an_earlier_negative_quality_rating(self):
        current = self.start(); a = current["assessment"]; key = current["thread_id"]
        self.post(f'/api/evals/assessments/{a["id"]}/review', {"version":a["version"],"result":0,"notes":"Initial concern"})
        self.post(f"/api/sessions/{key}/respond", {"action":"approve","version":current["pending"]["version"]})
        accepted = self.wait(key)
        self.assertEqual(accepted["assessment"]["human_result"],1)
        self.assertEqual(accepted["assessment"]["human_source"],"approval")
