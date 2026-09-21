# -*- coding: utf-8 -*-
"""
check_env.py —— 嘴替环境自检：路径、记忆库、dumps、配置、Ollama、人物卡，一次看全。
出问题时先跑这个，别猜。

用法：
  python check_env.py               # 检查当前生效库（ZUITI_DB 优先）
  python check_env.py --db demo_context.db   # 检查指定库
双击 检查环境.bat 效果一样。
"""
import argparse
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)


def mark(ok):
    return "✓" if ok else "✗"


def section(title):
    print(f"\n[{title}]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="检查指定库路径（默认检查生效库）")
    a = ap.parse_args()

    print("=" * 50)
    print("嘴替环境自检")
    print("=" * 50)

    # ---------- 路径 ----------
    section("路径")
    print(f"  脚本目录   : {BASE}")
    env_db = os.environ.get("ZUITI_DB")
    if env_db:
        print(f"  ZUITI_DB   : {env_db}   ← 环境变量优先生效（演示模式?）")
    else:
        print("  ZUITI_DB   : 未设置（走默认路径）")

    import context as ctx
    if a.db:
        ctx.DB = os.path.abspath(a.db)          # 检查指定库
    print(f"  生效记忆库 : {ctx.DB} {mark(os.path.exists(ctx.DB))}")
    if ctx.LAST_ERROR:
        print(f"  ⚠ 最近读库错误: {ctx.LAST_ERROR}")

    # 配置先读好，后面几节都要用
    cfg = {}
    try:
        cfg = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))
    except Exception as e:
        cfg_err = str(e)

    # ---------- 记忆库 ----------
    section("记忆库")
    st = ctx.db_stats()
    if not st["exists"]:
        print(f"  {mark(False)} 库文件不存在")
        print("    修：python build_context_db.py（需要 dumps/ 里的钉钉导出）")
        print("        或 python build_demo_db.py 生成演示库先跑起来")
    else:
        print(f"  会话 {st['convs']} · 消息 {st['msgs']} · 关系卡 {st['cards']} · 性格概括 {st['summaries']}"
              + ("   ← summaries 为 0，人物分析还没落过缓存" if st["summaries"] == 0 else ""))
        if -1 in (st["convs"], st["msgs"], st["cards"], st["summaries"]):
            print(f"  {mark(False)} 有表缺失（老库）——重跑一次 build_context_db.py 会自动补建")
        for name, n in st["per_conv"][:8]:
            print(f"    · {name}: {n} 条")
        if st["per_conv"] and st["per_conv"][0][1] > 0:
            auto = ctx.detect_self()
            mine = cfg.get("my_name") or auto
            note = f"（自动识别猜的，config my_name 才是权威）" if not cfg.get("my_name") and auto else ""
            print(f"  生效本人: {mine or '(没认出来)'} · 自动识别={auto or '失败'} {note}")
            card = ctx.style_card(cfg.get("my_name") or None)
            print(f"  自我卡: {'可生成（' + str(len(card)) + '字）' if card else '不可生成（本人消息<8条）'}")

    # ---------- dumps ----------
    section("dumps（钉钉导出）")
    try:
        import build_context_db as bcd
        print(f"  目录: {bcd.DUMPS} {mark(os.path.isdir(bcd.DUMPS))}")
        if os.path.isdir(bcd.DUMPS):
            have = set(os.listdir(bcd.DUMPS))
            print(f"  现有文件: {sorted(have) or '(空)'}")
            miss = [(c['name'], c['file']) for c in bcd.CONVS if c['file'] not in have]
            if miss:
                print(f"  {mark(False)} 清单里缺: " + "、".join(f"{n}({f})" for n, f in miss))
                print("    修：从钉钉重新导出这几个会话，放进 dumps/ 再跑 build_context_db.py")
            else:
                print(f"  {mark(True)} CONVS 清单 {len(bcd.CONVS)} 个会话的导出文件齐全")
        else:
            print("    修：建 dumps/ 目录并放入钉钉聊天导出（由 QwenWork dws chat 拉取）")
    except Exception as e:
        print(f"  {mark(False)} 读不到 build_context_db.py 的清单: {e}")

    # ---------- 配置 ----------
    section("config.json")
    cfgp = os.path.join(BASE, "config.json")
    if cfg:
        print(f"  {mark(True)} {cfgp}")
        print(f"  引擎 {cfg.get('provider', 'api')} · api模型 {cfg.get('api_model', 'qwen3.8-flash')}"
              f" · 本地模型 {cfg.get('model')} · 热键 {cfg.get('hotkey')} · 自动发出 {cfg.get('auto_enter')}"
              f" · 模仿本人 {cfg.get('mimic_me')} · my_name {cfg.get('my_name') or '(空，自动识别)'}")
    else:
        print(f"  {mark(False)} 读取失败: {locals().get('cfg_err', '文件不存在')}")

    # ---------- 引擎 ----------
    section("引擎（api / ollama 双档）")
    provider = (cfg.get("provider") if cfg else "api") or "api"
    key = ""
    try:
        lsp = os.path.join(BASE, "local_settings.json")
        if os.path.exists(lsp):
            key = json.load(open(lsp, encoding="utf-8")).get("api_key") or ""
    except Exception:
        pass
    key = key or os.environ.get("DASHSCOPE_API_KEY") or ""
    if provider == "api":
        if key:
            print(f"  {mark(True)} api 档生效（{cfg.get('api_model', 'qwen3.8-flash')}）· key 已配：{key[:8]}…")
            base = (cfg.get("api_base") or "").rstrip("/")
            try:
                req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + key})
                with urllib.request.urlopen(req, timeout=6) as r:
                    ok = r.status == 200
                print(f"  {mark(ok)} {base} 连通性 {'正常' if ok else '异常'}")
            except Exception as e:
                print(f"  {mark(False)} api 连不上: {str(e)[:60]}")
                print("    修：查网络/代理；key 不对就去 local_settings.json 核对，或换 api_base")
        else:
            print(f"  {mark(False)} provider=api 但没配 key —— 会自动回退本地模型")
            print("    修：把 key 写进 local_settings.json {\"api_key\": \"sk-…\"}（此文件已 gitignore）")
    else:
        print("  ollama 档生效（隐私档：草稿不出本机），api 段跳过")

    # ---------- Ollama ----------
    section("Ollama（本地档引擎）")
    host = (cfg.get("ollama_host") if 'cfg' in dir() else "http://localhost:11434") or "http://localhost:11434"
    try:
        with urllib.request.urlopen(host.rstrip("/") + "/api/tags", timeout=2) as r:
            tags = json.loads(r.read().decode("utf-8"))
        models = [m["name"] for m in tags.get("models", [])]
        print(f"  {mark(True)} {host} 在线，模型: {models or '(一个都没有)'}")
        want = cfg.get("model")
        if want and not any(m == want or m.split(":")[0] == want.split(":")[0] for m in models):
            print(f"  {mark(False)} 配置里的 {want} 没拉取 —— 跑: ollama pull {want}")
    except Exception:
        api_in_charge = provider == "api" and bool(key)
        if api_in_charge:
            print(f"  {mark(False)} {host} 连不上 —— 不影响改写（api 档在主位，它只是备胎）")
            print("    修（可选）：想用隐私档时再启动 Ollama（开始菜单开 Ollama，或 ollama serve）")
        else:
            print(f"  {mark(False)} {host} 连不上 —— 当前档改写会走规则兜底")
            print("    修：启动 Ollama（开始菜单开 Ollama，或 ollama serve）")

    # ---------- 运行日志 ----------
    section("运行日志（可观测性）")
    try:
        import runlog
        s = runlog.stats(7)
        if not s:
            print(f"  最近 7 天没有记录（{runlog.RUNLOG_PATH}）——不是问题，按一次 Alt+Z 就有")
        else:
            print(runlog.format_stats(s, indent="  "))
            print(f"  明细: {runlog.RUNLOG_PATH}（按 run 字段可追单次改写全程）")
    except Exception as e:
        print(f"  {mark(False)} 读不了运行日志: {e}")

    # ---------- persona_cards ----------
    section("persona_cards（人物卡）")
    pdir = os.path.join(os.path.dirname(ctx.DB), "persona_cards")
    if os.path.isdir(pdir):
        files = sorted(os.listdir(pdir))
        print(f"  {mark(True)} {pdir}")
        print(f"  内容: {files or '(空)'}")
        ovp = os.path.join(pdir, "_mbti_override.json")
        if os.path.exists(ovp):
            print(f"  MBTI 钦点: {json.load(open(ovp, encoding='utf-8'))}")
    else:
        print(f"  （还没有 —— 跑过 chat-persona 人物分析并落缓存后出现）")

    print("\n" + "=" * 50)
    print("逐项有 ✗ 就按后面'修：'处理；全 ✓ 还不正常再来找我。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
