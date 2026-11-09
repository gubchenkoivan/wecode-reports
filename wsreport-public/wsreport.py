#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Worksection -> ежедневная статистика по IT-командам We.Code.

Что делает:
  * забирает сотрудников (отдел = направление, норма часов),
  * списания времени (get_costs) за период,
  * задачи (get_all_tasks),
  * считает метрики по сотрудникам, по отделам и глобальный индекс,
  * сохраняет report.json (данные для дашборда) и dashboard.html (локальный просмотр).

Запуск:
  python wsreport.py                 # отчёт на сегодня (вчера + с начала месяца)
  python wsreport.py --dump          # дополнительно сохранить сырые ответы API в raw/
  python wsreport.py --date 2026-10-28

Только стандартная библиотека Python 3.8+, ничего ставить не нужно.
"""
import argparse
import calendar
import datetime as dt
import hashlib
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULT_CONFIG = {
    "account": "wecode",            # wecode.worksection.com
    "api_key": "",                  # admin API key (или переменная окружения WS_API_KEY)
    "default_norm_hours": 8,        # если у сотрудника норма не найдена
    "norm_overrides": {},           # {"email@company.com": 6} — ручная норма в часах/день
    "department_overrides": {},     # {"email@company.com": "1С"} — ручной отдел
    "exclude_emails": [],           # кого не считать (боты, клиенты, руководство)
    "exclude_departments": ["Administration Department", "АМІК Україна"],  # отделы вне отчёта
    "exclude_people": [],           # имена или email сотрудников вне отчёта (коммерческий отдел и т.п.)
    "roles": {},                    # {"Имя или email": "head" | "pm"}; остальные — технические специалисты
    "short_names": {                # короткие названия отделов для дашборда
        "Mobile Development Department": "Mobile",
        "Fuel Solutions Department": "Fuel Solutions",
        "BAS LCW Department": "BAS LCW",
        "BAS Solutions Development & Implementation Department": "BAS Solutions D&I",
        "Sales & Customer Support Department": "Customer Support",
        "LCW TEAM": "LCW TEAM"
    },
    "holidays": [],                 # ["2026-12-25", ...] — нерабочие дни
    "lookback_days": 90,            # окно списаний для расчёта план/факт
    "history_start": "2026-08-01",  # с какой даты брать списания (часы по задачам за весь их срок)
    "overrun_threshold": 0.2,       # перерасход > 20% от плана — флаг
    "output_dir": "output",
    "fund_hours_ft": 168,           # фонд времени фултаймера в месяц
    "staff": {},                    # {"Имя": {"type": "FT"|"PT", "norm": часов в месяц или null}}
    "plans": [],                    # [{"name", "departments": [...], "hours": план или null, "head"}]
    "paid_tag": "Оплачувана",
    "guarantee_tags": ["Гарантія"],                     # не идут в норму, показываются отдельно
    "excluded_status_tags": ["CLOSE - Won", "CLOSE - Lost"],  # не идут в норму
    "status_thresholds": {"ok": 1.0, "risk": 0.85},    # прогноз к норме: дотягивает / риск / нет
    "index_weights": {              # веса индекса здоровья отдела
        "utilization": 0.30,
        "on_time": 0.30,
        "no_overdue": 0.25,
        "estimate": 0.15
    }
}

# Возможные названия полей в ответе get_users (уточним после первого запуска с --dump)
NORM_KEYS = ("work_hours", "hours_per_day", "norm", "norm_hours", "workday",
             "work_day", "schedule_hours", "day_hours")
DEPT_KEYS = ("department", "dept", "group", "team", "otdel")


# ----------------------------------------------------------------- API client
class WSError(Exception):
    pass


class Worksection:
    # Способы отправки запроса: перебираем, пока сервер не "увидит" action.
    MODES = [
        ("/api/admin/v2/", "get"), ("/api/admin/v2/", "post_form"), ("/api/admin/v2/", "post_query"),
        ("/api/admin/v2", "get"), ("/api/admin/v2", "post_form"), ("/api/admin/v2", "post_query"),
    ]

    def __init__(self, account, api_key, dump_dir=None):
        self.host = "https://%s.worksection.com" % account
        self.key = api_key.strip()
        self.dump_dir = dump_dir
        self._last = 0.0
        self.mode = None   # найденный рабочий способ

    def _send(self, path, mode, payload):
        wait = 1.1 - (time.time() - self._last)   # лимит API: 1 запрос в секунду
        if wait > 0:
            time.sleep(wait)
        url = self.host + path
        headers = {"User-Agent": "wecode-wsreport/1.0", "Accept": "application/json"}
        if mode == "get":
            req = urllib.request.Request(url + "?" + payload, headers=headers)
        elif mode == "post_query":
            req = urllib.request.Request(url + "?" + payload, data=b"", headers=headers, method="POST")
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            req = urllib.request.Request(url, data=payload.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read().decode("utf-8")
                final_url = r.geturl()
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            final_url = url
        finally:
            self._last = time.time()
        try:
            return json.loads(raw), final_url
        except ValueError:
            return {"status": "error", "message": "не JSON: " + raw[:200]}, final_url

    @staticmethod
    def _action_missing(data):
        msg = str(data.get("message", "")).lower() if isinstance(data, dict) else ""
        return "action is required" in msg or "action" in msg and "required" in msg

    def call(self, action, **params):
        items = [("action", action)] + [(k, str(v)) for k, v in params.items() if v is not None]
        query = urllib.parse.urlencode(items)
        sign = hashlib.md5((query + self.key).encode("utf-8")).hexdigest()
        payload = query + "&hash=" + sign
        modes = [self.mode] if self.mode else self.MODES
        tried = []
        for path, mode in modes:
            data, final_url = self._send(path, mode, payload)
            tried.append("%s %s -> %s: %s" % (mode, path, final_url.split("?")[0],
                                              data.get("message") if isinstance(data, dict) else "?"))
            if not self._action_missing(data):
                if not self.mode:
                    self.mode = (path, mode)
                    print("    способ запроса: %s %s" % (mode.upper(), path))
                break
        else:
            raise WSError("%s: сервер не принял ни один способ запроса:\n  %s" % (action, "\n  ".join(tried)))
        if self.dump_dir:
            os.makedirs(self.dump_dir, exist_ok=True)
            name = "%s_%s.json" % (action, "_".join("%s-%s" % kv for kv in params.items()) or "all")
            name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
            with open(os.path.join(self.dump_dir, name), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        if isinstance(data, dict) and data.get("status") not in (None, "ok"):
            raise WSError("%s: %s" % (action, data.get("message") or data))
        return data


# ----------------------------------------------------------------- helpers

TEAM_TAGS = {"ІТС", "ITC", "ІTC", "IТС"}     # «ІТС» — вид оплаты, не этап


RESULT_TAGS = ("З першого разу!", "Не здали!")      # группа меток «Результат сдачі»
TYPE_TAGS = ("Функціонал", "Помилка", "Зміна вимог", "Аналітика/Консультація", "Технічна",
             "Оптимізація/Оновлення", "Зміна дизайну", "Документація", "Пресейл", "Навчання")  # «Тип задачі»


def pay_tags(cfg):
    """Метки группы «Вид оплати» в Worksection: Оплачувана, Не оплачувана, Гарантія, ІТС."""
    return [cfg["paid_tag"], "Не оплачувана"] + list(cfg["guarantee_tags"]) + ["ІТС", "ITC"]


def stage_from_tags(tags, pay_names):
    """Этап задачи по тегам Worksection: тег большими буквами (DONE, IN PROGRESS (ІТС), PAID\\CLOSE…),
    кроме меток вида оплаты (в т.ч. «ІТС»). «⛔️Definitively closed» — финальный этап (полная оплата),
    важнее остальных, кроме CANCELLED."""
    tags = [str(x.get("title") if isinstance(x, dict) else x) for x in tags]
    stage = next((x for x in tags if x not in pay_names and x.strip() not in TEAM_TAGS
                  and x.upper() == x and any(ch.isalpha() for ch in x)), "")
    final = next((x for x in tags if "definitively closed" in x.lower()), "")
    return final if final and not stage.upper().startswith("CANCEL") else stage


def is_paid_stage(stage):
    """PAID\\CLOSE (с суффиксом (ІТС) или без) и Definitively closed — полная оплата."""
    s = (stage or "").upper()
    return s.startswith("PAID") or "DEFINITIVELY CLOSED" in s

def parse_date(v):
    if not v:
        return None
    s = str(v).strip()[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_hours(v):
    """'7:30' -> 7.5 ; '1.25' -> 1.25 ; 90 (мин?) — числа считаем часами."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    neg = s.startswith("-")
    s = s.lstrip("-")
    if ":" in s:
        parts = s.split(":")
        try:
            h = int(parts[0] or 0) + int(parts[1] or 0) / 60.0
        except ValueError:
            return 0.0
    else:
        try:
            h = float(s)
        except ValueError:
            return 0.0
    return -h if neg else h


