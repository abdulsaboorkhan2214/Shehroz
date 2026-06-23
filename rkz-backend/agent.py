"""
RKZ Lead Agent v3.3 — agent.py
Async FastAPI server with SQLite dedup + in-memory queue.

v3.2 (fix):
  • Added _in_flight set to close the dedup race window. Between a lead
    being queued and the worker writing it to SQLite (~30s–several min),
    duplicate requests used to slip through. Now they're rejected at push.

Endpoints:
  POST /analyze       → social leads (queued)
  POST /enrich_maps   → Google Maps leads (queued)
  GET  /stats         → dashboard stats (includes queue size)
  GET  /recent        → recent leads
  GET  /queue         → queue depth + worker status
  GET  /health        → health check
"""

import asyncio
import httpx
import json
import os
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from groq import AsyncGroq

from db          import init_db, make_dedup_key, is_duplicate, save_lead, \
                        mark_sent_to_sheet, get_stats, get_recent_leads
from enrichment  import enrich_maps_lead

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_MODEL    = "llama-3.3-70b-versatile"
QUEUE_MAXSIZE = 500                 # backpressure — refuse new jobs past this

app = FastAPI(title="RKZ Lead Agent", version="3.3")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Queue + worker state ─────────────────────────────────────────────────────
_queue: asyncio.Queue | None = None
_worker_task: asyncio.Task | None = None
_http: httpx.AsyncClient | None = None
_groq: AsyncGroq | None = None
_worker_stats = {
    "processed_total":      0,
    "processed_success":    0,
    "processed_failed":     0,
    "currently_processing": None,
    "started_at":           None,
}

# ── In-flight dedup set ──────────────────────────────────────────────────────
# Tracks dedup keys that are queued or currently being processed.
# Closes the race window between "queued" and "saved to SQLite".
# Cleared per-job in the worker's finally block.
_in_flight: set[str] = set()


# ── Async Qwen call for social leads ─────────────────────────────────────────
async def analyze_with_qwen(post_text: str, poster_name: str,
                            profile_url: str, platform: str) -> dict:
    prompt = f"""You are a lead qualification expert working for Kevin Khoury, President of Direct Allied Agency (DAA) — a white-label web design and SEO company.

Analyze this social media post and return business lead data. Write all outreach in Kevin's voice: confident, direct, professional, warm — NOT salesy or generic.

Platform: {platform}
Poster Name: {poster_name}
Profile URL: {profile_url}
Post Text: {post_text}

IMPORTANT: If you don't know the owner's name, address them as "{poster_name}" or use "Hi there" — NEVER use placeholders like [Owner Name], [Name], or [OWNER].

Return ONLY a valid JSON object with these exact fields (no explanation, no markdown):
{{
  "business_name": "name of business or N/A",
  "owner_name_or_decision_maker": "decision maker name, use poster name if unknown",
  "need_summary": "1-2 sentence summary of what they need",
  "lead_score_1_10": <integer 1-10>,
  "comment_for_post": "short helpful comment to post publicly — friendly, not a pitch",
  "email_subject": "compelling personalized email subject line",
  "email_body": "3-4 sentence personalized outreach email from Kevin, mentions DAA's web design/SEO, soft CTA",
  "dm_message": "casual 2-3 sentence DM from Kevin",
  "website_contact_message": "3-4 sentence contact form message — professional",
  "notes_for_me": "internal notes: red flags, urgency, best contact method"
}}"""

    try:
        response = await _groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        raw = response.choices[0].message.content.strip()

        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                try:
                    return json.loads(part)
                except Exception:
                    continue

        return json.loads(raw.strip())

    except json.JSONDecodeError as e:
        print(f"[Agent] ❌ JSON parse error: {e}")
        return {}
    except Exception as e:
        print(f"[Agent] ❌ Groq error: {e}")
        return {}


# ── Async Sheet write ────────────────────────────────────────────────────────
async def write_to_sheet(sheet_url: str, payload: dict) -> bool:
    if not sheet_url or not sheet_url.startswith("http"):
        return False
    try:
        res = await _http.post(sheet_url, json=payload, timeout=15,
                               follow_redirects=True)
        ok = res.status_code < 400
        print(f"[Sheet] {'✅' if ok else '⚠'} Status {res.status_code}")
        return ok
    except Exception as e:
        print(f"[Sheet] ❌ Write failed: {e}")
        return False


