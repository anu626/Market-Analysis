/* Pulse — vanilla JS SPA. No framework, no build step. */

const API = ""; // same origin

const PAGE_SIZE = 25;

const state = {
  view: "ranked",         // 'ranked' | 'latest'
  source: "",             // exact source_name or ""
  search: "",             // free-text
  articles: [],           // full loaded list
  offset: 0,              // pagination cursor
  loading: false,         // initial / page change
  loadingMore: false,     // infinite scroll
  done: false,            // no more results
  searchSeq: 0,           // request sequencing for debounced search
  byId: new Map(),        // id -> article (for modal)
};

// --- DOM ---
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const listEl = $("#article-list");
const loadingEl = $("#loading");
const loadingMoreEl = $("#loading-more");
const endEl = $("#end-of-list");
const emptyEl = $("#empty");
const errorEl = $("#error");
const statsEl = $("#stats");
const statusEl = $("#status");
const chipsEl = $("#source-chips");
const searchEl = $("#search");
const btnRefresh = $("#btn-refresh");
const btnIngest = $("#btn-ingest");
const modalEl = $("#modal");

// --- Utilities ---
const domainOf = (url) => {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
};

const timeAgo = (iso) => {
  if (!iso) return "";
  const sec = Math.max(1, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (sec < 60) return `${sec}s ago`;
  const m = Math.floor(sec / 60); if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);   if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);   if (d < 30) return `${d}d ago`;
  return `${Math.floor(d / 30)}mo ago`;
};

const fullTime = (iso) => {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch { return iso; }
};

