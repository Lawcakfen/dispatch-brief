# -*- coding: utf-8 -*-
"""审计：所有项目的 WB memory，压缩后有没有丢小节标题

为什么：2026-11-20 踩到内容看板的 memory 被压到 1212 字后硬截，
B3/B4/B5 三个「交付回报」小节整段消失，导致判断写出「B3 尚未接入」这种错话。
修了 condense() 之后要确认：别的项目还有没有同样的问题。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths                      # noqa: E402
import project_watch as pw         # noqa: E402

ROOT = paths.PROJECTS_ROOT
rows = []

for d in sorted(os.listdir(ROOT)):
    p = os.path.join(ROOT, d)
    if not os.path.isdir(p) or d.startswith("_"):
        continue
    mdir = paths.memory_dir(p)
    if not os.path.isdir(mdir):
        continue
    for fn in sorted(os.listdir(mdir)):
        if not fn.endswith(".md") or fn.upper() == "MEMORY.MD":
            continue
        if not re.match(r"\d{4}-\d{2}-\d{2}\.md$", fn):
            continue
        fp = os.path.join(mdir, fn)
        try:
            txt = open(fp, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        raw_n = len(re.findall(r"^##\s", txt, re.M))
        wbm = pw.wb_memory(p)                      # 整个项目（2 天合并）
        # 数这一份在压缩结果里剩了几个标题
        body = pw.condense(txt, 4500)
        got_n = len(re.findall(r"^##\s", body, re.M))
        lost = raw_n - got_n
        rows.append((d, fn, len(txt), raw_n, got_n, lost, "标题超预算" in body))

print("=" * 78)
print("%-30s %-14s %7s %5s %5s %5s %s" % ("项目", "文件", "字数", "原节", "压后", "丢失", ""))
print("=" * 78)
bad = 0
for d, fn, n, r, g, lost, cut in rows:
    flag = ""
    if lost > 0:
        flag = "← ★ 丢了 %d 节" % lost
        bad += 1
    if cut:
        flag += " ［标题被硬截］"
    print("%-30s %-14s %7d %5d %5d %5d %s" % (d[:30], fn, n, r, g, lost, flag))

print("=" * 78)
print("共 %d 份 memory，%d 份丢了小节" % (len(rows), bad))
