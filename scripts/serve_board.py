# -*- coding: utf-8 -*-
"""项目进度看板 · 本地服务

为什么需要它：
    浏览器沙箱不允许网页执行本地程序（点 .cmd 链接只会把源码显示出来）。
    所以「看板里点按钮刷新」只能靠一个本地服务代劳。

它做三件事：
    GET  /             → 返回最新的 index.html（每次读磁盘，不缓存）
    POST /refresh      → 跑 project_watch.py --board，返回 {ok, time, out}
    GET  /refresh      → 同上（方便浏览器直接测）

只监听 127.0.0.1，不对外。
"""
import datetime
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ★★★ 2026-11-22 发布前脱敏：原来写死 E:\ObsidianVault\... 和 C:\Users\<某人>\...
#   改成从 paths.py 取（它读 agent-config.json），并支持环境变量覆盖。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

BOARD_DIR = os.path.join(paths.PROJECTS_ROOT, "_看板")
BOARD = os.path.join(BOARD_DIR, "index.html")
FOCUS = os.path.join(BOARD_DIR, "focus.html")
HOME = os.path.join(BOARD_DIR, "home.html")
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
PW = os.path.join(SCRIPTS, "project_watch.py")
BF = os.path.join(SCRIPTS, "build_focus.py")
# ★ 端口可配：8765 被占时（比如你已经开着一个）用 BOARD_PORT=8799 换一个
PORT = int(os.environ.get("BOARD_PORT") or 8765)


