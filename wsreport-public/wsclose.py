"""We.Bit · автоматичне закриття місяця й розрахунок ЗП (за остаточним знімком PAID\\CLOSE).

Коли: 1-го числа о 03:40 за Києвом, одразу після щоденного знімка (wspay.py) — закривається попередній місяць.
Вручну: PAY_MODE=close (і за потреби PAY_MONTH=YYYY-MM).

Вхідні дані (усе зашифровано в pay.enc, папка output/pay):
  tasks_state.json            — задачі в PAID\\CLOSE на момент знімка (з wspay.py);
  terms.json                  — умови співробітників, плани відділів, виплати на картки, офіційна частина тощо;
  saldo.json                  — стартове сальдо {"YYYY-MM": {key: год}} (сальдо на кінець місяця);
  inputs/YYYY-MM.json         — ручні дані місяця від Івана: години старої системи, компенсації, особливі нарахування;
  closing/YYYY-MM.json        — результат закриття (сальдо на кінець місяця береться звідси для наступного).

Правила (погоджено з Іваном, листопад 2026):
  * години — по тому, хто записав; записи < 10 хв не рахуються (уже в tasks_state);
  * «Оплачувана» та ІТС → у премію; «Не оплачувана» → рухає сальдо, не оплачується; «Гарантія» — лише показник;
    без мітки виду оплати — список на рішення Івана (не рахується);
  * стара система — години з inputs (факт), усі оплачувані;
  * повна зайнятість: сальдо = сальдо на кінець минулого місяця + опл. + неопл. + стара система − норма;
    премія лише при сальдо > 0: min(сальдо, опл. + стара) × ставка, після цього сальдо = 0;
  * часткова зайнятість: (опл. + стара) × ставка, без сальдо; неоплачувані години йдуть у план відділу;
  * керівник з планом відділу: сальдо відділу = старе + усі години відділу (вкл. керівника, PT, PM) − план;
    премія = додатне сальдо × premium_per_hour (10% × (1400 − 1000) = 40), потім 0;
  * виплачено на картку (56 700) і офіційна частина (6 699) віднімаються; через посередника — його винагорода
    (Степура М.П. 6 900): повернення = 56 700 − нараховано − 6 900; компенсації соцпакету додаються;
  * повтори: назва задачі збігається з задачею минулих закриттів — залишаються, але ведеться облік;
  * задачі, уже включені в затверджене закриття (task_ids), удруге не рахуються, навіть якщо ще в PAID\\CLOSE.

Нічого не пише у Worksection. Репозиторій публічний: у лог — лише кількості.
"""
import datetime as dt
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output", "pay")
WS = "https://wecode.worksection.com"


