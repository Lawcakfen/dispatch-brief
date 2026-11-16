#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目进度监管 · 证据采集（供 cron 的 agent 写「进度 + 建议」）

两种用法：
  python project_watch.py --evidence   ← cron 用这个。输出「证据包」给 agent 读
  python project_watch.py --init       初始化基线
  python project_watch.py              传统模式（只报变化，供手动调试）
  python project_watch.py --dry        只打印不写文件

设计（2026-11-13 v3）：
  脚本负责【客观事实】：谁动了、动了哪些文件、进度文件里写了什么、停了几天、历史记录
  agent 负责【判断】：这个项目现在到哪了、下一步该做什么
  → 分离的好处：事实不会被 AI 编，判断不会被 diff 限制
"""
import json
import os
import re
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths                      # ★ 所有路径与项目分类的唯一来源

ROOT = paths.PROJECTS_ROOT
LOG = os.path.join(ROOT, "_进度总表.md")
STATE = os.path.join(paths.CACHE_DIR, "project_watch_state.json")

# ★ 项目分类从 agent-config.json 读 —— 原来写死在这里，含客户名，无法发布
#   见 paths.py 顶部的说明
FOCUS = paths.FOCUS
CLIENTS = paths.CLIENTS
PAUSED_PROJECTS = paths.PAUSED_PROJECTS

PROGRESS_KEYS = ("版本记录", "进度", "readme", "工作记录", "进展", "todo", "待办", "计划")
# 忽略的目录名：构建产物 / 缓存 / 第三方依赖
# ★ 按需增删。如果你发现某个项目「文件总数」大得离谱，
#   大概率是它里面有个克隆下来的工程或构建目录 —— 把目录名加到这里。
IGNORE_DIRS = {"dist", "build", ".cache", "vendor", ".next", ".nuxt", "coverage"}
SKIP_DIRS = {".workbuddy", "node_modules", ".git", "Temp", "__pycache__"} | IGNORE_DIRS
# ★ 队列文件名（含兼容别名）：扫描时一律排除，避免我自己的写入污染「最近改动」
QUEUE_NAME = "_给WB.md"
QUEUE_ALIASES = {"_给WB.md", "_给执行Agent.md", "_给wb.md"}
# ★ 跳过备份/临时文件 —— 实测某项目一天生成 6597 个 _bak_before_*.html，
#   把「文件总数」和「今日新增」全部冲垮，还让停滞天数失真
SKIP_FILE_HINTS = ("_bak_", ".bak", ".tmp", ".temp", "~$", ".orig", ".swp",
                   ".pyc", ".pyo", ".lock")
MAX_FILES_PER_PROJ = 400


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


def read_tail(path, maxchars=420):
    """读 md 尾部（进度文件通常是追加写的），取最后 maxchars 字"""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            txt = f.read().strip()
        if not txt:
            return ""
        return txt[-maxchars:]
    except Exception:
        return ""


def condense(txt, maxchars=1800):
    """把 WB 报告压缩成「小节标题 + 每节首句」，丢掉表格正文/代码块/长列表。
    9725 字的报告能压到 1500 字左右，但所有「做了什么」都还在。"""
    out, in_code = [], False
    for raw in txt.split("\n"):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if s.startswith("|"):          # 表格行（logo 直链那种）——判断进度用不上
            continue
        if s.startswith("#"):          # 标题全留
            out.append(s)
            continue
        if s.startswith(("- ", "* ")):  # 列表项取前 46 字
            out.append("- " + s[2:][:46])
            continue
        out.append(s[:66])              # 普通段落取前 66 字
    s = "\n".join(out)
    if len(s) > maxchars:
        s = s[:maxchars] + "\n…（已压缩，原文更长）"
    return s


def wb_memory(proj_dir, maxchars=2400):
    """读 WorkBuddy 自己写的工作日志（项目内 .workbuddy/memory/<日期>.md）。
    ★ 这是「意图层」信息：为什么改、改到哪、等谁确认 —— 比文件 mtime 信息量大得多。
    ★ 读最近 **2 天**（不是只读最新一份），避免跨天遗漏。
    ★ 长报告会被 condense() 压缩成骨架，避免只读前几百字漏掉后面大半。"""
    mdir = os.path.join(proj_dir, ".workbuddy", "memory")
    if not os.path.isdir(mdir):
        return ""
    cands = []
    for f in os.listdir(mdir):
        if not f.endswith(".md") or f.upper() == "MEMORY.MD":
            continue
        if not re.match(r"\d{4}-\d{2}-\d{2}\.md$", f):
            continue
        cands.append(os.path.join(mdir, f))
    if not cands:
        return ""
    cands.sort(key=os.path.getmtime, reverse=True)
    picks = cands[:2]
    budget = maxchars // len(picks)
    parts = []
    for fp in picks:
        try:
            txt = open(fp, encoding="utf-8", errors="ignore").read().strip()
        except Exception:
            continue
        if not txt:
            continue
        body = condense(txt, budget)
        tag = os.path.basename(fp).replace(".md", "")
        parts.append(f"（文件 `{tag}.md`，原文 {len(txt)} 字 → 压缩 {len(body)} 字，保留全部小节标题）\n{body}")
    return "\n\n".join(parts)


def _skip_dir(name):
    """目录是否该跳过。

    ★ 真实数据踩到的：某项目的 clone_work 下有一整个 Edge 用户配置目录，
      Cache_Data/data_0 这类文件天天变，把「最近改动」全占了。
    """
    n = (name or "").lower()
    if name in SKIP_DIRS or n in SKIP_DIRS:
        return True
    if n.startswith("_edge_profile") or n.startswith("edge_profile"):
        return True
    if n in {"cache", "cache_data", "code cache", "gpucache", "crashpad",
             "blob_storage", "service worker", "local storage"}:
        return True
    return False


def scan():
    out = {}
    if not os.path.isdir(ROOT):
        return out
    for d in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p) or d.startswith("_"):
            continue
        files, prog = {}, {}
        for dp, dirs, fs in os.walk(p):
            dirs[:] = [x for x in dirs if not _skip_dir(x)]
            for f in fs:
                # ★★★ 跳过我自己写的元数据文件 —— 否则「停滞天数」会被我自己的写入拉新
                #     实测症状：项目 28 天没动，但我一写 _给WB.md，它就变成「停滞 0 天」
                if f in QUEUE_ALIASES:
                    continue
                # ★ 跳过备份/临时文件（见 SKIP_FILE_HINTS 注释）
                if any(h in f.lower() for h in SKIP_FILE_HINTS):
                    continue
                fp = os.path.join(dp, f)
                try:
                    m = os.path.getmtime(fp)
                except OSError:
                    continue
                files[os.path.relpath(fp, p)] = m
                low = f.lower()
                if f.lower().endswith(".md") and any(k in low for k in PROGRESS_KEYS):
                    prog[os.path.relpath(fp, p)] = read_tail(fp)
        if not files:
            continue
        out[d] = {"mtime": max(files.values()), "nfiles": len(files),
                  "files": files, "progress": prog}
    return out


def is_focus(name):
    return any(k in name for k in FOCUS)


def is_client(name):
    return any(k in name for k in CLIENTS)


def paused_reason(name):
    """已收尾项目的状态说明；不是收尾项目返回 None。"""
    for k, v in PAUSED_PROJECTS.items():
        if k in name:
            return v
    return None


def recent_log(name, n=2):
    """从 _进度总表.md 里取该项目最近 n 条记录（让 agent 知道上次说了什么）"""
    if not os.path.exists(LOG):
        return []
    try:
        txt = open(LOG, encoding="utf-8", errors="ignore").read()
    except Exception:
        return []
    hits = []
    for block in txt.split("\n## "):
        if name in block.split("\n")[0]:
            lines = [l.strip() for l in block.split("\n")[1:] if l.strip().startswith(("-", "  -"))]
            hits = lines[-n:]
            break
    return hits


def evidence(only=None):
    """输出证据包（markdown，给 agent 读）

    only: 只输出名字匹配的那一个项目（不管它在不在 FOCUS 里）。
          —— 用于「按项目刷新」：证据从 1 万汉字降到 2–3 千，成本从 ¥0.05 降到 ¥0.01。
    """
    today = datetime.date.today()
    cur = scan()
    st = load_state()
    focus_rows, rest, paused_rows = [], [], []

    for name, info in sorted(cur.items(), key=lambda x: -x[1]["mtime"]):
        mt = datetime.datetime.fromtimestamp(info["mtime"])
        days = (today - mt.date()).days
        prev = st.get(name, {})
        prev_files = prev.get("files", {}) or {}

        new_f = [f for f in info["files"] if f not in prev_files]
        chg_f = [f for f in info["files"] if f in prev_files and info["files"][f] > prev_files[f] + 1]
        gone_f = [f for f in prev_files if f not in info["files"]]

        # 最近修改的 5 个文件（用来判断"在做什么"）
        recent = sorted(info["files"].items(), key=lambda x: -x[1])[:5]
        recent_s = "、".join(
            f"{os.path.basename(f)}（{datetime.datetime.fromtimestamp(t).strftime('%m-%d %H:%M')}）"
            for f, t in recent)

        # ★★★ 已收尾项目：不进停滞统计、不写「现在/建议」，单独一节
        _pz = paused_reason(name)
        if _pz and not only:
            paused_rows.append(f"- {name}　{_pz}")
            continue

        if only:
            # ★ 指定了项目：只要能匹配上的一律详细输出，不看 FOCUS
            if not (only in name or name in only):
                continue
        elif not is_focus(name):
            rest.append((name, days, len(new_f) + len(chg_f)))
            continue

        tag = []
        if is_client(name):
            tag.append("客户项目")
        if days == 0:
            tag.append("今天有动")
        blocks = [f"\n### {name}　{'｜'.join(tag) if tag else ''}"]
        blocks.append(f"- 最后修改：{mt.strftime('%Y-%m-%d %H:%M')}　→ 停滞 {days} 天")
        blocks.append(f"- 文件总数：{info['nfiles']}")

        if new_f:
            blocks.append(f"- 今日新增 {len(new_f)} 个：{'、'.join(os.path.basename(x) for x in sorted(new_f)[:6])}")
        if chg_f:
            blocks.append(f"- 今日修改 {len(chg_f)} 个：{'、'.join(os.path.basename(x) for x in sorted(chg_f)[:6])}")
        if gone_f:
            blocks.append(f"- 今日删除 {len(gone_f)} 个")
        if not (new_f or chg_f or gone_f):
            if days > 0:
                blocks.append("- 今日无文件变动")
            else:
                blocks.append("- 今日有改动（基线为今日新建，无文件级差异可比）")

        blocks.append(f"- 最近改动的文件：{recent_s}")

        for rel, tail in list((info.get("progress") or {}).items())[:2]:
            t = re.sub(r"\s+", " ", tail)[-360:]
            blocks.append(f"- 进度文件 `{os.path.basename(rel)}` 尾部，原文照录：\n  > {t}")

        # ★★ 最强证据：WorkBuddy 自己写的工作报告
        wbm = wb_memory(os.path.join(ROOT, name))
        if wbm:
            indented = "\n".join("  " + l for l in wbm.split("\n"))
            blocks.append("- ★★★ **WorkBuddy 的工作报告（它自己写的，最权威）**：\n" + indented)
        else:
            blocks.append("- ⚠️ 该项目没有 WorkBuddy 工作日志（.workbuddy/memory 无日期文件）——"
                          "「现在到哪了」只能靠文件变动推断，判断要保守")

        hist = recent_log(name)
        if hist:
            blocks.append("- 总表里该项目最近记录（= 上次汇报过的）：\n" +
                          "\n".join(f"  {h}" for h in hist))
        focus_rows.append("\n".join(blocks))

    # 概览
    moving = [n for n, d, c in rest if d <= 7]
    hung = [n for n, d, c in rest if 8 <= d <= 20]
    dead = [n for n, d, c in rest if d >= 21]

    if only:
        # ★ 单项模式：只给这一个项目，不要概览（省 token、防干扰）
        head = [f"# 项目证据包 · {today.isoformat()}　［单项：{only}］", "",
                "> 以下是脚本扫出的客观事实。**不许在此基础上编造任何内容。**",
                "> 本次只判断这一个项目，其余项目不在范围内。", ""]
        if not focus_rows:
            head.append(f"⚠️ 找不到匹配「{only}」的项目目录。现有目录：")
            head.append("　" + "、".join(sorted(os.listdir(ROOT)) if os.path.isdir(ROOT) else []))
            return "\n".join(head)
        return "\n".join(head + focus_rows)

    out = [f"# 项目证据包 · {today.isoformat()}", "",
           "> 以下是脚本扫出的客观事实。**不许在此基础上编造任何内容。**",
           "> 需要写判断的项目在「一」，其余只是概览（除非某条特别值得一提）。", "",
           "## 一、需要详细汇报的项目", "".join(focus_rows) if focus_rows else "（无）", "",
           "## 二、其余项目概览", ""]
    if moving:
        out.append(f"- 7 天内在动：{'、'.join(moving)}")
    if hung:
        out.append(f"- 8–20 天悬着：{'、'.join(hung)}")
    if dead:
        out.append(f"- 21 天以上已停（{len(dead)} 个）：{'、'.join(dead)}")

    # ★★★ 已收尾（做完了）。放在最后，且明确告诉判断层：不要写建议。
    if paused_rows:
        out += ["", "## 三、已收尾（做完了，不参与停滞统计）",
                "",
                "> ★ 这些项目已上线/交付，**不要写「现在到哪 · 建议」**，",
                ">   也不要在汇总消息里报它们。它们下次出现是版本迭代或新需求。",
                ""]
        out += paused_rows
    return "\n".join(out)


def set_one(name, now_txt, advice_txt):
    """★ 只更新「最新汇报段」里某一个项目的那一行，其余行原样不动。

    用于「按项目刷新」：WB 做完一步 → 只重算这一个项目 → 写回这一行 → 刷看板。
    不做的话就只能等第二天 06:20 的全量判断。
    """
    if not os.path.exists(LOG):
        return f"ERROR: 找不到日志 {LOG}"
    now_txt = (now_txt or "").strip().replace("\n", " ")
    advice_txt = (advice_txt or "").strip().replace("\n", " ")
    if not now_txt or not advice_txt:
        return "ERROR: --now 和 --advice 都不能为空"

    md = open(LOG, encoding="utf-8").read()
    lines = md.split("\n")

    # 找最后一个「## … 汇报」段（注意「11-14 汇报」中间有空格，不能用 \S*）
    idx = -1
    for i, l in enumerate(lines):
        if re.fullmatch(r"##\s+.*汇报\s*", l):
            idx = i
    if idx < 0:
        return "ERROR: 日志里没有「## … 汇报」段，请先让每日 cron 跑一次"

    # 段尾 = 下一个 ## 或文件末
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break

    # 段内找该项目那一行（「- 名字：现在 … ｜ 建议 …」）
    hit = None
    for j in range(idx + 1, end):
        l = lines[j]
        if not l.startswith("- ") or "：" not in l:
            continue
        pname = l[2:].split("：", 1)[0].strip()
        pname = re.sub(r"@\S+$", "", pname)
        if pname == name or name in pname or pname in name:
            hit = j
            break

    new_line = f"- {name}：现在 {now_txt} ｜ 建议 {advice_txt}"
    if hit is None:
        lines.insert(end, new_line)
        action = "新增"
    else:
        lines[hit] = new_line
        action = "替换"

    open(LOG, "w", encoding="utf-8").write("\n".join(lines))
    return f"{action}：{name}"


def collect_todos():
    """从各项目的 _给WB.md 里抽出「⏳ 待发」条目，返回 markdown 段。
    格式：- <项目目录名> ｜ <动作第一句>
    """
    rows = []
    if not os.path.isdir(ROOT):
        return ""
    for d in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p) or d.startswith("_"):
            continue
        f = os.path.join(p, "_给WB.md")
        if not os.path.isfile(f):
            continue
        try:
            txt = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        # 定位「## ⏳ 待发」到下一个 ## 之间
        m = re.search(r"##\s*⏳\s*待发([\s\S]*?)(?=\n##\s|\Z)", txt)
        if not m:
            continue
        # 把「- [ ] **日期** 动作」+ 其后的缩进续行，合并成一条完整指令
        items, cur = [], None
        for line in m.group(1).split("\n"):
            st = line.strip()
            if st.startswith("- [ ]"):
                if cur:
                    items.append(cur)
                cur = st[5:].strip()
            elif cur and st and not st.startswith("#"):
                cur += " " + st          # 续行（为什么 / 做完请）拼进同一条
        if cur:
            items.append(cur)

        for it in items:
            age_txt = ""
            dm = re.match(r"\*\*(\d{2})-(\d{2})\*\*", it)
            if dm:
                try:
                    y = datetime.date.today().year
                    d0 = datetime.date(y, int(dm.group(1)), int(dm.group(2)))
                    if d0 > datetime.date.today():
                        d0 = d0.replace(year=y - 1)
                    n = (datetime.date.today() - d0).days
                    if n >= 2:
                        age_txt = f"（挂 {n} 天）"
                except ValueError:
                    pass
            full = re.sub(r"^\*\*[\d\-]+\*\* \s*", "", it).replace("**", "")
            full = re.sub(r"\s+", " ", full).strip()
            short = re.split(r"[。；]", full)[0].strip()
            if len(short) > 56:
                short = short[:56] + "…"
            # ★ 三段格式：项目 § 缩略 § 完整（完整用于一键复制）
            rows.append(f"- {d} § {short}{age_txt} § {full}")
    return ("## 给WB 待发\n" + "\n".join(rows) + "\n") if rows else ""


def sync_todos():
    """★ 把总表里「## 入队（给WB）」段的内容，追加进各项目 _给WB.md 的「⏳ 待发」。
    agent 只负责产出这个结构化段落，脚本负责写入 —— 格式不会乱。"""
    if not os.path.exists(LOG):
        return "找不到 _进度总表.md"
    md = open(LOG, encoding="utf-8").read()
    m = re.search(r"##\s*入队（给WB）([\s\S]*?)(?=\n##\s|\Z)", md)
    if not m:
        return "总表里没有「## 入队（给WB）」段，跳过"
    entries = []
    for line in m.group(1).split("\n"):
        s = line.strip()
        if not s.startswith("- "):
            continue
        body = s[2:].strip()
        if "｜" not in body:
            continue
        proj, todo = body.split("｜", 1)
        entries.append((proj.strip(), todo.strip()))
    if not entries:
        return "入队段为空，跳过"

    dirs = [d for d in os.listdir(ROOT)
            if os.path.isdir(os.path.join(ROOT, d)) and not d.startswith("_")]
    today_md = datetime.date.today().strftime("%m-%d")
    added, skipped, miss = 0, 0, []
    for proj, todo in entries:
        target = None
        for d in dirs:
            if d == proj or d.startswith(proj) or proj in d:
                target = d
                break
        if not target:
            miss.append(proj)
            continue
        f = os.path.join(ROOT, target, "_给WB.md")
        key = todo[:18]
        if os.path.exists(f):
            txt = open(f, encoding="utf-8").read()
            if key in txt:
                skipped += 1
                continue
            m2 = re.search(r"(##\s*⏳\s*待发[^\n]*\n)([\s\S]*?)(?=\n##\s|\Z)", txt)
            if not m2:
                continue
            ins = f"- [ ] **{today_md}** {todo}\n"
            txt = txt[:m2.end(2)] + ins + txt[m2.end(2):]
        else:
            txt = (f"# 给 WB 的指令 · {target}\n\n> 由 Hermes 维护，自动追加。"
                   f"复制「⏳ 待发」里的内容给这个项目的 WB 窗口。\n"
                   f"> 最后更新：{datetime.date.today().isoformat()}\n\n"
                   f"## ⏳ 待发\n\n- [ ] **{today_md}** {todo}\n\n"
                   f"## ✅ 已完成（从 WB 报告确认）\n\n（无）\n\n"
                   f"## ⚠️ 卡在外部\n\n（无）\n")
        open(f, "w", encoding="utf-8").write(txt)
        added += 1
    msg = f"入队：新增 {added} 条，已存在跳过 {skipped} 条"
    if miss:
        msg += f"；⚠️ 找不到目录：{'、'.join(miss)}"
    return msg


def heartbeat(note=""):
    """★ 心跳：每次跑完写一次。由 wb_watch.py 检查，
    超过 26 小时没更新就报警 —— 这就是「cron 的 cron」。"""
    p = os.path.join(paths.CACHE_DIR, "project_watch_heartbeat.txt")
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " " + note + "\n")
    except Exception:
        pass


def _arg(flag, default=""):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else default
    return default


def main():
    # ★ 按项目刷新（第一步）：只输出一个项目的证据包，不碰其他项目
    if "--only" in sys.argv:
        name = _arg("--only").strip()
        if not name:
            print('用法：python project_watch.py --only "<项目名>"')
            return
        print(evidence(only=name))
        heartbeat("only")
        return

    # ★ 按项目刷新（第二步）：只改最新汇报段里那一行，然后顺手刷看板
    if "--set" in sys.argv:
        name, now_t, adv = _arg("--set").strip(), _arg("--now"), _arg("--advice")
        if not name:
            print('用法：python project_watch.py --set "项目名" --now "…" --advice "…"')
            return
        r = set_one(name, now_t, adv)
        print(r)
        if r.startswith("ERROR"):
            return
        import subprocess
        _b = subprocess.run([sys.executable, os.path.abspath(__file__), "--board"],
                            capture_output=True, text=True, encoding="utf-8")
        _bo = (_b.stdout or "").strip()
        # ★ 看板刷新失败不能冒充 --set 的结果，单独标出来
        if _b.returncode != 0 or _bo.startswith("ERROR"):
            print("⚠️ 已改日志，但看板没刷新：" + (_bo or (_b.stderr or "").strip()[:200]))
        else:
            print(_bo)
        heartbeat(f"set:{name}")
        return

    # ★ 入队同步：把总表「## 入队（给WB）」写进各项目的 _给WB.md
    if "--sync" in sys.argv:
        print(sync_todos())
        heartbeat("sync")
        return

    # ★ 看板：把 _进度总表.md 注入 _template.html 的 raw-md 块，生成单文件 index.html
    if "--board" in sys.argv:
        import re as _re
        if not os.path.exists(LOG):
            print("ERROR: 找不到 _进度总表.md")
            return
        md = open(LOG, encoding="utf-8").read()
        # 去掉旧的「给WB 待发」段，避免重复累积
        md = _re.sub(r"\n##\s*给WB 待发[\s\S]*?(?=\n##\s|\Z)", "", md).rstrip("\n") + "\n"
        todos = collect_todos()
        if todos:
            md = md.rstrip("\n") + "\n\n" + todos
        md = md.replace("</script>", "&lt;/script&gt;")
        out_dir = os.path.join(ROOT, "_看板")
        tpl = os.path.join(out_dir, "_template.html")
        if not os.path.exists(tpl):
            print("ERROR: 找不到 _看板/_template.html（WB 原始皮肤，别删）")
            return
        html = open(tpl, encoding="utf-8").read()
        pattern = r'(<script type="text/plain" id="raw-md">)[\s\S]*?(</script>)'
        new, n = _re.subn(pattern, lambda m: m.group(1) + md + m.group(2), html, count=1)
        if n != 1:
            print("ERROR: 模板里找不到 raw-md 块，未生成")
            return
        # ★ 数据更新时间徽章：按 _进度总表.md 的 mtime 算，超过 26 小时标红
        mt = datetime.datetime.fromtimestamp(os.path.getmtime(LOG))
        age_h = (datetime.datetime.now() - mt).total_seconds() / 3600
        if age_h < 1:
            ago = "刚刚"
        elif age_h < 24:
            ago = f"{int(age_h)} 小时前"
        else:
            ago = f"{int(age_h / 24)} 天前"
        stale = age_h > 26
        badge = f"数据更新于 {mt.strftime('%m-%d %H:%M')}（{ago}）"
        if stale:
            badge += "· 已过期，检查 06:20 那个 cron"
        cls = "upd stale" if stale else "upd"
        new = _re.sub(r'<div class="upd" id="upd">.*?</div>',
                      f'<div class="{cls}" id="upd">{badge}</div>', new, count=1)

        out = os.path.join(out_dir, "index.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(new)
        print(f"✅ 看板已更新：{out}")
        print(f"   （源 MD {len(md)} 字符 → 页面 {len(new)} 字符）")
        return

    # ★ 默认就是证据包模式（cron 用 script 字段跑，没法传参数）
    if "--diff" in sys.argv or "--init" in sys.argv or "--dry" in sys.argv:
        pass
    else:
        print(evidence())
        heartbeat("evidence")
        return
    if "--evidence" in sys.argv:
        print(evidence())
        return

    # ---- 传统模式：只报变化（保留供调试）----
    init = "--init" in sys.argv
    dry = "--dry" in sys.argv
    today = datetime.date.today()
    today_s = today.strftime("%m-%d")
    cur = scan()
    st = load_state()
    if not st and not init:
        init = True

    for name, info in sorted(cur.items(), key=lambda x: -x[1]["mtime"]):
        prev_files = (st.get(name, {}) or {}).get("files", {}) or {}
        new_f = [f for f in info["files"] if f not in prev_files]
        chg_f = [f for f in info["files"] if f in prev_files and info["files"][f] > prev_files[f] + 1]
        st.setdefault(name, {})
        st[name].update({"mtime": info["mtime"], "files": info["files"],
                         "progress": info.get("progress") or {},
                         "nfiles": info["nfiles"]})
        st[name].setdefault("change_date", today.isoformat())
        if new_f or chg_f:
            st[name]["change_date"] = today.isoformat()
            if not init:
                print(f"{name}：新增 {len(new_f)} 改 {len(chg_f)}")
    if not dry:
        save_state(st)
    if init and not dry:
        print("（基线已建）")


if __name__ == "__main__":
    main()
