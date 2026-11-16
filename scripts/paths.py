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


def _looks_like_projects(p):
    """判断一个目录是不是「项目根」：它下面某个子目录里有队列文件。"""
    try:
        for d in os.listdir(p):
            sub = os.path.join(p, d)
            if not os.path.isdir(sub):
                continue
            for q in ("_给WB.md", "_给执行Agent.md", "_给wb.md", "_给Codex.md"):
                if os.path.isfile(os.path.join(sub, q)):
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
    return os.path.join(os.getcwd(), "projects")


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