def fmt_ws(d):
    return d.strftime("%d.%m.%Y")


def workdays(start, end, holidays):
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            n += 1
        d += dt.timedelta(days=1)
    return n


def user_key(u):
    if not u:
        return None
    return str(u.get("email") or u.get("id") or u.get("name"))


def first_number(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, "", 0, "0"):
            h = parse_hours(d[k])
            if h > 0:
                return h
    return None


def first_text(d, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("title")
        if v:
            return str(v).strip()
    return None


def flatten_tasks(tasks, parent=None):
    out = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        t = dict(t)
        if parent and not t.get("project"):
            t["project"] = parent.get("project")
        out.append(t)
        out.extend(flatten_tasks(t.get("child"), t))
    return out


def ratio(a, b):
    return round(a / b, 3) if b else None


# ----------------------------------------------------------------- data load
def load_users(ws, cfg):
    data = ws.call("get_users")
    users = {}
    for u in data.get("data", []):
        email = (u.get("email") or "").lower()
        if not email or email == "noone" or email in cfg["exclude_emails"]:
            continue
        key = str(u.get("email") or u.get("id"))
        norm = cfg["norm_overrides"].get(email) or first_number(u, NORM_KEYS)
        dept = cfg["department_overrides"].get(email) or first_text(u, DEPT_KEYS) or "Без отдела"
        if dept.lower() in cfg["exclude_departments"]:
            continue
        name = (u.get("name") or "").strip()
        if email in cfg["exclude_people"] or name.lower() in cfg["exclude_people"]:
            continue
        role = cfg["roles"].get(email) or cfg["roles"].get(name.lower()) or "specialist"
        users[key] = {
            "id": u.get("id"),
            "name": u.get("name") or ("%s %s" % (u.get("first_name", ""), u.get("last_name", ""))).strip(),
            "email": u.get("email"),
            "department": dept,
            "norm": float(norm) if norm else float(cfg["default_norm_hours"]),
            "norm_found": bool(norm),
            "role": role,
            "emp_type": (cfg["staff"].get(name.lower()) or {}).get("type"),
            "norm_month": ((cfg["staff"].get(name.lower()) or {}).get("norms") or {}).get(
                cfg.get("_month"), (cfg["staff"].get(name.lower()) or {}).get("norm")),
            "in_staff": name.lower() in cfg["staff"],
        }
    return users


def load_costs(ws, start, end):
    """Списания по кускам в месяц, чтобы не упереться в 10 000 записей."""
    rows, cur = [], start
    while cur <= end:
        last = dt.date(cur.year, cur.month, calendar.monthrange(cur.year, cur.month)[1])
        chunk_end = min(last, end)
        data = ws.call("get_costs", datestart=fmt_ws(cur), dateend=fmt_ws(chunk_end))
        rows.extend(data.get("data", []))
        cur = chunk_end + dt.timedelta(days=1)
    return rows


def load_schedule(ws, users, start, end):
    """Отпуска/больничные и отработанные выходные из графика Worksection: {user_key: {off, extra}}."""
    try:
        data = ws.call("get_users_schedule", datestart=fmt_ws(start), dateend=fmt_ws(end))
    except Exception as ex:   # график необязателен
        print("    график недоступен: %s" % ex)
        return {}
    by_email = {str(u.get("email") or "").lower(): k for k, u in users.items()}
    out = {}
    items = data.get("data") or {}
    for u in (items.values() if isinstance(items, dict) else items):
        k = by_email.get(str(u.get("email") or "").lower())
        if not k:
            continue
        sch = u.get("schedule") or {}
        out[k] = {"off": {d for d, v in sch.items() if v in ("vacation", "sick-leave")},
                  "extra": {d for d, v in sch.items() if v == "workday"}}
    return out


# Направление клиента — по папке проекта в Worksection (поле company проекта)
FOLDER_DIRECTIONS = {
    "ІТС_Супровід": "Customer Support", "ІТС_Cупровід": "Customer Support",
    "КОРП_BAS": "BAS Solutions D&I", "Офіс Казахстан": "BAS Solutions D&I",
    "Паливний напрямок": "Fuel Solutions", "Мобільні додатки": "Mobile",
    "LCW": "BAS LCW", "Наші продукти": "Internal Products",
}


def load_project_folders(ws):
    """{id проекта: папка, название проекта: папка} — для группировки клиентов по направлениям."""
    out = {}
    try:
        data = ws.call("get_projects")
    except WSError as e:
        print("    ! папки проектов не получены: %s" % e)
        return out
    for p in data.get("data", []):
        folder = (p.get("company") or "").strip()
        if p.get("id") is not None:
            out[str(p["id"])] = folder
        if p.get("name"):
            out.setdefault(p["name"], folder)
    return out


def load_tasks(ws, costs=None):
    """Задачи вместе с подзадачами. Если API не отдал подзадачи, добираем их из записей списаний
    (в каждой записи есть задача с датами создания/закрытия, статусом и тегами)."""
    try:
        data = ws.call("get_all_tasks", extra="subtasks")
    except WSError:
        data = ws.call("get_all_tasks")
    tasks = flatten_tasks(data.get("data", []))
    seen = {str(t.get("id")) for t in tasks}
    added = 0
    for c in costs or []:
        t = c.get("task") or {}
        tid = str(t.get("id") or "")
        if tid and tid not in seen:
            seen.add(tid)
            tasks.append(dict(t))
            added += 1
    if added:
        print("    + подзадач из списаний: %d" % added)
    return tasks


# ----------------------------------------------------------------- metrics
def person_workdays(start, end, holidays, off, extra):
    """Рабочие дни сотрудника: будни без праздников и его отпусков/больничных + отработанные выходные."""
    n, d = 0, start
    while d <= end:
        iso = d.isoformat()
        if iso in extra or (d.weekday() < 5 and iso not in holidays and iso not in off):
            n += 1
        d += dt.timedelta(days=1)
    return n


def build_report(users, costs, tasks, today, cfg, schedule=None, closed_month=False):
    """closed_month=True: итог закрытого месяца на дату today (последний день месяца), прогноз = факт."""
    schedule = schedule or {}
    yesterday = today if closed_month else today - dt.timedelta(days=1)
    month_start = today.replace(day=1)
    month_end = dt.date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    holidays = set(cfg["holidays"])
    wd_month = workdays(month_start, yesterday, holidays) if yesterday >= month_start else 0
    wd_total = workdays(month_start, month_end, holidays)
    fund = float(cfg["fund_hours_ft"])
    guarantee_tags = set(cfg["guarantee_tags"])
    excluded_tags = set(cfg["excluded_status_tags"])
    paid_tag = cfg["paid_tag"]

    def blank_user(key, u):
        sch = schedule.get(key, {})
        off, extra = sch.get("off", set()), sch.get("extra", set())
        elapsed = person_workdays(month_start, yesterday, holidays, off, extra) if yesterday >= month_start else 0
        total = person_workdays(month_start, month_end, holidays, off, extra)
        y_iso = yesterday.isoformat()
        return {
            "name": u.get("name", key), "email": u.get("email"), "role": u.get("role", "specialist"),
            "department": u.get("department", "Без отдела"),
            "emp_type": u.get("emp_type"), "norm": u.get("norm_month"),
            "wd_elapsed": elapsed, "wd_total": total,
            "absent_days": len([x for x in off if x[:7] == month_start.isoformat()[:7]]),
            "yesterday_workday": (y_iso in extra) or (yesterday.weekday() < 5 and y_iso not in holidays and y_iso not in off),
            "hours_yesterday": 0.0, "hours_mtd": 0.0, "hours_to_yday": 0.0, "timer_hours_mtd": 0.0,
            "counted_mtd": 0.0, "counted_to_yday": 0.0, "paid_mtd": 0.0, "guarantee_mtd": 0.0,
            "closed_yesterday": 0, "closed_mtd": 0, "closed_on_time_mtd": 0,
            "closed_with_deadline_mtd": 0, "open_tasks": 0, "overdue": 0,
            "max_overdue_days": 0, "flags": [],
        }

    emp = {k: blank_user(k, u) for k, u in users.items()}

    # --- часы: всё затабелированное + "к норме" по правилам мотивации
    fact_by_task = defaultdict(float)
    fact_by_task_month = defaultdict(float)
    daily = defaultdict(float)
    emp_daily = defaultdict(lambda: [0.0, 0.0])
    for c in costs:
        h = parse_hours(c.get("time"))
        task = c.get("task") or {}
        if task.get("id"):
            fact_by_task[str(task["id"])] += h
        d = parse_date(c.get("date"))
        k = user_key(c.get("user_from"))
        if k not in emp or not d or not (month_start <= d <= today):
            continue
        e = emp[k]
        tags = set((task.get("tags") or {}).values()) if isinstance(task.get("tags"), dict) else set(task.get("tags") or [])
        is_guarantee = bool(tags & guarantee_tags)
        counted = not is_guarantee and not (tags & excluded_tags)
        e["hours_mtd"] += h
        daily[(d.isoformat(), e["department"])] += h
        emp_daily[(d.isoformat(), k)][0] += h
        if task.get("id"):
            fact_by_task_month[str(task["id"])] += h
        if d <= yesterday:
            e["hours_to_yday"] += h
        if str(c.get("is_timer")).lower() in ("1", "true"):
            e["timer_hours_mtd"] += h
        if d == yesterday:
            e["hours_yesterday"] += h
        if is_guarantee:
            e["guarantee_mtd"] += h
        if counted:
            emp_daily[(d.isoformat(), k)][1] += h
            e["counted_mtd"] += h
            if d <= yesterday:
                e["counted_to_yday"] += h
            if paid_tag in tags:
                e["paid_mtd"] += h

    # --- клиенты: часы по проектам за месяц и сравнение с прошлым месяцем
    prev_end = month_start - dt.timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    prev_same_end = min(prev_start.replace(day=min(today.day, prev_end.day)), prev_end)
    type_names = {"SUPP": "Сопровождение", "IMPL": "Внедрение", "DEV": "Разработка", "ADM": "Внутреннее", "MOB": "Разработка"}
    clients = {}
    for c in costs:
        d = parse_date(c.get("date"))
        k = user_key(c.get("user_from"))
        if k not in emp or not d or d < prev_start or d > today:
            continue
        pobj = (c.get("task") or {}).get("project") or {}
        proj = pobj.get("name") or "Без проекта"
        folders = cfg.get("_proj_folder") or {}
        if folders:
            folder = folders.get(str(pobj.get("id"))) or folders.get(proj) or ""
            direction = FOLDER_DIRECTIONS.get(folder)
            if not direction:
                continue   # проект вне папок направлений (INBOX, ADM, HR…) — в таблицу клиентов не идёт
        else:
            direction = emp[k]["department"]   # запасной вариант, если папки не получены
        pref = proj.split("_", 1)[0].upper()
        ctype = type_names.get(pref, "Прочее")
        client = proj.split("_", 1)[1] if pref in type_names and "_" in proj else proj
        client = client.strip(" _") or proj
        h = parse_hours(c.get("time"))
        key = (direction, proj)
        r = clients.setdefault(key, {"department": direction, "client": client, "project": proj, "type": ctype,
                                     "hours": 0.0, "paid_hours": 0.0, "prev_same": 0.0, "prev_total": 0.0, "people": set()})
        tags = set(((c.get("task") or {}).get("tags") or {}).values()) if isinstance((c.get("task") or {}).get("tags"), dict) else set()
        if d >= month_start:
            r["hours"] += h
            r["people"].add(emp[k]["name"])
            if paid_tag in tags:
                r["paid_hours"] += h
        else:
            r["prev_total"] += h
            if d <= prev_same_end:
                r["prev_same"] += h
    client_rows = []
    for r in clients.values():
        r = dict(r, people=len(r["people"]))
        for f in ("hours", "paid_hours", "prev_same", "prev_total"):
            r[f] = round(r[f], 1)
        r["change"] = round((r["hours"] - r["prev_same"]) / r["prev_same"], 3) if r["prev_same"] else None
        client_rows.append(r)
    client_rows.sort(key=lambda r: (r["department"], -r["hours"]))

    # --- задачи
    overdue_list, overrun_list, no_owner = [], [], 0
    base = "https://%s.worksection.com" % cfg["account"]

    def link(page):
        return (base + page) if page and str(page).startswith("/") else page
    emp_tasks = []
    pay_names = pay_tags(cfg)

    def task_row(t, owner, state, deadline, closed):
        tags = list((t.get("tags") or {}).values()) if isinstance(t.get("tags"), dict) else list(t.get("tags") or [])
        pay = next((x for x in tags if x in pay_names), "")
        stage = stage_from_tags(tags, pay_names)
        tnames = [str(x.get("title") if isinstance(x, dict) else x) for x in tags]
        result = next((x for x in tnames if x in RESULT_TAGS), "")
        ttype = next((x for x in tnames if x in TYPE_TAGS), "")
        if state == "open":
            delay = (today - deadline).days if deadline and deadline < today else 0
        else:
            delay = (closed - deadline).days if deadline and closed and closed > deadline else 0
        plan = parse_hours(t.get("max_time"))
        return {"employee": owner["name"], "department": owner["department"],
                "task": t.get("name"), "url": link(t.get("page")),
                "project": (t.get("project") or {}).get("name", ""),
                "state": "Открыта" if state == "open" else "Закрыта", "stage": stage, "pay": pay,
                "deadline": deadline.isoformat() if deadline else None,
                "closed": closed.isoformat() if closed else None, "delay_days": delay,
                "plan_hours": round(plan, 1) if plan else None,
                "fact_month": round(fact_by_task_month.get(str(t.get("id")), 0.0), 1),
                "fact_total": round(fact_by_task.get(str(t.get("id")), 0.0), 1),
                "created": (parse_date(t.get("date_added")) or today).isoformat(),
                "result": result, "ttype": ttype}

    for t in tasks:
        added = parse_date(t.get("date_added"))
        if added and added > today:
            continue                      # задача появилась после даты отчёта
        k = user_key(t.get("user_to"))
        owner = emp.get(k)
        deadline = parse_date(t.get("date_end"))
        closed = parse_date(t.get("date_closed"))
        project = (t.get("project") or {}).get("name", "")
        # статус на дату отчёта: закрытая позже даты отчёта считается открытой
        status = "done" if str(t.get("status", "")).lower() == "done" and closed and closed <= today else "active"
        if status != "done":
            closed = None
        if owner and (status != "done" or (closed and month_start <= closed <= today)):
            emp_tasks.append(task_row(t, owner, "open" if status != "done" else "done", deadline, closed))
        if status == "done" and closed:
            if owner:
                if closed == yesterday:
                    owner["closed_yesterday"] += 1
                if month_start <= closed <= today:
                    owner["closed_mtd"] += 1
                    if deadline:
                        owner["closed_with_deadline_mtd"] += 1
                        if closed <= deadline:
                            owner["closed_on_time_mtd"] += 1
            plan = parse_hours(t.get("max_time"))
            fact = fact_by_task.get(str(t.get("id")), 0.0)
            if owner and plan > 0 and month_start <= closed <= today and fact > plan * (1 + cfg["overrun_threshold"]):
                overrun_list.append({"task": t.get("name"), "url": link(t.get("page")), "project": project,
                                     "owner": owner["name"], "department": owner["department"],
                                     "plan": round(plan, 1), "fact": round(fact, 1)})
        elif status != "done":
            if not owner:
                to = t.get("user_to") or {}
                if not to or str(to.get("email", "")).upper() == "NOONE" or str(to.get("id")) == "1":
                    no_owner += 1   # задачи исключённых сотрудников не считаем
                continue
            owner["open_tasks"] += 1
            if deadline and deadline < today:
                days = (today - deadline).days
                owner["overdue"] += 1
                owner["max_overdue_days"] = max(owner["max_overdue_days"], days)
                overdue_list.append({"task": t.get("name"), "url": link(t.get("page")), "project": project,
                                     "owner": owner["name"], "department": owner["department"],
                                     "deadline": deadline.isoformat(), "days": days})

    # --- точность оценки по закрытым в месяце задачам с планом
    est = defaultdict(list)
    for t in tasks:
        closed = parse_date(t.get("date_closed"))
        plan = parse_hours(t.get("max_time"))
        if str(t.get("status")).lower() != "done" or not closed or plan <= 0 or not (month_start <= closed <= today):
            continue
        owner = emp.get(user_key(t.get("user_to")))
        fact = fact_by_task.get(str(t.get("id")), 0.0)
        if owner and fact > 0:
            est[owner["department"]].append(min(plan / fact, fact / plan))

    # --- сотрудники: темп, прогноз, норма, фонд
    st = cfg["status_thresholds"]
    for e in emp.values():
        for f in ("hours_yesterday", "hours_mtd", "hours_to_yday", "timer_hours_mtd", "counted_mtd",
                  "counted_to_yday", "paid_mtd", "guarantee_mtd"):
            e[f] = round(e[f], 2)
        remaining = max(0, e["wd_total"] - e["wd_elapsed"])
        e["wd_remaining"] = remaining
        e["pace"] = round(e["counted_to_yday"] / e["wd_elapsed"], 2) if e["wd_elapsed"] else None
        e["forecast"] = round(max(e["counted_mtd"], e["counted_to_yday"] + (e["pace"] or 0) * remaining), 1)
        norm = e["norm"]
        e["norm_progress"] = ratio(e["counted_mtd"], norm) if norm else None
        e["forecast_progress"] = ratio(e["forecast"], norm) if norm else None
        e["need_per_day"] = (round(max(0.0, norm - e["counted_mtd"]) / remaining, 1) if remaining
                             else round(max(0.0, norm - e["counted_mtd"]), 1)) if norm else None
        if e["forecast_progress"] is None:
            e["status"] = None
        elif e["forecast_progress"] >= st["ok"]:
            e["status"] = "ok"
        elif e["forecast_progress"] >= st["risk"]:
            e["status"] = "risk"
        else:
            e["status"] = "miss"
        if e["emp_type"] == "FT" and e["wd_total"]:
            e["fund_mtd"] = round(fund * e["wd_elapsed"] / e["wd_total"], 1)
            e["fund_util"] = ratio(e["hours_to_yday"], e["fund_mtd"])
        else:
            e["fund_mtd"], e["fund_util"] = None, None
        e["on_time_rate_mtd"] = ratio(e["closed_on_time_mtd"], e["closed_with_deadline_mtd"])
        e["timer_share_mtd"] = ratio(e["timer_hours_mtd"], e["hours_mtd"])
        if e["yesterday_workday"] and e["hours_yesterday"] == 0:
            e["flags"].append("0 ч вчера")
        if e["hours_yesterday"] > 10:
            e["flags"].append("> 10 ч вчера")
        if e["role"] == "specialist":
            if e["status"] == "miss":
                e["flags"].append("не дотягивает до нормы")
            elif e["status"] == "risk":
                e["flags"].append("риск по норме")
        if e["max_overdue_days"] > 7:
            e["flags"].append("просрочка > 7 дн")

    # --- отделы
    w = cfg["index_weights"]
    depts = {}
    for e in emp.values():
        d = depts.setdefault(e["department"], {
            "department": e["department"], "people": 0, "hours_yesterday": 0.0, "hours_mtd": 0.0,
            "counted_mtd": 0.0, "paid_mtd": 0.0, "guarantee_mtd": 0.0,
            "fund_hours_to_yday": 0.0, "fund_mtd": 0.0, "norm_sum": 0.0, "forecast_normed": 0.0,
            "closed_yesterday": 0, "closed_mtd": 0, "closed_on_time_mtd": 0,
            "closed_with_deadline_mtd": 0, "open_tasks": 0, "overdue": 0, "at_risk": 0})
        d["people"] += 1
        for f in ("hours_yesterday", "hours_mtd", "counted_mtd", "paid_mtd", "guarantee_mtd", "closed_yesterday",
                  "closed_mtd", "closed_on_time_mtd", "closed_with_deadline_mtd", "open_tasks", "overdue"):
            d[f] += e[f]
        if e["fund_mtd"]:
            d["fund_mtd"] += e["fund_mtd"]
            d["fund_hours_to_yday"] += e["hours_to_yday"]
        if e["norm"]:
            d["norm_sum"] += e["norm"]
            d["forecast_normed"] += e["forecast"]
        if e["role"] == "specialist" and e["status"] in ("risk", "miss"):
            d["at_risk"] += 1
    total_hours = sum(d["hours_mtd"] for d in depts.values()) or 0
    for name, d in depts.items():
        for f in ("hours_yesterday", "hours_mtd", "counted_mtd", "paid_mtd", "guarantee_mtd", "fund_mtd",
                  "norm_sum", "forecast_normed"):
            d[f] = round(d[f], 1)
        d["hours_share"] = ratio(d["hours_mtd"], total_hours)
        d["fund_util"] = ratio(d["fund_hours_to_yday"], d["fund_mtd"])
        d["norm_progress"] = ratio(d["forecast_normed"], d["norm_sum"])
        d["utilization_mtd"] = d["norm_progress"]          # совместимость со старыми полями
        d["norm_mtd"] = d["norm_sum"]
        d["on_time_rate_mtd"] = ratio(d["closed_on_time_mtd"], d["closed_with_deadline_mtd"])
        d["overdue_share"] = ratio(d["overdue"], d["open_tasks"])
        d["estimate_accuracy"] = round(sum(est[name]) / len(est[name]), 3) if est[name] else None
        parts = {
            "utilization": min(d["norm_progress"], 1.0) if d["norm_progress"] is not None else None,
            "on_time": d["on_time_rate_mtd"],
            "no_overdue": (1 - d["overdue_share"]) if d["overdue_share"] is not None else None,
            "estimate": d["estimate_accuracy"],
        }
        got = {k: v for k, v in parts.items() if v is not None}
        wsum = sum(w[k] for k in got)
        d["health_index"] = round(100 * sum(w[k] * v for k, v in got.items()) / wsum) if wsum else None
        hi = d["health_index"]
        d["light"] = None if hi is None else ("green" if hi >= 80 else "yellow" if hi >= 60 else "red")

    # --- планы направлений (одно направление может объединять несколько отделов)
    rem_days = max(0, wd_total - wd_month)
    plans = []
    for p in cfg["plans"]:
        members = [e for e in emp.values() if e["department"].lower() in [x.lower() for x in p["departments"]]]
        counted = round(sum(e["counted_mtd"] for e in members), 1)
        forecast = round(sum(e["forecast"] for e in members), 1)
        hours = p.get("hours")
        plans.append({
            "plan": p["name"], "head": p.get("head"), "departments": p["departments"],
            "people": len(members), "plan_hours": hours,
            "hours_mtd": round(sum(e["hours_mtd"] for e in members), 1),
            "counted_mtd": counted, "paid_mtd": round(sum(e["paid_mtd"] for e in members), 1),
            "forecast": forecast,
            "progress": ratio(counted, hours) if hours else None,
            "forecast_progress": ratio(forecast, hours) if hours else None,
            "need_per_day": (round(max(0.0, hours - counted) / rem_days, 1) if rem_days else None) if hours else None,
        })

    # --- когорты: задачи какого месяца создания где сейчас (по направлениям)
    def stage_of(t):
        tags = list((t.get("tags") or {}).values()) if isinstance(t.get("tags"), dict) else list(t.get("tags") or [])
        return stage_from_tags(tags, pay_names)
    is_paid = is_paid_stage               # PAID\CLOSE и Definitively closed = полная оплата
    cohorts, backlog = {}, []
    mkey = month_start.isoformat()[:7]
    for t in tasks:
        added = parse_date(t.get("date_added"))
        if not added or added > today:
            continue
        owner = emp.get(user_key(t.get("user_to")))
        if not owner:
            continue
        stg = stage_of(t)
        if stg.upper().startswith("CANCEL"):
            continue
        closed = parse_date(t.get("date_closed"))
        is_closed = str(t.get("status", "")).lower() == "done" and closed and closed <= today
        # Факт оплаты — только этап PAID\CLOSE или ⛔️Definitively closed. Метки вида оплаты
        # (Оплачувана, Не оплачувана, Гарантія, ІТС) — тип задачи, а не факт оплаты.
        if is_paid(stg):
            state = "paid"
            if not is_closed or closed < month_start:
                continue                  # оплачена до этого месяца — не хвост и не закрытие месяца
        elif stg.upper().startswith("DONE") or is_closed:
            state = "done_wait"           # сдана (DONE) или закрыта без этапа оплаты — ждёт приёмки/оплаты
        else:
            state = "open"
        hours = fact_by_task.get(str(t.get("id")), 0.0)
        cm = added.isoformat()[:7]
        key = (owner["department"], cm)
        c = cohorts.setdefault(key, {"department": owner["department"], "created": cm,
                                     "open_tasks": 0, "open_hours": 0.0, "done_wait_tasks": 0, "done_wait_hours": 0.0,
                                     "closed_month_tasks": 0, "closed_month_hours": 0.0, "paid_month_tasks": 0,
                                     "paid_month_hours": 0.0, "testing_tasks": 0, "testing_hours": 0.0,
                                     "closed_final_hours": 0.0})
        if state == "open":
            c["open_tasks"] += 1; c["open_hours"] += hours
            if stg.upper().startswith("TESTING BY CLIENT"):
                c["testing_tasks"] += 1; c["testing_hours"] += hours
        if state == "done_wait":
            c["done_wait_tasks"] += 1; c["done_wait_hours"] += hours
        if is_closed and closed >= month_start:
            c["closed_month_tasks"] += 1; c["closed_month_hours"] += hours
            if state != "done_wait":
                c["closed_final_hours"] += hours
            if state == "paid":
                c["paid_month_tasks"] += 1; c["paid_month_hours"] += hours
        if cm < mkey and state in ("open", "done_wait"):
            backlog.append({"department": owner["department"], "employee": owner["name"],
                            "task": t.get("name"), "url": link(t.get("page")),
                            "project": (t.get("project") or {}).get("name", ""),
                            "created": added.isoformat(), "created_month": cm,
                            "closed": closed.isoformat() if is_closed else None,
                            "state": ("Ждёт приёмки/оплаты (DONE)" if stg.upper().startswith("DONE") else
                                      "Закрыта без PAID\\CLOSE") if state == "done_wait" else (
                                "Приёмка клиентом" if stg.upper().startswith("TESTING BY CLIENT") else "В работе"),
                            "stage": stg, "age_days": (today - added).days, "hours": round(hours, 1),
                            "pay": next((x for x in (list((t.get("tags") or {}).values()) if isinstance(t.get("tags"), dict) else [])
                                         if x in pay_names), "")})
    for c in cohorts.values():
        for f in ("open_hours", "done_wait_hours", "closed_month_hours", "paid_month_hours", "testing_hours", "closed_final_hours"):
            c[f] = round(c[f], 1)
    backlog.sort(key=lambda x: (x["created_month"], -x["hours"]))
    old_by_dept = defaultdict(lambda: [0, 0.0, 0, 0.0])
    for b in backlog:
        o = old_by_dept[b["department"]]
        if b["state"].startswith("Ждёт"):
            o[2] += 1; o[3] += b["hours"]
        else:
            o[0] += 1; o[1] += b["hours"]
    overdue_list.sort(key=lambda x: -x["days"])
    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "report_date": today.isoformat(),
        "yesterday": yesterday.isoformat(),
        "month_start": month_start.isoformat(),
        "workdays_mtd": wd_month,
        "workdays_month": wd_total,
        "totals": {
            "people": len(emp),
            "hours_yesterday": round(sum(e["hours_yesterday"] for e in emp.values()), 1),
            "hours_mtd": round(total_hours, 1),
            "counted_mtd": round(sum(e["counted_mtd"] for e in emp.values()), 1),
            "closed_yesterday": sum(e["closed_yesterday"] for e in emp.values()),
            "closed_mtd": sum(e["closed_mtd"] for e in emp.values()),
            "overdue": len(overdue_list),
            "tasks_without_owner": no_owner,
        },
        "departments": sorted(depts.values(), key=lambda d: -(d["hours_mtd"])),
        "plans": plans,
        "cohorts": sorted(cohorts.values(), key=lambda c: (c["department"], c["created"])),
        "clients": client_rows,
        "clients_period": {"prev_month": prev_start.isoformat()[:7], "same_to": prev_same_end.isoformat()},
        "backlog": backlog,
        "old_by_dept": {k: {"old_open_tasks": v[0], "old_open_hours": round(v[1], 1),
                            "old_done_tasks": v[2], "old_done_hours": round(v[3], 1)} for k, v in old_by_dept.items()},
        "employees": sorted(emp.values(), key=lambda e: (e["department"], e["name"])),
        "overdue_tasks": overdue_list[:200],
        "daily_hours": [{"day": d, "department": dep, "hours": round(v, 2)}
                        for (d, dep), v in sorted(daily.items())],
        "emp_daily": [{"day": d, "employee": emp[k]["name"], "hours": round(v[0], 2), "counted": round(v[1], 2)}
                      for (d, k), v in sorted(emp_daily.items())],
        "emp_tasks": emp_tasks,
        "overrun_tasks": sorted(overrun_list, key=lambda x: -(x["fact"] - x["plan"]))[:100],
    }