# ── Job handlers — actual processing happens here, in the worker ─────────────
async def process_social_job(job: dict):
    platform    = job["platform"]
    post_text   = job["post_text"]
    poster_name = job["poster_name"]
    profile_url = job["profile_url"]
    sheet_url   = job["sheet_url"]
    timestamp   = job["timestamp"]
    dedup_key   = job["dedup_key"]

    print(f"[Worker] 🤖 Social: {poster_name} ({platform})")
    ai = await analyze_with_qwen(post_text, poster_name, profile_url, platform)

    # Social leads derive their ENTIRE value (score + outreach) from Qwen. If
    # Qwen returned nothing (Ollama down / parse failure), saving would write an
    # empty score-0 row to the sheet AND burn the dedup slot — so the lead could
    # never be regenerated once Ollama is back. Skip instead; nothing is saved,
    # so the lead stays retryable on the next scan.
    if not ai:
        print(f"[Worker] ⚠ Qwen returned nothing for {poster_name} — skipping (stays retryable)")
        return

    score = ai.get("lead_score_1_10", ai.get("leadScore", 0)) or 0

    enriched = {
        "source_post_platform":         platform,
        "platform":                     platform,
        "business_name":                ai.get("business_name",                poster_name),
        "owner_name_or_decision_maker": ai.get("owner_name_or_decision_maker", poster_name),
        "need_summary":                 ai.get("need_summary",                 post_text[:200]),
        "lead_score_1_10":              score,
        "comment_for_post":             ai.get("comment_for_post",             ""),
        "email_subject":                ai.get("email_subject",                ""),
        "email_body":                   ai.get("email_body",                   ""),
        "dm_message":                   ai.get("dm_message",                   ""),
        "website_contact_message":      ai.get("website_contact_message",      ""),
        "notes_for_me":                 ai.get("notes_for_me",                 ""),
        "profile_url":                  profile_url,
        "timestamp":                    timestamp,
    }

    save_lead({
        "dedup_key":     dedup_key,
        "platform":      platform,
        "business_name": enriched["business_name"],
        "owner_name":    enriched["owner_name_or_decision_maker"],
        "profile_url":   profile_url,
        "post_text":     post_text,
        "lead_score":    score,
        "ai_payload":    ai,
    })

    ok = await write_to_sheet(sheet_url, enriched)
    mark_sent_to_sheet(dedup_key, ok)
    print(f"[Worker] ✅ Social done — score: {score} | sheet: {'✓' if ok else '✗'}")


async def process_maps_job(job: dict):
    business_name = job["business_name"]
    website       = job["website"]
    category      = job["category"]
    address       = job["address"]
    profile_url   = job["profile_url"]
    sheet_url     = job["sheet_url"]
    dedup_key     = job["dedup_key"]

    print(f"[Worker] 🗺 Maps: {business_name} [{category}]")

    enriched = await enrich_maps_lead(
        business_name=business_name,
        website=website,
        category=category,
        address=address,
        profile_url=profile_url,
    )

    save_lead({
        "dedup_key":         dedup_key,
        "platform":          "Google Maps",
        "business_name":     business_name,
        "owner_name":        enriched.get("owner_name", ""),
        "profile_url":       profile_url,
        "website":           website,
        "category":          category,
        "address":           address,
        "post_text":         "",
        "lead_score":        enriched.get("lead_score_1_10", 0),
        "signals":           enriched.get("signals", []),
        "ai_payload":        enriched.get("ai_payload", {}),
        "disqualified":      enriched.get("disqualified", False),
        "disqualify_reason": enriched.get("disqualify_reason", ""),
    })

    if not enriched.get("disqualified"):
        ok = await write_to_sheet(sheet_url, enriched)
        mark_sent_to_sheet(dedup_key, ok)
        print(f"[Worker] ✅ Maps done — score: {enriched.get('lead_score_1_10')} | sheet: {'✓' if ok else '✗'}")
    else:
        print(f"[Worker] 🚫 Maps skipped: {enriched.get('disqualify_reason')}")


# ── The single worker loop — processes one job at a time ────────────────────
async def worker_loop():
    print("[Worker] 🚀 Started — processing 1 lead at a time")
    _worker_stats["started_at"] = datetime.now().isoformat()
    while True:
        try:
            job = await _queue.get()
        except asyncio.CancelledError:
            print("[Worker] 🛑 Shutting down")
            return

        _worker_stats["currently_processing"] = job.get("display_name", "?")
        try:
            if job["type"] == "social":
                await process_social_job(job)
            elif job["type"] == "maps":
                await process_maps_job(job)
            else:
                print(f"[Worker] ❌ Unknown job type: {job['type']}")
            _worker_stats["processed_success"] += 1
        except Exception as e:
            print(f"[Worker] ❌ Job failed: {e}")
            _worker_stats["processed_failed"] += 1
        finally:
            _worker_stats["processed_total"]      += 1
            _worker_stats["currently_processing"] = None
            # v3.2: release the in-flight dedup slot now that the lead is
            # safely in SQLite (or failed — either way, it's no longer queued).
            _in_flight.discard(job.get("dedup_key", ""))
            _queue.task_done()


