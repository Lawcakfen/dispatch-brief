# -*- coding: utf-8 -*-
"""轻量判断：只给「有变动的项目」写两行判断

为什么单独一个文件：
    project_watch.py 是可发布的开源脚本（GitHub 上那个），不塞 LLM 调用。
    这个是本机用的薄壳，复用 project_watch.py 的 --only / --set 命令。

跟每天 06:20 那个 cron 的区别：
    cron 判全部 34 个项目（一天一次）
    这个只判「文件变动时间 > _进度总表.md 修改时间」的项目 —— 通常 0-3 个
    所以点一次刷新，成本几乎为零。

用法：
    python judge_lite.py              # 判断所有"有变动"的项目
    python judge_lite.py --dry        # 只列出要判哪些，不调 API
"""
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
PW = os.path.join(SCRIPTS, "project_watch.py")

# ★ 状态字段的合法取值 —— 和 project_watch.STAGES 保持一致。
#   为什么在这里再写一份：judge_lite 故意不 import project_watch（它要能独立跑，
#   而且 project_watch 是可开源的）。两份不一致会静默错，所以加了个自检（见下）。
STAGES_OK = ("未开始", "开发中", "待验收", "已完成", "暂停")
sys.path.insert(0, SCRIPTS)
import paths  # noqa: E402

ROOT = paths.PROJECTS_ROOT
LOG = os.path.join(ROOT, "_进度总表.md")

# ---- ★★★ 大模型配置（发布版：任何 OpenAI 兼容接口都能用）----
#   默认值 = DeepSeek，因为作者就是这么用的、验证过的。
#   想换别家：在 agent-config.json 里改这三项，不用动代码。
API_URL = paths.conf("judge_api_url", "https://api.deepseek.com/chat/completions")
MODEL = paths.conf("judge_model", "deepseek-chat")
KEY_ENV = paths.conf("judge_api_key_env", "DEEPSEEK_API_KEY")
KEY_IN_CFG = paths.conf("judge_api_key", "")      # 不推荐，但支持（方便试跑）

# key 可能在哪些 .env 文件里（按顺序找）：
#   ① 配置里显式指的 env_file
#   ② 老位置（Hermes 用户可能就是这里 —— 保持兼容）
#   ③ 脚本同目录的 .env（发布版最自然的位置）
_ENV_CANDS = [paths.conf("env_file", ""),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes", ".env"),
              os.path.join(SCRIPTS, ".env")]

DRY = "--dry" in sys.argv
ALL = "--all" in sys.argv


def reported_projects():
    """从最新的「## … 汇报」段里，取出这次真正有判断的项目（用真实目录名）"""
    if not os.path.exists(LOG):
        return []
    lines = open(LOG, encoding="utf-8").read().split("\n")
    idx = -1
    for i, l in enumerate(lines):
        if re.fullmatch(r"##\s+.*汇报\s*", l):
            idx = i
    if idx < 0:
        return []
    seg = []
    for j in range(idx + 1, len(lines)):
        if lines[j].startswith("## "):
            break
        seg.append(lines[j])

    # ★ 只认独立的「- 名字：…」行。
    #   不能拿整段做子串匹配 —— 报告里有「另有 18 个项目已停 21 天以上：A、B、C…」
    #   这种汇总行，会把 26 个名字全匹配出来，白花钱判一堆停摆项目。
    names = set()
    for l in seg:
        if not l.startswith("- ") or "：" not in l:
            continue
        n = l[2:].split("：", 1)[0].strip()
        n = re.sub(r"@\S+$", "", n).strip()
        if n:
            names.add(n)

    out = []
    for d in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p) or d.startswith("_"):
            continue
        base = re.sub(r"@\S+$", "", d).strip()
        if base in names:
            out.append((d, 0))
    return out


def load_env():
    """找 API key。顺序：环境变量 → agent-config.json → 几个 .env 文件。

    ★ 发布版要支持「换别家」：key 的环境变量名由 KEY_ENV 决定（默认 DEEPSEEK_API_KEY），
      所以在 agent-config.json 里把 judge_api_key_env 改成 OPENAI_API_KEY 就能用 OpenAI。
    """
    key = os.environ.get(KEY_ENV, "")
    if key:
        return key
    if KEY_IN_CFG:
        return KEY_IN_CFG
    for p in _ENV_CANDS:
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                for ln in f:
                    if ln.strip().startswith(KEY_ENV + "="):
                        v = ln.split("=", 1)[1].strip().strip('"').strip("'")
                        if v:
                            return v
        except Exception:
            continue
    return ""