# ----------------------------------------------------------------- HTML
def pct(v):
    return "—" if v is None else "%d%%" % round(v * 100)


def render_html(r):
    e = html.escape
    light = {"green": "#2e9e5b", "yellow": "#d9a400", "red": "#d64545", None: "#999"}
    rows_d = "".join(
        "<tr><td><span class=dot style='background:%s'></span>%s</td><td>%s</td><td>%s</td><td>%s</td>"
        "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td><b>%s</b></td></tr>" % (
            light[d["light"]], e(d["department"]), d["people"], d["hours_yesterday"], d["hours_mtd"],
            pct(d["utilization_mtd"]), d["closed_mtd"], pct(d["on_time_rate_mtd"]),
            "%d / %d" % (d["overdue"], d["open_tasks"]), pct(d["estimate_accuracy"]),
            "—" if d["health_index"] is None else d["health_index"])
        for d in r["departments"])
    rows_e = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class=f>%s</td></tr>" % (
            e(x["name"]), e(x["department"]), x["hours_yesterday"], x["hours_mtd"], pct(x["forecast_progress"]),
            x["closed_yesterday"], x["closed_mtd"], x["overdue"], e(", ".join(x["flags"])))
        for x in r["employees"])
    rows_o = "".join(
        "<tr><td><a href='%s'>%s</a></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            e(x["url"] or "#"), e(x["task"] or ""), e(x["project"]), e(x["owner"]), x["deadline"], x["days"])
        for x in r["overdue_tasks"])
    t = r["totals"]
    return """<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>IT-команды: статистика</title><style>
body{font:14px/1.4 system-ui,sans-serif;margin:16px;color:#222;background:#fafafa}
h1{font-size:20px}h2{font-size:16px;margin-top:28px}.kpi{display:flex;gap:12px;flex-wrap:wrap}
.kpi div{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:10px 14px}.kpi b{display:block;font-size:20px}
table{border-collapse:collapse;width:100%%;background:#fff;font-size:13px}th,td{border-bottom:1px solid #eee;padding:6px 8px;text-align:left}
th{background:#f2f2f2}.dot{display:inline-block;width:10px;height:10px;border-radius:50%%;margin-right:6px}.f{color:#c0392b}
.wrap{overflow-x:auto}</style>
<h1>IT-команды We.Code — %s</h1><p>Вчера: %s · с начала месяца: %s раб. дн. · обновлено %s</p>
<div class=kpi><div>Часы вчера<b>%s</b></div><div>Часы за месяц<b>%s</b></div><div>Закрыто вчера<b>%s</b></div>
<div>Закрыто за месяц<b>%s</b></div><div>Просрочено<b>%s</b></div><div>Без исполнителя<b>%s</b></div></div>
<h2>Отделы</h2><div class=wrap><table><tr><th>Отдел</th><th>Людей</th><th>Ч вчера</th><th>Ч месяц</th><th>Загрузка</th>
<th>Закрыто</th><th>В срок</th><th>Просроч./откр.</th><th>Точность оценки</th><th>Индекс</th></tr>%s</table></div>
<h2>Сотрудники</h2><div class=wrap><table><tr><th>Сотрудник</th><th>Отдел</th><th>Ч вчера</th><th>Ч месяц</th><th>Загрузка</th>
<th>Закр. вчера</th><th>Закр. месяц</th><th>Просрочено</th><th>Флаги</th></tr>%s</table></div>
<h2>Просроченные задачи</h2><div class=wrap><table><tr><th>Задача</th><th>Проект</th><th>Исполнитель</th><th>Срок</th><th>Дней</th></tr>%s</table></div>
</html>""" % (r["report_date"], r["yesterday"], r["workdays_mtd"], r["generated_at"],
               t["hours_yesterday"], t["hours_mtd"], t["closed_yesterday"], t["closed_mtd"], t["overdue"],
               t["tasks_without_owner"], rows_d, rows_e, rows_o)


