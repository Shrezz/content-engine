const $ = (s) => document.querySelector(s);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const paths = {
  article: "M5 3h11l3 3v15H5z M14 3v5h5 M8 12h8 M8 16h8",
  check: "m5 12 4 4L19 6",
  search: "m21 21-5-5 M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14",
  image: "M3 3h18v18H3z M3 17l6-6 4 4 3-3 5 5 M8 7h.01",
  activity: "M3 12h4l3-8 4 16 3-8h4",
  arrow: "M5 12h14 m-5-5 5 5-5 5",
  external: "M14 3h7v7 m0-7-11 11 M10 3H3v18h18v-7",
  edit: "m4 16 12-12 4 4-12 12-5 1z M13 7l4 4",
  shield: "M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6z m-4 9 3 3 5-6",
  clock: "M12 8v5l3 2 M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20",
  download: "M12 3v12 m-5-5 5 5 5-5 M4 16v5h16v-5",
  book: "M12 5v16 M3 3c4 0 6 0 9 2 3-2 5-2 9-2v16c-4 0-6 0-9 2-3-2-5-2-9-2z",
  alert: "M12 3 2 21h20z M12 9v5 M12 17h.01",
  plus: "M12 4v16 M4 12h16",
  chevron: "m9 5 7 7-7 7",
};
const icon = (name) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.article}"/></svg>`;
const S = {
  sessions: [],
  history: [],
  selected: new URLSearchParams(location.search).get("session"),
  view: new URLSearchParams(location.search).get("view") || "drafts",
  data: null,
  tab: "article",
  platform: "linkedin",
  editing: false,
  edited: null,
  poll: null,
  signature: "",
  configured: true,
};
const busy = () => ["queued", "running"].includes(S.data?.status);
const pending = () => S.data?.pending || {};
const state = () => S.data?.state || {};
const version = () => pending().version;
const modal = $("#modal");

async function api(path, body) {
  const response = await fetch(
    path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Content-Engine": "studio",
          },
          body: JSON.stringify(body),
        },
  );
  if (!response.ok) {
    const raw = await response.text();
    let message = raw;
    try {
      const detail = JSON.parse(raw).detail;
      message = Array.isArray(detail)
        ? detail.map((d) => `${d.loc.slice(1).join(" ")}: ${d.msg}`).join("; ")
        : detail;
    } catch {}
    throw new Error(
      message || "This action could not be completed. Please try again.",
    );
  }
  return response.json();
}

function toast(message, error = false) {
  const box = $("#toast");
  box.textContent = message;
  box.className = `toast ${error ? "error" : ""}`;
  box.hidden = false;
  clearTimeout(S.toastTimer);
  S.toastTimer = setTimeout(() => (box.hidden = true), 6000);
}

function statusLabel(row) {
  if (["queued", "running"].includes(row.status))
    return row.status === "queued" ? "In the queue" : "In progress";
  if (row.status === "approved") return "Approved";
  if (row.status === "failed") return "Needs attention";
  if (row.status === "interrupted") return "Ready to continue";
  if (row.status === "cancelled") return "Stopped";
  return (
    {
      clarification: "Your direction needed",
      angle_review: "Similar angle found",
      research_review: "Research needs direction",
      final_review: "Ready for your review",
    }[row.pending_kind] || "Draft saved"
  );
}

async function loadIndex() {
  const result = await api("/api/sessions");
  S.sessions = result.sessions;
  S.configured = result.configured;
  $("#draft-count").textContent = S.sessions.length;
  $("#history-count").textContent = result.history_count;
  renderSidebar();
}

function renderSidebar() {
  $("#drafts-nav").classList.toggle("nav-active", S.view === "drafts");
  $("#evals-nav").classList.toggle("nav-active", S.view === "evals");
  $("#history-nav").classList.toggle("nav-active", S.view === "history");
  $("#session-list").innerHTML = S.sessions.length
    ? S.sessions
        .map(
          (row) =>
            `<button class="session-link ${row.id === S.selected && S.view === "drafts" ? "selected" : ""}" data-session="${esc(row.id)}"><span class="title">${esc(row.title)}</span><span class="session-meta"><i class="status-dot ${row.pending_kind ? "amber" : row.status === "failed" ? "red" : ""}"></i>${esc(statusLabel(row))}</span></button>`,
        )
        .join("")
    : '<p class="muted small sidebar-loading">Your next idea starts here.</p>';
}

async function selectSession(key) {
  clearTimeout(S.poll);
  S.selected = key;
  S.view = "drafts";
  S.tab = "article";
  S.editing = false;
  S.signature = "";
  history.replaceState({}, "", `/?session=${encodeURIComponent(key)}`);
  renderSidebar();
  $("#main").innerHTML =
    '<div class="loading-state"><div class="spinner"></div><p>Opening your article…</p></div>';
  await loadSession();
}

async function loadSession() {
  if (!S.selected || S.view !== "drafts") return;
  const key = S.selected;
  const ticket = (S.requestSeq = (S.requestSeq || 0) + 1);
  try {
    const result = await api(`/api/sessions/${key}`);
    if (ticket !== S.requestSeq || key !== S.selected || S.view !== "drafts")
      return;
    S.data = result;
    const signature = JSON.stringify(result);
    if (signature !== S.signature && !S.editing && !modal.open) {
      S.signature = signature;
      renderDraft();
    }
    const row = S.sessions.find((r) => r.id === S.selected);
    if (row) {
      row.status = result.status;
      row.pending_kind = result.pending?.kind;
      row.title = result.state?.brief?.topic || row.title;
      renderSidebar();
    }
    if (
      (result.job?.status === "done" || result.state?.status === "approved") &&
      S.finishedJob !== result.job.trace_file
    ) {
      S.finishedJob = result.job.trace_file;
      await loadIndex();
    }
    if (["queued", "running"].includes(result.status)) {
      clearTimeout(S.poll);
      S.poll = setTimeout(loadSession, 1800);
    }
  } catch (error) {
    toast(error.message, true);
  }
}

