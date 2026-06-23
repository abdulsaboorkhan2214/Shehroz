/* ────────────────────────────────────────────────────────────────
   RKZ Lead Hunter v3 — background.js
   Routing:
     • Google Maps  → POST /enrich_maps   (signals + Qwen)
     • Social leads → POST /analyze       (Qwen)
   • Dedup is enforced by the BACKEND (SQLite). The extension keeps
     a tiny local cache (last 200 keys) just to skip obvious repeats
     before they hit the wire.
   ──────────────────────────────────────────────────────────────── */

// ── MV3 keep-alive via chrome.alarms (replaces unreliable setInterval) ──
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("keepalive", { periodInMinutes: 0.5 });
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create("keepalive", { periodInMinutes: 0.5 });
});
chrome.alarms.onAlarm.addListener(() => { /* just wakes the SW */ });

// ── Shared fetch headers (ngrok-skip bypasses the free-tier interstitial) ──
const RKZ_HEADERS = {
  "Content-Type": "application/json",
  "ngrok-skip-browser-warning": "true"
};

// ── Lightweight local dedup cache (backend is source of truth) ──────────
const LOCAL_CACHE_KEY = "_rkz_local_dedup";
const LOCAL_CACHE_MAX = 200;

async function localDedupHas(key) {
  const data = await chrome.storage.local.get(LOCAL_CACHE_KEY);
  const cache = data[LOCAL_CACHE_KEY] || [];
  return cache.includes(key);
}
async function localDedupAdd(key) {
  const data = await chrome.storage.local.get(LOCAL_CACHE_KEY);
  let cache = data[LOCAL_CACHE_KEY] || [];
  if (cache.includes(key)) return;
  cache.push(key);
  if (cache.length > LOCAL_CACHE_MAX) cache = cache.slice(-LOCAL_CACHE_MAX);
  await chrome.storage.local.set({ [LOCAL_CACHE_KEY]: cache });
}

// Match the backend's make_dedup_key logic (without crypto — we just need cheap collision avoidance for local skips)
function makeDedupHint(platform, profileUrl, name, postText) {
  if (profileUrl && profileUrl.startsWith("http")) {
    return `${platform}|${profileUrl.toLowerCase().trim()}`;
  }
  return `${platform}|${(name || "").toLowerCase().trim()}|${(postText || "").substring(0, 40)}`;
}

// ── Stats: stored in chrome.storage.local (NOT sync — sync has 100KB cap) ──
async function updateLocalStats(platform, score, postText, posterName) {
  const data = await chrome.storage.local.get("stats");
  const stats = data.stats || {
    total: 0, scores: [], recent: [],
    platforms: { linkedin: 0, facebook: 0, reddit: 0, instagram: 0, maps: 0 }
  };
  stats.total = (stats.total || 0) + 1;
  stats.scores = [...(stats.scores || []), score || 0].slice(-100);

  const key = (platform || "").toLowerCase().replace(" groups", "").replace("google maps", "maps");
  const platformKeys = ["linkedin", "facebook", "reddit", "instagram", "maps"];
  const match = platformKeys.find(k => key.includes(k)) || "facebook";
  stats.platforms[match] = (stats.platforms[match] || 0) + 1;

  stats.recent = [{
    platform: (platform || "").replace(" Groups", ""),
    score:    score || 0,
    text:     (postText || "").substring(0, 100),
    name:     posterName || "Unknown",
    time:     Date.now()
  }, ...(stats.recent || [])].slice(0, 20);

  await chrome.storage.local.set({ stats });
}

