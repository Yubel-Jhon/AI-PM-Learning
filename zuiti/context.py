# -*- coding: utf-8 -*-
"""
context.py —— 悬浮窗只读本地记忆库 context.db，按分层策略拼上下文。

分层：
  关系卡（永久）  +  更早摘要（若有）  +  近期原文（默认最近 1 个月、封顶 N 条）
选不到对话（通用）时返回空串，改写退回无上下文。
"""
import os
import sqlite3
import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "context.db")

RECENT_DAYS = 30      # 强记忆窗口：最近 1 个月原文
RECENT_CAP = 25       # 近期原文封顶条数，防爆上下文


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
