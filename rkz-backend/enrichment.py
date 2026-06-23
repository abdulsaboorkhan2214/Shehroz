"""
RKZ Enrichment — enrichment.py (v3.2)
Fully async. Signal-based scoring. Category whitelist for Maps.

v3.2 fixes:
  • Qwen prompt now explicitly forbids [Owner Name] placeholders when
    owner is unknown — addresses business by name or uses "Hi there".
  • Post-processing scrubs any [Owner Name]-style placeholders that
    leak through anyway (safety net).
  • Added 2 owner-name regex patterns for "founded by X" / "X, founder"
    construction common on home-services sites.

Pipeline:
  1. Category check  → reject non-contractor businesses early
  2. Scrape website  → extract owner, socials, signals
  3. Competitor check → reject web designers/devs
  4. Score signals   → no-website / no-https / outdated / mobile-broken
  5. Qwen outreach   → only for qualified leads
  6. Return enriched dict (caller writes to DB + Sheet)
"""

import httpx
import json
import os
import re
from bs4 import BeautifulSoup
from datetime import datetime
from groq import AsyncGroq

GROQ_MODEL   = "llama-3.3-70b-versatile"
_groq_client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY", ""))

# ── Target categories (home services DAA wants) ──────────────────────────────
TARGET_CATEGORIES = [
    "plumber", "plumbing", "hvac", "heating", "air conditioning", "cooling",
    "roofing", "roofer", "electrician", "electrical",
    "landscaper", "landscaping", "lawn", "tree service",
    "painter", "painting", "remodeling", "remodel", "renovation",
    "contractor", "construction", "handyman", "pest control",
    "carpet cleaning", "house cleaning", "cleaning service",
    "garage door", "fence", "fencing", "concrete", "paving",
    "pool service", "pool cleaning", "window cleaning", "gutter",
    "appliance repair", "locksmith", "moving", "movers",
    "junk removal", "pressure washing", "chimney", "siding",
    "septic", "well drilling", "solar", "insulation",
    "auto repair", "mechanic", "towing", "auto detailing",
]

# ── Competitor disqualifiers (we don't pitch web designers) ──────────────────
COMPETITOR_KEYWORDS = [
    "web design", "web designer", "web development", "web developer",
    "website design", "website developer", "we build websites",
    "wordpress developer", "shopify developer", "custom websites",
    "web agency", "digital agency that builds", "we create websites",
    "website solutions", "web solutions", "front-end developer",
    "full stack developer", "ux/ui designer", "ui/ux design",
    "seo agency", "marketing agency",
]

VALID_LEAD_KEYWORDS = [
    "plumber", "hvac", "roofer", "electrician", "contractor",
]

# ── Placeholder patterns to scrub from Qwen output ───────────────────────────
# Qwen sometimes ignores instructions and emits these even when told not to.
PLACEHOLDER_PATTERNS = [
    r"\[Owner Name\]",   r"\[Owner\]",      r"\[OWNER NAME\]",  r"\[OWNER\]",
    r"\[Name\]",         r"\[NAME\]",       r"\[Your Name\]",
    r"\[Decision Maker\]", r"\[Business Owner\]",
    r"\[Contact Name\]", r"\[Recipient\]",
]
_PLACEHOLDER_RE = re.compile("|".join(PLACEHOLDER_PATTERNS), re.IGNORECASE)


# ── Category check ────────────────────────────────────────────────────────────
def is_target_category(category: str, business_name: str = "") -> bool:
    """Return True if this is a home-services business we want to pitch."""
    haystack = f"{category} {business_name}".lower()
    return any(cat in haystack for cat in TARGET_CATEGORIES)


def is_competitor(text: str) -> bool:
    """Return True if the website suggests they build websites (skip lead)."""
    text_lower = text.lower()
    for kw in VALID_LEAD_KEYWORDS:
        if kw in text_lower:
            return False
    for kw in COMPETITOR_KEYWORDS:
        if kw in text_lower:
            return True
    return False


