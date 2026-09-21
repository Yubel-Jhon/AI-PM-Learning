# -*- coding: utf-8 -*-
"""
build_context_db.py —— 由 QwenWork 侧维护本地「记忆库」context.db

职责（脚本自己调不动 dws，所以拉取由 QwenWork/定时任务完成，本脚本负责落库）：
  1. 读取 dumps/*.json（dws chat +chat-messages --output 导出的消息）
  2. 建 SQLite：conversations / messages(强记忆·近1月原文) / summaries(更早摘要) / relationship_cards(永久关系卡)
  3. 每个常用对话挂一张「关系卡」，不随时间衰减

重跑即幂等更新（INSERT OR IGNORE / REPLACE）。
"""
import json
import os
import sqlite3
import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "context.db")
DUMPS = os.path.join(BASE, "dumps")

# 常用对话清单：name / type / 默认对方标签 / 对应导出文件 / 关系卡种子
CONVS = [
    {
        "name": "晓天睿士答疑交流2群", "type": "group", "tag": "group", "file": "dump_xiaotian.json",
        "card": "群聊：晓天睿士答疑交流2群。你是答疑/项目侧（斯蓝等），常有人@问新项目、考试入项等。"
                "对外口径统一、别承诺没定的项目与名额。",
    },
    {
        "name": "蚂上有创意咨询群", "type": "group", "tag": "group", "file": "dump_mayou.json",
        "card": "群聊：蚂上有创意（AI创意平台）咨询群。运营/客服场景，常问体验名额、API、版本升级。"
                "热情但克制，不透露未公布的安排与时间。",
    },
    {
        "name": "胡俊友", "type": "direct", "tag": "client", "file": "dump_hujunyou.json",
        "card": "胡俊友（胡老师），招聘/HR，正在帮【你=大宇】推进面试（德科-IT采购/COE采购岗，钟于泊）。"
                "关系：他帮你安排、你配合；沟通简短直接，涉及时间/进度拿不准就说'我确认后答复你'。",
    },
]


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations(
            name TEXT PRIMARY KEY, type TEXT, recipient_tag TEXT,
            conv_id TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT, conv_name TEXT, msg_id TEXT,
            sender TEXT, text TEXT, ts TEXT, UNIQUE(conv_name, msg_id));
        CREATE TABLE IF NOT EXISTS summaries(
            conv_name TEXT, period_start TEXT, period_end TEXT, summary TEXT,
            updated_at TEXT, PRIMARY KEY(conv_name, period_start));
        CREATE TABLE IF NOT EXISTS relationship_cards(
            conv_name TEXT PRIMARY KEY, card TEXT, updated_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_msg ON messages(conv_name, ts);
        """
    )


def load_msgs(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return d.get("messages") or d.get("data", {}).get("messages") or []


def build():
    conn = sqlite3.connect(DB)
    init(conn)
    total = 0
    for c in CONVS:
        p = os.path.join(DUMPS, c["file"])
        if not os.path.exists(p):
            print(f"[skip] 缺导出文件 {p}")
            continue
        msgs = load_msgs(p)
        conv_id = msgs[0]["conversationId"] if msgs else ""
        conn.execute(
            "INSERT OR REPLACE INTO conversations(name,type,recipient_tag,conv_id,updated_at) VALUES(?,?,?,?,?)",
            (c["name"], c["type"], c["tag"], conv_id, now()),
        )
        n = 0
        for m in msgs:
            txt = (m.get("text") or "").strip()
            if not txt:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO messages(conv_name,msg_id,sender,text,ts) VALUES(?,?,?,?,?)",
                (c["name"], m.get("messageId"), m.get("sender") or "?", txt, m.get("createTime") or ""),
            )
            n += cur.rowcount
        # 关系卡：只在没有时写种子，已有则不覆盖（用户可手改/模型回填）
        if not conn.execute("SELECT 1 FROM relationship_cards WHERE conv_name=?", (c["name"],)).fetchone():
            conn.execute("INSERT INTO relationship_cards(conv_name,card,updated_at) VALUES(?,?,?)",
                         (c["name"], c["card"], now()))
        total += n
        print(f"[ok] {c['name']}: 新入库 {n} 条，累计库内 "
              f"{conn.execute('SELECT COUNT(*) FROM messages WHERE conv_name=?',(c['name'],)).fetchone()[0]} 条")
    conn.commit()
    conn.close()
    print(f"完成：context.db 已就绪，共导入 {total} 条消息。")


if __name__ == "__main__":
    build()
