"""Independent editorial judgment plus deterministic checks and quote validation."""

import re

from .decisions import DecisionClient, EVALUATION_INSTRUCTIONS
from .models import DraftPackage, EditorialBrief, EvaluationReport, ReviewIssue, Source


class DraftEvaluator:
    def __init__(self, decisions: DecisionClient):
        self.decisions = decisions

    def evaluate(self, brief: EditorialBrief, drafts: DraftPackage, sources: list[Source]) -> EvaluationReport:
        report = self.decisions.ask("evaluate", EVALUATION_INSTRUCTIONS, EvaluationReport, {
            "brief": brief.model_dump(), "drafts": drafts.model_dump(),
            "sources": [s.model_dump(mode="json") for s in sources],
        })
        by_url = {str(source.url): source.content for source in sources}
        for claim in report.claims:
            if claim.verdict == "supported":
                quote = claim.supporting_quote
                if not quote or not quote.strip() or quote not in by_url.get(claim.source_url, ""):
                    claim.verdict = "unsupported"
                    report.issues.append(ReviewIssue(kind="evidence", severity="error", message=f"Unverifiable supporting quote: {claim.claim}"))
        if any(c.verdict != "supported" for c in report.claims):
            report.faithfulness = min(report.faithfulness, 3)
        for url in report.fetch_urls:
            if url not in by_url:
                raise ValueError("Evaluator requested a URL outside the supplied evidence")
        for platform, minimum, maximum in [("linkedin", 300, 500), ("medium", 800, 1200)]:
            text = getattr(drafts, platform)
            words = len(text.split())
            if not minimum <= words <= maximum:
                report.issues.append(ReviewIssue(kind="editorial", severity="warning" if report.needs_research else "error", message=f"{platform}: {words} words; target {minimum}-{maximum}."))
            for url in re.findall(r"\]\((https?://[^\s)]+)\)", text):
                if url not in by_url:
                    report.issues.append(ReviewIssue(kind="evidence", severity="error", message=f"{platform} cites an unsupplied URL: {url}"))
            if re.search(r"!\[.*?\]\(", text):
                report.issues.append(ReviewIssue(kind="visual", severity="error", message="Image assets must be reviewed separately; remove embedded draft image links."))
            count = sum(v.platform == platform for v in drafts.visuals)
            if count > (1 if platform == "linkedin" else 3):
                report.issues.append(ReviewIssue(kind="visual", severity="error", message=f"Too many {platform} visual plans."))
        return report
