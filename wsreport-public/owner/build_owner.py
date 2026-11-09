"""Дашборд власника We.Code: зведені дані по керівниках напрямів і комерції → набори даних (JSON-рядки).

Вхід: output/dash (нічна вивантажка wsreport.py), output/managers (wsmanagers.py) і список проєктів
дашборду менеджерів (JSON-масив документів {id, data: {name, manager, internal}}, як віддає ArtifactData list).
Вихід — папка з файлами: leaders.json (рядок на керівника й місяць), months.json, trend.json, managers.json (рядок на менеджера й місяць), tails.json (хвости по проєктах і місяцях), hyg.json (порядок у задачах сьогодні), paid_daily.json, hist.json, about.json.

Використання: python build_owner.py <dash_dir> <managers_dir> <projects.json> <out_dir> [snap.json]
snap.json (необов'язково) — документ snap/<YYYY-MM> дашборду менеджерів: закріплення проєктів на кінець місяця.

Керівники:
  Марина Васильєва — Fuel Solutions + BAS Solutions D&I (план «Fuel + Корп внедрение»)
  Анна Воропаєва — Mobile
  Світлана Степура — Customer Support
  Іван Губченко — BAS LCW (Вайкики)
  Наталя Свідрук — комерція: усі менеджери проєктів разом
Утилізація = години місяця на задачі, які вже в PAID\\CLOSE (або ⛔️Definitively closed), ÷ усі години місяця.
"""
import datetime as dt
import json
import os
import sys
from collections import defaultdict

LEADERS = [
    {"key": "marina", "leader": "Марина Васильєва", "area": "Fuel Solutions + BAS Solutions D&I",
     "depts": ["Fuel Solutions", "BAS Solutions D&I"], "plan": "Fuel + Корп внедрение"},
    {"key": "anna", "leader": "Анна Воропаєва", "area": "Mobile", "depts": ["Mobile"], "plan": "Mobile"},
    {"key": "svitlana", "leader": "Світлана Степура", "area": "Customer Support (ІТС)",
     "depts": ["Customer Support"], "plan": "ИТС (Customer Support)"},
    {"key": "lcw", "leader": "Іван Губченко", "area": "BAS LCW (Вайкики)", "depts": ["BAS LCW"], "plan": "BAS LCW (Вайкики)"},
]
COMMERCE = {"key": "natalia", "leader": "Свідрук Наталя", "area": "Комерція: усі менеджери проєктів"}
FROM = "2026-08"


def load(d, n):
    with open(os.path.join(d, n + ".json"), encoding="utf-8") as f:
        return json.load(f)


def r1(x):
    return None if x is None else round(float(x), 1)


def pct(a, b):
    return round(a / b * 100) if b else None


