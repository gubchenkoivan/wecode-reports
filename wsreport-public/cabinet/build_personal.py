"""Особисті кабінети фахівців: дані з output/dash (нічна вивантажка) → personal.json.

Кожен запис зашифровано паролем співробітника: PBKDF2-SHA256 (200 000 ітерацій) → AES-256-GCM.
Відкрито у файлі лише sha256 від логіна, сіль, iv і шифротекст — без пароля дані не прочитати.
Використання: python build_personal.py <dash_dir> <keys.json> <out.json> [<папка або JSON-файл з проєктами дашборду менеджерів>]
keys.json: {"Ім'я Прізвище": {"login": "...", "password": "..."}}
"""
import base64
import datetime as dt
import hashlib
import json
import os
import sys
from collections import Counter

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ITER = 200_000
DONE_STAGES = ("DONE", "TESTING BY CLIENT", "TESTING BY TEAM")


def load(d, name):
    with open(os.path.join(d, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def base_stage(s):
    s = (s or "").replace("(ІТС)", "").replace("(ИТС)", "").strip().upper()
    return s


def r1(x):
    return round(float(x or 0), 1)


def projects_of(name, d, month, today, p80, overdue_urls):
    """Проєкти співробітника: години по місяцях, частка в проєкті, менеджер, відкриті задачі."""
    rows = [t for t in d["emp_tasks"] if t["employee"] == name]
    months = sorted({t["month"] for t in d["emp_tasks"]})[-3:]
    clients = [c for c in d["clients"] if c["month"] == month]
    mgr = d.get("mgr", {})
    out = {}
    for t in rows:
        pr = t["project"] or "—"
        x = out.setdefault(pr, {"p": pr, "h": {m: 0.0 for m in months}, "tasks": set(), "open": 0, "late": 0, "total": {}})
        if t["month"] in x["h"]:
            x["h"][t["month"]] += float(t.get("fact_month") or 0)
        x["total"][t["url"]] = max(x["total"].get(t["url"], 0), float(t.get("fact_total") or 0))
        if t["month"] == month and t["state"] == "Открыта":
            x["open"] += 1
            if t["url"] in overdue_urls:
                x["late"] += 1
    my_month = sum(x["h"].get(month, 0) for x in out.values()) or 0
    res = []
    for pr, x in out.items():
        cs = [c for c in clients if c["project"] == pr]
        ph = sum(c["hours"] for c in cs)
        dh = [v for v in x["h"].values()]
        if not x["open"] and not any(dh):
            continue
        unpaid = [r for r in p80 if r["p"] == pr]
        res.append({"p": pr, "client": (cs[0]["client"] if cs else ""), "type": (cs[0]["type"] if cs else ""),
                    "mgr": mgr.get(pr, ""), "people": sum(c["people"] for c in cs) or None,
                    "h": {m: r1(v) for m, v in x["h"].items()}, "share_my": round(x["h"].get(month, 0) / my_month, 3) if my_month else 0,
                    "share_proj": round(x["h"].get(month, 0) / ph, 3) if ph else None, "proj_h": r1(ph) if ph else None,
                    "open": x["open"], "late": x["late"], "total": r1(sum(x["total"].values())),
                    "unpaid": r1(sum(r["h"] for r in unpaid)), "unpaid_n": len(unpaid)})
    res.sort(key=lambda r: (-r["h"].get(month, 0), -sum(r["h"].values()), -r["open"]))
    return {"months": months, "list": res}


def build_one(name, d, month, today):
    emp = next(r for r in d["employees"] if r["employee"] == name and r["month"] == month)
    tasks = [r for r in d["emp_tasks"] if r["employee"] == name and r["month"] == month]
    open_t = [t for t in tasks if t["state"] == "Открыта"]
    hist = sorted([r for r in d["emp_history"] if r["employee"] == name], key=lambda r: r["date"])
    daily = sorted([r for r in d["emp_daily"] if r["employee"] == name], key=lambda r: r["day"])[-31:]
    backlog = [b for b in d["backlog"] if b["employee"] == name and b["month"] == month]
    overrun = [o for o in d["overrun_tasks"] if o["owner"] == name and o["month"] == month]
    prev_month = (dt.date.fromisoformat(month + "-01") - dt.timedelta(days=1)).strftime("%Y-%m")
    prev = next((r for r in d["employees"] if r["employee"] == name and r["month"] == prev_month), None)

    def tinfo(t, **extra):
        x = {"t": t["task"], "u": t["url"], "p": t["project"], "st": t["stage"] or "", "pay": t.get("pay") or "",
             "dl": t.get("deadline"), "pl": t.get("plan_hours"), "fm": r1(t.get("fact_month")), "ft": r1(t.get("fact_total"))}
        x.update(extra)
        return x

    def overdue_days(t):
        if not t.get("deadline"):
            return 0
        try:
            return (today - dt.date.fromisoformat(t["deadline"][:10])).days
        except ValueError:
            return 0

    overdue = sorted([tinfo(t, od=overdue_days(t)) for t in open_t
                      if overdue_days(t) > 0 and base_stage(t["stage"]) not in DONE_STAGES],
                     key=lambda x: -x["od"])
    soon = sorted([tinfo(t, dd=-overdue_days(t)) for t in open_t
                   if t.get("deadline") and 0 <= -overdue_days(t) <= 5 and base_stage(t["stage"]) not in DONE_STAGES],
                  key=lambda x: x["dd"])
    nostage = [tinfo(t) for t in open_t if base_stage(t["stage"]) in ("", "ІТС", "ИТС")]
    nopay = [tinfo(t) for t in open_t if not t.get("pay") and not base_stage(t["stage"]).startswith("CANCEL")]
    pause = [tinfo(t) for t in open_t if base_stage(t["stage"]) == "PAUSE"]
    noest = [tinfo(t) for t in open_t if not t.get("plan_hours")
             and base_stage(t["stage"]) in ("IN PROGRESS", "PLANNED", "APPROVED", "UNDER APPROVAL")]
    planned_worked = [tinfo(t) for t in open_t if t.get("fact_month") and base_stage(t["stage"]) in ("PLANNED", "APPROVED", "UNDER APPROVAL", "")]
    over = sorted([{"t": o["task"], "u": o["url"], "p": o["project"], "pl": r1(o["plan_hours"]), "ft": r1(o["fact_hours"]),
                    "pct": round(100 * o["fact_hours"] / o["plan_hours"]) if o["plan_hours"] else None} for o in overrun],
                  key=lambda x: -(x["pct"] or 0))
    tails_work = sorted([b for b in backlog if b["state"] == "В работе"], key=lambda b: -b["hours"])
    tails_done = [b for b in backlog if "DONE" in b["state"] or "Приёмка" in b["state"]]
    stages = Counter(base_stage(t["stage"]) or "без етапу" for t in open_t)

    # ---------- 20/80: здані, але не оплачені задачі (DONE і TESTING BY CLIENT), як у дашборді менеджерів ----------
    pay_rows, seen = [], set()
    for t in tasks:
        bs = base_stage(t["stage"])
        if (bs.startswith("DONE") or bs.startswith("TESTING BY CLIENT")) and t["url"] not in seen:
            seen.add(t["url"])
            pay_rows.append({"t": t["task"], "u": t["url"], "p": t["project"], "st": t["stage"], "h": r1(t.get("fact_total")),
                             "m": (t.get("created") or "")[:7], "pay": t.get("pay") or ""})
    for b in backlog:
        bs = base_stage(b["stage"])
        if (bs.startswith("DONE") or bs.startswith("TESTING BY CLIENT")) and b["url"] not in seen:
            seen.add(b["url"])
            pay_rows.append({"t": b["task"], "u": b["url"], "p": b["project"], "st": b["stage"], "h": r1(b["hours"]),
                             "m": b.get("created_month") or "", "age": b.get("age_days"), "pay": b.get("pay") or ""})
    pay_rows.sort(key=lambda x: -x["h"])
    tot = sum(x["h"] for x in pay_rows) or 0
    acc = 0.0
    for x in pay_rows:
        x["top"] = tot > 0 and acc < 0.8 * tot
        acc += x["h"]
        x["cum"] = round(100 * acc / tot) if tot else 0

    # ---------- рекомендації (українською, з цифрами) ----------
    recs = []
    def add(sec, text, items=None):
        recs.append({"sec": sec, "text": text, "items": items or []})
    lead = emp["role"] in ("Руководитель", "PM")
    if emp["status"] != "Дотягивает" and emp["need_per_day"]:
        add("norm", "До особистої норми %d год потрібно в середньому %.1f год на день до кінця місяця (зараз %.1f год, прогноз %d%% норми)."
            % (emp["norm"], emp["need_per_day"], emp["counted_mtd"], round(100 * (emp["forecast_progress"] or 0))))
    elif emp["need_per_day"]:
        add("norm", "Ви йдете в норму: прогноз %d%% від %d год. Тримайте темп — у середньому %.1f год на день."
            % (round(100 * (emp["forecast_progress"] or 0)), emp["norm"], emp["need_per_day"]))
    if overdue:
        add("overdue", "Прострочених задач: %d (найдовша — %d дн.). Оновіть строк або етап, а якщо задача готова — переведіть у DONE." % (len(overdue), overdue[0]["od"]),
            overdue[:5])
    if soon:
        add("soon", "Найближчі 5 днів — строк по %d задач(і). Перевірте, чи встигаєте." % len(soon), soon[:5])
    if planned_worked:
        add("stage", "По %d задач(ах) уже є години, а етап ще не IN PROGRESS — переведіть в IN PROGRESS." % len(planned_worked), planned_worked[:5])
    if nostage:
        add("nostage", "Задач без етапу: %d. Проставте етап (PLANNED, IN PROGRESS або CANCELLED)." % len(nostage), nostage[:5])
    if nopay:
        add("nopay", "Задач без типу оплати: %d. Без нього години не потрапляють у звіти правильно — попросіть автора задачі або менеджера проставити." % len(nopay), nopay[:5])
    if pause:
        add("pause", "На паузі %d задач(і). Вирішіть по кожній: повернути в роботу, перенести в PLANNED або закрити CANCELLED." % len(pause), pause[:5])
    if over:
        add("overrun", "Перевищено оцінку по %d задач(ах). Попередьте менеджера проєкту, щоб погодити з клієнтом нову оцінку." % len(over), over[:5])
    if noest:
        add("estimate", "Без оцінки часу: %d задач(і) в роботі чи в плані. Оцінка допомагає не піти в мінус." % len(noest), noest[:5])
    if tails_work:
        h = sum(b["hours"] for b in tails_work)
        add("tails", "Хвости минулих місяців у роботі: %d задач, %.1f год. Їх закриваємо першими — починайте з найстаріших."
            % (len(tails_work), h), [{"t": b["task"], "u": b["url"], "p": b["project"], "st": b["stage"], "age": b["age_days"], "ft": r1(b["hours"])}
                                     for b in sorted(tails_work, key=lambda b: -b["age_days"])[:5]])
    if (emp.get("timer_share") or 0) < 0.5 and emp["hours_mtd"] > 0:
        add("timer", "Лише %d%% часу записано таймером. Таймер точніше рахує години і не дає їх загубити." % round(100 * emp["timer_share"]))
    if not recs:
        add("ok", "Усе в порядку: прострочок, задач без етапу чи типу оплати немає. Так тримати! 💚")

    return {
        "name": name, "dept": emp["department"], "role": emp["role"], "type": emp["emp_type"], "lead": lead,
        "month": month, "date": today.isoformat(),
        "k": {x: emp.get(x) for x in ("norm", "hours_mtd", "counted_mtd", "paid_mtd", "guarantee_mtd", "forecast", "norm_progress",
                                       "forecast_progress", "need_per_day", "status", "fund_util", "closed_mtd", "closed_yesterday",
                                       "hours_yesterday", "on_time_rate", "open_tasks", "overdue", "max_overdue_days",
                                       "timer_share", "wd_elapsed", "wd_total", "absent_days")},
        "prev": ({x: prev.get(x) for x in ("counted_mtd", "norm", "norm_progress", "status", "closed_mtd", "on_time_rate")} if prev else None),
        "hist": [{"d": h["date"], "f": h.get("forecast_progress"), "s": h.get("status"), "o": h.get("overdue")} for h in hist if h["month"] == month],
        "daily": [{"d": r["day"], "h": r1(r["hours"])} for r in daily],
        "stages": dict(stages),
        "p80": pay_rows,
        "projects": projects_of(name, d, month, today, pay_rows, {t["u"] for t in overdue}),
        "recs": recs,
        "tasks": sorted([tinfo(t, od=overdue_days(t)) for t in open_t], key=lambda x: (x["dl"] or "9999")),
        "tails": {"work": len(tails_work), "work_h": r1(sum(b["hours"] for b in tails_work)),
                  "done": len(tails_done), "done_h": r1(sum(b["hours"] for b in tails_done))},
    }


def enc(payload, login, password):
    salt, iv = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITER, 32)
    ct = AESGCM(key).encrypt(iv, json.dumps(payload, ensure_ascii=False).encode("utf-8"), None)
    b = lambda x: base64.b64encode(x).decode()
    return {"k": hashlib.sha256(("wecode:" + login.strip().lower()).encode()).hexdigest(), "s": b(salt), "iv": b(iv), "ct": b(ct)}


def main():
    dash_dir, keys_path, out_path = sys.argv[1:4]
    d = {n: load(dash_dir, n) for n in ("employees", "emp_tasks", "emp_history", "emp_daily", "backlog", "overrun_tasks", "clients")}
    mgr_dir = sys.argv[4] if len(sys.argv) > 4 else ""
    d["mgr"] = {}
    docs = []
    if mgr_dir and os.path.isdir(mgr_dir):
        for f in os.listdir(mgr_dir):
            try:
                docs.append(json.load(open(os.path.join(mgr_dir, f), encoding="utf-8")))
            except (ValueError, OSError):
                pass
    elif mgr_dir and os.path.isfile(mgr_dir):
        # один JSON-файл: список документів коллекції projects (як віддає ArtifactData list)
        j = json.load(open(mgr_dir, encoding="utf-8"))
        if isinstance(j, dict):
            j = j.get("documents") or j.get("docs") or j.get("items") or list(j.values())
        docs = j if isinstance(j, list) else []
    for j in docs:
        if not isinstance(j, dict):
            continue
        j = j.get("data", j)
        if isinstance(j, dict) and j.get("name") and j.get("manager"):
            d["mgr"][j["name"]] = j["manager"]
    month = max(r["month"] for r in d["employees"])
    today = max(dt.date.fromisoformat(r["date"]) for r in d["emp_history"])
    keys = json.load(open(keys_path, encoding="utf-8"))
    names = sorted({r["employee"] for r in d["employees"] if r["month"] == month})
    entries, built = [], []
    for name in names:
        if name not in keys:
            continue
        p = build_one(name, d, month, today)
        entries.append(enc(p, keys[name]["login"], keys[name]["password"]))
        built.append(name)
    entries.sort(key=lambda e: e["k"])
    json.dump({"v": 1, "generated": dt.datetime.now().isoformat(timespec="seconds"), "date": today.isoformat(), "month": month,
               "iter": ITER, "entries": entries}, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("кабінетів:", len(built), "з", len(names))


if __name__ == "__main__":
    main()
