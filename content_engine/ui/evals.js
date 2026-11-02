/* Evaluation views: persisted scores, exact-version human reviews, and benchmarks. */
const rate = (n) => (n == null ? "—" : `${Math.round(n * 100)}%`);
const binary = (n, human = false) =>
  n == null
    ? '<span class="badge gray">Not reviewed</span>'
    : `<span class="badge ${n ? "" : "amber"}">${n} · ${human ? (n ? "Acceptable" : "Needs changes") : n ? "Pass" : "Needs review"}</span>`;
const evalDate = (date) =>
  new Date(date).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });

async function showEvals(section = S.evalSection || "articles") {
  clearTimeout(S.poll);
  if (S.editing) return toast("Save or discard your text edits first.");
  S.view = "evals";
  S.evalSignature = "";
  S.evalSection = section;
  history.replaceState({}, "", "/?view=evals");
  renderSidebar();
  $("#page-location").textContent = "Evaluations";
  await loadEvals();
}

async function loadEvals() {
  if (S.view !== "evals") return;
  clearTimeout(S.poll);
  const ticket = (S.evalRequestSeq = (S.evalRequestSeq || 0) + 1);
  try {
    const data = await api("/api/evals");
    if (S.view !== "evals" || ticket !== S.evalRequestSeq) return;
    S.evals = data;
    const signature = JSON.stringify([data, S.evalSection]);
    if (!modal.open && signature !== S.evalSignature) {
      S.evalSignature = signature;
      renderEvals();
    }
  } catch (error) {
    toast(error.message, true);
  }
  if (S.view === "evals") S.poll = setTimeout(loadEvals, 4000);
}

function renderEvals() {
  const d = S.evals,
    m = d.metrics;
  const metric = (label, value, note) =>
    `<section class="metric-card"><div class="eyebrow">${label}</div><strong>${value}</strong><p>${note}</p></section>`;
  $("#main").innerHTML = `
    <div class="page-intro"><div><div class="eyebrow">QUALITY YOU CAN INSPECT</div><h1>Better writing. Measured.</h1><p>Compare the agent’s quality gate with your judgment, one draft version at a time.</p></div><div class="intro-actions"><button class="secondary" data-eval-action="run">${icon("activity")} Run dataset eval</button></div></div>
    <ol class="review-sequence" aria-label="Editorial review order"><li><span>1</span> Draft</li><li><span>2</span> LLM review</li><li><span>3</span> Rewrite & re-review if needed</li><li><span>4</span> Human review</li></ol>
    <div class="eval-metrics">${metric("PRECISION", rate(m.precision), `${m.tp} accepted / ${m.tp + m.fp} automatic passes`)}${metric("RECALL", rate(m.recall), `${m.tp} caught / ${m.tp + m.fn} human acceptances`)}${metric("F1 SCORE", rate(m.f1), "Balance of precision and recall")}${metric("HUMAN REVIEWED", `${m.reviewed}<span> / ${m.total}</span>`, "Assessed draft versions with a 0 or 1 label")}</div>
    <div class="eval-explainer"><span class="eval-positive">1 = acceptable as-is</span><p>0 = needs changes. Only human-labelled versions enter these metrics. A dash means the denominator is zero.</p></div>
    <div class="eval-confusion"><span><b>${m.tp}</b> True positives</span><span><b>${m.fp}</b> False positives</span><span><b>${m.fn}</b> False negatives</span><span><b>${m.tn}</b> True negatives</span><span>Agreement <b>${rate(m.accuracy)}</b></span></div>
    <p class="eval-caption">These figures describe your reviewed versions, including revisions of the same article. They are not a held-out estimate of general performance.</p>
    <div class="eval-tabs"><button data-eval-section="articles" class="${S.evalSection === "articles" ? "active" : ""}">Article quality <span>${d.assessments.length}</span></button><button data-eval-section="datasets" class="${S.evalSection === "datasets" ? "active" : ""}">Dataset runs <span>${d.runs.length}</span></button></div>
    ${S.evalSection === "datasets" ? datasetRuns(d) : articleAssessments(d)}`;
}

