/* PR Reviewer dashboard. Talks only to this server's /api/ routes, which need DASHBOARD_TOKEN. */
(function () {
  const { esc, toast, download, copy } = UI;
  const $main = document.getElementById("main");
  const $nav = document.getElementById("nav");
  const $mode = document.getElementById("mode-pill");
  const $drawer = document.getElementById("drawer-root");

  const state = { cfg: null, reviews: [], seenIds: null, newIds: new Set(), filter: "all", q: "", source: "all", auto: true, job: null };

  const OUT = {
    merged: { label: "Merged", dry: "Would merge", cls: "good", group: "merged" },
    changes_requested: { label: "Changes requested", cls: "warn", group: "changes" },
    blocked: { label: "Blocked", cls: "bad", group: "blocked" },
    merge_failed: { label: "Merge refused", cls: "warn", group: "other" },
    error: { label: "Error", cls: "bad", group: "other" },
  };
  const REASONS = {
    no_ticket_id: ["No ticket ID in the PR", "Put the ticket key (e.g. PROJ-123) in the PR title or branch name."],
    ticket_unavailable: ["Ticket not found", "Check the ticket key exists and the Jira token can read it."],
    no_acceptance_criteria: ["Ticket has no acceptance criteria", "Add acceptance criteria to the ticket, then push again."],
    diff_unavailable: ["Could not read the code changes", "Check GITHUB_TOKEN can read this repository."],
    empty_diff: ["PR has no code changes", "Push a commit with the actual change."],
    diff_too_large: ["Diff too large for the AI", "Split the PR, or raise MAX_DIFF_CHARS."],
    llm_unavailable: ["AI service unavailable", "Check LLM_API_KEY, LLM_MODEL and the provider status."],
    invalid_llm_response: ["AI answer was not valid", "The AI did not return the required JSON; nothing was merged."],
  };
  const outLabel = (r) => (r.outcome === "merged" && r.mode === "dry-run" ? OUT.merged.dry : (OUT[r.outcome] || { label: r.outcome }).label);
  const outPill = (r) => `<span class="pill ${(OUT[r.outcome] || {}).cls || ""}"><i class="dot"></i>${esc(outLabel(r))}</span>`;
  // AI feedback is markdown-ish: show **bold** and `code` properly, everything else as text (escaped first).
  const md = (t) => esc(t).replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>").replace(/`([^`\n]+)`/g, "<code>$1</code>");
  const group = (r) => (OUT[r.outcome] || { group: "other" }).group;

  // ------------------------------------------------------------ token + API
  const tokenStore = {
    get() { try { return sessionStorage.getItem("dash-token") || ""; } catch (e) { return state.memToken || ""; } },
    set(v) { state.memToken = v; try { v ? sessionStorage.setItem("dash-token", v) : sessionStorage.removeItem("dash-token"); } catch (e) { /* memory only */ } },
  };
  async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-Dashboard-Token": tokenStore.get(), ...(opts.headers || {}) } });
    let body = {};
    try { body = await res.json(); } catch (e) { /* non-JSON */ }
    if (res.status === 401 || res.status === 503 && body.kind === "no_token") { const err = new Error(body.error || "Locked"); err.auth = body.kind || "bad_token"; throw err; }
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
    return body;
  }

  function gate(message, kind) {
    $nav.hidden = true; $mode.hidden = true; closeDrawer();
    const noToken = kind === "no_token";
    $main.innerHTML = `
      <section class="card gate">
        <div class="lockmark"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#C7A17A" stroke-width="2" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg></div>
        <div><h1 style="font:800 24px var(--font-display);margin:0">Dashboard locked</h1>
        <p class="muted" style="margin:6px 0 0">This dashboard can start reviews and merges, so it needs the <b>DASHBOARD_TOKEN</b> from your <code>.env</code>.</p></div>
        ${message ? `<div class="alert ${noToken ? "warn" : "bad"}" role="alert">${esc(message)}</div>` : ""}
        ${noToken ? `<div class="prose-box">1. Open .env next to app.py\n2. Add a line:  DASHBOARD_TOKEN=pick-a-long-random-phrase\n3. Restart:  python app.py\n4. Reload this page and paste the same phrase</div>` : ""}
        <form id="gate-form" class="field">
          <label for="tok">Dashboard token</label>
          <input class="input" id="tok" type="password" autocomplete="current-password" required>
          <small class="muted">Kept only for this browser tab. Never sent anywhere except this server.</small>
          <button class="btn btn-dark" type="submit" style="margin-top:8px">Unlock</button>
        </form>
      </section>`;
    document.getElementById("gate-form").onsubmit = (e) => { e.preventDefault(); tokenStore.set(document.getElementById("tok").value.trim()); boot(); };
    document.getElementById("tok").focus();
  }

  // ------------------------------------------------------------ boot + routing
  async function boot() {
    try {
      state.cfg = await api("/api/config");
      await loadReviews(true);
    } catch (e) {
      if (e.auth) return gate(e.auth === "no_token" ? e.message : (tokenStore.get() ? "That token is not right." : ""), e.auth);
      $main.innerHTML = `<div class="alert bad" role="alert">Could not reach the reviewer server: ${esc(e.message)}. Is <code>python app.py</code> running?</div>`;
      return;
    }
    $nav.hidden = false;
    const live = state.cfg.mode === "live";
    $mode.hidden = false; $mode.className = "mode" + (live ? " live" : "");
    $mode.textContent = live ? "LIVE" : "DRY-RUN";
    $mode.title = live ? "Webhook reviews really comment and merge" : "Webhook reviews change nothing on GitHub";
    route();
  }
  document.getElementById("lock").onclick = () => { tokenStore.set(""); gate("", "locked"); };
  window.addEventListener("hashchange", route);

  function route() {
    if ($nav.hidden) return;
    const view = (location.hash.slice(1) || "overview").split("?")[0];
    $nav.querySelectorAll("a").forEach((a) => { if (a.dataset.view === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    ({ overview: viewOverview, reviews: viewReviews, run: viewRun, settings: viewSettings }[view] || viewOverview)();
    window.scrollTo(0, 0);
  }

  async function loadReviews(first) {
    const { reviews } = await api("/api/reviews");
    if (state.seenIds && !first) reviews.forEach((r) => { if (!state.seenIds.has(r.id)) state.newIds.add(r.id); });
    state.seenIds = new Set(reviews.map((r) => r.id));
    const changed = reviews.length !== state.reviews.length || (reviews[0] || {}).id !== (state.reviews[0] || {}).id;
    state.reviews = reviews;
    return changed;
  }
  setInterval(async () => {
    if (!state.auto || $nav.hidden || document.hidden || state.job) return;
    try {
      const changed = await loadReviews(false);
      const view = (location.hash.slice(1) || "overview");
      if (changed && (view === "overview" || view === "reviews")) { route(); toast("New review arrived"); }
    } catch (e) { if (e.auth) gate("Session ended. Unlock again.", e.auth); }
  }, 10000);

  // ------------------------------------------------------------ overview
  function counts(list) {
    const c = { total: list.length, merged: 0, changes: 0, blocked: 0, other: 0 };
    list.forEach((r) => { c[group(r)]++; });
    return c;
  }
  function viewOverview() {
    const R = state.reviews, c = counts(R);
    const decided = c.merged + c.changes;
    const passRate = decided ? Math.round((c.merged / decided) * 100) : null;
    const reasons = {};
    R.filter((r) => r.outcome === "blocked").forEach((r) => { reasons[r.reason] = (reasons[r.reason] || 0) + 1; });
    const reasonList = Object.entries(reasons).sort((a, b) => b[1] - a[1]);
    const maxReason = reasonList.length ? reasonList[0][1] : 1;
    const tickets = new Set(R.map((r) => r.ticket).filter(Boolean)).size;

    $main.innerHTML = `
      <div class="page-head">
        <div><h1>Overview</h1><p>Every pull request the reviewer has checked, from the webhook, this dashboard or the command line.</p></div>
        ${liveDot()}
      </div>
      ${R.length ? "" : `<div class="empty"><b>No reviews yet</b><span>Open a pull request on a repository with the webhook, or start one yourself.</span><a class="btn btn-primary" href="#run">Review a PR</a></div>`}
      <section class="grid-cards" aria-label="Totals">
        <div class="stat dark"><span class="eyebrow">Reviews</span><b>${c.total}</b><small>${tickets} ticket${tickets === 1 ? "" : "s"} checked</small></div>
        <div class="stat"><span class="eyebrow">Merged</span><b style="color:var(--good-ink)">${c.merged}</b><small>${R.filter((r) => r.outcome === "merged" && r.mode === "dry-run").length} as dry-run</small></div>
        <div class="stat"><span class="eyebrow">Changes requested</span><b style="color:var(--warn-ink)">${c.changes}</b><small>criteria not met</small></div>
        <div class="stat"><span class="eyebrow">Blocked</span><b style="color:var(--bad-ink)">${c.blocked}</b><small>stopped before AI verdict</small></div>
        <div class="stat"><span class="eyebrow">Pass rate</span><b>${passRate === null ? "—" : passRate + "%"}</b><small>of PRs the AI judged</small></div>
      </section>
      <div class="cols">
        <div class="stack">
          <section class="card chart">
            <div class="card-head"><h2 class="eyebrow">Outcomes, last 14 days</h2>
              <div class="legend"><span><i class="c-merged"></i>Merged</span><span><i class="c-changes"></i>Changes requested</span><span><i class="c-blocked"></i>Blocked</span><span><i class="c-other"></i>Other</span></div></div>
            ${dayChart(R)}
          </section>
          <section class="card">
            <div class="card-head"><h2 class="eyebrow">Latest reviews</h2><a class="btn btn-ghost btn-sm" href="#reviews">See all ${R.length}</a></div>
            ${R.length ? `<ul class="recent">${R.slice(0, 6).map((r) => `
              <li><button type="button" data-open="${esc(r.id)}">
                <span class="t">${esc(r.title)}</span>${outPill(r)}
                <small>${esc(r.repo)}#${r.number} · ${esc(r.ticket || "no ticket")} · ${esc(ago(r.time))}</small><small>${esc(r.mode)}</small>
              </button></li>`).join("")}</ul>` : `<p class="muted">Nothing here yet.</p>`}
          </section>
        </div>
        <div class="stack">
          <section class="card">
            <div class="card-head"><h2 class="eyebrow">Why reviews were blocked</h2></div>
            ${reasonList.length ? `<ul class="reasons">${reasonList.map(([code, n]) => `
              <li><div class="top"><span>${esc((REASONS[code] || [code])[0])}</span><span>${n}</span></div>
              <div class="meter"><i class="sand" style="width:${Math.max(6, (n / maxReason) * 100)}%"></i></div>
              <small>${esc((REASONS[code] || ["", ""])[1])}</small></li>`).join("")}</ul>` : `<p class="muted">No blocked reviews. Every PR had a ticket, criteria and a readable diff.</p>`}
          </section>
          <section class="card slate">
            <h2 class="eyebrow">Safety rules, always on</h2>
            <ul class="rules">
              <li><span>Merges <b>only</b> when every step worked and the AI said <b>fulfilled: true</b>.</span></li>
              <li><span>Merge is pinned to the reviewed commit, so a later push can't sneak in.</span></li>
              <li><span>Webhooks need a valid GitHub signature; the dashboard needs its token.</span></li>
              <li><span>Any error means no merge, a PR comment and a failed status.</span></li>
            </ul>
          </section>
        </div>
      </div>`;
    bindOpen();
  }

  function dayChart(R) {
    const days = [];
    const today = new Date(); today.setHours(0, 0, 0, 0);
    for (let i = 13; i >= 0; i--) { const d = new Date(today); d.setDate(d.getDate() - i); days.push({ key: d.toISOString().slice(0, 10), d, merged: 0, changes: 0, blocked: 0, other: 0 }); }
    const idx = Object.fromEntries(days.map((d, i) => [localKey(d.d), i]));
    R.forEach((r) => { const i = idx[r.time.slice(0, 10)]; if (i !== undefined) days[i][group(r)]++; });
    const max = Math.max(1, ...days.map((d) => d.merged + d.changes + d.blocked + d.other));
    const step = max <= 4 ? 1 : Math.ceil(max / 4);
    const top = Math.ceil(max / step) * step;
    const W = 640, H = 210, L = 30, B = 26, T = 10, plotH = H - B - T, bw = (W - L) / 14;
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Reviews per day by outcome, last 14 days">`;
    for (let v = 0; v <= top; v += step) { const y = T + plotH - (v / top) * plotH; svg += `<line class="grid" x1="${L}" x2="${W}" y1="${y}" y2="${y}"/><text class="axis" x="${L - 8}" y="${y + 4}" text-anchor="end">${v}</text>`; }
    days.forEach((d, i) => {
      let y = T + plotH; const x = L + i * bw + bw * 0.2, w = bw * 0.6;
      ["merged", "changes", "blocked", "other"].forEach((k) => {
        if (!d[k]) return; const h = (d[k] / top) * plotH; y -= h;
        svg += `<rect class="c-${k}" x="${x}" y="${y}" width="${w}" height="${Math.max(h - 2, 1)}" rx="3"><title>${d.key}: ${d[k]} ${k}</title></rect>`;
      });
      if (i % 2 === 1 || i === 13) svg += `<text class="axis" x="${x + w / 2}" y="${H - 8}" text-anchor="middle">${i === 13 ? "today" : d.d.toLocaleDateString(undefined, { day: "numeric", month: "short" })}</text>`;
    });
    return svg + "</svg>";
  }
  const localKey = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  function ago(t) {
    const s = (Date.now() - new Date(t.replace(" ", "T")).getTime()) / 1000;
    if (!isFinite(s)) return t;
    if (s < 60) return "just now"; if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago"; return t.slice(0, 10);
  }
  const liveDot = () => `<span class="live-dot ${state.auto ? "" : "off"}"><i></i>${state.auto ? "Auto-refresh every 10 s" : "Auto-refresh paused"}</span>`;

  // ------------------------------------------------------------ reviews table
  function filtered() {
    const q = state.q.toLowerCase();
    return state.reviews.filter((r) => (state.filter === "all" || group(r) === state.filter)
      && (state.source === "all" || r.source === state.source)
      && (!q || [r.title, r.repo, r.ticket, r.branch, String(r.number), r.reason].join(" ").toLowerCase().includes(q)));
  }
  function viewReviews() {
    const c = counts(state.reviews);
    const sources = [...new Set(state.reviews.map((r) => r.source))];
    $main.innerHTML = `
      <div class="page-head"><div><h1>Reviews</h1><p>Click a row for the AI feedback and what was done on GitHub.</p></div>
        <label class="switch"><input type="checkbox" id="auto" ${state.auto ? "checked" : ""}> Auto-refresh</label></div>
      <section class="card">
        <div class="toolbar">
          <div class="left">
            <div class="seg" role="group" aria-label="Outcome">
              ${[["all", "All", c.total], ["merged", "Merged", c.merged], ["changes", "Changes", c.changes], ["blocked", "Blocked", c.blocked], ["other", "Errors", c.other]]
                .map(([k, l, n]) => `<button type="button" data-f="${k}" aria-pressed="${state.filter === k}">${l} <span class="muted">${n}</span></button>`).join("")}
            </div>
            <select class="input" id="src" aria-label="Source" style="width:auto;min-height:38px;padding:6px 10px;font-size:14px">
              <option value="all">All sources</option>${sources.map((s) => `<option ${state.source === s ? "selected" : ""}>${esc(s)}</option>`).join("")}
            </select>
          </div>
          <div class="left">
            <input class="input" id="q" type="search" placeholder="Search title, ticket, repo…" value="${esc(state.q)}" aria-label="Search reviews">
            <button class="btn btn-ghost btn-sm" type="button" id="csv">Export CSV</button>
          </div>
        </div>
        <div id="tbl"></div>
      </section>`;
    $main.querySelectorAll("[data-f]").forEach((b) => b.onclick = () => { state.filter = b.dataset.f; viewReviews(); });
    document.getElementById("src").onchange = (e) => { state.source = e.target.value; drawTable(); };
    const q = document.getElementById("q");
    q.oninput = () => { state.q = q.value; drawTable(); };
    document.getElementById("auto").onchange = (e) => { state.auto = e.target.checked; };
    document.getElementById("csv").onclick = exportCsv;
    drawTable();
  }
  function drawTable() {
    const rows = filtered();
    const box = document.getElementById("tbl");
    if (!rows.length) { box.innerHTML = `<div class="empty"><b>No reviews match</b><span>Try another filter or clear the search.</span></div>`; return; }
    box.innerHTML = `<div class="table-box"><table>
      <thead><tr><th>Pull request</th><th>Ticket</th><th>Result</th><th class="hide-sm">Mode</th><th class="hide-sm">Source</th><th class="hide-sm">When</th></tr></thead>
      <tbody>${rows.map((r) => `
        <tr class="clickable ${state.newIds.has(r.id) ? "new" : ""}" data-open="${esc(r.id)}" tabindex="0">
          <td><span class="pr-t">${esc(r.title)}</span><small>${esc(r.repo)}#${r.number} · ${esc(r.branch)}</small></td>
          <td class="mono">${esc(r.ticket || "—")}</td>
          <td>${outPill(r)}${r.outcome === "blocked" ? `<br><small>${esc((REASONS[r.reason] || [r.reason])[0])}</small>` : ""}</td>
          <td class="hide-sm"><span class="pill ${r.mode === "live" ? "bad" : ""}">${esc(r.mode)}</span></td>
          <td class="hide-sm">${esc(r.source)}</td>
          <td class="hide-sm mono" title="${esc(r.time)}">${esc(ago(r.time))}</td>
        </tr>`).join("")}</tbody></table></div>
      <p class="muted small" style="margin:10px 0 0">${rows.length} of ${state.reviews.length} reviews</p>`;
    bindOpen();
  }
  function exportCsv() {
    const cols = ["time", "repo", "number", "title", "branch", "ticket", "outcome", "reason", "mode", "source", "url", "feedback"];
    const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    download("pr-reviews.csv", [cols.join(","), ...filtered().map((r) => cols.map((k) => q(r[k])).join(","))].join("\n"), "text/csv");
  }

  // ------------------------------------------------------------ detail drawer
  function bindOpen() {
    $main.querySelectorAll("[data-open]").forEach((el) => {
      el.onclick = () => openDrawer(el.dataset.open);
      el.onkeydown = (e) => { if (e.key === "Enter") openDrawer(el.dataset.open); };
    });
  }
  function openDrawer(id) {
    const r = state.reviews.find((x) => x.id === id); if (!r) return;
    state.newIds.delete(id);
    const cls = (OUT[r.outcome] || {}).cls || "";
    const reason = REASONS[r.reason];
    const headline = {
      merged: r.mode === "dry-run" ? "All criteria met: this PR would be merged (dry-run, nothing changed)." : "All criteria met: the PR was merged.",
      changes_requested: "Some acceptance criteria are not met. Feedback was posted on the PR and its status set to failed.",
      blocked: reason ? reason[0] + "." : "The review stopped before an AI verdict.",
      merge_failed: "The AI said all criteria are met, but GitHub refused the merge.",
      error: "Unexpected error. The reviewer failed safe: nothing was merged.",
    }[r.outcome] || r.outcome;
    const verb = r.mode === "dry-run" ? "Would " : "";
    $drawer.innerHTML = `
      <div class="drawer-back" data-close></div>
      <aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="d-title">
        <header><div><span class="eyebrow">${esc(r.repo)} · PR #${r.number}</span><h2 id="d-title">${esc(r.title)}</h2></div>
          <button class="btn btn-ghost btn-sm" type="button" data-close aria-label="Close">Close</button></header>
        <div class="body">
          <div class="banner ${cls}"><b>${esc(outLabel(r))}</b><span>${esc(headline)}</span>
            ${r.message && r.outcome !== "changes_requested" ? `<span>${esc(r.message)}</span>` : ""}
            ${reason ? `<span><b style="font-size:14px">Fix:</b> ${esc(reason[1])}</span>` : ""}</div>
          <div class="links">
            <a class="btn btn-ghost btn-sm" href="${esc(r.url)}" target="_blank" rel="noopener">Open PR</a>
            ${r.ticket_url ? `<a class="btn btn-ghost btn-sm" href="${esc(r.ticket_url)}" target="_blank" rel="noopener">Open ${esc(r.ticket)}</a>` : ""}
            <button class="btn btn-dark btn-sm" type="button" id="rerun">Re-run (dry-run)</button>
            ${r.feedback ? `<button class="btn btn-ghost btn-sm" type="button" id="cpy">Copy feedback</button>` : ""}
          </div>
          ${r.feedback ? `<section><h3>AI feedback</h3><div class="prose-box md" style="margin-top:8px">${md(r.feedback)}</div></section>` : ""}
          <section><h3>${r.mode === "dry-run" ? "Actions (dry-run: shown, not done)" : "Actions taken on GitHub"}</h3>
            ${r.actions.length ? `<ul class="acts" style="margin-top:8px">${r.actions.map((a) => `<li class="${a.startsWith("merge") ? "merge" : ""}">${esc(verb ? verb + a : a.charAt(0).toUpperCase() + a.slice(1))}</li>`).join("")}</ul>` : `<p class="muted">None.</p>`}</section>
          <section><h3>Details</h3><dl class="kv" style="margin-top:8px">
            <dt>Ticket</dt><dd>${esc(r.ticket || "—")} ${r.ticket ? `<span class="muted">(${esc(r.ticket_system)})</span>` : ""}</dd>
            <dt>Branch</dt><dd class="mono">${esc(r.branch)}</dd>
            <dt>Commit</dt><dd class="mono">${esc((r.head_sha || "").slice(0, 12))}</dd>
            <dt>AI model</dt><dd>${esc(r.model)}</dd>
            <dt>Mode</dt><dd>${esc(r.mode)}</dd>
            <dt>Started by</dt><dd>${esc(r.source)}</dd>
            <dt>Time</dt><dd>${esc(r.time)}</dd>
          </dl></section>
        </div>
      </aside>`;
    $drawer.querySelectorAll("[data-close]").forEach((b) => b.onclick = closeDrawer);
    const cp = document.getElementById("cpy"); if (cp) cp.onclick = () => copy(r.feedback);
    document.getElementById("rerun").onclick = () => { closeDrawer(); startRun(r.repo, r.number, false); };
    $drawer.querySelector("[data-close].btn").focus();
    if (location.hash.startsWith("#reviews")) drawTable();
  }
  function closeDrawer() { $drawer.innerHTML = ""; }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  // ------------------------------------------------------------ review a PR
  function viewRun(prefill) {
    const recentRepos = [...new Set(state.reviews.map((r) => r.repo))].slice(0, 5);
    const p = prefill || UI.store.get("pr-last", { repo: recentRepos[0] || "", number: "" });
    $main.innerHTML = `
      <div class="page-head"><div><h1>Review a PR</h1><p>Runs exactly the same pipeline as the webhook, on any open pull request.</p></div></div>
      <div class="run-grid">
        <section class="card">
          <form id="run-form" class="stack" style="gap:16px">
            <div class="form-row">
              <div class="field"><label for="repo">Repository</label><input class="input" id="repo" placeholder="owner/repo" required value="${esc(p.repo)}" autocomplete="off" spellcheck="false"></div>
              <div class="field"><label for="num">PR number</label><input class="input" id="num" type="number" min="1" required value="${esc(p.number)}" inputmode="numeric"></div>
            </div>
            ${recentRepos.length ? `<div class="chips" aria-label="Recent repositories">${recentRepos.map((r) => `<button type="button" class="chip" data-repo="${esc(r)}">${esc(r)}</button>`).join("")}</div>` : ""}
            <fieldset class="modebox" style="border:0;padding:0;margin:0">
              <legend class="eyebrow" style="margin-bottom:8px">Mode</legend>
              <label><input type="radio" name="mode" value="dry" checked><b>Dry-run</b>Shows what it would do. Changes nothing.</label>
              <label class="danger"><input type="radio" name="mode" value="live"><b>Live</b>Really comments, sets status and may merge.</label>
            </fieldset>
            <div class="confirm" id="confirm" hidden>
              <label class="switch"><input type="checkbox" id="ok"> I understand this can merge the PR into its base branch.</label>
            </div>
            <div class="alert bad" id="run-err" role="alert" hidden></div>
            <div><button class="btn btn-primary" type="submit" id="go">Start review</button></div>
          </form>
          <div id="run-out"></div>
        </section>
        <section class="card">
          <h2 class="eyebrow">What happens</h2>
          <ol class="flow">
            <li><span><b>Find the ticket</b>Ticket key from the PR title, else the branch name.</span></li>
            <li><span><b>Read the acceptance criteria</b>From ${esc(state.cfg.ticket_system === "azure" ? "Azure DevOps" : "Jira")}. No criteria means no review.</span></li>
            <li><span><b>Read the code changes</b>The PR diff from GitHub, up to ${UI.fmt(state.cfg.max_diff_chars)} characters.</span></li>
            <li><span><b>AI check</b>${esc(state.cfg.llm)} answers fulfilled true or false, with feedback.</span></li>
            <li><span><b>Act</b>True: success status and ${esc(state.cfg.merge_method)} merge. False: feedback comment and failed status.</span></li>
          </ol>
        </section>
      </div>`;
    const form = document.getElementById("run-form");
    $main.querySelectorAll("[data-repo]").forEach((b) => b.onclick = () => { document.getElementById("repo").value = b.dataset.repo; document.getElementById("num").focus(); });
    form.querySelectorAll("input[name=mode]").forEach((i) => i.onchange = () => { document.getElementById("confirm").hidden = form.mode.value !== "live"; });
    form.onsubmit = (e) => {
      e.preventDefault();
      const live = form.mode.value === "live";
      if (live && !document.getElementById("ok").checked) { showRunErr("Tick the box to confirm a live review."); return; }
      startRun(document.getElementById("repo").value.trim(), document.getElementById("num").value, live);
    };
  }
  function showRunErr(msg) { const el = document.getElementById("run-err"); if (el) { el.hidden = false; el.textContent = msg; } else toast(msg); }

  async function startRun(repo, number, live) {
    if (!location.hash.startsWith("#run")) { history.replaceState(null, "", "#run"); route(); }
    document.getElementById("repo").value = repo; document.getElementById("num").value = number;
    UI.store.set("pr-last", { repo, number });
    const out = document.getElementById("run-out"), go = document.getElementById("go");
    document.getElementById("run-err").hidden = true;
    go.disabled = true;
    out.innerHTML = `<div class="running" style="margin-top:20px"><div class="spinner" aria-hidden="true"></div><div><b>Reviewing ${esc(repo)}#${esc(number)}</b><br><span class="muted small" id="el">Fetching the PR…</span></div></div>`;
    try {
      const j = await api("/api/review", { method: "POST", body: JSON.stringify({ repo, number: Number(number), live }) });
      state.job = j.job;
      out.querySelector("b").textContent = `Reviewing ${j.pr}: ${j.title}`;
      let res;
      for (;;) {
        await new Promise((r) => setTimeout(r, 1200));
        res = await api(`/api/review-jobs/${j.job}`);
        const el = document.getElementById("el"); if (el) el.textContent = `${live ? "Live" : "Dry-run"} · ${res.elapsed}s · ticket, diff and AI check (the AI step takes longest)`;
        if (res.state !== "running") break;
      }
      state.job = null;
      await loadReviews(false);
      if (res.state === "error" || !res.entry) throw new Error(res.error || "The review did not finish. See the server log.");
      const r = res.entry; state.newIds.delete(r.id);
      const target = document.getElementById("run-out"); if (!target) return;
      target.innerHTML = `<div class="banner ${(OUT[r.outcome] || {}).cls || ""}" style="margin-top:20px"><b>${esc(outLabel(r))}</b>
        <span>${esc(r.ticket || "No ticket")} · ${esc(r.mode)}${r.outcome === "blocked" ? " · " + esc((REASONS[r.reason] || [r.reason])[0]) : ""}</span></div>
        <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap"><button class="btn btn-dark btn-sm" type="button" id="see">See feedback and actions</button><a class="btn btn-ghost btn-sm" href="#reviews">All reviews</a></div>`;
      document.getElementById("see").onclick = () => openDrawer(r.id);
    } catch (e) {
      state.job = null;
      if (e.auth) return gate("Session ended. Unlock again.", e.auth);
      const t = document.getElementById("run-out"); if (t) t.innerHTML = "";
      showRunErr(e.message);
    } finally { const g = document.getElementById("go"); if (g) g.disabled = false; }
  }

  // ------------------------------------------------------------ settings
  function viewSettings() {
    const c = state.cfg;
    const hook = location.origin + "/webhook";
    const ready = !c.missing.length;
    $main.innerHTML = `
      <div class="page-head"><div><h1>Settings</h1><p>Read from <code>.env</code> when the server started. Secrets are never shown, only whether they are set.</p></div></div>
      <div class="alert ${ready ? "good" : "warn"}" role="status">${ready ? "Ready: every required setting is present." : "Missing: " + esc(c.missing.join(", ")) + ". Add them to .env and restart app.py."}</div>
      <div class="settings-grid">
        <section class="card"><h2 class="eyebrow">Secrets and connections</h2>
          <ul class="checks">${c.settings.map((s) => `<li>${esc(s.name)}<span class="pill ${s.set ? "good" : "bad"}">${s.set ? "set" : "missing"}</span></li>`).join("")}</ul>
        </section>
        <section class="card"><h2 class="eyebrow">Behaviour</h2>
          <dl class="kv">
            <dt>Mode</dt><dd><span class="pill ${c.mode === "live" ? "bad" : "accent"}">${esc(c.mode)}</span> <span class="muted small">DRY_RUN=${c.mode === "live" ? "0" : "1"}</span></dd>
            <dt>Tickets</dt><dd>${esc(c.ticket_system === "azure" ? "Azure DevOps" : "Jira")}${c.project_keys ? ` <span class="muted small">keys: ${esc(c.project_keys)}</span>` : ""}</dd>
            <dt>AI model</dt><dd class="mono">${esc(c.llm)}</dd>
            <dt>Merge method</dt><dd>${esc(c.merge_method)}</dd>
            <dt>Max diff</dt><dd>${UI.fmt(c.max_diff_chars)} characters</dd>
            <dt>GitHub API</dt><dd class="mono">${esc(c.github_api)}</dd>
          </dl>
        </section>
        <section class="card"><h2 class="eyebrow">Connect a repository</h2>
          <ol class="flow">
            <li><span><b>Make this server public</b>For example <code>ngrok http ${esc(location.port || "5000")}</code>, then use the https address it shows.</span></li>
            <li><span><b>Add the webhook on GitHub</b>Repository → Settings → Webhooks → Add webhook.</span></li>
            <li><span><b>Payload URL</b><span class="copyline" style="margin-top:6px"><span>${esc(hook)}</span><button class="btn btn-ghost btn-sm" type="button" id="cp-hook">Copy</button></span></span></li>
            <li><span><b>Content type, secret, events</b>application/json · same value as WEBHOOK_SECRET · Pull requests only.</span></li>
          </ol>
        </section>
        <section class="card"><h2 class="eyebrow">Going live, safely</h2>
          <ol class="flow">
            <li><span><b>Test in dry-run first</b>Review a few PRs here and read the feedback.</span></li>
            <li><span><b>Protect the branch</b>Require the <code>ai-pr-reviewer</code> status check on main.</span></li>
            <li><span><b>Switch</b>Set <code>DRY_RUN=0</code> in .env and restart app.py.</span></li>
          </ol>
        </section>
      </div>`;
    document.getElementById("cp-hook").onclick = () => copy(hook);
  }

  boot();
})();
