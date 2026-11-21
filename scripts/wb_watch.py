#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb_watch.py · 队列完成监听 + 派单心跳（v3）

v3 新增（2026-11-15）：三次心跳
------------------------------
用户的原话：「在你给我指令之后，监测执行方有没有更新项目的 memory」

派单 = 队列里还有「⏳ 待发」。派单期间只说三句话：

  ① ▶ 它动了      —— 该项目的 memory 出现新内容（每个派单周期只报一次）
  ② ✅ 完成了      —— 队列某条被打勾 + 推荐下一条（v2 原有逻辑，不变）
  ③ ⚠ 卡住了      —— 派单 STALL_HOURS 小时，memory 一个字没动（只报一次）

★ 中间 WB 写多少段 memory 都不再报 —— 否则又变成刷屏，跟 v1 一个毛病。
★ 队列没有待发 → 三个标记全部重置，下一单重新开始。

v2 设计（保留）
--------------
★★★★ 队列撒谎比队列空更糟。
误打勾会让用户以为事情做完了 —— 代价远高于漏打（漏打他说一句就能补）。
所以：只看 memory 本次新增行；无基线不打勾；行数变少（被重写）跳过。
"""
import os
import re
import sys
import json
import time
import hashlib
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths                      # ★ 所有路径的唯一来源（换设备改 agent-paths.json，不碰脚本）
import project_watch as pw
import queue as Q
import judge as J

STATE = os.path.join(paths.CACHE_DIR, "wb_watch_state.json")
LOCK = os.path.join(paths.CACHE_DIR, "judge.lock")
HEARTBEAT = os.path.join(paths.CACHE_DIR, "project_watch_heartbeat.txt")
OUTPUT_DIR = paths.OUTPUT_DIR
QUEUE_NAME = "_给WB.md"
MAX_TICKS_PER_SCAN = 3          # 一次扫描最多打几个勾（防一次刷太多）
STALL_HOURS = 4                 # 派单后多少小时没动静 → 报「卡住了」
SUMMARY_CHARS = 90              # 心跳摘要截断长度

# ---- 产物监控（2026-11-16 加）----
# 目的：用户在 WB 里【对话式】改文件（没有我方指令、它可能不写 memory）时，
#      也能在 5 分钟内知道「有文件变了、是哪个」。
# ★ 它治「失明」，不治「失察」—— 只能报「哪个文件变了」，
#   报不了「改成了什么样、对不对」。后者要么靠它写 memory，要么人打开看。
ARTIFACT_MAX_DEPTH = 2          # 顶层 + 一层子目录（实测 1137 文件 / 30ms）
ARTIFACT_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "dist", "build", ".cache", "vendor",
    ".next", ".nuxt", "coverage", ".venv", "venv", "clone_work",
    "cache", "cache_data", "code cache", "gpucache", "crashpad",
    # ★ 2026-11-17 实测：跑一次 playwright 就冒出 2 个 .playwright-cli 临时文件，
    #   被当成「产物变了」。同理加 .tmp / tmp / .pytest_cache。
    ".playwright-cli", "playwright-report", "test-results", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "tmp", ".tmp",
    "blob_storage", "service worker", "local storage", ".workbuddy",
}
ARTIFACT_SKIP_EXT = (".log", ".lock", ".pyc", ".pyo", ".swp")

# ---- cron 输出清理（2026-11-16 加）----
# 5 分钟一次 → 一天 168 个输出文件，其中绝大多数是「silent (empty output)」。
OUTPUT_KEEP_DAYS = 7            # 保留天数
OUTPUT_CLEAN_HOURS = 24         # 多久清一次（不用每轮都扫目录）


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def digest(text):
    return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def acquire_lock():
    """判断要跑 2–3 分钟。拿不到锁就降级（只报事实，不给判断），绝不阻塞。"""
    try:
        if os.path.exists(LOCK):
            if time.time() - os.path.getmtime(LOCK) < 600:   # 10 分钟视为僵死
                return False
            os.remove(LOCK)
        with open(LOCK, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return False


def release_lock():
    try:
        os.remove(LOCK)
    except Exception:
        pass


def scan_memory():
    """{项目名: {日志文件名: {mtime, hash, path, nlines}}}"""
    out = {}
    if not os.path.isdir(pw.ROOT):
        return out
    for d in sorted(os.listdir(pw.ROOT)):
        p = os.path.join(pw.ROOT, d)
        if not os.path.isdir(p) or d.startswith("_"):
            continue
        mdir = os.path.join(p, ".workbuddy", "memory")
        if not os.path.isdir(mdir):
            continue
        files = {}
        for f in os.listdir(mdir):
            if not re.match(r"\d{4}-\d{2}-\d{2}\.md$", f):
                continue
            fp = os.path.join(mdir, f)
            try:
                txt = open(fp, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            files[f] = {"mtime": os.path.getmtime(fp), "hash": digest(txt),
                        "path": fp, "nlines": len(txt.split(chr(10)))}
        if files:
            out[d] = files
    return out


def pending_projects():
    """所有队列里还有「⏳ 待发」的项目 → {项目名: (队列路径, 解析结果)}"""
    out = {}
    if not os.path.isdir(pw.ROOT):
        return out
    for d in sorted(os.listdir(pw.ROOT)):
        p = os.path.join(pw.ROOT, d)
        if not os.path.isdir(p) or d.startswith("_"):
            continue
        # ★ 用 paths.find_queue：按别名顺序找，不再写死 _给WB.md
        #   （paths 探测认 4 个别名，读取只认 1 个 → 用 _给Codex.md 的人静默失效）
        qp = paths.find_queue(p)
        if not os.path.exists(qp):
            continue
        q = Q.parse_queue(qp)
        if q and q["items"]:
            out[d] = (qp, q)
    return out


def grep_topic(text, chars=SUMMARY_CHARS):
    """从新增 memory 里抓一段能看懂的话（跳过空行、标题符号、表格线、纯日期行）"""
    for line in text.split(chr(10)):
        t = line.strip().lstrip("#>-*·　 ").strip()
        t = re.sub(r"\*\*", "", t)
        if len(t) < 8:
            continue
        if set(t) <= set("-|+= "):
            continue
        # ★ 跳过纯日期 / 纯时间 / 纯数字（如「2026-11-15」「10:30」「1.」）
        if re.fullmatch(r"[\d\-–/. :年月日]+", t):
            continue
        if re.fullmatch(r"[\d\-–/. :年月日]+\s*[\u4e00-\u9fa5]{0,2}", t):
            continue
        return t[:chars] + ("…" if len(t) > chars else "")
    return "（有新内容，但抓不出摘要）"


def new_memory_text(files, prev):
    """★ 只取【本次新增的行】。

    ★★★ 关键：如果拿整个 memory 文件去匹配，文件里任何出现过的词都能配上。
        实测后果：「占位符总清单」那条明明没做完（报告里写「P0 #1-5 仍待提供」），
        只因为提到过这个词就被打了勾。所以只看新增段。

    返回 (新增文本, 是否有可比基线)。无基线的文件一律不匹配 —— 宁漏不错。
    """
    parts, has_base = [], False
    picks = sorted(files.items(), key=lambda kv: -kv[1]["mtime"])[:2]
    for fname, info in picks:
        try:
            txt = open(info["path"], encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        all_lines = txt.split(chr(10))
        p = prev.get(fname)
        if not isinstance(p, dict) or not isinstance(p.get("nlines"), int):
            continue                      # 无基线 → 不打勾（首次只建基线）
        n0 = p["nlines"]
        if len(all_lines) <= n0:
            continue                      # 没有新增，或被重写
        parts.append(chr(10).join(all_lines[n0:]))
        has_base = True
    return chr(10).join(parts), has_base


def check_heartbeat(max_hours=26):
    """检查「采集任务」（那个每天跑全量汇报的 cron）还活着没有。

    ★★★ 2026-11-16 改：心跳文件不存在时【静默】，不再报警。
        原因：原来返回「采集任务从未跑过」，但新用户可能只装了 5 分钟那个 cron、
        根本没配每日汇报 —— 于是每 5 分钟发一条警告，纯噪音。
        判据：文件不存在 = 没配（用户的选择）；文件在但过期 = 真的坏了（要报）。
    """
    hb = HEARTBEAT
    if not os.path.exists(hb):
        return None                      # 没配每日汇报 → 静默
    try:
        ts = datetime.datetime.strptime(open(hb, encoding="utf-8").read().strip()[:19],
                                        "%Y-%m-%d %H:%M:%S")
    except Exception:
        return "⚠️ 心跳文件读不出来 —— 检查定时任务"
    hours = (datetime.datetime.now() - ts).total_seconds() / 3600
    if hours > max_hours:
        return (f"🔴 采集任务已 {hours:.0f} 小时没跑（上次 {ts:%m-%d %H:%M}）\n"
                f"　→ 看板上的「现在到哪 + 建议」可能是旧的，检查定时任务")
    return None


def scan_artifacts(maxdepth=ARTIFACT_MAX_DEPTH):
    """扫各项目关键文件的 (mtime, size)，用于发现「产物变了但没写 memory」。

    返回 {项目: {相对路径: [mtime, size]}}

    ★ 为什么要它（2026-11-16）：用户在 WB 里【对话式】改文件时，没有我方指令，
      它可能不写 memory → 只靠 scan_memory 完全看不见（实测要等到次日 06:20 全量汇报）。
      加这层后，5 分钟内能报「哪个文件变了」。

    ★ 它治「失明」，不治「失察」：知道哪个文件变了，不知道改成了什么样。

    排除项（每一个都有理由）：
      · `.workbuddy/` —— memory 归 scan_memory 管，混进来会重复报
      · `_给WB.md` 等队列别名 —— 那是我们自己写的，会一直变
      · 备份 / 临时文件 —— 见 pw.SKIP_FILE_HINTS
      · 深层目录（默认 2 层）—— 实测 maxdepth=2 是 1137 文件 / 30ms；
        不限深度会到 12212 个（单个项目 9498），没必要
    """
    out = {}
    if not os.path.isdir(pw.ROOT):
        return out

    for d in sorted(os.listdir(pw.ROOT)):
        p = os.path.join(pw.ROOT, d)
        if not os.path.isdir(p) or d.startswith("_"):
            continue
        base_depth = p.rstrip(os.sep).count(os.sep)
        files = {}
        for dp, dirs, fs in os.walk(p):
            depth = dp.rstrip(os.sep).count(os.sep) - base_depth
            if depth >= maxdepth:
                dirs[:] = []
                continue
            dirs[:] = [x for x in dirs
                       if x.lower() not in ARTIFACT_SKIP_DIRS
                       and not x.lower().startswith("_edge")]
            for f in fs:
                low = f.lower()
                if any(h in low for h in pw.SKIP_FILE_HINTS):
                    continue
                if low.endswith(ARTIFACT_SKIP_EXT):
                    continue
                if f in pw.QUEUE_ALIASES:
                    continue
                fp = os.path.join(dp, f)
                try:
                    stt = os.stat(fp)
                except OSError:
                    continue
                files[os.path.relpath(fp, p)] = [round(stt.st_mtime, 3), stt.st_size]
        if files:
            out[d] = files
    return out


def diff_artifacts(prev, cur):
    """对比两轮产物快照，返回变化的文件列表。

    返回 [(相对路径, 旧size, 新size, 新mtime), ...]，按 mtime 倒序。

    ★ 只报「内容变了」（size 或 mtime 变）。新增/删除文件也算，但不单独区分。
    """
    if not isinstance(prev, dict):
        return []
    changes = []
    for f, sig in cur.items():
        old = prev.get(f)
        if old is None:
            changes.append((f, 0, sig[1], sig[0]))
            continue
        if old[0] != sig[0] or old[1] != sig[1]:
            changes.append((f, old[1], sig[1], sig[0]))
    changes.sort(key=lambda x: -x[3])
    return changes


def human_size(n):
    """12345 → '12.1KB'"""
    try:
        n = int(n)
    except Exception:
        return "?"
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def cleanup_outputs(keep_days=OUTPUT_KEEP_DAYS):
    """清掉 N 天前的「空跑」cron 输出文件。

    ★ 5 分钟一次 → 一天 168 个输出文件，绝大多数是 `**Status:** silent (empty output)`。
      不清会一个月攒 5000 个。

    ★★★ 只删「silent (empty output)」的；有内容的输出一律保留 —— 那是历史记录。

    返回 (删除数, 保留数)
    """
    if not os.path.isdir(OUTPUT_DIR):
        return 0, 0
    cutoff = time.time() - keep_days * 86400
    deleted = kept = 0
    for job in os.listdir(OUTPUT_DIR):
        jd = os.path.join(OUTPUT_DIR, job)
        if not os.path.isdir(jd):
            continue
        for f in os.listdir(jd):
            if not f.endswith(".md"):
                continue
            fp = os.path.join(jd, f)
            try:
                if os.path.getmtime(fp) > cutoff:
                    continue
                head = open(fp, encoding="utf-8", errors="ignore").read(400)
                if "silent (empty output)" in head:
                    os.remove(fp)
                    deleted += 1
                else:
                    kept += 1
            except OSError:
                continue
    return deleted, kept


def clean_next(it):
    _t = re.sub(r"^\*\*[\d\-]+\*\*\s*", "", it["text"]).replace("**", "")
    return {"text": _t,
            "full": re.sub(r"\s+", " ", (_t + " " + " ".join(x[1].strip() for x in it["extra"])).strip())}


def main():
    init = "--init" in sys.argv
    dry = "--dry" in sys.argv

    # ★ 手动验证用：清一次并报告结果，不碰任何事件逻辑
    if "--cleanup-report" in sys.argv:
        n_del, n_keep = cleanup_outputs()
        print(f"清理结果：删除 {n_del} 个过期空跑输出，保留 {n_keep} 个有内容的")
        st = load_state()
        st.setdefault("__meta", {})["last_cleanup"] = time.time()
        st["__meta"]["last_cleanup_result"] = [n_del, n_keep,
                                               datetime.datetime.now().strftime("%Y-%m-%d %H:%M")]
        save_state(st)
        return

    now = datetime.datetime.now()
    stamp = now.strftime("%m-%d")

    cur = scan_memory()
    art_cur = scan_artifacts()          # ★ 2026-11-16：产物快照（治「对话式改动静默」）
    st = load_state()
    events = []        # 完成事件
    beats = []         # 心跳事件（动了 / 卡住了）
    art_changes = []   # ★ 产物变了但 memory 没变的项目
    mem_changed_projs = set()   # 本轮 memory 有变化的项目

    # ---- ⓪ --init 只建基线：不碰心跳标记、不产生任何事件 ----
    if init:
        # ★★★ 一个都没扫到就【不许说成功】（2026-11-21 修）
        #   原来无论扫到 0 个还是 20 个，都打印「基线已建」并退出 0。
        #   于是 projects_root 没配的人会以为装好了，然后永远等不到事件，
        #   最后得出「这东西没用」——静默成功是这里最糟的失败方式。
        if not cur and not art_cur:
            print("✗ 一个项目都没扫到 —— 基线没建，先配好 projects_root。", file=sys.stderr)
            print("", file=sys.stderr)
            try:
                import paths
                print(paths.describe(), file=sys.stderr)
            except Exception as e:
                print(f"（顺带：连 paths.py 都读不了：{e}）", file=sys.stderr)
            sys.exit(1)
        for proj, files in cur.items():
            pst = st.setdefault(proj, {})
            for fname, info in files.items():
                pst[fname] = {"hash": info["hash"], "nlines": info["nlines"]}
        # ★ 同时建产物基线（否则第一次真跑会把「所有文件」当成刚变的）
        for proj, arts in art_cur.items():
            st.setdefault(proj, {})["__artifacts"] = arts
        save_state(st)
        print(f"（监听基线已建：memory {len(cur)} 项目 / 产物 {len(art_cur)} 项目，未产生任何事件）",
              file=sys.stderr)
        return

    pend = pending_projects()

    # ---- ① 派单状态维护 ----
    for proj in pend:
        pst = st.setdefault(proj, {})
        if "__pending_since" not in pst:
            pst["__pending_since"] = now.strftime("%Y-%m-%d %H:%M")
            pst["__notified_start"] = False
            pst["__notified_stall"] = False

    # ---- ①b 外部改队列检测 ----
    # ★★★ 2026-11-15 实测：WB 自己把 _给WB.md 的条目打勾并把报告贴进去，
    #      导致 cron 看不到「⏳ 待发」→ 永远不会触发判断。
    #      这里对比「上次扫描后的待发列表」和「本次扫描时的待发列表」：
    #      消失的条目里，凡不是本轮 tick 掉的，就是外部改的 → 报警。
    ticked_now = set()          # 本轮 tick 掉的条目指纹
    ext_changed = []            # 外部改的条目指纹

    for proj in list(st.keys()):
        if proj.startswith("__"):
            continue
        if proj not in pend:               # 队列已空 → 三个标记重置
            for k in ("__pending_since", "__notified_start", "__notified_stall"):
                st[proj].pop(k, None)

    for proj, (qp, q) in pend.items():
        pst = st.setdefault(proj, {})
        prev_p = pst.get("__last_pending")
        cur_p = [Q.fp(it) for it in q["items"]]
        if isinstance(prev_p, list):
            vanished = [x for x in prev_p if x not in cur_p]
            real = [x for x in vanished if x not in ticked_now]
            if real:
                ext_changed.append({"proj": proj, "items": real,
                                    "left": len(q["items"])})
        # ★ 不在这里写入 —— 必须等本轮所有 tick 结束后再写，
        #   否则下一轮会把「自己打的勾」当成外部修改（实测误报）。

    # 队列已清空的项目：清掉基线（下次重新建）
    for proj in list(st.keys()):
        if proj.startswith("__") or proj in pend:
            continue
        st[proj].pop("__last_pending", None)

    # ---- ② 扫 memory：心跳「动了」+ 完成打勾 ----
    for proj, files in cur.items():
        prev_raw = st.get(proj) or {}
        prev = {k: v for k, v in prev_raw.items() if not k.startswith("__")}
        # 兼容旧 state 格式（值是 hash 字符串）→ 视为无基线，只建基线不打勾
        prev = {k: v if isinstance(v, dict) else {"hash": v} for k, v in prev.items()}
        changed = [f for f in files if (prev.get(f) or {}).get("hash") != files[f]["hash"]]
        if changed:
            mem_changed_projs.add(proj)    # ★ 供产物检测判断「有没有记录」
        if not changed:
            continue

        # ★ 心跳 ①：派单期间第一次动 → 报一次
        if proj in pend:
            pst = st.setdefault(proj, {})
            if not pst.get("__notified_start"):
                mem_text0, base0 = new_memory_text(files, prev)
                src = mem_text0 if (base0 and mem_text0.strip()) else \
                    open(files[max(files, key=lambda k: files[k]["mtime"])]["path"],
                         encoding="utf-8", errors="ignore").read()
                beats.append({"proj": proj, "kind": "start",
                              "topic": grep_topic(src),
                              "left": len(pend[proj][1]["items"])})
                pst["__notified_start"] = True

        qpath = paths.find_queue(os.path.join(pw.ROOT, proj))
        q = Q.parse_queue(qpath)
        if not q or not q["items"]:
            continue

        # ★★★ 只匹配【本次新增的行】—— 详见 new_memory_text 的注释
        mem_text, has_base = new_memory_text(files, prev)
        if not has_base or not mem_text.strip():
            continue
        ticks = 0
        while ticks < MAX_TICKS_PER_SCAN:
            idx, why = Q.match_done(mem_text, q["items"])
            if idx is None:
                break
            if dry:
                it = q["items"][idx]
                nxt = clean_next(q["items"][idx + 1]) if idx + 1 < len(q["items"]) else None
                events.append({"proj": proj, "done": "[DRY] " + it["text"][:100],
                               "next": nxt, "why": why})
                break
            ticked_now.add(Q.fp(q["items"][idx]))
            done_text = Q.tick_item(qpath, q, idx, stamp)
            q = Q.parse_queue(qpath)          # 重新解析（行号已变）
            nxt = Q.next_item(qpath)
            events.append({"proj": proj, "done": done_text, "next": nxt, "why": why})
            ticks += 1
            # 打勾后重置心跳标记 —— 若队列还有下一条，下一单会重新报「动了」
            pst2 = st.setdefault(proj, {})
            pst2["__notified_start"] = False
            pst2["__notified_stall"] = False
            pst2["__pending_since"] = now.strftime("%Y-%m-%d %H:%M")

    # ---- ②b 产物变化检测：产物变了但 memory 没记录 → 报警 ----
    # ★ 治的是「用户在 WB 里对话式改文件，没走队列也没写 memory」这个盲区。
    # ★ 只在「有 memory 基线」之后才判（首次跑只建基线，否则会把所有文件当刚变的）。
    for proj, arts in art_cur.items():
        pst = st.get(proj) or {}
        prev_art = pst.get("__artifacts")
        if not isinstance(prev_art, dict):
            continue                      # 没基线 → 本轮只建，不报
        if proj in mem_changed_projs:
            continue                      # memory 有记录 → 正常，②已经报过「动了」
        ch = diff_artifacts(prev_art, arts)
        if not ch:
            continue
        # ★ 2026-11-17：带上「memory 最后一次写入时间」——
        #   用来区分「从来没记过」和「记过、之后又改了」。
        #   实测（11-17 12:07 写 memory → 12:15、12:21 又改海报）：
        #   原来的文案只说「memory 没记录」，会让人以为完全没写、跑去重写一遍。
        mm = 0
        for fmeta in (cur.get(proj) or {}).values():
            mm = max(mm, fmeta.get("mtime") or 0)
        art_changes.append({"proj": proj, "changes": ch[:6], "total": len(ch),
                            "mem_mtime": mm})

    # ---- ③ 心跳「卡住了」 ----
    for proj, (qp, q) in pend.items():
        pst = st.get(proj) or {}
        since = pst.get("__pending_since")
        if not since or pst.get("__notified_stall"):
            continue
        try:
            t0 = datetime.datetime.strptime(since, "%Y-%m-%d %H:%M")
        except Exception:
            continue
        hours = (now - t0).total_seconds() / 3600
        if hours >= STALL_HOURS:
            has_mem = proj in cur
            beats.append({"proj": proj, "kind": "stall",
                          "topic": f"派单 {hours:.1f} 小时，{'memory 无新增内容' if has_mem else '该项目连 memory 目录都没有'}" if has_mem
                                   else f"派单 {hours:.1f} 小时，还没建过 .workbuddy/memory",
                          "left": len(q["items"])})
            pst["__notified_stall"] = True

    if not dry:
        # ★ 本轮所有 tick 已结束 → 这时才记录最新待发列表
        for proj in pend:
            try:
                _q = Q.parse_queue(paths.find_queue(os.path.join(pw.ROOT, proj)))
                st.setdefault(proj, {})["__last_pending"] = (
                    [Q.fp(it) for it in _q["items"]] if _q else [])
            except Exception:
                pass
        # ★★★ 必须把 memory 新基线写回 state，否则下一轮会认为「又变了」→ 重复报「动了」
        for proj, files in cur.items():
            pst = st.setdefault(proj, {})
            for fname, info in files.items():
                pst[fname] = {"hash": info["hash"], "nlines": info["nlines"]}
        # ★ 产物基线（每轮都更新，否则同一批变化会连续报 N 轮）
        for proj, arts in art_cur.items():
            st.setdefault(proj, {})["__artifacts"] = arts
        # ★ 顺带清一次旧的空跑输出（每 OUTPUT_CLEAN_HOURS 小时才真扫目录）
        # ★★ 不在正常运行时打印 —— 否则每天都会推一条「清理了 N 个」打扰用户。
        #    要验证就手动跑 `python wb_watch.py --cleanup-report`。
        try:
            last_cl = st.setdefault("__meta", {}).get("last_cleanup", 0)
            if time.time() - last_cl > OUTPUT_CLEAN_HOURS * 3600:
                n_del, n_keep = cleanup_outputs()
                st["__meta"]["last_cleanup"] = time.time()
                st["__meta"]["last_cleanup_result"] = [n_del, n_keep,
                                                       now.strftime("%Y-%m-%d %H:%M")]
        except Exception:
            pass
        save_state(st)

    hb_warn = check_heartbeat()

    # ★ 什么都没发生 → 完全静默
    if not events and not beats and not hb_warn and not ext_changed and not art_changes:
        return

    # ★ 同一次扫描里，如果某项目已经报了「完成」，就不再报它的「动了」—— 那句是噪音
    done_projs = {e["proj"] for e in events}
    beats = [b for b in beats if not (b["kind"] == "start" and b["proj"] in done_projs)]

    out = []
    if hb_warn:
        out.append(hb_warn)

    # ★ 外部改队列报警（WB 抢着打勾时会触发）
    for e in ext_changed:
        names = "／".join(e["items"][:2])
        more = f" 等 {len(e['items'])} 条" if len(e["items"]) > 1 else ""
        out.append(f"⚠ {e['proj']}　队列被外部改了：「{names}」{more}从待发消失" + chr(10) +
                   f"　　剩下 {e['left']} 条待发 —— 如果不是你手动改的，就是 WB 自己打了勾" + chr(10) +
                   f"　　★ 这轮没触发验收判断。把那条改回 `- [ ]` 放回「⏳ 待发」，15 分钟内会自动判断")

    for b in beats:
        if b["kind"] == "start":
            out.append(f"▶ {b['proj']}　WB 动了（队列还剩 {b['left']} 条）\n"
                       f"　　{b['topic']}\n"
                       f"　　（这是开场信号，还看不出做完没有 —— 等它写「完成/已交付」再判）")
        elif b["kind"] == "stall":
            out.append(f"⚠ {b['proj']}　{b['topic']}\n"
                       f"　　队列还有 {b['left']} 条待发 —— 检查 WB 是否收到指令、或是否卡住")

    # ★ 产物变了但 memory 没记录（对话式修改的盲区）
    for a in art_changes:
        mm = a.get("mem_mtime") or 0
        if mm:
            when = datetime.datetime.fromtimestamp(mm).strftime("%m-%d %H:%M")
            head = f"⚠ {a['proj']}　产物变了（memory 最后一次写入 {when}，之后又改了 {a['total']} 个文件）"
        else:
            head = f"⚠ {a['proj']}　产物变了（这个项目还没有 memory 日志）"
        lines = [head]
        for f, old_sz, new_sz, mt in a["changes"]:
            hhmm = datetime.datetime.fromtimestamp(mt).strftime("%H:%M")
            delta = new_sz - old_sz
            sign = "+" if delta >= 0 else "−"
            if old_sz == 0:
                lines.append(f"　　{f}　{hhmm}　新增 {human_size(new_sz)}")
            else:
                lines.append(f"　　{f}　{hhmm}　{human_size(old_sz)} → {human_size(new_sz)}"
                             f"（{sign}{human_size(abs(delta))}）")
        if a["total"] > len(a["changes"]):
            lines.append(f"　　…还有 {a['total'] - len(a['changes'])} 个文件")
        if mm:
            # ★ 情况一：memory 记过，之后又改了 —— 只说明情况，不教学。
            #   实测（11-17）：WB 12:07 写得非常详细，12:15、12:21 又调了两版样。
            #   那种情况下再教用户「下次写 memory」是噪音，他已经写了。
            lines.append("　　—— 这些是 memory 写完之后又改的。")
            lines.append("　　　 改动大的话补一句「加到 memory 末尾」；一句话的小改可以忽略。")
        else:
            # ★ 情况二：这个项目从来没记过 —— 才教他怎么写。
            lines.append("　　—— 这个项目还没有 memory 日志。")
            lines.append("　　　 下次改完补一句「把这次改动加到 memory」，我才知道改了什么。")
        out.append("\n".join(lines))

    if events:
        if out:
            out.append("")
        # ★ 一次扫描里同一项目的完成事件合并成一次判断（省 token）
        by_proj = {}
        for e in events:
            by_proj.setdefault(e["proj"], []).append(e)

        judged = {}
        if events and acquire_lock():
            try:
                for proj, evs in by_proj.items():
                    pd = os.path.join(pw.ROOT, proj)
                    files = J.scan_recent(pd)
                    # 取该项目最近一次的新增 memory 作为判断依据
                    mem = ""
                    try:
                        cur_files = scan_memory().get(proj, {})
                        prev = {k: v for k, v in (st.get(proj) or {}).items()
                                if not k.startswith("__")}
                        mem, _ = new_memory_text(cur_files, prev)
                    except Exception:
                        mem = ""
                    t = J.judge(proj, [{"text": e["done"], "full": e.get("full") or e["done"]}
                                       for e in evs], mem, files, proj_dir=pd)
                    judged[proj] = t
            finally:
                release_lock()

        for e in events:
            s = f"✅ 完成了：{e['proj']}\n　　{e['done']}"
            jt = judged.get(e["proj"])
            if jt:
                s += "\n\n" + jt
            elif not judged:
                pass
            if e.get("next"):
                s += f"\n\n⏳ 下一条（已就绪，复制给它）：\n　　{e['next']['text'][:80]}"
            elif not jt:
                s += "\n\n（这轮判断没跑起来 —— 可能上一轮还在跑。事实已报，判断下次补。）"
            out.append(s)

    if out:
        print("\n\n".join(out))


if __name__ == "__main__":
    main()
