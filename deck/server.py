"""悬浮窗内置 HTTP 服务:接收 hook 桥接脚本的事件、审批、续跑与放行请求。

POST /event            状态事件,立即返回 {}
POST /permission       审批请求;挂起连接直到 UI 决定或超时
POST /stop-continue    续跑请求;armed 时挂起至多 wait 秒,等 UI 的"续跑"点击
POST /pretooluse       只读放行查询;armed 且工具在白名单时返回 allow
GET  /health           健康检查

所有状态放在 DeckState(锁保护),Qt 主线程用短周期轮询取增量,避免跨线程信号。
"""
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from deck import APP_NAME, __version__

MAX_EVENTS = 200
# 只读自动放行白名单:纯读取类工具,不含任何写操作与网络外发
READONLY_TOOLS = {"Read", "Grep", "Glob", "LS", "TodoRead", "WebSearch"}


class DeckState:
    """线程安全的事件流 + 审批/续跑注册表 + 放行开关。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._pending: dict[str, dict] = {"permission": {}, "stop": {}}
        # UI 线程写、hook 线程读的开关(bool 赋值在 GIL 下安全)
        self.allow_readonly = False
        self.stop_armed = False

    # ---- 事件流 ----

    def add_event(self, ev: dict) -> None:
        with self._lock:
            self._events.append(ev)
            if len(self._events) > MAX_EVENTS:
                self._events = self._events[-MAX_EVENTS:]

    def drain_events(self, after_index: int) -> tuple[list[dict], int]:
        with self._lock:
            batch = self._events[after_index:]
            return batch, len(self._events)

    # ---- 挂起请求(审批 / 续跑共用) ----

    def new(self, kind: str, payload: dict) -> str:
        pid = uuid.uuid4().hex[:12]
        holder = {"payload": payload, "event": threading.Event(), "result": None}
        with self._lock:
            self._pending[kind][pid] = holder
        return pid

    def resolve(self, kind: str, pid: str, result: dict) -> bool:
        with self._lock:
            holder = self._pending[kind].pop(pid, None)
            if holder is None:
                return False
            holder["result"] = result
            holder["event"].set()
        return True

    def wait(self, kind: str, pid: str, timeout: float) -> dict:
        with self._lock:
            holder = self._pending[kind].get(pid)
        if holder is None:
            return {"decision": "timeout"}
        holder["event"].wait(timeout)
        # 决定后条目已被 resolve 移除,结果以 holder 本体为准;
        # 此时再 pop 拿到 None 属正常,绝不能误判为超时
        if holder["event"].is_set() and holder["result"]:
            return holder["result"]
        with self._lock:
            self._pending[kind].pop(pid, None)
        return {"decision": "timeout"}

    def snapshot(self, kind: str) -> dict:
        with self._lock:
            return {pid: dict(h["payload"])
                    for pid, h in self._pending[kind].items()}


class DeckServer:
    def __init__(self, state: DeckState, port: int) -> None:
        self.state = state
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静默访问日志
                pass

            def _reply(self, code: int, obj: dict) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    self._reply(200, {"ok": True, "app": APP_NAME,
                                      "version": __version__})
                elif self.path.startswith("/events"):
                    # 调试端点:返回最近 N 条已收事件(默认 20)
                    qs = self.path.split("?", 1)[-1] if "?" in self.path else ""
                    n = 20
                    for kv in qs.split("&"):
                        if kv.startswith("count="):
                            try:
                                n = max(1, min(200, int(kv[6:])))
                            except ValueError:
                                pass
                    with server.state._lock:
                        evs = server.state._events[-n:]
                    self._reply(200, {"count": len(evs), "events": evs})
                else:
                    self._reply(404, {"error": "not found"})

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except Exception:
                    self._reply(400, {"error": "bad json"})
                    return

                if self.path == "/event":
                    server.state.add_event(payload)
                    self._reply(200, {})
                elif self.path == "/permission":
                    wait = min(float(payload.get("wait") or 165), 165.0)
                    pid = server.state.new("permission", payload)
                    self._reply(200, server.state.wait("permission", pid, wait))
                elif self.path == "/stop-continue":
                    if not server.state.stop_armed:
                        self._reply(200, {"decision": "off"})
                        return
                    wait = min(float(payload.get("wait") or 8), 12.0)
                    pid = server.state.new("stop", payload)
                    self._reply(200, server.state.wait("stop", pid, wait))
                elif self.path == "/resolve":
                    # 远程决定(与悬浮窗按钮走同一条 state.resolve 通路);
                    # 不带 pid 时作用于该类别下唯一(或最早的)挂起请求
                    kind = str(payload.get("kind") or "permission")
                    pid = str(payload.get("pid") or "")
                    decision = str(payload.get("decision") or "allow")
                    if decision not in ("allow", "deny", "continue"):
                        self._reply(400, {"error": "bad decision"})
                        return
                    if not pid:
                        snap = server.state.snapshot(kind)
                        if not snap:
                            self._reply(200, {"resolved": 0})
                            return
                        pid = next(iter(snap))
                    if kind == "stop":
                        result = {"decision": decision}
                    else:
                        result = {"decision": decision, "reason": str(
                            payload.get("reason") or "ZCodeDeck 远程决定")}
                    ok = server.state.resolve(kind, pid, result)
                    self._reply(200, {"resolved": 1 if ok else 0})
                elif self.path == "/pretooluse":
                    tool = str(payload.get("tool") or "")
                    if server.state.allow_readonly and tool in READONLY_TOOLS:
                        self._reply(200, {"decision": "allow"})
                    else:
                        self._reply(200, {})
                else:
                    self._reply(404, {"error": "not found"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                         name="deck-http").start()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