function llmReviewSummary(scores) {
  const names = {
    faithfulness: "Facts",
    substance: "Depth",
    voice: "Voice",
    structure: "Structure",
  };
  return `<span class="badge gray">Reviewed</span><div class="llm-score-summary" aria-label="LLM review scores">${Object.entries(
    names,
  )
    .map(([key, label]) => `<span>${label} <b>${scores[key]}/5</b></span>`)
    .join("")}</div>`;
}

function reviewFindings(report) {
  const issues = report.issues || [];
  return `<details class="eval-draft" ${issues.length || report.needs_research ? "open" : ""}><summary>Review feedback & validation findings (${issues.length})</summary><p class="modal-note">The critic supplies editorial and evidence feedback. Code also checks source quotes, citations, length, and visual limits.</p>${report.needs_research ? '<p class="finding error">The reviewer requested stronger evidence before the argument can be supported.</p>' : ""}${issues.length ? issues.map((i) => `<div class="finding ${esc(i.severity)}"><strong>${esc(i.kind)} · ${esc(i.severity)}</strong><p>${esc(i.message)}</p></div>`).join("") : "<p>No findings were flagged in the saved review.</p>"}</details>`;
}

function articleAssessments(d) {
  return `<div class="eval-section-head"><div><h2>The agent’s result. Your final call.</h2><p>The LLM critic reviews the draft first. The quality gate combines its findings with code checks, then you make the final decision for that version.</p></div></div>
    ${d.assessments.length ? `<div class="eval-table-wrap"><table class="eval-table llm-review-table"><thead><tr><th>Article / version</th><th>LLM review</th><th>Quality gate</th><th>Human review</th><th><span class="sr-only">Review</span></th></tr></thead><tbody>${d.assessments.map((a) => `<tr><td><strong>${esc(a.brief.topic)}</strong><p>${esc(a.brief.angle)}</p><small>${evalDate(a.created_at)} · ${esc(a.version.slice(0, 8))} · ${esc(a.policy_version)}</small></td><td class="llm-review-cell">${llmReviewSummary(a.scores)}</td><td>${binary(a.automatic_result)}<small>${a.reasons.length ? `${a.reasons.length} open finding${a.reasons.length === 1 ? "" : "s"}` : "All gate checks passed"}</small></td><td>${binary(a.human_result, true)}${a.human_source ? `<small>${esc(a.human_source)} · ${evalDate(a.reviewed_at)}</small>` : ""}</td><td><button class="secondary" data-assessment="${esc(a.id)}">Read & review ${icon("arrow")}</button></td></tr>`).join("")}</tbody></table></div>` : `<div class="empty-state"><div class="empty-icon">${icon("shield")}</div><h2>Start with a draft. Build a signal.</h2><p>Your first evaluated article will appear here automatically. Record a human review to begin measuring agreement.</p><button class="primary" data-action="new">Start an article ${icon("arrow")}</button></div>`}
    <details class="paper eval-policy"><summary>What makes the automatic result 1?</summary><p>Faithfulness, substance, voice, and structure must each score at least 4/5. There must be no error findings or request for more research, at least five unique sources, a current review package, and a decision for every picture. Otherwise the gate returns 0 with reasons.</p><p>These checks combine rules and an LLM critic; a pass does not establish factual truth. Your review remains the reference. Approve & export records human 1; Request changes or editing records human 0. Stopping a draft does not create a quality label.</p></details>`;
}