def do_refresh():
    judge_out = ""
    # ★ 第一步：给「有变动的项目」重新写判断（2026-11-20 加）
    #   只判「文件变动晚于 _进度总表.md 修改时间」的项目，通常 0-3 个。
    #   失败不影响后面的渲染 —— 刷新按钮至少要能把数据刷出来。
    #
    # ★★★ 2026-11-22 发布版降级：judge_lite.py 要接大模型（需要自己的 API key），
    #   不在发布包里。缺它时【跳过重判，只重建页面】，并把这件事说出来 ——
    #   不许静默跳过（静默跳过 = 你以为判断更新了，其实是旧的）。
    JL = os.path.join(SCRIPTS, "judge_lite.py")
    if not os.path.isfile(JL):
        judge_out = "（未装 judge_lite.py：跳过重判，本次只重建页面）"
    else:
        try:
            j = subprocess.run(
                [sys.executable, JL],
                cwd=SCRIPTS, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=240,
            )
            judge_out = ((j.stdout or "") + (j.stderr or "")).strip()
        except Exception as e:
            judge_out = "judge 出错：%s" % e

    try:
        r = subprocess.run(
            [sys.executable, PW, "--board"],
            cwd=SCRIPTS, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
        ok = r.returncode == 0 and os.path.exists(BOARD)
        out = ((r.stdout or "") + (r.stderr or "")).strip()[:2000]
    except Exception as e:
        ok, out = False, "异常：%s" % e

    mt = ""
    if os.path.exists(BOARD):
        mt = datetime.datetime.fromtimestamp(os.path.getmtime(BOARD)).strftime("%m-%d %H:%M:%S")

    # ★ 顺手重建聚焦看板（不花时间，2 秒内）
    #   ★ 发布版降级：build_focus.py 不在包里（它依赖你的「三层目标」数据），缺它就跳过
    focus_ok = False
    if os.path.isfile(BF):
        try:
            fb = subprocess.run(
                [sys.executable, BF], cwd=SCRIPTS, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
            focus_ok = fb.returncode == 0 and os.path.exists(FOCUS)
        except Exception:
            focus_ok = False

    return {"ok": ok, "time": mt, "out": out, "judge": judge_out[-1200:], "focus": focus_ok}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # ★ CORS：为了让人从 file:// 双击 index.html 打开时按钮也能用。
        #   file:// 页面的 origin 是 "null"，浏览器允许 "null" 匹配通配符 *。
        #   只监听 127.0.0.1，不对外，通配符没有安全问题。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        # 预检请求：直接放行
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/ping", "/api/ping"):
            self._send(200, json.dumps({"ok": True, "service": "serve_board"}, ensure_ascii=False),
                       "application/json; charset=utf-8")
            return
        if p in ("/suspects", "/api/suspects"):
            # ★ 队列检查（2026-11-21 改造）
            #   两类分开：
            #     · 带「验收: 判据」的条目 → 跑命令，通过就自动打勾（确定性，不用猜）
            #     · 没判据的条目 → 退回语义匹配，只提示，人点确认（假阳性 50% 那条路）
            #   GET 但有副作用（打勾），这是本地单人工具，换来「刷新一次就对齐」。
            try:
                import check_queue
                r = check_queue.scan(autotick=True)
                res = {
                    "ok": True,
                    "suspects": r["suspects"],
                    "verified": r["verified"],
                    "failed": r["failed"],
                }
            except Exception as e:
                res = {"ok": False, "out": str(e), "suspects": [], "verified": [], "failed": []}
            self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if p in ("/refresh", "/api/refresh"):
            self._send(200, json.dumps(do_refresh(), ensure_ascii=False), "application/json; charset=utf-8")
            return
        if p in ("/open-folder", "/api/open-folder"):
            # ★ 浏览器打开的 file:// 目录是「网页版文件列表」，点 .cmd 只会显示源码。
            #   要打开真正的资源管理器窗口，只能由服务代为启动。
            try:
                os.startfile(SCRIPTS)
                res = {"ok": True, "path": SCRIPTS}
            except Exception as e:
                res = {"ok": False, "out": str(e)}
            self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if p in ("/focus", "/focus.html"):
            if not os.path.exists(FOCUS):
                # 没生成过就现场生成一次
                if os.path.isfile(BF):
                    try:
                        subprocess.run([sys.executable, BF], cwd=SCRIPTS,
                                       capture_output=True, timeout=120)
                    except Exception:
                        pass
                if not os.path.exists(FOCUS):
                    self._send(404, "focus.html 不存在，先跑一次 python build_focus.py"
                               "（发布包不含这个脚本，聚焦页属于自用版）",
                               "text/plain; charset=utf-8")
                    return
            with open(FOCUS, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if p in ("/project", "/project.html", "/index.html"):
            if not os.path.exists(BOARD):
                self._send(404, "index.html 不存在，先跑一次 刷新项目看板.cmd", "text/plain; charset=utf-8")
                return
            with open(BOARD, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if p in ("/", "/home", "/home.html"):
            # ★ 默认页 = 标签页外壳（项目进度 / 当前聚焦）
            if not os.path.exists(HOME):
                self._send(404, "home.html 不存在（标签页外壳）", "text/plain; charset=utf-8")
                return
            with open(HOME, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        p = self.path.split("?")[0]
        if p in ("/tick", "/api/tick"):
            # ★ 人工确认打勾（2026-11-20）
            #   body: {"project": "内容看板@WorkBuddy", "fp": "README 从 v5.1 重写到 v9（只改 "}
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8") if n else "{}"
                d = json.loads(raw or "{}")
                import check_queue
                res = check_queue.do_tick(d.get("project", ""), d.get("fp", ""))
            except Exception as e:
                res = {"ok": False, "out": "参数错误：%s" % e}
            self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if p in ("/refresh", "/api/refresh"):
            self._send(200, json.dumps(do_refresh(), ensure_ascii=False), "application/json; charset=utf-8")
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        # 静默：不往控制台刷访问日志
        pass


def main():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print("端口 %d 起不来（可能已经有一个在跑了）：%s" % (PORT, e))
        return 1

    print("=" * 58)
    print("项目进度看板 · 本地服务")
    print("  看板地址  http://127.0.0.1:%d/" % PORT)
    print("  刷新接口  http://127.0.0.1:%d/refresh" % PORT)
    print("  只监听本机，不对外。关掉这个窗口就停。")
    print("=" * 58)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
