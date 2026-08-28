"""
Football predictions bot — powered by The Odds API (the-odds-api.com)

Modes:
    python main.py sports         -> debug: list all available sport_key values (run this first!)
    python main.py fetch          -> pulls today's matches + odds for tracked leagues, asks AI to
                                      pick the most promising bet, saves data/matches_<date>.json
    python main.py post           -> posts the next unposted time-batch to Telegram
    python main.py check_results  -> checks finished matches (via /scores), grades the AI's pick,
                                      edits the original Telegram messages, updates stats
    python main.py stats daily    -> posts a stats summary for today
    python main.py stats weekly   -> posts a stats summary for the trailing 7 days
    python main.py stats monthly  -> posts a stats summary for the previous calendar month
    python main.py stats yearly   -> posts a stats summary for the previous calendar year

Environment variables (set as GitHub Actions secrets):
    ODDS_API_KEY         - API key from the-odds-api.com
    TELEGRAM_BOT_TOKEN   - Telegram bot token from @BotFather
    TELEGRAM_CHANNEL     - channel username, e.g. @footballgolplus or numeric chat id
    GROQ_API_KEY         - API key from console.groq.com
    TIMEZONE             - IANA tz name, default "Europe/Moscow"
    FORCE_BATCH          - optional override ("morning"/"day"/"evening") for manual testing
"""

import os
import random
import sys
import re
import json
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# NOTE: verify these against `python main.py sports` output before trusting them blindly.
SPORT_KEYS = {
    "soccer_epl": "АПЛ",
    "soccer_spain_la_liga": "Ла Лига",
    "soccer_italy_serie_a": "Серия А",
    "soccer_germany_bundesliga": "Бундеслига",
    "soccer_france_ligue_one": "Лига 1",
    "soccer_uefa_champs_league": "Лига Чемпионов",
    "soccer_uefa_champs_league_qualification": "Квалификация ЛЧ",
    "soccer_uefa_europa_league": "Лига Европы",
    "soccer_efl_champ": "Чемпионшип",
    "soccer_fa_cup": "Кубок Англии",
    "soccer_england_efl_cup": "Кубок английской лиги",
    "soccer_germany_bundesliga2": "Бундеслига 2",
    "soccer_usa_mls": "MLS",
}

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_REGIONS = "eu"
ODDS_MARKETS = "h2h,spreads,totals"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Moscow"))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
IMAGES_DIR = os.path.join(os.path.dirname(__file__), "images")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")  # auto-set by GitHub Actions
STATS_PATH = os.path.join(DATA_DIR, "stats.json")

BATCHES = [
    ("morning", 0, 15),
    ("day", 15, 19),
    ("evening", 19, 24),
]

STAKE_RUB = 1000
MIN_PICK_ODD = 1.35

MARKET_LABELS = {
    "h2h": "Исход матча",
    "spreads": "Фора",
    "totals": "Тотал матча",
}


def today_str():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def data_path(day_str):
    return os.path.join(DATA_DIR, f"matches_{day_str}.json")


