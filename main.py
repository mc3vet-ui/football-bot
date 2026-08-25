"""
Football predictions bot.

Modes:
    python main.py fetch   -> pulls today's fixtures + odds from API-Football,
                               computes probabilities, saves data/matches_<date>.json
    python main.py post    -> posts the next unposted time-batch to Telegram

Environment variables (set as GitHub Actions secrets):
    API_FOOTBALL_KEY     - RapidAPI key for API-Football
    TELEGRAM_BOT_TOKEN   - Telegram bot token from @BotFather
    TELEGRAM_CHANNEL     - channel username, e.g. @footballgolplus or numeric chat id
    TIMEZONE             - IANA tz name, default "Europe/Moscow"
"""

import os
import sys
import json
import time
import requests
from datetime import datetime, date
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LEAGUE_IDS = {
    39: "АПЛ",
    140: "Ла Лига",
    135: "Серия А",
    78: "Бундеслига",
    61: "Лига 1",
    2: "Лига Чемпионов",
    3: "Лига Европы",
    235: "РПЛ",
}

API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")
API_FOOTBALL_HOST = "v3.football.api-sports.io"
API_BASE = f"https://{API_FOOTBALL_HOST}"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "")

TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Moscow"))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# Batch windows are defined by local kickoff hour.
# (batch_name, hour_from_inclusive, hour_to_exclusive)
BATCHES = [
    ("morning", 0, 15),
    ("day", 15, 19),
    ("evening", 19, 24),
]

BET_ID_MATCH_WINNER = 1  # "Match Winner" market id in API-Football odds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def today_str():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def data_path(day_str):
    return os.path.join(DATA_DIR, f"matches_{day_str}.json")


def api_get(endpoint, params):
    headers = {
        "x-rapidapi-host": API_FOOTBALL_HOST,
        "x-rapidapi-key": API_FOOTBALL_KEY,
    }
    resp = requests.get(f"{API_BASE}{endpoint}", headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def implied_probabilities(home_odd, draw_odd, away_odd):
    """Convert decimal odds to normalized (de-margined) win probabilities."""
    raw = [1 / home_odd, 1 / draw_odd, 1 / away_odd]
    total = sum(raw)
    return [round(r / total * 100, 1) for r in raw]


def verdict_phrase(probs, home, away):
    labels = [home, "Ничья", away]
    best_idx = probs.index(max(probs))
    best_prob = probs[best_idx]
    if best_prob >= 65:
        confidence = "Уверенный фаворит"
    elif best_prob >= 50:
        confidence = "Небольшой перевес"
    else:
        confidence = "Матч без явного фаворита"
    return f"{confidence}: {labels[best_idx]} ({best_prob}%)"


# ---------------------------------------------------------------------------
# Fetch mode
# ---------------------------------------------------------------------------

def fetch_and_build():
    day_str = today_str()
    print(f"[fetch] Getting fixtures for {day_str}")

        fixtures_resp = api_get("/fixtures", {"date": day_str})
    fixtures = fixtures_resp.get("response", [])
    print(f"[debug] Raw fixtures from API: {len(fixtures)}")
    print(f"[debug] API errors field: {fixtures_resp.get('errors')}")
    print(f"[debug] API results field: {fixtures_resp.get('results')}")
    if fixtures:
        sample_leagues = {fx['league']['id']: fx['league']['name'] for fx in fixtures[:20]}
        print(f"[debug] Sample league IDs found: {sample_leagues}")

    matches = []
    for fx in fixtures:
        league_id = fx["league"]["id"]
        if league_id not in LEAGUE_IDS:
            continue

        fixture_id = fx["fixture"]["id"]
        kickoff_utc = fx["fixture"]["date"]
        kickoff_local = datetime.fromisoformat(kickoff_utc).astimezone(TIMEZONE)

        home = fx["teams"]["home"]["name"]
        away = fx["teams"]["away"]["name"]
        league_name = LEAGUE_IDS[league_id]

        # Fetch 1X2 odds for this fixture
        odds_resp = api_get("/odds", {"fixture": fixture_id, "bet": BET_ID_MATCH_WINNER})
        odds_data = odds_resp.get("response", [])

        home_odd = draw_odd = away_odd = None
        if odds_data:
            try:
                bookmaker = odds_data[0]["bookmakers"][0]
                bet = bookmaker["bets"][0]
                values = {v["value"]: float(v["odd"]) for v in bet["values"]}
                home_odd = values.get("Home")
                draw_odd = values.get("Draw")
                away_odd = values.get("Away")
            except (IndexError, KeyError):
                pass

        match = {
            "fixture_id": fixture_id,
            "league": league_name,
            "home": home,
            "away": away,
            "kickoff_local": kickoff_local.strftime("%Y-%m-%d %H:%M"),
            "kickoff_hour": kickoff_local.hour,
        }

        if home_odd and draw_odd and away_odd:
            probs = implied_probabilities(home_odd, draw_odd, away_odd)
            match.update({
                "odds": {"home": home_odd, "draw": draw_odd, "away": away_odd},
                "probs": {"home": probs[0], "draw": probs[1], "away": probs[2]},
                "verdict": verdict_phrase(probs, home, away),
            })
        else:
            match.update({"odds": None, "probs": None, "verdict": "Коэффициенты пока недоступны"})

        matches.append(match)
        time.sleep(0.3)  # be gentle on rate limits

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
# Post mode
# ---------------------------------------------------------------------------

def current_batch_name():
    hour = datetime.now(TIMEZONE).hour
    for name, start, end in BATCHES:
        if start <= hour < end:
            return name
    return BATCHES[-1][0]


def format_match_block(m):
    lines = [f"⚽ {m['league']}: {m['home']} — {m['away']}", f"🕒 {m['kickoff_local']} (мск)"]
    if m["odds"]:
        lines.append(
            f"П1 {m['odds']['home']} | X {m['odds']['draw']} | П2 {m['odds']['away']}"
        )
        lines.append(f"📊 {m['verdict']}")
    else:
        lines.append("Коэффициенты пока недоступны")
    return "\n".join(lines)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHANNEL,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()


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

    # Telegram messages have a ~4096 char limit; chunk if needed.
    chunk = [header]
    current_len = len(header)
    chunks = []
    for m in batch_matches:
        block = format_match_block(m)
        if current_len + len(block) + 2 > 3800:
            chunks.append("\n\n".join(chunk))
            chunk = [block]
            current_len = len(block)
        else:
            chunk.append(block)
            current_len += len(block) + 2
    chunks.append("\n\n".join(chunk))

    for text in chunks:
        send_telegram_message(text)
        time.sleep(1)

    payload["posted_batches"].append(batch_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[post] Posted batch '{batch_name}' with {len(batch_matches)} matches")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "post"
    if mode == "fetch":
        fetch_and_build()
    elif mode == "post":
        post_batch()
    else:
        print("Usage: python main.py [fetch|post]")
        sys.exit(1)