function stepper() {
  const p = pending().kind,
    st = state();
  let current = {
    clarification: 0,
    angle_review: 1,
    research_review: 2,
    final_review: 5,
  }[p];
  if (current === undefined) {
    const node = S.data?.next_nodes?.[0];
    current =
      {
        intake: 0,
        clarify: 0,
        novelty: 1,
        angle_review: 1,
        research: 2,
        deepen: 2,
        research_review: 2,
        write: 3,
        evaluate: 4,
        assets: 5,
        review: 5,
        export: 5,
      }[node] ?? (st.drafts ? 4 : 0);
  }
  return `<div class="stepper" aria-label="Article progress">${["Brief", "History check", "Research", "Writing", "LLM review", "Your review"].map((label, i) => `<div class="step ${i < current || st.status === "approved" ? "done" : i === current ? "current" : ""}"><span class="step-number">${i < current || st.status === "approved" ? icon("check") : i + 1}</span>${label}</div>`).join("")}</div>`;
}

function renderDraft() {
  $("#page-location").textContent = "Draft review";
  const st = state(),
    p = pending(),
    row = { status: S.data.status, pending_kind: p.kind },
    brief = st.brief;
  const title =
    brief?.topic || (st.conversation || [])[0]?.content || "Your next article";
  const isFinal = p.kind === "final_review",
    approved = st.status === "approved",
    cancelled = st.status === "cancelled";
  const label = statusLabel(row),
    tone = ["failed", "interrupted"].includes(row.status)
      ? "red"
      : row.pending_kind
        ? "amber"
        : approved
          ? ""
          : "gray";
  const intro = `<div class="page-intro"><div><div class="eyebrow">THE EDITORIAL DESK</div><h1>${esc(title)}</h1><p>${esc(brief?.angle || "A good article begins with a little direction.")}</p></div><div class="intro-actions"><span class="badge ${tone}"><i></i>${esc(label)}</span></div></div>`;
  let banner = "";
  if (
    S.data.job?.error ||
    S.data.errors?.length ||
    S.data.status === "interrupted"
  )
    banner = `<div class="banner error">${icon("alert")}<div><strong>${S.data.status === "interrupted" ? "Your work is saved. Continue when you’re ready." : "This run needs attention."}</strong><p>${esc(S.data.job?.error || S.data.errors?.[0] || "The previous run stopped before its next step finished.")}</p><div class="actions"><button class="secondary" data-action="retry">${icon("arrow")} Continue run</button></div></div></div>`;
  if (approved)
    banner = `<div class="banner success">${icon("check")}<div><strong>Approved and saved to your article history.</strong><p>Your articles and pictures are ready to download. Publication is a separate step.</p><div class="actions"><a class="primary" href="/api/sessions/${S.selected}/download">${icon("download")} Download articles & pictures</a></div></div></div>`;
  if (cancelled)
    banner =
      '<div class="banner"><div><strong>This draft is stopped.</strong><p>The saved work remains available here. Start a new article whenever you’re ready.</p></div></div>';
  let content;
  if (p.kind && !isFinal && !busy()) {
    content = renderGate();
    if (p.kind === "research_review" && st.drafts)
      content += `<div id="research-draft-review" class="review-layout"><section class="review-main">${tabs()}<div id="tab-content">${tabContent()}</div></section><aside class="review-aside">${assessmentPanel()}${checksPanel()}${detailsPanel()}</aside></div>`;
  } else if (!st.drafts)
    content = busy()
      ? renderRunning()
      : `<div class="paper gate"><h2>Your work is saved.</h2><p>${cancelled ? "Start a new article to explore another direction." : "Continue the run to move to the next step."}</p></div>`;
  else
    content = `<div class="review-layout"><section class="review-main">${tabs()}<div id="tab-content">${tabContent()}</div></section><aside class="review-aside">${assessmentPanel()}${checksPanel()}${detailsPanel()}</aside></div>`;
  $("#main").innerHTML =
    intro +
    stepper() +
    banner +
    content +
    (isFinal && !busy() ? reviewBar() : "");
  if (busy() && st.drafts)
    $("#main").insertAdjacentHTML(
      "afterbegin",
      `<div class="banner"><div class="spinner"></div><div><strong>${esc(activeLabel())}</strong><p>Your saved article stays visible while the next step runs.</p></div></div>`,
    );
}

function activeLabel() {
  const events = S.data.activity || [];
  const complete = new Set(
    events.filter((e) => e.event !== "start").map((e) => e.span_id),
  );
  return (
    [...events]
      .reverse()
      .find(
        (e) =>
          e.event === "start" && e.kind === "node" && !complete.has(e.span_id),
      )?.label || "Working on your article"
  );
}

function renderRunning() {
  return `<div class="paper running-card"><div class="spinner"></div><div><h2>${esc(activeLabel())}</h2><p>You can leave this page open while the agent works.<br>Your progress is saved between steps.</p></div></div><div class="section-title">As it happens</div>${activityView()}`;
}

function tabs() {
  return `<div class="content-tabs" role="tablist" aria-label="Review sections">${[
    ["article", "Article", "article"],
    ["evidence", "Evidence", "search"],
    ["pictures", "Pictures", "image"],
    ["activity", "Activity", "activity"],
  ]
    .map(
      ([id, label, name]) =>
        `<button class="content-tab ${S.tab === id ? "active" : ""}" data-tab="${id}" role="tab" aria-selected="${S.tab === id}">${icon(name)}${label}${id === "pictures" ? `<span class="tab-count">${state().drafts?.visuals?.length || 0}</span>` : ""}</button>`,
    )
    .join("")}</div>`;
}
function tabContent() {
  return {
    article: articleView,
    evidence: evidenceView,
    pictures: picturesView,
    activity: activityView,
  }[S.tab]();
}

