# temp_server.py —— UDP温度 + 投票 + HTTP API / SSE（仅新包 +vote）
# 依赖：aiohttp（pip install aiohttp）
# 客户端上报格式（仅支持新格式）：
#   <uid>:temp:<float>:vote:<int>     # vote ∈ {-1,0,1}

import asyncio, socket, json, time, sys
import contextlib
from typing import Dict, Tuple, Any, List, Optional
from aiohttp import web

# ================= 配置 =================
UDP_LISTEN_IP   = "0.0.0.0"
UDP_LISTEN_PORT = 8080
HTTP_LISTEN_IP  = "0.0.0.0"
HTTP_LISTEN_PORT= 5000

HISTORY_MAX     = 200        # 每设备最多保留N条历史
EXPIRE_SEC      = 60 * 60    # 最近1小时无更新判离线
# ======================================

# temps[uid] = {"temp": float, "vote": int, "ts": float, "addr": (ip,port)}
temps: Dict[str, Dict[str, Any]] = {}
# history[uid] = [{"ts": float, "temp": float, "vote": int}, ...]
history: Dict[str, List[Dict[str, Any]]] = {}

# SSE 客户端队列
sse_clients: List[asyncio.Queue] = []

# ---------- 工具 ----------
def clamp_vote(v: Optional[int]) -> Optional[int]:
    if v is None: return None
    try: v = int(v)
    except: return None
    if v < -1: v = -1
    if v >  1: v =  1
    return v

def vote_tag(v: Optional[int]) -> Optional[str]:
    if v is None: return None
    return "warm" if v > 0 else "cold" if v < 0 else "conf"

def _broadcast_sse(obj: dict):
    data = json.dumps(obj, ensure_ascii=False)
    dead = []
    for q in sse_clients:
        try: q.put_nowait(data)
        except Exception: dead.append(q)
    for q in dead:
        try: sse_clients.remove(q)
        except ValueError: pass

def _format_row(uid: str, row: Dict[str, Any]) -> Dict[str, Any]:
    ts = row.get("ts", 0.0)
    online = (time.time() - ts) <= EXPIRE_SEC
    ip, port = row.get("addr", ("", 0))
    v = row.get("vote")
    return {
        "uid": uid,
        "temp": row.get("temp"),
        "vote": v,
        "vote_tag": vote_tag(v),
        "ts": ts,
        "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else None,
        "online": online,
        "ip": ip,
        "port": port,
    }

