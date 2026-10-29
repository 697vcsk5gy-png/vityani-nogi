"""
Агент для Telegram-канала с выгодными авиабилетами.

Два режима:
  python channel_agent.py search     — ищет выгодные билеты и присылает вам черновики постов
  python channel_agent.py approvals  — забирает ваши нажатия «Опубликовать / Пропустить»
                                       и публикует одобренное в канал

Ссылки в постах — партнёрские (Travelpayouts), покупки по ним приносят комиссию.
"""
import json
import os
import statistics
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

TP_TOKEN = os.environ["TRAVELPAYOUTS_TOKEN"]
TP_MARKER = os.environ["TRAVELPAYOUTS_MARKER"].strip()
TP_TRS = os.environ.get("TRAVELPAYOUTS_TRS", "").strip()
TG_TOKEN = os.environ["CHANNEL_BOT_TOKEN"]
ADMIN_CHAT = os.environ["TELEGRAM_CHAT_ID"].strip()
CHANNEL = os.environ["TELEGRAM_CHANNEL_ID"].strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "channel_config.json")
STATE_PATH = os.path.join(BASE, "channel_state.json")
HISTORY_LIMIT = 120
NEW_LOW_MARGIN = 0.03

CLASS_CODES = {"business": 1, "first": 2}
MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря"]


# ---------- утилиты ----------

def now():
    return datetime.now(timezone.utc)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_dt(s):
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def rub(x):
    return f"{int(x):,} ₽".replace(",", " ")


def ru_date(s):
    d = parse_dt(s)
    return f"{d.day} {MONTHS_RU[d.month - 1]}"


def months_ahead(n):
    d = now().date().replace(day=1)
    out = []
    for _ in range(n):
        out.append(d.strftime("%Y-%m"))
        d = (d + timedelta(days=32)).replace(day=1)
    return out


def tg(method, **payload):
    resp = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data["result"]


# ---------- цены ----------

