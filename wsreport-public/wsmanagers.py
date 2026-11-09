#!/usr/bin/env python3
"""Выгрузка для «Дашборда менеджеров» (отдельно от wsreport.py, текущий отчёт не трогает).

Берёт из Worksection задачи с подзадачами и записи времени (с комментариями), пишет в output/managers/:
  tasks.json    — задачи и подзадачи, которые нужны дашборду (хвосты, DONE, TESTING BY CLIENT, PAID)
  entries.json  — записи времени по этим задачам: [дата, кто, часы, описание, таймер]
  pm.json       — часы по проектам и месяцам (для индекса утилизации): staff — сотрудники отчёта, all — все
  months.json   — снимок состояния задач на каждый месяц (текущий месяц перезаписывается, прошлые хранятся)
  meta.json     — дата выгрузки, текущий месяц, список месяцев

Запуск:  python wsmanagers.py               (ключ — WS_API_KEY или config.json, как у wsreport.py)
         python wsmanagers.py --date 2026-10-30
         python wsmanagers.py --from-raw raw (проверка без API: сырые ответы, сохранённые wsreport.py --dump)
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sys

import wsreport as W

HERE = os.path.dirname(os.path.abspath(__file__))
CONTACT_KEYS = ("контактна особа", "контактное лицо")
RESULT_KEYS = ("результат по завданню", "результат по задаче")


class RawWS:
    """Подмена API: читает ответы, сохранённые wsreport.py --dump."""
    def __init__(self, folder):
        self.folder = folder

    def call(self, action, **params):
        if action == "get_costs":
            name = "get_costs_datestart-%s_dateend-%s.json" % (params.get("datestart"), params.get("dateend"))
            path = os.path.join(self.folder, name)
            if not os.path.exists(path):
                return {"status": "ok", "data": []}
        else:
            hits = sorted(glob.glob(os.path.join(self.folder, action + "_*.json")))
            if not hits:
                raise W.WSError("%s: нет файла в %s" % (action, self.folder))
            path = hits[0]
        with open(path, encoding="utf-8") as f:
            return json.load(f)


def tags_of(t):
    tg = t.get("tags")
    if isinstance(tg, dict):
        return [str(v) for v in tg.values()]
    if isinstance(tg, list):
        return [str(x.get("title") if isinstance(x, dict) else x) for x in tg]
    return []


def custom(t, keys):
    cf = t.get("custom_fields")
    items = cf.values() if isinstance(cf, dict) else (cf or [])
    for v in items:
        if not isinstance(v, dict):
            continue
        name = str(v.get("name") or "").strip().lower()
        val = str(v.get("value") or "").strip()
        if val and any(name.startswith(k) for k in keys):
            return val
    return ""


def flatten(tasks, parent=None, out=None):
    out = [] if out is None else out
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        t = dict(t)
        if parent is not None:
            t.setdefault("parent", {"id": parent.get("id"), "name": parent.get("name")})
            if not t.get("project"):
                t["project"] = parent.get("project")
        out.append(t)
        flatten(t.get("child"), t, out)
    return out


def load_all_tasks(ws):
    for extra in ("subtasks,custom_fields", "subtasks", None):
        try:
            data = ws.call("get_all_tasks", extra=extra) if extra else ws.call("get_all_tasks")
            print("    задачи: get_all_tasks extra=%s" % (extra or "—"))
            return flatten(data.get("data", []))
        except W.WSError as e:
            print("    get_all_tasks extra=%s не принят: %s" % (extra, e))
    return []


def merge(base, extra):
    """Дополнить задачу полями из другого источника (не перетирая заполненные)."""
    for k, v in extra.items():
        if k == "child":
            continue
        if base.get(k) in (None, "", [], {}) and v not in (None, "", [], {}):
            base[k] = v
    return base


def category(stage, status):
    s = (stage or "").upper()
    if s.startswith("CANCEL"):
        return None
    if s.startswith("PAID") or "DEFINITIVELY CLOSED" in s:
        return "PAID"                     # факт оплаты — только PAID\CLOSE и Definitively closed
    if s.startswith("DONE"):
        return "DONE"
    if str(status).lower() == "done":
        return "DONE"                     # закрыта в Worksection без этапа оплаты — не оплачена, ждёт оплаты
    if s.startswith("TESTING BY CLIENT"):
        return "TESTING BY CLIENT"
    return "IN WORK"


def main():
    ap = argparse.ArgumentParser(description="Worksection -> дашборд менеджеров")
    ap.add_argument("--date", help="дата выгрузки YYYY-MM-DD (по умолчанию сегодня)")
    ap.add_argument("--from-raw", help="папка с сырыми ответами API (проверка без сети)")
    a = ap.parse_args()
    cfg = W.load_config()
    today = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    cfg["_month"] = today.strftime("%Y-%m")
    start = dt.date.fromisoformat(cfg.get("mgr_history_start") or "2025-01-01")
    out = os.path.join(HERE, cfg["output_dir"], "managers")
    os.makedirs(out, exist_ok=True)
    if a.from_raw:
        ws = RawWS(a.from_raw)
    else:
        if not cfg["api_key"]:
            sys.exit("Нет API-ключа: WS_API_KEY или api_key в config.json")
        ws = W.Worksection(cfg["account"], cfg["api_key"])
    base = "https://%s.worksection.com" % cfg["account"]
    link = lambda page: (base + page) if page and str(page).startswith("/") else (page or "")
    pay_names = W.pay_tags(cfg)

    print("1/3 сотрудники...")
    users = W.load_users(ws, cfg)
    staff = {str((u.get("email") or "")).lower() for u in users.values()}
    names = {str((u.get("email") or "")).lower(): u["name"] for u in users.values()}
    print("    в отчёте: %d" % len(staff))
    print("2/3 списания времени с %s..." % start)
    costs = W.load_costs(ws, start, today)
    print("    записей: %d" % len(costs))
    print("3/3 задачи...")
    tasks = {}
    for t in load_all_tasks(ws):
        tasks[str(t.get("id"))] = t
    for c in costs:                       # в записях времени задача приходит с parent, custom_fields, тегами
        t = c.get("task") or {}
        i = str(t.get("id") or "")
        if i:
            tasks[i] = merge(tasks.get(i, {}), dict(t))
    print("    задач всего: %d" % len(tasks))

    # --- все проекты (чтобы закреплять за менеджерами и проекты без задач к закрытию)
    projects = {}
    for t in tasks.values():
        n = (t.get("project") or {}).get("name") or ""
        if n:
            projects.setdefault(n, 0)
            if str(t.get("status")).lower() != "done":
                projects[n] += 1
    try:
        for pr in ws.call("get_projects").get("data", []) or []:
            n = str(pr.get("name") or "").strip()
            if n and str(pr.get("status") or "active").lower() not in ("archive", "archived", "done", "closed"):
                projects.setdefault(n, 0)
        print("    проектов (get_projects + задачи): %d" % len(projects))
    except Exception as e:                # нет метода или нет файла в raw — берём проекты из задач
        print("    get_projects недоступен (%s), проектов из задач: %d" % (e, len(projects)))

    # --- записи времени и часы по проектам
    ent, fact, staff_hours = {}, {}, {}
    pm_staff, pm_all = {}, {}
    for c in costs:
        t = c.get("task") or {}
        i = str(t.get("id") or "")
        d = W.parse_date(c.get("date"))
        if not i or not d or d > today:
            continue
        h = W.parse_hours(c.get("time"))
        u = c.get("user_from") or {}
        em = str(u.get("email") or "").lower()
        who = names.get(em) or u.get("name") or ""
        ent.setdefault(i, []).append([d.isoformat(), who, round(h, 2), str(c.get("comment") or "").strip()[:600],
                                      1 if c.get("is_timer") else 0])
        fact[i] = fact.get(i, 0.0) + h
        p = ((t.get("project") or {}).get("name") or (tasks.get(i, {}).get("project") or {}).get("name") or "")
        m = d.isoformat()[:7]
        pm_all.setdefault(p, {})[m] = pm_all.setdefault(p, {}).get(m, 0.0) + h
        if em in staff:
            staff_hours[i] = staff_hours.get(i, 0.0) + h
            pm_staff.setdefault(p, {})[m] = pm_staff.setdefault(p, {}).get(m, 0.0) + h

    # --- строки задач
    rows = []
    for i, t in tasks.items():
        added = W.parse_date(t.get("date_added"))
        if not added or added > today:
            continue
        tg = tags_of(t)
        pay = next((x for x in tg if x in pay_names), "")
        stage = W.stage_from_tags(tg, pay_names)   # без голого тега «ІТС»; Definitively closed важнее прочих
        s = "PAID" if W.is_paid_stage(stage) else category(stage, t.get("status"))
        if s is None or s == "CLOSED":
            continue
        owner = t.get("user_to") or {}
        em = str(owner.get("email") or "").lower()
        if em not in staff and i not in staff_hours:
            continue                      # задачи вне отчёта (коммерческий отдел, клиенты и т.п.)
        if s == "PAID" and not any(e[0] >= start.isoformat() for e in ent.get(i, [])):
            continue                      # оплаченные без часов в окне истории дашборду не нужны
        closed = W.parse_date(t.get("date_closed"))
        par = t.get("parent") or {}
        proj = t.get("project") or {}
        plan = W.parse_hours(t.get("max_time"))
        rows.append({
            "id": int(i) if i.isdigit() else i, "u": link(t.get("page")),
            "p": proj.get("name") or "", "pid": proj.get("id"),
            "t": t.get("name") or "", "par": par.get("id"),
            "st": stage, "s": s, "pay": pay,
            "ct": custom(t, CONTACT_KEYS), "res": custom(t, RESULT_KEYS),
            "e": names.get(em) or owner.get("name") or "",
            "added": added.isoformat(), "cm": added.isoformat()[:7],
            "closed": closed.isoformat() if closed else None,
            "pl": round(plan, 2) if plan else None, "f": round(fact.get(i, 0.0), 2),
        })
    ids = {str(r["id"]) for r in rows}
    entries = {i: sorted(v) for i, v in ent.items() if i in ids}
    rnd = lambda d: {p: {m: round(h, 2) for m, h in ms.items()} for p, ms in d.items() if p}
    cur = today.strftime("%Y-%m")

    # --- снимок месяца (прошлые месяцы не трогаем, кроме итогового снимка в первую ночь нового месяца)
    mpath = os.path.join(out, "months.json")
    months = {}
    if os.path.exists(mpath):
        with open(mpath, encoding="utf-8") as f:
            months = json.load(f)
    snap = {str(r["id"]): [r["s"], r["st"], r["f"]] for r in rows}
    if cur not in months:
        # первая ночь нового месяца: состояние сейчас = состояние на конец прошлого месяца.
        # Перезаписываем снимок прошлого месяца, чтобы в него попали изменения последнего дня
        # (задачи, закрытые или переведённые в оплату днём, уже после предыдущей выгрузки).
        prev = (dt.date(today.year, today.month, 1) - dt.timedelta(days=1)).strftime("%Y-%m")
        if prev in months:
            months[prev] = dict(snap)
    months[cur] = snap
    all_months = sorted(set(months) | {m for ms in pm_all.values() for m in ms if m >= start.isoformat()[:7]})

    files = {
        "tasks.json": {"generated": dt.datetime.now().isoformat(timespec="seconds"), "date": today.isoformat(),
                       "cur": cur, "tasks": rows,
                       "projects": sorted([n, c] for n, c in projects.items())},
        "entries.json": entries,
        "pm.json": {"staff": rnd(pm_staff), "all": rnd(pm_all)},
        "months.json": months,
        "meta.json": {"generated": dt.datetime.now().isoformat(timespec="seconds"), "date": today.isoformat(),
                      "cur": cur, "months": all_months, "tasks": len(rows), "entries": sum(len(v) for v in entries.values()),
                      "history_start": start.isoformat()},
    }
    for name, data in files.items():
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    cnt = {}
    for r in rows:
        cnt[r["s"]] = cnt.get(r["s"], 0) + 1
    print("\nГотово: %s — задач %d (%s), записей %d" % (out, len(rows), ", ".join("%s %d" % kv for kv in sorted(cnt.items())),
                                                        files["meta.json"]["entries"]))


if __name__ == "__main__":
    main()
