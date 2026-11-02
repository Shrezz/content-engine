"""Durable benchmark jobs against frozen fixtures; independent of article jobs."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from .config import PROJECT_DIR
from .evals import GoldenEvaluator
from .metrics import ReviewMetrics, now
from .tracing import RunTrace
from .visuals import digest


class EvaluationRuns:
    def __init__(self, settings, evaluator_factory=GoldenEvaluator, dataset_path=None, legacy_paths=None):
        self.settings = settings
        self.store = ReviewMetrics(settings.metrics_db)
        self.factory = evaluator_factory
        self.dataset_path = dataset_path or PROJECT_DIR / "evals" / "golden.json"
        self.lock = Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="benchmark")
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS benchmark_runs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
                selection TEXT NOT NULL, report TEXT, error TEXT)""")
            db.execute("UPDATE benchmark_runs SET status='interrupted', error='The server stopped before this benchmark completed. Start a new run to retry.' WHERE status IN ('running','queued')")
        # Import saved CLI reports without performing any model calls.
        paths = list((settings.data_dir / "evaluations").glob("*.json"))
        if legacy_paths is None:
            legacy_paths = [PROJECT_DIR / "work" / name for name in (
                "seed-evaluation.json", "seed-evaluation-refined.json",
                "held-out-seed-evaluation.json", "delegation-evaluation.json")]
        for path in paths + list(legacy_paths):
            self.import_report(path)

    def catalog(self):
        dataset = json.loads(self.dataset_path.read_text())
        return {"version": dataset["version"], "reference_status": dataset.get("reference_status", "unreviewed"),
                "cases": [{"id": c["id"], "split": c["split"], "target": c["target"]} for c in dataset["cases"]]}

    def import_report(self, path):
        if not path.is_file():
            return
        try:
            report = json.loads(path.read_text())
            if not isinstance(report.get("cases"), list) or not report.get("total"):
                return
            key = report.get("run_id") or "saved-" + digest(report)[:24]
            created = report.get("created_at") or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            with self.store.connect() as db:
                db.execute("INSERT OR IGNORE INTO benchmark_runs(id,status,created_at,selection,report) VALUES(?,'completed',?,?,?)",
                           (key, created, json.dumps({"split": report.get("split", "unknown"), "imported": True}), json.dumps(report)))
        except (ValueError, OSError, TypeError, AttributeError):
            return

    def get(self, key):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM benchmark_runs WHERE id=?", (key,)).fetchone()
        if not row:
            raise ValueError("Unknown evaluation run")
        data = dict(row)
        data["selection"] = json.loads(data["selection"])
        data["report"] = json.loads(data["report"]) if data["report"] else None
        return data

    def rows(self):
        with self.store.connect() as db:
            keys = [r[0] for r in db.execute("SELECT id FROM benchmark_runs ORDER BY created_at DESC")]
        return [self.summary(self.get(key)) for key in keys]

    @staticmethod
    def summary(item):
        report = item.get("report") or {}
        initial = [r["baseline"]["passed"] for r in report.get("cases", []) if r.get("baseline")]
        return {**{k: v for k, v in item.items() if k != "report"},
                **{k: report.get(k) for k in ("total", "passed", "pass_rate", "model", "reference_status", "dataset_version", "prompt_fingerprint")},
                "baseline_passed": sum(initial) if initial else None, "baseline_total": len(initial)}

    def launch(self, *, split="dev", compare_baseline=False, case_ids=None):
        catalog = self.catalog()
        eligible = {c["id"] for c in catalog["cases"] if split == "all" or c["split"] == split}
        if not eligible or (case_ids is not None and (not case_ids or not set(case_ids) <= eligible)):
            raise ValueError("Select existing cases within the chosen split")
        selection = {"split": split, "compare_baseline": compare_baseline, "case_ids": case_ids}
        with self.lock:
            with self.store.connect() as db:
                if db.execute("SELECT 1 FROM benchmark_runs WHERE status IN ('queued','running')").fetchone():
                    raise ValueError("An evaluation run is already in progress")
                key = uuid4().hex
                db.execute("INSERT INTO benchmark_runs(id,status,created_at,selection) VALUES(?,'queued',?,?)", (key, now(), json.dumps(selection)))
            self.pool.submit(self._run, key, selection)
        return self.summary(self.get(key))

    def _run(self, key, selection):
        trace = RunTrace(self.settings.data_dir / "traces", secrets=(self.settings.nebius_api_key, self.settings.keenable_api_key), console=False)
        with self.store.connect() as db:
            db.execute("UPDATE benchmark_runs SET status='running' WHERE id=?", (key,))
        try:
            report = self.factory(self.settings, trace).run(self.dataset_path, **selection)
            report.update(run_id=key, created_at=now())
            directory = self.settings.data_dir / "evaluations"
            directory.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(report, indent=2)
            for secret in (self.settings.nebius_api_key, self.settings.keenable_api_key):
                if secret:
                    serialized = serialized.replace(json.dumps(secret)[1:-1], "[redacted]")
            (directory / f"{key}.json").write_text(serialized)
            with self.store.connect() as db:
                db.execute("UPDATE benchmark_runs SET status='completed', report=? WHERE id=?", (serialized, key))
        except Exception as error:
            message = str(error)
            for secret in (self.settings.nebius_api_key, self.settings.keenable_api_key):
                if secret:
                    message = message.replace(secret, "[redacted]")
            with self.store.connect() as db:
                db.execute("UPDATE benchmark_runs SET status='failed',error=? WHERE id=?", (message, key))

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
