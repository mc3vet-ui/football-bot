"""
Football predictions bot.

Modes:
    python main.py fetch          -> pulls today's fixtures + all odds markets, asks AI to
                                      pick the most promising bet, saves data/matches_<date>.json
    python main.py post           -> posts the next unposted time-batch to Telegram
    python main.py leagues        -> debug: search league IDs by name
    python main.py bet_types      -> debug: list available odds markets for today's first match
    python main.py check_results  -> checks finished matches, grades the AI's pick, edits the
                                      original Telegram messages, updates stats
    python main.py stats daily    -> posts a stats summary for today
    python main.py stats weekly   -> posts a stats summary for the trailing 7 days
    python main.py stats monthly  -> posts a stats summary for the previous calendar month
    python main.py stats yearly   -> posts a stats summary for the previous calendar year

Environment variables (set as GitHub Actions secrets):
    API_FOOTBALL_KEY     - API key from dashboard.api-football.com
    TELEGRAM_BOT_TOKEN   - Telegram bot token from @BotFather
    TELEGRAM_CHANNEL     - channel username, e.g. @footballgolplus or numeric chat id
    GROQ_API_KEY         - API key from console.groq.com
    TIMEZONE             - IANA tz name, default "Europe/Moscow"
    FORCE_BATCH          - optional override ("morning"/"day"/"evening") for manual testing
"""

import os
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

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Moscow"))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
STATS_PATH = os.path.join(DATA_DIR, "stats.json")

BATCHES = [
    ("morning", 0, 15),
    ("day", 15, 19),
    ("evening", 19, 24),
]

FINISHED_STATUSES = {"FT", "AET", "PEN"}
DEAD_STATUSES = {"PST", "CANC", "ABD", "AWD", "WO"}
STAKE_RUB = 1000

MARKET_IDS = {
    "match_winner": 1,
    "handicap": 4,
    "total_match": 5,
    "total_1h": 6,
    "total_2h": 26,
    "total_home": 16,
    "total_away": 17,
}
MARKET_LABELS = {
    "match_winner": "Исход матча",
    "handicap": "Фора",
    "total_match": "Тотал матча",
    "total_1h": "Тотал 1-го тайма",
    "total_2h": "Тотал 2-го тайма",
    "total_home": "Тотал хозяев",
    "total_away": "Тотал гостей",
}


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


def predicted_outcome_from_probs(probs):
    keys = ["home", "draw", "away"]
    return keys[probs.index(max(probs))]


def get_all_odds(fixture_id):
    try:
        resp = api_get("/odds", {"fixture": fixture_id})
    except requests.exceptions.HTTPError:
        return {}
    data = resp.get("response", [])
    if not data or not data[0].get("bookmakers"):
        return {}
    bets = data[0]["bookmakers"][0]["bets"]
    return {bet["id"]: bet for bet in bets}


def extract_lines(bets_by_id, market_key, limit=6):
    bet_id = MARKET_IDS[market_key]
    bet = bets_by_id.get(bet_id)
    if not bet:
        return []
    out = []
    for v in bet.get("values", [])[:limit]:
        try:
            out.append((v["value"], float(v["odd"])))
        except (KeyError, ValueError):
            continue
    return out


def get_match_winner_odds(bets_by_id):
    lines = extract_lines(bets_by_id, "match_winner", limit=3)
    odds = {v.lower(): odd for v, odd in lines}
    return odds.get("home"), odds.get("draw"), odds.get("away")


def build_odds_context(bets_by_id):
    parts = []
    for key in ["match_winner", "handicap", "total_match", "total_1h", "total_2h", "total_home", "total_away"]:
        lines = extract_lines(bets_by_id, key)
        if not lines:
            continue
        label = MARKET_LABELS[key]
        line_text = ", ".join(f"{v} ({odd})" for v, odd in lines)
        parts.append(f"{label}: {line_text}")
    return "\n".join(parts)


def get_recent_form(team_id):
    try:
        resp = api_get("/fixtures", {"team": team_id, "last": 5})
    except requests.exceptions.HTTPError:
        return "нет данных"
    results = []
    for fx in resp.get("response", []):
        home_id = fx["teams"]["home"]["id"]
        gh, ga = fx["goals"]["home"], fx["goals"]["away"]
        if gh is None or ga is None:
            continue
        is_home = (home_id == team_id)
        tg, og = (gh, ga) if is_home else (ga, gh)
        results.append("W" if tg > og else "L" if tg < og else "D")
    return "-".join(results) if results else "нет данных"