def key_help():
    """没找到 key 时该说什么 —— 必须让人能照着做，而不是只说「没有 key」。"""
    return (
        "✗ 没找到大模型 API key（找的是 %s）。\n"
        "\n"
        "  三种放法，任选一种：\n"
        "    1) 环境变量：  %s=sk-xxxx\n"
        "    2) 配置文件：  在 agent-config.json 里加 \"judge_api_key\": \"sk-xxxx\"（不推荐，容易被 git 带上）\n"
        "    3) .env 文件： 在 scripts/ 目录下建 .env，写一行 %s=sk-xxxx\n"
        "\n"
        "  看过的位置（按顺序）：\n"
        "%s"
        "\n"
        "  想换别家模型（OpenAI / 通义 / 智谱 / 本地 ollama 都行，只要兼容 /chat/completions）：\n"
        "    在 agent-config.json 里配 judge_api_url / judge_model / judge_api_key_env\n"
        "    现在用的是：%s · %s\n"
        % (KEY_ENV, KEY_ENV, KEY_ENV,
           "\n".join("    · %s%s" % (p or "（未配置 env_file）",
                                      "（存在）" if p and os.path.isfile(p) else "")
                     for p in _ENV_CANDS),
           MODEL, API_URL))


def run(args, timeout=180):
    return subprocess.run(
        [sys.executable, PW] + args,
        cwd=SCRIPTS, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def changed_projects():
    """找出「项目最新文件时间 > _进度总表.md 修改时间」的项目"""
    if not os.path.exists(LOG):
        return []
    last_judge = os.path.getmtime(LOG)
    out = []
    for d in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p) or d.startswith("_"):
            continue
        latest = 0
        for dp, dirs, fs in os.walk(p):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in
                       ("node_modules", "dist", "build", "__pycache__", ".git")]
            for f in fs:
                if f == "_给WB.md" or f.endswith((".bak", ".tmp", ".log")):
                    continue
                try:
                    latest = max(latest, os.path.getmtime(os.path.join(dp, f)))
                except OSError:
                    pass
        if latest > last_judge:
            out.append((d, latest))
    out.sort(key=lambda x: -x[1])
    return out


SYSTEM = """你是一个项目进度判断助手。只根据我给的证据写判断，不许编造。

★ 只输出一个 JSON 对象，不要任何别的文字，不要 ``` 围栏：

{
  "stage": "开发中",
  "next": "一个具体动作（一句话）",
  "blocker": "无",
  "criterion": "怎么算做完（要可观测）",
  "now": "一句话说清具体进展或停滞，必须带证据里的具体事实"
}

【stage】只能从这五个里选一个原词，不许自创。
  ★ 按这个顺序逐条问，第一个「是」的就是答案 —— 不要所有项目都填「开发中」，
    那样这个字段等于没写：
    ① 证据里有「做完 / 自检通过 / 已交付 / 物料包齐了」，但**还没有人实机验收过**？→ 待验收
    ② 证据里有「不做了 / 冻结 / 归档 / 收手」？→ 暂停
    ③ 已经有人验收过、能用了（实机确认过 / 已经发出 / 已经成交）？→ 已完成
    ④ 最近有文件在改、还在做 → 开发中
    ⑤ 一个字都没动过、连文件都没有 → 未开始

  五个词的意思（不许混用）：
    未开始 = 还没动手
    开发中 = 正在改
    待验收 = **代码/产物已经齐了，就差人看一眼** ← 这个最容易漏填
    已完成 = 人已经验收过、能用了
    暂停   = 明确不做了（收手 / 冻结）—— 「暂时没动」不算暂停，那还是开发中

【next】按这个顺序：
  1. 证据里「队列待发 N 条」且 N > 0 → 必须指向队列里的第一条（说清是哪一条），
     不要另起新活，也不要建议队列里已经列过的事。
  2. 队列为空 → 建议「验收/确认/收尾」类动作。
  3. 证据写着「已收尾/已上线」→ 写「无」。
  ★ 如果证据里写着某件事「已完成但没打勾」→ 写「先确认打勾，不要重做」，绝对不能建议重做。

【blocker】只有明确的阻塞才填（等谁拍板、等外部审核、等对方回话），否则写「无」。
  ★「还没做」「还没开始」不是阻塞，不许写成阻塞。

【criterion】写「怎么算这件事做完」，必须可观测（能查文件 / 能跑命令 / 能看到数字）。
  想不出可观测的就写「无」，不许编。

【now】必须带证据里的具体事实；看不出做到哪一步就写「看不出做到哪一步」，不要靠文件名猜功能。

★ 特别提醒：证据里 WorkBuddy 的工作报告是按时间顺序排的小节标题，
  最新状态在**最后几个小节**里。不要只看前面就下结论。"""


def _parse_json_out(txt):
    """从模型输出里抠出 JSON。允许它加了 ``` 围栏或前后废话。"""
    t = (txt or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"```\s*$", "", t).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        # ★ 有大括号但没闭合 → 说清「不完整」，别说「没有 JSON」。
        #   两者对排查的意义完全不同：前者是模型截断了输出，后者是它根本没照格式写。
        if "{" in t:
            return None, "JSON 不完整（有 { 没有 }，可能被截断）：%s" % t[-80:]
        return None, "输出里没有 JSON：%s" % t[:120]
    try:
        d = json.loads(m.group(0))
    except Exception as e:
        return None, "JSON 解析失败：%s ｜ 原文：%s" % (e, t[:120])
    if not isinstance(d, dict):
        return None, "不是对象"
    return d, ""


