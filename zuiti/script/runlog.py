# -*- coding: utf-8 -*-
"""runlog.py —— 嘴替可观测性：运行日志与指标聚合（纯标准库，悬浮窗与自检共用）。

设计红线（与 zuiti-evolve 同款）：
  只落数字、档位、违规类别码、引擎、耗时——原话、成稿、对方是谁、窗口标题一个字不落。
  文件在本地 self-improvement/runs.jsonl，不进任何仓库。

两类事件（一行一条 JSON）：
  attempt  一次改写尝试：引擎/耗时/档位/违规码/结局（缓存命中也是一次尝试）
  action   一次拍板：发出/手改后发/自动发出/放弃/没抓到草稿/草稿过长/没改出内容
一次 Alt+Z = 一个 run 号；预览窗里"换一版/换档"共用 run，只加 seq——
按 run 字段把行挑出来按 seq 排，就是这一次改写的完整链路。
"""

import json
import os
import time

VERSION = "v3.2"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OBS_DIR = os.path.join(BASE_DIR, "self-improvement")
RUNLOG_PATH = os.path.join(OBS_DIR, "runs.jsonl")

# 质检文案 → 类别码。日志只落码：文案可能带草稿里的数字/时间词，码不会。
_VIOL_CODES = [
    ("出现客服腔", "banned"), ("残留脏字", "curse"), ("带了说明性文字", "meta"),
    ("emoji超量", "emoji"), ("太短", "too_short"), ("超过50字", "too_long"),
    ("拒绝，成稿变", "refuse_flip"), ("答应，成稿变", "agree_flip"),
    ("原话没有的数字", "new_number"), ("时间口径变了", "time_drift"),
    ("时间段变了", "part_drift"), ("输出为空", "empty"),
]

_ENGINE_LABEL = {"api": "api", "ollama": "local", "fallback_rule": "fallback", "cache": "cache"}


def viol_codes(reasons):
    """质检违规文案 → 类别码（认不出的落 other，别丢）。"""
    codes = []
    for r in reasons or ():
        for mark, code in _VIOL_CODES:
            if mark in r:
                codes.append(code)
                break
        else:
            codes.append("other")
    return codes