function datasetRuns(d) {
  return `<div class="eval-section-head"><div><h2>Test the workflow against a fixed set.</h2><p>${esc(d.dataset.version)} · ${d.dataset.cases.length} cases · frozen evidence</p></div></div><div class="banner"><div><strong>Candidate references need human approval.</strong><p>Suite pass rate measures whether the expected behavior and rubric checks pass. It is separate from article precision / recall. Dev cases are for tuning; reserve test cases for checks after changes.</p></div></div>
    ${d.runs.length ? `<div class="dataset-grid">${d.runs.map((r) => `<section class="dataset-card"><div class="picture-top"><span class="eyebrow">${esc(r.selection.split)} SPLIT</span><span class="badge ${r.status === "completed" ? "" : "amber"}">${esc(r.status)}</span></div><div class="dataset-result"><strong>${r.total ? `${r.passed}<span> / ${r.total}</span>` : "—"}</strong><span>${r.total ? `${rate(r.pass_rate)} cases passed` : "Waiting for a completed result"}</span></div>${r.total ? `<progress max="${r.total}" value="${r.passed}" aria-label="Cases passed"></progress>` : ""}<p>${esc(r.model || "Configured model")}<br>${evalDate(r.created_at)}</p>${r.baseline_total ? `<p class="baseline-note">Initial draft baseline: ${r.baseline_passed}/${r.baseline_total} writing cases passed.</p>` : ""}${r.error ? `<p class="finding error">${esc(r.error)}</p>` : ""}<div class="history-footer"><small>${r.prompt_fingerprint ? `Prompts ${esc(r.prompt_fingerprint.slice(0, 8))}` : "Saved report · prompt version unknown"}</small><button class="text-button" data-eval-run="${esc(r.id)}">Inspect run ${icon("arrow")}</button></div></section>`).join("")}</div>` : '<div class="paper empty-small">No dataset runs saved yet. Choose a small dev case to begin.</div>'}`;
}

function assessmentPanel() {
  const a = S.data?.assessment;
  if (!a) return "";
  return `<section class="paper eval-aside"><div class="eyebrow">REVIEW RESULTS</div><p>LLM review completed. Your decision comes after the critic and quality checks.</p><div class="eval-gate-line">${binary(a.automatic_result)}<small>LLM + rules</small></div><div class="eval-gate-line">${binary(a.human_result, true)}<small>Human</small></div><p>${a.reasons.length ? esc(a.reasons[0]) : "All automatic gate checks passed."}</p><button class="text-button" data-assessment="${esc(a.id)}">Read & record review ${icon("arrow")}</button></section>`;
}