# ── Owner + socials extraction ───────────────────────────────────────────────
def extract_owner_and_socials(soup: BeautifulSoup) -> dict:
    owner_name = ""
    socials = {"linkedin": "", "facebook": "", "instagram": "", "twitter": "", "youtube": ""}

    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        if "linkedin.com"  in href and not socials["linkedin"]:  socials["linkedin"]  = a["href"]
        elif "facebook.com" in href and not socials["facebook"]: socials["facebook"]  = a["href"]
        elif "instagram.com" in href and not socials["instagram"]: socials["instagram"] = a["href"]
        elif ("twitter.com" in href or "x.com" in href) and not socials["twitter"]: socials["twitter"] = a["href"]
        elif "youtube.com" in href and not socials["youtube"]:   socials["youtube"]   = a["href"]

    # ── Owner-name extraction DISABLED (v3.3) ────────────────────────────────
    # The regex approach could not distinguish a real owner name from any
    # capitalized two-word phrase on the page (street names, testimonial
    # authors, suppliers). Live failure: "President George Bush Turnpike" in an
    # address matched the President|...|([A-Z][a-z]+ [A-Z][a-z]+) pattern and
    # produced owner_name="George Bush" for Dean's Plumbing. Hit rate on real
    # home-services sites was ~0/10, so the feature only generated false names.
    # owner_name stays "" — outreach falls back to the business-name / "Hi there"
    # path, which scrub_placeholders() and generate_outreach() already handle.

    return {"owner_name": "", "socials": socials}


# ── Signal extraction (the heart of qualification) ───────────────────────────
def extract_signals(html: str, url: str, resp_headers: dict) -> dict:
    """
    Extract qualification signals from a website. Higher score = better lead.

    Signals (each adds to score):
      - no_https            : +3  (huge tell — outdated site)
      - outdated_copyright  : +2  (footer year > 3 years old)
      - no_viewport_meta    : +2  (mobile-broken)
      - no_ssl_or_404       : +3  (site barely functional)
      - tiny_page           : +1  (under 1KB of content)
      - no_og_tags          : +1  (no SEO meta tags)
    """
    signals = []
    score_bonus = 0

    if not url.lower().startswith("https://"):
        signals.append("no_https")
        score_bonus += 3

    soup = BeautifulSoup(html, "html.parser")

    # Mobile-broken check
    viewport = soup.find("meta", attrs={"name": "viewport"})
    if not viewport:
        signals.append("no_viewport_meta")
        score_bonus += 2

    # Copyright year check
    text = soup.get_text(" ", strip=True)
    years = re.findall(r"©\s*(\d{4})|copyright\s*(\d{4})", text, re.IGNORECASE)
    flat_years = [int(y) for pair in years for y in pair if y]
    current_year = datetime.now().year
    if flat_years and (current_year - max(flat_years)) > 3:
        signals.append(f"outdated_copyright_{max(flat_years)}")
        score_bonus += 2

    # SEO meta tags
    og_tags = soup.find_all("meta", attrs={"property": re.compile(r"^og:")})
    if len(og_tags) < 2:
        signals.append("no_og_tags")
        score_bonus += 1

    # Tiny page
    if len(text) < 500:
        signals.append("tiny_page")
        score_bonus += 1

    return {"signals": signals, "score_bonus": score_bonus}


# ── Website scraper (fully async) ────────────────────────────────────────────
# ── Bot-challenge / CAPTCHA wall detection ───────────────────────────────────
# A 200 OK can still be a Cloudflare/Altcha/hCaptcha interstitial, not the real
# site. Those bodies are tiny and were being scored as "outdated / tiny real
# site" (~82 chars → tiny_page). They must be treated as unreadable, not as a
# real page and not as "no website".
CHALLENGE_MARKERS = [
    "just a moment", "checking your browser", "verifying you are human",
    "verify you are human", "enable javascript and cookies", "cf-challenge",
    "challenge-platform", "/cdn-cgi/challenge", "altcha", "hcaptcha",
    "recaptcha", "attention required", "ddos protection by", "ray id",
]


