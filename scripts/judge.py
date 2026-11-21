#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""judge.py · 交付判断（被 wb_watch.py 调用，也可单独跑）

★ 设计原则：不给「通过/不通过」的结论，只给三样事实 ——
   看起来完成了什么 / 你该自己核哪几点 / 下一步建议

为什么不做结论：
   2026-11-14 一天里，Hermes 人工核 WB 交付误报了三次（都是我自己的验证工具错）。
   自动化以后没人复核，误报会直接推给用户。
   所以这里只做「把该核的点列清楚」，结论留给他。
"""
import os
import re
import sys
import time
import shutil
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths                      # ★ 所有路径的唯一来源

# Hermes 可执行文件：先用 PATH 里的，找不到就按 HERMES_HOME 推
HERMES = shutil.which("hermes") or os.path.join(
    paths.HERMES_HOME, "hermes-agent", "venv", "Scripts", "hermes.EXE")


def hermes_missing_help():
    """这个重版本靠 hermes 命令行起一次完整 session。

    ★ 发布版必须说清楚：没装 Hermes 的人用不了它 —— 但【不影响其它一切】，
      普通用户用 judge_lite.py 就够了（那个只要一个 API key）。
      原来这里没有这层判断，没装 Hermes 的人会看到一句「（判断失败）」，不知道为什么。
    """
    return (
        "✗ 没找到 hermes 命令行，judge.py 跑不了。\n"
        "\n"
        "  judge.py 是「重版本」：它起一次完整的 Hermes session 来做判断，\n"
        "  所以只在装了 Hermes Agent 的机器上能用。\n"
        "\n"
        "  找过的位置：\n"
        "    · PATH 里的 hermes\n"
        "    · %s\n"
        "\n"
        "  ★ 你大概率不需要它：\n"
        "    · 要判断 → 用 judge_lite.py（只要一个 API key，任何 OpenAI 兼容接口都行）\n"
        "    · 要数据 → project_watch.py / scan_projects.py 不依赖 Hermes\n"
        "  这个文件留在包里，是给「已经装了 Hermes 的人」用的。\n" % HERMES)
TIMEOUT = 200          # 秒
MAX_FILES = 40         # 列出多少改动文件
MAX_MEM = 4000         # memory 截断字符
MAX_INSTR = 2500       # 指令截断字符


def scan_recent(proj_dir, hours=24):
    """项目目录里过去 N 小时被改动的文件（排除 memory / 依赖 / 缓存）
    ★ 返回绝对路径 —— judge 子进程要靠它去读内容，只给相对路径它找不到。"""
    cutoff = time.time() - hours * 3600
    out = []
    skip = {".workbuddy", ".agent", ".codex", ".claude",
            "node_modules", ".git", "__pycache__", "Temp", "dist", "build"}
    for root, dirs, files in os.walk(proj_dir):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            if f.startswith(".") or f.endswith((".pyc", ".log")):
                continue
            fp = os.path.join(root, f)
            try:
                m = os.path.getmtime(fp)
            except OSError:
                continue
            if m > cutoff:
                rel = os.path.relpath(fp, proj_dir)
                size = os.path.getsize(fp)
                out.append((m, rel, size, os.path.abspath(fp)))
    out.sort(reverse=True)
    return out[:MAX_FILES]


def build_prompt(proj, done_items, mem_text, files, proj_dir=None):
    instr = "\n\n---\n\n".join(
        d.get("full") or d.get("text", "") for d in done_items)[:MAX_INSTR]
    mem = (mem_text or "（没有新增 memory）")[:MAX_MEM]
    if files:
        flist = "\n".join(
            f"· {rel}（{size:,} bytes，改于 {time.strftime('%m-%d %H:%M', time.localtime(m))}）\n"
            f"    {ap}"
            for m, rel, size, ap in files)
    else:
        flist = "（24 小时内没有任何文件被改动）"
    prow = f"\n## 项目绝对路径\n{proj_dir}\n" if proj_dir else ""

    return f"""你是 Hermes 的验收助手。WorkBuddy（一个执行型 AI）刚在项目「{proj}」里报告完成了一条任务。

你要独立判断，**不采信它的自报**。
{prow}
## 一、任务指令原文
{instr}

## 二、WB 的 memory 新增内容
{mem}

## 三、该项目过去 24 小时被改动的文件（★ 带绝对路径，你可以直接读）
{flist}

## 你可以用的工具
你有 file 和 terminal 工具。**请真的去读关键文件、真的跑一遍验证命令** —— 不要只靠文件大小和时间戳猜。
至少做这三件事（按项目性质挑最相关的）：
1. 读 memory 里声称改动过的那个文件，确认改动真的在里面
2. 如果项目有生成脚本，跑一遍看退出码和输出
3. 如果项目有产出的 html / md，读它的关键段落确认内容

## 你要输出的三段（总长不超过 500 字，中文）

【看起来完成了什么】
基于你**实际读到的内容**，用你自己的话说清它动了什么。不要照抄 memory 的措辞。
如果文件内容和 memory 说的对不上，直接指出来。

【你该自己核这几点】
列 2–4 条**具体可执行**的核对动作。要具体到查哪个文件、看哪个字段、跑哪个命令。
不要写「检查是否正常」这种没法执行的话。

【下一步建议】
一句话。如果队列还有下一条，说明是哪一条；如果这是最后一条，说明这个项目现在缺什么才能继续。

## 硬约束
- 不许编造文件内容 —— 没读到就写「没读到」，不要猜
- 读不到文件时，明确说「我没能读到 X」，不要用时间戳代替事实
- 不确定的地方就写「不确定」
- **不要给「通过 / 不通过」的结论** —— 结论归用户
- 不要客套话、不要开场白，直接从【看起来完成了什么】开始
"""


def judge(proj, done_items, mem_text, files, verbose=False, proj_dir=None):
    """返回判断文本；失败返回 None（调用方降级处理）"""
    if not HERMES or not os.path.isfile(HERMES):
        return None
    prompt = build_prompt(proj, done_items, mem_text, files, proj_dir=proj_dir)
    try:
        t0 = time.time()
        r = subprocess.run([HERMES, "-z", prompt, "-t", "file,terminal"],
                           capture_output=True, text=True, timeout=TIMEOUT,
                           encoding="utf-8", errors="replace")
        if verbose:
            print(f"（judge 耗时 {time.time()-t0:.1f}s，返回码 {r.returncode}）", file=sys.stderr)
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            if verbose and r.stderr:
                print("stderr:", r.stderr[:300], file=sys.stderr)
            return None
        return out
    except subprocess.TimeoutExpired:
        if verbose:
            print(f"（judge 超时 {TIMEOUT}s）", file=sys.stderr)
        return None
    except Exception as e:
        if verbose:
            print(f"（judge 异常 {e}）", file=sys.stderr)
        return None


if __name__ == "__main__":
    # 单跑测试：python judge.py "项目名"
    if not HERMES or not os.path.isfile(HERMES):
        print(hermes_missing_help())
        sys.exit(2)
    if len(sys.argv) < 2:
        print("用法: python judge.py <项目目录名>")
        sys.exit(1)
    proj = sys.argv[1]
    root = paths.PROJECTS_ROOT
    pd = os.path.join(root, proj)
    if not os.path.isdir(pd):
        print("项目不存在:", pd)
        sys.exit(1)
    files = scan_recent(pd)
    print(f"过去 24 小时改动 {len(files)} 个文件", file=sys.stderr)
    t = judge(proj, [{"text": "（单跑测试，无指令）"}],
              "（单跑测试，无 memory）", files, verbose=True, proj_dir=pd)
    print(t or "（判断失败）")
