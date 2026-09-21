# -*- coding: utf-8 -*-
"""
persona_export.py —— chat-persona skill 的机械部分：从记忆库导出某人的语料 + 统计信号。

分析本身由 Claude 按 SKILL.md 做，本脚本只管"把这个人从库里拎出来"：
  · 该人出现的所有窗口（单聊 + 群里的发言）全部拉出
  · 近 N 月 = 细读语料（逐条）；窗口外 = 只给条数+抽样（按 SKILL.md 压成概括，不逐条分析）
  · stats.json：主动率/回复延迟/句长/emoji/时段 等客观信号

用法：
  python persona_export.py --list
  python persona_export.py 张三
  python persona_export.py 张三 --months 1 --db D:/path/context.db
  python persona_export.py 张三 --extra 微信导出.csv 更多聊天.txt   # 合并外部语料（微信导出/粘贴）
  python persona_export.py 张三 --set-mbti ENTJ              # 用户钦点 MBTI，之后所有卡都标"用户钦定"
  python persona_export.py 张三 --card-file 卡片浓缩版.md   # 回灌 relationship_cards，嘴替改写直接吃到

--extra 支持三种格式（按扩展名自动识别）：
  .json  [ {"sender":.., "text":.., "ts":..}, ... ]（ts 可省）
  .csv   表头自动认列：sender/name/nickname + text/content/StrContent + ts/time/StrTime
  .txt   每行一条：`[2025-01-02 10:00] 张三：xxx` / `张三：xxx` / 裸文本（视作张三本人说的）

输出：<db同目录>/persona_cards/<人名>/{corpus_recent.md, corpus_old.md, stats.json}
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import statistics
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # GBK 控制台遇 ⚠/emoji 直接崩，先转 UTF-8

EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿️️]")
PARTICLES = ["哈哈", "嗯", "吧", "呀", "嘛", "呢", "哦", "唉", "诶", "哈？"]
RECENT_CAP = 1500          # 近期语料逐条上限（超出取最新，标注截断）
OLD_SAMPLE = 20            # 窗口外抽样条数
GAP_HOURS = 4              # 超过这个间隔的新会话开头算"主动发起"

# ---- 语言地层学：网络流行语的"年代锚点" ----
# 原理：语言习惯在青春期定型，强锚点词使用峰只有 1~2 年、世代辨识度极高；
# 单词定位 ±5 岁，词汇组合 ±3 岁（计算社会语言学"语言地层学"思路）。
# 注意"形成期锚定"：中年人用年轻时的词（如当年的真诚"呵呵"），判读交给 SKILL.md 规则。
ERA_LAYERS = [
    ("70-80后", ["大虾", "灌水", "斑竹", "美眉", "青蛙", "886"]),
    ("85-95",   ["呵呵", "给力", "浮云", "神马", "伤不起", "有木有", "蓝瘦香菇"]),
    ("90-00",   ["yyds", "绝绝子", "awsl", "666", "skr", "扎心了", "硬核"]),
    ("00-05后", ["泰裤辣", "尊嘟假嘟", "city不city", "那咋了", "偷感", "班味", "栓Q", "集美"]),
]
# 微信中老年经典 bracket 表情组（[微笑][玫瑰]一族）——独立于文字层的代际信号
ELDER_BRACKETS = ["[微笑]", "[玫瑰]", "[强]", "[握手]", "[抱拳]", "[咖啡]", "[太阳]", "[爱心]"]


def _era_markers(mine_recent):
    """统计年代锚点词命中 → stats.json 的 era 字段（词级明细 + 地层聚合 + bracket 表情）。"""
    joined = "\n".join(mine_recent).lower()
    hints = {}
    for era, words in ERA_LAYERS:
        n = sum(joined.count(w.lower()) for w in words)
        if n:
            hints[era] = n
    markers = {w: joined.count(w.lower()) for _, words in ERA_LAYERS for w in words
               if joined.count(w.lower()) >= 1}
    brackets = {b: joined.count(b) for b in ELDER_BRACKETS if joined.count(b)}
    return {"era_hints": hints, "markers": markers, "bracket_emoji": brackets}


def q(c, sql, args=()):
    return c.execute(sql, args).fetchall()


MBTI_TYPES = {"INTJ", "INTP", "ENTJ", "ENTP", "INFJ", "INFP", "ENFJ", "ENFP",
              "ISTJ", "ISFJ", "ESTJ", "ESFJ", "ISTP", "ISFP", "ESTP", "ESFP"}


def override_path(out_root):
    return os.path.join(out_root, "_mbti_override.json")


def read_override(out_root):
    p = override_path(out_root)
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def set_mbti(person, mbti, out_root):
    """用户钦点 MBTI：写入 override 文件，之后的分析一律采用，不再自行推断。"""
    mbti = (mbti or "").strip().upper()
    if mbti not in MBTI_TYPES:
        print(f"[x] {mbti} 不是合法的 16 型（合法示例：INTJ/ENFP/…）");  return 1
    os.makedirs(out_root, exist_ok=True)
    ov = read_override(out_root)
    ov[person] = mbti
    json.dump(ov, open(override_path(out_root), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[ok] 已钦点 {person} = {mbti}（persona_cards/_mbti_override.json）。"
          f"之后出卡都标「用户钦定」，分析只写'为什么像'。")
    return 0


def find_windows(c, person):
    """该人出现的全部窗口：单聊（conv 名=人名）+ 群聊（sender 出现过）。"""
    direct = [r[0] for r in q(c, "SELECT name FROM conversations WHERE name=?", (person,))]
    groups = [r[0] for r in q(c,
        "SELECT DISTINCT conv_name FROM messages WHERE sender=? AND conv_name NOT IN "
        "(SELECT name FROM conversations WHERE name=?)", (person, person))]
    return direct, groups


def _norm_ts(ts):
    """外部时间戳 → %Y-%m-%d %H:%M:%S；认不出返回今天（能排到最新且不炸统计）。"""
    ts = str(ts).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            d = datetime.datetime.strptime(ts, fmt)
            return d.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_extra(path, person):
    """外部语料 → [(sender, text, ts), ...]。裸文本行视作 person 本人说的。"""
    ext = os.path.splitext(path)[1].lower()
    out = []
    if ext == ".json":
        data = json.load(open(path, encoding="utf-8"))
        for m in data:
            out.append((str(m.get("sender") or person), str(m.get("text") or m.get("content") or ""),
                        str(m.get("ts") or m.get("time") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))))
    elif ext == ".csv":
        import csv
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        if rows:
            cols = rows[0].keys()
            def pick(cands):
                for cc in cands:
                    for col in cols:
                        if cc == col.lower():
                            return col
                return None
            cs, ct, cts = pick(["sender", "name", "sendername", "remark", "nickname", "talker"]), \
                          pick(["text", "content", "msg", "strcontent"]), \
                          pick(["ts", "time", "strtime", "date"])
            for r in rows:
                out.append((str(r.get(cs) or person), str(r.get(ct) or ""),
                            str(r.get(cts) or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))))
    else:                                    # .txt 及其他：按行解析
        today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for ln in open(path, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            m = re.match(r"^\[(.+?)\]\s*([^：:]{1,20})[：:](.*)$", ln)
            if m:
                out.append((m.group(2).strip(), m.group(3).strip(), m.group(1).strip()))
            else:
                m = re.match(r"^([^：:]{1,20})[：:](.+)$", ln)
                if m:
                    out.append((m.group(1).strip(), m.group(2).strip(), today))
                else:
                    out.append((person, ln, today))
    out = [(s, t, _norm_ts(ts)) for s, t, ts in out if t.strip()]
    return out[-3000:]                        # 单文件上限，超出取最新


def export(person, db, months, out_root, extra_files=()):
    c = sqlite3.connect(db)
    direct, groups = find_windows(c, person)
    if not direct and not groups and not extra_files:
        print(f"[x] 库里没找到「{person}」（也没有 --extra 外部语料）");  return 1
    now = datetime.datetime.now()
    cutoff = (now - datetime.timedelta(days=months * 30)).strftime("%Y-%m-%d %H:%M:%S")
    outdir = os.path.join(out_root, person)
    os.makedirs(outdir, exist_ok=True)

    # ---- 语料 ----
    recent, old = [], []
    for conv in direct + groups:
        typ = "单聊" if conv in direct else "群聊"
        for sender, text, ts in q(c, "SELECT sender,text,ts FROM messages WHERE conv_name=? "
                                    "AND ts>=? ORDER BY ts", (conv, cutoff)):
            recent.append((conv, typ, ts, sender, text))
        older = q(c, "SELECT sender,text,ts FROM messages WHERE conv_name=? AND ts<? ORDER BY ts",
                  (conv, cutoff))
        if older:
            old.append((conv, typ, older))

    # ---- 外部语料（微信导出/用户粘贴）合并进细读层 ----
    extra_n = 0
    for p in extra_files:
        rows = _parse_extra(p, person)
        extra_n += len(rows)
        label = "外部语料·" + os.path.splitext(os.path.basename(p))[0]
        senders = {s for s, _, _ in rows}
        if senders and person not in senders:
            print(f"[!] 注意：{p} 里的发送者没有「{person}」（实际有 {sorted(senders)[:5]}），检查人名是否一致")
        recent += [(label, "外部", ts, s, t) for s, t, ts in rows]
    if extra_files:
        recent.sort(key=lambda r: r[2])

    recent.sort(key=lambda r: r[2])
    truncated = ""
    if len(recent) > RECENT_CAP:
        recent = recent[-RECENT_CAP:]
        truncated = f"（超出 {RECENT_CAP} 条，已按最新截断）"
    with open(os.path.join(outdir, "corpus_recent.md"), "w", encoding="utf-8") as f:
        f.write(f"# {person} · 近期语料（近 {months} 个月，{len(recent)} 条）{truncated}\n\n")
        cur = None
        for conv, typ, ts, sender, text in recent:
            if conv != cur:
                f.write(f"\n## 窗口：{conv}（{typ}）\n")
                cur = conv
            f.write(f"[{ts[5:16]}] {sender}：{text}\n")

    with open(os.path.join(outdir, "corpus_old.md"), "w", encoding="utf-8") as f:
        f.write(f"# {person} · 窗口外语料（只做精炼概括，不逐条分析）\n")
        for conv, typ, rows in old:
            f.write(f"\n## 窗口：{conv}（{typ}）· 窗口外 {len(rows)} 条\n")
            for sender, text, ts in rows[-OLD_SAMPLE:]:
                f.write(f"- [{ts[:10]}] {sender}：{text}\n")

    # ---- 统计信号 ----
    mine_recent = [t for (_, _, _, s, t) in recent if s == person]
    all_recent = len(recent)
    joined = "\n".join(mine_recent)
    stats = {
        "person": person, "db": db, "window_months": months,
        "mbti_override": read_override(out_root).get(person),
        "windows": {"direct": direct, "group": groups},
        "recent_msgs": all_recent, "person_recent_msgs": len(mine_recent),
        "old_msgs_total": sum(len(r) for _, _, r in old),
        "initiation_rate": round(_initiation(recent, person), 2),
        "reply_median_min": _reply_latency(recent, person, direct),
        "avg_len": round(statistics.mean(len(t) for t in mine_recent), 1) if mine_recent else 0,
        "emoji_rate": round(sum(1 for t in mine_recent if EMOJI_RE.search(t)) / len(mine_recent), 2) if mine_recent else 0,
        "question_rate": round(sum(1 for t in mine_recent if "？" in t or "?" in t) / len(mine_recent), 2) if mine_recent else 0,
        "particles": {w: joined.count(w) for w in PARTICLES if joined.count(w) >= 2},
        "hour_hist": _hour_hist(recent, person),
        "era": _era_markers(mine_recent),
    }
    with open(os.path.join(outdir, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)

    flag = "" if all_recent >= 30 else "  ⚠ 样本<30，只够出统计卡"
    extra_note = f" + 外部 {extra_n} 条" if extra_files else ""
    print(f"[ok] {person}: 近期 {all_recent} 条（本人 {len(mine_recent)}）{extra_note}"
          f" / 窗口外 {stats['old_msgs_total']} 条"
          f" / 窗口 {len(direct)} 单聊 + {len(groups)} 群聊{flag}")
    print(f"     -> {outdir}")
    return 0


def _initiation(rows, person):
    """隔 GAP_HOURS 以上出现的新一轮里，由本人开头的比例（1=全程都是TA先开口）。"""
    starts, total = 0, 0
    prev_ts, prev_conv = None, None
    for conv, _, ts, sender, _ in rows:
        try:
            t = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if conv != prev_conv:
            prev_ts, prev_conv = None, conv
        if prev_ts is not None and (t - prev_ts).total_seconds() > GAP_HOURS * 3600:
            total += 1
            if sender == person:
                starts += 1
        prev_ts = t
    return round(starts / total, 2) if total else -1


def _reply_latency(rows, person, direct):
    """单聊里：对方发消息后本人隔多久回（分钟中位数）。-1=算不出。"""
    lats, prev = [], None
    for conv, _, ts, sender, _ in rows:
        if conv not in direct:
            continue
        try:
            t = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if prev and prev[0] == conv and prev[1] != person and sender == person:
            mins = (t - prev[2]).total_seconds() / 60
            if mins < 60 * 24:                    # 超过一天的隔夜回复不算
                lats.append(mins)
        prev = (conv, sender, t)
    return round(statistics.median(lats)) if lats else -1


def _hour_hist(rows, person):
    h = {}
    for _, _, ts, sender, _ in rows:
        if sender == person:
            k = int(ts[11:13]) // 4 * 4
            h[k] = h.get(k, 0) + 1
    return dict(sorted(h.items()))


def to_card(person, db, card_file):
    card = open(card_file, encoding="utf-8").read().strip()
    c = sqlite3.connect(db)
    c.execute("INSERT OR REPLACE INTO relationship_cards(conv_name,card,updated_at) VALUES(?,?,?)",
              (person, card, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    c.commit()
    print(f"[ok] 人物卡已回灌 relationship_cards：{person}（{len(card)} 字）。悬浮窗右键「重载上下文库」生效。")


def cache_summaries(person, db, sum_file):
    """把分层性格概括写进 summaries 表：改写端从此吃缓存，不再每次重读全部对话。
    json: [ {"start":"2025-09-20","end":"2026-06-20","text":"这三个月他……"}, ... ]"""
    rows = json.load(open(sum_file, encoding="utf-8"))
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c = sqlite3.connect(db)
    n = 0
    for r in rows:
        ps, pe, sm = str(r.get("start") or ""), str(r.get("end") or ""), str(r.get("text") or "").strip()
        if not sm:
            continue
        c.execute("INSERT OR REPLACE INTO summaries(conv_name,period_start,period_end,summary,updated_at) "
                  "VALUES(?,?,?,?,?)", (person, ps, pe, sm, now))
        n += 1
    c.commit()
    print(f"[ok] {person} 的分层性格概括已落库 summaries（{n} 层）。悬浮窗右键「重载上下文库」生效。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("person", nargs="?", help="人名；配合 --list 使用时可省略")
    ap.add_argument("--list", action="store_true", help="列出库里可作为分析对象的人")
    ap.add_argument("--months", type=int, default=3, help="细读窗口月数，默认 3")
    ap.add_argument("--db", default=os.path.join(os.path.expanduser("~"),
                    "Desktop", "嘴替-zuiti", "context.db"), help="记忆库路径")
    ap.add_argument("--extra", nargs="+", default=[], metavar="FILE",
                    help="外部语料文件（.json/.csv/.txt，微信导出或粘贴的聊天），合并进细读层")
    ap.add_argument("--set-mbti", metavar="TYPE",
                    help="钦点该人的 MBTI（16 型之一），写入 _mbti_override.json")
    ap.add_argument("--card-file", help="人物卡浓缩版 md 文件路径 → 回灌 relationship_cards")
    ap.add_argument("--summaries-file", help="分层性格概括 json → 落库 summaries（改写端吃缓存）")
    a = ap.parse_args()

    db = os.path.abspath(os.path.expanduser(a.db))
    if not os.path.exists(db):
        print(f"[x] 找不到库 {db}");  return 1
    c = sqlite3.connect(db)
    if a.list:
        for name, n, tag in q(c, "SELECT name, "
                                 "(SELECT COUNT(*) FROM messages m WHERE m.conv_name=c.name), "
                                 "recipient_tag FROM conversations c ORDER BY name"):
            print(f"{name}  [{tag}]  {n} 条")
        return 0
    c.close()
    if not a.person:
        print("用法：python persona_export.py 人名（或 --list）");  return 1
    if a.card_file:
        to_card(a.person, db, a.card_file);  return 0
    if a.summaries_file:
        cache_summaries(a.person, db, a.summaries_file);  return 0
    out_root = os.path.join(os.path.dirname(db), "persona_cards")
    if a.set_mbti:
        return set_mbti(a.person, a.set_mbti, out_root)
    return export(a.person, db, a.months, out_root, a.extra)


if __name__ == "__main__":
    raise SystemExit(main())
