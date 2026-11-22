# -*- coding: utf-8 -*-
"""日志目录可配 的单测（2026-11-22）

背景
----
原来「执行方日志目录」写死 <项目>/.workbuddy/memory。
Codex / Claude / Cursor 用户被告知要往 .workbuddy 里写 —— 心理上别扭，是真实的劝退点。

现在：对外约定 .agent/memory，读取时兜底旧路径。

测什么
------
  ① 只有 .agent/memory       → 用它（新约定）
  ② 只有 .workbuddy/memory    → 兜底用它（存量 21 个项目靠这条，不能坏）
  ③ 两处都有                → 用 .agent（优先级高），且 others 报出 .workbuddy（不能装看不见）
  ④ 都没有                  → 返回默认 .agent/memory（供报错时给具体路径）
  ⑤ 配置里写了 memory_dir    → 最高优先，压过一切
  ⑥ 端到端：wb_watch.scan_memory 能读到 .agent/memory 的日志
"""
import os
import shutil
import sys
import tempfile

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)

fails = []


def chk(name, cond, extra=""):
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("　→ " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


def mk(root, name, rel, files=("2026-11-22.md",)):
    d = os.path.join(root, name, *rel.split("/"))
    os.makedirs(d)
    for f in files:
        with open(os.path.join(d, f), "w", encoding="utf-8") as fh:
            fh.write("## 10:30 测试\n")
    # 队列文件，让这个目录算「项目」
    with open(os.path.join(root, name, "_给WB.md"), "w", encoding="utf-8") as fh:
        fh.write("## ⏳ 待发\n\n- [ ] **11-22** **测试**\n")
    return d


def main():
    tmp = tempfile.mkdtemp(prefix="memdir-")
    root = os.path.join(tmp, "projects")
    os.makedirs(root)

    mk(root, "A约定", ".workbuddy/memory")
    mk(root, "B兜底", ".agent/memory")
    mk(root, "C两处都有", ".workbuddy/memory")
    os.makedirs(os.path.join(root, "C两处都有", ".agent", "memory"))
    os.makedirs(os.path.join(root, "D都没有"))

    os.environ.pop("PROJECTS_ROOT", None)
    os.environ["PROJECTS_ROOT"] = root
    for m in ("paths", "project_watch", "wb_watch"):
        sys.modules.pop(m, None)

    import paths

    pA = os.path.join(root, "A约定")
    pB = os.path.join(root, "B兜底")
    pC = os.path.join(root, "C两处都有")
    pD = os.path.join(root, "D都没有")

    chk("① 只有 .workbuddy → 用 .workbuddy（这是约定）",
        paths.memory_dir(pA).replace("\\", "/").endswith("A约定/.workbuddy/memory"),
        paths.memory_dir(pA))
    chk("② 只有 .agent → 兜底用它（白天那版说明建的目录不能读不到）",
        paths.memory_dir(pB).replace("\\", "/").endswith("B兜底/.agent/memory"),
        paths.memory_dir(pB))
    chk("③ 两处都有 → 优先 .workbuddy",
        paths.memory_dir(pC).replace("\\", "/").endswith("C两处都有/.workbuddy/memory"),
        paths.memory_dir(pC))
    others = paths.memory_dir_others(pC)
    chk("③ 两处都有 → others 报出被忽略的那一处",
        len(others) == 1 and others[0].replace("\\", "/").endswith(".agent/memory"),
        [o.replace("\\", "/") for o in others])
    chk("④ 都没有 → 返回默认 .workbuddy/memory",
        paths.memory_dir(pD).replace("\\", "/").endswith("D都没有/.workbuddy/memory"),
        paths.memory_dir(pD))
    chk("④ 都没有时 others 为空", paths.memory_dir_others(pD) == [], paths.memory_dir_others(pD))

    # ⑤ 显式配置压过一切
    paths._cfg["memory_dir"] = ".agent/memory"
    chk("⑤ 配置 memory_dir 时优先生效",
        paths.memory_dir(pB).replace("\\", "/").endswith("B兜底/.agent/memory"),
        paths.memory_dir(pB))
    chk("⑤ 配置了就不再提示 others", paths.memory_dir_others(pC) == [], paths.memory_dir_others(pC))
    paths._cfg["memory_dir"] = ""

    # ⑥ 端到端：wb_watch 两处都要能读到
    import wb_watch
    got = wb_watch.scan_memory()
    chk("⑥ wb_watch 读到 .workbuddy/memory 的项目", "A约定" in got, list(got))
    chk("⑥ wb_watch 也读到 .agent/memory 的项目", "B兜底" in got, list(got))

    shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print("结论：✗ %d 项不过：%s" % (len(fails), "；".join(fails)))
        return 1
    print("结论：全部通过（新约定可用，旧路径没坏，两处并存会报出来）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
