"""所有脚本的唯一配置来源 —— 换设备只改 agent-config.json，不碰任何 .py。

配置文件优先级（从高到低）
  1. 环境变量           HERMES_HOME / PROJECTS_ROOT（只覆盖这两项）
  2. agent-config.json  放在本文件同目录
  3. 自动探测
  4. 兜底

★ 为什么要有这个文件（2026-11-16）
  原来 4 个脚本里散着 9 处写死的绝对路径 + 9 处客户/项目名。
  换一台设备直接跑不起来，而且客户名不能发布。
  集中到一个 json 之后：
      复制 agent-config.example.json 为 agent-config.json
      改两三个值
  不用碰任何 .py，也不会把别人的客户名带出去。
"""
import os
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG_FILE = os.path.join(_HERE, "agent-config.json")


def _read_cfg():
    """读 agent-config.json。读不到就当空配置，不报错 —— 缺配置时走探测。"""
    try:
        with open(_CFG_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _expand(p):
    """支持 ~ 和 %VAR% / $VAR —— 让用户写的路径不用管平台差异。"""
    return os.path.expandvars(os.path.expanduser(str(p)))


# ★★★ 队列文件名的【唯一来源】（2026-11-22 收口）
#   原来三处各写一份、而且不一致：
#     paths.py          4 个（含 _给Codex.md）
#     project_watch.py  3 个（没有 Codex）
#     wb_watch.py       借用 project_watch 的那份
#   后果：用 _给Codex.md 的人能通过「这是不是项目」的探测，
#         但 wb_watch 读队列时只认 _给WB.md → 静默什么都不发生。
#   ★ 顺序有意义：排在前面的优先（第一个存在的就用它）。
QUEUE_ALIASES = ("_给WB.md", "_给执行Agent.md", "_给wb.md", "_给Codex.md")


def find_queue(proj_dir):
    """在一个项目目录里找队列文件；按 QUEUE_ALIASES 顺序取第一个存在的。

    找不到时返回默认名（调用方自己判断存不存在），这样报错里能给出一个具体文件名。
    """
    for n in QUEUE_ALIASES:
        p = os.path.join(proj_dir, n)
        if os.path.isfile(p):
            return p
    return os.path.join(proj_dir, QUEUE_ALIASES[0])


# ★★★ 执行方日志目录：约定 .workbuddy/memory（2026-11-22 晚更正，按本机原有规则）
#   ★ 背景：11-22 白天一度把对外约定改成 .agent/memory，理由是「名字不该绑某个产品」。
#     但作者本机的实际工作流 —— 原始指令文档、Vault 规则、21 个项目 / 148 个日志 ——
#     全部是 .workbuddy/memory。两套写法并存会让执行方不知道往哪写，
#     或者同一个项目里出现两个日志目录（一半日志读不到）。
#   → 结论：以 .workbuddy/memory 为准；.agent/memory 降级为【兜底】，
#     只服务「按白天那版说明建过目录」的情况。
#   ★ 约定可以换，兜底不能删；每个项目只用一处，冲突由 memory_dir_others 报出来。
MEMORY_CANDIDATES = (".workbuddy/memory", ".agent/memory", ".codex/memory",
                     ".claude/memory", "memory")


def _memory_cfg():
    """配置里显式指定的日志子目录（相对项目根），没配返回空串。"""
    return (_cfg.get("memory_dir") or "").strip().replace("\\", "/")


def memory_dir(proj_dir):
    """这个项目的「执行方日志目录」在哪：配置 > 候选顺序里第一个存在的 > 默认第一个。"""
    cfg = _memory_cfg()
    if cfg:
        return os.path.join(proj_dir, *cfg.split("/"))
    for rel in MEMORY_CANDIDATES:
        p = os.path.join(proj_dir, *rel.split("/"))
        if os.path.isdir(p):
            return p
    return os.path.join(proj_dir, *MEMORY_CANDIDATES[0].split("/"))


def memory_dir_others(proj_dir):
    """同一项目里【另外也存在】的日志目录 —— 用来提示「有两处，只用了一处」。

    ★ 什么情况下会两处都有：从早期版本升上来的人（早期约定是 .workbuddy/memory），
      新项目又按 .agent/memory 建了 → 同一个项目里两处都在。
      ★ 更正：之前这里写「WorkBuddy 产品自己会往 .workbuddy/memory 写」—— 那是错的。
        那个目录是我们自己的指令让它写的，不是产品默认行为。所以新用户不会碰上两处。
      只读一处会静默丢一半日志，所以要把「另一处被忽略」说出来。
    """
    cfg = _memory_cfg()
    if cfg:
        return []
    chosen = os.path.normpath(memory_dir(proj_dir))
    return [os.path.join(proj_dir, *rel.split("/")) for rel in MEMORY_CANDIDATES
            if os.path.isdir(os.path.join(proj_dir, *rel.split("/")))
            and os.path.normpath(os.path.join(proj_dir, *rel.split("/"))) != chosen]


def conf(key, default=""):
    """读 agent-config.json 里的任意字段（没配 / 空串 → 返回默认值）。

    ★ 给「需要用户自己填的东西」用的通用读法：模型名、API 地址、key 所在的环境变量名。
      配了就用配的，没配就用默认 —— 这样默认能开箱跑，又不锁死在某一家。
    """
    v = _cfg.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    return v


def _looks_like_projects(p):
    """判断一个目录是不是「项目根」：它下面某个子目录里有队列文件。"""
    try:
        for d in os.listdir(p):
            sub = os.path.join(p, d)
            if not os.path.isdir(sub):
                continue
            if os.path.isfile(find_queue(sub)):
                return True
    except Exception:
        pass
    return False


def _detect_hermes_home():
    la = os.environ.get("LOCALAPPDATA")          # Windows
    if la:
        p = os.path.join(la, "hermes")
        if os.path.isdir(p):
            return p
    home = os.path.expanduser("~")
    for cand in (os.path.join(home, ".hermes"), os.path.join(home, "hermes")):
        if os.path.isdir(cand):
            return cand
    return os.path.join(home, ".hermes")


def _detect_projects_root():
    """从当前目录往上找，最多 4 层，看哪一层下面有「带队列文件的项目」。"""
    for b in (os.getcwd(), _HERE):
        cur = b
        for _ in range(4):
            for cand in (cur, os.path.join(cur, "projects")):
                if os.path.isdir(cand) and _looks_like_projects(cand):
                    return cand
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    # ★★★ 找不到就【返回空】，绝不瞎猜一个路径（2026-11-21 修）
    #   原来这里是 return os.path.join(os.getcwd(), "projects") ——
    #   那个目录几乎永远不存在，于是下游会报「目录不存在: C:\...\scripts\projects」，
    #   读者以为是自己目录结构不对，其实是压根没填 projects_root。
    #   返回空之后，下游才能说清「你还没配」，而不是让它去猜。
    return ""


_cfg = _read_cfg()

HERMES_HOME = _expand(
    os.environ.get("HERMES_HOME") or _cfg.get("hermes_home") or _detect_hermes_home()
)
PROJECTS_ROOT = _expand(
    os.environ.get("PROJECTS_ROOT") or _cfg.get("projects_root") or _detect_projects_root()
)

CACHE_DIR = os.path.join(HERMES_HOME, "cache")
OUTPUT_DIR = os.path.join(HERMES_HOME, "cron", "output")
SCRIPTS_DIR = _HERE

# ---- 项目分类（原来写死在 project_watch.py 里，含客户名，不能发布）----
FOCUS = _cfg.get("focus_projects") or []
CLIENTS = _cfg.get("client_projects") or []
PAUSED_PROJECTS = _cfg.get("paused_projects") or {}


def describe():
    """打印当前生效的配置 —— 排查「为什么它什么都没发现」时先跑这个。"""
    src_h = "环境变量" if os.environ.get("HERMES_HOME") else (
        "agent-config.json" if _cfg.get("hermes_home") else "自动探测")
    src_p = "环境变量" if os.environ.get("PROJECTS_ROOT") else (
        "agent-config.json" if _cfg.get("projects_root") else "自动探测")

    ok_cfg = os.path.isfile(_CFG_FILE)
    ok_root = os.path.isdir(PROJECTS_ROOT)
    ok_cache = os.path.isdir(CACHE_DIR)

    lines = [
        f"HERMES_HOME     {HERMES_HOME}",
        f"                （来源：{src_h}）",
        f"PROJECTS_ROOT   {PROJECTS_ROOT}",
        f"                （来源：{src_p}）",
        f"配置文件        {_CFG_FILE}",
        f"                {'（存在）' if ok_cfg else '（不存在）'}",
        f"项目根可用      {'是' if ok_root else '否 ← 这个必须修'}",
        f"缓存目录        {'已存在' if ok_cache else '还没建（首次运行会自动创建，不用管）'}",
        f"重点项目数      {len(FOCUS)}",
        f"客户项目数      {len(CLIENTS)}",
        f"已收尾项目数    {len(PAUSED_PROJECTS)}",
    ]

    if not ok_cfg or not ok_root:
        lines += [
            "",
            "─" * 52,
            "★ 还不能用，按这两步配完再跑：",
            "",
            f"  1) 复制配置模板",
            f"       copy agent-config.example.json agent-config.json",
            f"     （Mac/Linux 用 cp）",
            "",
            f"  2) 打开 agent-config.json，填 projects_root",
            f"     —— 你的项目根目录，它下面每个子文件夹是一个项目，",
            f"        每个项目里要有队列文件（如 _给WB.md）",
            "",
            f"     hermes_home 可以留空，自动探测；探测错了才填。",
            "",
            f"  3) 再跑一次：python paths.py",
            "     看到「项目根可用  是」就配好了。",
            "─" * 52,
        ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
