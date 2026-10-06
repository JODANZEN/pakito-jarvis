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
