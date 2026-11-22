# -*- coding: utf-8 -*-
"""异常区的判定逻辑单测（2026-11-21 · 设计 3）

为什么单独测：这段逻辑的三个分支写反了都不会报错 ——
  漏判（该响不响）会让人错过卡住的项目；
  误判（不该响总响）会让人不再看异常区，功能等于不存在。
所以每种情况都要造出来跑一遍，断言「该响的响、该静的静」。

时间全部用假 now 控制，不依赖真实时钟（否则测试明天就坏了）。
"""
import datetime
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_watch as pw   # noqa: E402

NOW = datetime.datetime(2026, 11, 21, 20, 0, 0)


def make_project(root, name, pend=0, idle_days=0.0, queue_age_h=0.0):
    """造一个项目：queue 里有 pend 条待发，项目文件 idle_days 天没动，
    队列文件 queue_age_h 小时没动。"""
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    qf = os.path.join(d, "_给WB.md")
    body = "## ⏳ 待发\n\n"
    for i in range(pend):
        body += "- [ ] **11-21** 任务 %d\n" % (i + 1)
    body += "\n## ✅ 已完成\n\n（暂无）\n"
    with open(qf, "w", encoding="utf-8") as f:
        f.write(body)

    # 产物文件（代表项目自身活动）
    prod = os.path.join(d, "产物.html")
    with open(prod, "w", encoding="utf-8") as f:
        f.write("<html></html>")

    t_prod = NOW.timestamp() - idle_days * 86400
    t_q = NOW.timestamp() - queue_age_h * 3600
    os.utime(prod, (t_prod, t_prod))
    os.utime(qf, (t_q, t_q))
    return {"mtime": t_prod, "files": {"产物.html": t_prod}, "nfiles": 1}


def run_case(label, projects, judge_times, expect_kinds, expect_names):
    root = tempfile.mkdtemp(prefix="anom_")
    old = pw.ROOT
    pw.ROOT = root
    try:
        scan_result = {}
        for name, kw in projects.items():
            scan_result[name] = make_project(root, name, **kw)
        got = pw.compute_anomalies(scan_result, judge_times, NOW, warn_h=12, bad_h=72)
    finally:
        pw.ROOT = old
        shutil.rmtree(root, ignore_errors=True)

    kinds = [k for k, _, _ in got]
    names = [n for _, n, _ in got]
    ok = (kinds == expect_kinds) and (names == expect_names)
    print("  %s %s" % ("✓" if ok else "✗", label))
    print("     期望 类别=%s 项目=%s" % (expect_kinds, expect_names))
    print("     实际 类别=%s 项目=%s" % (kinds, names))
    for k, n, w in got:
        print("       [%d] %s —— %s" % (k, n, w))
    return ok


def main():
    print("=== 异常区判定单测（假 now = 2026-11-21 20:00）===")
    res = []

    # 1 停滞 20 天，队列还挂着 2 条 → 该响（优先级 1）
    res.append(run_case(
        "停滞 20 天 + 队列 2 条 → 报「停滞」",
        {"甲项目": {"pend": 2, "idle_days": 20, "queue_age_h": 5}},
        {}, [1], ["甲项目"],
    ))

    # 2 项目今天动过，但队列挂了 100 小时 → 该响（优先级 2）
    res.append(run_case(
        "队列挂 100 小时（项目刚动过）→ 报「队列没动」",
        {"乙项目": {"pend": 1, "idle_days": 0, "queue_age_h": 100}},
        {}, [2], ["乙项目"],
    ))

    # 3 一切新鲜，但判断是 30 小时前写的 → 该响（优先级 3）
    res.append(run_case(
        "判断 30 小时没更新 → 报「判断过期」",
        {"丙项目": {"pend": 1, "idle_days": 0, "queue_age_h": 1}},
        {"丙项目": "11-20 14:00"}, [3], ["丙项目"],
    ))

    # 4 队列里没有待发 → 不判定（没在办不等于出问题）
    res.append(run_case(
        "队列空 + 停滞 30 天 → 不该响",
        {"丁项目": {"pend": 0, "idle_days": 30, "queue_age_h": 800}},
        {"丁项目": "11-01 09:00"}, [], [],
    ))

    # 5 全正常 → 空
    res.append(run_case(
        "一切正常 → 不该响",
        {"戊项目": {"pend": 1, "idle_days": 0, "queue_age_h": 1}},
        {"戊项目": "11-21 19:00"}, [], [],
    ))

    # 6 三种同时命中 → 按优先级 1,2,3 排，且同一项目可出多条
    res.append(run_case(
        "停滞 + 判断过期 同时命中 → 两条，按优先级排",
        {"己项目": {"pend": 3, "idle_days": 20, "queue_age_h": 500}},
        {"己项目": "11-18 09:00"}, [1, 3], ["己项目", "己项目"],
    ))

    # 7 ★ 2026-11-22 新增档：派了没动（> warn_h，默认 12h）
    res.append(run_case(
        "队列挂 20 小时 + 停滞 1 天 → 该响「派了没动」档",
        {"庚项目": {"pend": 3, "idle_days": 1, "queue_age_h": 20}},
        {}, [1], ["庚项目"],
    ))

    # 8 边界：刚好 11 小时 → 不该响（阈值是 >12，不是 >=12）
    res.append(run_case(
        "队列挂 11 小时 → 不该响（没到 12 小时）",
        {"辛项目": {"pend": 2, "idle_days": 0, "queue_age_h": 11}},
        {}, [], [],
    ))

    # 9 两档不叠加：挂 100 小时只报「挂太久」一条，不能又报「派了没动」
    res.append(run_case(
        "队列挂 100 小时 → 只响「挂太久」一条，不重复响小时档",
        {"壬项目": {"pend": 1, "idle_days": 1, "queue_age_h": 100}},
        {}, [2], ["壬项目"],
    ))

    print()
    n_ok = sum(1 for x in res if x)
    if n_ok == len(res):
        print("  结论：%d/%d 全部通过" % (n_ok, len(res)))
        return 0
    print("  结论：%d/%d 通过，%d 项失败" % (n_ok, len(res), len(res) - n_ok))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
