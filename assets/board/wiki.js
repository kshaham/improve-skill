"use strict";
// Knowledge view: the wiki. The skill rebuilds it from the knowledge graph only when an improve
// session finishes, so it is a settled digest: what the graph knows, what changed, and what its
// dreaming found. Every page is built from data with DOM elements, never from HTML strings.

const WIKI_FIXED_PAGES = [
  ["index", "Overview"],
  ["knowledge", "Knowledge"],
  ["changes", "Changes"],
  ["dreams", "Dreams"],
  ["sessions", "Sessions"],
];
// `1 note`, `2 notes`.
const wikiPlural = (count, word) => `${countText(count)} ${count === 1 ? word : word + "s"}`;
let wikiFilter = "",
  wikiShown = 0,
  wikiTotal = 0;
let wikiData = null,
  wikiPage = "index",
  wikiLoading = false,
  wikiMode = false;

function applyKnowledgeMode(wiki) {
  wikiMode = wiki;
  $("knowledge-graph-mode").setAttribute("aria-pressed", String(!wiki));
  $("knowledge-wiki-mode").setAttribute("aria-pressed", String(wiki));
  $("graph-panel").hidden = wiki;
  $("wiki-panel").hidden = !wiki;
}

function setKnowledgeMode(wiki) {
  applyKnowledgeMode(wiki);
  if (wiki) loadWiki();
  else {
    graphFitPending = true;
    renderGraphView();
  }
}

// A link to a graph node always shows the graph; the caller renders it.
function showGraphMode() {
  if (!wikiMode) return;
  applyKnowledgeMode(false);
  graphFitPending = true;
}

async function loadWiki() {
  if (wikiLoading) return;
  wikiLoading = true;
  try {
    wikiData = await boardJSON("/api/wiki");
    renderWiki();
  } catch (error) {
    $("wiki-page").replaceChildren(element("p", "form-error", "The wiki could not load. " + error.message));
  } finally {
    wikiLoading = false;
  }
}

function wikiPages() {
  const pages = wikiData?.pages || {};
  const areas = (pages.index?.areas || []).map((row) => [row.page, row.name]);
  return [...WIKI_FIXED_PAGES, ...areas].filter(([name]) => pages[name]);
}

function renderWiki() {
  const pages = wikiData?.pages || {};
  if (!pages.index) {
    $("wiki-pages").replaceChildren();
    $("wiki-page").replaceChildren(
      element("h3", "", "No wiki yet"),
      element(
        "p",
        "focus-hint",
        "The wiki is rebuilt from the knowledge graph when an improve session finishes. It appears after the next session ends.",
      ),
    );
    return;
  }
  if (!pages[wikiPage]) wikiPage = "index";
  const list = element("ul", "wiki-page-list");
  for (const [name, title] of wikiPages()) {
    const item = element("li");
    const button = element("button", "link-button" + (name.startsWith("areas/") ? " wiki-area" : ""), title);
    button.type = "button";
    if (name === wikiPage) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => openWikiPage(name));
    item.append(button);
    list.append(item);
  }
  $("wiki-pages").replaceChildren(list);
  wikiShown = wikiTotal = 0;
  $("wiki-page").replaceChildren(...wikiPageContent(wikiPage, pages[wikiPage], pages));
  $("wiki-filter-count").textContent =
    wikiFilter && wikiTotal
      ? `${countText(wikiShown)} of ${countText(wikiTotal)} notes on this page match`
      : wikiFilter
        ? "The filter applies to notes; this page has none."
        : "";
}

function openWikiPage(name) {
  wikiPage = name;
  renderWiki();
  $("wiki-page").focus({ preventScroll: true });
}

// Every note row the wiki carries: the knowledge page's groups, and the rows area pages list
// beyond the kind caps.
function wikiAllNotes(pages) {
  const knowledge = pages.knowledge || {};
  return [...(knowledge.groups || []).flatMap((group) => group.notes), ...(knowledge.more || [])];
}

// Open a note in the graph view, where its links, evidence, and curation live.
function wikiGraphButton(id, text, className = "link-button") {
  const button = element("button", className, text);
  button.type = "button";
  button.addEventListener("click", () => openGraphNode(id));
  return button;
}

// A note matches the filter when every word appears in its text, areas, files, or kind.
function wikiMatches(row) {
  if (!wikiFilter) return true;
  const haystack = [row.text, row.areas, row.files, row.kind, row.verdict].join(" ").toLowerCase();
  return wikiFilter.split(/\s+/).every((word) => haystack.includes(word));
}

