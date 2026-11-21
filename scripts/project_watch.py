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
# ★ 每个项目的「判断写于什么时候」（2026-11-20 加）
#   总表里只存判断文字，不存写入时间。没有这个，看板就分不清
#   「这条判断是刚刚写的」还是「三天前写的」。
JUDGE_TIMES = os.path.join(ROOT, "_看板", "judge_times.json")

# ★★★ 项目状态字段（2026-11-21 · 设计 2）
#   存在一个 json 侧车里，不进 _进度总表.md —— 总表是【日志】(只追加)，
#   这里是【当前快照】(每次覆盖)。两者用途不同，合在一起会互相破坏。
#   字段固定：stage / next / blocker / criterion / note
PSTATE = os.path.join(ROOT, "_看板", "pstate.json")
STAGES = ("未开始", "开发中", "待验收", "已完成", "暂停")


def load_pstate():
    try:
        if os.path.exists(PSTATE):
            with open(PSTATE, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def save_pstate(d):
    try:
        os.makedirs(os.path.dirname(PSTATE), exist_ok=True)
        tmp = PSTATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PSTATE)
        return True
    except Exception:
        return False


def set_pstate(name, fields):
    """写一个项目的状态字段。★ stage 必须在枚举里，否则整个不写（不猜）。"""
    stage = (fields.get("stage") or "").strip()
    if stage not in STAGES:
        return False, "stage 不在允许取值内：%r（允许：%s）" % (stage, " / ".join(STAGES))
    d = load_pstate()
    rec = {
        "stage": stage,
        "next": (fields.get("next") or "无").strip()[:90],
        "blocker": (fields.get("blocker") or "无").strip()[:60],
        # ★ 160 不是 80：实测 80 会把判据断在半句（「…可点击、不出现」后面没了），
        #   断句的判据比没有更糟 —— 看着像结论，其实是残句。
        "criterion": (fields.get("criterion") or "无").strip()[:160],
        "note": (fields.get("note") or "").strip()[:120],
        "at": datetime.datetime.now().strftime("%m-%d %H:%M"),
    }
    d[name] = rec
    save_pstate(d)
    return True, rec


