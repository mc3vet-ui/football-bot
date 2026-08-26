"""
Football predictions bot.

Modes:
    python main.py fetch          -> pulls today's fixtures + odds, computes probabilities,
                                      saves data/matches_<date>.json
    python main.py post           -> posts the next unposted time-batch to Telegram
    python main.py leagues        -> debug: search league IDs by name
    python main.py check_results  -> checks finished matches, marks predictions right/wrong,
                                      edits the original Telegram messages, updates stats
    python main.py stats daily    -> posts a stats summary for today
    python main.py stats weekly   -> posts a stats summary for the trailing 7 days
    python main.py stats monthly  -> posts a stats summary for the previous calendar month
    python main.py stats yearly   -> posts a stats summary for the previous calendar year

Environment variables (set as GitHub Actions secrets):
    API_FOOTBALL_KEY     - API key from dashboard.api-football.com
    TELEGRAM_BOT_TOKEN   - Telegram bot token from @BotFather
    TELEGRAM_CHANNEL     - channel username, e.g. @footballgolplus or numeric chat id
    TIMEZONE             - IANA tz name, default "Europe/Moscow"
    FORCE_BATCH          - optional override ("morning"/"day"/"evening") for manual testing
"""

import os
import sys
import json
import time
import glob
import requests
from datetime import datetime, timedelta
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
    40: "Чемпионшип",
    45: "Кубок Англии",
    79: "Бундеслига 2",
    253: "MLS",
}

API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")
API_BASE = "https://v3.football.api-sports.io"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "")

TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Moscow"))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
STATS_PATH = os.path.join(DATA_DIR, "stats.json")

BATCHES = [
    ("morning", 0, 15),
    ("day", 15, 19),
    ("evening", 19, 24),
]

BET_ID_MATCH_WINNER = 1
FINISHED_STATUSES = {"FT", "AET", "PEN"}
DEAD_STATUSES = {"PST", "CANC", "ABD", "AWD", "WO"}
STAKE_RUB = 1000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def today_str():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def data_path(day_str):
    return os.path.join(DATA_DIR, f"matches_{day_str}.json")


def api_get(endpoint, params):
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    resp = requests.get(f"{API_BASE}{endpoint}", headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def implied_probabilities(home_odd, draw_odd, away_odd):
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


def predicted_outcome_from_probs(probs):
    keys = ["home", "draw", "away"]
    return keys[probs.index(max(probs))]


# ---------------------------------------------------------------------------
# Fetch mode
# ---------------------------------------------------------------------------

def fetch_and_build():
    day_str = today_str()
    print(f"[fetch] Getting fixtures for {day_str}")

    fixtures_resp = api_get("/fixtures", {"date": day_str})
    fixtures = fixtures_resp.get("response", [])
    print(f"[debug] Raw fixtures from API: {len(fixtures)}")

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
        home_logo = fx["teams"]["home"].get("logo")
        league_name = LEAGUE_IDS[league_id]

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
            "home_logo": home_logo,
            "kickoff_local": kickoff_local.strftime("%Y-%m-%d %H:%M"),
            "kickoff_hour": kickoff_local.hour,
            "message_id": None,
            "result_checked": False,
            "correct": None,
        }

        if home_odd and draw_odd and away_odd:
            probs = implied_probabilities(home_odd, draw_odd, away_odd)
            match.update({
                "odds": {"home": home_odd, "draw": draw_odd, "away": away_odd},
                "probs": {"home": probs[0], "draw": probs[1], "away": probs[2]},
                "verdict": verdict_phrase(probs, home, away),
                "predicted_outcome": predicted_outcome_from_probs(probs),
            })
        else:
            match.update({
                "odds": None, "probs": None,
                "verdict": "Коэффициенты пока недоступны",
                "predicted_outcome": None,
            })

        matches.append(match)
        time.sleep(0.3)

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


def format_match_block(m):
    lines = [f"⚽ {m['league']}: {m['home']} — {m['away']}", f"🕒 {m['kickoff_local']} (мск)"]
    if m["odds"]:
        lines.append(f"П1 {m['odds']['home']} | X {m['odds']['draw']} | П2 {m['odds']['away']}")
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
    data = resp.json()
    return data.get("result", {}).get("message_id")