def get_h2h_summary(home_id, away_id):
    try:
        resp = api_get("/fixtures/headtohead", {"h2h": f"{home_id}-{away_id}", "last": 5})
    except requests.exceptions.HTTPError:
        return "нет данных"
    lines = []
    for fx in resp.get("response", []):
        gh, ga = fx["goals"]["home"], fx["goals"]["away"]
        if gh is None or ga is None:
            continue
        lines.append(f"{fx['teams']['home']['name']} {gh}:{ga} {fx['teams']['away']['name']}")
    return "; ".join(lines) if lines else "личных встреч не найдено"


def call_groq(prompt):
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 300,
    }
    resp = requests.post(GROQ_URL, headers=headers, json=body, timeout=30)
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


def build_ai_pick(home, away, league, odds_context, form_home, form_away, h2h):
    prompt = (
        f"Ты профессиональный аналитик по футбольным ставкам. Матч: {home} — {away}, "
        f"турнир «{league}».\n\n"
        f"Доступные рынки и коэффициенты букмекера (используй ТОЛЬКО эти данные, "
        f"ничего не придумывай):\n{odds_context}\n\n"
        f"Форма {home} (последние 5 матчей, от старых к новым): {form_home}\n"
        f"Форма {away} (последние 5 матчей): {form_away}\n"
        f"Личные встречи: {h2h}\n\n"
        f"Выбери ОДИН наиболее вероятный ('проходимый') вариант ставки из перечисленных "
        f"выше рынков и линий. Это может быть исход матча, фора или тотал (общий, по "
        f"таймам или по команде) — выбирай то, что статистически выглядит надёжнее всего, "
        f"необязательно с наименьшим коэффициентом.\n\n"
        f"Ответь СТРОГО в виде JSON без какого-либо текста до или после него, в формате:\n"
        f'{{"market": "match_winner|handicap|total_match|total_1h|total_2h|total_home|total_away", '
        f'"selection": "home|draw|away|over|under", "line": число или null, '
        f'"odd": число (скопируй точно из списка выше), '
        f'"reasoning": "1-2 предложения на русском с обоснованием"}}'
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

    required = {"market", "selection", "odd", "reasoning"}
    if not required.issubset(parsed.keys()):
        print(f"[ai] Groq response missing fields: {parsed}")
        return None
    if parsed["market"] not in MARKET_IDS:
        print(f"[ai] Unknown market from Groq: {parsed['market']}")
        return None

    parsed.setdefault("line", None)
    return parsed


def fallback_pick(bets_by_id):
    home_odd, draw_odd, away_odd = get_match_winner_odds(bets_by_id)
    if not (home_odd and draw_odd and away_odd):
        return None
    probs = implied_probabilities(home_odd, draw_odd, away_odd)
    outcome = predicted_outcome_from_probs(probs)
    odd = {"home": home_odd, "draw": draw_odd, "away": away_odd}[outcome]
    return {
        "market": "match_winner",
        "selection": outcome,
        "line": None,
        "odd": odd,
        "reasoning": "Прогноз по коэффициентам букмекера (ИИ-анализ временно недоступен).",
    }


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
        home_id = fx["teams"]["home"]["id"]
        away_id = fx["teams"]["away"]["id"]
        home_logo = fx["teams"]["home"].get("logo")
        league_name = LEAGUE_IDS[league_id]

        bets_by_id = get_all_odds(fixture_id)

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
            "pick": None,
        }

        if bets_by_id:
            odds_context = build_odds_context(bets_by_id)
            form_home = get_recent_form(home_id)
            form_away = get_recent_form(away_id)
            h2h = get_h2h_summary(home_id, away_id)

            pick = None
            if odds_context and GROQ_API_KEY:
                pick = build_ai_pick(home, away, league_name, odds_context, form_home, form_away, h2h)
            if not pick:
                pick = fallback_pick(bets_by_id)

            match["pick"] = pick

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
        return "Прогноз недоступен"
    label = MARKET_LABELS.get(pick["market"], pick["market"])
    if pick["market"] == "match_winner":
        sel_text = {"home": m["home"], "draw": "Ничья", "away": m["away"]}.get(pick["selection"], pick["selection"])
        return f"{label}: {sel_text} (кф. {pick['odd']})"
    if pick["market"] == "handicap":
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
        lines.append("Прогноз недоступен")
    return "\n".join(lines)


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


