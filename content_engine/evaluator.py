"""Independent editorial judgment plus deterministic checks and quote validation."""

import re

from .decisions import DecisionClient, EVALUATION_INSTRUCTIONS
from .evidence import evidence_passages
from .models import ClaimCheck, DraftPackage, EditorialBrief, EvidenceEvaluation, EvaluationReport, ReviewIssue, Source


class DraftEvaluator:
    def __init__(self, decisions: DecisionClient):
        self.decisions = decisions

    def evaluate(self, brief: EditorialBrief, drafts: DraftPackage, sources: list[Source]) -> EvaluationReport:
        passages = evidence_passages(sources)
        response = self.decisions.ask("evaluate", EVALUATION_INSTRUCTIONS, EvidenceEvaluation, {
            "brief": brief.model_dump(), "drafts": drafts.model_dump(),
            "sources": [{"title": s.title, "url": str(s.url)} for s in sources],
            "evidence_passages": list(passages.values()),
        })
        report = EvaluationReport.model_validate(response.model_dump())
        by_url = {str(source.url): source.content for source in sources}
        for claim in report.claims:
            if claim.verdict == "supported":
                if claim.evidence_id:
                    passage = passages.get(claim.evidence_id)
                    if passage and claim.source_url in (None, passage["source_url"]):
                        claim.source_url = passage["source_url"]
                        claim.supporting_quote = passage["text"]
                    else:
                        claim.verdict = "unsupported"
                        claim.supporting_quote = None
                        report.issues.append(ReviewIssue(kind="evidence", severity="error", message=f"Invalid evidence passage or URL: {claim.claim}"))
                        continue
                quote = claim.supporting_quote
                if not quote or not quote.strip() or quote not in by_url.get(claim.source_url, ""):
                    claim.verdict = "unsupported"
                    report.issues.append(ReviewIssue(kind="evidence", severity="error", message=f"Unverifiable supporting quote: {claim.claim}"))
        if any(c.verdict in {"unsupported", "conflicting"} for c in report.claims):
            report.faithfulness = min(report.faithfulness, 3)
        for url in report.fetch_urls:
            if url not in by_url:
                report.issues.append(ReviewIssue(kind="evidence", severity="warning", message=f"Ignored evaluator fetch request outside supplied evidence: {url}"))
        report.fetch_urls = [url for url in report.fetch_urls if url in by_url]
        for platform, minimum, maximum in [("linkedin", 300, 500), ("medium", 800, 1200)]:
            text = getattr(drafts, platform)
            words = len(text.split())
            if not minimum <= words <= maximum:
                report.issues.append(ReviewIssue(kind="editorial", severity="warning" if report.needs_research else "error", message=f"{platform}: {words} words; target {minimum}-{maximum}."))
            for url in re.findall(r"\]\((https?://[^\s)]+)\)", text):
                if url not in by_url:
                    report.issues.append(ReviewIssue(kind="evidence", severity="error", message=f"{platform} cites an unsupplied URL: {url}"))
            # Verify source-attributed direct quotations separately from the LLM.
            # Hypothetical dialogue without an attribution cue is excluded.
            for match in re.finditer(r'["“]([^"“”]{30,1000})["”]\*{0,2}\s*\[[^\]]+\]\((https?://[^\s)]+)\)', text):
                before = text[max(0, match.start() - 150):match.start()]
                if not re.search(r"\b(?:puts it|advises|writes|states|notes|says|according to|research notes)\b", before, re.I):
                    continue
                quote, url = match.groups()
                if " ".join(quote.split()) not in " ".join(by_url.get(url, "").split()):
                    report.claims.append(ClaimCheck(claim=f"Direct quotation: {quote}", verdict="unsupported", source_url=url,
                        explanation="The attributed quotation is absent from the supplied source text."))
                    report.issues.append(ReviewIssue(kind="evidence", severity="error", message=f"{platform}: attributed quotation not found at {url}: {quote}"))
                    report.faithfulness = min(report.faithfulness, 3)
            if re.search(r"!\[.*?\]\(", text):
                report.issues.append(ReviewIssue(kind="visual", severity="error", message="Image assets must be reviewed separately; remove embedded draft image links."))
            count = sum(v.platform == platform for v in drafts.visuals)
            if count > (1 if platform == "linkedin" else 3):
                report.issues.append(ReviewIssue(kind="visual", severity="error", message=f"Too many {platform} visual plans."))
        return report
