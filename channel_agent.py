"""
Агент для Telegram-канала «Вытяни ноги!» с выгодными авиабилетами.

  python channel_agent.py search  — ищет выгодные билеты (туда-обратно и в одну сторону)
                                    и присылает вам черновики постов с картинкой.

Кнопки «Опубликовать / Пропустить» под черновиком обрабатывает мгновенно
отдельный маленький сервер на Cloudflare (файл worker.js), а не GitHub.

Ссылки в постах — партнёрские (Travelpayouts), покупки по ним приносят комиссию.
"""
import html
import json
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

TP_TOKEN = os.environ["TRAVELPAYOUTS_TOKEN"]
TP_MARKER = os.environ["TRAVELPAYOUTS_MARKER"].strip()
TP_TRS = os.environ.get("TRAVELPAYOUTS_TRS", "").strip()
TG_TOKEN = os.environ["CHANNEL_BOT_TOKEN"]
ADMIN_CHAT = os.environ["TELEGRAM_CHAT_ID"].strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY", "").strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "channel_config.json")
STATE_PATH = os.path.join(BASE, "channel_state.json")
HISTORY_LIMIT = 60
NEW_LOW_MARGIN = 0.03
CAPTION_LIMIT = 1000   # у подписи к картинке в Telegram лимит 1024 символа

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря"]
TRIP_CLASS = {"economy": 0, "business": 1, "first": 2}
CLASS_LETTER = {0: "", 1: "c", 2: "f"}   # эконом — без буквы, бизнес — c, первый — f
CLASS_TITLE = {"business": "БИЗНЕС-КЛАСС", "economy": "ЭКОНОМ", "first": "ПЕРВЫЙ КЛАСС"}


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
    # компактно: так файл в репозитории в разы меньше
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


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
    resp = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload, timeout=60)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data["result"]


def cap(s):
    return s[:1].upper() + s[1:]


def arrow(d):
    return "→" if d["oneway"] else "⇄"


# ---------- цены ----------

