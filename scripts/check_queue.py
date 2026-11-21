# -*- coding: utf-8 -*-
"""刷新时检查队列 —— 只提示，不自己打勾

为什么不全自动：
    实测过，全自动会错。例子：队列里挂着「README 从 v5.1 重写到 v9」，
    而 README 其实还是 v5.1（没做）；但 WB 的 memory 里到处是「已完成/交付」，
    也到处提到 README，于是 match_done 判它「已完成」，标识符还重合 10 个。
    → 全自动打勾 = 错打勾。你的铁律是「宁漏不错」，所以这一步必须人确认。

所以本脚本只做两件事：
    ① 检查：扫各项目队列，找出「疑似已完成」的条目 → 输出给人看
    ② 打勾：只有明确指定「哪一条」时才动手（供看板上的「确认完成」按钮调）

用法：
    python check_queue.py --dry                     # JSON 输出疑似清单，不改文件
    python check_queue.py --tick "项目名" "指纹"      # 打勾指定那一条
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths       # noqa: E402
import queue as Q  # noqa: E402

ROOT = paths.PROJECTS_ROOT
LOOKBACK_DAYS = 2


def recent_memory(proj_dir):
    mdir = paths.memory_dir(proj_dir)
    if not os.path.isdir(mdir):
        return ""
    today = datetime.date.today()
    keep = {(today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(LOOKBACK_DAYS)}
    parts = []
    for fn in sorted(os.listdir(mdir)):
        if not fn.endswith(".md") or fn.upper() == "MEMORY.MD":
            continue
        if os.path.splitext(fn)[0] not in keep:
            continue
        try:
            with open(os.path.join(mdir, fn), encoding="utf-8", errors="ignore") as f:
                parts.append(f.read())
        except Exception:
            pass
    return "\n".join(parts)


def scan(autotick=True):
    """扫所有项目队列，分三类返回。

    ★★ 2026-11-21 改造（设计 1：验收判据）
       以前只有一条路：读 memory 猜语义 → 假阳性 50%，必须人点确认。
       现在分两条路，按「判据是事实还是语义」分开（这条原则写在 skill 里）：
         · 条目带 `验收:` → 跑命令，退出码 0 = 通过 → **可以全自动打勾**
         · 条目没有判据   → 退回语义匹配 → 仍然只提示，人点确认

    返回 {"suspects": [...], "verified": [...], "failed": [...]}
        suspects  语义疑似，需人确认
        verified  验收判据通过（已打勾，或本轮打勾）
        failed    有判据但没通过 → 卡片要显示「验收未通过」+ 输出，比「还没做」更有用
    """
    hints, verified, failed = [], [], []

    for d in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p) or d.startswith("_"):
            continue
        qf = os.path.join(p, "_给WB.md")
        if not os.path.isfile(qf):
            continue
        try:
            q = Q.parse_queue(qf)
        except Exception:
            continue
        if not q.get("items"):
            continue

        mem = None  # 延迟读：只有走语义分支时才需要
        seen_fp = set()

        for i, it in enumerate(q["items"]):
            f = Q.fp(it)
            if f in seen_fp:
                continue

            acc = Q.acceptance_of(it)
            if acc:
                ok, detail = Q.run_acceptance(acc, p)
                rec = {
                    "project": d, "queue": qf, "idx": i, "fp": f,
                    "text": it["text"].replace("**", "")[:70],
                    "criterion": acc, "detail": detail, "kind": "verified" if ok else "failed",
                }
                (verified if ok else failed).append(rec)
                seen_fp.add(f)
                continue

            # ── 没有判据：退回语义匹配（需人确认）──
            if mem is None:
                mem = recent_memory(p)
            if not mem.strip():
                continue
            idx, why = Q.match_done(mem, [it])
            if idx is None:
                continue
            seen_fp.add(f)
            hints.append({
                "project": d, "queue": qf, "idx": i, "fp": f,
                "text": it["text"].replace("**", "")[:70],
                "why": why, "kind": "hint",
            })

    # ★ 验收通过的自动打勾（确定性，不需要人点）
    if autotick:
        for v in verified:
            r = do_tick(v["project"], v["fp"])
            v["ticked"] = bool(r.get("ok"))
            v["tick_out"] = r.get("out", "")

    return {"suspects": hints, "verified": verified, "failed": failed}


def suspects():
    """兼容旧调用：只要语义疑似（不打勾）。"""
    return scan(autotick=False)["suspects"]


def do_tick(proj, fp):
    """按「项目名 + 条目指纹」打勾 —— 指纹比索引稳定，队列改了也不会勾错"""
    qf = os.path.join(ROOT, proj, "_给WB.md")
    if not os.path.isfile(qf):
        return {"ok": False, "out": "找不到队列 %s" % qf}
    try:
        q = Q.parse_queue(qf)
    except Exception as e:
        return {"ok": False, "out": "队列解析失败：%s" % e}

    for i, it in enumerate(q["items"]):
        if Q.fp(it) == fp:
            stamp = datetime.datetime.now().strftime("%m-%d")
            try:
                r = Q.tick_item(qf, q, i, stamp)
                return {"ok": True, "out": "%s ← %s" % (r, it["text"].replace("**", "")[:50])}
            except Exception as e:
                return {"ok": False, "out": "打勾失败：%s" % e}
    return {"ok": False, "out": "队列里找不到这条（可能已经打过了）"}


def main():
    if "--tick" in sys.argv:
        i = sys.argv.index("--tick")
        args = [a for a in sys.argv[i + 1:] if not a.startswith("--")]
        if len(args) < 2:
            print(json.dumps({"ok": False, "out": "用法：--tick 项目名 指纹"}, ensure_ascii=False))
            return 1
        print(json.dumps(do_tick(args[0], args[1]), ensure_ascii=False))
        return 0

    # --no-tick：只看不勾（默认会勾掉「验收判据通过」的条目）
    res = scan(autotick=("--no-tick" not in sys.argv))
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
