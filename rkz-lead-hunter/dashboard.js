const platformKeys = ["linkedin", "facebook", "reddit", "instagram", "maps"];

// ── Industry Presets ───────────────────────────────────────────────────
const INDUSTRY_PRESETS = {
  "🏠 Real Estate":     ["looking for house", "need realtor", "selling home", "buying property", "mortgage help", "first time buyer", "investment property", "rental property"],
  "💻 SaaS / Tech":     ["need software", "looking for tool", "CRM recommendation", "automate workflow", "project management", "team collaboration", "API integration", "tech stack"],
  "🏥 Healthcare":      ["looking for doctor", "need specialist", "mental health", "physical therapy", "dental work", "medical billing", "telehealth", "insurance coverage"],
  "📈 Marketing":       ["need more leads", "grow my business", "social media help", "SEO strategy", "content marketing", "email campaign", "paid ads", "brand awareness"],
  "⚖️ Legal":           ["need lawyer", "legal advice", "contract review", "business formation", "personal injury", "employment dispute", "IP protection", "compliance help"],
  "🎓 Education":       ["online course", "looking for tutor", "learn skills", "certification", "career change", "coaching program", "e-learning", "training program"],
  "🏗️ Construction":    ["need contractor", "home renovation", "roof repair", "plumbing issue", "electrical work", "landscaping", "interior design", "permit help"],
  "💰 Finance":         ["financial advisor", "investment help", "tax planning", "bookkeeping", "accounting software", "business loan", "retirement planning", "wealth management"],
  "🛒 E-commerce":      ["dropshipping", "online store", "product sourcing", "Shopify help", "Amazon FBA", "inventory management", "fulfillment", "payment gateway"],
  "🎨 Design / Agency": ["need designer", "logo design", "website redesign", "branding help", "UI/UX", "graphic designer", "creative agency", "web developer"],
};

// ── Tab switching ──────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});

// ── Keywords logic ─────────────────────────────────────────────────────
let currentKeywords = [];

function renderKeywords() {
  const container  = document.getElementById('kwTags');
  const empty      = document.getElementById('kwEmpty');
  const countBadge = document.getElementById('kwCount');

  countBadge.textContent = currentKeywords.length;

  if (!currentKeywords.length) {
    container.innerHTML = '';
    container.appendChild(empty);
    empty.style.display = 'inline';
    return;
  }

  empty.style.display = 'none';
  container.innerHTML = currentKeywords.map((kw, i) => `
    <span class="kw-tag">
      ${escHtml(kw)}
      <button class="kw-tag-remove" data-index="${i}" title="Remove">x</button>
    </span>
  `).join('');

  container.querySelectorAll('.kw-tag-remove').forEach(btn => {
    btn.addEventListener('click', () => {
      currentKeywords.splice(parseInt(btn.dataset.index), 1);
      saveKeywords();
      renderKeywords();
    });
  });
}

function saveKeywords() {
  chrome.storage.sync.set({ rkzKeywords: currentKeywords }, () => {
    const toast = document.getElementById('savedToast');
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 2000);
  });
}

function addKeyword(kw) {
  kw = kw.trim().toLowerCase();
  if (!kw || currentKeywords.includes(kw)) return;
  currentKeywords.push(kw);
  saveKeywords();
  renderKeywords();
}

document.getElementById('kwAddBtn').addEventListener('click', () => {
  const input = document.getElementById('kwInput');
  addKeyword(input.value);
  input.value = '';
  input.focus();
});

document.getElementById('kwInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    addKeyword(e.target.value);
    e.target.value = '';
  }
});

document.getElementById('kwClearAll').addEventListener('click', () => {
  if (!currentKeywords.length) return;
  if (!confirm('Clear all keywords?')) return;
  currentKeywords = [];
  saveKeywords();
  renderKeywords();
});

// Build preset buttons
const presetsContainer = document.getElementById('industryPresets');
Object.entries(INDUSTRY_PRESETS).forEach(([label, keywords]) => {
  const btn = document.createElement('button');
  btn.className = 'preset-btn';
  btn.textContent = label;
  btn.addEventListener('click', () => {
    if (currentKeywords.length && !confirm('Replace current keywords with ' + label + ' preset?')) return;
    currentKeywords = [...keywords];
    saveKeywords();
    renderKeywords();
    document.querySelector('[data-tab="keywords"]').click();
  });
  presetsContainer.appendChild(btn);
});

chrome.storage.sync.get(['rkzKeywords'], data => {
  currentKeywords = data.rkzKeywords || [];
  renderKeywords();
});

// ── Helper ─────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