def looks_like_challenge(html: str, text: str) -> bool:
    """True if the fetched page is a bot-check/CAPTCHA wall, not the real site."""
    low = html.lower()
    if any(m in low for m in CHALLENGE_MARKERS):
        return True
    # 200 with almost no readable text + a challenge-ish word = JS/challenge shell
    if len(text.strip()) < 120 and any(w in low for w in ("captcha", "challenge", "verify")):
        return True
    return False


async def scrape_website(url: str) -> dict:
    # website_status: "none" (no URL at all) | "unreachable" (URL present but we
    # couldn't read it) | "ok" (real page fetched). has_website kept as a derived
    # alias (== status == "ok") for any older callers.

    # No URL on the listing → genuinely no website.
    if not url:
        return {"text": "", "owner_name": "", "socials": {}, "signals": [],
                "score_bonus": 0, "error": "no_url",
                "website_status": "none", "has_website": False}

    if not url.startswith("http"):
        url = "https://" + url

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            }
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            html = resp.text
            final_url = str(resp.url)

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["nav", "footer", "script", "style", "noscript"]):
            tag.decompose()
        page_text = soup.get_text(separator=" ", strip=True)[:3000]

        # Bot wall → we never actually saw the site. Treat as unreachable.
        if looks_like_challenge(html, page_text):
            print(f"[Enrichment] 🤖 Bot-challenge wall at {final_url} — site not readable")
            return {
                "text": "", "owner_name": "", "socials": {},
                "signals": ["bot_challenge"], "score_bonus": 1,
                "website_status": "unreachable", "has_website": False,
                "error": "bot_challenge"
            }

        extracted = extract_owner_and_socials(soup)
        sig_data  = extract_signals(html, final_url, dict(resp.headers))

        return {
            "text":           page_text,
            "owner_name":     extracted["owner_name"],
            "socials":        extracted["socials"],
            "signals":        sig_data["signals"],
            "score_bonus":    sig_data["score_bonus"],
            "website_status": "ok",
            "has_website":    True,
            "error":          ""
        }

    except Exception as e:
        # URL existed but we couldn't load it (timeout, block, DNS, 4xx/5xx).
        # This is NOT "no website" — the site may be perfectly fine. Flag for
        # manual review, give only a small bonus, and never tell Qwen they
        # have no site.
        print(f"[Enrichment] ⚠ Unreachable {url}: {e}")
        return {
            "text": "", "owner_name": "", "socials": {},
            "signals": ["website_unreachable"], "score_bonus": 1,
            "website_status": "unreachable", "has_website": False,
            "error": str(e)
        }


# ── Placeholder scrubber (post-processing safety net) ────────────────────────
def scrub_placeholders(ai_output: dict, business_name: str, owner_name: str) -> dict:
    """
    Replace any [Owner Name]-style placeholders Qwen leaked into the output.
    If owner known: use owner. Otherwise: use business name as the addressee.
    Operates on text fields only; leaves scores/lists alone.
    """
    if not isinstance(ai_output, dict):
        return ai_output

    # What to substitute placeholders WITH
    replacement = owner_name.strip() if owner_name else f"{business_name} team"

    text_fields = [
        "need_summary", "email_subject", "email_body",
        "dm_message", "website_contact_message", "notes_for_me",
    ]

    for field in text_fields:
        val = ai_output.get(field)
        if isinstance(val, str) and "[" in val:
            ai_output[field] = _PLACEHOLDER_RE.sub(replacement, val)

    return ai_output


