# -*- coding: utf-8 -*-
"""单测：有待发但当天无产出的项目，必须补出一张「事实卡」

背景（2026-11-22 实测）
----------------------
空本子派出去 8 小时、html 20 小时没动，看板上却找不到它 ——
卡片只来自最新一次「## … 汇报」段，而入段条件是「24h 内有产出文件变动」。
补卡的难点不在拼接字符串，在【时间取哪个】：

    直接取项目 mtime → 会把「我刚改过队列」算成「它刚干过活」
    → 卡上写「8 小时前」，等于替执行方撒谎

所以这条测的就是：_out_mtime 必须排除队列文件和指令文件。
"""
import datetime
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_watch as pw

NOW = datetime.datetime(2026, 11, 22, 10, 0, 0)
H = 3600.0


def ts(hours_ago):
    return (NOW - datetime.timedelta(hours=hours_ago)).timestamp()


def case(label, md, scan_result, expect_n, checks):
    """checks: [(子串, 该不该出现)]"""
    got_md, n = pw.fill_missing_cards(md, scan_result)
    ok = (n == expect_n)
    detail = []
    for sub, want in checks:
        hit = sub in got_md
        if hit != want:
            ok = False
            detail.append("「%s」应%s，实际%s" % (sub, "出现" if want else "不出现", "出现" if hit else "没出现"))
    print("  %s %s" % ("✓" if ok else "✗", label))
    print("     期望补 %d 条 / 实际补 %d 条" % (expect_n, n))
    for d in detail:
        print("     ✗ " + d)
    return ok


def main():
    print("=== 补事实卡单测（假 now = 2026-11-22 10:00）===")
    root = tempfile.mkdtemp(prefix="cards_")
    old_root, old_now = pw.ROOT, pw.datetime.datetime
    res = []

    class FakeDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    pw.ROOT = root
    pw.datetime.datetime = FakeDT
    try:
        # 造一个项目：产物 20 小时前，队列刚改过
        p = os.path.join(root, "甲项目")
        os.makedirs(p)
        open(os.path.join(p, "_给WB.md"), "w", encoding="utf-8").write(
            "# 给\n\n## ⏳ 待发\n\n- [ ] 一条活\n- [ ] 两条活\n")
        open(os.path.join(p, "产物.html"), "w", encoding="utf-8").write("x")

        scan = {"甲项目": {"mtime": ts(0), "files": {
            "_给WB.md": ts(0),                                  # 队列：刚改
            "WB指令-2026-11-21.md": ts(0),                       # 指令：刚改
            "产物.html": ts(20),                                 # ← 真正的最后产出
        }}}

        md0 = ("# 总表\n\n## 11-22 汇报\n\n"
               "- 乙项目：现在 在动 ｜ 建议 继续\n\n## 给WB 待发\n")

        # 1 有待发 + 当天无产出 → 补一条，且时间取产物（20 小时前），不是队列（刚改）
        res.append(case(
            "有待发、当天无产出 → 补一条，时间取「产物」不取队列",
            md0, scan, 1,
            [("甲项目：现在 队列挂着 2 条待发", True),
             ("（20 小时前）", True),
             ("（0 小时前）", False),
             ("系统补报·非判断", True),
             ("｜ 建议", True)],
        ))

        # 2 已在汇报段里的项目 → 不重复补
        md1 = md0.replace("- 乙项目", "- 甲项目")
        res.append(case(
            "汇报段里已经有它 → 不重复补",
            md1, scan, 0, [("甲项目", True)],
        ))

        # 3 队列没待发 → 不当异常，也不补卡
        open(os.path.join(p, "_给WB.md"), "w", encoding="utf-8").write(
            "# 给\n\n## ⏳ 待发\n\n## ✅ 已完成\n\n- [x] 做完了\n")
        res.append(case(
            "队列里没有待发 → 不补（没在办 ≠ 卡住）",
            md0, scan, 0, [("系统补报", False)],
        ))

        # 4 没有汇报段 → 原样返回，不崩
        res.append(case(
            "没有「## … 汇报」段 → 原样返回，不崩",
            "# 总表\n\n## 甲项目\n- 11-22 动了\n", scan, 0,
            [("系统补报", False)],
        ))

        # 5 ★ _out_mtime 必须排除队列和指令
        om = pw._out_mtime(scan["甲项目"])
        ok5 = abs(om - ts(20)) < 2
        print("  %s _out_mtime 排除队列/指令文件" % ("✓" if ok5 else "✗"))
        print("     期望 ≈ 20 小时前，实际 %s" %
              datetime.datetime.fromtimestamp(om).strftime("%m-%d %H:%M:%S"))
        res.append(ok5)

        # 6 只有队列和指令、没有任何产物 → 退回项目 mtime，不能崩
        om2 = pw._out_mtime({"mtime": ts(5), "files": {"_给WB.md": ts(0)}})
        ok6 = abs(om2 - ts(5)) < 2
        print("  %s 只有队列/指令时退回项目 mtime，不崩" % ("✓" if ok6 else "✗"))
        res.append(ok6)

    finally:
        pw.ROOT = old_root
        pw.datetime.datetime = old_now
        shutil.rmtree(root, ignore_errors=True)

    print()
    n_ok = sum(1 for x in res if x)
    if n_ok == len(res):
        print("  结论：%d/%d 全部通过" % (n_ok, len(res)))
        return 0
    print("  结论：%d/%d 通过，%d 项失败" % (n_ok, len(res), len(res) - n_ok))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
