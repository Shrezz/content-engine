"""Versioned automatic gates and human labels; no model score is ground truth."""

import json
import sqlite3
from datetime import datetime, timezone

from .models import DraftPackage, EvaluationReport
from .visuals import digest

POLICY_VERSION = "quality-v1"
SCORE_NAMES = ("faithfulness", "substance", "voice", "structure")


def now():
    return datetime.now(timezone.utc).isoformat()


def classification_metrics(rows):
    """Positive means acceptable as-is (1); unreviewed versions are excluded."""
    counts = dict(tp=0, fp=0, fn=0, tn=0)
    for row in rows:
        actual = row.get("human_result")
        if actual is None:
            continue
        predicted = row["automatic_result"]
        counts[{(1, 1): "tp", (1, 0): "fp", (0, 1): "fn", (0, 0): "tn"}[predicted, actual]] += 1
    tp, fp, fn, tn = (counts[k] for k in ("tp", "fp", "fn", "tn"))
    divide = lambda a, b: a / b if b else None
    labelled = sum(counts.values())
    return {**counts, "reviewed": labelled, "total": len(rows),
            "precision": divide(tp, tp + fp), "recall": divide(tp, tp + fn),
            "f1": divide(2 * tp, 2 * tp + fp + fn), "accuracy": divide(tp + tn, labelled),
            "coverage": divide(labelled, len(rows))}


def assess(state):
    """A conjunctive gate, deliberately separate from the eventual human decision."""
    if not state.get("drafts") or not state.get("evaluation"):
        return None
    report = EvaluationReport.model_validate(state["evaluation"])
    drafts = DraftPackage.model_validate(state["drafts"]).model_dump()
    reasons = [f"{name.capitalize()} is below 4/5." for name in SCORE_NAMES if getattr(report, name) < 4]
    reasons += [issue.message for issue in report.issues if issue.severity == "error"]
    if report.needs_research:
        reasons.append("More research is needed.")
    if len({s["url"] for s in state.get("sources", [])}) < 5:
        reasons.append("Fewer than five unique research sources.")
    packet = state.get("packet", {})
    snapshot = packet.get("snapshot", {})
    # An old packet must never transfer approval or a human label to edited text.
    current = (snapshot.get("drafts") == drafts and snapshot.get("evaluation") == state["evaluation"]
               and snapshot.get("sources") == state.get("sources", [])
               and packet.get("version") == digest(snapshot))
    if not current:
        reasons.append("The final review package has not been prepared for this draft.")
    assets = snapshot.get("assets", []) if current else []
    if current and (len(assets) != len(drafts.get("visuals", [])) or any(a.get("status") not in {"ready", "omitted"} for a in assets)):
        reasons.append("Pictures still need a decision or a valid asset.")
    payload = {"brief": state.get("brief", {}), "drafts": drafts, "sources": state.get("sources", []),
               "evaluation": state["evaluation"], "assets": assets,
               "articles": snapshot.get("articles", {}) if current else {k: drafts[k] for k in ("linkedin", "medium")}}
    version = packet["version"] if current else digest(payload)
    return {"id": digest([state["thread_id"], version, POLICY_VERSION]), "thread_id": state["thread_id"],
            "version": version, "policy_version": POLICY_VERSION, "automatic_result": int(not reasons),
            "reasons": list(dict.fromkeys(reasons)), "scores": {name: getattr(report, name) for name in SCORE_NAMES},
            "snapshot": payload}


class ReviewMetrics:
    """SQLite assessments are immutable; subsequent labels have an audit trail."""

    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS assessments (
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, version TEXT NOT NULL,
                created_at TEXT NOT NULL, model TEXT, payload TEXT NOT NULL,
                human_result INTEGER CHECK(human_result IN (0,1)), human_notes TEXT,
                human_source TEXT, reviewed_at TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS review_labels (
                id INTEGER PRIMARY KEY, assessment_id TEXT NOT NULL, result INTEGER NOT NULL,
                notes TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL)""")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def record(self, state, model=""):
        item = assess(state)
        if item is None:
            return None
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO assessments(id,thread_id,version,created_at,model,payload) VALUES(?,?,?,?,?,?)",
                       (item["id"], item["thread_id"], item["version"], now(), model, json.dumps(item)))
        return self.get(item["id"])

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM assessments WHERE id=?", (key,)).fetchone()
        if not row:
            raise ValueError("Unknown assessment")
        data = dict(row)
        payload = json.loads(data.pop("payload"))
        return {**data, **payload}

    def rows(self):
        with self.connect() as db:
            keys = [r[0] for r in db.execute("SELECT id FROM assessments ORDER BY created_at DESC, rowid DESC")]
        return [self.get(key) for key in keys]

    def review(self, key, version, result, notes="", source="manual"):
        if type(result) is not int or result not in {0, 1}:
            raise ValueError("A human result must be the integer 0 or 1")
        if result == 0 and not notes.strip():
            raise ValueError("Explain what needs changes for a result of 0")
        item = self.get(key)
        if version != item["version"]:
            raise ValueError("Review the exact assessed version")
        if (item["human_result"], item["human_notes"], item["human_source"]) == (result, notes.strip(), source):
            return item
        timestamp = now()
        with self.connect() as db:
            db.execute("INSERT INTO review_labels(assessment_id,result,notes,source,created_at) VALUES(?,?,?,?,?)",
                       (key, result, notes.strip(), source, timestamp))
            db.execute("UPDATE assessments SET human_result=?,human_notes=?,human_source=?,reviewed_at=? WHERE id=?",
                       (result, notes.strip(), source, timestamp, key))
        return self.get(key)

    def capture(self, state, model="", approved=False, replace_label=False):
        item = self.record(state, model)
        if item and approved and (item["human_result"] is None or replace_label) and state.get("approval", {}).get("version") == item["version"]:
            item = self.review(item["id"], item["version"], 1, state["approval"].get("reason", "Approved after human review"), "approval")
        return item

    def overview(self):
        rows = self.rows()
        # Overall figures are descriptive of reviewed versions, not an unbiased benchmark.
        policies = sorted({r["policy_version"] for r in rows})
        return {"metrics": classification_metrics(rows), "policies": policies, "assessments": [self.summary(r) for r in rows]}

    @staticmethod
    def summary(item):
        return {k: v for k, v in item.items() if k != "snapshot"} | {"brief": item["snapshot"]["brief"]}