def _validate_fields(d):
    """校验模型给的对象，返回 ((now, next, fields), err)。

    ★ 抽成独立函数是为了能单测 —— 原来这段埋在 judge_one 里，
      要测它就得真调一次 API，那种测试没人会跑。
    ★ 一条铁律：stage 不合法就整条拒绝。宁可这次不写状态，也不要写个假状态 ——
      假状态会被卡片当成事实显示，比没有更难发现。
    """
    if not isinstance(d, dict):
        return None, "不是对象"
    now_txt = (d.get("now") or "").strip()
    stage = (d.get("stage") or "").strip()
    nxt = (d.get("next") or "").strip()
    if not now_txt:
        return None, "缺 now 字段"
    if not nxt:
        return None, "缺 next 字段"
    if stage not in STAGES_OK:
        return None, "stage 不合法：%r（允许：%s）" % (stage, " / ".join(STAGES_OK))
    return (now_txt, nxt, {
        "stage": stage,
        "next": nxt,
        "blocker": (d.get("blocker") or "无").strip() or "无",
        "criterion": (d.get("criterion") or "无").strip() or "无",
        "note": "",
    }), ""


def judge_one(name, evidence):
    key = load_env()
    if not key:
        return None, key_help()

    # ★★ 2026-11-20 修：原来是 evidence[:6000] —— 修好 condense 之后证据包涨到
    #    18836 字符，「队列待发」和 memory 的后面几个小节全被这个 6000 砍掉，
    #    等于前面的修复白做。DeepSeek 上下文够大，提到 30000。
    ev = evidence
    if len(ev) > 30000:
        ev = ev[:30000] + "\n…（★ 证据包超 30000 字符已截断，后面还有内容没读到）"

    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "项目：" + name + "\n\n证据：\n" + ev},
        ],
        "temperature": 0.3,
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode("utf-8"))
        txt = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return None, "API 出错：%s" % e

    d, perr = _parse_json_out(txt)
    if d is None:
        return None, perr
    return _validate_fields(d)


def main():
    # ★ --force "项目名"：不管有没有变动，强制重判这一个（2026-11-20 加）
    #   为什么需要：手工改过 _进度总表.md 之后它的 mtime 会变新，
    #   于是「项目变动 > 总表 mtime」永远不成立，判断就卡在旧版本上。
    if "--force" in sys.argv:
        i = sys.argv.index("--force")
        names = [a for a in sys.argv[i + 1:] if not a.startswith("--")]
        if not names:
            print("[judge] --force 后面要跟项目目录名")
            return 1
        todo = []
        for n in names:
            p = os.path.join(ROOT, n)
            if os.path.isdir(p):
                todo.append((n, 0.0))     # 与 changed_projects() 的 (名字, mtime) 结构保持一致
            else:
                print("[judge] 找不到项目目录：%s" % n)
        if not todo:
            return 1
    else:
        todo = reported_projects() if ALL else changed_projects()

    if not todo:
        print("[judge] 没有需要重新判断的项目")
        return 0

    print("[judge] 需要判断 %d 个：%s" % (len(todo), "、".join(n for n, _ in todo)))
    if DRY:
        return 0

    ok = fail = 0
    for name, _mt in todo:
        ev = run(["--only", name])
        if ev.returncode != 0 or not (ev.stdout or "").strip():
            print("[judge] %s 拿不到证据，跳过" % name)
            fail += 1
            continue
        res, err = judge_one(name, ev.stdout)
        if not res:
            print("[judge] %s 判断失败：%s" % (name, err))
            fail += 1
            continue
        now_txt, advice, fields = res
        s = run(["--set", name, "--now", now_txt, "--advice", advice])
        out = (s.stdout or "").strip()
        if out.startswith("ERROR"):
            print("[judge] %s 写入失败：%s" % (name, out))
            fail += 1
            continue
        # ★ 状态字段（2026-11-21 · 设计 2）：散文写总表，字段写 pstate.json
        ps = run(["--pstate", name,
                  "--stage", fields["stage"],
                  "--next", fields["next"],
                  "--blocker", fields["blocker"],
                  "--criterion", fields["criterion"]])
        pso = (ps.stdout or "").strip()
        if pso.startswith("ERROR"):
            print("[judge] %s 判断已写，但状态字段没写：%s" % (name, pso))
        print("[judge] %s ✓ %s ｜ %s" % (name, fields["stage"], out))
        ok += 1

    print("[judge] 完成：成功 %d，失败 %d" % (ok, fail))
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