# ----------------------------------------------------------------- dashboard export
HISTORY_FIELDS = ("people", "hours_mtd", "counted_mtd", "norm_progress", "fund_util", "closed_mtd",
                  "on_time_rate_mtd", "open_tasks", "overdue", "overdue_share", "estimate_accuracy", "health_index")


def export_dashboard(r, out, cfg):
    """Файлы для дашборда (output/dash/*.json) + накопительная история output/history.json."""
    S = lambda name: cfg["short_names"].get(name, name)
    dash_dir = os.path.join(out, "dash")
    os.makedirs(dash_dir, exist_ok=True)
    files = {
        "departments": [{
            "department": S(d["department"]), "people": d["people"], "hours_yesterday": d["hours_yesterday"],
            "hours_mtd": d["hours_mtd"], "counted_mtd": d["counted_mtd"], "paid_mtd": d["paid_mtd"],
            "guarantee_mtd": d["guarantee_mtd"], "fund_util": d["fund_util"], "norm_sum": d["norm_sum"],
            "forecast_normed": d["forecast_normed"], "norm_progress": d["norm_progress"], "at_risk": d["at_risk"],
            **r["old_by_dept"].get(d["department"], {"old_open_tasks": 0, "old_open_hours": 0.0,
                                                      "old_done_tasks": 0, "old_done_hours": 0.0}),
            "closed_yesterday": d["closed_yesterday"], "closed_mtd": d["closed_mtd"],
            "on_time_rate": d["on_time_rate_mtd"], "open_tasks": d["open_tasks"], "overdue": d["overdue"],
            "overdue_share": d["overdue_share"], "estimate_accuracy": d["estimate_accuracy"],
            "health_index": d["health_index"]} for d in r["departments"]],
        "employees": [{
            "employee": e["name"], "department": S(e["department"]),
            "role": {"head": "Руководитель", "pm": "PM"}.get(e["role"], "Специалист"),
            "hours_yesterday": e["hours_yesterday"],
            "emp_type": e["emp_type"] or "—", "norm": e["norm"],
            "hours_mtd": e["hours_mtd"], "counted_mtd": e["counted_mtd"], "paid_mtd": e["paid_mtd"],
            "guarantee_mtd": e["guarantee_mtd"], "pace": e["pace"], "forecast": e["forecast"],
            "norm_progress": e["norm_progress"], "forecast_progress": e["forecast_progress"],
            "need_per_day": e["need_per_day"],
            "status": {"ok": "Дотягивает", "risk": "Риск", "miss": "Не дотягивает"}.get(e["status"], "—"),
            "fund_util": e["fund_util"], "absent_days": e["absent_days"], "timer_share": e["timer_share_mtd"],
            "wd_elapsed": e["wd_elapsed"], "wd_total": e["wd_total"],
            "closed_yesterday": e["closed_yesterday"], "closed_mtd": e["closed_mtd"],
            "on_time_rate": e["on_time_rate_mtd"], "open_tasks": e["open_tasks"], "overdue": e["overdue"],
            "max_overdue_days": e["max_overdue_days"],
            "wd_next": e.get("wd_next"), "absent_next": e.get("absent_next"), "capacity_next": e.get("capacity_next"),
            "flags": ", ".join(f for f in e["flags"] if f != "норма по умолчанию")} for e in r["employees"]],
        "daily_hours": [dict(x, department=S(x["department"])) for x in r["daily_hours"]],
        "emp_daily": r["emp_daily"],
        "emp_tasks": [dict(x, department=S(x["department"])) for x in r["emp_tasks"]],
        "overdue_tasks": [{"task": o["task"], "url": o["url"], "project": o["project"], "owner": o["owner"],
                           "department": S(o["department"]), "deadline": o["deadline"],
                           "days_overdue": o["days"]} for o in r["overdue_tasks"]],
        "plans": [{k: v for k, v in pl.items() if k != "departments"} |
                  {"departments": ", ".join(S(x) for x in pl["departments"])} for pl in r["plans"]],
        "cohorts": [dict(c, department=S(c["department"])) for c in r["cohorts"]],
        "clients": [dict(c, department=S(c["department"]), prev_month=r["clients_period"]["prev_month"],
                         prev_same_to=r["clients_period"]["same_to"]) for c in r["clients"]],
        "backlog": [dict(b, department=S(b["department"])) for b in r["backlog"]],
        "overrun_tasks": [{"task": o["task"], "url": o["url"], "project": o["project"], "owner": o["owner"],
                           "department": S(o["department"]), "plan_hours": o["plan"],
                           "fact_hours": o["fact"]} for o in r["overrun_tasks"]],
    }
    # архив месяца: последний запуск в месяце перезаписывает его итог
    month = r["month_start"][:7]
    for name in files:
        files[name] = [dict(row, month=month) for row in files[name]]
    arch_dir = os.path.join(out, "archive", month)
    os.makedirs(arch_dir, exist_ok=True)
    for name, rows in files.items():
        with open(os.path.join(arch_dir, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
    # данные дашборда = все месяцы из архива
    months = sorted(m for m in os.listdir(os.path.join(out, "archive")) if len(m) == 7 and m[4] == "-")
    combined = {name: [] for name in files}
    for m in months:
        for name in files:
            path = os.path.join(out, "archive", m, name + ".json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    combined[name].extend(json.load(f))
    files = combined
    # история: один снимок на дату отчёта, повторный запуск в тот же день перезаписывает снимок
    hist_path = os.path.join(out, "history.json")
    history = []
    if os.path.exists(hist_path):
        with open(hist_path, encoding="utf-8") as f:
            history = json.load(f)
    history = [h for h in history if h.get("date") != r["report_date"]]
    for d in r["departments"]:
        row = {"date": r["report_date"], "kind": "department", "department": S(d["department"])}
        for k in HISTORY_FIELDS:
            row[k.replace("_rate_mtd", "_rate")] = d[k]
        row.update(r["old_by_dept"].get(d["department"], {"old_open_tasks": 0, "old_open_hours": 0.0,
                                                          "old_done_tasks": 0, "old_done_hours": 0.0}))
        history.append(row)
    for pl in r["plans"]:
        history.append({"date": r["report_date"], "kind": "plan", "department": pl["plan"],
                        "plan_hours": pl["plan_hours"], "counted_mtd": pl["counted_mtd"],
                        "forecast": pl["forecast"], "progress": pl["progress"],
                        "forecast_progress": pl["forecast_progress"]})
    history.sort(key=lambda h: (h["date"], h.get("kind", ""), h["department"]))
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)
    files["history"] = history
    # ежедневный снимок по каждому сотруднику: отслеживать, меняется ли картина от отчёта к отчёту
    eh_path = os.path.join(out, "emp_history.json")
    emp_hist = []
    if os.path.exists(eh_path):
        with open(eh_path, encoding="utf-8") as f:
            emp_hist = json.load(f)
    emp_hist = [h for h in emp_hist if h.get("date") != r["report_date"]]
    for e in r["employees"]:
        emp_hist.append({
            "date": r["report_date"], "month": month, "employee": e["name"], "department": S(e["department"]),
            "role": {"head": "Руководитель", "pm": "PM"}.get(e["role"], "Специалист"),
            "counted_mtd": e["counted_mtd"], "forecast": e["forecast"], "forecast_progress": e["forecast_progress"],
            "need_per_day": e["need_per_day"],
            "status": {"ok": "Дотягивает", "risk": "Риск", "miss": "Не дотягивает"}.get(e["status"], "—"),
            "fund_util": e["fund_util"], "open_tasks": e["open_tasks"], "overdue": e["overdue"],
            "max_overdue_days": e["max_overdue_days"], "closed_mtd": e["closed_mtd"]})
    emp_hist.sort(key=lambda h: (h["date"], h["department"], h["employee"]))
    with open(eh_path, "w", encoding="utf-8") as f:
        json.dump(emp_hist, f, ensure_ascii=False)
    files["emp_history"] = emp_hist
    for name, rows in files.items():
        with open(os.path.join(dash_dir, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
    return dash_dir


# ----------------------------------------------------------------- main
def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if k == "api_key" and not v:
                continue
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    if not cfg["api_key"] or "ВСТАВЬТЕ" in str(cfg["api_key"]):
        cfg["api_key"] = DEFAULT_CONFIG["api_key"]
    cfg["api_key"] = os.environ.get("WS_API_KEY") or cfg["api_key"]
    cfg["exclude_departments"] = [x.lower() for x in cfg["exclude_departments"]]
    cfg["exclude_people"] = [x.strip().lower() for x in cfg["exclude_people"]]
    cfg["roles"] = {k.strip().lower(): v for k, v in cfg["roles"].items()}
    cfg["staff"] = {k.strip().lower(): v for k, v in cfg["staff"].items()}
    cfg["exclude_emails"] = [x.lower() for x in cfg["exclude_emails"]]
    cfg["norm_overrides"] = {k.lower(): v for k, v in cfg["norm_overrides"].items()}
    cfg["department_overrides"] = {k.lower(): v for k, v in cfg["department_overrides"].items()}
    return cfg


def add_next_capacity(report, ws, users, today, cfg):
    """Загрузка на следующий месяц: рабочие дни, отпуска/больничные из графика и ёмкость в часах."""
    ns = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    ne = dt.date(ns.year, ns.month, calendar.monthrange(ns.year, ns.month)[1])
    hol = set(cfg.get("holidays") or [])
    total = workdays(ns, ne, hol)
    try:
        sch = load_schedule(ws, users, ns, ne)
    except Exception:
        sch = {}
    by_name = {u.get("name"): k for k, u in users.items()}
    for e in report.get("employees", []):
        k = by_name.get(e.get("name"))
        off = {d for d in (sch.get(k) or {}).get("off", set()) if d[:7] == ns.isoformat()[:7]}
        off_wd = len([d for d in off if dt.date.fromisoformat(d).weekday() < 5 and d not in hol])
        e["wd_next"] = max(0, total - off_wd)
        e["absent_next"] = off_wd
        e["capacity_next"] = round(e["norm"] * e["wd_next"] / total, 1) if e.get("norm") and total else None


def main():
    ap = argparse.ArgumentParser(description="Worksection -> статистика IT-команд")
    ap.add_argument("--date", help="дата отчёта YYYY-MM-DD (по умолчанию сегодня)")
    ap.add_argument("--dump", action="store_true", help="сохранить сырые ответы API в raw/")
    ap.add_argument("--month", help="пересчитать итог прошлого месяца YYYY-MM (например 2026-09)")
    a = ap.parse_args()
    cfg = load_config()
    if not cfg["api_key"]:
        sys.exit("Нет API-ключа: укажите api_key в config.json или переменную WS_API_KEY")
    closed_month = False
    if a.month:
        y, m = map(int, a.month.split("-"))
        today = dt.date(y, m, calendar.monthrange(y, m)[1])
        closed_month = True
    else:
        today = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    cfg["_month"] = today.strftime("%Y-%m")
    out = os.path.join(HERE, cfg["output_dir"])
    os.makedirs(out, exist_ok=True)
    ws = Worksection(cfg["account"], cfg["api_key"], os.path.join(HERE, "raw") if a.dump else None)

    print("1/3 сотрудники...")
    users = load_users(ws, cfg)
    print("    найдено: %d" % len(users))
    lookback_start = min(today.replace(day=1), today - dt.timedelta(days=cfg["lookback_days"]))
    if cfg.get("history_start"):
        lookback_start = min(lookback_start, dt.date.fromisoformat(cfg["history_start"]))
    print("2/3 списания времени с %s..." % lookback_start)
    costs = load_costs(ws, lookback_start, today)
    print("    записей: %d" % len(costs))
    print("3/3 задачи...")
    tasks = load_tasks(ws, costs)
    print("    задач: %d" % len(tasks))
    cfg["_proj_folder"] = load_project_folders(ws)
    print("    папок проектов: %d" % len(set(cfg["_proj_folder"].values())))
    month_end = dt.date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    schedule = load_schedule(ws, users, today.replace(day=1), month_end)
    print("    отпуска/больничные в графике: %d сотр." % sum(1 for v in schedule.values() if v["off"]))

    report = build_report(users, costs, tasks, today, cfg, schedule, closed_month)
    add_next_capacity(report, ws, users, today, cfg)
    with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out, "report_%s.json" % today.isoformat()), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False)
    with open(os.path.join(out, "dashboard.html"), "w", encoding="utf-8") as f:
        f.write(render_html(report))
    export_dashboard(report, out, cfg)

    missing = [u["name"] for u in users.values() if not u.get("in_staff")]
    nodept = [e["name"] for e in report["employees"] if e["department"] == "Без отдела"]
    print("\nГотово: %s" % out)
    if missing:
        print("! Нет в разделе staff config.json (тип FT/PT и норма): %s" % ", ".join(missing))
    if nodept:
        print("! Без отдела: %s" % ", ".join(nodept[:10]))


if __name__ == "__main__":
    # --log: писать вывод в run.log рядом со скриптом (для фонового запуска через pythonw без окна)
    if "--log" in sys.argv:
        sys.argv.remove("--log")
        _log = open(os.path.join(HERE, "run.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = _log
        print("\n===== %s =====" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        main()
    except WSError as ex:
        print("Ошибка API: %s" % ex)
        sys.exit(1)
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