function articleView() {
  const st = state(),
    draft = st.drafts?.[S.platform] || "",
    words = draft.split(/\s+/).filter(Boolean).length;
  return `<div class="paper"><div class="article-toolbar"><div class="platforms" aria-label="Article platform">${["linkedin", "medium"].map((p) => `<button class="platform-tab ${S.platform === p ? "active" : ""}" data-platform="${p}" aria-pressed="${S.platform === p}"><span class="platform-letter">${p === "linkedin" ? "in" : "M"}</span>${p === "linkedin" ? "LinkedIn" : "Medium"}</button>`).join("")}</div><div class="toolbar-right"><span>${words.toLocaleString()} words</span>${pending().kind === "final_review" && !busy() ? `<button data-action="edit">${icon("edit")} Edit draft</button>` : ""}</div></div>${S.editing ? `<div class="edit-mode"><p class="form-help">Edit this ${S.platform === "linkedin" ? "LinkedIn" : "Medium"} article in Markdown. Saving sends both versions through evaluation again.</p><label class="sr-only" for="draft-editor">Article text</label><textarea id="draft-editor">${esc(S.edited[S.platform])}</textarea><div class="form-actions"><button class="primary" data-action="save-edit">Save & re-evaluate ${icon("arrow")}</button><button class="secondary" data-action="cancel-edit">Discard edits</button></div></div>` : `<div class="article-body"><div class="article-kicker"><span class="line"></span> ${S.platform === "linkedin" ? "A professional perspective" : "Ideas, with room to unfold"} <span>·</span> ${Math.max(1, Math.ceil(words / 220))} MIN READ</div><article class="article">${S.data.articles?.[S.platform] || "<p>Your article will appear here.</p>"}</article></div>`}</div>`;
}

function checksPanel() {
  const report = state().evaluation;
  if (!report) return "";
  const issues = report.issues || [],
    errors = issues.filter((i) => i.severity === "error");
  const names = {
    faithfulness: "Factual support",
    substance: "Depth & usefulness",
    voice: "Author voice",
    structure: "Structure",
  };
  return `<section class="check-panel"><div class="panel-heading"><span>LLM editorial review</span>${icon("shield")}</div><p class="panel-copy">A separate critic call reviews both articles against your brief and sources.</p>${Object.entries(
    names,
  )
    .map(
      ([key, label]) =>
        `<div class="score-row"><span>${label}</span><b class="${report[key] < 4 ? "low" : ""}">${report[key]}<span class="muted"> / 5</span></b></div><div class="score-track ${report[key] < 4 ? "low" : ""}">${Array.from({ length: 5 }, (_, i) => `<i class="${i < report[key] ? "fill" : ""}"></i>`).join("")}</div>`,
    )
    .join(
      "",
    )}<div class="section-divider"></div><div class="mini-heading"><span>Worth a closer look</span><span class="count">${issues.length}</span></div>${
    issues.length
      ? issues
          .slice(0, 3)
          .map(
            (i) =>
              `<div class="finding ${i.severity}"><strong>${i.kind === "evidence" ? "Claim support" : i.kind === "visual" ? "Visual review" : "Editorial detail"}</strong><p>${esc(i.message)}</p><button data-tab="evidence">Review finding ↗</button></div>`,
          )
          .join("")
      : '<p class="panel-copy">No issues were flagged in this pass.</p>'
  }${issues.length > 3 ? `<button class="text-button extra-findings" data-tab="evidence">View all ${issues.length} findings ${icon("chevron")}</button>` : ""}<p class="panel-foot">Critic scores include evidence validation. Findings combine LLM feedback and code checks; your review comes next.${errors.length ? " Findings remain open." : ""}</p></section>`;
}