const escapeHtml = (s) => String(s ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#39;");

const debounce = (fn, ms) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

const setStatus = (text, kind = "") => {
  statusEl.textContent = text;
  statusEl.className = `status ${kind}`;
  if (text) {
    setTimeout(() => {
      if (statusEl.textContent === text) {
        statusEl.textContent = ""; statusEl.className = "status";
      }
    }, 4500);
  }
};

// --- Tag config: maps vertical + source signals → display tag ---
// Priority: source-based (YouTube/GitHub) > vertical override > vertical fallback
const TAG_CONFIG = {
  layoffs:     { label: "Layoffs",          icon: "📉", cls: "layoffs"  },
  hiring:      { label: "Hiring",           icon: "💼", cls: "hiring"   },
  funding:     { label: "Funding",          icon: "💰", cls: "funding"  },
  ai:          { label: "AI",               icon: "✦",  cls: "ai"       },
  skills_tools:{ label: "Skills & Tools",   icon: "🛠", cls: "skills"   },
  software:    { label: "Dev",              icon: "💻", cls: "software" },
  hardware:    { label: "Hardware",         icon: "⚡", cls: "hardware" },
  industry:    { label: "Industry",         icon: "📊", cls: "industry" },
  youtube:     { label: "YouTube",          icon: "▶",  cls: "youtube"  },
  github:      { label: "GitHub Trending",  icon: "⭐", cls: "github"   },
};

function resolveTag(a) {
  const src = (a.source_name || "").toLowerCase();
  if (src.includes("youtube"))              return TAG_CONFIG.youtube;
  if (src.includes("github") || src.includes("trending")) return TAG_CONFIG.github;
  return TAG_CONFIG[a.vertical] ?? null;
}

// --- Rendering ---
function renderArticleHtml(a, idx) {
  const domain = domainOf(a.url);
  const displayTitle = escapeHtml(a.ai_title || a.title);
  const displaySummary = a.ai_summary || a.summary;
  const aiEnriched = !!a.ai_title;
  const when = timeAgo(a.published_at || a.created_at);
  const rank = (a.rank_score ?? 0).toFixed(2);
  const rankClass = idx < 3 ? "top" : "";
  const highlightedClass = a.is_highlighted ? " article--highlighted" : "";
  const tag = resolveTag(a);
  const tagHtml = tag
    ? `<span class="meta-item"><span class="tag tag--${tag.cls}">${tag.icon} ${tag.label}</span></span>`
    : "";
  return `
    <li class="article" data-id="${a.id}">
      <div class="article-rank ${rankClass}">${idx + 1}</div>
      <div class="article-body">
        <div class="article-title">
          ${a.is_highlighted ? `<span class="featured-badge">Featured</span>` : ""}
          ${displayTitle}${domain ? `<span class="article-domain">${escapeHtml(domain)}</span>` : ""}
        </div>
        ${displaySummary ? `<p class="article-summary">${escapeHtml(displaySummary)}${aiEnriched ? `<span class="ai-badge" title="AI-enhanced">✦</span>` : ""}</p>` : ""}
        <div class="article-meta">
          <span class="meta-item"><span class="source-pill">${escapeHtml(a.source_name)}</span></span>
          ${tagHtml}
          ${(a.source_count ?? 1) > 1 ? `<span class="meta-item source-count">+${a.source_count - 1} more source${a.source_count > 2 ? "s" : ""}</span>` : ""}
          <span class="meta-item">⏱ ${when}</span>
          <span class="meta-item rank-score">rank ${rank}</span>
        </div>
      </div>
    </li>
  `;
}

function renderList() {
  if (!state.articles.length) {
    listEl.classList.add("hidden");
    emptyEl.classList.remove("hidden");
    statsEl.textContent = "";
    endEl.classList.add("hidden");
    return;
  }
  emptyEl.classList.add("hidden");
  listEl.classList.remove("hidden");
  listEl.innerHTML = state.articles.map(renderArticleHtml).join("");

  const sources = new Set(state.articles.map((a) => a.source_name));
  const top = state.articles[0];
  statsEl.innerHTML = `
    <span class="stats-item"><strong>${state.articles.length}</strong> articles</span>
    <span class="stats-item"><strong>${sources.size}</strong> sources</span>
    ${top ? `<span class="stats-item">Top score: <strong>${top.score}</strong></span>` : ""}
    ${state.search ? `<span class="stats-item">Filter: <strong>"${escapeHtml(state.search)}"</strong></span>` : ""}
  `;
  endEl.classList.toggle("hidden", !state.done);
}

async function loadSourceChips() {
  try {
    const resp = await fetch(`${API}/sources`);
    if (!resp.ok) return;
    const sources = await resp.json();
    const total = sources.reduce((n, s) => n + s.count, 0);
    chipsEl.innerHTML = [
      `<button class="chip ${state.source === "" ? "active" : ""}" data-source="">All (${total})</button>`,
      ...sources.map((s) =>
        `<button class="chip ${state.source === s.name ? "active" : ""}" data-source="${escapeHtml(s.name)}">${escapeHtml(s.name)} <span style="opacity:.6">${s.count}</span></button>`
      ),
    ].join("");
  } catch { /* fail open */ }
}

function showLoading(on) {
  state.loading = on;
  loadingEl.classList.toggle("hidden", !on);
  if (on) {
    listEl.classList.add("hidden");
    emptyEl.classList.add("hidden");
    errorEl.classList.add("hidden");
    endEl.classList.add("hidden");
    loadingMoreEl.classList.add("hidden");
  }
  btnRefresh.disabled = on;
}

function showError(msg) {
  errorEl.textContent = `⚠ ${msg}`;
  errorEl.classList.remove("hidden");
}

// --- Data loading ---
function buildUrl(offset) {
  const path = state.view === "ranked" ? "/articles" : "/articles/latest";
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
  if (state.source) params.set("source", state.source);
  if (state.search) params.set("q", state.search);
  return `${API}${path}?${params}`;
}

async function loadFirstPage() {
  showLoading(true);
  errorEl.classList.add("hidden");
  state.offset = 0;
  state.done = false;
  state.byId.clear();
  const seq = ++state.searchSeq;
  try {
    const resp = await fetch(buildUrl(0));
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (seq !== state.searchSeq) return; // stale
    state.articles = data;
    state.offset = data.length;
    state.done = data.length < PAGE_SIZE;
    data.forEach((a) => state.byId.set(a.id, a));
    renderList();
  } catch (e) {
    showError(`Failed to load articles: ${e.message}`);
    listEl.classList.add("hidden");
    emptyEl.classList.add("hidden");
  } finally {
    if (seq === state.searchSeq) showLoading(false);
  }
}

async function loadNextPage() {
  if (state.loading || state.loadingMore || state.done) return;
  state.loadingMore = true;
  loadingMoreEl.classList.remove("hidden");
  try {
    const resp = await fetch(buildUrl(state.offset));
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.length === 0) { state.done = true; return; }
    state.articles = state.articles.concat(data);
    state.offset += data.length;
    state.done = data.length < PAGE_SIZE;
    data.forEach((a) => state.byId.set(a.id, a));
    renderList();
  } catch (e) {
    setStatus(`Pagination failed: ${e.message}`, "error");
  } finally {
    state.loadingMore = false;
    loadingMoreEl.classList.add("hidden");
  }
}

