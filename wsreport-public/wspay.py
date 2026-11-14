"""We.Bit · облік задач для автоматичного розрахунку ЗП (етап 1 — лише читання Worksection).

Щодня (03:40 за Києвом) робить знімок задач на етапі PAID\\CLOSE і веде історію:
  * paid          — задача вперше потрапила в PAID\\CLOSE (з годинами на цей момент);
  * hours_grew    — на задачі в PAID\\CLOSE додалися години (можлива маніпуляція) → сигнал власнику;
  * reopened      — задача вийшла з PAID\\CLOSE не в ⛔️Definitively closed (повернули в роботу) → сигнал власнику;
  * definitively_closed — задача перейшла в ⛔️Definitively closed (потрапила в нарахування).

Правила (погоджено з Іваном 14.11.2026):
  * кожна задача й підзадача — лише за власним етапом PAID\\CLOSE; батьківська без PAID\\CLOSE не рахується;
  * рахується весь час у треку задачі (без відсічки за місяцем), кожному — те, що він сам списав;
  * записи часу коротші за 10 хвилин не рахуються;
  * мітки: «Оплачувана» → у премію; «Не оплачувана» → рухає сальдо, не оплачується; «Гарантія» → лише показник;
    «ІТС» має йти разом з «Оплачувана»; задачі без мітки виду оплати — окремий список для ручного рішення;
  * пресейл: батьківська задача CLOSE - Won / CLOSE - Lost — лише аналітика; рахуються її підзадачі в PAID\\CLOSE.

Етап 1 нічого не пише у Worksection. Переведення в ⛔️Definitively closed і коментарі We.Bit — етап 2,
тільки після підтвердження Івана.

Репозиторій публічний: у лог виводяться лише кількості, без імен і годин. Усі дані — у зашифрованому pay.enc.

Режими (PAY_MODE або --mode):
  snapshot — щоденний знімок та історія (за замовчуванням);
  calc     — додатково чернетка нарахування: години на людину по задачах у PAID\\CLOSE, ще не закритих остаточно.
"""
import argparse
import datetime as dt
import json
import os
from collections import defaultdict

import wsreport as W

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output", "pay")
MIN_ENTRY_HOURS = 10 / 60.0          # записи коротші за 10 хвилин не рахуються
GROW_EPS = 0.01                      # поріг росту годин, год


def tag_list(t):
    tg = (t or {}).get("tags")
    if isinstance(tg, dict):
        return [str(v) for v in tg.values()]
    return [str(x.get("title") if isinstance(x, dict) else x) for x in (tg or [])]


def pay_kind(tags, cfg):
    """Вид оплати задачі за мітками → paid / unpaid / guarantee / its_no_paid / none."""
    s = {x.strip() for x in tags}
    if s & set(cfg["guarantee_tags"]):
        return "guarantee"
    if "Не оплачувана" in s:
        return "unpaid"
    if cfg["paid_tag"] in s:
        return "paid"
    if s & W.TEAM_TAGS:
        return "its_no_paid"          # ІТС без «Оплачувана» — на ручне рішення
    return "none"


def is_paid_close(stage):
    s = (stage or "").upper()
    return s.startswith("PAID") and "DEFINITIVELY" not in s


def is_definitive(stage):
    return "DEFINITIVELY CLOSED" in (stage or "").upper()


def flatten_with_parent(tasks, parent=None):
    out = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        t = dict(t)
        if parent is not None:
            t["_parent"] = str(parent.get("id"))
            if not t.get("project"):
                t["project"] = parent.get("project")
        out.append(t)
        out.extend(flatten_with_parent(t.get("child"), t))
    return out


def load_tasks(ws, costs):
    try:
        data = ws.call("get_all_tasks", extra="subtasks")
    except W.WSError:
        data = ws.call("get_all_tasks")
    tasks = {str(t.get("id")): t for t in flatten_with_parent(data.get("data", []))}
    for c in costs:                                # підзадачі, яких не віддав get_all_tasks
        t = c.get("task") or {}
        tid = str(t.get("id") or "")
        if tid and tid not in tasks:
            tasks[tid] = dict(t)
    return tasks


def hours_by_task(costs):
    """{task_id: {email: годин}} — лише записи від 10 хвилин."""
    res = defaultdict(lambda: defaultdict(float))
    for c in costs:
        h = W.parse_hours(c.get("time"))
        if h < MIN_ENTRY_HOURS:
            continue
        tid = str((c.get("task") or {}).get("id") or "")
        who = W.user_key(c.get("user_from"))
        if tid and who:
            res[tid][who.lower()] += h
    return res