async function readAssessment(key) {
  const a = await api(`/api/evals/assessments/${key}`);
  const snapshot = a.snapshot;
  const issueMessages = new Set(
    snapshot.evaluation.issues.map((i) => i.message),
  );
  const gateReasons = a.reasons.filter((reason) => !issueMessages.has(reason));
  openModal(
    "EXACT-VERSION QUALITY REVIEW",
    a.snapshot.brief.topic,
    `Version ${a.version.slice(0, 8)} · ${a.policy_version}. A quality rating saves a label; it does not export or publish.`,
    `
    <section class="llm-review-report" aria-labelledby="llm-review-heading"><div class="eyebrow">STEP 1 · LLM REVIEW</div><h3 id="llm-review-heading">The critic’s review</h3><p>A separate LLM call reviewed the brief, both drafts, and supplied sources. These are the saved critic scores with evidence validation.</p>
    <div class="eval-score-strip">${Object.entries(a.scores)
      .map(([name, value]) => `<span>${esc(name)} <b>${value}/5</b></span>`)
      .join("")}</div>
    </section>
    <div class="eval-review-heading">${binary(a.automatic_result)}<span>Quality gate · LLM + rules</span>${binary(a.human_result, true)}<span>Human review</span></div>
    ${reviewFindings(snapshot.evaluation)}
    ${gateReasons.length ? `<div class="eval-reasons"><strong>Other quality gate findings</strong><ul>${gateReasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>` : ""}
    ${Object.entries(a.articles)
      .map(
        ([platform, html]) =>
          `<details class="eval-draft" ${platform === "linkedin" ? "open" : ""}><summary>${esc(platform === "linkedin" ? "LinkedIn article" : "Medium article")}</summary><article class="article">${html}</article></details>`,
      )
      .join("")}
    <details class="eval-draft"><summary>Evidence & claim checks (${snapshot.sources.length} sources)</summary>${(snapshot.evaluation.claims || []).map((c) => `<div class="claim-card"><strong>${esc(c.verdict)} · ${esc(c.claim)}</strong><p>${esc(c.explanation)}</p><blockquote>${esc(c.supporting_quote)}</blockquote></div>`).join("")}${sourceCards(snapshot.sources)}</details>
    <form id="quality-rating" data-id="${esc(a.id)}" data-version="${esc(a.version)}"><fieldset class="eval-rating"><legend>Step 2 · Your final quality result</legend><label><input type="radio" name="result" value="1" required ${a.human_result === 1 ? "checked" : ""}><span><strong>1 · Acceptable as-is</strong><small>I would accept this exact version.</small></span></label><label><input type="radio" name="result" value="0" required ${a.human_result === 0 ? "checked" : ""}><span><strong>0 · Needs changes</strong><small>Something must change before I accept it.</small></span></label></fieldset><div class="field"><label for="quality-notes">Review notes <span class="muted">(required for 0)</span></label><textarea id="quality-notes" name="notes" placeholder="Do you agree with the critic? What needs to improve?">${esc(a.human_notes || "")}</textarea></div><div class="form-actions"><button class="primary" type="submit">Save human review ${icon("check")}</button><button class="text-button" data-action="close" type="button">Close</button></div></form>`,
  );
}

function runEvalModal() {
  const d = S.evals?.dataset;
  if (!d)
    return api("/api/evals").then((data) => {
      S.evals = data;
      runEvalModal();
    });
  openModal(
    "RUN A FROZEN DATASET",
    "Give the workflow a test.",
    "This uses your configured LLM and model credits. Research comes from frozen fixtures; no new web search runs.",
    `<form id="eval-run"><div class="field"><label for="eval-split">Dataset split</label><select name="split" id="eval-split"><option value="dev">Dev · tune and inspect</option><option value="test">Test · held-out check</option><option value="all">All · development and test</option></select></div><fieldset class="eval-case-select"><legend>Cases to run</legend><div id="eval-case-options"></div></fieldset><label class="checkbox-label"><input type="checkbox" name="baseline"><span>Also grade the initial draft as a baseline for writing cases (extra model calls).</span></label><p class="modal-note">References are still candidates. Each case gets a 0/1 result and an explanation; this run does not create human article labels.</p><div class="form-actions"><button class="primary" type="submit" ${S.evals.runs.some((r) => ["running", "queued"].includes(r.status)) || !S.configured ? "disabled" : ""}>Run selected cases ${icon("arrow")}</button><button class="text-button" type="button" data-action="close">Cancel</button></div></form>`,
  );
  renderEvalCases();
}
function renderEvalCases() {
  const split = $("#eval-split").value;
  $("#eval-case-options").innerHTML = S.evals.dataset.cases
    .filter((c) => split === "all" || c.split === split)
    .map(
      (c) =>
        `<label class="checkbox-label"><input type="checkbox" name="case" value="${esc(c.id)}" checked><span><strong>${esc(c.id)}</strong> · ${esc(c.target)} · ${esc(c.split)}</span></label>`,
    )
    .join("");
}
async function readEvalRun(key) {
  const run = await api(`/api/evals/runs/${key}`),
    report = run.report;
  openModal(
    "DATASET RUN",
    report
      ? `${report.passed} of ${report.total} cases passed.`
      : `Run ${run.status}`,
    report
      ? `${report.model} · ${report.split} · ${report.reference_status}`
      : run.error || "Close this dialog to see updated run status.",
    report
      ? `
    <p class="modal-note">Pass requires the expected route, runtime quality checks, and every reference criterion. Candidate references and model-judged checks are provisional.</p>
    ${report.cases.map(renderEvalCase).join("")}`
      : '<p class="modal-note">The run is saved and will remain visible if you leave this page.</p>',
  );
}

function renderEvalCase(c) {
  const evaluation = c.output?.evaluation;
  const route = c.expected
    ? `<p>Expected: <b>${esc(c.expected)}</b> · Actual: <b>${esc(c.output?.status || c.output?.decision || "No output")}</b></p>`
    : '<p class="muted">Expected route was not recorded in this older report.</p>';
  const checks = (c.checks || [])
    .map(
      (check) =>
        `<div class="eval-check"><span class="badge ${check.passed ? "" : "amber"}">${check.passed ? "1" : "0"}</span><div><strong>${esc(check.criterion)}</strong><p>${esc(check.reason)}</p></div></div>`,
    )
    .join("");
  const quality = evaluation
    ? `<div class="eval-reasons"><strong>Runtime quality: ${c.runtime_quality_passed ? "passed" : "did not pass"}</strong><p>${["faithfulness", "substance", "voice", "structure"].map((k) => `${esc(k)} ${evaluation[k]}/5`).join(" · ")}</p>${evaluation.needs_research ? "<p>More research requested.</p>" : ""}${(evaluation.issues || []).map((i) => `<p>${esc(i.severity)} · ${esc(i.message)}</p>`).join("")}</div>`
    : "";
  const baseline = c.baseline
    ? `<details><summary>Initial draft baseline · ${c.baseline.passed ? "1 · Pass" : "0 · Fail"}</summary>${c.baseline.checks.map((b) => `<p><b>${b.passed ? 1 : 0}</b> · ${esc(b.criterion)}<br>${esc(b.reason)}</p>`).join("")}<pre>${esc(JSON.stringify(c.baseline.drafts, null, 2))}</pre></details>`
    : "";
  return `<details class="eval-case-result" ${c.passed ? "" : "open"}><summary>${binary(Number(c.passed))}<strong>${esc(c.id)}</strong><span>${c.duration_seconds ?? "—"}s</span></summary><div class="eval-case-body">${route}${c.status_matches === false ? '<p class="finding error">The model took a different route from the expected one.</p>' : ""}${c.error ? `<p class="finding error">${esc(c.error)}</p>` : ""}${quality}${checks}${baseline}<details><summary>Actual model output</summary><pre>${esc(JSON.stringify(c.output, null, 2))}</pre></details></div></details>`;
}

document.addEventListener("click", async (event) => {
  const t = event.target.closest("button");
  if (!t || t.disabled) return;
  try {
    if (t.dataset.action === "evals") return await showEvals();
    if (t.dataset.evalSection) return await showEvals(t.dataset.evalSection);
    if (t.dataset.evalAction === "run") return runEvalModal();
    if (t.dataset.assessment) return await readAssessment(t.dataset.assessment);
    if (t.dataset.evalRun) return await readEvalRun(t.dataset.evalRun);
  } catch (error) {
    modal.open ? modalError(error.message) : toast(error.message, true);
  }
});
document.addEventListener("change", (event) => {
  if (event.target.id === "eval-split") renderEvalCases();
  if (event.target.name === "result" && $("#quality-notes"))
    $("#quality-notes").required = event.target.value === "0";
});
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!["quality-rating", "eval-run"].includes(form.id)) return;
  event.preventDefault();
  if (form.dataset.submitting) return;
  form.dataset.submitting = "true";
  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  const values = new FormData(form);
  try {
    if (form.id === "quality-rating") {
      await api(`/api/evals/assessments/${form.dataset.id}/review`, {
        version: form.dataset.version,
        result: Number(values.get("result")),
        notes: values.get("notes").trim(),
      });
      closeModal();
      S.signature = "";
      S.evalSignature = "";
      if (S.view === "evals") await loadEvals();
      else await loadSession();
      toast("Human review saved for this exact version.");
    } else {
      const ids = values.getAll("case");
      if (!ids.length) throw new Error("Select at least one case.");
      await api("/api/evals/runs", {
        split: values.get("split"),
        compare_baseline: values.has("baseline"),
        case_ids: ids,
      });
      closeModal();
      S.evalSignature = "";
      await showEvals("datasets");
      toast("Dataset evaluation started. Results will update here.");
    }
  } catch (error) {
    modalError(error.message);
  } finally {
    delete form.dataset.submitting;
    button.disabled = false;
  }
});
modal.addEventListener("close", () => {
  if (S.view === "evals") {
    S.evalSignature = "";
    loadEvals();
  }
});
window.addEventListener("focus", () => {
  if (S.view === "evals" && !modal.open) loadEvals();
});