def odds_api_get(endpoint, params=None):
    params = dict(params or {})
    params["apiKey"] = ODDS_API_KEY
    resp = requests.get(f"{ODDS_API_BASE}{endpoint}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Debug: list sport keys
# ---------------------------------------------------------------------------

def list_sports():
    data = odds_api_get("/sports", {"all": "true"})
    print(f"[debug] Total sports returned: {len(data)}")
    for s in data:
        if "soccer" in s.get("key", "").lower():
            print(f"key={s['key']:35s} title={s.get('title')}  active={s.get('active')}")


# ---------------------------------------------------------------------------
# Odds parsing
# ---------------------------------------------------------------------------

def build_odds_context(bookmakers, home, away, max_lines_per_market=8):
    """Builds a compact text block listing available markets/lines for the AI prompt.
    Scans ALL bookmakers (not just the first) so markets like spreads/totals aren't
    missed just because the first bookmaker in the list doesn't offer them for this
    match. Lines below MIN_PICK_ODD are dropped entirely so the AI never sees them.
    Capped at max_lines_per_market lines per market to keep the prompt compact — a
    huge prompt was causing Groq to run out of its token budget mid-response.
    """
    if not bookmakers:
        return "", {}

    market_lookup = {}
    seen_keys = set()
    parts = []
    # Order matters for readability: h2h first, then spreads, then totals.
    for key in ["h2h", "spreads", "totals"]:
        label = MARKET_LABELS[key]
        line_bits = []
        for bookmaker in bookmakers:
            market = next((m for m in bookmaker.get("markets", []) if m["key"] == key), None)
            if not market:
                continue
            for o in market.get("outcomes", []):
                if len(line_bits) >= max_lines_per_market:
                    break
                name = o["name"]
                price = o["price"]
                point = o.get("point")
                if price < MIN_PICK_ODD:
                    continue
                dedup_key = (name, point)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                if point is not None:
                    line_bits.append(f"{name} {point} ({price})")
                else:
                    line_bits.append(f"{name} ({price})")
                market_lookup.setdefault(key, []).append({
                    "name": name, "point": point, "price": price,
                })
            if line_bits:
                # Found this market from some bookmaker — no need to keep scanning
                # further bookmakers for it once we have a usable set of lines.
                break
        seen_keys = set()
        if line_bits:
            parts.append(f"{label}: {', '.join(line_bits)}")
    return "\n".join(parts), market_lookup


def find_odd_for_pick(market_lookup, market_key, selection, line, home, away):
    """Cross-checks the AI's chosen odd against the real data, falls back to a lookup if needed."""
    entries = market_lookup.get(market_key, [])
    if market_key == "h2h":
        target_name = {"home": home, "away": away, "draw": "Draw"}.get(selection)
        for e in entries:
            if e["name"] == target_name:
                return e["price"]
    elif market_key == "spreads":
        target_name = home if selection == "home" else away
        for e in entries:
            if e["name"] == target_name and e["point"] == line:
                return e["price"]
    elif market_key == "totals":
        target_name = "Over" if selection == "over" else "Under"
        for e in entries:
            if e["name"] == target_name and e["point"] == line:
                return e["price"]
    return None


# ---------------------------------------------------------------------------
# AI pick
# ---------------------------------------------------------------------------

def call_groq(prompt, retries=3):
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }
    for attempt in range(retries):
        resp = requests.post(GROQ_URL, headers=headers, json=body, timeout=30)
        if resp.status_code == 429 and attempt < retries - 1:
            wait_seconds = int(resp.headers.get("Retry-After", 8))
            print(f"[ai] Groq rate limited, retrying in {wait_seconds}s (attempt {attempt + 1}/{retries})")
            time.sleep(wait_seconds)
            continue
        if resp.status_code >= 400:
            print(f"[ai] Groq error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


def parse_json_block(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def build_ai_pick(home, away, league, odds_context):
    prompt = (
        f"Ты профессиональный аналитик по футбольным ставкам. Матч: {home} — {away}, "
        f"турнир «{league}».\n\n"
        f"Ниже перечислены ТОЛЬКО те рынки и линии, коэффициент которых уже не ниже "
        f"{MIN_PICK_ODD} (более низкие коэффициенты заранее убраны из списка):\n"
        f"{odds_context}\n\n"
        f"Выбери ОДИН наиболее вероятный ('проходимый') вариант ставки из этого списка.\n\n"
        f"Важно: если в рынке h2h (исход матча) остались только ничья и/или победа "
        f"аутсайдера (то есть явный фаворит был отфильтрован из-за слишком низкого "
        f"коэффициента), а рынки spreads (фора) и totals (тотал) в списке ОТСУТСТВУЮТ "
        f"или тоже не дают уверенного варианта — это означает, что для этого матча нет "
        f"статистически обоснованной ставки. В таком случае НЕ выбирай ничью или "
        f"аутсайдера просто чтобы формально что-то выбрать. Вместо этого ответь ровно "
        f"{{\"market\": null}} и больше ничего.\n\n"
        f"Если же среди рынков spreads или totals есть разумный вариант — выбирай его, "
        f"это почти всегда статистически надёжнее, чем ничья явного аутсайдера.\n\n"
        f"Не придумывай данные, используй только то, что дано выше.\n\n"
        f"Если выбор есть, ответь СТРОГО в виде JSON без какого-либо текста до или "
        f"после него, в формате:\n"
        f'{{"market": "h2h|spreads|totals", '
        f'"selection": "home|draw|away|over|under", "line": число или null, '
        f'"odd": число (скопируй точно из списка выше), '
        f'"reasoning": "1-2 предложения на русском с обоснованием"}}\n\n'
        f'Если варианта нет, ответь ровно {{"market": null}}.'
    )
    try:
        raw = call_groq(prompt)
    except (requests.exceptions.RequestException, KeyError, IndexError) as e:
        print(f"[ai] Groq request failed: {e}")
        return None

    parsed = parse_json_block(raw)
    if not parsed:
        print(f"[ai] Could not parse JSON from Groq response: {raw[:200]}")
        return None
    if parsed.get("market") is None:
        print("[ai] AI decided no confident pick exists for this match")
        return None
    required = {"market", "selection", "odd", "reasoning"}
    if not required.issubset(parsed.keys()) or parsed["market"] not in MARKET_LABELS:
        print(f"[ai] Invalid Groq response: {parsed}")
        return None
    parsed.setdefault("line", None)
    return parsed


def fallback_pick(market_lookup, home, away):
    """Simple match-winner pick from the best available h2h odds, respecting MIN_PICK_ODD.
    Never defaults to draw or the underdog just because they're the only thing left —
    that's a low-quality guess, not a real prediction. If home/away don't clear the
    threshold, we'd rather post nothing than a weak forced pick.
    """
    entries = market_lookup.get("h2h", [])
    if not entries:
        return None
    team_candidates = [
        e for e in entries
        if e["price"] >= MIN_PICK_ODD and e["name"] in (home, away)
    ]
    if not team_candidates:
        return None
    best = min(team_candidates, key=lambda e: e["price"])
    selection = "home" if best["name"] == home else "away"
    return {
        "market": "h2h",
        "selection": selection,
        "line": None,
        "odd": best["price"],
        "reasoning": "Прогноз по коэффициентам букмекера (ИИ-анализ временно недоступен).",
    }


# ---------------------------------------------------------------------------
# Fetch mode
# ---------------------------------------------------------------------------

def fetch_and_build():
    day_str = today_str()
    print(f"[fetch] Getting matches for {day_str}")

    matches = []
    for sport_key, league_name in SPORT_KEYS.items():
        try:
            events = odds_api_get(f"/sports/{sport_key}/odds", {
                "regions": ODDS_REGIONS,
                "markets": ODDS_MARKETS,
                "oddsFormat": "decimal",
            })
        except requests.exceptions.HTTPError as e:
            print(f"[fetch] Error fetching {sport_key}: {e}")
            continue

        print(f"[debug] {sport_key}: {len(events)} events")

        for ev in events:
            commence = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
            kickoff_local = commence.astimezone(TIMEZONE)
            if kickoff_local.strftime("%Y-%m-%d") != day_str:
                continue

            home, away = ev["home_team"], ev["away_team"]
            odds_context, market_lookup = build_odds_context(ev.get("bookmakers", []), home, away)

            match = {
                "event_id": ev["id"],
                "sport_key": sport_key,
                "league": league_name,
                "home": home,
                "away": away,
                "kickoff_local": kickoff_local.strftime("%Y-%m-%d %H:%M"),
                "kickoff_hour": kickoff_local.hour,
                "message_id": None,
                "result_checked": False,
                "correct": None,
                "pick": None,
            }

            if odds_context:
                pick = None
                if GROQ_API_KEY:
                    pick = build_ai_pick(home, away, league_name, odds_context)
                    if pick:
                        # Cross-check / correct the odd against real data where possible.
                        real_odd = find_odd_for_pick(
                            market_lookup, pick["market"], pick["selection"], pick.get("line"), home, away
                        )
                        if real_odd is not None:
                            pick["odd"] = real_odd
                        if pick["odd"] < MIN_PICK_ODD:
                            pick = None
                if not pick:
                    pick = fallback_pick(market_lookup, home, away)
                match["pick"] = pick

            matches.append(match)
            time.sleep(6)

    matches.sort(key=lambda m: m["kickoff_local"])

    payload = {
        "date": day_str,
        "generated_at": datetime.now(TIMEZONE).isoformat(),
        "posted_batches": [],
        "matches": matches,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(data_path(day_str), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[fetch] Saved {len(matches)} matches to {data_path(day_str)}")


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def current_batch_name():
    forced = os.environ.get("FORCE_BATCH")
    if forced:
        return forced
    hour = datetime.now(TIMEZONE).hour
    for name, start, end in BATCHES:
        if start <= hour < end:
            return name
    return BATCHES[-1][0]


def describe_pick(m):
    pick = m.get("pick")
    if not pick:
        return f"Нет вариантов с коэффициентом от {MIN_PICK_ODD}"
    label = MARKET_LABELS.get(pick["market"], pick["market"])
    if pick["market"] == "h2h":
        sel_text = {"home": m["home"], "draw": "Ничья", "away": m["away"]}.get(pick["selection"], pick["selection"])
        return f"{label}: {sel_text} (кф. {pick['odd']})"
    if pick["market"] == "spreads":
        team = m["home"] if pick["selection"] == "home" else m["away"]
        line = pick.get("line")
        sign = "+" if isinstance(line, (int, float)) and line > 0 else ""
        return f"{label}: {team} {sign}{line} (кф. {pick['odd']})"
    sel_text = "Больше" if pick["selection"] == "over" else "Меньше"
    return f"{label}: {sel_text} {pick.get('line')} (кф. {pick['odd']})"


def format_match_block(m):
    lines = [f"⚽ {m['league']}: {m['home']} — {m['away']}", f"🕒 {m['kickoff_local']} (мск)"]
    if m.get("pick"):
        lines.append(f"🎯 {describe_pick(m)}")
        lines.append(f"💬 {m['pick']['reasoning']}")
    else:
        lines.append(describe_pick(m))
    return "\n".join(lines)


def get_random_image_url():
    """Picks a random image from the images/ folder in the repo and returns a
    public raw.githubusercontent.com URL Telegram can fetch. Returns None if the
    folder is missing/empty or GITHUB_REPOSITORY isn't set (e.g. local testing).
    """
    if not GITHUB_REPOSITORY or not os.path.isdir(IMAGES_DIR):
        return None
    valid_ext = (".jpg", ".jpeg", ".png", ".webp")
    files = [f for f in os.listdir(IMAGES_DIR) if f.lower().endswith(valid_ext)]
    if not files:
        return None
    chosen = random.choice(files)
    return f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/images/{chosen}"


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHANNEL, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json().get("result", {}).get("message_id")


def send_telegram_photo(photo_url, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHANNEL, "photo": photo_url,
        "caption": caption, "parse_mode": "HTML",
    }, timeout=30)
    resp.raise_for_status()
    return resp.json().get("result", {}).get("message_id")


def edit_telegram_message(message_id, new_text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHANNEL, "message_id": message_id,
        "text": new_text, "parse_mode": "HTML",
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()


def edit_telegram_caption(message_id, new_caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageCaption"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHANNEL, "message_id": message_id,
        "caption": new_caption, "parse_mode": "HTML",
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Post mode
# ---------------------------------------------------------------------------

def post_batch():
    day_str = today_str()
    path = data_path(day_str)

    if not os.path.exists(path):
        print("[post] No data file for today yet, running fetch first")
        fetch_and_build()

    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    batch_name = current_batch_name()
    if batch_name in payload["posted_batches"]:
        print(f"[post] Batch '{batch_name}' already posted today, nothing to do")
        return

    start, end = next((s, e) for n, s, e in BATCHES if n == batch_name)
    batch_matches = [m for m in payload["matches"] if start <= m["kickoff_hour"] < end]

    if not batch_matches:
        print(f"[post] No matches in batch '{batch_name}', marking as posted")
        payload["posted_batches"].append(batch_name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return

    header = {
        "morning": "🌅 <b>Прогнозы на сегодня — утренняя подборка</b>",
        "day": "☀️ <b>Дневная подборка матчей</b>",
        "evening": "🌆 <b>Вечерние матчи</b>",
    }[batch_name]

    send_telegram_message(header)
    time.sleep(1)

    for m in batch_matches:
        text = format_match_block(m)
        image_url = get_random_image_url()
        if image_url:
            m["message_id"] = send_telegram_photo(image_url, text)
            m["has_photo"] = True
        else:
            m["message_id"] = send_telegram_message(text)
            m["has_photo"] = False
        time.sleep(1)

    payload["posted_batches"].append(batch_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[post] Posted batch '{batch_name}' with {len(batch_matches)} matches")


# ---------------------------------------------------------------------------
# Stats storage
# ---------------------------------------------------------------------------

def load_stats():
    if not os.path.exists(STATS_PATH):
        return {}
    with open(STATS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_stats(stats):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATS_PATH, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def record_result(date_str, correct, odd):
    stats = load_stats()
    entry = stats.setdefault(date_str, {"correct": 0, "incorrect": 0, "balance": 0.0})
    entry.setdefault("balance", 0.0)
    if correct:
        entry["correct"] += 1
        entry["balance"] += round((odd - 1) * STAKE_RUB, 2)
    else:
        entry["incorrect"] += 1
        entry["balance"] -= STAKE_RUB
    entry["balance"] = round(entry["balance"], 2)
    save_stats(stats)


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def grade_pick(pick, home_score, away_score):
    market = pick["market"]
    selection = pick["selection"]
    line = pick.get("line")

    if market == "h2h":
        actual = "home" if home_score > away_score else "away" if away_score > home_score else "draw"
        return actual == selection, False

    if market == "totals":
        total = home_score + away_score
        if line is None:
            return None, None
        if total == line:
            return None, True
        return (total > line) if selection == "over" else (total < line), False

    if market == "spreads":
        if line is None:
            return None, None
        if selection == "home":
            adjusted, other = home_score + line, away_score
        else:
            adjusted, other = away_score + line, home_score
        if adjusted == other:
            return None, True
        return adjusted > other, False

    return None, None


def check_results():
    today = datetime.now(TIMEZONE).date()
    checked_total = 0

    # Group matches by sport_key so we call /scores once per sport, not once per match.
    for days_back in range(0, 3):
        day = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
        path = data_path(day)
        if not os.path.exists(path):
            continue

        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        pending = [m for m in payload["matches"] if not m.get("result_checked") and m.get("pick")]
        if not pending:
            continue

        sport_keys_needed = {m["sport_key"] for m in pending}
        scores_by_event = {}
        for sport_key in sport_keys_needed:
            try:
                scores = odds_api_get(f"/sports/{sport_key}/scores", {"daysFrom": 3})
            except requests.exceptions.HTTPError as e:
                print(f"[check_results] Error fetching scores for {sport_key}: {e}")
                continue
            for s in scores:
                scores_by_event[s["id"]] = s

        changed = False
        for m in pending:
            score_data = scores_by_event.get(m["event_id"])
            if not score_data or not score_data.get("completed"):
                continue

            score_list = score_data.get("scores")
            if not score_list:
                continue
            score_map = {s["name"]: int(s["score"]) for s in score_list}
            home_score = score_map.get(m["home"])
            away_score = score_map.get(m["away"])
            if home_score is None or away_score is None:
                continue

            correct, is_push = grade_pick(m["pick"], home_score, away_score)
            m["result_checked"] = True
            m["final_score"] = f"{home_score}:{away_score}"
            changed = True

            if correct is None and is_push is None:
                m["correct"] = None
                new_text = format_match_block(m) + f"\n\nИтог: {m['final_score']} (не удалось проверить прогноз)"
            elif is_push:
                m["correct"] = None
                new_text = format_match_block(m) + f"\n\nИтог: {m['final_score']} ➖ Возврат (пуш)"
            else:
                m["correct"] = correct
                profit = round((m["pick"]["odd"] - 1) * STAKE_RUB, 2) if correct else -STAKE_RUB
                sign = "+" if profit >= 0 else ""
                emoji = "✅" if correct else "❌"
                new_text = (
                    format_match_block(m)
                    + f"\n\nИтог: {m['final_score']} {emoji}"
                    + f"\nСтавка {STAKE_RUB}₽: {sign}{profit}₽"
                )
                record_result(day, correct, m["pick"]["odd"])
                checked_total += 1

            if m.get("message_id"):
                try:
                    if m.get("has_photo"):
                        edit_telegram_caption(m["message_id"], new_text)
                    else:
                        edit_telegram_message(m["message_id"], new_text)
                except requests.exceptions.HTTPError as e:
                    print(f"[check_results] Failed to edit message {m['message_id']}: {e}")
            time.sleep(0.3)

        if changed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[check_results] Newly graded matches: {checked_total}")


# ---------------------------------------------------------------------------
# Stats posting
# ---------------------------------------------------------------------------

def post_stats(period):
    stats = load_stats()
    now = datetime.now(TIMEZONE)

    if period == "daily":
        dates = [now.strftime("%Y-%m-%d")]
        title = f"📊 <b>Итоги дня — {now.strftime('%d.%m.%Y')}</b>"
    elif period == "weekly":
        dates = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        title = "📊 <b>Итоги недели</b>"
    elif period == "monthly":
        last_of_prev_month = now.replace(day=1) - timedelta(days=1)
        prefix = last_of_prev_month.strftime("%Y-%m")
        dates = [d for d in stats if d.startswith(prefix)]
        title = f"📊 <b>Итоги месяца — {last_of_prev_month.strftime('%m.%Y')}</b>"
    elif period == "yearly":
        prev_year = now.year - 1
        dates = [d for d in stats if d.startswith(str(prev_year))]
        title = f"📊 <b>Итоги года — {prev_year}</b>"
    else:
        print(f"Unknown period: {period}")
        return

    correct = sum(stats.get(d, {}).get("correct", 0) for d in dates)
    incorrect = sum(stats.get(d, {}).get("incorrect", 0) for d in dates)
    balance = round(sum(stats.get(d, {}).get("balance", 0) for d in dates), 2)
    total = correct + incorrect

    if total == 0:
        print(f"[stats] No graded predictions for period '{period}', skipping post")
        return

    winrate = round(correct / total * 100, 1)
    sign = "+" if balance >= 0 else ""
    text = (
        f"{title}\n\n"
        f"✅ Верных прогнозов: {correct}\n"
        f"❌ Неверных: {incorrect}\n"
        f"📈 Точность: {winrate}%\n"
        f"💰 Баланс (ставка {STAKE_RUB}₽ на прогноз): {sign}{balance}₽"
    )
    send_telegram_message(text)
    print(f"[stats] Posted {period} summary: {correct}/{total} correct, balance {sign}{balance}₽")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "post"
    if mode == "sports":
        list_sports()
    elif mode == "fetch":
        fetch_and_build()
    elif mode == "post":
        post_batch()
    elif mode == "check_results":
        check_results()
    elif mode == "stats":
        period = sys.argv[2] if len(sys.argv) > 2 else "daily"
        post_stats(period)
    else:
        print("Usage: python main.py [sports|fetch|post|check_results|stats <daily|weekly|monthly|yearly>]")
        sys.exit(1)