# ── Qwen outreach generation (async) ─────────────────────────────────────────
async def generate_outreach(business_name: str, category: str, address: str,
                            owner_name: str, website_text: str,
                            signals: list, website_status: str) -> dict:
    """Call Ollama/Qwen asynchronously to generate Kevin's outreach."""

    # v3.2: explicit instruction for unknown owner — no more [Owner Name] leaks.
    if owner_name:
        owner_line = f"Owner/decision-maker: {owner_name}"
        addressing_rule = f"Address the recipient as '{owner_name}' (e.g. 'Hi {owner_name.split()[0]},')."
    else:
        owner_line = "Owner name: UNKNOWN — do not invent one"
        addressing_rule = (
            f"You do NOT know the owner's name. DO NOT use placeholders like "
            f"[Owner Name], [Name], [OWNER], or any bracketed text. "
            f"Either address the business directly (e.g. 'Hi {business_name} team,') "
            f"or use a friendly opener ('Hi there,' / 'Hello,'). "
            f"Do not fabricate a name."
        )

    signal_line  = f"Qualification signals: {', '.join(signals)}" if signals else "No specific signals"

    # Website state controls what Kevin is allowed to claim. Critical trap: on an
    # unreachable / bot-walled fetch we must NOT assert "you have no website".
    if website_status == "none":
        website_status_line = "Has website: NO — confirmed, no site found for this business."
        website_line = "NO WEBSITE FOUND — no online presence; they need a site built."
        website_rule = "They have NO website. Offer to build their first one."
    elif website_status == "unreachable":
        website_status_line = "Has website: UNKNOWN — their site could not be loaded (timeout, block, or bot-check)."
        website_line = "Site could not be loaded — its existence and quality are unconfirmed."
        website_rule = ("Their website could NOT be loaded, so you do NOT know its state. "
                        "DO NOT say they have no website and DO NOT call it outdated. "
                        "At most, offer to review their current site or note it seemed hard to reach.")
    else:  # ok
        website_status_line = "Has website: YES — a live site was read (see excerpt + signals)."
        website_line = website_text[:800] if website_text else "(no readable text extracted)"
        website_rule = ("They HAVE a website. Reference only the specific weaknesses listed in the "
                        "signals above (e.g. outdated copyright, not mobile-friendly, weak SEO). "
                        "Do not claim they have no website.")

    prompt = f"""You are writing outreach messages for Kevin Khoury, President of Direct Allied Agency (DAA) — a white-label web design and SEO company based in Oklahoma. DAA builds websites and runs SEO for home-services contractors.

Business Details:
- Name: {business_name}
- Category: {category}
- Location: {address}
- {owner_line}
- {signal_line}
- {website_status_line}
- Website excerpt: {website_line}

ADDRESSING RULE: {addressing_rule}
WEBSITE RULE: {website_rule}

Write outreach in Kevin's voice: confident, direct, warm, genuine — NOT corporate or salesy. Only reference a website problem that is actually supported by the signals/website state above — never invent one.

Return ONLY valid JSON, no markdown, no explanation:
{{
  "need_summary": "1-2 sentences on what this business likely needs",
  "lead_score_1_10": <integer 1-10>,
  "email_subject": "personalized email subject line",
  "email_body": "3-4 sentence email from Kevin — references their business + signal, soft CTA",
  "dm_message": "2-3 sentence casual DM for social media",
  "website_contact_message": "3-4 sentence contact form message — professional, warm",
  "notes_for_me": "internal notes: urgency, best contact method, anything useful"
}}"""

    try:
        response = await _groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        raw = response.choices[0].message.content.strip()

        parsed = {}
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                try:
                    parsed = json.loads(part)
                    break
                except Exception:
                    continue
        else:
            parsed = json.loads(raw.strip())

        return scrub_placeholders(parsed, business_name, owner_name)

    except json.JSONDecodeError as e:
        print(f"[Enrichment] ❌ JSON parse error: {e}")
        return {}
    except Exception as e:
        print(f"[Enrichment] ❌ Groq error: {e}")
        return {}


