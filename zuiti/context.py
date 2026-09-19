# -*- coding: utf-8 -*-
"""
context.py —— 悬浮窗只读本地记忆库 context.db，按分层策略拼上下文。

分层：
  关系卡（永久）  +  更早摘要（若有）  +  近期原文（默认最近 1 个月、封顶 N 条）
选不到对话（通用）时返回空串，改写退回无上下文。
"""
import os
import re
import time
import sqlite3
import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "context.db")

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
    except Exception:
        return False


def list_conversations():
    """返回 [(name, recipient_tag), ...]，供下拉。"""
    if not os.path.exists(DB):
        return []
    try:
        c = _conn()
        rows = c.execute("SELECT name, recipient_tag FROM conversations ORDER BY name").fetchall()
        c.close()
        return rows
    except Exception:
        return []


def tag_of(name):
    try:
        c = _conn()
        r = c.execute("SELECT recipient_tag FROM conversations WHERE name=?", (name,)).fetchone()
        c.close()
        return r[0] if r else "none"
    except Exception:
        return "none"


def build_context(conv_name, recent_days=RECENT_DAYS, cap=RECENT_CAP):
    """拼出给模型的上下文字符串；conv_name 为空/通用则返回 ''。"""
    if not conv_name or conv_name == "通用" or not os.path.exists(DB):
        return ""
    try:
        c = _conn()
        card = c.execute("SELECT card FROM relationship_cards WHERE conv_name=?", (conv_name,)).fetchone()
        summ = c.execute(
            "SELECT summary FROM summaries WHERE conv_name=? ORDER BY period_end DESC LIMIT 1",
            (conv_name,)).fetchone()
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=recent_days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = c.execute(
            "SELECT sender, text, ts FROM messages WHERE conv_name=? AND ts>=? ORDER BY ts DESC LIMIT ?",
            (conv_name, cutoff, cap)).fetchall()
        c.close()
    except Exception:
        return ""

    parts = []
    if card and card[0].strip():
        parts.append("【关系卡·长期有效】" + card[0].strip())
    if summ and summ[0].strip():
        parts.append("【更早对话摘要】" + summ[0].strip())
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
    except Exception:
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
    except Exception:
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