def _record_judge_time(name):
    """记下这个名字的判断写入时间（set_one 调用，所以 cron 和刷新按钮都会自动记）"""
    try:
        d = {}
        if os.path.exists(JUDGE_TIMES):
            with open(JUDGE_TIMES, encoding="utf-8") as f:
                d = json.load(f)
        d[name] = datetime.datetime.now().strftime("%m-%d %H:%M")
        os.makedirs(os.path.dirname(JUDGE_TIMES), exist_ok=True)
        with open(JUDGE_TIMES, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass

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
SKIP_DIRS = {".agent", ".workbuddy", ".codex", ".claude",
             "node_modules", ".git", "Temp", "__pycache__"} | IGNORE_DIRS
# ★ 预览/截图缓存目录（2026-11-20 加）
#   实测：内容看板@WorkBuddy 下有个 .reviewpro，里面有 327 个缓存文件天天变，
#   把「最近变动」全占了，看板上显示「共 327 个」，掩盖了真正改动的源文件。
SKIP_DIR_PREFIXES = (".reviewpro", "reviewpro", "_reviewpro")
# ★ 队列文件名（含兼容别名）：扫描时一律排除，避免我自己的写入污染「最近改动」
QUEUE_NAME = "_给WB.md"
# ★ 队列文件名从 paths.py 取（唯一来源）；这里保留 set 形式给成员判断用
QUEUE_ALIASES = set(paths.QUEUE_ALIASES)
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
    """把 WB 报告压成骨架。

    ★★ 2026-11-20 修：原来是从头硬截 `s[:maxchars]`，长报告后面的小节会整段消失。
       实际踩到的：11-20 的 memory 里 B3/B4/B5 都有交付回报，但压缩后只剩到 B2 ——
       于是判断写出「B3 尚未接入 render」，而事实是 B3/B4/B5 前一天凌晨就交付了。
       进度全在**小节标题**里，标题丢了判断就错，所以这里改成：
       ① 按 ## 分节 ② 优先保全部标题 ③ 剩余预算按 3行/2行/1行/0行 逐档降级
       ④ 实在连标题都放不下才硬截（并明说被截了）
    """
    sections = []            # [(标题, [正文行...])]
    cur_title, cur_lines = None, []
    in_code = False

    def flush():
        if cur_title is not None:
            sections.append((cur_title, cur_lines[:]))

    for raw in txt.split("\n"):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if s.startswith("|"):          # 表格行，判断进度用不上
            continue
        if s.startswith("## "):
            flush()
            cur_title, cur_lines = s, []
            continue
        if s.startswith("#") and cur_title is None:
            cur_title, cur_lines = s, []
            continue
        if cur_title is None:
            cur_title = "（开头）"
        if s.startswith(("- ", "* ")):
            cur_lines.append("- " + s[2:][:46])
        else:
            cur_lines.append(s[:66])
    flush()

    if not sections:
        return txt[:maxchars]

    # 逐档降级：先试「标题 + 每节 3 行」，不行再 2 / 1 / 0 行
    for keep in (3, 2, 1, 0):
        out = []
        for t, ls in sections:
            out.append(t)
            if keep:
                out.extend(ls[:keep])
        body = "\n".join(out)
        if len(body) <= maxchars:
            return body

    # 连标题都放不下 —— 硬截，但明确标注，别让人以为读全了
    body = "\n".join(t for t, _ in sections)
    if len(body) > maxchars:
        body = body[:maxchars] + "\n…（★ 标题超预算已截断，后面还有 %d 节没读到）" % len(sections)
    return body


def wb_memory(proj_dir, maxchars=9000):
    """读执行方自己写的工作日志（默认 <项目>/.agent/memory/<日期>.md，兜底旧路径）。
    ★ 这是「意图层」信息：为什么改、改到哪、等谁确认 —— 比文件 mtime 信息量大得多。
    ★ 读最近 **2 天**（不是只读最新一份），避免跨天遗漏。
    ★ 长报告会被 condense() 压缩成骨架 —— 现在保证【所有小节标题】都在，
      预算从 2400 提到 9000（每份 4500），因为进度全在标题里，标题丢了判断就会错。"""
    mdir = paths.memory_dir(proj_dir)
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


def queue_pending(proj_dir, max_items=4):
    """读该项目 _给WB.md 的「⏳ 待发」，返回缩略清单。

    ★ 2026-11-20 加：判断一直看不到队列，所以它只会说「项目状态如何」，
      永远不会说「你还有 N 条活没干」。实际踩到的：内容看板队列挂着两条
      （未归位角度 / README 重写），但「建议」给的是「跑一次 selfcheck」——
      因为 judge 根本不知道队列存在。

    只提供事实，不要求派单 —— 派单和判断仍然是两件事。
    自己用正则解析，不 import queue.py（会跟标准库 queue 撞名）。
    """
    qf = os.path.join(proj_dir, "_给WB.md")
    if not os.path.isfile(qf):
        return None
    try:
        txt = open(qf, encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    m = re.search(r"##\s*⏳\s*待发([\s\S]*?)(?=\n##\s|\Z)", txt)
    if not m:
        return "0 条（没有「⏳ 待发」段）"
    items = []
    for ln in m.group(1).split("\n"):
        st = ln.strip()
        if not st.startswith("- [ ]"):
            continue
        t = st[5:].strip()
        t = re.sub(r"^\*\*[\d\-]+\*\*\s*", "", t).replace("**", "")
        t = re.sub(r"\s+", " ", t).strip()
        items.append(t[:54])
    if not items:
        return "0 条（已全部打勾完毕 —— ★ 不要再建议「去打勾收尾」，已经打过了）"
    more = "" if len(items) <= max_items else "（还有 %d 条）" % (len(items) - max_items)
    return "%d 条：%s%s" % (len(items), " ／ ".join(items[:max_items]), more)


def _skip_dir(name):
    """目录是否该跳过。

    ★ 真实数据踩到的：某项目的 clone_work 下有一整个 Edge 用户配置目录，
      Cache_Data/data_0 这类文件天天变，把「最近改动」全占了。
    """
    n = (name or "").lower()
    if name in SKIP_DIRS or n in SKIP_DIRS:
        return True
    if any(n.startswith(p) for p in SKIP_DIR_PREFIXES):
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
        # ★ 2026-11-21 加：防止把「派单文件」误当成「执行成果」
        #   实际踩到的：空本子项目里 Hermes 刚写完 `WB指令-空本子v1.1三件.md`，
        #   判断就写成「已产出 WB指令文件」，看起来像有进展 —— 其实一个字活都没干。
        if any("WB指令" in os.path.basename(f) for f in list(info.get("files") or {})[:40]):
            blocks.append("- ★ **注意**：上面出现的 `WB指令-*.md` 是**派单文件**（由泽哥或 Hermes 写的执行说明书），"
                          "**不是 WorkBuddy 的产出**。看到它只能说明「活派出去了」，不能说明「活干完了」。"
                          "判断进度只能看 WorkBuddy 的 memory 和实际产物文件。")

        for rel, tail in list((info.get("progress") or {}).items())[:2]:
            t = re.sub(r"\s+", " ", tail)[-360:]
            blocks.append(f"- 进度文件 `{os.path.basename(rel)}` 尾部，原文照录：\n  > {t}")

        # ★★ 最强证据：WorkBuddy 自己写的工作报告
        wbm = wb_memory(os.path.join(ROOT, name))
        if wbm:
            indented = "\n".join("  " + l for l in wbm.split("\n"))
            blocks.append("- ★★★ **执行方的工作报告（它自己写的，最权威）**：\n" + indented)
        else:
            blocks.append("- ⚠️ 该项目没有执行方工作日志（记忆目录里没有日期文件）——"
                          "「现在到哪了」只能靠文件变动推断，判断要保守")

        # ★★ 队列待发（2026-11-20 加）：让判断知道「还有哪些活没干」
        qp = queue_pending(os.path.join(ROOT, name))
        if qp is not None:
            blocks.append(f"- **队列待发（`_给WB.md` 的「⏳ 待发」）**：{qp}")
            blocks.append(
                "  > ★ 写「建议」时的三条规则：\n"
                "  > ① **先对照下面 memory 的最后几个小节**：如果某条待发已经在 memory 里"
                "写了「已完成/已交付/已重写」，说明它**做完了只是没打勾** —— 建议要写"
                "「先确认打勾，不要重做」，绝对不能建议重做一遍。\n"
                "  > ② 队列里确实还没做的 → 建议指向第一条没做的那条，说清是哪一条。\n"
                "  > ③ 队列全空 → 才建议「验收/确认/收尾」类动作。\n"
                "  > ★ 队列和判断是两套数据，队列不会自动打勾，所以「挂着」不等于「没做」。")
        else:
            blocks.append("- 队列待发：该项目没有 `_给WB.md`（无派单队列）")

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
    _record_judge_time(name)
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


def compute_anomalies(scan_result, judge_times, now=None):
    """算出「能纯脚本判定」的三种异常，返回 [(优先级, 项目名, 说明)]。

    ★ 2026-11-21 从 --board 的渲染代码里抽出来 —— 埋在 main() 里就没法单独测，
      而「无异常」和「停滞」这两个分支最容易写错：
      条件写反了不报错，只是永远不响，或者永远响。

    三种：
      1 停滞 >14 天，但队列还挂着活   ← 有活、没人做
      2 队列挂了 >72 小时没动          ← 派下去了，没回音
      3 判断 >26 小时没更新            ← 话是旧的

    ★ 共同前提：只有【队列里还有待发】的项目才判定。
      没有待发的项目不算异常 —— 那是「没在办」，不是「出问题」。
      进异常区只会变成噪音，而噪音会让人不再看异常区。
    """
    now = now or datetime.datetime.now()
    out = []
    for name, info in (scan_result or {}).items():
        qf = os.path.join(ROOT, name, "_给WB.md")
        pend, q_age_h = 0, None
        if os.path.isfile(qf):
            try:
                with open(qf, encoding="utf-8", errors="ignore") as f:
                    qt = f.read()
                m = re.search(r"^##\s*⏳?\s*待发(.*?)(?=^##\s|\Z)", qt, re.S | re.M)
                if m:
                    pend = len(re.findall(r"^- \[ \]", m.group(1), re.M))
                q_age_h = (now.timestamp() - os.path.getmtime(qf)) / 3600.0
            except Exception:
                pass
        if not pend:
            continue

        idle_d = (now.timestamp() - info["mtime"]) / 86400.0
        if idle_d > 14:
            out.append((1, name, "停滞 %d 天，队列还挂着 %d 条" % (int(idle_d), pend)))
        elif q_age_h is not None and q_age_h > 72:
            out.append((2, name, "队列挂 %d 条，%d 天没动" % (pend, int(q_age_h // 24))))

        js = (judge_times or {}).get(name, "")
        if js:
            try:
                dt = datetime.datetime.strptime(js, "%m-%d %H:%M").replace(year=now.year)
                if dt > now:
                    dt = dt.replace(year=now.year - 1)
                age_h = (now - dt).total_seconds() / 3600.0
                if age_h > 26:
                    out.append((3, name, "判断 %d 小时没更新" % int(age_h)))
            except Exception:
                pass

    out.sort(key=lambda x: (x[0], x[1]))
    return out


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
    if "--pstate" in sys.argv:
        # ★ 2026-11-21 新增：写项目状态字段（judge_lite 调这个，保证只有一个写入者）
        _n = _arg("--pstate").strip()
        if not _n:
            print("用法：python project_watch.py --pstate \"项目名\" --stage 开发中 "
                  "--next \"…\" --blocker \"…\" --criterion \"…\"")
            return
        _ok, _r = set_pstate(_n, {
            "stage": _arg("--stage"),
            "next": _arg("--next"),
            "blocker": _arg("--blocker"),
            "criterion": _arg("--criterion"),
            "note": _arg("--note"),
        })
        if not _ok:
            print("ERROR " + str(_r))
            return
        print("pstate ← %s ｜ %s" % (_n, _r.get("stage")))
        return

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
        # ★ 数据更新时间徽章（2026-11-20 改）
        #   两个时间分开：页面什么时候生成的 / 判断什么时候写的。
        #   因为刷新按钮只重新渲染，不会重新判断 —— 混成一个数会让人以为判断也更新了。
        # ★ 判断写于 = judge_times.json 里最新的一条
        #   不能用 _进度总表.md 的 mtime —— 那个文件只要有人手工改一行
        #   （比如把「停滞7天」改成「已结束」），时间就跳到当前，徽章会说谎。
        mt = None
        try:
          if os.path.exists(JUDGE_TIMES):
              with open(JUDGE_TIMES, encoding="utf-8") as _f:
                  _jt_all = json.load(_f)
              if _jt_all:
                  # ★ 历史数据里格式混过（有的带年有的不带），先统一再去最大值
                  _norm = {}
                  for _k, _v in _jt_all.items():
                      _norm[_k] = _re.sub(r"^\d{4}-", "", str(_v).strip())
                  _latest = max(_norm.values())                 # "MM-DD HH:MM"
                  mt = datetime.datetime.strptime(
                      str(datetime.datetime.now().year) + "-" + _latest, "%Y-%m-%d %H:%M")
        except Exception:
          mt = None
        if mt is None:
          mt = datetime.datetime.fromtimestamp(os.path.getmtime(LOG))
        gen = datetime.datetime.now()                                  # 页面生成
        age_h = (gen - mt).total_seconds() / 3600
        if age_h < 1:
            ago = "刚刚"
        elif age_h < 24:
            ago = f"{int(age_h)} 小时前"
        else:
            ago = f"{int(age_h / 24)} 天前"
        badge = (f"页面生成 {gen.strftime('%m-%d %H:%M')}"
                 f" ｜ 判断写于 {mt.strftime('%m-%d %H:%M')}（{ago}）")
        # 12 小时起变黄提醒（判断半天没更新），26 小时起变红（错过了一次 06:20）
        if age_h > 26:
            badge += " ｜ ⚠ 判断已过期，明早 06:20 才会更新"
            cls = "upd stale"
        elif age_h > 12:
            badge += " ｜ 判断已是半天前"
            cls = "upd warn"
        else:
            cls = "upd"
        new = _re.sub(r'<div class="upd[^"]*" id="upd">.*?</div>',
                      f'<div class="{cls}" id="upd">{badge}</div>', new, count=1)

        # ★ 实时变动块（2026-11-20）
        #   为什么加这块：刷新按钮只是重新渲染 _进度总表.md，内容不会变
        #   （判断要 LLM 才能写）。加一块「当场扫的最近 24 小时变动」，
        #   刷新后至少能看到哪些项目的文件刚动过。只进 index.html，不写总表。
        try:
            import html as _html
            _now = datetime.datetime.now()
            _lim = _now.timestamp() - 86400
            _cur = scan()
            _rows = []
            for _n, _i in _cur.items():
                _rec = [(f, m) for f, m in _i["files"].items() if m >= _lim]
                if _rec:
                    _rec.sort(key=lambda x: -x[1])
                    _rows.append((_n, _rec))
            if _rows:
                _rows.sort(key=lambda x: (-len(x[1]), x[0]))
                _lis = []
                for _n, _rec in _rows:
                    _top = _rec[:4]
                    _parts = "、".join(
                        "%s %s" % (_html.escape(f),
                                   datetime.datetime.fromtimestamp(m).strftime("%m-%d %H:%M"))
                        for f, m in _top)
                    _more = "（共 %d 个）" % len(_rec) if len(_rec) > 4 else ""
                    _lis.append('<li><b>%s</b>　%s%s</li>' % (_html.escape(_n), _parts, _more))
                _live = ('<div class="live"><b>★ 实时变动</b>'
                         '<span class="live-t">刚扫的 %s ｜ 最近 24 小时有变动的项目 %d 个</span>'
                         '<ul>%s</ul></div>'
                         % (_now.strftime("%m-%d %H:%M:%S"), len(_rows), "".join(_lis)))
            else:
                _live = ('<div class="live"><b>★ 实时变动</b>'
                         '<span class="live-t">刚扫的 %s</span>'
                         '<div class="live-none">最近 24 小时没有项目文件变动</div></div>'
                         % _now.strftime("%m-%d %H:%M:%S"))
            new = new.replace('<div id="live"></div>', _live, 1)
        except Exception as _e:
            new = new.replace(
                '<div id="live"></div>',
                '<div class="live"><b>★ 实时变动</b>'
                '<div class="live-none">扫描出错：%s</div></div>' % _e, 1)

        # ★ 每个项目的两个时间（2026-11-20）
        #   卡片上显示「信息更新 10:39 ｜ 指令 12:03」：
        #   前者 = 该项目文件最后动过的时间（客观）
        #   后者 = 这条判断是什么时候写的（主观）
        #   两个时间差得远，说明判断可能已经跟不上项目了。
        try:
            _jt = {}
            if os.path.exists(JUDGE_TIMES):
                with open(JUDGE_TIMES, encoding="utf-8") as _f:
                    _jt = json.load(_f)
            try:
                _cur2 = _cur
            except NameError:
                _cur2 = scan()
            _meta = {}
            for _n, _i in _cur2.items():
                _meta[_n] = {
                    "info": datetime.datetime.fromtimestamp(_i["mtime"]).strftime("%m-%d %H:%M"),
                    "judge": _jt.get(_n, ""),
                }
            _payload = json.dumps(_meta, ensure_ascii=False).replace("</", "<\\/")
            new = _re.sub(
                r'<script type="application/json" id="meta-times">.*?</script>',
                '<script type="application/json" id="meta-times">' + _payload + '</script>',
                new, count=1)
        except Exception:
            pass

        # ★ 状态字段注入（2026-11-21 · 设计 2）—— 跟 meta-times 同一个手法
        try:
            _ps = load_pstate()
            # ★★ 顺带算「状态字段多久没更新」：
            #   06:20 那个 cron 走 --set 只写散文，不写 pstate（它的 prompt 没带字段）。
            #   于是 pstate 会比判断旧 —— 卡片上必须看得出来，不能显示一个三天前的 stage
            #   让人当成现状。（同类坑：任何「写下来就不再重算」的东西都会过期。）
            _now3 = datetime.datetime.now()
            for _k, _v in list(_ps.items()):
                _at = (_v or {}).get("at", "")
                try:
                    _dt2 = datetime.datetime.strptime(_at, "%m-%d %H:%M").replace(year=_now3.year)
                    if _dt2 > _now3:
                        _dt2 = _dt2.replace(year=_now3.year - 1)
                    _v["stale_h"] = int((_now3 - _dt2).total_seconds() // 3600)
                except Exception:
                    _v["stale_h"] = None
            _ps_payload = json.dumps(_ps, ensure_ascii=False).replace("</", "<\\/")
            new = _re.sub(
                r'<script type="application/json" id="pstate">.*?</script>',
                '<script type="application/json" id="pstate">' + _ps_payload + '</script>',
                new, count=1)
        except Exception:
            pass

        # ★★★ 异常区（2026-11-21 · 设计 3）
        #   为什么加：8 张卡平铺，靠滚动找问题 —— 你得先看完才知道哪个不对。
        #   改成顶部一个异常区，只列【纯脚本能算出来】的三种不对，零 token：
        #     ① 停滞 >14 天，但队列还挂着活   ← 有活、没人做（最该看这条）
        #     ② 队列挂了 >72 小时没动          ← 派下去了，没回音
        #     ③ 判断 >26 小时没更新            ← 话是旧的，别照着它做决定
        #   ★ 「验收未通过 / 疑似完成」不进这里 —— 那两块由 /suspects 当场算，
        #     写进静态 HTML 会在刷新前就过期。
        try:
            import html as _h2
            _now2 = datetime.datetime.now()
            try:
                _scan2 = _cur2
            except NameError:
                _scan2 = scan()
            _jt3 = {}
            try:
                if os.path.exists(JUDGE_TIMES):
                    with open(JUDGE_TIMES, encoding="utf-8") as _f3:
                        _jt3 = json.load(_f3) or {}
            except Exception:
                _jt3 = {}

            # 判定逻辑抽到 compute_anomalies()，这里只负责渲染（可单独测）
            _anoms = compute_anomalies(_scan2, _jt3, _now2)

            if _anoms:
                _anoms.sort(key=lambda x: (x[0], x[1]))
                _li2 = "".join(
                    '<li><b>%s</b>　<span>%s</span></li>' % (_h2.escape(n), _h2.escape(w))
                    for _, n, w in _anoms)
                _blk = ('<div class="anom"><b>⚠ 异常 %d 条</b>'
                        '<span class="anom-t">只列能算出来的：停滞但有活 / 队列超 72 小时没动 / 判断过期</span>'
                        '<ul>%s</ul></div>' % (len(_anoms), _li2))
            else:
                _blk = ('<div class="anom ok"><b>✓ 无异常</b>'
                        '<span class="anom-t">没有「停滞但有活」/「队列超 72 小时没动」/「判断过期」的项目</span></div>')
            new = new.replace('<div class="live">', _blk + '<div class="live">', 1)
        except Exception:
            pass

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
