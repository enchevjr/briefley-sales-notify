"""Нови заявки за демо от детски градини → Notion („Градини – срещи и демо“).

Чете GET /kindergarten-bookings (Briefley Analytics API) и за всяко записване, чието id го няма
в базата „Градини“ (свойство „ID записване“), създава:
  - страница в „Градини“;
  - два реда в „Разговори“, свързани с градината: „Записване от сайта“ и „Демо разговор“.
Съществуващи страници НИКОГА не се обновяват (бележките на Ясен остават непокътнати).
Telegram известията (kg.py) не зависят от този скрипт.

Репото е публично: в логовете само броеве и id-та – никакви имена, телефони, имейли.

  --loop N           повтаря проверката всяка минута N минути (като notify.py) – новите
                     заявки влизат в Notion до ~1 мин след известието в Telegram.
  --dry-run          нищо не създава; показва само кои id-та са нови.
                     Без NOTION_TOKEN сравнява с --known-ids (напр. --known-ids 20-31,34-40).
"""
import datetime as dt, os, sys, time
from zoneinfo import ZoneInfo
import requests

DRY = "--dry-run" in sys.argv
SOFIA = ZoneInfo("Europe/Sofia")
SKIP_IDS = {32, 33}                                   # тестови записвания на Ясен

E = dict(os.environ)
cfg = os.path.expanduser("~/.kidsapp/keys/config.env")
if os.path.exists(cfg):                                # локално; в GitHub Actions идват от secrets
    for line in open(cfg):
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); E.setdefault(k.strip(), v.strip())

API = E.get("BRIEFLEY_ANALYTICS_URL", "https://www.briefley.com/analytics/v1").rstrip("/")
H_API = {"Authorization": f"Bearer {E.get('BRIEFLEY_ANALYTICS_KEY', '').strip()}"}
NOTION = "https://api.notion.com/v1"
NOTION_TOKEN = E.get("NOTION_TOKEN", "").strip()
H_N = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
# database id-та (от URL-ите на базите), не data source id-та
DB_KG = E.get("NOTION_KG_DB", "41ca8756667781309759feb14de35a81")      # „Градини“
DB_TALKS = E.get("NOTION_TALKS_DB", "41ca8756667781e1aad6e88e38cc748e")  # „Разговори“

SOURCES = {"fb_kg_direktori": "Реклама · Директори", "fb_kg_kratak": "Реклама · Кратък",
           "fb_kg_tri_momenta": "Реклама · Три момента", "fb_kg_zaspiva": "Реклама · Заспива",
           "fb_kg_krag": "Реклама · Кръг"}


def req(method, url, headers, **kw):
    for i in range(5):
        r = requests.request(method, url, headers=headers, timeout=60, **kw)
        if r.status_code == 429:
            time.sleep(int(float(r.headers.get("Retry-After", "5")))); continue
        if r.status_code >= 500:
            time.sleep(5 * (i + 1)); continue
        if r.status_code >= 400:                       # само кодът – тялото може да съдържа данни
            raise RuntimeError(f"HTTP {r.status_code} {method} {url.split('?')[0].replace(API, '<api>')}")
        return r.json()
    raise RuntimeError(f"HTTP {r.status_code} след повторения")


def bookings():
    out, cur = [], None
    while True:
        p = {"limit": 1000}
        if cur: p["cursor"] = cur
        j = req("GET", f"{API}/kindergarten-bookings", H_API, params=p)
        out += j.get("data", []); cur = j.get("next_cursor")
        if not cur: return out


def notion_ids():
    """Всички стойности на „ID записване“ в „Градини“ (вкл. ръчно добавени)."""
    ids, cur = set(), None
    while True:
        body = {"page_size": 100}
        if cur: body["start_cursor"] = cur
        j = req("POST", f"{NOTION}/databases/{DB_KG}/query", H_N, json=body)
        for pg in j.get("results", []):
            rt = pg["properties"].get("ID записване", {}).get("rich_text", [])
            s = "".join(t.get("plain_text", "") for t in rt).strip()
            if s.isdigit(): ids.add(int(s))
        if not j.get("has_more"): return ids
        cur = j["next_cursor"]


