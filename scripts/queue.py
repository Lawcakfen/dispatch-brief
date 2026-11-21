#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
queue.py · 队列 <-> 工作报告 的匹配与打勾

设计原则（宁可漏，不可错）
--------------------------
★★★★ 队列撒谎比队列空更糟。
所以只承认【明确的完成信号 + 有可辨识的标识符重合】，其余一律不动，
让用户手动说一句来补 —— 补一次的成本远低于信一次错的数据。
"""
import os
import re

# 明确的完成信号（缺一个都不算）
DONE_SIGNALS = (
    "已修", "已改", "已完成", "已上架", "已交付", "已发布", "已上线",
    "已生成", "已落地", "自检通过", "改完了", "做完了", "跑通", "已补齐", "已整理",
)
# ★★★ 2026-11-19：原来这里是「改完」两个字，实测会误命中。
#   WB 写「改完做 py_compile + node --check + md5 复核再汇报」——语义是「改完之后要做什么」，
#   还没做完，但「改完」被当成完成信号，直接打勾。
#   改成「改完了」：要求后面跟「了」，才表示已经发生。

# 探路词：出现这些但没完成信号时，明确不算完成
PROBE_SIGNALS = ("发现", "查了一下", "试了", "报错", "不能用", "需要确认", "建议")


def _tokens(text):
    """抽取可辨识的 ASCII 标识符（文件名 / 函数名 / 变量名 / 编号）。

    ★ 中文分词不可靠，但代码项目里的『标识符』是天然的唯一指纹：
      ideaToTopic / main.js / detail.js / SKU1 / appid / CONTACT / prepare
    """
    out = set()
    for m in re.findall(r"[A-Za-z_][A-Za-z0-9_.\-]{2,}", text or ""):
        t = m.lower().strip(".-_")
        if len(t) < 3:
            continue
        # 过滤掉无辨识度的常见词
        if t in {"html", "json", "css", "the", "and", "for", "with", "src", "www", "com",
                 "md", "py", "js", "http", "https", "www"}:
            continue
        out.add(t)
    return out


def fp(item):
    """条目指纹：取正文前 24 个字（去掉日期前缀和 **），用于跨轮对比。"""
    t = item["text"] if isinstance(item, dict) else str(item)
    t = re.sub(r"^\*\*[\d\-]+\*\*\s*", "", t).replace("**", "").strip()
    return t[:24]


def acceptance_of(item):
    """取条目里的 `验收:` 判据（支持全角/半角冒号，允许反引号）。

    格式（写在条目的续行里，缩进两个空格或 `  - `）：
        - [ ] **11-21** **空本子删周历条目**
          - 验收: grep -c "delCal" 空本子.html && python selftest.py

    ★ 2026-11-21 新增：队列此前只能靠「读 memory 猜语义」打勾，实测假阳性 50%。
      加了这一行之后，打勾变成「跑一条命令」—— 确定性，不需要猜。
    """
    for _, raw in item.get("extra", []):
        m = re.match(r"^\s*[-*]?\s*\**验收\**\s*[:：]\s*(.+)$", raw.strip())
        if m:
            return m.group(1).strip().strip("`").strip()
    return None


def _run_sh(cmd, cwd, timeout):
    """跑一条 shell 命令，返回 (returncode, 合并输出)。"""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash:
        p = subprocess.run([bash, "-lc", cmd], cwd=cwd, timeout=timeout,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="ignore")
    else:
        p = subprocess.run(cmd, shell=True, cwd=cwd, timeout=timeout,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="ignore")
    out = (p.stdout or "").strip() or (p.stderr or "").strip()
    return p.returncode, re.sub(r"\s+", " ", out)[:150]


def run_acceptance(cmd, cwd, timeout=25):
    """跑一条验收判据，返回 (ok, detail)。约定：退出码 0 = 通过。

    ★★ 2026-11-21：失败时要指明「是哪一条没过」，否则详情只有「退出码 1」，
       对修的人没用。判据一般写成 a && b && c，所以失败时按 && 拆开逐条跑，
       报第一条失败的那句 —— 这比让人自己一行行试强。
    """
    try:
        rc, out = _run_sh(cmd, cwd, timeout)
        if rc == 0:
            return True, (out or "通过")

        # 失败：逐条定位（只对 && 连接的判据有效，其余直接返回）
        parts = [p.strip() for p in cmd.split(" && ") if p.strip()]
        if len(parts) > 1:
            for i, part in enumerate(parts, 1):
                prc, pout = _run_sh(part, cwd, timeout)
                if prc != 0:
                    return False, "第 %d/%d 条判据未过：`%s`%s" % (
                        i, len(parts), part[:70],
                        ("；" + pout) if pout else "",
                    )
        return False, "退出码 %d；%s" % (rc, out or "无输出")
    except Exception as e:
        msg = str(e)
        if "TimeoutExpired" in type(e).__name__:
            msg = "超时 %d 秒" % timeout
        return False, "执行失败：%s" % msg


def parse_queue(path):
    """解析 _给WB.md，返回 {'todo': [(date, text, extra_lines)], 'done': [...], 'raw': ...}"""
    if not os.path.isfile(path):
        return None
    txt = open(path, encoding="utf-8", errors="ignore").read()
    lines = txt.split("\n")

    def section_bounds(title_re):
        start = None
        for i, l in enumerate(lines):
            if re.match(title_re, l.strip()):
                start = i + 1
                break
        if start is None:
            return None, None
        end = len(lines)
        for j in range(start, len(lines)):
            if lines[j].startswith("## "):
                end = j
                break
        return start, end

    s, e = section_bounds(r"^##\s*⏳?\s*待发")
    items = []
    if s is not None:
        cur = None
        for i in range(s, e):
            l = lines[i]
            st = l.strip()
            if st.startswith("- [ ]"):
                if cur:
                    items.append(cur)
                cur = {"line": i, "text": st[5:].strip(), "extra": []}
            elif cur is not None and st and not st.startswith("#"):
                cur["extra"].append((i, l))
            elif not st and cur is not None:
                pass
        if cur:
            items.append(cur)
    return {"items": items, "lines": lines, "todo_range": (s, e)}


def is_done_signal(mem_text):
    """工作报告里有没有明确的『这一步做完了』信号。"""
    t = mem_text or ""
    hit = [k for k in DONE_SIGNALS if k in t]
    if not hit:
        return None
    # 只有探路词、没有完成词时不算（这个分支在上面已排除，这里留作说明）
    return hit


STOP_PHRASES = {
    "现在", "包括但不限于", "为什么", "做完请", "的问题", "并修掉", "每条标出",
    "需要谁提供什么", "缺了会影响什么", "改成按", "所属账号", "公众号", "小红书",
    "视频号", "图文", "视频", "测试", "条目", "内容", "工作", "今天", "明天",
}


def _phrases(text):
    """把中文文本切成「有意义的中文短语」。

    ★ 没有分词库时的折中：按标点/空白切开，保留 3–14 字、且不是套话的片段。
      再补上原文里所有 4 字窗口（覆盖「复盘表单」这种没被标点分开的词）。
    """
    out = set()
    raw = re.split(r"[，。；：、「」『』（）()／/、\s\-—·…,.;:!?！？\[\]【】<>《》]+", text or "")
    for seg in raw:
        seg = seg.strip()
        seg = re.sub(r"[*`#*>\[\]]", "", seg)
        if 3 <= len(seg) <= 14 and seg not in STOP_PHRASES:
            out.add(seg)
    # 4 字滑窗（捕捉未分隔的复合词）
    flat = re.sub(r"[^一-龥]", "", text or "")
    for i in range(len(flat) - 3):
        w = flat[i:i + 4]
        if w not in STOP_PHRASES:
            out.add(w)
    return out


def match_done(mem_text, queue_items):
    """从队列里找出「这条报告说它做完了」的那一条。

    判据：① 报告有完成信号 ② 报告与该条的 ASCII 标识符有交集
    ★ 两个条件缺一不可。标识符一个都对不上的，宁可漏掉。
    """
    if not is_done_signal(mem_text):
        return None, "无完成信号"
    mem_t = _tokens(mem_text)
    best, best_score = None, 0
    for idx, it in enumerate(queue_items):
        qt = _tokens(it["text"] + " " + " ".join(x[1] for x in it["extra"]))
        if not qt or not mem_t:
            continue
        inter = qt & mem_t
        if len(inter) > best_score:
            best, best_score = idx, len(inter)
    if best is not None and best_score >= 2:
        # ★★★ 2026-11-19 加阈值：原来这里没有 best_score 检查，重合 1 个就打勾。
        #   实测：v8 那条队列和 memory 里的报告共享 build_board.py / board.html / md5 /
        #   py_compile 等多个标识，1 个根本挡不住。而两个具体标识（任务名 + 文件名）
        #   同时出现，才说明报告讲的是这件事，不是顺手提到它。
        #   ★ 为什么是 2 不是更高：纯中文任务走下面那个分支（阈值 4）；
        #     能走到这里的都是带英文标识的，2 个已经是很强的一致性要求。
        return best, "标识符重合 %d 个" % best_score
    if best is not None:
        return None, "标识符重合不足（%d 个，需 ≥2）" % best_score

    # ★ 兜底：双方都没有英文标识符时（纯中文任务），改用中文短语重合，要求 ≥2
    mt = _phrases(mem_text)
    if not mt:
        return None, "报告里没有可辨识短语"
    best, best_score = None, 0
    for idx, it in enumerate(queue_items):
        qt = _phrases(it["text"] + " " + " ".join(x[1] for x in it["extra"]))
        inter = qt & mt
        if len(inter) > best_score:
            best, best_score = idx, len(inter)
    # ★ 阈值 4：实测「真命中」重合 10 个、「无关的完成」重合 3 个，4 能干净分开。
    #   不要用「短语长度」当判据 —— 中文切出来最长往往就 4 字，会把真命中挡掉。
    if best is None or best_score < 4:
        return None, "中文短语重合不足（%d 个，需 ≥4）" % best_score
    return best, "中文短语重合 %d 个" % best_score


def tick_item(path, q, idx, stamp):
    """把第 idx 条从「⏳ 待发」移到「✅ 已完成」，其余行原样不动。"""
    lines = list(q["lines"])
    it = q["items"][idx]

    # 1) 摘出这一条的所有行（正文 + 续行），从待发段删掉
    drop = {it["line"]} | {ln for ln, _ in it["extra"]}
    for ln in sorted(drop, reverse=True):
        del lines[ln]

    # 2) 重新定位「✅ 已完成」段（行号已变）
    start = None
    for i, l in enumerate(lines):
        if re.match(r"^##\s*✅\s*已完成", l.strip()):
            start = i + 1
            break
    if start is None:
        # 没有这一节，追加到文件末尾
        lines += ["", "## ✅ 已完成", ""]
        start = len(lines)

    # 3) 组装打勾后的条目
    text = re.sub(r"^\*\*[\d\-]+\*\*\s*", "", it["text"])
    dm = re.match(r"^\*\*([\d\-]+)\*\*", it["text"])
    date = dm.group(1) if dm else ""
    new = [f"- [x] **{date}** {text}" if date else f"- [x] {text}"]
    new[0] += f"（完成于 {stamp}）"
    for _, raw in it["extra"]:
        new.append(raw)

    # 4) 去掉「（无）」占位，再插入到段首
    insert_at = start
    while insert_at < len(lines) and lines[insert_at].strip() in ("", "（无）", "<!-- 无 -->"):
        if lines[insert_at].strip() == "（无）":
            del lines[insert_at]
            continue
        insert_at += 1
        if insert_at < len(lines) and lines[insert_at].startswith("## "):
            break
    lines[insert_at:insert_at] = new + [""]

    open(path, "w", encoding="utf-8").write("\n".join(lines))
    return text


def next_item(path):
    """取队列里当前第一条待发（用于推荐「下一条」）。"""
    q = parse_queue(path)
    if not q or not q["items"]:
        return None
    it = q["items"][0]
    text = re.sub(r"^\*\*[\d\-]+\*\*\s*", "", it["text"]).replace("**", "")
    extra = " ".join(x[1].strip() for x in it["extra"])
    full = (text + " " + extra).strip()
    full = re.sub(r"\s+", " ", full)
    return {"text": text, "full": full}