function wikiNoteRows(rows) {
  wikiTotal += rows.length;
  rows = rows.filter((row) => wikiMatches(row));
  wikiShown += rows.length;
  if (!rows.length) return [element("p", "focus-hint", wikiFilter ? "No notes match the filter." : "None recorded.")];
  const texts = new Map(wikiAllNotes(wikiData?.pages || {}).map((row) => [row.id, row.text]));
  const list = element("ul", "wiki-notes");
  for (const row of rows) {
    const item = element("li");
    const tags = element("span", "wiki-tags");
    tags.append(element("span", "wiki-tag kind-" + (KIND_LABELS[row.kind] ? row.kind : "fact"), KIND_LABELS[row.kind] || "Fact"));
    if (row.verdict === "proven" || row.verdict === "contested")
      tags.append(element("span", "wiki-tag verdict-" + row.verdict, row.verdict === "proven" ? "Proven" : "Contested"));
    if (row.pinned) tags.append(element("span", "wiki-tag pinned", "Pinned"));
    item.append(tags, wikiGraphButton(row.id, row.text, "wiki-text wiki-open"));
    const where = [...(row.areas || "").split(" ").filter(Boolean), ...(row.files || "").split(" ").filter(Boolean)];
    if (where.length) item.append(element("small", "wiki-where", where.join(" · ")));
    const related = (row.related || "").split(" ").filter((id) => texts.has(id));
    if (related.length) {
      const links = element("small", "wiki-related", "Related: ");
      related.forEach((id, index) => {
        if (index) links.append(" · ");
        links.append(wikiGraphButton(id, shortLabel(texts.get(id), 48)));
      });
      item.append(links);
    }
    list.append(item);
  }
  return [list];
}

function wikiHotspots(rows) {
  if (!rows.length) return [element("p", "focus-hint", "None: no file keeps failing.")];
  const list = element("ul", "wiki-areas");
  for (const row of rows) {
    const item = element("li");
    item.append(element("span", "wiki-path", row.file), element("small", "", `${row.done}/${row.attempts} verified`));
    list.append(item);
  }
  return [list];
}

function wikiContradictions(rows) {
  if (!rows.length) return [element("p", "focus-hint", "None: no two notes say opposite things.")];
  const list = element("ul", "wiki-changes");
  for (const row of rows) {
    const item = element("li", "change-verdict");
    item.append(
      element("strong", "", "Decide which holds, then rate the other Wrong"),
      wikiGraphButton(row.a, row.a_text, "wiki-text wiki-open"),
      wikiGraphButton(row.b, row.b_text, "wiki-text wiki-open"),
    );
    list.append(item);
  }
  return [list];
}

// A capped list says how much it leaves out; the graph view and its search hold the rest.
function wikiCapped(shown, total) {
  return total > shown
    ? [element("p", "focus-hint", `Showing the strongest ${countText(shown)} of ${countText(total)}; search the graph for the rest.`)]
    : [];
}

function wikiSection(title, children) {
  const section = element("section", "wiki-section");
  section.append(element("h4", "", title), ...children);
  return section;
}

function wikiIndexPage(page) {
  const counts = page.counts || {};
  const last = page.last_session || {};
  const areas = element("ul", "wiki-areas");
  for (const row of page.areas || []) {
    const item = element("li");
    const button = element("button", "link-button", row.name);
    button.type = "button";
    button.addEventListener("click", () => openWikiPage(row.page));
    item.append(button, element("small", "", `${wikiPlural(row.notes, "note")} · ${row.done}/${row.attempts} verified`));
    areas.append(item);
  }
  return [
    element("p", "wiki-stamp", `Rebuilt ${relativeTime(page.built_at)} when session ${page.run_id || "?"} finished · graph revision ${page.revision}, dream tick ${page.tick}`),
    element(
      "p",
      "",
      `${wikiPlural(counts.notes || 0, "live note")} (${counts.proven || 0} proven, ${counts.contested || 0} contested, ${counts.archived || 0} archived) across ${wikiPlural(counts.areas || 0, "area")} and ${wikiPlural(counts.files || 0, "file")}.`,
    ),
    wikiSection("Key knowledge", wikiNoteRows(page.key || [])),
    wikiSection("Areas", [page.areas?.length ? areas : element("p", "focus-hint", "None yet.")]),
    wikiSection("Files that resist change", wikiHotspots(page.hotspots || [])),
    wikiSection("Last session", [element("p", "", `${last.added || 0} added, ${last.removed || 0} removed, ${last.verdicts || 0} verdicts changed.`)]),
  ];
}

function wikiKnowledgePage(page) {
  const sections = [];
  for (const group of page.groups || []) {
    const before = wikiShown;
    const rows = wikiNoteRows(group.notes);
    if (!wikiFilter || wikiShown > before)
      sections.push(wikiSection((KIND_LABELS[group.kind] || "Fact") + "s", [...wikiCapped(group.notes.length, group.total), ...rows]));
  }
  if (wikiFilter && !sections.length) sections.push(element("p", "focus-hint", "No notes match the filter."));
  return sections;
}