def send_telegram_photo(photo_url, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHANNEL,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", {}).get("message_id")


def edit_telegram_caption(message_id, new_caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageCaption"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHANNEL,
        "message_id": message_id,
        "caption": new_caption,
        "parse_mode": "HTML",
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
        caption = format_match_block(m)
        if m.get("home_logo"):
            message_id = send_telegram_photo(m["home_logo"], caption)
        else:
            message_id = send_telegram_message(caption)
        m["message_id"] = message_id
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
        profit = round((odd - 1) * STAKE_RUB, 2)
        entry["balance"] += profit
    else:
        entry["incorrect"] += 1
        entry["balance"] -= STAKE_RUB
    entry["balance"] = round(entry["balance"], 2)
    save_stats(stats)


# ---------------------------------------------------------------------------
# Check results mode
# ---------------------------------------------------------------------------

def check_results():
    today = datetime.now(TIMEZONE).date()
    checked_total = 0

    for days_back in range(0, 3):
        day = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
        path = data_path(day)
        if not os.path.exists(path):
            continue

        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        changed = False
        for m in payload["matches"]:
            if m.get("result_checked"):
                continue
            if not m.get("predicted_outcome"):
                # No prediction was made for this match, nothing to grade.
                m["result_checked"] = True
                changed = True
                continue

            resp = api_get("/fixtures", {"id": m["fixture_id"]})
            items = resp.get("response", [])
            if not items:
                continue
            fixture = items[0]
            status_short = fixture["fixture"]["status"]["short"]

            if status_short in DEAD_STATUSES:
                m["result_checked"] = True
                changed = True
                continue

            if status_short not in FINISHED_STATUSES:
                continue  # still not finished, check again next run

            goals_home = fixture["goals"]["home"]
            goals_away = fixture["goals"]["away"]
            if goals_home is None or goals_away is None:
                continue

            if goals_home > goals_away:
                actual = "home"
            elif goals_away > goals_home:
                actual = "away"
            else:
                actual = "draw"

            correct = (actual == m["predicted_outcome"])
            m["result_checked"] = True
            m["correct"] = correct
            m["final_score"] = f"{goals_home}:{goals_away}"
            changed = True
            checked_total += 1

            odd = m["odds"][m["predicted_outcome"]]
            profit = round((odd - 1) * STAKE_RUB, 2) if correct else -STAKE_RUB
            sign = "+" if profit >= 0 else ""

            emoji = "✅" if correct else "❌"
            new_caption = (
                format_match_block(m)
                + f"\n\nИтог: {m['final_score']} {emoji}"
                + f"\nСтавка {STAKE_RUB}₽: {sign}{profit}₽"
            )
            if m.get("message_id"):
                try:
                    edit_telegram_caption(m["message_id"], new_caption)
                except requests.exceptions.HTTPError as e:
                    print(f"[check_results] Failed to edit message {m['message_id']}: {e}")

            record_result(day, correct, odd)
            time.sleep(0.5)

        if changed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[check_results] Newly graded matches: {checked_total}")


# ---------------------------------------------------------------------------
# Stats posting mode
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
        first_of_this_month = now.replace(day=1)
        last_of_prev_month = first_of_this_month - timedelta(days=1)
        month_prefix = last_of_prev_month.strftime("%Y-%m")
        dates = [d for d in stats if d.startswith(month_prefix)]
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
    balance = sum(stats.get(d, {}).get("balance", 0) for d in dates)
    total = correct + incorrect

    if total == 0:
        print(f"[stats] No graded predictions for period '{period}', skipping post")
        return

    winrate = round(correct / total * 100, 1)
    balance = round(balance, 2)
    balance_sign = "+" if balance >= 0 else ""
    text = (
        f"{title}\n\n"
        f"✅ Верных прогнозов: {correct}\n"
        f"❌ Неверных: {incorrect}\n"
        f"📈 Точность: {winrate}%\n"
        f"💰 Баланс (ставка {STAKE_RUB}₽ на прогноз): {balance_sign}{balance}₽"
    )
    send_telegram_message(text)
    print(f"[stats] Posted {period} summary: {correct}/{total} correct, balance {balance_sign}{balance}₽")


# ---------------------------------------------------------------------------
# Debug: league search
# ---------------------------------------------------------------------------

def find_leagues():
    for term in ["MLS", "FA Cup"]:
        resp = api_get("/leagues", {"search": term})
        print(f"--- search: {term} ---")
        for item in resp.get("response", []):
            league = item["league"]
            country = item["country"]["name"]
            print(f"id={league['id']} name={league['name']} type={league['type']} country={country}")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "post"
    if mode == "fetch":
        fetch_and_build()
    elif mode == "post":
        post_batch()
    elif mode == "leagues":
        find_leagues()
    elif mode == "check_results":
        check_results()
    elif mode == "stats":
        period = sys.argv[2] if len(sys.argv) > 2 else "daily"
        post_stats(period)
    else:
        print("Usage: python main.py [fetch|post|leagues|check_results|stats <daily|weekly|monthly|yearly>]")
        sys.exit(1)