# ── Main pipeline ────────────────────────────────────────────────────────────
async def enrich_maps_lead(
    business_name: str,
    website: str,
    category: str,
    address: str,
    profile_url: str,
) -> dict:
    """
    Full enrichment for a Google Maps lead. Returns enriched dict.
    Caller (agent.py) handles DB + Sheet writes.
    """
    base = {
        "source_post_platform": "Google Maps",
        "platform":              "Google Maps",
        "business_name":         business_name,
        "businessName":          business_name,   # legacy alias
        "category":              category,
        "address":               address,
        "profile_url":           profile_url,
        "website":               website,
        "timestamp":             datetime.now().isoformat(),
    }

    # ── Gate 1: category check ────────────────────────────────────────────
    if not is_target_category(category, business_name):
        print(f"[Enrichment] 🚫 SKIP (off-category): {business_name} [{category}]")
        return {
            **base,
            "disqualified":      True,
            "disqualify_reason": f"Off-category: {category}",
        }

    # ── Gate 2: scrape website ────────────────────────────────────────────
    print(f"[Enrichment] 🔍 Scraping: {website or 'NO WEBSITE'}")
    scraped = await scrape_website(website)

    # ── Gate 3: competitor check ──────────────────────────────────────────
    if scraped["text"] and is_competitor(scraped["text"]):
        print(f"[Enrichment] 🚫 SKIP (competitor): {business_name}")
        return {
            **base,
            "disqualified":      True,
            "disqualify_reason": "Competitor — builds websites",
        }

    # ── Score calculation: signal-driven ─────────────────────────────────
    signals     = scraped.get("signals", [])
    score_bonus = scraped.get("score_bonus", 0)
    status      = scraped.get("website_status", "ok")

    if status == "none":
        # Confirmed no website at all — strongest signal.
        signals.append("no_website_at_all")
        score_bonus += 5
    elif status == "unreachable":
        # Site exists but we couldn't read it (block / timeout / bot-wall).
        # NOT the same as having no website. Flag for a human; the small bonus
        # from scrape_website already reflects "maybe a problem, maybe not".
        signals.append("verify_manually")

    base_score   = 4  # category-matched leads start at 4
    signal_score = min(base_score + score_bonus, 10)

    # ── Gate 4: AI outreach (async) ───────────────────────────────────────
    owner_name = scraped.get("owner_name", "")
    socials    = scraped.get("socials", {})

    print(f"[Enrichment] 🤖 Qwen generating for: {business_name} (score: {signal_score}, status: {status}, signals: {signals})")
    ai = await generate_outreach(
        business_name=business_name,
        category=category,
        address=address,
        owner_name=owner_name,
        website_text=scraped["text"],
        signals=signals,
        website_status=status,
    )

    # Score is driven by observable signals only. Qwen used to FLOOR the score
    # via max(pre, qwen), which flattened almost everything to ~8 and killed
    # discrimination between clean and broken sites. Qwen's number is now kept
    # for visibility/comparison but does NOT set the lead score.
    qwen_suggested_score = ai.get("lead_score_1_10", 0) or 0
    final_score = signal_score

    return {
        **base,
        "owner_name_or_decision_maker": owner_name or "Unknown",
        "owner_name":                   owner_name,
        "need_summary":                 ai.get("need_summary",            ""),
        "lead_score_1_10":              final_score,
        "lead_score":                   final_score,
        "qwen_suggested_score":         qwen_suggested_score,
        "website_status":               status,
        "signals":                      signals,
        "comment_for_post":             "",
        "email_subject":                ai.get("email_subject",           ""),
        "email_body":                   ai.get("email_body",              ""),
        "dm_message":                   ai.get("dm_message",              ""),
        "website_contact_message":      ai.get("website_contact_message", ""),
        "notes_for_me":                 ai.get("notes_for_me",            ""),
        "linkedin":                     socials.get("linkedin",  ""),
        "facebook":                     socials.get("facebook",  ""),
        "instagram":                    socials.get("instagram", ""),
        "ai_payload":                   ai,
        "disqualified":                 False,
    }