def main():
    dash_dir, mgr_dir, proj_path, out_dir = sys.argv[1:5]
    snap = json.load(open(sys.argv[5], encoding="utf-8")) if len(sys.argv) > 5 and os.path.isfile(sys.argv[5]) else {}
    snap = snap.get("data", snap).get("assign", {}) if snap else {}
    os.makedirs(out_dir, exist_ok=True)

    deps = load(dash_dir, "departments")
    plans = load(dash_dir, "plans")
    hist = load(dash_dir, "history")
    emps = load(dash_dir, "employees")
    cur = max(r["month"] for r in deps)
    today = max(r["date"] for r in hist)
    months_it = sorted({r["month"] for r in deps})

    tk = load(mgr_dir, "tasks")
    tasks = tk["tasks"]
    entries = load(mgr_dir, "entries")
    pm = load(mgr_dir, "pm").get("staff", {})
    msnap = load(mgr_dir, "months")

    docs = json.load(open(proj_path, encoding="utf-8")) if os.path.isfile(proj_path) else []
    if isinstance(docs, dict):
        docs = docs.get("documents") or docs.get("docs") or list(docs.values())
    assign, internal = {}, set()
    for x in docs:
        x = x.get("data", x) if isinstance(x, dict) else {}
        if not x.get("name"):
            continue
        if x.get("manager"):
            assign[x["name"]] = x["manager"]
        if x.get("internal"):
            internal.add(x["name"])
    managers = sorted(set(assign.values()))

    # ---- години й оплачене (PAID\CLOSE) по напрямах, з записів часу
    dep_of = {}
    for r in sorted(emps, key=lambda r: r["month"]):
        dep_of[r["employee"]] = r["department"]
    byid = {str(t["id"]): t for t in tasks}
    lab_d, paid_d = defaultdict(float), defaultdict(float)
    for tid, es in entries.items():
        t = byid.get(tid)
        for e in es:
            m, who, h = e[0][:7], e[1], float(e[2] or 0)
            d = dep_of.get(who)
            if not d or m < FROM:
                continue
            lab_d[(d, m)] += h
            if t and t.get("s") == "PAID":
                paid_d[(d, m)] += h
    all_months = sorted({m for (_, m) in lab_d} | set(months_it))
    all_months = [m for m in all_months if FROM <= m <= cur]

    dep_row = {(r["department"], r["month"]): r for r in deps}
    plan_row = {(r["plan"], r["month"]): r for r in plans}

    leaders, months_rows, trend = [], [], []
    prev = [m for m in months_it if m < cur]
    prev = prev[-1] if prev else None

    def agg(L, m):
        rs = [dep_row[(d, m)] for d in L["depts"] if (d, m) in dep_row]
        if not rs:
            return None
        s = lambda k: sum(float(r.get(k) or 0) for r in rs)
        hrs = s("hours_mtd") or 1
        p = plan_row.get((L["plan"], m), {})
        prog = p.get("forecast_progress") if m == cur else p.get("progress")
        return {
            "fact": s("counted_mtd"), "people": int(s("people")), "at_risk": int(s("at_risk")),
            "overdue": int(s("overdue")), "closed": int(s("closed_mtd")),
            "tails_h": s("old_open_hours") + s("old_done_hours"), "tails_done_h": s("old_done_hours"),
            "tails_n": int(s("old_open_tasks") + s("old_done_tasks")),
            "health": round(sum(float(r.get("health_index") or 0) * float(r.get("hours_mtd") or 0) for r in rs) / hrs),
            "plan": p.get("plan_hours"), "forecast": p.get("forecast") if m == cur else s("counted_mtd"),
            "pct": round(prog * 100) if prog is not None else None,
        }

    def util(L, m):
        return sum(lab_d.get((d, m), 0) for d in L["depts"]), sum(paid_d.get((d, m), 0) for d in L["depts"])

    def before(m):
        y, mm = int(m[:4]), int(m[5:7]) - 1
        return "%04d-%02d" % (y - (mm == 0), mm or 12)

    for L in LEADERS:
        for m in months_it:
            a, b = agg(L, m), agg(L, before(m))
            if not a:
                continue
            lab, pd = util(L, m)
            plab, ppd = util(L, before(m))
            ft = sum(1 for r in emps if r["month"] == m and r["department"] in L["depts"] and r.get("emp_type") == "FT")
            leaders.append({
                "month": m, "partial": m == cur, "key": L["key"], "leader": L["leader"], "area": L["area"], "kind": "direction",
                "plan": a["plan"], "fact": r1(a["fact"]), "forecast": r1(a["forecast"]), "forecast_pct": a["pct"],
                "prev_fact": r1(b["fact"]) if b else (r1(plab) if plab else None), "prev_pct": b["pct"] if b else None,
                "people": a["people"], "ft": ft, "at_risk": a["at_risk"], "overdue": a["overdue"],
                "closed": a["closed"], "health": a["health"],
                "tails_h": r1(a["tails_h"]), "tails_done_h": r1(a["tails_done_h"]), "tails_n": a["tails_n"],
                "prev_tails_h": r1(b["tails_h"]) if b else None,
                "paid_h": r1(pd), "util": pct(pd, lab), "prev_paid_h": r1(ppd), "prev_util": pct(ppd, plab),
            })
        for m in all_months:
            g = agg(L, m)
            lb, p_ = util(L, m)
            months_rows.append({"key": L["key"], "leader": L["leader"], "month": m,
                                "fact": r1(g["fact"]) if g else r1(lb), "plan": g["plan"] if g else None,
                                "pct": g["pct"] if g else None,
                                "paid_h": r1(p_), "util": pct(p_, lb), "tails_h": r1(g["tails_h"]) if g else None,
                                "partial": m == cur})

    # ---- комерція: менеджери проєктів (як у дашборді менеджерів)
    def owner(p, m=None):
        if m and m < cur and snap and p in snap:
            return snap.get(p)
        return assign.get(p)
    ent = lambda t: entries.get(str(t["id"]), [])
    hm = lambda t, m: sum(float(e[2] or 0) for e in ent(t) if e[0][:7] == m)
    nonint = [t for t in tasks if t.get("p") not in internal]

    def state(t, m):
        """Етап задачі на кінець місяця m (для поточного — зараз); None — задачі тоді не було."""
        if m == cur:
            return t.get("s"), t.get("f") or 0
        sn = msnap.get(m)
        if not sn:
            return None
        v = sn.get(str(t["id"]))
        return (v[0], v[2] or 0) if v else None

    def mgr_rows(m):
        rows = []
        has = m == cur or m in msnap
        for mg in managers + [None]:
            own = [t for t in nonint if owner(t.get("p"), m) == mg]
            tl = []
            if has:
                for t in own:
                    st = state(t, m)
                    if st and t.get("cm", "") < m and st[0] != "PAID":
                        tl.append((st[0], st[1]))
            lab = sum(v.get(m, 0) for p, v in pm.items() if p not in internal and owner(p, m) == mg)
            paid = sum(hm(t, m) for t in own if t.get("s") == "PAID")
            pm_ = before(m)
            plab = sum(v.get(pm_, 0) for p, v in pm.items() if p not in internal and owner(p, pm_) == mg)
            ppaid = sum(hm(t, pm_) for t in nonint if owner(t.get("p"), pm_) == mg and t.get("s") == "PAID")
            rows.append({"month": m, "manager": mg or "Не закріплені",
                         "projects": len({p for p, v in pm.items() if p not in internal and owner(p, m) == mg and v.get(m)}),
                         "tails_done_h": r1(sum(f for s_, f in tl if s_ == "DONE")) if has else None,
                         "tails_done_n": sum(1 for s_, f in tl if s_ == "DONE") if has else None,
                         "tails_testing_h": r1(sum(f for s_, f in tl if s_ == "TESTING BY CLIENT")) if has else None,
                         "tails_h": r1(sum(f for s_, f in tl)) if has else None, "tails_n": len(tl) if has else None,
                         "labor_h": r1(lab), "paid_h": r1(paid), "util": pct(paid, lab), "prev_util": pct(ppaid, plab)})
        return [r for r in rows if r["manager"] != "Не закріплені" or r["labor_h"] or r["tails_h"]]

    rows = []
    for m in all_months:
        mr = mgr_rows(m)
        rows += mr
        tot = lambda k: sum(r[k] or 0 for r in mr)
        has = m == cur or m in msnap
        prev_m = before(m)
        prev_tl = None
        if prev_m in msnap or prev_m == cur:
            prev_tl = r1(sum(st[1] for t in nonint for st in [state(t, prev_m)] if st and t.get("cm", "") < prev_m and st[0] != "PAID"))
        lab_t, paid_t = tot("labor_h"), tot("paid_h")
        plab_t = sum(v.get(prev_m, 0) for p, v in pm.items() if p not in internal)
        ppaid_t = sum(hm(t, prev_m) for t in nonint if t.get("s") == "PAID")
        if m in months_it:
            leaders.append({
                "month": m, "partial": m == cur, "key": COMMERCE["key"], "leader": COMMERCE["leader"], "area": COMMERCE["area"],
                "kind": "commerce", "plan": None, "fact": r1(lab_t), "forecast": None, "forecast_pct": None,
                "prev_fact": r1(plab_t), "prev_pct": None, "people": len(managers), "ft": None, "at_risk": None, "overdue": None,
                "closed": None, "health": None,
                "tails_h": r1(tot("tails_h")) if has else None, "tails_done_h": r1(tot("tails_done_h")) if has else None,
                "tails_n": int(tot("tails_n")) if has else None, "prev_tails_h": prev_tl,
                "paid_h": r1(paid_t), "util": pct(paid_t, lab_t), "prev_paid_h": r1(ppaid_t), "prev_util": pct(ppaid_t, plab_t),
                "unassigned": sum(1 for p in pm if p not in internal and not owner(p, m) and pm[p].get(m)),
            })
        months_rows.append({"key": COMMERCE["key"], "leader": COMMERCE["leader"], "month": m, "fact": r1(lab_t), "plan": None,
                            "pct": None, "paid_h": r1(paid_t), "util": pct(paid_t, lab_t),
                            "tails_h": r1(tot("tails_h")) if has else None, "partial": m == cur})

    # ---- хвости по проєктах: місяць створення, етап, рух за місяць
    def tails_at(m):
        """{id: (етап, години)} незакритих задач, створених до m, на кінець місяця m; None — знімка немає."""
        if m != cur and m not in msnap:
            return None
        res = {}
        for t in nonint:
            if t.get("cm", "") >= m:
                continue
            st = state(t, m)
            if st and st[0] != "PAID":
                res[str(t["id"])] = st
        return res
    byid_t = {str(t["id"]): t for t in nonint}
    tails_rows = []
    for m in months_it:
        end = tails_at(m)
        if end is None:
            continue
        start = tails_at(before(m))
        agg_p = {}
        def P(pname):
            if pname not in agg_p:
                agg_p[pname] = {"month": m, "project": pname, "manager": owner(pname, m) or "", "n": 0, "h": 0.0,
                                "done_h": 0.0, "test_h": 0.0, "work_h": 0.0, "by_cm": {},
                                "start_h": None if start is None else 0.0, "closed_h": None if start is None else 0.0,
                                "new_h": None if start is None else 0.0, "growth_h": None if start is None else 0.0}
            return agg_p[pname]
        for tid, (st, f) in end.items():
            t = byid_t[tid]; r = P(t.get("p") or "—")
            r["n"] += 1; r["h"] += f
            r["done_h" if st == "DONE" else "test_h" if st == "TESTING BY CLIENT" else "work_h"] += f
            r["by_cm"][t["cm"]] = r["by_cm"].get(t["cm"], 0) + f
            if start is not None:
                if tid in start:
                    r["growth_h"] += f - start[tid][1]
                elif t.get("cm") == before(m):
                    r["new_h"] += f
        if start is not None:
            for tid, (st, f) in start.items():
                t = byid_t[tid]; r = P(t.get("p") or "—")
                r["start_h"] += f
                if tid not in end:
                    r["closed_h"] += f
        for r in agg_p.values():
            for k in ("h", "done_h", "test_h", "work_h", "start_h", "closed_h", "new_h", "growth_h"):
                if r[k] is not None:
                    r[k] = r1(r[k])
            r["by_cm"] = {k: r1(v) for k, v in sorted(r["by_cm"].items())}
            r["delta"] = r1(r["h"] - r["start_h"]) if r["start_h"] is not None else None
            tails_rows.append(r)

    # ---- залученість: порядок у задачах сьогодні (знімок) і переведення в PAID\CLOSE по днях
    et = [r for r in load(dash_dir, "emp_tasks") if r["month"] == cur]
    base_stage = lambda st: (st or "").replace("(ІТС)", "").replace("(ИТС)", "").strip().upper()
    hyg = []
    for L in LEADERS:
        rs = {r["url"]: r for r in et if r["department"] in L["depts"]}.values()
        op = [r for r in rs if r["state"] == "Открыта"]
        a_ = agg(L, cur) or {}
        dr = [dep_row[(d, cur)] for d in L["depts"] if (d, cur) in dep_row]
        hyg.append({"key": L["key"], "date": today,
                    "nostage": sum(1 for r in op if not base_stage(r["stage"])),
                    "pause": sum(1 for r in op if base_stage(r["stage"]) in ("PAUSE", "PAUSED")),
                    "nopay": sum(1 for r in rs if not r.get("pay") and base_stage(r["stage"]) != "CANCELLED"),
                    "noest": sum(1 for r in op if not r.get("plan_hours") and base_stage(r["stage"]) in ("PLANNED", "APPROVED", "UNDER APPROVAL", "IN PROGRESS")),
                    "overdue": a_.get("overdue"), "tails_h": r1(a_.get("tails_h")), "tails_done_h": r1(a_.get("tails_done_h")),
                    "test_h": r1(sum(f for tid, (st_, f) in (tails_at(cur) or {}).items() if st_ == "TESTING BY CLIENT" and dep_of.get(byid_t[tid].get("e")) in L["depts"])),
                    "at_risk": a_.get("at_risk")})
    ct = tails_at(cur) or {}
    hyg.append({"key": COMMERCE["key"], "date": today,
                "unassigned": sum(1 for p in pm if p not in internal and not assign.get(p) and pm[p].get(cur)),
                "nopay": sum(1 for tid, (st_, f) in ct.items() if st_ in ("DONE", "TESTING BY CLIENT") and not byid_t[tid].get("pay")),
                "nocontact": sum(1 for tid, (st_, f) in ct.items() if st_ == "DONE" and not byid_t[tid].get("ct")),
                "tails_h": r1(sum(f for st_, f in ct.values())), "tails_done_h": r1(sum(f for st_, f in ct.values() if st_ == "DONE")),
                "test_h": r1(sum(f for st_, f in ct.values() if st_ == "TESTING BY CLIENT"))})
    paid_daily = defaultdict(lambda: [0.0, 0])
    since = before(before(cur)) + "-01"
    for t in nonint:
        c = (t.get("closed") or "")[:10]
        if t.get("s") != "PAID" or c < since:
            continue
        d = dep_of.get(t.get("e"))
        for L in LEADERS:
            if d in L["depts"]:
                paid_daily[(L["key"], c)][0] += t.get("f") or 0; paid_daily[(L["key"], c)][1] += 1
        if owner(t.get("p")):
            paid_daily[("natalia", c)][0] += t.get("f") or 0; paid_daily[("natalia", c)][1] += 1
    paid_rows = [{"key": k, "date": d, "h": r1(v[0]), "n": v[1]} for (k, d), v in sorted(paid_daily.items())]
    # історія по днях із history.json (прострочки, хвости) — для керівників напрямів
    hist_rows = []
    for L in LEADERS:
        for day in sorted({r["date"] for r in hist}):
            rs = [r for r in hist if r["date"] == day and r.get("kind") == "department" and r.get("department") in L["depts"]]
            if rs:
                hist_rows.append({"key": L["key"], "date": day, "overdue": int(sum(r.get("overdue") or 0 for r in rs)),
                                  "tails_h": r1(sum((r.get("old_open_hours") or 0) + (r.get("old_done_hours") or 0) for r in rs)),
                                  "tails_done_h": r1(sum(r.get("old_done_hours") or 0 for r in rs)), "closed": int(sum(r.get("closed_mtd") or 0 for r in rs))})

    # ---- пресейли: задачі проєкту ADM_Пресейл (верхній рівень), підказка можливого виграшу
    import re as _re
    pre = [t for t in tasks if (t.get("p") or "") == "ADM_Пресейл"]
    pre_ids = {str(t["id"]) for t in pre}
    STOPW = {"аналіз", "оцінка", "оцінки", "зустріч", "підготовка", "перша", "підбір", "рішення", "доопрацювання", "обговорення",
             "вимог", "системи", "передпродажні", "переговори", "передпродаж", "презентація", "демо", "інтеграції", "інтеграція",
             "обміну", "робіт", "збір", "інфо", "група", "груп", "україна", "компані", "лід", "надати", "запит", "автоматизацію",
             "складу", "центр", "обласний", "перевірте", "роботу", "бази", "базою", "модуля", "клієнт", "клієнтом", "новий", "розробки", "підключення",
             "завантаження", "номенклатури", "впровадження", "демонстраці"}
    allpm = load(mgr_dir, "pm").get("all", {})
    presale = []
    for t in pre:
        if t.get("par") and str(t["par"]) in pre_ids:
            continue
        kids = [x for x in pre if str(x.get("par")) == str(t["id"])]
        head = _re.split(r"[._\-–:(]", t.get("t") or "")[0]
        ws = [w for w in _re.findall(r"[A-Za-zА-Яа-яЇїІіЄєҐґ]{5,}", head) if w.lower() not in STOPW]
        hint = None
        for w in ws[:3]:
            best = None
            for pn, mm in allpm.items():
                if pn == "ADM_Пресейл" or pn in internal or w.lower() not in pn.lower():
                    continue
                after = sum(v for m_, v in mm.items() if m_ >= (t.get("cm") or ""))
                if after >= 5 and (not best or after > best[1]):
                    best = (pn, after)
            if best:
                hint = {"project": best[0], "h": r1(best[1])}
                break
        presale.append({"id": str(t["id"]), "u": t.get("u"), "title": t.get("t"), "added": t.get("added"), "cm": t.get("cm"),
                        "closed": t.get("closed"), "stage": t.get("st") or "", "state": t.get("s"), "who": t.get("e") or "",
                        "h": r1((t.get("f") or 0) + sum(k.get("f") or 0 for k in kids)), "kids": len(kids), "hint": hint})
    presale.sort(key=lambda r: r["added"] or "", reverse=True)

    about = [{"date": today, "month": cur, "prev_month": prev, "months": months_it, "generated": dt.datetime.now().isoformat(timespec="seconds"),
              "export": tk.get("generated") or ""}]
    for name, data in (("leaders", leaders), ("months", months_rows), ("trend", trend), ("managers", rows), ("tails", tails_rows), ("hyg", hyg), ("paid_daily", paid_rows), ("hist", hist_rows), ("presale", presale), ("about", about)):
        with open(os.path.join(out_dir, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print("рядків керівників:", len(leaders), "· місяців:", len(all_months), "· менеджерів:", len(rows), "· дата:", today)


if __name__ == "__main__":
    main()