def tp_get(path, params):
    params = dict(params, token=TP_TOKEN)
    resp = requests.get("https://api.travelpayouts.com" + path, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", True):
        raise RuntimeError(f"API error: {data}")
    return data.get("data", [])


TRIP_CLASS = {"economy": 0, "business": 1, "first": 2}
CLASS_LETTER = {0: "", 1: "c", 2: "f"}   # эконом — без буквы, бизнес — c, первый — f


def search_link(origin, dest, dep, ret, trip_class):
    """Ссылка на поиск Aviasales с уже выбранным классом, вида /search/MOW1001HKT2401c1."""
    path = f"/search/{origin}{dep[8:10]}{dep[5:7]}{dest}"
    if ret:
        path += f"{ret[8:10]}{ret[5:7]}"
    return "https://www.aviasales.ru" + path + CLASS_LETTER.get(trip_class, "") + "1"


def fetch_month(cfg, origin, month, cls):
    """Все направления из города за месяц одним запросом (по 1000 строк на страницу)."""
    offers = []
    for page in (1, 2, 3):
        raw = tp_get("/aviasales/v3/get_latest_prices", {
            "origin": origin, "currency": cfg["currency"],
            "period_type": "month", "beginning_of_period": month + "-01", "one_way": "false",
            "trip_class": TRIP_CLASS[cls], "show_to_affiliates": "false",
            "sorting": "price", "limit": 1000, "page": page,
        })
        for d in raw:
            if d.get("trip_class") not in (None, TRIP_CLASS[cls]):
                continue
            dep, ret, dest = d.get("depart_date"), d.get("return_date"), d.get("destination")
            if not dep or not ret or not dest:
                continue
            offers.append({"destination": dest, "price": d["value"], "departure_at": dep,
                           "return_at": ret, "transfers": d.get("number_of_changes", 0),
                           "return_transfers": d.get("number_of_changes", 0),
                           "link": search_link(origin, dest, dep, ret, TRIP_CLASS[cls])})
        if len(raw) < 1000:
            break
        time.sleep(0.3)
    return offers


def filter_offers(cfg, offers):
    out = []
    today = now().date()
    for o in offers:
        if o.get("transfers", 0) > cfg["max_transfers"] or o.get("return_transfers", 0) > cfg["max_transfers"]:
            continue
        if not o.get("return_at"):
            continue
        dep, ret = parse_dt(o["departure_at"]).date(), parse_dt(o["return_at"]).date()
        if dep <= today + timedelta(days=1):
            continue
        nights = (ret - dep).days
        if nights < cfg["min_nights"] or nights > cfg["max_nights"]:
            continue
        o["nights"] = nights
        out.append(o)
    return sorted(out, key=lambda o: o["price"])


# ---------- партнёрские ссылки ----------

def aviasales_url(o):
    l = o.get("link", "")
    return "https://www.aviasales.ru" + l if l.startswith("/") else l


def partner_link(url):
    if TP_TRS:
        try:
            resp = requests.post(
                "https://api.travelpayouts.com/links/v1/create",
                headers={"X-Access-Token": TP_TOKEN, "Content-Type": "application/json"},
                json={"trs": int(TP_TRS), "marker": int(TP_MARKER), "shorten": True,
                      "links": [{"url": url, "sub_id": "channel"}]},
                timeout=30,
            )
            link = resp.json()["result"]["links"][0]
            if link.get("code") == "success" and link.get("partner_url"):
                return link["partner_url"]
            print(f"Partner Links API не сделал ссылку: {link}")
        except Exception as e:
            print(f"Partner Links API недоступен: {e}")
    return url + ("&" if "?" in url else "?") + f"marker={TP_MARKER}"


# ---------- текст поста ----------

def template_post(cfg, d):
    o = d["offer"]
    ratio = o["price"] / d["economy"] if d.get("economy") else None
    if d["cls"] == "business" and ratio and ratio <= 1.5:
        head = f"🔥 Бизнес-класс {d['from']} {d['to']} почти по цене эконома"
    elif d["cls"] == "business" and ratio and ratio <= 2.05:
        head = f"💺 Бизнес-класс {d['from']} {d['to']} всего вдвое дороже эконома"
    elif d["cls"] == "business":
        head = f"💺 Выгодный бизнес-класс {d['from']} {d['to']}"
    else:
        head = f"🔥 Дешёвые билеты {d['from']} {d['to']}"
    lines = [head, "",
             f"✈️ {d['origin_name']} ⇄ {d['name']}",
             f"📅 {ru_date(o['departure_at'])} — {ru_date(o['return_at'])} ({o['nights']} ноч.)",
             f"💰 {rub(o['price'])} туда-обратно",
             "🔄 прямой рейс" if o.get("transfers", 0) == 0 else f"🔄 пересадок: {o['transfers']}"]
    if d.get("usual"):
        lines.append(f"📉 Обычно на этом направлении около {rub(d['usual'])}")
    if d["cls"] == "business" and d.get("economy"):
        lines.append(f"Для сравнения: эконом сейчас от {rub(d['economy'])}")
    lines += ["", "⚡ Такие цены быстро уходят — проверяйте актуальность по кнопке."]
    return "\n".join(lines)


def claude_post(cfg, d):
    o = d["offer"]
    facts = {
        "направление": f"{d['origin_name']} ⇄ {d['name']} (туда и обратно, в тексте пиши через ⇄)",
        "класс": "бизнес" if d["cls"] == "business" else "эконом",
        "даты": f"{ru_date(o['departure_at'])} — {ru_date(o['return_at'])}, {o['nights']} ночей",
        "цена": rub(o["price"]),
        "пересадки": o.get("transfers", 0),
        "обычная цена на направлении": rub(d["usual"]) if d.get("usual") else "неизвестно",
        "эконом сейчас": rub(d["economy"]) if d.get("economy") else "неизвестно",
    }
    prompt = ("Напиши короткий пост для Telegram-канала о выгодных авиабилетах. "
              "Живо, без канцелярита и без кликбейта, 4–7 строк, пара уместных эмодзи. "
              "Используй только эти факты, ничего не выдумывай:\n"
              + json.dumps(facts, ensure_ascii=False, indent=1)
              + "\nВ конце одна строка о том, что цены быстро меняются. Ссылку не вставляй. "
                "Ответь только текстом поста.")
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": CLAUDE_MODEL, "max_tokens": 500,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    resp.raise_for_status()
    return "".join(b.get("text", "") for b in resp.json()["content"]).strip()


# ---------- режим поиска ----------

def is_deal(cfg, cls, best, hist, economy_best):
    prices = [h["price"] for h in hist]
    median = statistics.median(prices) if len(prices) >= cfg["min_history"] else None
    reasons = []
    if median and best["price"] <= median * (1 - cfg["drop_percent"] / 100) \
            and best["price"] < min(prices) * (1 - NEW_LOW_MARGIN):
        reasons.append("drop")
    if cls == "business" and economy_best and best["price"] <= economy_best * cfg["business_vs_economy"]:
        reasons.append("business_cheap")
    return reasons, median


def run_search():
    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {})
    hist_all = state.setdefault("history", {})
    proposed = state.setdefault("proposed", {})
    drafts = state.setdefault("drafts", {})
    dests = {d["code"]: d for d in cfg["destinations"]}
    candidates = []
    stats = {}          # город -> сколько вариантов в кэше / по нашим направлениям
    cheapest = {}       # (город, направление, класс) -> лучшая цена за все месяцы

    for org in cfg["origins"]:
        for month in months_ahead(cfg["months_ahead"]):
            best_by = {}   # (класс, направление) -> лучшее предложение
            for cls in cfg["classes"]:
                try:
                    offers = filter_offers(cfg, fetch_month(cfg, org["code"], month, cls))
                except Exception as e:
                    print(f"[{org['code']} {month} {cls}] ошибка: {e}")
                    continue
                st = stats.setdefault(org["code"], {"all": 0, "ours": 0})
                st["all"] += len(offers)
                for o in offers:   # уже отсортированы по цене
                    if o["destination"] in dests:
                        st["ours"] += 1
                        if (cls, o["destination"]) not in best_by:
                            best_by[(cls, o["destination"])] = o
                time.sleep(0.3)

            for code, dest in dests.items():
                economy = best_by.get(("economy", code))
                economy_best = economy["price"] if economy else None
                for cls in cfg["classes"]:
                    best = best_by.get((cls, code))
                    if not best:
                        continue
                    ck = (org["code"], code, cls)
                    if ck not in cheapest or best["price"] < cheapest[ck]["price"]:
                        cheapest[ck] = {"price": best["price"], "org": org, "dest": dest,
                                        "economy": economy_best, "offer": best}
                    key = f"{org['code']}-{code}-{month}-{cls}"
                    hist = hist_all.setdefault(key, [])
                    reasons, median = is_deal(cfg, cls, best, hist, economy_best)
                    hist.append({"ts": now().isoformat(), "price": best["price"]})
                    hist_all[key] = hist[-HISTORY_LIMIT:]
                    if not reasons:
                        continue
                    p = proposed.get(key)
                    if p and (now() - parse_dt(p["ts"])).days < cfg["repeat_after_days"] \
                            and best["price"] > p["price"] * 0.95:
                        continue
                    score = (1 - best["price"] / median) if median else 0
                    if cls == "business" and economy_best:
                        score = max(score, 1 - best["price"] / (economy_best * 2.5))
                    candidates.append({"key": key, "cls": cls, "name": dest["name"], "to": dest["to"],
                                       "origin": org["code"], "origin_name": org["name"],
                                       "from": org["from"], "offer": best, "usual": median,
                                       "economy": economy_best, "score": score})
                    print(f"[{key}] находка: {rub(best['price'])} ({', '.join(reasons)})")

    candidates.sort(key=lambda c: -c["score"])
    picked, per_origin = [], {}
    for c in candidates:
        if len(picked) >= cfg["max_drafts_per_run"]:
            break
        if per_origin.get(c["origin"], 0) >= cfg.get("max_drafts_per_origin", 99):
            continue
        per_origin[c["origin"]] = per_origin.get(c["origin"], 0) + 1
        picked.append(c)
    for d in picked:
        text = template_post(cfg, d)
        if ANTHROPIC_KEY:
            try:
                text = claude_post(cfg, d)
            except Exception as e:
                print(f"Claude недоступен ({e}), использую шаблон")
        url = aviasales_url(d["offer"])
        draft_id = uuid.uuid4().hex[:8]
        msg = tg("sendMessage", chat_id=ADMIN_CHAT, disable_web_page_preview=True,
                 text="📝 Черновик для канала:\n\n" + text,
                 reply_markup={"inline_keyboard": [
                     [{"text": "✅ Опубликовать", "callback_data": f"pub:{draft_id}"},
                      {"text": "❌ Пропустить", "callback_data": f"skip:{draft_id}"}],
                     [{"text": "🔎 Проверить на Aviasales", "url": url}]]})
        drafts[draft_id] = {"text": text, "url": url, "ts": now().isoformat(),
                            "message_id": msg["message_id"], "cls": d["cls"]}
        proposed[d["key"]] = {"ts": now().isoformat(), "price": d["offer"]["price"]}

    for code, st in stats.items():
        print(f"[{code}] вариантов в кэше: {st['all']}, по нашим направлениям: {st['ours']}")

    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    every = cfg.get("report", "every") == "every"
    if manual or every or (now().hour in (6, 7) and not state.get("report_day") == now().date().isoformat()):
        state["report_day"] = now().date().isoformat()
        send_report(cfg, stats, cheapest, len(candidates), len(picked))

    # убираем старые черновики
    ttl = timedelta(hours=cfg["draft_ttl_hours"])
    for k in [k for k, v in drafts.items() if now() - parse_dt(v["ts"]) > ttl * 2]:
        drafts.pop(k)
    save_json(STATE_PATH, state)


