"""Зонд API Worksection: только чтение (get_*). Ответы пишет в probe/*.json — workflow шифрует их в probe.enc.
В лог выводит только количества и имена полей, без данных."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wsreport as w

calls = json.loads(os.environ.get("PROBE_CALLS") or "[]")
c = json.load(open("config.json"))
ws = w.Worksection(c["account"], os.environ.get("WS_API_KEY") or c["api_key"])
os.makedirs("probe", exist_ok=True)
for i, item in enumerate(calls):
    action = item.get("action", "")
    params = {k: v for k, v in item.items() if k != "action"}
    if not action.startswith("get_"):
        print(f"[{i}] пропущено: разрешены только get_*")
        continue
    try:
        d = ws.call(action, **params)
    except Exception as e:
        print(f"[{i}] {action}: ошибка {str(e)[:200]}")
        d = {"error": str(e)}
    with open(f"probe/{i:02d}_{action}.json", "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    data = d.get("data") if isinstance(d, dict) else None
    keys = set()
    if isinstance(data, list):
        for x in data:
            if isinstance(x, dict):
                keys.update(x.keys())
    elif isinstance(data, dict):
        keys.update(data.keys())
    print(f"[{i}] {action} {sorted(params)}: записей {len(data) if isinstance(data, list) else (1 if data else 0)}; поля: {sorted(keys)}")