async function triggerIngest() {
  btnIngest.disabled = true;
  setStatus("Fetching from sources…");
  try {
    const resp = await fetch(`${API}/ingest`, { method: "POST" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const stats = await resp.json();
    setStatus(
      `✓ +${stats.inserted} new · ${stats.duplicates} dupes · ${stats.errors} errors`,
      "success"
    );
    await loadSourceChips();
    await loadFirstPage();
  } catch (e) {
    setStatus(`Failed: ${e.message}`, "error");
  } finally {
    btnIngest.disabled = false;
  }
}

// --- Modal ---
function openModal(id) {
  const a = state.byId.get(id);
  if (!a) return;
  $("#modal-title").textContent = a.ai_title || a.title;
  $("#modal-summary").textContent = a.ai_summary || a.summary || "";
  $("#modal-source").textContent = a.source_name;
  $("#modal-source-2").textContent = a.source_name;
  $("#modal-domain").textContent = domainOf(a.url);
  $("#modal-time").textContent = fullTime(a.published_at || a.created_at);
  $("#modal-score").textContent = a.score;
  $("#modal-rank").textContent = (a.rank_score ?? 0).toFixed(2);
  $("#modal-link").href = a.url;
  modalEl.classList.remove("hidden");
  document.body.style.overflow = "hidden";
}
function closeModal() {
  modalEl.classList.add("hidden");
  document.body.style.overflow = "";
}

// --- Event wiring ---
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    if (state.loading) return;
    state.view = tab.dataset.view;
    $$(".tab").forEach((t) =>
      t.setAttribute("aria-pressed", t.dataset.view === state.view ? "true" : "false")
    );
    loadFirstPage();
  });
});

chipsEl.addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip || state.loading) return;
  state.source = chip.dataset.source;
  $$("#source-chips .chip").forEach((c) =>
    c.classList.toggle("active", c.dataset.source === state.source)
  );
  loadFirstPage();
});

const debouncedSearch = debounce(() => {
  state.search = searchEl.value.trim();
  loadFirstPage();
}, 280);
searchEl.addEventListener("input", debouncedSearch);

// "/" focus search; Esc clears or closes modal
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!modalEl.classList.contains("hidden")) closeModal();
    else if (document.activeElement === searchEl) {
      searchEl.value = ""; state.search = ""; searchEl.blur(); loadFirstPage();
    }
  }
  if (e.key === "/" && document.activeElement !== searchEl) {
    e.preventDefault(); searchEl.focus(); searchEl.select();
  }
});

btnRefresh.addEventListener("click", loadFirstPage);
btnIngest.addEventListener("click", triggerIngest);

// Modal close handlers
modalEl.addEventListener("click", (e) => {
  if (e.target.dataset.close !== undefined || e.target.closest("[data-close]")) closeModal();
});

// Article click → modal (ignore clicks on actual links if any)
listEl.addEventListener("click", (e) => {
  const card = e.target.closest(".article");
  if (!card) return;
  if (e.target.closest("a")) return; // let real links work
  openModal(Number(card.dataset.id));
});

// Infinite scroll
window.addEventListener("scroll", () => {
  const scrolled = window.innerHeight + window.scrollY;
  const threshold = document.body.offsetHeight - 600;
  if (scrolled >= threshold) loadNextPage();
});

// --- Init ---
(async function init() {
  await loadSourceChips();
  await loadFirstPage();
})();