def rj(path, default=None):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def wj(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def r2(x):
    return round(float(x or 0) + 1e-9, 2)


def prev_month(m):
    y, mm = map(int, m.split("-"))
    return "%04d-%02d" % ((y, mm - 1) if mm > 1 else (y - 1, 12))


def person_terms(p, month):
    """Умови людини на місяць: базові + зміни з полем from <= month (останні перемагають)."""
    t = {k: v for k, v in p.items() if k != "changes"}
    for ch in sorted(p.get("changes") or [], key=lambda c: c["from"]):
        if ch["from"] <= month:
            t.update({k: v for k, v in ch.items() if k not in ("from", "note")})
    return t


def active(t, month):
    return (t.get("start") or "0000-00")[:7] <= month and (not t.get("end") or t["end"][:7] >= month)


def calc(month, tasks, terms, saldo_start, inp, history_names=None):
    inp = inp or {}
    old = inp.get("old_hours") or {}
    gym = inp.get("compensations") or {}
    override = inp.get("accrual_override") or {}
    prorate = inp.get("prorate") or {}
    excluded_tasks = set(str(x) for x in inp.get("exclude_tasks") or [])

    # ---- години з Worksection по людях
    hrs = defaultdict(lambda: {"paid": 0.0, "unpaid": 0.0, "guarantee": 0.0, "none": 0.0})
    tasks_by = defaultdict(list)
    manual = []
    for tid, t in tasks.items():
        if tid in excluded_tasks:
            continue
        kind = t.get("pay") or "none"
        if kind == "its_no_paid":       # ІТС — завжди оплачувана
            kind = "paid"
        if kind == "none" and t.get("hours"):
            manual.append({"id": tid, "name": t.get("name"), "url": WS + (t.get("page") or ""), "project": t.get("project"),
                           "hours": t.get("hours"), "by_user": t.get("by_user")})
        for email, h in (t.get("by_user") or {}).items():
            key = email.split("@")[0].lower()
            hrs[key][kind] += h
            tasks_by[key].append({"id": tid, "t": t.get("name"), "u": WS + (t.get("page") or ""), "p": t.get("project"),
                                  "pay": kind, "h": r2(h), "since": t.get("paid_since"),
                                  "rep": bool(history_names and (t.get("name") or "").strip() in history_names)})

    people, questions = [], []
    known = {p["key"] for p in terms["people"]}
    for k in sorted(set(hrs) - known):
        if any(hrs[k].values()):
            questions.append("%s: є години в PAID\\CLOSE, але немає умов у terms.json" % k)
    for p in terms["people"]:
        t = person_terms(p, month)
        if not active(t, month):
            continue
        k = t["key"]
        h = hrs.get(k, {"paid": 0, "unpaid": 0, "guarantee": 0, "none": 0})
        o = sum(x["h"] for x in old.get(k, [])) if isinstance(old.get(k), list) else float(old.get(k) or 0)
        oklad, norm = float(t.get("oklad") or 0), t.get("norm")
        if k in prorate:
            f = prorate[k]["days"] / prorate[k]["of"]
            oklad, norm = round(oklad * f, 2), round(float(norm or 0) * f, 2)
        row = {"key": k, "name": t["name"], "cabinet": t.get("cabinet"), "level": t.get("level", "spec"), "head": t.get("head", ""),
               "dept": t.get("dept", ""), "type": t["type"], "grade": t.get("grade", ""), "oklad": oklad, "norm": norm,
               "rate": t.get("rate"), "paid": r2(h["paid"]), "unpaid": r2(h["unpaid"]), "guarantee": r2(h["guarantee"]),
               "old": r2(o), "s0": None, "s_calc": None, "s1": None, "premium_h": 0, "premium": 0, "hourly": 0, "accrual": 0,
               "note": t.get("note", "")}
        if t["type"] == "F":
            s0 = float(saldo_start.get(k, t.get("saldo_start", 0)) or 0)
            sc = s0 + row["paid"] + row["unpaid"] + row["old"] - float(norm or 0)
            ph = min(max(sc, 0), row["paid"] + row["old"])
            row.update(s0=r2(s0), s_calc=r2(sc), s1=r2(0 if sc > 0 else sc), premium_h=r2(ph),
                       premium=round(ph * float(t["rate"])), accrual=round(oklad + round(ph * float(t["rate"])), 2))
        elif t["type"] == "PT":
            if t.get("rate") is None:
                questions.append("%s: не задано погодинну ставку — нараховано 0" % t["name"])
            row.update(hourly=round((row["paid"] + row["old"]) * float(t.get("rate") or 0)))
            row["accrual"] = row["hourly"]
        elif t["type"] in ("FIX", "OWNER", "PM"):
            row["accrual"] = float(t.get("oklad") or 0)
        row["accrual"] = float(override.get(k, row["accrual"]))
        row["tasks"] = sorted(tasks_by.get(k, []), key=lambda x: -x["h"])
        row["old_items"] = old.get(k) if isinstance(old.get(k), list) else []
        people.append(row)

    # ---- керівники з планом відділу
    for code, plan in (terms.get("plans") or {}).items():
        dept_h = sum(r["paid"] + r["unpaid"] + r["old"] for r in people if r["dept"] == code)
        for r in people:
            if r["key"] == plan["head"]:
                s0 = float(saldo_start.get("dept:" + code, plan.get("saldo_start", 0)) or 0)
                sc = s0 + dept_h - plan["hours"]
                prem = round(max(sc, 0) * plan["premium_per_hour"])
                r.update(type="H", norm=plan["hours"], rate=plan["premium_per_hour"], dept_hours=r2(dept_h), s0=r2(s0), s_calc=r2(sc),
                         s1=r2(min(sc, 0)), premium_h=r2(max(sc, 0)), premium=prem, accrual=round(r["oklad"] + prem, 2))

    # ---- взаєморозрахунки
    cards = terms.get("card_payments") or {}
    for r in people:
        k = r["key"]
        r["comp"] = float(gym.get(k, 0))
        r["card"] = float(cards.get("amount", 0)) if k in cards.get("keys", []) else 0.0
        r["card"] = float((inp.get("card_override") or {}).get(k, r["card"]))
        r["official"] = float((terms.get("official_part") or {}).get(k, 0))
        via = (terms.get("card_via") or {}).get(k)
        r["fee"] = float(via["fee"]) if via else 0.0
        r["via"] = via["name"] if via else ""
        r["total"] = round(r["accrual"] + r["comp"], 2)
        r["to_pay"] = round(r["total"] - r["card"] - r["official"] + r["fee"], 2)

    saldo_end = {r["key"]: r["s1"] for r in people if r["type"] == "F"}
    for code, plan in (terms.get("plans") or {}).items():
        hr = next((r for r in people if r["key"] == plan["head"]), None)
        if hr:
            saldo_end["dept:" + code] = hr["s1"]
    tot = lambda f: round(sum(r[f] for r in people), 2)
    return {
        "month": month, "people": people, "manual": manual, "saldo_end": saldo_end, "questions": questions,
        "totals": {"accrual": tot("accrual"), "comp": tot("comp"), "total": tot("total"), "card": tot("card"),
                   "official": tot("official"), "fee": tot("fee"), "to_pay": round(sum(r["to_pay"] for r in people if r["to_pay"] > 0), 2),
                   "to_return": round(sum(r["to_pay"] for r in people if r["to_pay"] < 0), 2), "net": tot("to_pay")},
        "return_card": terms.get("return_card", ""),
        "repeats": [dict(x, who=r["name"]) for r in people for x in r["tasks"] if x["rep"]],
        "task_ids": sorted(tasks.keys()),
        "tasks_hours": round(sum(t.get("hours") or 0 for t in tasks.values()), 2),
    }


def history_task_names(month):
    names = set()
    d = os.path.join(OUT, "closing")
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.endswith(".json") and f[:7] < month:
                c = rj(os.path.join(d, f), {})
                for r in c.get("people", []):
                    names.update((x.get("t") or "").strip() for x in r.get("tasks", []))
                names.update(c.get("extra_repeat_names", []))
    names.update((rj(os.path.join(OUT, "repeat_names.json"), {}) or {}).get(month, []))
    names.discard("")
    return names


def write_xlsx(res, path):
    """Відомість у форматі «Виплата ЗП» (значення; формули — у ручній версії)."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        print("openpyxl відсутній — Excel пропущено")
        return
    wb = openpyxl.Workbook()
    F = "Arial"
    hdr = Font(name=F, bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="1F3864")
    grp = PatternFill("solid", fgColor="D9E1F2")
    NUM = '#,##0.00;-#,##0.00;"-"'
    ws = wb.active
    ws.title = "Виплата " + res["month"]
    cols = ["Рівень", "Співробітник", "Нараховано", "Компенсація", "Разом", "Виплачено на картку", "Офіційна частина",
            "Винагорода посередника", "До виплати (+) / повернути (−)", "Куди повернути"]
    ws.append(cols)
    for c in ws[1]:
        c.font, c.fill, c.alignment = hdr, fill, Alignment(wrap_text=True)
    order = [("commercial", "1. Комерційний відділ"), ("head", "2. Керівники"), ("spec", "3. Технічні спеціалісти")]
    for lvl, title in order:
        ws.append([title])
        for c in ws[ws.max_row]:
            c.fill, c.font = grp, Font(name=F, bold=True)
        for r in [x for x in res["people"] if (x["level"] if x["level"] != "owner" else "head") == lvl]:
            ws.append([r["level"] + (" (" + r["head"] + ")" if lvl == "spec" else ""), r["name"], r["accrual"], r["comp"], r["total"],
                       r["card"], r["official"], r["fee"], r["to_pay"], ("повернути на картку " + res["return_card"]) if r["to_pay"] < 0 else ""])
    t = res["totals"]
    ws.append(["", "РАЗОМ", t["accrual"], t["comp"], t["total"], t["card"], t["official"], t["fee"], t["net"]])
    ws.append(["", "у т.ч. до виплати", None, None, None, None, None, None, t["to_pay"]])
    ws.append(["", "у т.ч. до повернення", None, None, None, None, None, None, t["to_return"]])
    ws.append(["", "Повернення робиться на картку: " + res["return_card"]])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            if c.column >= 3 and c.column <= 9:
                c.number_format = NUM
            if not c.font or c.font.name != F:
                c.font = Font(name=F, bold=bool(c.font and c.font.bold))
    w2 = wb.create_sheet("Розрахунок спеціалістів")
    w2.append(["Співробітник", "Керівник", "Тип", "Опл.", "Неопл.", "Стара система", "Норма/план", "Сальдо поч.", "Години відділу",
               "Сальдо розрах.", "Сальдо кін.", "Оклад", "Ставка", "Премія / погодинна", "Нараховано"])
    for r in res["people"]:
        if r["level"] in ("commercial", "owner"):
            continue
        w2.append([r["name"], r["head"], r["type"], r["paid"], r["unpaid"], r["old"], r["norm"], r["s0"], r.get("dept_hours"),
                   r["s_calc"], r["s1"], r["oklad"], r["rate"], r["premium"] or r["hourly"], r["accrual"]])
    w3 = wb.create_sheet("Облік повторів")
    w3.append(["Задача", "Проєкт", "Співробітник", "Год", "Посилання"])
    for x in res["repeats"]:
        w3.append([x["t"], x["p"], x["who"], x["h"], x["u"]])
    w4 = wb.create_sheet("Без мітки оплати")
    w4.append(["Задача", "Проєкт", "Год", "Посилання"])
    for x in res["manual"]:
        w4.append([x["name"], x["project"], x["hours"], x["url"]])
    for s in wb.worksheets:
        for i, wdt in enumerate([34, 40, 14, 12, 12, 14, 12, 14, 16, 40, 12, 12, 10, 14, 14], 1):
            s.column_dimensions[openpyxl.utils.get_column_letter(i)].width = wdt
    wb.save(path)


def main():
    today = dt.date.today()
    month = os.environ.get("PAY_MONTH") or prev_month(today.strftime("%Y-%m"))
    terms = rj(os.path.join(OUT, "terms.json"))
    if not terms:
        sys.exit("Немає terms.json — розрахунок неможливий")
    cpath = os.path.join(OUT, "closing", month + ".json")
    cur = rj(cpath)
    if cur and cur.get("approved") and os.environ.get("PAY_FORCE") != "1":
        print("Закриття %s уже затверджене — не перераховую" % month)
        return
    tasks = rj(os.path.join(OUT, "tasks_state.json"), {})
    prevc = rj(os.path.join(OUT, "closing", prev_month(month) + ".json"))
    saldo_start = (prevc or {}).get("saldo_end") or (rj(os.path.join(OUT, "saldo.json"), {}) or {}).get(prev_month(month), {})
    inp = rj(os.path.join(OUT, "inputs", month + ".json"), {}) or {}
    # задачі, уже оплачені в затверджених закриттях, але ще не переведені в ⛔️Definitively closed, — не рахуємо вдруге
    paid_before = set()
    cdir = os.path.join(OUT, "closing")
    for f in sorted(os.listdir(cdir)) if os.path.isdir(cdir) else []:
        if f.endswith(".json") and f[:7] < month:
            c = rj(os.path.join(cdir, f), {})
            if c.get("approved"):
                paid_before.update(str(x) for x in c.get("task_ids", []))
    already = sorted(set(tasks) & paid_before)
    tasks = {k: v for k, v in tasks.items() if k not in paid_before}
    res = calc(month, tasks, terms, saldo_start, inp, history_task_names(month))
    res["already_paid_still_open"] = already
    res.update({"generated": dt.datetime.now().isoformat(timespec="seconds"), "approved": False,
                "snapshot": (rj(os.path.join(OUT, "meta.json"), {}) or {}).get("last_run")})
    wj(cpath, res)
    write_xlsx(res, os.path.join(OUT, "closing", "Виплата ЗП %s.xlsx" % month))
    print("Закриття %s: людей %d, задач %d, без мітки %d, повторів %d, уже оплачених (виключено) %d"
          % (month, len(res["people"]), len(res["task_ids"]), len(res["manual"]), len(res["repeats"]), len(already)))


if __name__ == "__main__":
    main()