function detailsPanel() {
  const st = state(),
    assets = st.packet?.snapshot?.assets || [],
    unresolved = assets.filter((a) => a.status === "pending").length;
  return `<section class="detail-card"><div class="detail-row">${icon("search")}<div>${st.sources?.length || 0} sources consulted<span>Evidence linked throughout the article</span></div></div><div class="detail-row">${icon("image")}<div>${st.drafts?.visuals?.length || 0} visual suggestions<span>${!st.packet?.version ? "Pictures are prepared after evidence review" : unresolved ? `${unresolved} need an image or an omission decision` : "Review the pictures alongside your text"}</span></div></div><div class="detail-row">${icon("edit")}<div>${st.revision_count || 0} rewrites completed<span>Feedback stays with this draft</span></div></div>${st.brief ? `<div class="section-divider"></div><div class="detail-row">${icon("book")}<div>Written for<span>${esc(st.brief.audience)}</span></div></div><div class="detail-row">${icon("arrow")}<div>The article’s aim<span>${esc(st.brief.goal)}</span></div></div>` : ""}${st.assumptions?.length ? `<details class="brief-assumptions"><summary>Choices made for this brief</summary><ul>${st.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul></details>` : ""}</section>`;
}

function reviewBar() {
  const st = state(),
    p = pending(),
    report = st.evaluation || {},
    assets = st.packet?.snapshot?.assets || [];
  const missing = assets.filter((a) => a.status === "pending").length,
    errors = (report.issues || []).filter((i) => i.severity === "error").length;
  const low = ["faithfulness", "substance", "voice", "structure"].some(
      (k) => report[k] < 4,
    ),
    findings = errors || low || report.needs_research;
  const disabled = missing || findings || S.editing;
  const line = missing
    ? `${missing} picture${missing > 1 ? "s" : ""} need a decision`
    : findings
      ? "Review the open findings before approval"
      : "Ready for your final decision";
  return `<div class="review-bar"><div><div class="review-bar-title">${icon(missing || findings ? "alert" : "check")}${line}</div><p>${missing ? "Attach an image or explicitly omit each suggestion." : findings ? "Request changes, or accept the findings with a reason." : "Approving saves an export and adds the article to history."}</p></div><div class="review-bar-actions"><button class="text-button" data-action="stop">Stop draft</button>${findings && !missing ? '<button class="text-button" data-action="accept-findings">Accept findings…</button>' : ""}<button class="secondary" data-action="revise" ${S.editing ? "disabled" : ""}>${icon("edit")} Request changes</button><button class="primary" data-action="approve" ${disabled ? "disabled" : ""}>Approve & export ${icon("arrow")}</button></div></div>`;
}

function evidenceView() {
  const st = state(),
    report = st.evaluation || {};
  return `<div class="tab-intro"><div><h2>The thinking behind the writing</h2><p>Review the evidence, claim checks, and anything still unresolved.</p></div><span class="badge gray">${st.sources?.length || 0} sources</span></div>${(report.issues || []).length ? `<div class="paper gate"><div class="mini-heading">Open findings <span class="count">${report.issues.length}</span></div>${report.issues.map((i) => `<div class="finding ${i.severity}"><strong>${esc(i.severity === "error" ? "Needs attention" : "Consideration")} · ${esc(i.kind)}</strong><p>${esc(i.message)}</p></div>`).join("")}</div>` : ""}${(report.claims || []).length ? `<h2 class="section-title">Claim-by-claim review</h2>${report.claims.map((c) => `<div class="claim-card"><span class="badge ${c.verdict === "supported" ? "" : c.verdict === "conflicting" ? "red" : "amber"}">${esc(c.verdict === "supported" ? "Support found" : c.verdict === "conflicting" ? "Conflicting evidence" : "Support not established")}</span><p class="claim">${esc(c.claim)}</p><details><summary>See supporting evidence</summary>${c.supporting_quote ? `<blockquote>${esc(c.supporting_quote)}</blockquote>` : ""}<p>${esc(c.explanation)}</p>${safeLink(c.source_url) ? `<a href="${esc(c.source_url)}" target="_blank" rel="noopener noreferrer">Open source ↗</a>` : ""}</details></div>`).join("")}` : ""}<h2 class="section-title">Research sources</h2>${sourceCards(st.sources || [])}`;
}

function safeLink(url) {
  try {
    return ["http:", "https:"].includes(new URL(url).protocol);
  } catch {
    return false;
  }
}
function sourceCards(sources) {
  return (
    sources
      .map(
        (source, i) =>
          `<div class="source-card"><span class="source-domain">${String(i + 1).padStart(2, "0")} / ${esc(new URL(source.url).hostname.replace(/^www\./, ""))}</span><h3>${esc(source.title)}</h3><details><summary>Read the supplied evidence</summary><p>${esc(source.content)}</p></details><a href="${esc(source.url)}" target="_blank" rel="noopener noreferrer">Open original source ${icon("external")}</a></div>`,
      )
      .join("") || '<p class="empty-small">Research has not started yet.</p>'
  );
}

function picturesView() {
  const st = state(),
    plans = st.drafts?.visuals || [],
    assets = st.packet?.snapshot?.assets || [],
    canEdit = pending().kind === "final_review" && !busy();
  return `<div class="tab-intro"><div><h2>Make the idea visible</h2><p>Review each image, caption, and placement before approval.</p></div>${canEdit ? '<button class="quiet-button" data-action="refresh">Refresh pictures</button>' : ""}</div><div class="banner"><div><strong>Diagrams render automatically.</strong><p>For other pictures, attach an image you can use, add a credit, or deliberately leave it out.</p></div></div>${
    plans
      .map((plan, index) => {
        const asset = assets.find((a) => a.index === index) || {
          status: "pending",
        };
        return `<section class="picture-card"><div class="picture-top"><span class="eyebrow">${esc(plan.platform)} · VISUAL ${index + 1}</span><span class="badge ${asset.status === "pending" ? "amber" : asset.status === "omitted" ? "gray" : ""}">${asset.status === "ready" ? "Ready to review" : asset.status === "omitted" ? "Omitted" : "Image needed"}</span></div>${asset.status === "ready" ? `<img src="/api/sessions/${S.selected}/assets/${encodeURIComponent(asset.path.split("/").pop())}?v=${encodeURIComponent(version() || "")}" alt="${esc(plan.alt_text)}" loading="lazy">` : `<div class="picture-placeholder">${icon("image")}<span>${asset.status === "omitted" ? "This picture will be left out" : "An illustration to bring this idea to life"}</span></div>`}<h3>${esc(plan.caption)}</h3><p>${esc(plan.description)}</p><p class="placement">Placement: ${esc(asset.placement_used || plan.placement)}</p>${asset.credit ? `<p class="credit">Credit: ${esc(asset.credit)}</p>` : ""}${asset.status === "omitted" ? `<p class="asset-note">Omitted: ${esc(asset.reason)}</p>` : ""}${asset.error && plan.mermaid ? `<p class="asset-note">${esc(asset.error)}</p>` : ""}${canEdit ? `<div class="form-actions"><button class="secondary" data-upload="${index}">${icon("image")} ${asset.status === "ready" ? "Replace image" : "Attach an image"}</button>${asset.status !== "omitted" ? `<button class="text-button" data-omit="${index}">Leave this picture out</button>` : ""}</div>` : ""}</section>`;
      })
      .join("") ||
    '<div class="paper empty-small">No pictures were suggested for this article.</div>'
  }`;
}

function activityView() {
  const events = S.data.activity || [],
    completed = events.filter((e) => e.event !== "start"),
    active = events.filter(
      (e) =>
        e.event === "start" && !completed.some((c) => c.span_id === e.span_id),
    );
  const rows = [...completed, ...active].sort((a, b) =>
    b.time.localeCompare(a.time),
  );
  return `<div class="tab-intro"><div><h2>A clear view of the process</h2><p>Research, writing, evaluation, and your decisions, as they happen.</p></div></div><div class="activity-card">${rows.map((e) => `<div class="activity-row"><span class="activity-symbol ${e.event}">${e.event === "error" ? "!" : e.event === "pause" ? "Ⅱ" : e.event === "start" ? "·" : "✓"}</span><div><strong>${esc(e.label)}</strong><p>${esc(e.event === "start" ? "In progress" : e.event === "pause" ? "Waiting for your input" : e.event === "error" ? e.error || "Could not complete" : e.duration_ms ? `${(e.duration_ms / 1000).toFixed(1)} seconds` : "Completed")}${e.metadata?.usage?.total_tokens ? ` · ${e.metadata.usage.total_tokens.toLocaleString()} tokens` : ""}</p></div><time>${new Date(e.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time></div>`).join("") || '<p class="empty-small">Actions will appear here as the workflow runs.</p>'}</div><p class="activity-label">The full execution record is saved locally with this session. No article is published from this workspace.</p>`;
}

function renderGate() {
  const p = pending();
  if (p.kind === "clarification")
    return `<div class="paper gate"><div class="eyebrow">LET’S FIND YOUR ANGLE</div><h2>A little direction goes a long way.</h2><p class="gate-question">${esc(p.question)}</p><div class="choices">${(p.choices || []).map((choice) => `<button class="choice" data-answer="${esc(choice)}">${esc(choice)} ${icon("arrow")}</button>`).join("")}</div><form id="clarify-form"><div class="field"><label for="clarify-answer">Or tell me in your own words</label><textarea id="clarify-answer" name="answer" placeholder="Who is this for, and what would you like them to take away?" required></textarea></div><div class="form-actions"><button class="primary" type="submit">Continue with this direction ${icon("arrow")}</button><button class="text-button" type="button" data-action="stop">Stop this draft</button></div></form></div>`;
  if (p.kind === "angle_review")
    return `<div class="paper gate"><div class="eyebrow">HISTORY CHECK</div><h2>You’ve explored similar ground.</h2><p>${esc(p.finding.reason)}</p>${(p.matching_articles || []).map((a) => `<div class="comparison"><div class="eyebrow">FROM YOUR ARTICLE HISTORY</div><strong>${esc(a.topic)}</strong><p>${esc(a.angle)}</p><button class="text-button" data-history-read="${esc(a.id)}">Read the earlier article ${icon("external")}</button></div>`).join("")}<form id="angle-form"><div class="field"><label for="new-angle">Give this article a different angle</label><textarea id="new-angle" name="angle" required placeholder="What would make this argument new?"></textarea></div><div class="choices">${(p.finding.alternative_angles || []).map((a) => `<button class="choice" type="button" data-angle="${esc(a)}">${esc(a)}</button>`).join("")}</div><div class="form-actions"><button class="primary" type="submit">Use this new angle ${icon("arrow")}</button><button class="secondary" type="button" data-action="allow-repeat">Continue with the overlap</button><button class="text-button" type="button" data-action="stop">Stop draft</button></div></form></div>`;
  return `<div class="paper gate"><div class="eyebrow">RESEARCH CHECKPOINT</div><h2>The evidence needs a decision.</h2><p>${(p.sources || []).length < 5 ? "There aren’t enough usable sources to start a grounded draft yet. A more focused search may help." : "Some support is still missing. Refine the research, or explicitly narrow the argument to what the sources can sustain."}</p>${(
    p.evaluation?.issues || []
  )
    .filter((i) => i.kind === "evidence")
    .map(
      (i) =>
        `<div class="finding ${i.severity}"><p>${esc(i.message)}</p></div>`,
    )
    .join(
      "",
    )}<form id="query-form"><div class="field"><label for="research-query">Refine the search</label><textarea id="research-query" name="query" required>${esc(p.query || "")}</textarea></div><div class="form-actions"><button class="primary" type="submit">Research this direction ${icon("search")}</button>${(p.sources || []).length >= 5 ? '<button class="secondary" type="button" data-action="narrow">Narrow the scope</button>' : ""}<button class="text-button" type="button" data-action="stop">Stop draft</button>${state().drafts ? '<button class="text-button" type="button" data-action="view-current-draft">Read the current draft ↓</button>' : ""}</div></form></div>${state().drafts ? "" : `<h2 class="section-title">Sources found so far</h2>${sourceCards(p.sources || [])}`}`;
}

function openModal(eyebrow, title, description, content) {
  $("#modal-content").innerHTML =
    `<div class="modal-inner"><div class="modal-head"><div><div class="eyebrow">${esc(eyebrow)}</div><h2 id="modal-title">${esc(title)}</h2></div><button class="close" aria-label="Close dialog" data-action="close">×</button></div>${description ? `<p class="modal-description">${esc(description)}</p>` : ""}<div id="modal-error" class="modal-error" hidden></div>${content}</div>`;
  modal.showModal();
}
function closeModal() {
  modal.close();
}
function modalError(message) {
  const target = $("#modal-error");
  target.textContent = message;
  target.hidden = false;
  target.scrollIntoView({ block: "nearest" });
}

function newArticle() {
  if (S.editing) return toast("Save or discard your text edits first.");
  openModal(
    "A FRESH PAGE",
    "What’s on your mind?",
    "Start with an idea, a question, or a point of view. We’ll help you shape it into an article.",
    `<form id="new-form"><div class="field"><label for="new-request">Your article idea</label><textarea class="compose-text" id="new-request" name="request" required placeholder="I want to write about…"></textarea></div><div class="prompt-examples"><button type="button" data-example="Write about how a busy parent can start a five-minute meditation habit, without medical promises.">A daily meditation habit</button><button type="button" data-example="Write for software engineers about why an agent’s final message isn’t enough to evaluate success. Use a concrete refund example.">Evaluating AI agents</button><button type="button" data-example="Write about making household routines easier to share. You choose a practical angle and audience for me.">Shared household routines</button></div>${!S.configured ? '<p class="modal-error">Set your model and API key in the project’s .env file before starting a live run.</p>' : ""}<div class="form-actions"><button class="primary" type="submit" ${S.configured ? "" : "disabled"}>Start the article ${icon("arrow")}</button><button class="text-button" data-action="close" type="button">Keep thinking</button></div><p class="modal-note">You’ll review the writing, evidence, and pictures before anything is approved. Starting uses your configured research and model services.</p></form>`,
  );
}

async function respond(response) {
  const key = S.selected;
  await api(`/api/sessions/${key}/respond`, response);
  closeModal();
  S.editing = false;
  S.signature = "";
  await loadIndex();
  await loadSession();
}

function feedbackModal(kind) {
  const scope = kind === "scope";
  openModal(
    scope ? "RESEARCH DIRECTION" : "A NOTE TO YOUR EDITOR",
    scope ? "Keep the argument grounded." : "What would you like to change?",
    scope
      ? "Explain the narrower argument you want. Unsupported claims must still be removed or qualified."
      : "Be specific about the argument, evidence, tone, or detail you’d like improved.",
    `<form id="feedback-form" data-kind="${kind}" data-version="${esc(version() || "")}"><div class="field"><label for="feedback">${scope ? "Narrower scope" : "Your feedback"}</label><textarea id="feedback" name="feedback" required placeholder="${scope ? "Limit the article to…" : "For the Medium version, walk through…"}"></textarea></div><div class="form-actions"><button class="primary" type="submit">${scope ? "Use this qualified scope" : "Request a revision"} ${icon("arrow")}</button><button class="text-button" type="button" data-action="close">Cancel</button></div></form>`,
  );
}

function approveModal(warnings = false) {
  const st = state(),
    pictures = st.packet?.snapshot?.assets || [];
  openModal(
    "YOUR FINAL REVIEW",
    warnings
      ? "Accept the findings and export?"
      : "Ready to put your name to it?",
    warnings
      ? "The automated review still has open findings. Explain your decision for this exact version."
      : "This saves the reviewed articles and resolved pictures, and adds the angle to your history.",
    `<div class="approval-summary"><strong>${esc(st.brief.topic)}</strong><br>LinkedIn + Medium · ${pictures.filter((a) => a.status === "ready").length} pictures included · ${pictures.filter((a) => a.status === "omitted").length} explicitly omitted</div><form id="approval-form" data-warnings="${warnings}" data-version="${esc(version())}">${warnings ? '<div class="field"><label for="approval-reason">Why are you accepting these findings?</label><textarea id="approval-reason" name="reason" required></textarea></div>' : ""}<label class="checkbox-label"><input type="checkbox" name="reviewed" required><span>I have reviewed both articles, their evidence, and the final pictures for this version.</span></label><div class="form-actions"><button class="primary" type="submit">Approve & save export ${icon("download")}</button><button class="text-button" type="button" data-action="close">Back to review</button></div><p class="modal-note">This creates a local export. It does not publish to LinkedIn or Medium.</p></form>`,
  );
}

function attachModal(index) {
  openModal(
    "PICTURE REVIEW",
    "Add an image with a purpose.",
    "Choose an image you own or have permission to use. It will be inserted at the suggested placement.",
    `<form id="upload-form" data-index="${index}" data-version="${esc(version())}"><div class="field"><label for="image-file">Image file</label><input id="image-file" type="file" name="file" accept="image/png,image/jpeg,image/gif,image/webp" required><small>PNG, JPEG, GIF, or WebP. Up to 10 MiB.</small></div><div class="field"><label for="image-credit">Credit or ownership</label><input id="image-credit" name="credit" required placeholder="Created by me, or photographer / license details"></div><div class="form-actions"><button class="primary" type="submit">Attach & update review ${icon("arrow")}</button><button class="text-button" type="button" data-action="close">Cancel</button></div></form>`,
  );
}
function omitModal(index) {
  openModal(
    "PICTURE REVIEW",
    "Leave this picture out?",
    "Your reason will be saved with the review. The article can still be approved once every image has a decision.",
    `<form id="omit-form" data-index="${index}" data-version="${esc(version())}"><div class="field"><label for="omit-reason">Why isn’t this picture needed?</label><textarea id="omit-reason" name="reason" required placeholder="The explanation is clearer without this illustration…"></textarea></div><div class="form-actions"><button class="primary" type="submit">Omit this picture</button><button class="text-button" type="button" data-action="close">Keep it</button></div></form>`,
  );
}

async function showHistory() {
  clearTimeout(S.poll);
  S.view = "history";
  S.editing = false;
  history.replaceState({}, "", "/?view=history");
  renderSidebar();
  $("#page-location").textContent = "Article history";
  const result = await api("/api/history");
  S.history = result.articles;
  renderHistory();
}
function renderHistory(filter = "") {
  const rows = S.history.filter((a) =>
    `${a.brief.topic} ${a.brief.angle}`
      .toLowerCase()
      .includes(filter.toLowerCase()),
  );
  $("#main").innerHTML =
    `<div class="page-intro"><div><div class="eyebrow">THE WORK YOU’VE ALREADY DONE</div><h1>Your ideas, remembered.</h1><p>Approved articles and earlier writing help you find a genuinely new angle.</p></div><div class="intro-actions"><button class="secondary" data-action="import">${icon("plus")} Import an article</button></div></div>${S.history.length ? `<div class="history-toolbar"><input id="history-search" type="search" aria-label="Search article history" placeholder="Search topics or angles…" value="${esc(filter)}"><span class="history-total">${S.history.length} ${S.history.length === 1 ? "ARTICLE" : "ARTICLES"} IN YOUR HISTORY</span></div><div id="history-results" class="history-grid">${historyCards(rows)}</div><p class="history-search-note">The new-article check compares your topic and angle with these saved briefs. Unapproved drafts stay in your workspace.</p>` : `<div class="empty-state"><div class="empty-icon">${icon("book")}</div><h2>Make room for your next new idea.</h2><p>Articles appear here after approval. Import earlier writing so the engine can spot arguments you’ve already explored.</p><button class="primary" data-action="import">Import your first article ${icon("arrow")}</button></div>`}`;
}
function historyCards(rows) {
  return (
    rows
      .map(
        (a) =>
          `<section class="history-card"><div><span class="badge ${a.status === "imported" ? "gray" : ""}">${a.status === "imported" ? "Imported writing" : "Approved article"}</span></div><h3>${esc(a.brief.topic)}</h3><p class="angle">${esc(a.brief.angle)}</p><p>For ${esc(a.brief.audience)}</p><div class="history-footer"><span>${new Date(a.recorded_at).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })}</span><button class="text-button" data-history-read="${esc(a.id)}">Read article ${icon("external")}</button></div></section>`,
      )
      .join("") ||
    '<p class="empty-small">No articles match that topic or angle.</p>'
  );
}
function importModal() {
  openModal(
    "BRING YOUR EARLIER WORK",
    "An angle you’ve already explored.",
    "Add an existing article so new briefs can be checked against it. This does not publish or approve the old article.",
    `<form id="import-form"><div class="field"><label for="import-topic">Article title or topic</label><input id="import-topic" name="topic" required placeholder="The value of a five-minute pause"></div><div class="field"><label for="import-angle">The main angle or argument</label><input id="import-angle" name="angle" required placeholder="A short daily practice is easier to maintain than an ambitious one"></div><div class="field"><label for="import-audience">Who was it written for?</label><input id="import-audience" name="audience" required placeholder="Busy working parents"></div><div class="field"><label for="import-content">Article text</label><textarea id="import-content" name="content" required placeholder="Paste your existing article here…"></textarea></div><div class="field"><label for="import-url">Original link <span class="muted">(optional)</span></label><input id="import-url" name="url" type="url" placeholder="https://…"></div><div class="form-actions"><button class="primary" type="submit">Add to article history ${icon("arrow")}</button><button class="text-button" type="button" data-action="close">Cancel</button></div></form>`,
  );
}
async function readHistory(key) {
  const item = await api(`/api/history/${encodeURIComponent(key)}`);
  openModal(
    "FROM YOUR ARTICLE HISTORY",
    item.brief.topic,
    item.brief.angle,
    `<div class="modal-content-article">${Object.entries(item.articles)
      .map(
        ([platform, html]) =>
          `<div class="eyebrow">${esc(platform === "content" ? "Imported article" : platform)}</div><article class="article">${html}</article>`,
      )
      .join("")}</div>`,
  );
}

function renderWelcome() {
  S.view = "drafts";
  renderSidebar();
  $("#main").innerHTML =
    `<div class="page-intro"><div><div class="eyebrow">YOUR EDITORIAL DESK</div><h1>Good ideas deserve a little space.</h1><p>A thoughtful path from your first idea to a piece you’re proud to share.</p></div></div><div class="empty-state"><div class="empty-icon">${icon("edit")}</div><h2>What will you write next?</h2><p>Bring a topic or a point of view. Research, refine, and review it for LinkedIn and Medium, with you making the final call.</p><button class="primary" data-action="new">Start your first article ${icon("arrow")}</button></div>`;
}

document.addEventListener("click", async (event) => {
  const target = event.target.closest("button,[data-session]");
  if (!target || target.disabled) return;
  try {
    if (target.dataset.session) {
      if (S.editing) return toast("Save or discard your text edits first.");
      return await selectSession(target.dataset.session);
    }
    if (target.dataset.tab) {
      if (S.editing) return toast("Save or discard your text edits first.");
      S.tab = target.dataset.tab;
      renderDraft();
      return;
    }
    if (target.dataset.platform) {
      if (S.editing) {
        S.edited[S.platform] = $("#draft-editor").value;
      }
      S.platform = target.dataset.platform;
      renderDraft();
      return;
    }
    if (target.dataset.answer)
      return await respond({ answer: target.dataset.answer });
    if (target.dataset.angle) {
      $("#new-angle").value = target.dataset.angle;
      return;
    }
    if (target.dataset.example) {
      $("#new-request").value = target.dataset.example;
      $("#new-request").focus();
      return;
    }
    if (target.dataset.upload !== undefined)
      return attachModal(Number(target.dataset.upload));
    if (target.dataset.omit !== undefined)
      return omitModal(Number(target.dataset.omit));
    if (target.dataset.historyRead)
      return await readHistory(target.dataset.historyRead);
    const action = target.dataset.action;
    if (
      S.editing &&
      ["new", "history", "drafts", "evals", "stop"].includes(action)
    )
      return toast("Save or discard your text edits first.");
    if (action === "new") return newArticle();
    if (action === "view-current-draft")
      return $("#research-draft-review").scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    if (action === "close") return closeModal();
    if (action === "history") return await showHistory();
    if (action === "drafts")
      return S.selected ? await selectSession(S.selected) : renderWelcome();
    if (action === "import") return importModal();
    if (action === "revise") return feedbackModal("revision");
    if (action === "narrow") return feedbackModal("scope");
    if (action === "approve") return approveModal();
    if (action === "accept-findings") return approveModal(true);
    if (action === "allow-repeat")
      return await respond({ action: "allow_repeat" });
    if (action === "stop")
      return openModal(
        "PAUSE THIS DIRECTION",
        "Stop this draft?",
        "The saved work stays in your workspace. No article will be approved or exported.",
        `<form id="stop-form" data-version="${esc(version() || "")}"><div class="form-actions"><button class="primary" type="submit">Stop this draft</button><button class="text-button" type="button" data-action="close">Keep working</button></div></form>`,
      );
    if (action === "retry" || action === "refresh") {
      await api(`/api/sessions/${S.selected}/${action}`, {});
      S.signature = "";
      await loadSession();
      return;
    }
    if (action === "edit") {
      S.editing = true;
      S.edited = structuredClone(state().drafts);
      S.editVersion = version();
      S.tab = "article";
      renderDraft();
      return;
    }
    if (action === "cancel-edit") {
      S.editing = false;
      S.edited = null;
      S.signature = "";
      renderDraft();
      return;
    }
    if (action === "save-edit") {
      S.edited[S.platform] = $("#draft-editor").value;
      target.disabled = true;
      await respond({
        action: "edit",
        version: S.editVersion,
        drafts: S.edited,
      });
    }
  } catch (error) {
    if (modal.open) modalError(error.message);
    else toast(error.message, true);
    target.disabled = false;
  }
});

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.id.endsWith("-form")) return;
  event.preventDefault();
  if (form.dataset.submitting) return;
  form.dataset.submitting = "true";
  const button = form.querySelector('[type="submit"]');
  if (button) button.disabled = true;
  const values = Object.fromEntries(new FormData(form));
  try {
    switch (form.id) {
      case "new-form": {
        const result = await api("/api/sessions", {
          request: values.request.trim(),
        });
        closeModal();
        await loadIndex();
        await selectSession(result.id);
        break;
      }
      case "clarify-form":
        await respond({ answer: values.answer.trim() });
        break;
      case "angle-form":
        await respond({ action: "new_angle", angle: values.angle.trim() });
        break;
      case "query-form":
        await respond({ action: "query", query: values.query.trim() });
        break;
      case "feedback-form":
        await respond({
          action: form.dataset.kind === "scope" ? "accept_scope" : "revise",
          feedback: values.feedback.trim(),
          ...(form.dataset.kind === "scope"
            ? {}
            : { version: form.dataset.version }),
        });
        break;
      case "approval-form":
        await respond({
          action:
            form.dataset.warnings === "true"
              ? "approve_with_warnings"
              : "approve",
          version: form.dataset.version,
          ...(form.dataset.warnings === "true"
            ? { reason: values.reason.trim() }
            : {}),
        });
        toast("Your decision was submitted. Preparing the export.");
        break;
      case "stop-form":
        await respond({
          action: "cancel",
          ...(form.dataset.version ? { version: form.dataset.version } : {}),
        });
        break;
      case "omit-form":
        await respond({
          action: "assets",
          version: form.dataset.version,
          choices: {
            [form.dataset.index]: {
              action: "omit",
              reason: values.reason.trim(),
            },
          },
        });
        toast("Omission submitted. Updating your review.");
        break;
      case "upload-form": {
        const file = values.file;
        if (file.size > 10 * 1024 * 1024)
          throw new Error("Choose an image no larger than 10 MiB.");
        const encoded = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result.split(",")[1]);
          reader.onerror = reject;
          reader.readAsDataURL(file);
        });
        await api(`/api/sessions/${S.selected}/images/${form.dataset.index}`, {
          version: form.dataset.version,
          content: encoded,
          credit: values.credit.trim(),
        });
        closeModal();
        S.signature = "";
        await loadSession();
        break;
      }
      case "import-form":
        await api("/api/history", {
          brief: {
            topic: values.topic.trim(),
            angle: values.angle.trim(),
            audience: values.audience.trim(),
            goal: "Record the existing argument to identify overlap in future articles",
          },
          content: values.content.trim(),
          source_url: values.url.trim() || null,
        });
        closeModal();
        await loadIndex();
        await showHistory();
        toast("Your earlier article is now included in history checks.");
        break;
    }
  } catch (error) {
    if (modal.open) modalError(error.message);
    else toast(error.message, true);
  } finally {
    delete form.dataset.submitting;
    if (button) button.disabled = false;
  }
});