def tp_get(path, params):
    params = dict(params, token=TP_TOKEN)
    resp = requests.get("https://api.travelpayouts.com" + path, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", True):
        raise RuntimeError(f"API error: {data}")
    return data.get("data", [])


def search_link(origin, dest, dep, ret, trip_class):
    """Ссылка на поиск Aviasales с уже выбранным классом: /search/MOW1001HKT2401c1 (без обратной даты — в одну сторону)."""
    path = f"/search/{origin}{dep[8:10]}{dep[5:7]}{dest}"
    if ret:
        path += f"{ret[8:10]}{ret[5:7]}"
    return "https://www.aviasales.ru" + path + CLASS_LETTER.get(trip_class, "") + "1"


def fetch_month(cfg, origin, month, cls, oneway):
    """Все направления из города за месяц одним запросом (по 1000 строк на страницу)."""
    offers = []
    for page in (1, 2, 3):
        raw = tp_get("/aviasales/v3/get_latest_prices", {
            "origin": origin, "currency": cfg["currency"],
            "period_type": "month", "beginning_of_period": month + "-01",
            "one_way": "true" if oneway else "false",
            "trip_class": TRIP_CLASS[cls], "show_to_affiliates": "false",
            "sorting": "price", "limit": 1000, "page": page,
        })
        for d in raw:
            if d.get("trip_class") not in (None, TRIP_CLASS[cls]):
                continue
            dep, ret, dest = d.get("depart_date"), d.get("return_date"), d.get("destination")
            if not dep or not dest:
                continue
            if oneway:
                ret = None
            elif not ret:
                continue
            offers.append({"destination": dest, "price": d["value"], "departure_at": dep,
                           "return_at": ret, "transfers": d.get("number_of_changes", 0),
                           "link": search_link(origin, dest, dep, ret, TRIP_CLASS[cls])})
        if len(raw) < 1000:
            break
        time.sleep(0.3)
    return offers


def filter_offers(cfg, offers, oneway):
    out = []
    today = now().date()
    for o in offers:
        if o.get("transfers", 0) > cfg["max_transfers"]:
            continue
        dep = parse_dt(o["departure_at"]).date()
        if dep <= today + timedelta(days=1):
            continue
        if not oneway:
            nights = (parse_dt(o["return_at"]).date() - dep).days
            if nights < cfg["min_nights"] or nights > cfg["max_nights"]:
                continue
            o["nights"] = nights
        out.append(o)
    return sorted(out, key=lambda o: o["price"])


# ---------- партнёрские ссылки ----------

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


# ---------- картинки ----------

def pick_photo(state, dest):
    """Красивое фото направления с Pixabay (бесплатно, можно в коммерческих целях).
    Возвращает байты картинки — скачиваем сами и загружаем в Telegram. Повторов избегаем."""
    if not PIXABAY_KEY or not dest.get("photo"):
        return None
    try:
        resp = requests.get("https://pixabay.com/api/",
                            params={"key": PIXABAY_KEY, "q": dest["photo"], "image_type": "photo",
                                    "orientation": "horizontal", "safesearch": "true",
                                    "order": "popular", "per_page": 30},
                            timeout=30)
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
    except Exception as e:
        print(f"Pixabay недоступен ({e}), пост будет без картинки")
        return None
    used = state.setdefault("photos_used", {}).setdefault(dest["code"], [])
    fresh = [h for h in hits if h["id"] not in used] or hits
    if not fresh:
        return None
    h = random.choice(fresh[:15])
    used.append(h["id"])
    del used[:-20]
    try:
        img = requests.get(h.get("largeImageURL") or h["webformatURL"], timeout=60)
        img.raise_for_status()
        return img.content
    except Exception as e:
        print(f"Не скачал картинку ({e}), пост будет без картинки")
        return None


# ---------- текст поста ----------

def template_body(d):
    o = d["offer"]
    ratio = o["price"] / d["economy"] if d.get("economy") else None
    if d["cls"] == "business" and ratio and ratio <= 1.5:
        head = f"🔥 {cap(d['from'])} {d['to']} в бизнесе почти по цене эконома"
    elif d["cls"] == "business" and ratio and ratio <= 2.05:
        head = f"💺 {cap(d['from'])} {d['to']} в бизнесе — всего вдвое дороже эконома"
    elif d["cls"] == "business":
        head = f"💺 Выгодный бизнес {d['from']} {d['to']}"
    else:
        head = f"🔥 Дешёвые билеты {d['from']} {d['to']}"
    if d["oneway"]:
        dates = f"📅 вылет {ru_date(o['departure_at'])}"
        price = f"💰 {rub(o['price'])} в одну сторону"
    else:
        dates = f"📅 {ru_date(o['departure_at'])} — {ru_date(o['return_at'])} ({o['nights']} ноч.)"
        price = f"💰 {rub(o['price'])} туда-обратно"
    lines = [head, "",
             f"✈️ {d['origin_name']} {arrow(d)} {d['name']}", dates, price,
             "🔄 прямой рейс" if o.get("transfers", 0) == 0 else f"🔄 пересадок: {o['transfers']}"]
    if d.get("usual"):
        lines.append(f"📉 Обычно на этом направлении около {rub(d['usual'])}")
    if d["cls"] == "business" and d.get("economy"):
        lines.append(f"Для сравнения: эконом сейчас от {rub(d['economy'])}")
    lines += ["", "⚡ Такие цены быстро уходят — проверяйте актуальность по кнопке."]
    return "\n".join(lines)


def claude_body(d):
    o = d["offer"]
    facts = {
        "направление": f"{d['origin_name']} {arrow(d)} {d['name']} (в тексте пиши именно через {arrow(d)})",
        "тип билета": "в одну сторону" if d["oneway"] else "туда и обратно",
        "класс": "бизнес" if d["cls"] == "business" else "эконом",
        "даты": f"вылет {ru_date(o['departure_at'])}" if d["oneway"]
                else f"{ru_date(o['departure_at'])} — {ru_date(o['return_at'])}, {o['nights']} ночей",
        "цена": rub(o["price"]) + (" в одну сторону" if d["oneway"] else " туда-обратно"),
        "пересадки": o.get("transfers", 0),
        "обычная цена на направлении": rub(d["usual"]) if d.get("usual") else "неизвестно",
        "эконом сейчас": rub(d["economy"]) if d.get("economy") else "неизвестно",
    }
    prompt = ("Напиши короткий пост для Telegram-канала о выгодных авиабилетах. "
              "Живо, без канцелярита и без кликбейта, 4–7 строк, пара уместных эмодзи. "
              "Строку с классом билета в начале не пиши — она добавится автоматически. "
              "Без markdown, без звёздочек. Используй только эти факты, ничего не выдумывай:\n"
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
    text = "".join(b.get("text", "") for b in resp.json()["content"]).strip()
    return re.sub(r"\*\*|__", "", text)


def build_post(d):
    """Готовый пост в HTML: первая строка — класс жирным."""
    body = template_body(d)
    if ANTHROPIC_KEY:
        try:
            body = claude_body(d)
        except Exception as e:
            print(f"Claude недоступен ({e}), использую шаблон")
    head = f"<b>{CLASS_TITLE.get(d['cls'], d['cls'].upper())}</b>"
    post = head + "\n\n" + html.escape(body, quote=False)
    if len(post) > CAPTION_LIMIT:
        post = head + "\n\n" + html.escape(template_body(d), quote=False)
    return post


def send_draft(state, d, dests):
    post = build_post(d)
    url = partner_link(d["offer"]["link"])
    label = "💺 Смотреть бизнес-класс" if d["cls"] == "business" else "✈️ Смотреть билеты"
    keyboard = {"inline_keyboard": [
        [{"text": "✅ Опубликовать", "callback_data": "pub"},
         {"text": "❌ Пропустить", "callback_data": "skip"}],
        [{"text": label, "url": url}]]}
    photo = pick_photo(state, dests[d["dest_code"]])
    if photo:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                data={"chat_id": ADMIN_CHAT, "caption": post, "parse_mode": "HTML",
                      "reply_markup": json.dumps(keyboard, ensure_ascii=False)},
                files={"photo": ("photo.jpg", photo, "image/jpeg")}, timeout=120)
            if not resp.json().get("ok"):
                raise RuntimeError(resp.json())
            return
        except Exception as e:
            print(f"Не удалось отправить с картинкой ({e}), отправляю текстом")
    tg("sendMessage", chat_id=ADMIN_CHAT, text=post, parse_mode="HTML",
       disable_web_page_preview=True, reply_markup=keyboard)


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
    state.pop("drafts", None)       # кнопки теперь обрабатывает Cloudflare, черновики хранить не нужно
    state.pop("tg_offset", None)
    hist_all = state.setdefault("history", {})
    proposed = state.setdefault("proposed", {})
    dests = {d["code"]: d for d in cfg["destinations"]}
    trip_types = cfg.get("trip_types", ["roundtrip"])
    candidates = []
    stats = {}          # город -> сколько вариантов
    cheapest = {}       # (город, направление, класс, тип) -> лучшая цена за все месяцы

    for org in cfg["origins"]:
        for month in months_ahead(cfg["months_ahead"]):
            for trip in trip_types:
                oneway = trip == "oneway"
                best_by = {}   # (класс, направление) -> лучшее предложение
                for cls in cfg["classes"]:
                    try:
                        offers = filter_offers(cfg, fetch_month(cfg, org["code"], month, cls, oneway), oneway)
                    except Exception as e:
                        print(f"[{org['code']} {month} {cls} {trip}] ошибка: {e}")
                        continue
                    st = stats.setdefault(org["code"], {"all": 0, "ours": 0})
                    st["all"] += len(offers)
                    for o in offers:   # уже отсортированы по цене
                        if o["destination"] in dests:
                            st["ours"] += 1
                            best_by.setdefault((cls, o["destination"]), o)
                    time.sleep(0.3)

                for code, dest in dests.items():
                    economy = best_by.get(("economy", code))
                    economy_best = economy["price"] if economy else None
                    for cls in cfg["classes"]:
                        best = best_by.get((cls, code))
                        if not best:
                            continue
                        ck = (org["code"], code, cls, trip)
                        if ck not in cheapest or best["price"] < cheapest[ck]["price"]:
                            cheapest[ck] = {"price": best["price"], "org": org, "dest": dest,
                                            "economy": economy_best, "oneway": oneway}
                        key = f"{org['code']}-{code}-{month}-{cls}" + ("-ow" if oneway else "")
                        hist = hist_all.setdefault(key, [])
                        reasons, median = is_deal(cfg, cls, best, hist, economy_best)
                        hist.append({"ts": now().isoformat(timespec="minutes"), "price": best["price"]})
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
                        candidates.append({"key": key, "cls": cls, "oneway": oneway,
                                           "dest_code": code, "name": dest["name"], "to": dest["to"],
                                           "origin": org["code"], "origin_name": org["name"],
                                           "from": org["from"], "offer": best, "usual": median,
                                           "economy": economy_best, "score": score})
                        print(f"[{key}] находка: {rub(best['price'])} ({', '.join(reasons)})")

    candidates.sort(key=lambda c: -c["score"])
    picked, per_origin, seen_routes = [], {}, set()
    for c in candidates:
        if len(picked) >= cfg["max_drafts_per_run"]:
            break
        if per_origin.get(c["origin"], 0) >= cfg.get("max_drafts_per_origin", 99):
            continue
        route = (c["origin"], c["dest_code"], c["cls"], c["oneway"])
        if route in seen_routes:          # одно направление — один пост за запуск
            continue
        seen_routes.add(route)
        per_origin[c["origin"]] = per_origin.get(c["origin"], 0) + 1
        picked.append(c)
    for d in picked:
        try:
            send_draft(state, d, dests)
            proposed[d["key"]] = {"ts": now().isoformat(timespec="minutes"), "price": d["offer"]["price"]}
        except Exception as e:
            print(f"Не удалось отправить черновик {d['key']}: {e}")

    for code, st in stats.items():
        print(f"[{code}] вариантов: {st['all']}, по нашим направлениям: {st['ours']}")

    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    every = cfg.get("report", "every") == "every"
    if manual or every or (now().hour in (6, 7) and not state.get("report_day") == now().date().isoformat()):
        state["report_day"] = now().date().isoformat()
        send_report(cfg, stats, cheapest, len(candidates), len(picked))
    save_json(STATE_PATH, state)


def send_report(cfg, stats, cheapest, found, sent):
    total = sum(st["ours"] for st in stats.values())
    lines = ["🔎 Отчёт поиска",
             f"Городов проверено: {len(stats)} из {len(cfg['origins'])}, вариантов по нашим направлениям: {total}",
             f"Находок по правилам: {found}, отправлено черновиков: {sent}"]

    def row(v):
        return f"• {v['org']['name']} {'→' if v['oneway'] else '⇄'} {v['dest']['name']}: {rub(v['price'])}"

    for oneway, title in ((False, "туда-обратно"), (True, "в одну сторону")):
        eco = sorted([v for (o, d, c, t), v in cheapest.items() if c == "economy" and v["oneway"] == oneway],
                     key=lambda v: v["price"])[:5]
        if eco:
            lines += ["", f"Самый дешёвый эконом, {title}:"] + [row(v) for v in eco]
    bus = [v for (o, d, c, t), v in cheapest.items() if c == "business" and v["economy"]]
    bus = sorted(bus, key=lambda v: v["price"] / v["economy"])[:5]
    if bus:
        lines += ["", "Бизнес ближе всего к эконому:"]
        lines += [row(v) + f" (×{v['price'] / v['economy']:.1f} к эконому)" for v in bus]
    if not total:
        lines += ["", "⚠️ API не вернул ни одного варианта — проверьте токен Travelpayouts."]
    if not PIXABAY_KEY:
        lines += ["", "ℹ️ Картинки выключены: нет секрета PIXABAY_API_KEY."]
    tg("sendMessage", chat_id=ADMIN_CHAT, text="\n".join(lines), disable_web_page_preview=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "search"
    if mode == "search":
        run_search()
    else:
        print("Кнопки теперь обрабатывает Cloudflare Worker, этот режим больше не нужен.")
