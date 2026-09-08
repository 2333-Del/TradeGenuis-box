"""扫描状态变更通知。传输函数由调用方注入，失败不记为已发送。"""
import json
import threading

LOCK = threading.Lock()


def fingerprint(row):
    return [row.get("resonance_level"), [[p, f.get("state"), f.get("box_high"), f.get("breakout_at")]
            for p, f in sorted((row.get("timeframes") or {}).items())]]


def dispatch(rows, path, scope, send, render):
    with LOCK:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        saved = state.setdefault(scope, {})
        for row in rows:
            key = row["code"]
            old = saved.get(key)
            if row.get("qualified"):
                current = fingerprint(row)
                if old == current:
                    continue
                message = render([row])
            elif old and row.get("resonance_status") == "已评估":
                current = None
                message = f"共振信号失效/降级：{row.get('name', '')} {key}\n截止：{row.get('resonance_as_of', '—')}\n状态：{row.get('mode', '—')}"
            else:
                continue  # 数据失败或本次未扫描到，不能当成信号失效
            if not send(message):
                return False
            if current is None:
                saved.pop(key, None)
            else:
                saved[key] = current
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        return True
