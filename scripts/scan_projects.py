#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按项目目录聚合「最后活动 / 文件数 / 最新文件」，输出四档表。

用法:
    python scan_projects.py [ROOT] [--json]

默认 ROOT 读 <同目录>/paths.py 的 PROJECTS_ROOT（即 agent-config.json 里的 projects_root）；
    也可以用环境变量 PROJECTS_ROOT 或命令行第一个参数覆盖。

设计要点（踩过的坑写在注释里）：
  · 排除 .workbuddy / node_modules / .venv / __pycache__ 等 —— 否则工程型项目会刷出上万文件
    （实测：某微信小程序项目 10,166 个文件）
  · mtime 只能证明「有人碰过」，证明不了「推进了」→ 只用于报警，不用于评价
  · 分档是纯时间算术，不引入任何主观判断
"""
import json
import os
import sys
import datetime

DEFAULT_ROOT = None
try:
    # ★ 2026-11-21：原来这里写死本机项目路径，带这个文件发布就会漏出去。
    #   改成读 paths.py（它自己读 agent-config.json）。
    import paths as _paths
    DEFAULT_ROOT = _paths.PROJECTS_ROOT
except Exception:
    DEFAULT_ROOT = os.environ.get("PROJECTS_ROOT", "")

# ★ 这些目录不算「项目的活动」
SKIP_DIRS = {
    # ★ 2026-11-22：日志目录名从工作流里解放出来，四个候选都排掉
    ".agent", ".workbuddy", ".codex", ".claude",
    "node_modules", ".venv", "venv", "__pycache__",
    ".git", "dist", ".next", ".cache", "Temp", "backups",
}


def scan(root):
    now = datetime.datetime.now()
    rows = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        newest = 0.0
        nfile = 0
        newest_rel = ""
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    m = os.path.getmtime(fp)
                except OSError:
                    continue
                nfile += 1
                if m > newest:
                    newest = m
                    newest_rel = os.path.relpath(fp, path)
        if not newest:
            continue
        t = datetime.datetime.fromtimestamp(newest)
        rows.append({
            "project": name,
            "last": t.strftime("%Y-%m-%d %H:%M"),
            "days": (now - t).days,
            "files": nfile,
            "newest_file": newest_rel[:60],
        })
    rows.sort(key=lambda r: r["days"])
    return rows


def bucket(days):
    if days <= 2:
        return "在动"
    if days <= 7:
        return "慢下来"
    if days <= 20:
        return "悬着"
    return "事实上停了"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = args[0] if args else DEFAULT_ROOT
    as_json = "--json" in sys.argv

    if not root:
        print("✗ 还没配项目根目录（projects_root）。")
        print()
        print("  按这两步：")
        print("    1) 把 scripts/agent-config.example.json 另存为同目录的 agent-config.json")
        print('    2) 在它里面填 projects_root —— 你的项目根目录')
        print("       （它下面每个子文件夹 = 一个项目，每个项目里要有队列文件如 _给WB.md）")
        print()
        print("  配完先跑这个确认：python paths.py")
        print("  看到「项目根可用  是」再回来跑本脚本。")
        return 1

    if not os.path.isdir(root):
        # ★ 和「没配」分开说：这里配了，但路径不存在 —— 多半是打错字或盘符不对
        print(f"✗ 配置里的 projects_root 不存在：{root}")
        print()
        print("  可能原因：打错字 / 盘符不对 / 目录被移动")
        print("  排查：python paths.py   （它会告诉你这个值是从哪来的）")
        return 1

    rows = scan(root)
    for r in rows:
        r["state"] = bucket(r["days"])

    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0

    total = len(rows)
    alive = sum(1 for r in rows if r["days"] <= 1)
    print(f"共 {total} 个项目 · 今天/昨天有动作 {alive} 个\n")
    print(f"{'项目':44s} {'最后活动':16s} {'档':<6s} {'文件':>6s}  最新文件")
    print("-" * 118)
    for r in rows:
        print(f"{r['project'][:42]:44s} {r['last']:16s} {r['state']:<6s} "
              f"{r['files']:6d}  {r['newest_file']}")

    print("\n=== 分档汇总 ===")
    for b in ("在动", "慢下来", "悬着", "事实上停了"):
        names = [r["project"] for r in rows if r["state"] == b]
        print(f"{b:6s} {len(names):2d} 个" + ("  · " + " / ".join(n[:14] for n in names[:8]) if names else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