def edit_telegram_caption(message_id, new_caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageCaption"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHANNEL, "message_id": message_id,
        "caption": new_caption, "parse_mode": "HTML",
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

    send_telegram_message(header)
    time.sleep(1)

    for m in batch_matches:
        caption = format_match_block(m)
        message_id = send_telegram_photo(m["home_logo"], caption) if m.get("home_logo") else send_telegram_message(caption)
        m["message_id"] = message_id
        time.sleep(1)

    payload["posted_batches"].append(batch_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[post] Posted batch '{batch_name}' with {len(batch_matches)} matches")


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


def grade_pick(pick, goals_home, goals_away, ht_home, ht_away):
    market = pick["market"]
    selection = pick["selection"]
    line = pick.get("line")

    if market == "match_winner":
        actual = "home" if goals_home > goals_away else "away" if goals_away > goals_home else "draw"
        return actual == selection, False

    if market == "total_2h":
        if ht_home is None or ht_away is None:
            return None, None
        total = (goals_home - ht_home) + (goals_away - ht_away)
    elif market == "total_1h":
        if ht_home is None or ht_away is None:
            return None, None
        total = ht_home + ht_away
    elif market == "total_match":
        total = goals_home + goals_away
    elif market == "total_home":
        total = goals_home
    elif market == "total_away":
        total = goals_away
    elif market == "handicap":
        if line is None:
            return None, None
        if selection == "home":
            adjusted, other = goals_home + line, goals_away
        else:
            adjusted, other = goals_away + line, goals_home
        if adjusted == other:
            return None, True
        return adjusted > other, False
    else:
        return None, None

    if line is None:
        return None, None
    if total == line:
        return None, True
    correct = (total > line) if selection == "over" else (total < line)
    return correct, False


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
            if not m.get("pick"):
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
                continue

            goals_home = fixture["goals"]["home"]
            goals_away = fixture["goals"]["away"]
            if goals_home is None or goals_away is None:
                continue

            halftime = fixture.get("score", {}).get("halftime", {}) or {}
            ht_home, ht_away = halftime.get("home"), halftime.get("away")

            correct, is_push = grade_pick(m["pick"], goals_home, goals_away, ht_home, ht_away)
            m["result_checked"] = True
            m["final_score"] = f"{goals_home}:{goals_away}"
            changed = True

            if correct is None and is_push is None:
                m["correct"] = None
                new_caption = format_match_block(m) + f"\n\nИтог: {m['final_score']} (не удалось проверить прогноз)"
            elif is_push:
                m["correct"] = None
                new_caption = format_match_block(m) + f"\n\nИтог: {m['final_score']} ➖ Возврат (пуш)"
            else:
                m["correct"] = correct
                profit = round((m["pick"]["odd"] - 1) * STAKE_RUB, 2) if correct else -STAKE_RUB
                sign = "+" if profit >= 0 else ""
                emoji = "✅" if correct else "❌"
                new_caption = (
                    format_match_block(m)
                    + f"\n\nИтог: {m['final_score']} {emoji}"
                    + f"\nСтавка {STAKE_RUB}₽: {sign}{profit}₽"
                )
                record_result(day, correct, m["pick"]["odd"])
                checked_total += 1

            if m.get("message_id"):
                try:
                    edit_telegram_caption(m["message_id"], new_caption)
                except requests.exceptions.HTTPError as e:
                    print(f"[check_results] Failed to edit message {m['message_id']}: {e}")

            time.sleep(0.5)

        if changed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[check_results] Newly graded matches: {checked_total}")


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


def find_leagues():
    for term in ["MLS", "FA Cup"]:
        resp = api_get("/leagues", {"search": term})
        print(f"--- search: {term} ---")
        for item in resp.get("response", []):
            league = item["league"]
            print(f"id={league['id']} name={league['name']} type={league['type']} country={item['country']['name']}")


def find_bet_types():
    resp = api_get("/fixtures", {"date": today_str()})
    fixtures = resp.get("response", [])
    for fx in fixtures:
        if fx["league"]["id"] in LEAGUE_IDS:
            fixture_id = fx["fixture"]["id"]
            odds_resp = api_get("/odds", {"fixture": fixture_id})
            data = odds_resp.get("response", [])
            if data and data[0].get("bookmakers"):
                bookmaker = data[0]["bookmakers"][0]
                print(f"Fixture {fixture_id}: {fx['teams']['home']['name']} vs {fx['teams']['away']['name']}")
                print(f"Bookmaker: {bookmaker['name']}")
                for bet in bookmaker["bets"]:
                    print(f"  id={bet['id']} name={bet['name']}")
                return
    print("No fixtures with odds found today in tracked leagues")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "post"
    if mode == "fetch":
        fetch_and_build()
    elif mode == "post":
        post_batch()
    elif mode == "leagues":
        find_leagues()
    elif mode == "bet_types":
        find_bet_types()
    elif mode == "check_results":
        check_results()
    elif mode == "stats":
        period = sys.argv[2] if len(sys.argv) > 2 else "daily"
        post_stats(period)
    else:
        print("Usage: python main.py [fetch|post|leagues|bet_types|check_results|stats <daily|weekly|monthly|yearly>]")
        sys.exit(1)
