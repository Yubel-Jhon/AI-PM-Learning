#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOSS直聘求职全自动流水线 —— 抓岗位 → 生成看板 → 自动投递 → 增量补新岗/清下架

子命令:
  init      生成默认配置 ~/.boss-apply/config.json（改 keywords/cities 后再跑）
  doctor    环境体检：DrissionPage / 配置 / Chrome profile / 看板 / 数据目录
  login     弹出 Chrome 扫码登录 BOSS（会话失效时用，轮询 5 分钟）
  scrape    全量深抓岗位（关键词×城市 API 拦截 + 评分）→ jobs.json
  board     从 jobs.json 生成全新看板 HTML（board_template.html）
  dry-run   打印投递队列预览（不开浏览器）
  apply     纯投递，不补刷
  auto      主命令：投递 + 周期补刷新岗 + 收尾清下架 + 生成看板导入文件
  refresh   只增量补岗（只读搜索页，安全）
  status    进度摘要
  export    生成看板导入文件 board-import.json

投递参数: --limit N  --priority S,A  --ids <job_id>  --min-delay  --max-delay
中途停止: 新建空文件 ~/.boss-apply/STOP，当前岗位处理完优雅停止
"""
import sys, re, json, html, time, random, argparse
from urllib.parse import urlencode
from pathlib import Path
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HOME = Path.home()
DATA_DIR = HOME / ".boss-apply"
CONFIG_FILE = DATA_DIR / "config.json"
PROGRESS_FILE = DATA_DIR / "progress.json"
STATUS_FILE = DATA_DIR / "status.json"
RUNLOG_FILE = DATA_DIR / "run-log.jsonl"
BOARD_IMPORT_FILE = DATA_DIR / "board-import.json"
STOP_FILE = DATA_DIR / "STOP"
SHOTS_DIR = DATA_DIR / "screenshots"

DEFAULT_CONFIG = {
    "daily_cap": 25,
    "min_delay_s": 18,
    "max_delay_s": 45,
    "priorities": ["S", "A"],
    "max_run_minutes": 45,
    "captcha_wait_s": 300,
    "profile_dir": r"C:\Users\Admin\.boss-zhipin-scraper\chrome-profile",
    "chrome_port": 9222,
    "board_html": r"C:\Users\Admin\Desktop\BOSS投递看板.html",
    "selectors": {
        "startchat_class": "btn-startchat",
        "startchat_text": "立即沟通",
        "continued_text": "继续沟通",
        "modal_confirm_texts": ["发送问候语", "开始沟通", "确认发送"]
    },
    "offline_texts": ["职位已关闭", "已下线", "职位不存在", "已失效"],
    "limit_texts": ["已达上限", "用完", "今日沟通人数"],
    "refresh": {
        "every_n_applies": 5,
        "every_m_minutes": 12,
        "max_new_per_round": 10,
        "max_new_per_run": 30
    },
    "auto_send_resume": True
}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def load_config():
    DATA_DIR.mkdir(exist_ok=True)
    SHOTS_DIR.mkdir(exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"已生成默认配置 {CONFIG_FILE}")
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    merged = dict(DEFAULT_CONFIG); merged.update(cfg)
    merged["selectors"] = {**DEFAULT_CONFIG["selectors"], **cfg.get("selectors", {})}
    return merged

def load_progress():
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"jobs": {}, "daily": {}}

def save_progress(p):
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(PROGRESS_FILE)

def write_status(phase, **kw):
    st = {"phase": phase, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    st.update(kw)
    STATUS_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

def append_runlog(rec):
    with open(RUNLOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def check_stop():
    if STOP_FILE.exists():
        STOP_FILE.unlink()
        return True
    return False

# ---------------- 看板数据 ----------------

def load_board_jobs(cfg):
    p = Path(cfg["board_html"])
    if not p.exists():
        raise FileNotFoundError(f"看板文件不存在: {p}")
    text = p.read_text(encoding="utf-8")
    m = re.search(r"const JOBS = (\[.*?\]);", text, re.DOTALL)
    if not m:
        raise ValueError("看板 HTML 里没找到 const JOBS = [...]（结构变了？）")
    return json.loads(m.group(1))

def build_queue(cfg, jobs, progress, priority=None, ids=None):
    done_states = {"applied", "already_contacted", "offline", "limit_hit"}
    pri = [x.strip().upper() for x in priority.split(",")] if priority else cfg["priorities"]
    q = []
    for j in jobs:
        if not (j.get("jd") or ""):
            continue
        if progress["jobs"].get(j["id"], {}).get("st") in done_states:
            continue
        if ids and j["id"] not in ids:
            continue
        if not ids and j.get("p") not in pri:
            continue
        q.append(j)
    # 刷新来源(src=r)的岗位排前面 —— 老岗位很多是用户手动沟通过的积压，先投新货
    q.sort(key=lambda j: 0 if j.get("src") == "r" else 1)
    return q

AUTH_ENDPOINT = ("https://www.zhipin.com/wapi/zprelation/interaction/geekGetJob"
                 "?page=1&tag=5&isActive=true")

def start_browser(cfg):
    from DrissionPage import ChromiumPage, ChromiumOptions
    from DrissionPage.common import Settings
    Settings.set_singleton_tab_obj(False)
    profile = Path(cfg["profile_dir"])
    profile.mkdir(parents=True, exist_ok=True)
    co = ChromiumOptions()
    co.set_argument("--disable-blink-features=AutomationControlled")
    minor = random.randint(0, 99)
    co.set_user_agent(
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/130.0.0.{minor} Safari/537.36")
    co.set_pref("excludeSwitches", ["enable-automation"])
    co.set_pref("useAutomationExtension", False)
    w = random.choice([1440, 1512, 1680]) + random.randint(-20, 20)
    h = random.choice([900, 960, 1050]) + random.randint(-20, 20)
    co.set_argument(f"--window-size={w},{h}")
    co.set_argument("--lang=zh-CN")
    co.set_user_data_path(str(profile))
    co.set_local_port(cfg["chrome_port"])
    page = ChromiumPage(addr_or_opts=co)
    try:
        page.set.timeouts(base=30, page_load=30, script=20)
    except Exception:
        pass
    try:
        page.run_js(
            'Object.defineProperty(navigator,"webdriver",{get:()=>undefined});'
            'Object.defineProperty(navigator,"plugins",{get:()=>[1,2,3,4,5]});'
            'Object.defineProperty(navigator,"languages",{get:()=>["zh-CN","zh","en"]});'
            'window.chrome={runtime:{},loadTimes:()=>({}),csi:()=>({})};')
    except Exception:
        pass
    return page

def parse_boss_auth_response(markup):
    match = re.search(r"<pre[^>]*>(.*?)</pre>", str(markup or ""), re.IGNORECASE | re.DOTALL)
    raw = html.unescape(match.group(1) if match else str(markup or "")).strip()
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("code") not in (0, "0"):
        return None
    return payload if isinstance(payload.get("zpData"), dict) else None

def session_ok(page):
    tab = None
    try:
        tab = page.new_tab(AUTH_ENDPOINT)
        time.sleep(0.8)
        return parse_boss_auth_response(tab.html) is not None
    except Exception:
        return False
    finally:
        if tab is not None:
            try:
                tab.close()
            except Exception:
                pass

def random_delay(lo, hi):
    mean = (lo + hi) / 2
    std = (hi - lo) / 4
    d = max(lo * 0.7, random.gauss(mean, std))
    if random.random() < 0.05:
        d += random.uniform(2, 6)
    time.sleep(d)

def simulate_human(page):
    for _ in range(random.randint(1, 3)):
        try:
            page.scroll.down(random.randint(150, 400))
        except Exception:
            pass
        time.sleep(random.uniform(0.4, 1.4))
    try:
        page.scroll.to_top()
    except Exception:
        pass
    time.sleep(random.uniform(0.3, 0.8))

def match_any(text, words):
    return any(w in text for w in words if w)

def page_text_safe(tab):
    try:
        return tab("tag:body").text or ""
    except Exception:
        try:
            return tab.html
        except Exception:
            return ""

CAPTCHA_SELECTORS = ("iframe[src*=captcha]", "div[class*=captcha]", "div[class*=geetest]",
                     "div[id^=nc_]", "div[class*=verify-wrap]", "div[class*=dy_slid]")

def captcha_dom(tab):
    """滑块/验证组件必须看 DOM —— JD 正文出现「验证码」字样是正常内容，不能误判"""
    for sel in CAPTCHA_SELECTORS:
        try:
            if tab.eles(sel):
                return True
        except Exception:
            continue
    return False

def classify_job_page(tab, cfg):
    try:
        url = tab.url or ""
    except Exception:
        url = ""
    if any(k in url for k in ("login", "passport")):
        return "need_login"
    if captcha_dom(tab):
        return "captcha"
    text = page_text_safe(tab)
    if match_any(text, cfg["offline_texts"]):
        return "offline"
    return "ok"
    if match_any(text, cfg["offline_texts"]):
        return "offline"
    return "ok"

def _visible(el):
    try:
        return el.states.is_displayed
    except Exception:
        try:
            st = el.attr("style") or ""
            return "display: none" not in st and "visibility: hidden" not in st
        except Exception:
            return False

def find_chat_buttons(tab, cfg):
    s = cfg["selectors"]
    starts, continued = [], False
    seen = set()

    def q_first(queries):
        """依次尝试选择器，命中即返回 —— 避免 text: 包含匹配拉回整条祖先链"""
        for q in queries:
            try:
                r = tab.eles(q)
                if r:
                    return r
            except Exception:
                pass
        return []

    candidates = []
    try:
        candidates += tab.eles(f"@class:{s['startchat_class']}")
    except Exception:
        pass
    candidates += q_first([f"text={s['continued_text']}", f"text:{s['continued_text']}"])
    candidates += q_first([f"text={s['startchat_text']}", f"text:{s['startchat_text']}"])

    for el in candidates:
        try:
            if id(el) in seen:
                continue
            seen.add(id(el))
            t = " ".join((el.text or "").split())
            if not t:
                continue
            if t == s["continued_text"]:
                continued = True
            elif s["startchat_text"] in t and len(t) <= 12:
                if _visible(el):
                    starts.append(el)
        except Exception:
            continue
    return starts, continued

def click_confirm_modal(tab, cfg):
    texts = cfg["selectors"]["modal_confirm_texts"]
    for txt in texts:
        if not txt:
            continue
        # 定向查询代替全页扫描（全页 button+a 每个 text 都是一次 CDP 往返，巨慢）
        for q in (f"text={txt}", f"text:{txt}"):
            try:
                els = tab.eles(q)
            except Exception:
                continue
            for el in els:
                try:
                    t = " ".join((el.text or "").split())
                    if t == txt and _visible(el):
                        el.click()
                        return t
                except Exception:
                    continue
            if els:
                break
    return None


def new_tabs_diff(page, before_ids):
    try:
        return list(set(page.tab_ids) - before_ids)
    except Exception:
        return []

def shot(tab, job_id, tag):
    try:
        f = SHOTS_DIR / f"{job_id}_{tag}_{datetime.now().strftime('%H%M%S')}.png"
        tab.get_screenshot(path=str(f))
        return f.name
    except Exception:
        return ""

def send_resume_if_present(tab, cfg):
    """聊天窗出现「发送简历」入口就点掉（附件简历需已在 BOSS 平台上传过）"""
    if not cfg.get("auto_send_resume"):
        return False
    for q in ("text=发送简历", "text:发送简历"):
        try:
            els = tab.eles(q)
        except Exception:
            continue
        for el in els:
            try:
                t = " ".join((el.text or "").split())
                if t == "发送简历" and _visible(el):
                    el.click()
                    time.sleep(random.uniform(1.2, 2.0))
                    click_confirm_modal(tab, cfg)
                    log("    已点「发送简历」")
                    return True
            except Exception:
                continue
        if els:
            break
    return False

def process_one(page, job, cfg):
    jd = job.get("jd") or job["id"]
    url = f"https://www.zhipin.com/job_detail/{jd}.html"
    try:
        tab_ids_before = set(page.tab_ids)
    except Exception:
        tab_ids_before = set()
    try:
        page.get(url)
    except Exception as e:
        return "unknown", f"open_fail:{e}"
    time.sleep(random.uniform(2.0, 3.5))
    state = classify_job_page(page, cfg)
    if state in ("offline", "need_login", "captcha"):
        return state, ""
    simulate_human(page)
    starts, continued = find_chat_buttons(page, cfg)
    if continued and not starts:
        return "already_contacted", "按钮=继续沟通"
    if not starts:
        shot(page, job["id"], "nobutton")
        return "unknown", "页面上没找到「立即沟通」按钮"
    try:
        starts[0].click()
    except Exception:
        try:
            starts[0].click(by_js=True)
        except Exception as e:
            shot(page, job["id"], "clickfail")
            return "unknown", f"click_fail:{e}"
    time.sleep(random.uniform(2.5, 4.0))
    new_ids = new_tabs_diff(page, tab_ids_before)
    if new_ids:
        try:
            chat = page.get_tab(new_ids[0])
        except Exception:
            chat = None
        if chat is not None:
            time.sleep(random.uniform(1.5, 2.5))
            if classify_job_page(chat, cfg) == "captcha":
                return "captcha", "聊天页出现安全验证"
            click_confirm_modal(chat, cfg)
            send_resume_if_present(chat, cfg)
            time.sleep(random.uniform(1.5, 3.0))
            try:
                chat.close()
            except Exception:
                pass
    else:
        try:
            now_url = page.url or ""
        except Exception:
            now_url = ""
        if ("/chat/" in now_url) or ("im.html" in now_url):
            if classify_job_page(page, cfg) == "captcha":
                return "captcha", "聊天页出现安全验证"
            click_confirm_modal(page, cfg)
            send_resume_if_present(page, cfg)
            time.sleep(random.uniform(1.5, 3.0))
        elif click_confirm_modal(page, cfg):
            send_resume_if_present(page, cfg)
            time.sleep(random.uniform(1.5, 3.0))
    try:
        if match_any(page_text_safe(page), cfg["limit_texts"]):
            return "limit_hit", "当日沟通额度已用完"
    except Exception:
        pass
    time.sleep(random.uniform(1.0, 2.0))
    try:
        page.get(url)
    except Exception as e:
        return "unknown", f"verify_open_fail:{e}"
    time.sleep(random.uniform(2.0, 3.5))
    st2 = classify_job_page(page, cfg)
    if st2 in ("captcha", "need_login"):
        return st2, ""
    starts2, continued2 = find_chat_buttons(page, cfg)
    if continued2:
        return "applied", ""
    if starts2:
        shot(page, job["id"], "stillstart")
        return "unknown", "投后按钮仍是「立即沟通」"
    shot(page, job["id"], "unverified")
    return "unknown", "验证页无法确认按钮状态"

def wait_out_captcha(page, cfg):
    log(f"!!! 出现安全验证，请在 Chrome 窗口里手动完成（最多等 {cfg['captcha_wait_s']}s）")
    deadline = time.time() + cfg["captcha_wait_s"]
    while time.time() < deadline:
        time.sleep(8)
        try:
            if not captcha_dom(page):
                log("安全验证已通过，继续")
                return True
        except Exception:
            pass
    log("等待验证超时")
    return False

def cmd_apply(cfg, limit=None, priority=None, ids=None, dry_run=False, refresh=False):
    jobs = load_board_jobs(cfg)
    progress = load_progress()
    today = datetime.now().strftime("%Y-%m-%d")
    queue = build_queue(cfg, jobs, progress, priority=priority, ids=ids)
    if dry_run:
        print(json.dumps({"queue_size": len(queue),
                          "preview": [{"p": j["p"], "sc": j["sc"], "t": j["t"],
                                       "corp": j["corp"], "city": j["city"], "sal": j["sal"],
                                       "id": j["id"]} for j in queue[:60]]},
                         ensure_ascii=False, indent=1))
        return 0
    applied_today = progress.get("daily", {}).get(today, 0)
    cap = cfg["daily_cap"]
    if limit is None:
        limit = max(0, cap - applied_today)
    if applied_today >= cap:
        log(f"今日已投 {applied_today} 达上限 {cap}，收工")
        write_status("done", applied_today=applied_today, message="daily cap")
        return 0
    limit = min(limit, cap - applied_today)
    run_start = time.time()
    results = []
    write_status("running", applied_today=applied_today, limit=limit,
                 results=[], current=None, message="启动浏览器")
    page = start_browser(cfg)
    log(f"浏览器已启动 (profile={cfg['profile_dir']}, port={cfg['chrome_port']})")
    if not session_ok(page):
        write_status("need_login", applied_today=applied_today, message="会话无效，请登录")
        log("!! BOSS 会话无效 —— 请运行: python apply_boss.py login 然后扫码登录")
        return 3
    log(f"鉴权 OK。队列 {len(queue)} 条，本次投递额度 {limit}，今日已投 {applied_today}/{cap}")
    rcfg = cfg["refresh"]
    new_total = 0
    last_refresh = time.time()
    applied_at_refresh = 0
    applied_today0 = applied_today
    try:
        queue = queue[:limit + rcfg["max_new_per_run"]]
        i = -1
        while True:
            i += 1
            if i >= len(queue) or i >= limit + rcfg["max_new_per_run"]:
                break
            if applied_today - applied_today0 >= limit:
                log("本次投递额度已用完，收工")
                break
            job = queue[i]
            n_total = min(limit + rcfg["max_new_per_run"], len(queue))
            if check_stop():
                log("检测到 STOP 文件，本批结束")
                break
            if time.time() - run_start > cfg["max_run_minutes"] * 60:
                log("达到单次运行时限，收工")
                break
            write_status("running", applied_today=applied_today, limit=limit,
                         results=results, current={"id": job["id"], "t": job["t"], "corp": job["corp"]},
                         message=f"({i+1}/{n_total})")
            log(f"[{i+1}/{n_total}] {job['corp']} · {job['t']} ({job['city']} {job['sal']})")
            status, note = process_one(page, job, cfg)
            if status == "captcha":
                if wait_out_captcha(page, cfg):
                    status, note = process_one(page, job, cfg)
                else:
                    write_status("captcha", applied_today=applied_today, results=results,
                                 message="验证未通过，中止")
                    return 4
            if status == "need_login":
                progress.setdefault("jobs", {})[job["id"]] = {"st": "need_login",
                    "ts": datetime.now().isoformat(timespec="seconds"), "note": note}
                save_progress(progress)
                write_status("need_login", applied_today=applied_today, results=results,
                             message="登录失效，中止")
                log("!! 登录失效，中止。请重新扫码后继续")
                return 3
            progress.setdefault("jobs", {})[job["id"]] = {"st": status,
                "ts": datetime.now().isoformat(timespec="seconds"), "note": note}
            if status == "applied":
                # already_contacted 是之前就沟通过的，今天没消耗额度，不计数
                progress.setdefault("daily", {})[today] = progress.get("daily", {}).get(today, 0) + 1
                applied_today += 1
            save_progress(progress)
            results.append({"id": job["id"], "t": job["t"], "corp": job["corp"],
                            "st": status, "note": note})
            append_runlog({"ts": datetime.now().isoformat(timespec="seconds"),
                           "id": job["id"], "t": job["t"], "corp": job["corp"],
                           "st": status, "note": note})
            if status == "limit_hit":
                log("命中今日沟通上限，收工")
                break
            # ---- 周期补刷新岗位（一边投一边扒）----
            if refresh and new_total < rcfg["max_new_per_run"]:
                n_applied = applied_today - applied_today0
                if (n_applied - applied_at_refresh >= rcfg["every_n_applies"]
                        or time.time() - last_refresh >= rcfg["every_m_minutes"] * 60):
                    log("== 触发补刷新岗位 ==")
                    applied_at_refresh = n_applied
                    last_refresh = time.time()
                    try:
                        fresh, _ = refresh_all(
                            page, cfg["board_html"],
                            max_new_per_round=rcfg["max_new_per_round"],
                            existing_ids={j["id"] for j in jobs},
                            progress_ids=set(progress.get("jobs", {}).keys()))
                        if fresh:
                            new_total += len(fresh)
                            jobs = load_board_jobs(cfg)
                            fullq = build_queue(cfg, jobs, progress,
                                                priority=priority, ids=ids)
                            known = {q["id"] for q in queue}
                            for q in fullq:
                                if q["id"] not in known:
                                    queue.append(q)
                                    known.add(q["id"])
                            log(f"  队列扩至 {len(queue)} 条待处理")
                    except Exception as e:
                        log(f"  补刷失败（不影响投递）: {e!r}")
            if i + 1 < len(queue):
                random_delay(cfg["min_delay_s"], cfg["max_delay_s"])
    except KeyboardInterrupt:
        log("手动中断，进度已保存")
    except Exception as e:
        log(f"!! 异常中止: {e!r}")
        write_status("error", applied_today=applied_today, results=results, message=repr(e))
        return 1
    write_status("done", applied_today=applied_today, results=results, message="本批完成")
    ok = sum(1 for r in results if r["st"] in ("applied", "already_contacted"))
    log(f"完成：{ok}/{len(results)} 成功。进度在 {PROGRESS_FILE}")
    if refresh:
        try:
            n = prune_offline(cfg["board_html"], load_progress())
            if n:
                log(f"已从看板清理 {n} 个下架岗位")
            cmd_export(cfg)
            log("看板导入文件已生成，打开看板点「导入投递」同步列位置")
        except Exception as e:
            log(f"收尾清理/导出失败（不影响投递结果）: {e!r}")
    return 0

def cmd_doctor(cfg):
    print("== BOSS apply 体检 ==")
    ok = True
    try:
        from DrissionPage import ChromiumPage, ChromiumOptions  # noqa: F401
        print("[OK] DrissionPage 已安装")
    except Exception as e:
        print(f"[!!] DrissionPage 未安装: {e} —— pip install DrissionPage==4.1.1.4")
        ok = False
    board = Path(cfg["board_html"])
    if board.exists():
        jobs = load_board_jobs(cfg)
        with_jd = sum(1 for j in jobs if j.get("jd"))
        print(f"[OK] 看板 {board.name}: {len(jobs)} 条, 其中 {with_jd} 条有 jd(可投)")
        if with_jd == 0:
            print("[!!] 没有任何 jd 字段，投不了 —— 链接必须是 encrypt_job_id")
            ok = False
    else:
        print(f"[!!] 看板文件不存在: {board}")
        ok = False
    prof = Path(cfg["profile_dir"])
    if prof.exists():
        print(f"[OK] Chrome profile 存在: {prof}")
    else:
        print(f"[!!] Chrome profile 不存在: {prof}（首次 login 会自动创建）")
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / ".t").write_text("ok", encoding="utf-8")
        (DATA_DIR / ".t").unlink()
        print(f"[OK] 数据目录可写: {DATA_DIR}")
    except Exception as e:
        print(f"[!!] 数据目录不可写: {e}")
        ok = False
    prog = load_progress()
    if prog:
        today = datetime.now().strftime("%Y-%m-%d")
        print(f"     进度: 已处理 {len(prog.get('jobs', {}))} 条, 今日已投 "
              f"{prog.get('daily', {}).get(today, 0)}")
    print("== 体检" + ("通过 ==" if ok else "有问题，见上 =="))
    return 0 if ok else 1

def cmd_login(cfg):
    page = start_browser(cfg)
    page.get("https://www.zhipin.com/")
    log("请在打开的 Chrome 里扫码登录 BOSS直聘（最多等 5 分钟）...")
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(5)
        if session_ok(page):
            log("登录成功，会话有效")
            write_status("done", message="login ok")
            return 0
    log("5 分钟内未检测到登录成功")
    return 3

# ==================== 岗位抓取 · 评分 · 看板 ====================

LISTEN_TARGET = "wapi/zpgeek/search/joblist.json"

KEYWORDS = ["AI产品经理", "Agent产品经理"]
CITIES = {  # cityName 自校验：code 错了抓回来的城市名不匹配会被过滤，不污染看板
    "上海": "101020100", "深圳": "101280600", "杭州": "101210100",
    "南京": "101190100", "苏州": "101190400",
}

AI_TERMS = ['AI', 'AIGC', 'AGI', '人工智能', '大模型', 'LLM', 'GPT', 'NLP', '智能',
            '算法', '机器学习', '深度学习', '智能体', 'AGENT', '多模态', '生成式',
            'RAG', 'COPILOT', 'CHATBOT', '数字人', '语音', '计算机视觉']
ROLE_TERMS = ['产品', 'PRODUCT', 'PM', '解决方案', '交付']
BLACKLIST = ['美术', '特效', '地编', '原画', '开发', '程序', '测试', '工程师岗前训',
             '广告投放专员', '营销', '电商运营', '客服', '销售', '行政', '人事', '前端', '后端']

SCORE_HINTS = [("agent", 2.0), ("智能体", 2.0), ("大模型", 2.0), ("llm", 2.0),
               ("ai产品", 2.0), ("rag", 1.5), ("多模态", 1.5), ("语音", 1.5),
               ("语义", 1.5), ("aigc", 1.0), ("数字人", 1.0), ("nlp", 1.0),
               ("chatbot", 1.0), ("copilot", 1.0)]

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def build_search_url(keyword, city_code):
    return f"https://www.zhipin.com/web/geek/job?{urlencode({'query': keyword, 'city': city_code})}"

def extract_jobs(body):
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return []
    out = []
    for it in (body or {}).get("zpData", {}).get("jobList", []) or []:
        out.append({
            "title": it.get("jobName", ""),
            "salary": it.get("salaryDesc", ""),
            "corp": it.get("brandName", ""),
            "city": it.get("cityName", ""),
            "loc": it.get("areaDistrict", ""),
            "exp": it.get("jobExperience", ""),
            "degree": it.get("jobDegree", ""),
            "industry": it.get("brandIndustry", ""),
            "skills": "、".join(it.get("skillLabels", []) or it.get("skills", []) or []),
            "scale": it.get("brandScaleName", "") or it.get("brandScale", "") or "",
            "eid": it.get("encryptJobId", ""),
        })
    return out

def collect(page, timeout):
    got = []
    while True:
        try:
            r = page.listen.wait(timeout=timeout)
        except Exception:
            break
        if not (r and getattr(r, "response", None)):
            break
        jobs = extract_jobs(getattr(r.response, "body", None))
        if not jobs:
            continue
        got.extend(jobs)
    return got

def scrape_combo(page, keyword, city_code, max_scrolls=2):
    """一个 关键词×城市 组合：打开搜索页 + 滚动，拦截 API。只读操作。"""
    page.listen.start(LISTEN_TARGET)
    try:
        page.get(build_search_url(keyword, city_code))
        time.sleep(random.uniform(2.0, 3.5))
        jobs = collect(page, timeout=8)
        for _ in range(max_scrolls):
            if random.random() < 0.3:
                try:
                    page.scroll.to_bottom()
                except Exception:
                    page.run_js("window.scrollBy(0, document.body.scrollHeight)")
            else:
                page.scroll.down(random.randint(800, 1400))
            time.sleep(random.uniform(0.8, 1.6))
            jobs.extend(collect(page, timeout=3))
    finally:
        try:
            page.listen.stop()
        except Exception:
            pass
    return jobs

def parse_salary_k(sal):
    """'15-30K·15薪' -> (15+30)/2 = 22.5；解析失败返回 0"""
    m = re.match(r"(\d+)-(\d+)K", sal or "")
    if m:
        return (int(m.group(1)) + int(m.group(2))) / 2
    m = re.match(r"(\d+)K", sal or "")
    return float(m.group(1)) if m else 0.0

def relevant(j):
    text = f"{j['title']} {j['skills']}".upper()
    if not (any(k.upper() in text for k in AI_TERMS)
            and any(k.upper() in text for k in ROLE_TERMS)):
        return False, "不相关"
    title = j["title"].upper()
    for b in BLACKLIST:
        if b.upper() in title:
            return False, f"黑名单:{b}"
    if not j["eid"]:
        return False, "无encryptId"
    if j["city"] not in CITIES:
        return False, f"城市:{j['city']}"
    return True, ""

def score(j):
    text = f"{j['title']} {j['skills']}".lower()
    sc = 8.0
    hit = sum(w for kw, w in SCORE_HINTS if kw in text)
    sc += min(hit, 6.0)
    k = parse_salary_k(j["salary"])
    if k >= 25: sc += 3
    elif k >= 20: sc += 2
    elif k >= 15: sc += 1
    elif 0 < k < 12: sc -= 1
    if "1000" in (j["scale"] or "") or "10000" in (j["scale"] or ""): sc += 1
    sc = max(5, min(18, round(sc)))
    return sc

def to_board_job(j):
    sc = score(j)
    return {
        "id": j["eid"], "p": "S" if sc >= 11 else ("A" if sc >= 8 else "B"),
        "sc": sc, "t": j["title"], "corp": j["corp"], "loc": j["loc"],
        "city": j["city"], "sal": j["salary"], "scale": j["scale"],
        "tags": j["skills"][:60], "note0": "", "jd": j["eid"], "src": "r",
    }

def refresh_all(page, board_html, max_new_per_round=10, keywords=None, cities=None,
                existing_ids=None, progress_ids=None):
    """跑一轮快刷：全部 关键词×城市 组合 → 过滤 → 合并看板。返回 (新增列表, 统计)"""
    keywords = keywords or KEYWORDS
    cities = cities or CITIES
    existing_ids = existing_ids or set()
    progress_ids = progress_ids or set()
    seen, uniq = set(), []
    for kw in keywords:
        for cname, ccode in cities.items():
            raw = scrape_combo(page, kw, ccode)
            log(f"  {kw} × {cname}: 抓到 {len(raw)} 条")
            for j in raw:
                if j["eid"] and j["eid"] not in seen:
                    seen.add(j["eid"])
                    uniq.append(j)
            time.sleep(random.uniform(2.5, 5.0))
    ok, rej = [], 0
    for j in uniq:
        good, why = relevant(j)
        if good:
            ok.append(j)
        else:
            rej += 1
    cand = [to_board_job(j) for j in ok]
    fresh = [c for c in cand if c["id"] not in existing_ids and c["id"] not in progress_ids]
    fresh.sort(key=lambda c: (-c["sc"], c["id"]))
    fresh = fresh[:max_new_per_round]
    added = merge_into_board(board_html, fresh) if fresh else 0
    stat = {"seen": len(uniq), "relevant": len(ok), "rejected": rej,
            "fresh": len(fresh), "added_to_board": added}
    log(f"  刷新统计: 去重{len(uniq)} 相关{len(ok)} 拒绝{rej} 新增看板{added}")
    return fresh, stat

def merge_into_board(board_html, new_jobs):
    """把新岗位 append 进看板 const JOBS（先备份，原子写）。返回实际写入数"""
    p = Path(board_html)
    html = p.read_text(encoding="utf-8")
    m = re.search(r"const JOBS = (\[.*?\]);", html, re.S)
    if not m:
        log("!! 看板里找不到 const JOBS")
        return 0
    jobs = json.loads(m.group(1))
    have = {j["id"] for j in jobs}
    add = [j for j in new_jobs if j["id"] not in have]
    if not add:
        return 0
    jobs.extend(add)
    bak = p.with_suffix(p.suffix + ".bak")
    if not bak.exists():
        bak.write_text(html, encoding="utf-8")
    nj = json.dumps(jobs, ensure_ascii=False, separators=(",", ":"))
    html = html[:m.start(1)] + nj + html[m.end(1):]
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(p)
    return len(add)

def scrape_combo_deep(page, keyword, city_code, max_scrolls=25, target=60):
    """全量深抓：滚动到无新增或达标（用于首次建库）"""
    page.listen.start(LISTEN_TARGET)
    jobs = []
    try:
        page.get(build_search_url(keyword, city_code))
        time.sleep(random.uniform(2.0, 3.5))
        jobs.extend(collect(page, timeout=8))
        zero = 0
        for s in range(max_scrolls):
            if len(jobs) >= target:
                break
            if random.random() < 0.3:
                try:
                    page.scroll.to_bottom()
                except Exception:
                    page.run_js("window.scrollBy(0, document.body.scrollHeight)")
            else:
                page.scroll.down(random.randint(900, 1500))
            time.sleep(random.uniform(0.8, 1.7))
            got = collect(page, timeout=3)
            jobs.extend(got)
            zero = zero + 1 if not got else 0
            if zero >= 5:
                break
    finally:
        try:
            page.listen.stop()
        except Exception:
            pass
    return jobs

def full_scrape(page, keywords=None, cities=None, per_combo_target=60):
    """全量抓取所有 关键词×城市，返回去重后的原始岗位列表"""
    keywords = keywords or KEYWORDS
    cities = cities or CITIES
    seen, uniq = set(), []
    for kw in keywords:
        for cname, ccode in cities.items():
            raw = scrape_combo_deep(page, kw, ccode, target=per_combo_target)
            log(f"  深抓 {kw} × {cname}: {len(raw)} 条")
            for j in raw:
                if j["eid"] and j["eid"] not in seen:
                    seen.add(j["eid"])
                    uniq.append(j)
            time.sleep(random.uniform(2.5, 5.0))
    return uniq

def scrape_to_board_jobs(raw):
    """原始抓取 → 过滤 → 打分 → 看板字段（不查重，供首次建库用）"""
    ok = []
    for j in raw:
        good, _ = relevant(j)
        if good:
            ok.append(to_board_job(j))
    ok.sort(key=lambda c: (-c["sc"], c["id"]))
    return ok

def prune_offline(board_html, progress):
    """从看板移除已标记下架(offline)的岗位 —— 看板 render 已有丢岗防护，安全"""
    p = Path(board_html)
    if not p.exists():
        return 0
    dead = {jid for jid, v in (progress or {}).get("jobs", {}).items()
            if v.get("st") == "offline"}
    if not dead:
        return 0
    html = p.read_text(encoding="utf-8")
    m = re.search(r"const JOBS = (\[.*?\]);", html, re.S)
    if not m:
        return 0
    jobs = json.loads(m.group(1))
    keep = [j for j in jobs if j["id"] not in dead]
    removed = len(jobs) - len(keep)
    if not removed:
        return 0
    bak = p.with_suffix(p.suffix + ".bak")
    if not bak.exists():
        bak.write_text(html, encoding="utf-8")
    nj = json.dumps(keep, ensure_ascii=False, separators=(",", ":"))
    html = html[:m.start(1)] + nj + html[m.end(1):]
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(p)
    return removed

def generate_board(template_path, board_path, jobs, store_key="kanban-boss-apply"):
    """从模板生成全新看板"""
    tpl = Path(template_path).read_text(encoding="utf-8")
    if "__JOBS_JSON__" not in tpl:
        raise RuntimeError("模板缺少 __JOBS_JSON__ 占位符")
    html = tpl.replace("__JOBS_JSON__",
                       json.dumps(jobs, ensure_ascii=False, separators=(",", ":")))
    html = html.replace("__STORE_KEY__", store_key)
    bp = Path(board_path)
    if bp.exists():
        bak = bp.with_suffix(bp.suffix + ".bak")
        if not bak.exists():
            bak.write_text(bp.read_text(encoding="utf-8"), encoding="utf-8")
    bp.write_text(html, encoding="utf-8")
    return len(jobs)


def cmd_init(cfg):
    if CONFIG_FILE.exists():
        print(f"配置已存在: {CONFIG_FILE}")
    else:
        load_config()
        print(f"已生成默认配置: {CONFIG_FILE}")
    print("\n下一步（改完配置再跑）:")
    print(f"  1. 编辑 {CONFIG_FILE}:")
    print("     keywords  想投的岗位关键词（如 [\"AI产品经理\"]）")
    print("     cities    城市和BOSS城市码（如 {\"上海\": \"101020100\"}）")
    print("     board_html 生成的看板保存位置")
    print("  2. python apply_boss.py login    扫码登录")
    print("  3. python apply_boss.py scrape   全量抓岗位 → jobs.json")
    print("  4. python apply_boss.py board    生成看板 HTML")
    print("  5. python apply_boss.py auto     每日自动投递+边投边补新岗")
    return 0

def cmd_scrape(cfg, per_city=60):
    page = start_browser(cfg)
    if not session_ok(page):
        log("!! 会话无效，请先 login")
        return 3
    log(f"全量深抓: {len(cfg.get('keywords', KEYWORDS))} 关键词 × "
        f"{len(cfg.get('cities', CITIES))} 城市, 每组合目标 {per_city} 条 ...")
    raw = full_scrape(page,
                         keywords=cfg.get("keywords") or None,
                         cities=cfg.get("cities") or None,
                         per_combo_target=per_city)
    jobs = scrape_to_board_jobs(raw)
    out = DATA_DIR / "jobs.json"
    out.write_text(json.dumps(
        {"scraped_at": datetime.now().isoformat(timespec="seconds"),
         "count": len(jobs), "jobs": jobs},
        ensure_ascii=False, indent=1), encoding="utf-8")
    s = sum(1 for j in jobs if j["p"] == "S")
    a = sum(1 for j in jobs if j["p"] == "A")
    print(f"抓到 {len(jobs)} 个相关岗位 (S档{s} A档{a}) → {out}")
    print("下一步: python apply_boss.py board  生成看板")
    return 0

def cmd_board(cfg, source=None):
    src = Path(source) if source else DATA_DIR / "jobs.json"
    if not src.exists():
        print(f"!! 找不到 {src} —— 先跑 python apply_boss.py scrape")
        return 1
    jobs = json.loads(src.read_text(encoding="utf-8")).get("jobs", [])
    if not jobs:
        print("!! jobs.json 里没有岗位")
        return 1
    tpl = Path(__file__).resolve().parent / "board_template.html"
    if not tpl.exists():
        print(f"!! 找不到模板 {tpl}")
        return 1
    slug = re.sub(r"\W+", "-", Path(cfg["board_html"]).stem) or "boss-apply"
    n = generate_board(tpl, cfg["board_html"], jobs,
                         store_key=f"kanban-{slug.lower()}")
    print(f"看板已生成: {cfg['board_html']}  ({n} 条岗位)")
    print("用浏览器打开即可使用；右上「导入投递」可同步自动投递进度")
    return 0

def cmd_refresh(cfg, priority=None):
    rcfg = cfg["refresh"]
    page = start_browser(cfg)
    if not session_ok(page):
        log("!! 会话无效，请先 login")
        return 3
    jobs = load_board_jobs(cfg)
    progress = load_progress()
    log("开始快刷（5 城 × 2 关键词，只读搜索页）...")
    fresh, stat = refresh_all(page, cfg["board_html"],
                                 max_new_per_round=rcfg["max_new_per_round"],
                                 existing_ids={j["id"] for j in jobs},
                                 progress_ids=set(progress.get("jobs", {}).keys()))
    print(json.dumps({"added_to_board": len(fresh), "stat": stat,
                      "preview": fresh[:15]}, ensure_ascii=False, indent=1))
    return 0

def cmd_status(cfg):
    prog = load_progress()
    today = datetime.now().strftime("%Y-%m-%d")
    jobs = prog.get("jobs", {})
    cnt = {}
    for v in jobs.values():
        s = v.get("st", "?")
        cnt[s] = cnt.get(s, 0) + 1
    print(f"今日已投: {prog.get('daily', {}).get(today, 0)} / 上限 {cfg['daily_cap']}")
    print(f"累计处理: {len(jobs)} 条  " + "  ".join(f"{k}={v}" for k, v in sorted(cnt.items())))
    if STATUS_FILE.exists():
        print("最近状态:", STATUS_FILE.read_text(encoding="utf-8")[:300])
    return 0

def cmd_export(cfg):
    prog = load_progress()
    data = {"exported_at": datetime.now().isoformat(timespec="seconds"),
            "jobs": prog.get("jobs", {})}
    BOARD_IMPORT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    print(f"已生成 {BOARD_IMPORT_FILE}")
    return 0

def main():
    ap = argparse.ArgumentParser(description="BOSS直聘自动投递")
    ap.add_argument("cmd", nargs="?", default="doctor",
                    choices=["init", "doctor", "login", "scrape", "board",
                             "dry-run", "apply", "auto", "refresh",
                             "status", "export"])
    ap.add_argument("--per-city", type=int, default=60, help="scrape 每城市目标条数")
    ap.add_argument("--from", dest="from_file", default=None,
                    help="board 从指定 jobs.json 生成")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--priority", default=None, help="逗号分隔, 如 S,A")
    ap.add_argument("--ids", default=None, help="逗号分隔的 job id")
    ap.add_argument("--min-delay", type=float, default=None)
    ap.add_argument("--max-delay", type=float, default=None)
    a = ap.parse_args()
    cfg = load_config()
    if a.min_delay is not None:
        cfg["min_delay_s"] = a.min_delay
    if a.max_delay is not None:
        cfg["max_delay_s"] = a.max_delay
    pri = a.priority.split(",") if a.priority else None
    ids = [x.strip() for x in a.ids.split(",") if x.strip()] if a.ids else None
    if a.cmd == "doctor":
        return cmd_doctor(cfg)
    if a.cmd == "init":
        return cmd_init(cfg)
    if a.cmd == "scrape":
        return cmd_scrape(cfg, per_city=a.per_city)
    if a.cmd == "board":
        return cmd_board(cfg, source=a.from_file)
    if a.cmd == "dry-run":
        return cmd_apply(cfg, limit=a.limit, priority=pri, ids=ids, dry_run=True)
    if a.cmd == "login":
        return cmd_login(cfg)
    if a.cmd == "apply":
        return cmd_apply(cfg, limit=a.limit, priority=pri, ids=ids)
    if a.cmd == "auto":
        return cmd_apply(cfg, limit=a.limit, priority=pri, ids=ids, refresh=True)
    if a.cmd == "refresh":
        return cmd_refresh(cfg, priority=pri)
    if a.cmd == "status":
        return cmd_status(cfg)
    if a.cmd == "export":
        return cmd_export(cfg)
    return 0

if __name__ == "__main__":
    sys.exit(main())