def send_report(cfg, stats, cheapest, found, sent):
    total = sum(st["ours"] for st in stats.values())
    lines = [f"🔎 Отчёт поиска",
             f"Городов проверено: {len(stats)} из {len(cfg['origins'])}, вариантов по нашим направлениям: {total}",
             f"Находок по правилам: {found}, отправлено черновиков: {sent}"]
    eco = sorted([v for (o, d, c), v in cheapest.items() if c == "economy"], key=lambda v: v["price"])[:5]
    if eco:
        lines += ["", "Самый дешёвый эконом сейчас:"]
        lines += [f"• {v['org']['name']} ⇄ {v['dest']['name']}: {rub(v['price'])}" for v in eco]
    bus = [v for (o, d, c), v in cheapest.items() if c == "business" and v["economy"]]
    bus = sorted(bus, key=lambda v: v["price"] / v["economy"])[:5]
    if bus:
        lines += ["", "Бизнес ближе всего к эконому:"]
        lines += [f"• {v['org']['name']} ⇄ {v['dest']['name']}: {rub(v['price'])} "
                  f"(×{v['price'] / v['economy']:.1f} к эконому)" for v in bus]
    if not total:
        lines += ["", "⚠️ API не вернул ни одного варианта — проверьте токен Travelpayouts."]
    tg("sendMessage", chat_id=ADMIN_CHAT, text="\n".join(lines), disable_web_page_preview=True)


