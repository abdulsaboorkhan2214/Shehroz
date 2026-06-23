document.addEventListener("DOMContentLoaded", () => {

  const agentUrlInput   = document.getElementById("agentUrl");
  const webhookInput    = document.getElementById("webhook");
  const autoScrollInput = document.getElementById("autoscroll");
  const saveBtn         = document.getElementById("save");
  const sendBtn         = document.getElementById("send");
  const stopBtn         = document.getElementById("stopScroll");
  const clearBtn        = document.getElementById("clearStats");
  const findGroupsBtn   = document.getElementById("findGroups");
  const statusEl        = document.getElementById("status");
  const groupStatusEl   = document.getElementById("groupStatus");
  const scrollDot       = document.getElementById("scrollDot");
  const scrollStatusEl  = document.getElementById("scrollStatus");

  const totalLeadsEl   = document.getElementById("totalLeads");
  const sessionLeadsEl = document.getElementById("sessionLeads");
  const avgScoreEl     = document.getElementById("avgScore");
  const topScoreEl     = document.getElementById("topScore");
  const recentListEl   = document.getElementById("recentList");

  const platformKeys = ["linkedin", "facebook", "reddit", "instagram", "maps"];

  const SUPPORTED = ["facebook.com","linkedin.com","instagram.com","reddit.com","google.com/maps"];
  function isSupportedUrl(url) { return SUPPORTED.some(s => url.includes(s)); }

  // ── Agent URL validation ───────────────────────────────────────────────
  // Rejects blanks, embedded spaces/arrows/newlines, and non-http(s) schemes.
  // A pasted "ngrok-url -> localhost:8000" now fails HERE (visibly, in the UI)
  // instead of being saved and dying silently inside fetch() in background.js.
  function isValidAgentUrl(s) {
    if (!s || /\s/.test(s)) return false;
    try {
      const u = new URL(s);
      return u.protocol === "http:" || u.protocol === "https:";
    } catch {
      return false;
    }
  }

  let sessionLeadCount = 0;
  let isScrolling = false;

  // ── Tab switching ──────────────────────────────────────────────────────
  document.querySelectorAll(".tab").forEach(tab => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
      document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
      tab.classList.add("active");
      document.getElementById("tab-" + tab.dataset.tab).classList.add("active");
    });
  });

  // ── Group keywords ─────────────────────────────────────────────────────
  const GROUP_KEYWORDS = {
    usa:       ["small business owners USA","entrepreneurs USA help","need a website USA","SEO help small business USA","digital marketing help USA"],
    uk:        ["small business owners UK","entrepreneurs UK help","need a website UK","SEO help UK small business","digital marketing UK help"],
    canada:    ["small business owners Canada","entrepreneurs Canada help","need a website Canada","SEO help Canada","digital marketing Canada"],
    australia: ["small business owners Australia","entrepreneurs Australia help","need a website Australia","SEO help Australia","digital marketing Australia"],
    singapore: ["small business Singapore","startup founders Singapore","digital marketing Singapore","SEO Singapore help","ecommerce Singapore help"],
    malaysia:  ["small business Malaysia","startup founders Malaysia","digital marketing Malaysia","SEO Malaysia help","website help Malaysia"]
  };
  let selectedRegion = "usa";

  // ── Load saved settings (config stays in sync, stats in local) ─────────
  chrome.storage.sync.get(["agentUrl", "webhook", "autoscroll"], data => {
    if (data.agentUrl)             agentUrlInput.value     = data.agentUrl;
    if (data.webhook)              webhookInput.value      = data.webhook;
    if (data.autoscroll !== undefined) autoScrollInput.checked = data.autoscroll;
  });

  chrome.storage.local.get(["stats"], data => {
    renderStats(data.stats || defaultStats());
  });

  // ── Live stats from backend ────────────────────────────────────────────
  async function fetchBackendStats() {
    const { agentUrl } = await chrome.storage.sync.get(["agentUrl"]);
    if (!agentUrl) return;
    try {
      const res = await fetch(agentUrl.replace(/\/$/, "") + "/stats", { method: "GET" });
      if (!res.ok) return;
      const backendStats = await res.json();
      // Backend stats are authoritative for totals; merge with local for recent + session
      const data = await chrome.storage.local.get(["stats"]);
      const local = data.stats || defaultStats();
      const merged = {
        ...local,
        total:    backendStats.total      || local.total,
        platforms: backendStats.by_platform
                   ? mergePlatformCounts(backendStats.by_platform)
                   : local.platforms
      };
      renderStats(merged);
    } catch (e) { /* backend offline — fall back to local */ }
  }

  function mergePlatformCounts(byPlatform) {
    const out = { linkedin: 0, facebook: 0, reddit: 0, instagram: 0, maps: 0 };
    for (const [k, v] of Object.entries(byPlatform)) {
      const key = k.toLowerCase().replace(" groups", "").replace("google maps", "maps");
      const match = platformKeys.find(p => key.includes(p)) || "facebook";
      out[match] = (out[match] || 0) + v;
    }
    return out;
  }

  // Refresh backend stats every 5s while popup is open
  fetchBackendStats();
  const statsTimer = setInterval(fetchBackendStats, 5000);
  window.addEventListener("unload", () => clearInterval(statsTimer));

  // ── Auto-save inputs ───────────────────────────────────────────────────
  let agentSaveTimer = null;
  agentUrlInput.addEventListener("input", () => {
    clearTimeout(agentSaveTimer);
    agentUrlInput.style.borderColor = "var(--muted)";
    agentSaveTimer = setTimeout(() => {
      const url = agentUrlInput.value.trim();
      if (url && !isValidAgentUrl(url)) {
        agentUrlInput.style.borderColor = "var(--red)";
        setStatus("❌ Invalid Agent URL — paste only the ngrok link, nothing else", "error");
        return;                                   // do NOT save garbage to storage
      }
      chrome.storage.sync.set({ agentUrl: url }, () => {
        if (chrome.runtime.lastError) return;
        clearStatus();
        agentUrlInput.style.borderColor = "var(--green)";
        setTimeout(() => { agentUrlInput.style.borderColor = ""; }, 1200);
      });
    }, 800);
  });

  let webhookSaveTimer = null;
  webhookInput.addEventListener("input", () => {
    clearTimeout(webhookSaveTimer);
    webhookInput.style.borderColor = "var(--muted)";
    webhookSaveTimer = setTimeout(() => {
      const url = webhookInput.value.trim();
      chrome.storage.sync.set({ webhook: url }, () => {
        if (chrome.runtime.lastError) return;
        webhookInput.style.borderColor = "var(--green)";
        setTimeout(() => { webhookInput.style.borderColor = ""; }, 1200);
      });
    }, 800);
  });

  function defaultStats() {
    return {
      total: 0, scores: [], recent: [],
      platforms: { linkedin: 0, facebook: 0, reddit: 0, instagram: 0, maps: 0 }
    };
  }

  function renderStats(stats) {
    totalLeadsEl.textContent   = stats.total || 0;
    sessionLeadsEl.textContent = sessionLeadCount;

    const scores = stats.scores || [];
    if (scores.length) {
      avgScoreEl.textContent = (scores.reduce((a,b) => a+b, 0) / scores.length).toFixed(1);
      topScoreEl.textContent = Math.max(...scores);
    } else {
      avgScoreEl.textContent = "—";
      topScoreEl.textContent = "—";
    }

    const platforms = stats.platforms || {};
    const maxCount = Math.max(...Object.values(platforms), 1);
    platformKeys.forEach(key => {
      const count = platforms[key] || 0;
      const pct = Math.round((count / maxCount) * 100);
      const bar = document.getElementById("bar-" + key);
      const cnt = document.getElementById("cnt-" + key);
      if (bar) bar.style.width = pct + "%";
      if (cnt) cnt.textContent = count;
    });

    const recent = (stats.recent || []).slice(0, 5);
    if (recent.length === 0) {
      recentListEl.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">📭</div>
          No leads yet — go scan a page
        </div>`;
    } else {
      recentListEl.innerHTML = recent.map(lead => {
        const scoreClass = lead.score >= 7 ? "score-high" : lead.score >= 5 ? "score-mid" : "score-low";
        return `
          <div class="lead-item">
            <div class="lead-top">
              <span class="lead-platform">${lead.platform || "?"}</span>
              <span class="lead-score ${scoreClass}">${lead.score}/10</span>
            </div>
            <div class="lead-text">${lead.text || ""}</div>
            <div class="lead-name">👤 ${lead.name || "Unknown"}</div>
          </div>`;
      }).join("");
    }
  }

  // ── Region selector ────────────────────────────────────────────────────
  document.querySelectorAll(".region-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".region-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      selectedRegion = btn.dataset.region;
    });
  });

  findGroupsBtn.addEventListener("click", () => {
    const keywords = GROUP_KEYWORDS[selectedRegion] || [];
    if (!keywords.length) return;
    groupStatusEl.textContent = "Opening searches...";
    groupStatusEl.style.color = "var(--green)";
    keywords.slice(0, 3).forEach((kw, i) => {
      setTimeout(() => {
        chrome.tabs.create({ url: `https://www.facebook.com/groups/search/?q=${encodeURIComponent(kw)}`, active: i === 0 });
      }, i * 800);
    });
    setTimeout(() => { groupStatusEl.textContent = `✓ Opened 3 searches for ${selectedRegion.toUpperCase()}`; }, 2500);
  });

  // ── Save ───────────────────────────────────────────────────────────────
  saveBtn.addEventListener("click", () => {
    const agentUrl = agentUrlInput.value.trim();
    if (agentUrl && !isValidAgentUrl(agentUrl)) {
      agentUrlInput.style.borderColor = "var(--red)";
      setStatus("❌ Invalid Agent URL — paste only the ngrok link, nothing else", "error");
      return;
    }
    chrome.storage.sync.set({
      agentUrl,
      webhook:    webhookInput.value.trim(),
      autoscroll: autoScrollInput.checked
    }, () => {
      if (chrome.runtime.lastError) { setStatus("❌ Save failed", "error"); return; }
      const orig = saveBtn.textContent;
      saveBtn.textContent = "✓ Saved!";
      setStatus("Settings saved", "success");
      setTimeout(() => { saveBtn.textContent = orig; clearStatus(); }, 1500);
    });
  });

  // ── Send / Extract ─────────────────────────────────────────────────────
  sendBtn.addEventListener("click", () => {
    setStatus("Scanning page...", "");
    chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
      const tab = tabs[0];
      if (!isSupportedUrl(tab.url || "")) {
        setStatus("❌ Not on a supported page", "error"); return;
      }
      chrome.scripting.executeScript(
        { target: { tabId: tab.id }, files: ["content.js"] },
        () => {
          setTimeout(() => {
            chrome.tabs.sendMessage(tab.id, { action: "extract" }, res => {
              if (chrome.runtime.lastError) {
                setStatus("❌ Could not scan — try refreshing", "error"); return;
              }
              if (res && res.count !== undefined) {
                sessionLeadCount += res.count;
                sessionLeadsEl.textContent = sessionLeadCount;
                if (res.count > 0) {
                  setStatus(`✅ Found ${res.count} lead(s)!${autoScrollInput.checked ? " Scrolling..." : ""}`, "success");
                } else {
                  setStatus("⏭ Scrolling to find leads...", "");
                }
                if (autoScrollInput.checked) {
                  chrome.tabs.sendMessage(tab.id, { action: "startScroll" }, () => setScrolling(true));
                }
                fetchBackendStats();
              }
            });
          }, 500);
        }
      );
    });
  });

  stopBtn.addEventListener("click", () => {
    chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
      chrome.tabs.sendMessage(tabs[0].id, { action: "stopScroll" }, () => {
        if (chrome.runtime.lastError) { setStatus("❌ Could not stop", "error"); return; }
        setScrolling(false);
        setStatus("⏹ Scroll stopped", "");
      });
    });
  });

  autoScrollInput.addEventListener("change", () => {
    if (!autoScrollInput.checked && isScrolling) {
      chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
        chrome.tabs.sendMessage(tabs[0].id, { action: "stopScroll" }, () => setScrolling(false));
      });
    }
  });

  document.getElementById("openDashboard").addEventListener("click", () => {
    chrome.tabs.create({ url: chrome.runtime.getURL("dashboard.html") });
  });

  clearBtn.addEventListener("click", () => {
    chrome.storage.local.set({ stats: defaultStats() }, () => {
      sessionLeadCount = 0;
      renderStats(defaultStats());
      setStatus("Local stats cleared (backend untouched)", "");
      setTimeout(clearStatus, 1800);
    });
  });

  // React to stat changes from background.js
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.stats) {
      renderStats(changes.stats.newValue || defaultStats());
    }
  });

  function setScrolling(active) {
    isScrolling = active;
    if (active) {
      scrollDot.classList.add("active");
      scrollStatusEl.textContent = "Auto-scroll: active";
      stopBtn.style.display = "block";
    } else {
      scrollDot.classList.remove("active");
      scrollStatusEl.textContent = "Auto-scroll: inactive";
      stopBtn.style.display = "none";
    }
  }

  function setStatus(msg, type) { statusEl.textContent = msg; statusEl.className = type; }
  function clearStatus() { statusEl.textContent = ""; statusEl.className = ""; }

});