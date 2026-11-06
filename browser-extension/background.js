// Asks Pakito (running on this PC) whether you're locked in, and if so sends blocked sites to a "locked in" page.
// When Pakito isn't running, nothing is blocked.
const API = "http://127.0.0.1:8765";
let cache = { t: 0, state: { locked: false, sites: [] } };

async function getState() {
  if (Date.now() - cache.t < 4000) return cache.state;
  try {
    const r = await fetch(API + "/state");
    cache = { t: Date.now(), state: await r.json() };
  } catch (e) {
    cache = { t: Date.now(), state: { locked: false, sites: [] } };
  }
  return cache.state;
}

function matchSite(url, sites) {
  try {
    const host = new URL(url).hostname.replace(/^www\./, "");
    return sites.find((s) => host === s || host.endsWith("." + s)) || null;
  } catch (e) {
    return null;
  }
}

async function check(tabId, url) {
  if (!url || !url.startsWith("http")) return;
  const s = await getState();
  if (!s.locked) return;
  const site = matchSite(url, s.sites || []);
  if (!site) return;
  const page = chrome.runtime.getURL("blocked.html") +
    "?site=" + encodeURIComponent(site) + "&left=" + encodeURIComponent(s.remaining_min ?? "") +
    "&label=" + encodeURIComponent(s.label || "");
  chrome.tabs.update(tabId, { url: page });
  fetch(API + "/blocked", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ site }),
  }).catch(() => {});
}

chrome.webNavigation.onBeforeNavigate.addListener((d) => { if (d.frameId === 0) check(d.tabId, d.url); });
chrome.tabs.onUpdated.addListener((id, info) => { if (info.url) check(id, info.url); }); // YouTube-style in-page navigation

// When a lock-in starts, also catch tabs that were already open.
chrome.alarms.create("sweep", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(async () => {
  const s = await getState();
  if (!s.locked) return;
  for (const t of await chrome.tabs.query({})) check(t.id, t.url);
});

// ---------- Pakito sees your tabs (assistant/tabs.py) ----------
// Every few seconds: send Pakito the open tabs + results of what it asked, get back what to do next
// (switch to a tab, close tabs, open a link, read a page's text). Pakito holds the request open until it has
// something, so orders arrive at once. Nothing happens unless you ask Pakito.
let results = [];
let syncing = false;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function tabSnapshot() {
  const wins = await chrome.windows.getAll({});
  const focused = new Set(wins.filter((w) => w.focused).map((w) => w.id));
  const tabs = await chrome.tabs.query({});
  return tabs
    .filter((t) => t.url && !t.url.startsWith("chrome-extension://"))
    .map((t) => ({ id: t.id, windowId: t.windowId, title: t.title || "", url: t.url || "",
                   active: t.active, focused: focused.has(t.windowId) }));
}

async function run(cmd) {
  try {
    if (cmd.op === "activate") {
      const t = await chrome.tabs.update(cmd.tab, { active: true });
      await chrome.windows.update(t.windowId, { focused: true });
      return { ok: true };
    }
    if (cmd.op === "close") {
      await chrome.tabs.remove(cmd.tabs || [cmd.tab]);
      return { ok: true };
    }
    if (cmd.op === "open") {
      await chrome.tabs.create({ url: cmd.url });
      return { ok: true };
    }
    if (cmd.op === "read") {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId: cmd.tab },
        func: () => (document.body ? document.body.innerText : ""),
      });
      return { text: String((r && r.result) || "").slice(0, 60000) };
    }
    return { ok: true };
  } catch (e) {
    return { error: "The browser said: " + (e && e.message ? e.message : String(e)) };
  }
}

async function syncLoop() {
  if (syncing) return;
  syncing = true;
  while (true) {
    const sending = results;
    results = [];
    try {
      const body = JSON.stringify({ tabs: await tabSnapshot(), results: sending, wait: 3 });
      const r = await fetch(API + "/ext/sync", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Pakito-Ext": "1" }, body,
      });
      if (!r.ok) { await sleep(15000); continue; }       // an older Pakito without this: check again later
      const { cmds = [] } = await r.json();
      for (const c of cmds) results.push({ id: c.id, ...(await run(c)) });
    } catch (e) {
      results = sending.concat(results);                 // Pakito isn't running: keep them, try again
      await sleep(3000);
    }
  }
}

syncLoop();
chrome.runtime.onStartup.addListener(syncLoop);
chrome.runtime.onInstalled.addListener(syncLoop);
chrome.alarms.onAlarm.addListener(syncLoop);             // the 30-s "sweep" alarm also revives the loop
