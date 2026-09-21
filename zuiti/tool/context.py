# -*- coding: utf-8 -*-
"""
context.py —— 悬浮窗只读本地记忆库 context.db，按分层策略拼上下文。

分层：
  关系卡（永久）  +  更早摘要（若有）  +  近期原文（默认最近 1 个月、封顶 N 条）
选不到对话（通用）时返回空串，改写退回无上下文。
"""
import os
import re
import sys
import time
import sqlite3
import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
# 演示模式：设置环境变量 ZUITI_DB 可切到独立演示库（真库不上台）
DB = os.environ.get("ZUITI_DB") or os.path.join(BASE, "context.db")

LAST_ERROR = ""    # 最近一次读库失败原因（原先是静默吞掉，排障抓瞎）


def _err(where, e):
    global LAST_ERROR
    LAST_ERROR = f"{where}: {e}"
    print(f"[context] {where} 失败: {e}", file=sys.stderr)

RECENT_DAYS = 30      # 强记忆窗口：最近 1 个月原文
RECENT_CAP = 25       # 近期原文封顶条数，防爆上下文

STYLE_POOL = 400      # 风格统计窗口：本人最近 N 条
SAMPLE_CAP = 5        # 原话样本条数
STYLE_TTL = 300       # 风格卡缓存秒数
_emoji_re = re.compile(r"[\U0001F000-\U0001FAFF☀-➿️️]")
_style_cache = {"ts": 0.0, "key": None, "card": ""}


def _conn():
    return sqlite3.connect(DB)


def available():
    """库是否存在且有会话。"""
    if not os.path.exists(DB):
        return False
    try:
        c = _conn()
        n = c.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        c.close()
        return n > 0
    except Exception as e:
        _err("available", e)
        return False


def db_stats():
    """库概况（会话/消息/关系卡/摘要条数 + 每会话计数），状态栏和自检都用它。
    表缺失记 -1，打不开记 LAST_ERROR。"""
    out = {"db": DB, "exists": os.path.exists(DB), "convs": 0, "msgs": 0,
           "cards": 0, "summaries": 0, "per_conv": []}
    if not out["exists"]:
        return out
    try:
        c = _conn()
        for key, tbl in (("convs", "conversations"), ("msgs", "messages"),
                         ("cards", "relationship_cards"), ("summaries", "summaries")):
            try:
                out[key] = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except Exception:
                out[key] = -1                    # 老库没这张表
        out["per_conv"] = c.execute(
            "SELECT conv_name, COUNT(*) FROM messages GROUP BY conv_name ORDER BY 2 DESC").fetchall()
        c.close()
    except Exception as e:
        _err("db_stats", e)
    return out


def list_conversations():
    """返回 [(name, recipient_tag), ...]，供下拉。"""
    if not os.path.exists(DB):
        return []
    try:
        c = _conn()
        rows = c.execute("SELECT name, recipient_tag FROM conversations ORDER BY name").fetchall()
        c.close()
        return rows
    except Exception as e:
        _err("list_conversations", e)
        return []


def tag_of(name):
    try:
        c = _conn()
        r = c.execute("SELECT recipient_tag FROM conversations WHERE name=?", (name,)).fetchone()
        c.close()
        return r[0] if r else "none"
    except Exception as e:
        _err("tag_of", e)
        return "none"


def build_context(conv_name, recent_days=RECENT_DAYS, cap=RECENT_CAP):
    """拼出给模型的上下文字符串；conv_name 为空/通用则返回 ''。
    三层：关系卡（人格分寸，chat-persona 落库）+ 更早性格概括（summaries 分层缓存）
        + 近期原文（现读，结合上下文）。"""
    if not conv_name or conv_name == "通用" or not os.path.exists(DB):
        return ""
    try:
        c = _conn()
        card = c.execute("SELECT card FROM relationship_cards WHERE conv_name=?", (conv_name,)).fetchone()
        summs = c.execute(
            "SELECT period_start, period_end, summary FROM summaries WHERE conv_name=? "
            "AND summary!='' ORDER BY period_start LIMIT 3",
            (conv_name,)).fetchall()
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=recent_days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = c.execute(
            "SELECT sender, text, ts FROM messages WHERE conv_name=? AND ts>=? ORDER BY ts DESC LIMIT ?",
            (conv_name, cutoff, cap)).fetchall()
        c.close()
    except Exception as e:
        _err(f"build_context({conv_name})", e)
        return ""

    parts = []
    if card and card[0].strip():
        parts.append("【关系卡·长期有效】" + card[0].strip())
    if summs:
        lines = [f"· {ps[:10]}~{pe[:10]}：{sm.strip()}" for ps, pe, sm in summs]
        parts.append("【更早性格概括（缓存，不必重读原文）】\n" + "\n".join(lines))
    if rows:
        lines = [f"{s}｜{t}：{x}" for s, x, t in reversed(rows)]
        parts.append("【近期对话（旧→新）】\n" + "\n".join(lines))
    return "\n\n".join(parts)