# ---------- режим одобрения ----------

def mark(draft, suffix):
    try:
        tg("editMessageText", chat_id=ADMIN_CHAT, message_id=draft["message_id"],
           text="📝 Черновик для канала:\n\n" + draft["text"] + "\n\n" + suffix,
           disable_web_page_preview=True)
    except Exception as e:
        print(f"не смог обновить черновик: {e}")


def run_approvals():
    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {})
    drafts = state.setdefault("drafts", {})
    offset = state.get("tg_offset", 0)
    params = {"timeout": 0, "allowed_updates": ["callback_query", "message"]}
    if offset:
        params["offset"] = offset + 1
    updates = tg("getUpdates", **params)
    for u in updates:
        state["tg_offset"] = u["update_id"]
        cq = u.get("callback_query")
        if not cq or str(cq["from"]["id"]) != ADMIN_CHAT:
            continue
        action, _, draft_id = cq.get("data", "").partition(":")
        draft = drafts.get(draft_id)
        note = ""
        if not draft or draft.get("status"):
            note = "Уже обработано"
        elif action == "skip":
            draft["status"] = "skipped"
            mark(draft, "❌ Пропущено")
            note = "Пропущено"
        elif action == "pub":
            if now() - parse_dt(draft["ts"]) > timedelta(hours=cfg["draft_ttl_hours"]):
                draft["status"] = "expired"
                mark(draft, "⌛ Устарело — цена могла уйти, не публикую")
                note = "Устарело"
            else:
                label = "💺 Смотреть бизнес-класс" if draft["cls"] == "business" else "✈️ Смотреть билеты"
                tg("sendMessage", chat_id=CHANNEL, text=draft["text"], disable_web_page_preview=True,
                   reply_markup={"inline_keyboard": [[{"text": label, "url": partner_link(draft["url"])}]]})
                draft["status"] = "published"
                mark(draft, "✅ Опубликовано в канале")
                note = "Опубликовано"
        try:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text=note)
        except Exception:
            pass  # кнопку нажали давно — Telegram уже не принимает ответ, это нормально
        print(f"{action} {draft_id}: {note}")
    save_json(STATE_PATH, state)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "search"
    run_approvals() if mode == "approvals" else run_search()