def append(rec):
    """追加一条记录。观测层坏了不许拖垮改写主流程，任何异常都吞掉。"""
    try:
        os.makedirs(OBS_DIR, exist_ok=True)
        rec = dict(rec)
        rec.setdefault("type", "attempt")
        rec.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        rec.setdefault("ver", VERSION)
        with open(RUNLOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def stats(days=7):
    """聚合最近 N 天。返回 None = 还没有任何记录。"""
    cut = time.time() - days * 86400
    attempts, actions = [], []
    try:
        with open(RUNLOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                try:
                    t = time.mktime(time.strptime(rec.get("ts", ""), "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    continue
                if t < cut:
                    continue
                (actions if rec.get("type") == "action" else attempts).append(rec)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except Exception:
        return None
    if not attempts and not actions:
        return None

    engines, ms, viol = {}, [], {}
    outcomes = {}
    for a in attempts:
        eng = _ENGINE_LABEL.get(a.get("engine"), a.get("engine", "?"))
        engines[eng] = engines.get(eng, 0) + 1
        if a.get("ms") is not None:
            ms.append(a["ms"])
        for c in a.get("viol") or []:
            viol[c] = viol.get(c, 0) + 1
        outcomes[a.get("outcome", "?")] = outcomes.get(a.get("outcome", "?"), 0) + 1

    act = {}
    for x in actions:
        act[x.get("action", "?")] = act.get(x.get("action", "?"), 0) + 1
    sent = act.get("sent", 0) + act.get("edited_sent", 0)
    decided = sent + act.get("cancelled", 0)
    slow = [m for m in ms if m > 5000]

    s = {"days": days, "attempts": len(attempts), "sessions": len(actions),
         "engines": engines, "avg_ms": int(sum(ms) / len(ms)) if ms else None,
         "max_ms": max(ms) if ms else None,
         "fallback": engines.get("fallback", 0), "act": act,
         "sent": sent, "cancelled": act.get("cancelled", 0),
         "edited": act.get("edited_sent", 0),
         "accept": round(sent / decided * 100) if decided else None,
         "edit_rate": round(act.get("edited_sent", 0) / sent * 100) if sent else None,
         "top_viol": sorted(viol.items(), key=lambda kv: -kv[1])[:3],
         "top_outcome": sorted(outcomes.items(), key=lambda kv: -kv[1]),
         "slow": len(slow)}
    return s


def format_stats(s, indent=""):
    """聚合结果 → 人话文本。GBK 控制台安全（无 emoji、无生僻符号）。"""
    if not s:
        return ""
    L = []
    days = s["days"]
    L.append(f"最近 {days} 天 · {s['attempts']} 次改写（{s['sessions']} 次拍板）")
    if s["engines"]:
        eng = " · ".join(f"{k} {v}" for k, v in sorted(s["engines"].items(), key=lambda kv: -kv[1]))
        n = sum(s["engines"].values())
        fb = round(s["fallback"] / n * 100) if n else 0
        L.append(f"引擎   {eng}" + (f"（兜底率 {fb}%）" if fb else ""))
    if s["avg_ms"] is not None:
        L.append(f"耗时   平均 {s['avg_ms'] / 1000:.1f}s · 最慢 {s['max_ms'] / 1000:.1f}s"
                 + (f" · 超5s {s['slow']} 次" if s["slow"] else ""))
    act = s["act"]
    parts = []
    if s["sent"]:
        parts.append(f"发出 {s['sent']}" + (f"（其中手改 {s['edited']}）" if s["edited"] else ""))
    if s["cancelled"]:
        parts.append(f"放弃 {s['cancelled']}")
    if act.get("no_draft"):
        parts.append(f"没抓到草稿 {act['no_draft']}")
    if act.get("draft_too_long"):
        parts.append(f"草稿过长 {act['draft_too_long']}")
    if act.get("no_output"):
        parts.append(f"没改出内容 {act['no_output']}")
    if act.get("auto_sent"):
        parts.append(f"自动发出 {act['auto_sent']}")
    L.append("拍板   " + (" · ".join(parts) if parts else "（无）"))
    if s["accept"] is not None:
        er = f" · 手改率 {s['edit_rate']}%" if s["edit_rate"] is not None else ""
        L.append(f"采纳率 {s['accept']}%（发出/拍板）{er}")
    if s["top_viol"]:
        L.append("高频违规 " + " · ".join(f"{c} x{n}" for c, n in s["top_viol"]))
    tips = []
    n = sum(s["engines"].values()) if s["engines"] else 0
    if n and s["fallback"] / n > 0.10:
        tips.append("兜底率>10%：查引擎（网络/超时/模型名），规则兜底是保命不是常态")
    if s["avg_ms"] and s["avg_ms"] > 5000:
        tips.append("平均>5s：qwen3 思考坑复发或该换 qwen-plus（SKILL.md 悬浮窗引擎节）")
    if s["accept"] is not None and s["accept"] < 70 and s["sent"] + s["cancelled"] >= 5:
        tips.append("采纳率<70%：该场景的示例/分寸优先复盘（L2）")
    if s["edit_rate"] is not None and s["edit_rate"] > 20 and s["sent"] >= 5:
        tips.append("手改率高：规则盲区在扩大，复盘时对照 viol 码找规律")
    if tips:
        L.append("提示   " + "；".join(tips))
    return "\n".join(indent + x for x in L)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    days = 7
    if "--days" in sys.argv:
        i = sys.argv.index("--days")
        if i + 1 < len(sys.argv):
            try:
                days = int(sys.argv[i + 1])
            except ValueError:
                pass
    s = stats(days)
    print(format_stats(s) or f"最近 {days} 天没有运行记录（{RUNLOG_PATH}）——改写一次就有了")