# ---------------- 自我卡：提炼用户本人的说话风格，让成稿“像你” ----------------

def detect_self():
    """从库里猜“哪个 sender 是用户本人”：优先取所有会话都出现的人，
    并列/交集为空则取发言最多者。config 的 my_name 永远优先于此。"""
    if not os.path.exists(DB):
        return ""
    try:
        c = _conn()
        convs = [r[0] for r in c.execute("SELECT name FROM conversations").fetchall()]
        sets = []
        for cv in convs:
            rows = c.execute("SELECT DISTINCT sender FROM messages WHERE conv_name=? AND sender!='?'",
                             (cv,)).fetchall()
            sets.append({r[0] for r in rows})
        inter = set.intersection(*sets) if sets else set()
        if len(inter) == 1:
            c.close()
            return inter.pop()
        row = c.execute(
            "SELECT sender FROM messages WHERE sender!='?' GROUP BY sender "
            "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        c.close()
        return row[0] if row else ""
    except Exception as e:
        _err("detect_self", e)
        return ""


def style_card(my_name=None, exclude=()):
    """统计 + 原话样本，拼一张“用户口吻卡”。样本太少/库缺失返回空串。带 TTL 缓存。"""
    key = (my_name or "", tuple(sorted(exclude)))
    now = time.time()
    if _style_cache["card"] and _style_cache["key"] == key and now - _style_cache["ts"] < STYLE_TTL:
        return _style_cache["card"]
    name = my_name or detect_self()
    if not name:
        return ""
    try:
        c = _conn()
        rows = c.execute("SELECT text FROM messages WHERE sender=? ORDER BY ts DESC LIMIT ?",
                         (name, STYLE_POOL)).fetchall()
        c.close()
    except Exception as e:
        _err("style_card", e)
        return ""
    texts = [t.strip() for (t,) in rows if t and t.strip()]
    if len(texts) < 8:
        return ""                      # 样本太少，拼不出像样的风格
    n = len(texts)
    joined = "\n".join(texts)
    avg = sum(len(t) for t in texts) / n
    feats = [f"平均一句{avg:.0f}字，" + ("句子偏短" if avg <= 18 else "句子偏长")]
    ni, nw, nz = joined.count("您"), joined.count("你"), joined.count("咱")
    if ni >= 3 and ni > nw:
        feats.append("习惯称“您”")
    elif nz >= 3:
        feats.append("爱用“咱”，平称“你”")
    else:
        feats.append("平称“你”，不称“您”")
    hits = [w for w in ("哈哈", "嗯", "吧", "呀", "嘛", "呢", "哦", "唉", "诶")
            if joined.count(w) >= max(3, n * 0.15)]
    if hits:
        feats.append("常用语气词：" + "、".join(hits[:4]))
    emoj = sum(1 for t in texts if _emoji_re.search(t)) / n
    if emoj < 0.08:
        feats.append("几乎不用emoji")
    elif emoj < 0.35:
        feats.append("偶尔用emoji")
    else:
        feats.append("常用emoji")
    if joined.count("~") + joined.count("～") >= max(3, n * 0.2):
        feats.append("爱用“~”收尾")
    bad = tuple(exclude)
    samples = []
    for t in texts:                    # 新→旧取干净样本：短句、无脏字、无链接
        if len(samples) >= SAMPLE_CAP:
            break
        if 4 <= len(t) <= 30 and "http" not in t and t not in samples and not any(w in t for w in bad):
            samples.append(t)
    card = "【用户的说话风格——成稿要像他本人发的】\n· " + "\n· ".join(feats)
    if samples:
        card += "\n他的原话样本：\n" + "\n".join("- " + x for x in samples)
    _style_cache.update({"ts": now, "key": key, "card": card})
    return card