def read_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default=os.environ.get("PAY_MODE") or "snapshot", choices=["snapshot", "calc"])
    args = ap.parse_args()

    cfg = W.load_config()
    pay_names = W.pay_tags(cfg)
    ws = W.Worksection(cfg["account"], cfg["api_key"])
    now = dt.datetime.now()
    today = now.date()
    start = W.parse_date(cfg.get("pay_history_start") or cfg.get("history_start") or "2026-01-01")

    costs = W.load_costs(ws, start, today)
    tasks = load_tasks(ws, costs)
    hrs = hours_by_task(costs)

    state_path = os.path.join(OUT, "tasks_state.json")
    hist_path = os.path.join(OUT, "history.jsonl")
    prev = read_json(state_path, {})          # {tid: {...}} задачі, що були в PAID\CLOSE на минулий знімок
    cur, events = {}, []

    def task_rec(tid, t, stage):
        per = {k: round(v, 2) for k, v in hrs.get(tid, {}).items()}
        return {
            "id": tid, "name": t.get("name"), "page": t.get("page"),
            "project": (t.get("project") or {}).get("name") if isinstance(t.get("project"), dict) else t.get("project"),
            "parent": t.get("_parent"), "stage": stage, "pay": pay_kind(tag_list(t), cfg),
            "hours": round(sum(per.values()), 2), "by_user": per,
        }

    for tid, t in tasks.items():
        stage = W.stage_from_tags(tag_list(t), pay_names)
        if is_paid_close(stage):
            rec = task_rec(tid, t, stage)
            old = prev.get(tid)
            if old:
                rec["paid_since"] = old.get("paid_since")
                rec["hours_at_paid"] = old.get("hours_at_paid", old.get("hours"))
                if rec["hours"] > old.get("hours", 0) + GROW_EPS:
                    events.append({"date": today.isoformat(), "event": "hours_grew", "task": tid,
                                   "from": old.get("hours"), "to": rec["hours"], "by_user": rec["by_user"]})
            else:
                rec["paid_since"] = today.isoformat()
                rec["hours_at_paid"] = rec["hours"]
                events.append({"date": today.isoformat(), "event": "paid", "task": tid,
                               "hours": rec["hours"], "by_user": rec["by_user"], "pay": rec["pay"]})
            cur[tid] = rec

    for tid, old in prev.items():
        if tid in cur:
            continue
        t = tasks.get(tid) or {}
        stage = W.stage_from_tags(tag_list(t), pay_names) if t else ""
        ev = "definitively_closed" if is_definitive(stage) else "reopened"
        events.append({"date": today.isoformat(), "event": ev, "task": tid, "stage": stage,
                       "hours": old.get("hours"), "name": old.get("name"), "page": old.get("page")})

    write_json(state_path, cur)
    write_json(os.path.join(OUT, "snapshots", today.isoformat() + ".json"), cur)
    if events:
        os.makedirs(OUT, exist_ok=True)
        with open(hist_path, "a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    alerts = [e for e in events if e["event"] in ("hours_grew", "reopened")]
    write_json(os.path.join(OUT, "alerts_latest.json"), {"date": today.isoformat(), "alerts": alerts})

    counts = defaultdict(int)
    for e in events:
        counts[e["event"]] += 1
    # лише кількості — репозиторій публічний
    print("Знімок %s: у PAID\\CLOSE %d задач; нових %d; остаточно закрито %d; ріст годин %d; повернено в роботу %d"
          % (today.isoformat(), len(cur), counts["paid"], counts["definitively_closed"],
             counts["hours_grew"], counts["reopened"]))

    if args.mode == "calc":
        people = defaultdict(lambda: defaultdict(float))
        manual = []
        for tid, rec in cur.items():
            if rec["pay"] in ("none", "its_no_paid"):
                manual.append({k: rec.get(k) for k in ("id", "name", "page", "project", "pay", "hours", "by_user")})
                continue
            for who, h in rec["by_user"].items():
                people[who][rec["pay"]] += h
                people[who]["tasks"] += 1
        calc = {"date": today.isoformat(), "rule": "усі задачі в PAID\\CLOSE на дату розрахунку",
                "people": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in people.items()},
                "manual": manual, "task_ids": sorted(cur.keys())}
        write_json(os.path.join(OUT, "calc_%s.json" % today.isoformat()), calc)
        print("Чернетка нарахування: людей %d, задач %d, без мітки виду оплати %d"
              % (len(people), len(cur) - len(manual), len(manual)))


if __name__ == "__main__":
    main()
