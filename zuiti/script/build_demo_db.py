# -*- coding: utf-8 -*-
"""
build_demo_db.py —— 生成现场演示用的脱敏记忆库 demo_context.db

和真库（context.db，来自真实钉钉记录）完全分开：
  · 对象全是虚构人设（张总/王姐/项目推进群），上台不暴露任何真实人名群名
  · 每个对象一组"经典对话场景"作背景记忆，演示时注入改写引擎
  · sender=大宇 的消息同时喂自我卡（口吻模仿）
  · 全库交集恰好是"大宇"，detect_self 在演示库下也能自动猜对

用法： python build_demo_db.py   （重跑即重建，幂等）
演示启动：双击 启动嘴替-演示.bat（内部设 ZUITI_DB=demo_context.db）
"""
import os
import sqlite3
import datetime

from build_context_db import init

BASE = os.path.dirname(os.path.abspath(__file__))
DEMO_DB = os.path.join(BASE, "demo_context.db")

# 消息时间戳统一往后挪，保证落在"近 1 月强记忆"窗口内
def _ts(days_ago, hh, mm):
    d = datetime.datetime.now() - datetime.timedelta(days=days_ago)
    return d.strftime("%Y-%m-%d") + f" {hh:02d}:{mm:02d}:00"


# 三个虚构对象：人设卡 + 经典对话脚本（sender=大宇 的行会同时进自我卡）
SCENARIOS = [
    {
        "name": "张总", "type": "direct", "tag": "boss",
        "card": "张总，你的直属上级。对结果负责，说话短，常在晚上批方案、开会前要结论。"
                "跟他沟通：先给结论，再给两个选项让他挑，时间点说死，别解释过程。",
        "msgs": [
            ("张总", 6, 19, 42, "方案我看完了，方向不对，再想想"),
            ("大宇", 6, 19, 45, "好，我先梳理两个方向，明早十点前发出来对比着看"),
            ("张总", 6, 22, 13, "明天上午要跟客户过一遍，晚上辛苦一下"),
            ("大宇", 6, 22, 15, "行，我理完先发预览版，十一点前给到"),
            ("张总", 2, 9, 2, "这个报价是不是高了？客户那边不好交代"),
            ("大宇", 2, 9, 5, "我把三项成本拆开放大表格里了，看完咱再定给客户的口径"),
            ("张总", 2, 17, 30, "行，就这么定，下周给我排期"),
        ],
    },
    {
        "name": "王姐", "type": "direct", "tag": "peer",
        "card": "王姐，平级同事，坐你斜对面。嘴快心直，活多的时候爱往前推。"
                "跟她的分寸：话可以直，但责任边界每次都钉在文字上，别当面呛她，也别憋着吃暗亏。",
        "msgs": [
            ("王姐", 5, 10, 20, "这个需求当时不是你接的吗？怎么数据对不上"),
            ("大宇", 5, 10, 22, "我翻了下当时的确认记录，是按口头说的做的，没落到文字上"),
            ("王姐", 5, 10, 23, "那现在咋办，客户下午就要"),
            ("大宇", 5, 10, 25, "我先按新口径改一版给客户应急，改完咱俩把口径钉进文档里"),
            ("王姐", 1, 16, 40, "又是你签收的验收单？你咋什么活都接"),
            ("大宇", 1, 16, 42, "那单子是顺手帮前台代的，下回这种我直接转给你，哈哈"),
        ],
    },
    {
        "name": "项目推进群", "type": "group", "tag": "group",
        "card": "项目推进群（8 人），跨部门联调用。群口径要收敛：进度只报确定的时间点，"
                "锅不接也不甩，@到谁谁回，别在群里点名怼人。",
        "msgs": [
            ("陈工", 3, 11, 0, "@大宇 接口什么时候好？联调这边等着呢"),
            ("大宇", 3, 11, 4, "后天上午出第一版，参数文档我晚上补到群里"),
            ("老周", 3, 11, 5, "别后天了，今天必须给个能跑的"),
            ("大宇", 3, 11, 7, "今天给个半联调版可以，但只保证主链路，边缘问题别当天报"),
            ("陈工", 3, 15, 30, "收到，那明天上午我对一遍"),
        ],
    },
]


def build():
    if os.path.exists(DEMO_DB):
        os.remove(DEMO_DB)
    conn = sqlite3.connect(DEMO_DB)
    init(conn)
    total = 0
    for sc in SCENARIOS:
        conv_id = f"demo_{sc['tag']}"
        conn.execute(
            "INSERT OR REPLACE INTO conversations(name,type,recipient_tag,conv_id,updated_at) "
            "VALUES(?,?,?,?,?)",
            (sc["name"], sc["type"], sc["tag"], conv_id, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.execute("INSERT OR REPLACE INTO relationship_cards(conv_name,card,updated_at) VALUES(?,?,?)",
                     (sc["name"], sc["card"], datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        for i, (sender, days, hh, mm, text) in enumerate(sc["msgs"]):
            conn.execute(
                "INSERT OR REPLACE INTO messages(conv_name,msg_id,sender,text,ts) VALUES(?,?,?,?,?)",
                (sc["name"], f"{conv_id}_{i}", sender, text, _ts(days, hh, mm)))
            total += 1
    conn.commit()
    inter = None
    for sc in SCENARIOS:
        s = {r[0] for r in conn.execute("SELECT DISTINCT sender FROM messages WHERE conv_name=?",
                                        (sc["name"],)).fetchall()}
        inter = s if inter is None else (inter & s)
    conn.close()
    print(f"演示库已生成：demo_context.db，共 {total} 条消息；全库交集（=演示者本人）{inter}")


if __name__ == "__main__":
    build()
