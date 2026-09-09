"""本地用量记账:只读查询 ZCode 的 db.sqlite(turn_usage 表,逐轮 token 记录)。

口径说明:input/output/reasoning 为模型实际计费口径的生成与输入量,
cache_read/cache_creation 单列,不并入合计,避免把缓存命中算成消耗。
"""
import datetime
import sqlite3
import time
from pathlib import Path

DB_PATH = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"


def usage_summary(db_path: Path = DB_PATH) -> dict:
    """返回 today / h5 / d7 三个窗口的 token 与轮次统计;库不可读时返回 None 项。"""
    now_ms = int(time.time() * 1000)
    today0_ms = int(datetime.datetime.combine(
        datetime.date.today(), datetime.time.min).timestamp() * 1000)
    windows = {
        "today": today0_ms,
        "h5": now_ms - 5 * 3600 * 1000,
        "d7": now_ms - 7 * 86400 * 1000,
    }
    result: dict = {"ok": False}
    try:
        con = sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            for name, since in windows.items():
                row = con.execute(
                    "SELECT COUNT(*),"
                    " COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),"
                    " COALESCE(SUM(reasoning_tokens),0),"
                    " COALESCE(SUM(cache_read_input_tokens),0),"
                    " COALESCE(SUM(cache_creation_input_tokens),0)"
                    " FROM turn_usage WHERE started_at >= ? AND status = 'completed'",
                    (since,),
                ).fetchone()
                result[name] = {
                    "turns": row[0], "input": row[1], "output": row[2],
                    "reasoning": row[3], "cache_read": row[4], "cache_write": row[5],
                }
            result["ok"] = True
        finally:
            con.close()
    except Exception:
        pass
    return result


def sessions_meta(ids: list) -> dict:
    """批量取会话标题与轮次:{sid: {"title": str, "turns": int}}。

    标题取 session 表的生成标题,缺失时回退目录名;轮次 = turn_usage 行数
    (含进行中的轮)。库不可读或会话不存在时返回空dict,调用方自行回退。
    """
    if not ids:
        return {}
    out: dict = {}
    try:
        con = sqlite3.connect(
            f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=0.3)
        try:
            q = ",".join("?" * len(ids))
            for sid, title, directory in con.execute(
                    f"SELECT id, title, directory FROM session WHERE id IN ({q})",
                    ids):
                name = (title or "").strip() or Path(directory or "").name or sid
                out[sid] = {"title": name, "turns": 0}
            for sid, n in con.execute(
                    f"SELECT session_id, COUNT(*) FROM turn_usage "
                    f"WHERE session_id IN ({q}) GROUP BY session_id", ids):
                if sid in out:
                    out[sid]["turns"] = n
        finally:
            con.close()
    except Exception:
        pass
    return out


def fmt_tokens(n) -> str:
    if not n:
        return "0"
    if n >= 1e8:
        return f"{n / 1e8:.2f}亿"
    if n >= 1e4:
        return f"{n / 1e4:.1f}万"
    return f"{n:,.0f}"


if __name__ == "__main__":
    import json
    print(json.dumps(usage_summary(), ensure_ascii=False, indent=2))

def discover_sessions(exec_root=None, db_path=None,
                      recent_tool_ms=300_000, recent_done_ms=900_000):
    """从事实源回填错过的会话(仅补充,hook 事件状态永远更真)。

    近 recent_tool_ms 有工具调用输出(exec 目录调用日志 mtime)→ running;
    近 recent_done_ms 有完结轮(turn_usage)→ done。返回 {sid: {status,tool,ts}},
    ts 取事实时间,行内"X分前"显示真实陈旧度。
    """
    from pathlib import Path as _P
    import time as _t
    root = _P(exec_root) if exec_root else Path.home() / ".zcode" / "cli" / "exec"
    db = _P(db_path) if db_path else DB_PATH
    now = _t.time() * 1000
    tool_at: dict = {}
    try:
        for d in root.iterdir():
            if not d.name.startswith("sess_"):
                continue
            newest = max((f.stat().st_mtime * 1000
                          for f in d.glob("call_*")), default=0)
            if newest and now - newest < recent_tool_ms:
                tool_at[d.name] = int(newest)
    except OSError:
        pass
    done_at: dict = {}
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True,
                              timeout=0.3)
        try:
            rows = con.execute(
                "SELECT session_id, MAX(completed_at) FROM turn_usage "
                "WHERE completed_at >= ? GROUP BY session_id",
                (now - recent_done_ms,)).fetchall()
        finally:
            con.close()
        for sid, ts in rows:
            done_at[sid] = int(ts or 0)
    except Exception:
        pass
    # 时间戳裁决:工具输出晚于完结 → 在跑;完结晚于工具输出(或无工具信号)
    # → 已完成。避免"完成后 5 分钟内"被误判为执行中。
    out: dict = {}
    for sid in set(tool_at) | set(done_at):
        t, d = tool_at.get(sid, 0), done_at.get(sid, 0)
        if t > d:
            out[sid] = {"status": "running", "tool": "", "ts": t}
        else:
            out[sid] = {"status": "done", "tool": "", "ts": d or t}
    return out

def latest_completions(sids):
    """批量查会话最新完结轮时间(毫秒),不受滚动窗口限制。

    用于纠正已跟踪会话:完结时间新于我方最后已知事件 → 实为已完成。
    """
    if not sids:
        return {}
    out: dict = {}
    try:
        con = sqlite3.connect(
            f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=0.3)
        try:
            q = ",".join("?" * len(sids))
            rows = con.execute(
                f"SELECT session_id, MAX(completed_at) FROM turn_usage "
                f"WHERE session_id IN ({q}) GROUP BY session_id", sids).fetchall()
        finally:
            con.close()
        out = {sid: int(ts or 0) for sid, ts in rows}
    except Exception:
        pass
    return out