def parse_ids(s):
    ids = set()
    for part in filter(None, s.split(",")):
        a, _, b = part.partition("-")
        ids.update(range(int(a), int(b or a) + 1))
    return ids


def txt(s): return {"rich_text": [{"text": {"content": str(s)[:2000]}}]} if s not in (None, "") else None


def meeting_local(b):
    try:
        d = dt.datetime.fromisoformat(f"{b['meeting_date']}T{b['meeting_time']}")
        return d.replace(tzinfo=SOFIA).isoformat()
    except Exception:
        return b.get("meeting_at")


def kg_props(b):
    pending = b.get("status") == "pending"
    when = meeting_local(b)
    p = {
        "Градина": {"title": [{"text": {"content": (b.get("kindergarten") or "—")[:2000]}}]},
        "Етап": {"status": {"name": "Записан час" if pending else "Проведена демо"}},
        "Контакт": txt(b.get("name")),
        "Длъжност": txt(b.get("role_label")),
        "Телефон": {"phone_number": str(b["phone"])} if b.get("phone") else None,
        "Имейл": {"email": b["email"]} if b.get("email") else None,
        "Деца / групи": txt(f"{b['groups']} групи") if b.get("groups") is not None else None,
        "Източник": {"select": {"name": SOURCES.get(b.get("source") or "", "Директно от сайта")}},
        "ID записване": txt(b["id"]),
        "Час за демо": {"date": {"start": when}} if when else None,
        "Кога": {"date": {"start": when}} if when else None,
        "Записано на": {"date": {"start": b["created_at"]}} if b.get("created_at") else None,
        "Следващ ход": txt("Демо разговор") if pending else None,
    }
    return {k: v for k, v in p.items() if v is not None}


def talk(title, kind, date, page_id, result=None):
    p = {"Разговор": {"title": [{"text": {"content": title}}]}, "Вид": {"select": {"name": kind}},
         "Градина": {"relation": [{"id": page_id}]}}
    if date: p["Дата"] = {"date": {"start": date}}
    if result: p["Резултат"] = {"select": {"name": result}}
    return {"parent": {"database_id": DB_TALKS}, "properties": p}


def main():
    rows = [b for b in bookings() if b.get("id") not in SKIP_IDS]
    if NOTION_TOKEN:
        known = notion_ids()
    elif DRY:
        arg = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--known-ids=")), "")
        known = parse_ids(arg)
        print(f"notion: без NOTION_TOKEN – сравнявам с --known-ids ({len(known)} id-та)")
    else:
        print("notion: няма NOTION_TOKEN – пропускам"); return
    new = sorted((b for b in rows if b["id"] not in known), key=lambda b: b["id"])
    print(f"notion: заявки в API {len(rows)}, вече в Notion {len(known)}, нови {len(new)}: {[b['id'] for b in new]}")
    if DRY: return
    made = 0
    for b in new:
        try:
            pg = req("POST", f"{NOTION}/pages", H_N, json={"parent": {"database_id": DB_KG}, "properties": kg_props(b)})
        except Exception as ex:
            print(f"notion: грешка при градина id={b['id']}:", ex); continue
        pending = b.get("status") == "pending"
        for t in (talk("Записване от сайта", "Записване от сайта", b.get("created_at"), pg["id"]),
                  talk("Демо разговор", "Демо", meeting_local(b), pg["id"], "Предстои" if pending else None)):
            try:
                req("POST", f"{NOTION}/pages", H_N, json=t)
            except Exception as ex:
                print(f"notion: грешка при разговор за id={b['id']}:", ex)
        made += 1; print(f"notion: внесено id={b['id']}")
    print(f"notion: внесени {made} от {len(new)}")


if __name__ == "__main__":
    mins = int(sys.argv[sys.argv.index("--loop") + 1]) if "--loop" in sys.argv else 0
    end = time.time() + mins * 60
    while True:
        try:
            main()
        except Exception as ex:                        # само вида на грешката – без данни
            print("notion: грешка:", type(ex).__name__, str(ex)[:120] if isinstance(ex, RuntimeError) else "")
        if time.time() + 60 > end: break
        time.sleep(60)