// ── Load stats from chrome.storage.sync (same data source as the popup) ──
function loadFromStorage() {
  chrome.storage.sync.get(["stats"], function(data) {
    var stats      = data.stats || { total:0, scores:[], recent:[], platforms:{linkedin:0,facebook:0,reddit:0,instagram:0,maps:0} };
    var scores     = (stats.scores || []).filter(function(s){ return s > 0; });
    var total      = stats.total || 0;
    var platCounts = stats.platforms || {linkedin:0,facebook:0,reddit:0,instagram:0,maps:0};
    var hotCount   = (stats.recent  || []).filter(function(r){ return (r.score||0) >= 8; }).length;

    document.getElementById("totalLeads").textContent = total;
    document.getElementById("hotLeads").textContent   = hotCount;

    if (scores.length) {
      var avg = scores.reduce(function(a,b){ return a+b; }, 0) / scores.length;
      document.getElementById("avgScore").textContent = avg.toFixed(1);
      document.getElementById("topScore").textContent = Math.max.apply(null, scores);
    } else {
      document.getElementById("avgScore").textContent = "—";
      document.getElementById("topScore").textContent = "—";
    }

    var maxCount = Math.max.apply(null, Object.values(platCounts).concat([1]));
    platformKeys.forEach(function(key) {
      var bar = document.getElementById("bar-" + key);
      var cnt = document.getElementById("cnt-" + key);
      var val = platCounts[key] || 0;
      if (bar) bar.style.width = Math.round((val / maxCount) * 100) + "%";
      if (cnt) cnt.textContent = val;
    });
  });
}

// ── Sheet fetch ────────────────────────────────────────────────────────
function loadFromSheet(webhookUrl) {
  var container = document.getElementById("leadsContainer");
  container.innerHTML = '<div class="empty"><div class="empty-icon">⏳</div>Loading leads from sheet...</div>';

  fetch(webhookUrl)
    .then(function(res){ return res.json(); })
    .then(function(data) {
      var leads = data.leads || [];
      if (!leads.length) {
        container.innerHTML = '<div class="empty"><div class="empty-icon">📭</div>No leads in sheet yet</div>';
        return;
      }
      renderTable(leads);
      document.getElementById("refreshNote").textContent = "last refreshed " + new Date().toLocaleTimeString();
    })
    .catch(function(err) {
      container.innerHTML = '<div class="empty"><div class="empty-icon">⚠️</div>Could not reach sheet.<br><small style="color:var(--muted)">Check your webhook URL in the extension popup.</small></div>';
      console.error("[RKZ Dashboard] Sheet fetch error:", err);
    });
}

// ── Leads Table ────────────────────────────────────────────────────────
function renderTable(leads) {
  var container = document.getElementById("leadsContainer");
  var sorted = leads.slice().reverse();
  var crmColors = { "New":"#4285f4","Contacted":"#ff9800","Replied":"#9c27b0","Meeting":"#00bcd4","Closed":"#4caf50","Lost":"#f44336" };

  var rows = sorted.map(function(lead) {
    var score = parseInt(lead.leadScore) || 0;
    var sc    = score >= 8 ? "score-high" : score >= 5 ? "score-mid" : "score-low";
    var ts    = lead.timestamp ? new Date(lead.timestamp).toLocaleString([], { month:"short", day:"numeric", hour:"2-digit", minute:"2-digit" }) : "";
    var owner = (lead.ownerName && lead.ownerName !== "Unknown") ? lead.ownerName : "";
    var crmColor = crmColors[lead.crmStatus] || "#666";
    var crmBadge = '<span style="font-size:10px;padding:2px 6px;border-radius:3px;background:' + crmColor + ';color:#fff;font-weight:600">' + (lead.crmStatus || "New") + '</span>';

    return '<tr>' +
      '<td><span class="score-badge ' + sc + '">' + score + '/10</span></td>' +
      '<td><span class="platform-badge">' + escHtml((lead.platform || "?").replace(" Groups","")) + '</span></td>' +
      '<td>' +
        '<div class="lead-text-cell">' + escHtml(lead.businessName || "") + '</div>' +
        '<div class="lead-name">👤 ' + escHtml(owner) + '</div>' +
        '<div class="lead-name" style="margin-top:3px;color:var(--label);font-size:11px">' + escHtml((lead.needSummary || "").substring(0,80)) + '</div>' +
      '</td>' +
      '<td>' + crmBadge + '</td>' +
      '<td style="color:var(--muted);font-size:11px;white-space:nowrap">' + ts + '</td>' +
      '</tr>';
  }).join("");

  container.innerHTML = '<table class="leads-table"><thead><tr><th>Score</th><th>Platform</th><th>Lead</th><th>Status</th><th>Time</th></tr></thead><tbody>' + rows + '</tbody></table>';
}

// ── Boot ───────────────────────────────────────────────────────────────
loadFromStorage();
setInterval(loadFromStorage, 15000);
chrome.storage.onChanged.addListener(function(changes, area) {
  if (area === "sync" && changes.stats) loadFromStorage();
});

chrome.storage.sync.get(["webhook"], function(data) {
  var url = data.webhook || "";
  if (url) {
    loadFromSheet(url);
    document.getElementById("refreshBtn").addEventListener("click", function() {
      loadFromSheet(url);
    });
  } else {
    document.getElementById("leadsContainer").innerHTML =
      '<div class="empty"><div class="empty-icon">🔗</div>No webhook URL saved yet.<br>' +
      '<small style="color:var(--muted)">Paste your Apps Script URL in the extension popup.</small></div>';
  }
});

document.getElementById("clearBtn").addEventListener("click", function() {
  if (!confirm("Clear local extension stats? Sheet data is NOT affected.")) return;
  chrome.storage.sync.set({
    stats: { total:0, scores:[], platforms:{linkedin:0,facebook:0,reddit:0,instagram:0,maps:0}, recent:[] }
  }, function() {
    loadFromStorage();
    alert("Local stats cleared. Sheet data is safe.");
  });
});