# ── Startup / shutdown ───────────────────────────────────────────────────────
@app.on_event("startup")
async def _startup():
    global _http, _queue, _worker_task, _groq
    init_db()
    _http = httpx.AsyncClient(timeout=300)
    _groq = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])
    _queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    _worker_task = asyncio.create_task(worker_loop())
    print("[Agent] ✅ v3.3 ready — queue + worker + race-safe dedup")


@app.on_event("shutdown")
async def _shutdown():
    global _http, _worker_task
    if _worker_task:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
    if _http:
        await _http.aclose()


# ── /analyze — Social leads (queued) ─────────────────────────────────────────
@app.post("/analyze")
@app.post("/lead")
async def analyze_lead(request: Request):
    data        = await request.json()
    platform    = data.get("platform",    data.get("source_post_platform", "Unknown"))
    post_text   = data.get("text",        data.get("postText", ""))
    poster_name = data.get("posterName",  "Unknown")
    profile_url = data.get("profile_url", data.get("profileUrl", ""))
    sheet_url   = data.get("sheetUrl",    "")
    timestamp   = data.get("timestamp",   datetime.now().isoformat())

    dedup_key = make_dedup_key(platform, profile_url, poster_name, post_text)

    # v3.2: check BOTH the persistent DB and the in-flight set.
    # The DB catches re-runs across sessions; _in_flight catches the race.
    if is_duplicate(dedup_key) or dedup_key in _in_flight:
        return {"duplicate": True, "dedup_key": dedup_key}

    if _queue.full():
        return {"queued": False, "reason": "Queue full — try again in a minute"}

    _in_flight.add(dedup_key)
    await _queue.put({
        "type":         "social",
        "platform":     platform,
        "post_text":    post_text,
        "poster_name":  poster_name,
        "profile_url":  profile_url,
        "sheet_url":    sheet_url,
        "timestamp":    timestamp,
        "dedup_key":    dedup_key,
        "display_name": poster_name,
    })
    print(f"[Agent] 📥 Queued social: {poster_name} (queue size: {_queue.qsize()})")
    return {"queued": True, "queue_size": _queue.qsize(), "dedup_key": dedup_key}


# ── /enrich_maps — Google Maps (queued) ──────────────────────────────────────
@app.post("/enrich_maps")
async def enrich_maps(request: Request):
    data          = await request.json()
    business_name = data.get("businessName", "Unknown")
    website       = data.get("website",      "")
    category      = data.get("category",     "")
    address       = data.get("address",      "")
    profile_url   = data.get("profileUrl",   "")
    sheet_url     = data.get("sheetUrl",     "")

    dedup_key = make_dedup_key("Google Maps", profile_url, business_name)

    # v3.2: check BOTH the persistent DB and the in-flight set.
    if is_duplicate(dedup_key) or dedup_key in _in_flight:
        return {"duplicate": True, "dedup_key": dedup_key}

    if _queue.full():
        return {"queued": False, "reason": "Queue full — try again in a minute"}

    _in_flight.add(dedup_key)
    await _queue.put({
        "type":          "maps",
        "business_name": business_name,
        "website":       website,
        "category":      category,
        "address":       address,
        "profile_url":   profile_url,
        "sheet_url":     sheet_url,
        "dedup_key":     dedup_key,
        "display_name":  business_name,
    })
    print(f"[Agent] 📥 Queued maps: {business_name} (queue size: {_queue.qsize()})")
    return {"queued": True, "queue_size": _queue.qsize(), "dedup_key": dedup_key}


# ── Queue status ─────────────────────────────────────────────────────────────
@app.get("/queue")
def queue_status():
    return {
        "queue_size":   _queue.qsize()       if _queue else 0,
        "queue_max":    QUEUE_MAXSIZE,
        "in_flight":    len(_in_flight),
        "worker_alive": (_worker_task is not None and not _worker_task.done()),
        **_worker_stats,
    }


# ── Stats / Dashboard ────────────────────────────────────────────────────────
@app.get("/stats")
def stats():
    s = get_stats()
    s["queue_size"] = _queue.qsize() if _queue else 0
    return s


@app.get("/recent")
def recent(limit: int = 20):
    return {"leads": get_recent_leads(limit)}


@app.get("/")
@app.get("/health")
def health():
    return {
        "status":  "RKZ Lead Agent v3.3 running",
        "model":   GROQ_MODEL,
        "port":    8000,
        "version": "3.3",
        "queue":   _queue.qsize() if _queue else 0,
    }