document.addEventListener("input", (event) => {
  if (event.target.id === "history-search")
    $("#history-results").innerHTML = historyCards(
      S.history.filter((a) =>
        `${a.brief.topic} ${a.brief.angle}`
          .toLowerCase()
          .includes(event.target.value.toLowerCase()),
      ),
    );
});
document.addEventListener("keydown", (event) => {
  if (
    event.key.toLowerCase() === "n" &&
    !event.metaKey &&
    !event.ctrlKey &&
    !event.altKey &&
    !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName) &&
    !modal.open
  ) {
    event.preventDefault();
    newArticle();
  }
});
modal.addEventListener("close", () => {
  if (S.view === "drafts" && S.selected) {
    S.signature = "";
    loadSession();
  }
});
window.addEventListener("focus", () => {
  if (!modal.open && !S.editing) {
    loadIndex()
      .then(() => (S.view === "drafts" && S.selected ? loadSession() : null))
      .catch((error) => toast(error.message, true));
  }
});
window.addEventListener("beforeunload", (event) => {
  if (S.editing) {
    event.preventDefault();
    event.returnValue = "";
  }
});

(async () => {
  try {
    await loadIndex();
    if (S.view === "evals") await showEvals();
    else if (S.view === "history") await showHistory();
    else if (S.selected || S.sessions.length)
      await selectSession(S.selected || S.sessions[0].id);
    else renderWelcome();
  } catch (error) {
    $("#main").innerHTML =
      `<div class="banner error"><div><strong>The workspace could not load.</strong><p>${esc(error.message)}</p><button class="secondary" data-action="reload">Reload the page</button></div></div>`;
    document.querySelector('[data-action="reload"]').onclick = () =>
      location.reload();
  }
})();