function wikiChangesPage(page) {
  return (page.sessions || []).map((session) => {
    const rows = [
      ...session.added.map((row) => ["Added", row.text, "added"]),
      ...session.removed.map((row) => [`Removed: ${row.reason}`, row.text, "removed"]),
      ...session.verdicts.map((row) => [`Verdict ${row.from} → ${row.to}`, row.text, "verdict"]),
    ];
    const list = element("ul", "wiki-changes");
    for (const [label, text, kind] of rows) {
      const item = element("li", "change-" + kind);
      item.append(element("strong", "", label), element("span", "wiki-text", text));
      list.append(item);
    }
    const counts = session.counts || {};
    return wikiSection(`Session ${session.run_id || "?"} · ${relativeTime(session.built_at)}`, [
      element("p", "focus-hint", `${counts.added || 0} added, ${counts.removed || 0} removed, ${counts.verdicts || 0} verdicts changed${session.first ? " · first build" : ""}`),
      rows.length ? list : element("p", "focus-hint", "No changes."),
    ]);
  });
}

function wikiSessionsPage(page) {
  const list = element("ul", "wiki-changes");
  for (const row of page.runs || []) {
    const item = element("li", row.outcome === "completed" ? "change-added" : row.outcome.startsWith("halted") ? "change-verdict" : "");
    item.append(
      element("strong", "", `${row.run_id} · ${row.outcome}`),
      element("span", "wiki-text", `${row.done}/${row.tasks} done, ${row.rejected} rejected, ${row.blocked} blocked · ${wikiPlural(row.notes, "note")} recorded`),
    );
    list.append(item);
  }
  return [
    element("p", "", "Each recorded improve session, newest first: what it attempted and the knowledge it recorded."),
    page.runs?.length ? list : element("p", "focus-hint", "No session recorded yet."),
  ];
}

function wikiList(className, texts, empty) {
  if (!texts.length) return [element("p", "focus-hint", empty)];
  const list = element("ul", className);
  list.append(...texts.map((text) => element("li", "", text)));
  return [list];
}

function wikiDreamsPage(page) {
  const inferred = page.inferred || {};
  const proposals = element("ul", "wiki-changes");
  for (const row of page.proposals || []) {
    const item = element("li");
    item.append(element("strong", "", wikiPlural(row.members, "note")), element("span", "wiki-text", row.text || row.id));
    proposals.append(item);
  }
  return [
    element("p", "wiki-stamp", `Latest dream: tick ${page.tick}${page.at ? " · " + relativeTime(page.at) : ""}`),
    element("p", "", `Inferred links: ${inferred.new || 0} new, ${inferred.kept || 0} kept, ${inferred.dropped || 0} dropped. Knowledge archived ${page.archived || 0}, forgotten ${page.forgotten || 0}. ${page.proven || 0} proven, ${page.contested || 0} contested.`),
    wikiSection("Contradictions", wikiContradictions(page.contradictions || [])),
    wikiSection("Consolidation proposals", [page.proposals?.length ? proposals : element("p", "focus-hint", "None.")]),
    wikiSection("Knowledge gaps", wikiList("wiki-gaps", page.gaps || [], "None.")),
  ];
}

// An area page carries note ids; the rows live on the knowledge page (and its `more` table).
function wikiAreaPage(page, pages) {
  const rows = new Map(wikiAllNotes(pages).map((row) => [row.id, row]));
  return [
    element("p", "", `${page.done || 0}/${page.attempts || 0} attempts verified${typeof page.success === "number" ? ` · success ${Math.round(page.success * 100)}%` : ""}`),
    wikiSection("Knowledge", [
      ...wikiCapped((page.knowledge || []).length, page.total),
      ...wikiNoteRows((page.knowledge || []).map((id) => rows.get(id)).filter(Boolean)),
    ]),
    wikiSection("Files", wikiList("wiki-files", (page.files || []).map((row) => `${row.path} · ${wikiPlural(row.attempts, "attempt")}`), "None recorded.")),
  ];
}

const WIKI_PAGE_RENDERERS = {
  index: wikiIndexPage,
  knowledge: wikiKnowledgePage,
  changes: wikiChangesPage,
  sessions: wikiSessionsPage,
  dreams: wikiDreamsPage,
};

function wikiPageContent(name, page, pages) {
  const render = WIKI_PAGE_RENDERERS[name];
  return [element("h3", "", page.title || name), ...(render ? render(page) : wikiAreaPage(page, pages))];
}

function bindWiki() {
  $("wiki-filter").addEventListener("input", () => {
    wikiFilter = $("wiki-filter").value.trim().toLowerCase().slice(0, 120);
    if (wikiData) renderWiki();
  });
  $("knowledge-graph-mode").addEventListener("click", () => setKnowledgeMode(false));
  $("knowledge-wiki-mode").addEventListener("click", () => setKnowledgeMode(true));
}