// ── Main message handler ─────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action !== "sendLead") return false;

  (async () => {
    try {
    const platform   = msg.platform   || "Unknown";
    const postText   = (msg.postText  || "").trim();
    const posterName = msg.posterName || "Unknown";
    const profileUrl = msg.profileUrl || "";
    const isMaps     = platform.includes("Google Maps");

    // ── Social leads need real post text. Maps leads don't. ───────────
    if (!isMaps) {
      const EMPTY = ["", "no post text", "n/a", "none", "null", "undefined"];
      if (!postText || EMPTY.includes(postText.toLowerCase()) || postText.length < 30) {
        console.log("[RKZ] ⏭ Skipped empty/short social lead:", posterName);
        return;
      }
    }

    // ── Local dedup hint (backend is authoritative) ───────────────────
    const hint = makeDedupHint(platform, profileUrl, posterName, postText);
    if (await localDedupHas(hint)) {
      console.log("[RKZ] ⏭ Local cache hit:", posterName);
      return;
    }
    await localDedupAdd(hint);

    // ── Load settings ─────────────────────────────────────────────────
    const { agentUrl: rawAgent, webhook: rawWebhook } = await chrome.storage.sync.get(["agentUrl", "webhook"]);
    const agentUrl = (rawAgent  || "").trim().replace(/\/$/, "");
    const sheetUrl = (rawWebhook || "").trim();

    if (!agentUrl || !agentUrl.startsWith("http")) {
      console.warn("[RKZ] ⚠ No agent URL set — go to Controls and save Agent URL");
      return;
    }

    // ── Route 1: Google Maps → /enrich_maps ───────────────────────────
    if (isMaps) {
      const mapsPayload = {
        businessName: msg.posterName || "Unknown",
        website:      msg.website    || "",
        category:     msg.category   || "",
        address:      msg.address    || "",
        profileUrl:   profileUrl,
        sheetUrl:     sheetUrl
      };

      console.log("[RKZ] 🗺 /enrich_maps:", mapsPayload.businessName, "|", mapsPayload.category);

      try {
        const res = await fetch(agentUrl + "/enrich_maps", {
          method:  "POST",
          headers: RKZ_HEADERS,
          body:    JSON.stringify(mapsPayload)
        });
        const result = await res.json();

        if (result.duplicate) {
          console.log("[RKZ] ⏭ Backend says duplicate:", mapsPayload.businessName);
          return;
        }
        // v3.1: backend now queues. Real enrichment happens async on the server.
        if (result.queued === true) {
          console.log(`[RKZ] 📥 Queued (#${result.queue_size}): ${mapsPayload.businessName}`);
          return;
        }
        if (result.queued === false) {
          console.warn(`[RKZ] ⚠ Backend queue full — skipped: ${mapsPayload.businessName}`);
          return;
        }
        // Legacy synchronous response (older backend) — still handle it
        if (result.disqualified) {
          console.log(`[RKZ] 🚫 ${result.disqualify_reason}:`, mapsPayload.businessName);
          return;
        }
        const score = result.lead_score_1_10 || 0;
        console.log(`[RKZ] ✅ Maps enriched — score ${score}: ${mapsPayload.businessName}`);
        await updateLocalStats(platform, score, `${mapsPayload.category} - ${mapsPayload.address}`, mapsPayload.businessName);
      } catch (err) {
        console.warn("[RKZ] ⚠ Maps enrichment request failed:", err.message);
      }
      return;
    }

    // ── Route 2: Social → /analyze ────────────────────────────────────
    const payload = {
      platform,
      text:                 postText,
      source_post_platform: platform,
      profile_url:          profileUrl,
      posterName,
      quality:              msg.quality || 0,
      sheetUrl,                                       // backend writes to Sheet now
      timestamp:            new Date().toISOString()
    };

    console.log("[RKZ] 📤 /analyze:", posterName, "| pre-score:", payload.quality);

    try {
      const res = await fetch(agentUrl + "/analyze", {
        method:  "POST",
        headers: RKZ_HEADERS,
        body:    JSON.stringify(payload)
      });
      const aiData = await res.json();

      if (aiData.duplicate) {
        console.log("[RKZ] ⏭ Backend says duplicate:", posterName);
        return;
      }
      // v3.1: backend queues social leads now
      if (aiData.queued === true) {
        console.log(`[RKZ] 📥 Queued (#${aiData.queue_size}): ${posterName}`);
        return;
      }
      if (aiData.queued === false) {
        console.warn(`[RKZ] ⚠ Backend queue full — skipped: ${posterName}`);
        return;
      }

      // Legacy synchronous response
      const score = aiData.lead_score_1_10 || aiData.leadScore || payload.quality || 0;
      console.log(`[RKZ] ✅ /analyze done — Score: ${score}`);
      await updateLocalStats(platform, score, postText, posterName);
    } catch (err) {
      console.warn("[RKZ] ⚠ Agent unreachable:", err.message);
      // No fallback Sheet write — backend owns Sheet writes now.
      // The lead stays in local dedup cache only; user can re-trigger after agent comes back.
    }
    } catch (err) {
      // Any unexpected throw (storage, dedup, JSON) is logged as a warning so it
      // never becomes an uncaught rejection that lights the service-worker error badge.
      console.warn("[RKZ] ⚠ Unhandled in sendLead handler:", err && err.message);
    }
  })();

  return false;     // we don't use sendResponse async
});