# ---------- UDP 协议 ----------
class TempUDPProtocol(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport
        print(f"[ OK ] UDP listening on {UDP_LISTEN_IP}:{UDP_LISTEN_PORT}")

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        msg = data.decode("utf-8", "ignore").strip()
        parts = msg.split(":")
        # 仅接受：<uid>:temp:<float>:vote:<int>[:...]
        if len(parts) < 5 or parts[1] != "temp":
            return
        uid = parts[0]
        try:
            t = float(parts[2])
        except ValueError:
            return

        # 必须携带 vote；在键值对中查找
        v: Optional[int] = None
        for i in range(3, len(parts) - 1, 2):
            if parts[i] == "vote":
                v = clamp_vote(parts[i+1])
                break
        if v is None:
            return  # 没有 vote 就忽略（按你要求不兼容旧包）

        now = time.time()
        temps[uid] = {"temp": t, "vote": v, "ts": now, "addr": addr}

        lst = history.setdefault(uid, [])
        lst.append({"ts": now, "temp": t, "vote": v})
        if len(lst) > HISTORY_MAX:
            del lst[: len(lst) - HISTORY_MAX]

        payload = {"uid": uid, "temp": t, "vote": v, "vote_tag": vote_tag(v), "ts": now}
        _broadcast_sse(payload)

# ---------- CORS 中间件 ----------
@web.middleware
async def cors_mw(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

# ---------- HTTP 路由 ----------
async def index(request):   # host website
    return web.FileResponse("./index.html")

async def api_health(request):  # GET /api/health
    return web.json_response({"ok": True, "time": time.time()})

async def api_all(request):     # GET /api/temps
    data = [_format_row(uid, row) for uid, row in temps.items()]
    data.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return web.json_response({"devices": data})

async def api_one(request):     # GET /api/temps/{uid}
    uid = request.match_info.get("uid", "")
    row = temps.get(uid)
    if not row:
        return web.json_response({"error": "not found", "uid": uid}, status=404)
    return web.json_response(_format_row(uid, row))

async def api_history(request): # GET /api/temps/{uid}/history
    uid = request.match_info.get("uid", "")
    arr = history.get(uid) or []
    # 附上 tag（不改变原存储）
    out = []
    for it in arr:
        o = {"ts": it["ts"], "temp": it["temp"], "vote": it["vote"], "vote_tag": vote_tag(it["vote"])}
        out.append(o)
    return web.json_response({"uid": uid, "history": out})

async def api_vote_stats(request):  # GET /api/vote_stats?window=600
    q = request.rel_url.query
    try:
        window = int(q.get("window", "600"))  # 秒
    except ValueError:
        window = 600

    now = time.time()
    since = now - max(1, window)

    def zero():
        return {"warm": 0, "conf": 0, "cold": 0}

    total = {"warm": 0, "conf": 0, "cold": 0}
    per = {}

    # 一机一票：对每个 uid 仅取“时间窗内的最近一条”
    for uid, arr in history.items():
        last = None
        # arr 是按时间 append 的，倒序找第一条进入窗口的
        for it in reversed(arr):
            ts = it.get("ts", 0.0)
            if ts < since:
                break
            last = it
            break

        if not last:
            # 兜底：若 history 被裁剪，但 temps 里有且在窗口内，则也可计入
            row = temps.get(uid)
            if not row or row.get("ts", 0.0) < since:
                continue
            last = {"ts": row.get("ts"), "temp": row.get("temp"), "vote": row.get("vote")}

        v = clamp_vote(last.get("vote"))
        if v is None:
            continue
        tag = vote_tag(v)
        if not tag:
            continue

        total[tag] += 1
        per[uid] = {
            "warm": 1 if tag == "warm" else 0,
            "conf": 1 if tag == "conf" else 0,
            "cold": 1 if tag == "cold" else 0,
        }

    payload = {
        "window": window,
        "now": now,
        "total": total,
        "per_uid": per,                 # 总是返回
        "device_count": len(per),       # 方便前端直接用
    }
    return web.json_response(payload)



async def api_sse(request):     # GET /api/sse
    # SSE 基础格式：text/event-stream，按行写 event:/data:，以空行分隔。:contentReference[oaicite:3]{index=3}
    resp = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )
    await resp.prepare(request)

    q: asyncio.Queue = asyncio.Queue()
    sse_clients.append(q)
    print(f"[SSE] client +1, total={len(sse_clients)}")

    try:
        snapshot = {"devices": [_format_row(uid, row) for uid, row in temps.items()]}
        await resp.write(b"event: snapshot\n")
        await resp.write(f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n".encode())

        while True:
            data = await q.get()
            await resp.write(b"event: temp\n")
            await resp.write(f"data: {data}\n\n".encode())
            await resp.write(b"event: temp\n")
            await resp.write(f"data: {data}\n\n".encode())
# 不再需要 await resp.drain()

    except asyncio.CancelledError:
        pass
    except ConnectionResetError:
        pass
    finally:
        try: sse_clients.remove(q)
        except ValueError: pass
        print(f"[SSE] client -1, total={len(sse_clients)}")
    return resp

def make_app():
    app = web.Application(middlewares=[cors_mw])
    app.add_routes([
        web.get("/", index),
        web.get("/api/health", api_health),
        web.get("/api/temps",  api_all),
        web.get("/api/temps/{uid}", api_one),
        web.get("/api/temps/{uid}/history", api_history),
        web.get("/api/vote_stats", api_vote_stats),
        web.get("/api/sse", api_sse),
        web.options("/{tail:.*}", api_health),
    ])
    return app

# ---------- 主入口（跨平台退出） ----------
async def main():
    loop = asyncio.get_running_loop()

    # UDP（asyncio DatagramTransport/Protocol）:contentReference[oaicite:4]{index=4}
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_sock.bind((UDP_LISTEN_IP, UDP_LISTEN_PORT))
    transport, _ = await loop.create_datagram_endpoint(
        lambda: TempUDPProtocol(), sock=udp_sock
    )

    # HTTP（aiohttp Web）:contentReference[oaicite:5]{index=5}
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HTTP_LISTEN_IP, HTTP_LISTEN_PORT)
    await site.start()
    print(f"[ OK ] HTTP on http://{HTTP_LISTEN_IP}:{HTTP_LISTEN_PORT}")
    print("[INFO] Press Ctrl+C to stop")

    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass

    print("[CLEANUP] closing ...")
    transport.close()
    await runner.cleanup()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
    print("\n[EXIT] bye!")
