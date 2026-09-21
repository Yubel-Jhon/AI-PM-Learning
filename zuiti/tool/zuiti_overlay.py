# -*- coding: utf-8 -*-
"""
「嘴替」悬浮窗 —— 本地独立脚本

工作方式（钉钉没有发送前拦截接口，这里用系统级模拟逼近该效果）：
  你在钉钉输入框写好草稿 → 按全局热键(默认 Alt+Z)
  → 脚本抓选中文字(走剪贴板) → 本地 Ollama 改写 → 粘回输入框
  → (可选)自动回车发出；开预览时弹窗，Enter发/Esc弃/Alt+R换一版

⚠️ 提醒：开启“自动发出”后，万一改写不满意也已经发出去了，风险自负。
     想稳一点就在悬浮窗里切到“预览:开”。

依赖： pip install keyboard pyautogui pyperclip requests pillow
运行： python zuiti_overlay.py
"""

import json
import os
import re
import sys
import time
import queue
import threading

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

import pyautogui
import pyperclip
import requests

try:
    from PIL import Image, ImageDraw, ImageTk
    HAS_PIL = True
except Exception:
    HAS_PIL = False

try:
    import context as ctxmod          # 本地记忆库读取（可选，缺库则只走通用）
    HAS_CTX = True
except Exception:
    ctxmod = None
    HAS_CTX = False

try:
    import keyboard  # 全局热键（Windows 一般无需管理员）
except Exception:
    print("缺少 keyboard 库：pip install keyboard")
    sys.exit(1)

# 出错时不因坐标异常直接崩溃
pyautogui.FAILSAFE = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "ollama_host": "http://localhost:11434",
    "model": "qwen2.5:7b",
    "provider": "api",          # 引擎二选一："api"=云端大模型(质量档) / "ollama"=本地模型(隐私档)
    "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",  # 任意 OpenAI 兼容端点都行
    "api_model": "qwen3.8-flash",   # api 档用的模型；key 放 local_settings.json（不入库）。嫌慢换 qwen-plus
    "hotkey": "alt+z",
    "mode": "polish",
    "recipient": "none",
    "conversation": "通用",
    "auto_enter": False,
    "send_key": "enter",        # 有的环境发消息是 ctrl+enter，可改这里
    "temperature": 0.5,         # 改写求稳，偏高会跑飞；“换一版”会临时 +0.25
    "num_predict": 160,         # 50 字成稿用不了 300；stop 换行也防多版本输出
    "keep_alive": "30m",        # 模型常驻内存，避免每次 Alt+Z 冷启动干等
    "num_ctx": 4096,            # Ollama 默认 2048，带记忆库上下文会爆
    "mimic_me": True,           # 自我卡：从库里你的历史消息提炼口吻，让成稿像你本人
    "my_name": "",              # 你在钉钉里的名字；空=自动检测（不准就在这里写死）
    "req_timeout": 60,
    "key_pause": 0.12,
    "grace_seconds": 1.2,       # 预览关时的反悔缓冲；按 Esc 取消发送
    "start_expanded": False     # 启动时是否展开（默认收起成小图标，不挡内容）
}

MODE_LABELS = {
    "polish": "高情商",
    "firm": "强硬",
    "drama": "损一点",
}

# 悬浮窗只用“改写你要发的话”这一类模式；reply(帮我回)走聊天里的 zuiti 技能
BAR_MODES = ["polish", "firm", "drama"]

MODE_DESC = {
    "polish": "把你想说的话，改得体面、有分寸，截图也不怕。",
    "firm": "立场更硬，先把边界钉死，但不失礼。",
    "drama": "更有梗、更损一点，却还是发得出去的体面话。",
}

# 对方是谁 —— 嘴替的灵魂：同样一句改写，对老板和对同事分寸完全不同
RECIPIENTS = {
    "none": "不指定",
    "boss": "老板",
    "peer": "同事",
    "subordinate": "下属",
    "client": "客户",
    "vendor": "服务商",
    "friend": "朋友",
    "group": "群",
}
RECIPIENT_CTX = {
    "none": "",
    "boss": " 对方是你的上级/老板：先给结论、语气尊重、不卑不亢，别显得顶撞或推责。",
    "peer": " 对方是平级同事：可以随意些，但把责任边界和该谁做说清楚。",
    "subordinate": " 对方是你的下属：清晰给方向、可指派，但别居高临下，给对方面子。",
    "client": " 对方是客户/外部：专业、克制、留余地，绝不承诺没定的时间和范围。",
    "vendor": " 对方是服务商/乙方：明确诉求与验收标准，客气但不松口不该松的。",
    "friend": " 对方是关系好的同事/朋友：可以轻松口语，但别留下能被截图做文章的把柄。",
    "group": " 这是多人可见的群聊：更收敛，对事不对人，避免点名冲突。",
}

# ---------- 提示词 v2 ----------
# 结构：通用规则 + 模式分支 + 该模式的示例 + 对象分寸 (+ 可选对话上下文)。
# 7B 本地模型对示例极敏感：给足“好成稿长什么样”，比堆指令管用。
# 反 AI 味禁令参考 humanizer-zh；"先识别对方招式再落笔" 参考 zuiti-jiafang / reply-for-me。

BASE_RULES = (
    "你是在职场混了十年的“嘴替”，替用户把要发出去的话改写成对方接得住、截图也不怕的版本。\n"
    "动笔前先判断对方那句话在干什么（甩锅 / 催你 / 压你 / 试探 / 正常沟通），"
    "成稿落在事情和下一步上，不落在情绪上；该有的立场一点不少。\n"
    "硬规则：\n"
    "1. 成稿不超过50字，像真人随手打的消息，不像客服话术。\n"
    "2. 禁用：亲、在吗、收到~、好的呢、感谢您的理解与支持、我理解您的感受、"
    "给您带来不便/困扰、请您放心、如有任何问题、高度重视、深表歉意。\n"
    "3. 不写空洞承诺（尽快、及时跟进、争取早日），除非原话里有明确时间。\n"
    "4. 不编造事实，不添加未被授权的时间/范围/金额承诺，拿不准就写“我确认后答复你”。\n"
    "5. 不解释、不排比堆砌、不写积极的空话结尾，最多1个emoji。\n"
    "6. 只输出成稿本身：无引号、无前缀、无说明。\n"
)

# 各语气的分支规则（polish 不加分支，走通用规则+示例）
MODE_RULES = {
    "polish": "",
    "firm": " 这一版立场要硬：先把边界和事实钉死，语气克制但不含糊，绝不阴阳怪气。",
    "drama": " 这一版带点梗和损劲儿：放松、口语、可以调侃，但截图出去不致命、不针对个人。",
}

# 每档 3 条示例（原话→成稿）。示例即风格锚，是输出质量的最大杠杆。
MODE_EXAMPLES = {
    "polish": (
        "示例（原话→成稿）：\n"
        "原话：这文档写的是啥？错得离谱，到底有没有人看过\n"
        "成稿：文档里有几处口径对不上，我标了三处发你，确认下以哪个为准再往下走。\n"
        "原话：这锅我不背，是你们需求没写清楚\n"
        "成稿：这里和需求确认单对不上，我把记录拉出来对一下，明确了马上排。\n"
        "原话：催什么催，没看到我忙成什么样吗\n"
        "成稿：手头压着两个急活，你的排明天上午；急的话说一声，我调下顺序。"
    ),
    "firm": (
        "示例：\n"
        "原话：凭什么让我白干，工时也不给算\n"
        "成稿：这不在原排期内。要赶就得走加急流程把工时定下来，不然按原计划走。\n"
        "原话：别画饼了，这功能根本做不了\n"
        "成稿：当前架构做不了这个。要么改方案，要么立专项，这周内定一个。\n"
        "原话：你们再这么拖，别怪我不客气\n"
        "成稿：节点是上周定的，再拖影响整体上线。今天下班前给个明确时间。"
    ),
    "drama": (
        "示例：\n"
        "原话：这方案改了八遍了到底谁说了算\n"
        "成稿：第八轮了，建议给这方案立块纪念牌。这轮定一个人拍板，别再全员共创了。\n"
        "原话：又来了？这需求不是上周砍了吗\n"
        "成稿：它又活过来了。这次先把结论落成文字，省得下周它再失踪一回。\n"
        "原话：行行行，都听你们的\n"
        "成稿：那就这么定，决定权和锅一起归位，后面出状况咱一起复盘。"
    ),
}

# ---------- 设计令牌 ----------
PANEL_W, PANEL_H = 324, 236
COLLAPSED_W = COLLAPSED_H = 58   # 收起态：只留吉祥物图标
CORNER = 18
C_BG = "#010203"        # 透明键色（做出圆角）
C_CARD = "#171922"      # 面板底色
C_CARD2 = "#232634"     # 次级底（分段条/悬停）
C_LINE = "#2C3040"      # 描边
C_TEXT = "#ECEEF5"
C_MUTED = "#868CA0"
C_ACCENT = "#4C7DFF"
C_ACCENT2 = "#8A5CFF"   # 收件人行强调色
C_ACCENT_D = "#3A63D8"
C_OK = "#3DDC84"
C_WARN = "#FFC24B"
C_ERR = "#FF6B6B"
C_INFO = "#7CB8FF"

def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# 根据草稿情绪 + 对方关系，自动推荐语气档（保守：只对平级/未知的强火气推“强硬”，其余走高情商）
# 前半是脏字/火气词：lint 查残留、规则兜底照这个洗；后半（离谱/画饼…）是纯火气信号，只参与推档。
# 注意补拼音简写：草稿里“他妈”绝大多数打成 tm，漏一个，降级时脏字就原样发出去了。
_CURSE_WORDS = ["傻", "傻逼", "煞笔", "sb", "滚", "滚蛋", "卧槽", "我操", "我草", "艹",
               "尼玛", "他妈", "他妈的", "妈的", "tm", "tmd", "特么", "脑子", "有病",
               "废物", "垃圾", "智障", "白痴", "去死", "操", "fuck", "shit",
               "放屁", "狗东西", "狗屎", "贱人", "犯贱",
               "离谱", "疯了", "画饼", "甩锅", "背锅", "装死", "什么玩意", "服了"]


def suggest_mode(draft, recipient):
    d = draft or ""
    angry = any(w in d for w in _CURSE_WORDS)
    excl = sum(d.count(c) for c in "！!？?")
    rhetorical = any(k in d for k in ("到底", "凭什么", "是不是你", "会不会", "有没有"))
    hot = angry or (rhetorical and excl >= 2) or excl >= 3
    if recipient in ("boss", "client", "vendor", "group", "subordinate"):
        return "polish"
    if hot and recipient in ("peer", "friend", "none"):
        return "firm"
    return "polish"


class DarkDropdown(tk.Frame):
    """深色自定义下拉：按钮 + 弹出列表，支持分组标题/分隔线。"""

    def __init__(self, master, get_label, items, on_pick, font, small_font, close_hook=None, **kw):
        super().__init__(master, bg=C_CARD2, highlightthickness=1,
                         highlightbackground=C_LINE, **kw)
        self.get_label = get_label
        self.items = items
        self.on_pick = on_pick
        self.font = font
        self.small_font = small_font
        self.close_hook = close_hook
        self.popup = None
        self._btn = tk.Label(self, text="", bg=C_CARD2, fg=C_TEXT, font=font,
                             anchor="w", cursor="hand2", padx=8)
        self._btn.pack(fill="both", expand=True)
        self._btn.bind("<Button-1>", self._toggle)
        self.refresh()

    def refresh(self):
        self._btn.configure(text="▾  " + self.get_label())

    def _toggle(self, e=None):
        if self.popup:
            self._close()
            return
        p = tk.Toplevel(self)
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        p.configure(bg=C_LINE)
        inner = tk.Frame(p, bg=C_CARD, padx=3, pady=3)
        inner.pack()
        for val, label, kind in self.items:
            if kind == "head":
                tk.Label(inner, text=label, bg=C_CARD, fg=C_MUTED,
                         font=self.small_font, anchor="w").pack(fill="x", padx=8, pady=(6, 2))
            elif kind == "sep":
                tk.Frame(inner, bg=C_LINE, height=1).pack(fill="x", pady=3)
            else:
                b = tk.Label(inner, text=label, bg=C_CARD, fg=C_TEXT, font=self.font,
                             anchor="w", cursor="hand2", padx=10, pady=5)
                b.pack(fill="x")
                b.bind("<Button-1>", lambda e, v=val: self._choose(v))
                b.bind("<Enter>", lambda e, w=b: w.configure(bg=C_ACCENT, fg="#FFFFFF"))
                b.bind("<Leave>", lambda e, w=b: w.configure(bg=C_CARD, fg=C_TEXT))
        p.update_idletasks()
        w = max(p.winfo_reqwidth(), self.winfo_width())
        h = p.winfo_reqheight()
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 2
        # 防止超出屏幕底部：往上弹
        sh = p.winfo_screenheight()
        if y + h > sh - 8:
            y = self.winfo_rooty() - h - 2
        p.geometry(f"{w}x{h}+{x}+{y}")
        p.bind("<Escape>", lambda e: self._close())
        p.bind("<FocusOut>", lambda e: self._close())
        p.focus_set()
        self.popup = p

    def _choose(self, val):
        self._close()
        self.on_pick(val)

    def _close(self):
        if self.popup:
            try:
                self.popup.destroy()
            except Exception:
                pass
            self.popup = None


class ZuitiApp:
    def __init__(self, root):
        self.root = root
        self.cfg = self._load_config()
        self.mode = self.cfg.get("mode", "polish")
        if self.mode not in BAR_MODES:
            self.mode = "polish"
        self.conv = self.cfg.get("conversation", "通用") or "通用"
        self.recipient = self._derive_recipient(self.conv)
        self.auto_mode = bool(self.cfg.get("auto_mode", True))
        self.auto_enter = bool(self.cfg.get("auto_enter", False))
        self.mimic_me = bool(self.cfg.get("mimic_me", True))
        self._cancel = threading.Event()
        self._preview_out = None    # 预览窗口结果
        self._busy = False          # 处理中加锁，防并发
        self.job_q = queue.Queue()
        self._cache = {}            # (草稿,模式,对象,对话)→成稿，防双击热键重复等
        self._ctxblock = ""         # 本次流水线的上下文块（run_pipeline 填充）

        self._build_ui()
        self._register_hotkey()
        self._drain_queue()
        threading.Thread(target=self._warm_model, daemon=True).start()  # 后台预热，首次 Alt+Z 不干等

    # ---------------- 配置 ----------------
    def _load_config(self):
        cfg = dict(DEFAULT_CONFIG)
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg.update(json.load(f))
        except Exception as e:
            print(f"[config] 读取失败，用默认：{e}")
        # API key 只放 local_settings.json（gitignore 排除）或系统环境变量，绝不进 config.json/仓库
        try:
            sp = os.path.join(BASE_DIR, "local_settings.json")
            if os.path.exists(sp):
                with open(sp, "r", encoding="utf-8") as f:
                    sec = json.load(f)
                if sec.get("api_key"):
                    cfg["api_key"] = sec["api_key"]
        except Exception as e:
            print(f"[config] local_settings.json 读取失败：{e}")
        if not cfg.get("api_key"):
            cfg["api_key"] = os.environ.get("DASHSCOPE_API_KEY", "")
        return cfg

    # ---------------- 悬浮窗 UI ----------------
    def _font(self, size, bold=False):
        fam = "Microsoft YaHei UI"
        try:
            return tkfont.Font(family=fam, size=size, weight="bold" if bold else "normal")
        except Exception:
            return tkfont.Font(size=size, weight="bold" if bold else "normal")

    def _make_panel_bg(self, w, h, big=True):
        img = Image.new("RGBA", (w, h), _rgb(C_BG) + (255,))
        d = ImageDraw.Draw(img)
        rad = CORNER if big else 14
        # 主卡片圆角矩形
        d.rounded_rectangle([1, 1, w - 3, h - 3], radius=rad,
                            fill=_rgb(C_CARD) + (255,), outline=_rgb(C_LINE) + (255,), width=1)
        if big:
            # 顶部一条品牌渐变高光
            for i in range(3):
                mix = [int(a + (b - a) * (i / 2)) for a, b in zip(_rgb(C_ACCENT), _rgb("#8A5CFF"))]
                d.line([(CORNER, 2 + i), (w - CORNER, 2 + i)], fill=tuple(mix) + (200,))
        return ImageTk.PhotoImage(img)

    def _load_mascots(self):
        self.mascots = {}
        if not HAS_PIL:
            return
        for st in ("idle", "work", "done", "err"):
            p = os.path.join(BASE_DIR, "assets", f"mascot_{st}.png")
            if os.path.exists(p):
                try:
                    self.mascots[st] = ImageTk.PhotoImage(Image.open(p).convert("RGBA"))
                except Exception:
                    pass

    def _set_mascot(self, state):
        if state not in ("idle", "work", "done", "err"):
            state = "idle"
        img = self.mascots.get(state) or self.mascots.get("idle")
        if img is not None:
            self._set(lambda: self.mascot_lbl.configure(image=img))

    def _bob_tick(self):
        self._bob_i = getattr(self, "_bob_i", 0) + 1
        off = [0, -1, -2, -1][self._bob_i % 4]
        try:
            bx, by = self._mascot_base
            self.mascot_lbl.place(x=bx, y=by + off)
        except Exception:
            pass
        self.root.after(140, self._bob_tick)

    def _build_ui(self):
        r = self.root
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        self.expanded = bool(self.cfg.get("start_expanded", False))
        self._x = sw - (PANEL_W if self.expanded else COLLAPSED_W) - 26
        self._y = sh - (PANEL_H if self.expanded else COLLAPSED_H) - 96
        r.configure(bg=C_BG)

        self._panel_big = self._make_panel_bg(PANEL_W, PANEL_H, big=True) if HAS_PIL else None
        self._panel_small = self._make_panel_bg(COLLAPSED_W, COLLAPSED_H, big=False) if HAS_PIL else None
        try:
            r.attributes("-transparentcolor", C_BG)
        except Exception:
            pass
        self._bg_lbl = tk.Label(r, bd=0, highlightthickness=0, bg=C_BG)
        self._bg_lbl.place(x=0, y=0)

        self._load_mascots()
        self._exp = []  # 仅展开态显示的控件： (widget, place_kwargs)

        def add(widget, **kw):
            self._exp.append((widget, kw))

        # 吉祥物（收起/展开都在；点一下切换，拖一下移动）
        self.mascot_lbl = tk.Label(r, bg=C_CARD, bd=0, highlightthickness=0, cursor="hand2")
        self.mascot_lbl.bind("<Button-1>", self._press)
        self.mascot_lbl.bind("<B1-Motion>", self._drag_move)
        self.mascot_lbl.bind("<ButtonRelease-1>", self._release)

        # 标题 / 副标题 / 收起按钮
        add(tk.Label(r, text="嘴替", bg=C_CARD, fg=C_TEXT,
                     font=self._font(13, True)), x=66, y=14)
        add(tk.Label(r, text="职场全自动嘴替", bg=C_CARD, fg=C_MUTED,
                     font=self._font(8)), x=66, y=36)
        self.collapse_lbl = tk.Label(r, text="收起 ⌄", bg=C_CARD, fg=C_MUTED,
                                     font=self._font(9), cursor="hand2")
        self.collapse_lbl.bind("<Button-1>", lambda e: self._set_expanded(False))
        add(self.collapse_lbl, x=PANEL_W - 66, y=16)

        # 状态点 + 文案
        self._dot = tk.Canvas(r, width=12, height=12, bg=C_CARD, highlightthickness=0, bd=0)
        add(self._dot, x=18, y=66)
        self._dot_id = self._dot.create_oval(3, 3, 9, 9, fill=C_OK, outline="")
        self.status = tk.Label(r, text="就绪", bg=C_CARD, fg=C_MUTED, anchor="w",
                               font=self._font(9))
        add(self.status, x=34, y=64, width=PANEL_W - 50, height=14)
        add(tk.Frame(r, bg=C_LINE, height=1), x=16, y=84, width=PANEL_W - 32)

        # 沟通对象（常用对话→带上下文；角色→定分寸；通用→都不带）
        add(tk.Label(r, text="对象", bg=C_CARD, fg=C_MUTED, font=self._font(8)), x=16, y=104)
        self.dropdown = DarkDropdown(
            r, get_label=self._obj_label, items=self._obj_items(),
            on_pick=self._on_pick_obj, font=self._font(9), small_font=self._font(8))
        add(self.dropdown, x=48, y=98, width=PANEL_W - 64, height=28)

        # 语气（三个旋钮）+ 自动/手动标记
        add(tk.Label(r, text="语气", bg=C_CARD, fg=C_MUTED, font=self._font(8)), x=16, y=140)
        self.auto_badge = tk.Label(r, text="", bg=C_CARD, fg=C_ACCENT, font=self._font(8))
        add(self.auto_badge, x=42, y=140, width=40, height=12)
        seg = tk.Frame(r, bg=C_CARD2)
        add(seg, x=48, y=136, width=PANEL_W - 64, height=38)
        self.mode_btns = {}
        keys = list(BAR_MODES)
        for i, key in enumerate(keys):
            b = tk.Label(seg, text=MODE_LABELS[key], bg=C_CARD2, fg=C_TEXT,
                         font=self._font(10), cursor="hand2")
            b.place(relx=i / len(keys), relwidth=1 / len(keys), relheight=1)
            b.bind("<Button-1>", lambda e, k=key: self.set_mode(k))
            b.bind("<Enter>", lambda e, k=key: self._hover(k, True))
            b.bind("<Leave>", lambda e, k=key: self._hover(k, False))
            self.mode_btns[key] = b

        # 当前语气的大白话说明（随选择变化）
        self.desc = tk.Label(r, text="", bg=C_CARD, fg=C_MUTED, anchor="w",
                             justify="left", font=self._font(8))
        add(self.desc, x=16, y=180, width=PANEL_W - 32, height=18)

        # 胶囊开关：自动发出 / 先预览再发
        self.auto_canvas = tk.Canvas(r, width=52, height=24, bg=C_CARD,
                                     highlightthickness=0, bd=0)
        self.auto_canvas.bind("<Button-1>", lambda e: self.toggle_auto())
        add(self.auto_canvas, x=16, y=204)
        self.auto_lbl = tk.Label(r, text="自动发出", bg=C_CARD, fg=C_MUTED,
                                 anchor="w", font=self._font(9))
        add(self.auto_lbl, x=76, y=208)
        self.hotkey_lbl = tk.Label(r, text="", bg=C_CARD, fg=C_MUTED, anchor="e",
                                   font=self._font(8))
        add(self.hotkey_lbl, x=PANEL_W - 150, y=208, width=134, height=16)

        # 右键菜单
        self.menu = tk.Menu(r, tearoff=0)
        self.menu.add_command(label="展开 / 收起", command=self._toggle_expand)
        self.menu.add_command(label="自动语气（点此切换开/关）", command=self.toggle_auto_mode)
        if HAS_CTX:
            self.menu.add_command(label="", command=self.toggle_mimic)
            self._mimic_mi = self.menu.index("end")
            self._refresh_mimic()
        self.menu.add_command(label="退出", command=self._quit)
        r.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

        self._set_mascot("idle")
        self._redraw_auto()
        self._highlight_mode()
        self._refresh_auto_mode()
        self._apply_layout()
        self._bob_tick()

    def _apply_layout(self):
        r = self.root
        if self.expanded:
            w, h = PANEL_W, PANEL_H
            for wd, kw in self._exp:
                wd.place(**kw)
            self._mascot_base = (14, 10)
            self.mascot_lbl.configure(cursor="fleur")
            if HAS_PIL:
                self._bg_lbl.configure(image=self._panel_big)
                self._bg_lbl.place(x=0, y=0, width=w, height=h)
        else:
            w, h = COLLAPSED_W, COLLAPSED_H
            for wd, _ in self._exp:
                wd.place_forget()
            self._mascot_base = (7, 7)
            self.mascot_lbl.configure(cursor="hand2")
            if HAS_PIL:
                self._bg_lbl.configure(image=self._panel_small)
                self._bg_lbl.place(x=0, y=0, width=w, height=h)
        # 把窗口夹回屏幕内，避免展开时贴边被顶出屏幕
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        self._x = max(4, min(self._x, sw - w - 4))
        self._y = max(4, min(self._y, sh - h - 4))
        r.geometry(f"{w}x{h}+{self._x}+{self._y}")
        self.mascot_lbl.place(x=self._mascot_base[0], y=self._mascot_base[1])
        self._bob_i = 0

    def _toggle_expand(self):
        self.expanded = not self.expanded
        self._apply_layout()

    def _set_expanded(self, on):
        self.expanded = on
        self._apply_layout()

    def _hover(self, key, on):
        b = self.mode_btns[key]
        if key == self.mode:
            return
        b.configure(bg=(C_LINE if on else C_CARD2))

    def _redraw_auto(self):
        cv = self.auto_canvas
        cv.delete("all")
        on = self.auto_enter
        track = C_ACCENT if on else "#3A3F52"
        knob_x = 36 if on else 8
        cv.create_rectangle(2, 6, 50, 18, fill=track, outline="", width=0)
        cv.create_oval(2, 4, 22, 20, fill=track, outline=track)
        cv.create_oval(30, 4, 50, 20, fill=track, outline=track)
        cv.create_rectangle(12, 6, 40, 18, fill=track, outline=track)
        cv.create_oval(knob_x, 5, knob_x + 14, 19, fill="#FFFFFF", outline="")
        self.auto_lbl.configure(
            text="自动发出" if on else "先预览再发",
            fg=C_WARN if on else C_OK)

    def _press(self, e):
        self._drag_moved = False
        self._px, self._py = e.x_root, e.y_root
        self._ox = e.x_root - self.root.winfo_x()
        self._oy = e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        if abs(e.x_root - self._px) + abs(e.y_root - self._py) > 3:
            self._drag_moved = True
        if self._drag_moved:
            self._x = e.x_root - self._ox
            self._y = e.y_root - self._oy
            self.root.geometry(f"+{self._x}+{self._y}")

    def _release(self, e):
        if not getattr(self, "_drag_moved", True):
            self._toggle_expand()

    def _highlight_mode(self):
        for k, b in self.mode_btns.items():
            if k == self.mode:
                b.configure(bg=C_ACCENT, fg="#FFFFFF", font=self._font(10, True))
            else:
                b.configure(bg=C_CARD2, fg=C_TEXT, font=self._font(10))
        self.desc.configure(text=MODE_DESC.get(self.mode, ""))

    def _derive_recipient(self, conv):
        if HAS_CTX and conv and conv != "通用":
            tag = ctxmod.tag_of(conv)
            if tag in RECIPIENTS:
                return tag
        return "none"

    def _obj_label(self):
        if self.conv and self.conv != "通用":
            return self.conv
        if self.recipient and self.recipient != "none":
            return RECIPIENTS[self.recipient]
        return "通用"

    def _obj_items(self):
        items = [("通用", "通用（不带上下文）", "item")]
        convs = ctxmod.list_conversations() if HAS_CTX else []
        if convs:
            items.append(("__h1", "常用对话 · 带记忆", "head"))
            for name, tag in convs:
                items.append((name, name, "item"))
        items.append(("__h2", "按角色定分寸", "head"))
        for k in ("boss", "peer", "subordinate", "client", "vendor", "friend", "group"):
            items.append(("role:" + k, RECIPIENTS[k], "item"))
        return items

    def _on_pick_obj(self, val):
        if val == "通用":
            self.conv, self.recipient = "通用", "none"
        elif val.startswith("role:"):
            self.conv, self.recipient = "通用", val[5:]
        else:
            self.conv, self.recipient = val, self._derive_recipient(val)
        self.dropdown.refresh()
        self._save_cfg()

    def _refresh_auto_mode(self):
        try:
            self.auto_badge.configure(text="· 自动" if self.auto_mode else "· 手动",
                                      fg=C_ACCENT if self.auto_mode else C_WARN)
        except Exception:
            pass

    def toggle_auto_mode(self):
        self.auto_mode = not self.auto_mode
        self._refresh_auto_mode()
        self._save_cfg()

    def _refresh_mimic(self):
        try:
            self.menu.entryconfig(
                self._mimic_mi,
                label=f"模仿我的口吻：{'开' if self.mimic_me else '关'}（自我卡）")
        except Exception:
            pass

    def toggle_mimic(self):
        self.mimic_me = not self.mimic_me
        self._refresh_mimic()
        self._save_cfg()

    def _save_cfg(self):
        try:
            data = dict(self.cfg)
            data.update({"mode": self.mode, "conversation": self.conv,
                         "auto_enter": self.auto_enter, "auto_mode": self.auto_mode,
                         "mimic_me": self.mimic_me})
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _quit(self):
        try:
            self.root.destroy()
        except Exception:
            os._exit(0)

    # ---------------- 线程安全的 UI 更新 ----------------
    def _status(self, text, color=C_TEXT, state=None):
        if state is None:
            state = {C_ERR: "err", C_WARN: "work", C_INFO: "work", C_OK: "done"}.get(color, "idle")

        def apply():
            self.status.configure(text=text[:44], fg=color)
            self._dot.itemconfigure(self._dot_id, fill=color)
        self._set(apply)
        self._set_mascot(state)
        if state in ("done", "err"):
            delay = 1600 if state == "done" else 2600
            self.root.after(delay, lambda: self._set_mascot("idle"))

    def _set(self, fn):
        self.root.after(0, fn)

    def set_mode(self, key):
        if key in MODE_LABELS:
            self.mode = key
            self.auto_mode = False          # 手动点过 → 锁定语气，不再自动切
            self._set(self._highlight_mode)
            self._refresh_auto_mode()
            self._save_cfg()

    def toggle_auto(self):
        self.auto_enter = not self.auto_enter
        self._set(self._redraw_auto)
        self._save_cfg()

    # ---------------- 热键 ----------------
    def _register_hotkey(self):
        try:
            keyboard.add_hotkey(self.cfg["hotkey"], lambda: self.job_q.put("run"))
        except Exception as e:
            self._status(f"快捷键启动失败:{e}", C_ERR, state="err")
        hk = self.cfg["hotkey"].upper().replace("+", " + ")
        self._set(lambda: self.hotkey_lbl.configure(text=f"快捷键 {hk} 触发"))
        # 状态栏把引擎和记忆库情况说出来，别让"没记忆/走错引擎"变成哑巴问题
        eng = self._engine_label()
        if self.cfg.get("provider", "api") == "api" and not self._api_key():
            eng += "（没配 key，回退本地）"
        if HAS_CTX and ctxmod.available():
            st = ctxmod.db_stats()
            tag = f"（{os.path.basename(ctxmod.DB)}）" if os.environ.get("ZUITI_DB") else ""
            self._status(f"就绪 · 引擎 {eng} · 记忆库{tag} {st['convs']}会话/{st['msgs']}条", C_OK, state="idle")
        elif HAS_CTX:
            self._status(f"就绪 · 引擎 {eng} · 未建记忆库，只走通用模式", C_WARN, state="idle")
        else:
            self._status(f"就绪 · 引擎 {eng} · 在钉钉里按热键改写", C_OK, state="idle")

    # ---------------- 主循环 / 任务队列 ----------------
    def _drain_queue(self):
        try:
            while True:
                item = self.job_q.get_nowait()
                if item == "run":
                    if self._busy:
                        self._status("上一条还在处理…", C_WARN, state="work")
                        continue
                    self._busy = True
                    threading.Thread(target=self._guarded_pipeline, daemon=True).start()
        except queue.Empty:
            pass
        self.root.after(80, self._drain_queue)

    def _guarded_pipeline(self):
        try:
            self.run_pipeline()
        finally:
            self._busy = False

    # ---------------- 核心流水线 ----------------
    def run_pipeline(self):
        clip0 = ""
        try:
            clip0 = pyperclip.paste()
        except Exception:
            pass
        self._fg_capture()   # 记下 Alt+Z 时的前台窗口（多半是钉钉），预览窗会抢焦点

        self._status("读你写的…", C_WARN, state="work")
        draft = self._grab_selection()
        if not draft.strip():
            who = self._fg_title()
            if who:
                self._status(f"没读到字：焦点在「{who[:12]}」，先点进钉钉输入框", C_ERR, state="err")
            else:
                self._status("没读到你写的字：先在输入框里全选", C_ERR, state="err")
            return
        if len(draft) > 200:
            self._status("抓到内容过长，可能选错了，已中止", C_ERR, state="err")
            self._restore_clip(clip0)
            return

        self._autopush = False
        if self.auto_mode:
            rec = suggest_mode(draft, self.recipient)
            self._autopush = rec != self.mode   # 预览说明行要交代为什么推档
            if rec != self.mode:
                self.mode = rec
                self._set(self._highlight_mode)
        self._ctxblock = ctxmod.build_context(self.conv) if (HAS_CTX and self.conv and self.conv != "通用") else ""
        ctag = f"·{self.conv}" if self._ctxblock else ""
        self._status(f"正在改写（{MODE_LABELS[self.mode]}{ctag}）…", C_INFO, state="work")
        out, degraded = self._rewrite(draft)
        if not out:
            self._status("没改出内容，原样保留没发", C_ERR, state="err")
            self._restore_clip(clip0)
            return

        if degraded:
            self._status("模型不可用·已本地降级", C_WARN, state="done")
        else:
            self._status("改好了 ✓", C_OK, state="done")

        if self.auto_enter:
            self._paste(out)                     # 覆盖草稿
            self._status("即将发出 · 按 Esc 可取消", C_OK, state="work")
            self._cancel.clear()
            time.sleep(float(self.cfg.get("grace_seconds", 1.2)))
            if self._cancel.is_set():
                self._status("已取消，没发出去", C_WARN, state="idle")
            else:
                self._press_send()
                self._status("已发出 ✓", C_OK, state="done")
        else:
            # 预览模式：先开窗，改写边出字边显示；框里可直接改字，Enter 发的是改过的
            sent, last = self._preview_flow(draft)
            if sent is not None:
                self._focus_back()   # 预览窗把焦点抢走了，先还给钉钉再粘
                self._paste(sent)
                self._press_send()
                self._status("已发出 ✓", C_OK, state="done")
            else:
                self._status("已放弃 · 成稿在剪贴板里", C_WARN, state="idle")
                if last:
                    try:
                        pyperclip.copy(last)
                    except Exception:
                        pass
            return

        self._restore_clip(clip0)

    # ---------------- 改写引擎（提示组装 → 调用 → 质检重试） ----------------
    def _build_system(self, variation=False, lint_feedback=""):
        s = (BASE_RULES + MODE_RULES.get(self.mode, "")
             + MODE_EXAMPLES.get(self.mode, "")
             + RECIPIENT_CTX.get(self.recipient, ""))
        if self._ctxblock:
            s += (" 下面给了你与对方的对话上下文，请结合它保持连贯，别重复对方已知信息，"
                  "也别编造上下文里没有的事实。")
        if self.mimic_me and HAS_CTX:
            excl = list(self._CURSE) + list(self._OUT_BANNED)
            card = ctxmod.style_card(self.cfg.get("my_name") or None, exclude=excl)
            if card:
                s += ("\n" + card +
                      "\n成稿的称呼、句长、语气词都要向上面这个口吻靠，像用户本人发的；"
                      "但硬规则（禁词、不承诺、只输出成稿）优先于风格。")
        if variation:
            s += "\n这次换个角度：措辞和思路都要跟常见改法不同，但立场和事实不变。"
        if lint_feedback:
            s += f"\n注意：你上一版的问题：{lint_feedback}。重新输出，严格避开。"
        return s

    def _lint(self, t):
        """成稿质检，返回违规原因列表；空列表 = 过关。"""
        if not t:
            return ["输出为空"]
        bad = [f"出现客服腔“{w}”" for w in self._OUT_BANNED if w in t]
        low = t.lower()
        if any(w in low for w in self._CURSE):
            bad.append("残留脏字")
        if any(m in t for m in self._META_WORDS):
            bad.append("带了说明性文字")
        emoji = re.findall(r"[\U0001F000-\U0001FAFF☀-➿️]", t)
        if len(emoji) > 1:
            bad.append("emoji超量")
        if len(t) < 4:
            bad.append("太短，没信息量")
        if len(t) > 50:
            bad.append("成稿超过50字，要砍到50字以内")
        return bad

    # ---------- 原意保持度校验：lint 管格式，这里管事实 ----------
    _REFUSE_MARKS = ["做不了", "干不了", "加不了", "办不了", "去不了", "来不了", "帮不了",
                     "接受不了", "不接", "不做", "不批", "不同意", "拒绝", "无法满足"]
    _AGREE_OUT = ["没问题", "可以安排", "马上安排", "立刻安排", "安排上", "这就去",
                  "包在我身上", "交给我", "保证完成"]
    _AGREE_IN = ["好的", "没问题", "同意", "就这么办", "行吧", "安排上", "OK", "ok"]
    _REFUSE_OUT = ["做不了", "办不了", "干不了", "不行", "拒绝", "无法", "没法", "不接受", "帮不了"]
    _TIME_DAY = ("今天", "明天", "后天", "本周", "这周", "下周", "上周",
                 "周一", "周二", "周三", "周四", "周五", "周六", "周日", "周末",
                 "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
    _TIME_PART = ("上午", "下午", "中午", "晚上")

    def _fact_check(self, draft, out):
        """查成稿有没有把事实改了：拒绝变答应、数字凭空出现、时间口径漂移。
        返回违规原因列表（措辞直接喂给重试器）；丢弃细节不算违规。"""
        bad = []
        d, o = draft or "", out or ""
        if not d or not o:
            return bad
        if any(w in d for w in self._REFUSE_MARKS) and any(w in o for w in self._AGREE_OUT):
            bad.append("原话是拒绝，成稿变成了答应")
        if any(w in d for w in self._AGREE_IN) and any(w in o for w in self._REFUSE_OUT):
            bad.append("原话是答应，成稿变成了拒绝")
        d_nums = set(re.findall(r"\d+(?:\.\d+)?", d))
        for num in re.findall(r"\d+(?:\.\d+)?", o):
            if num not in d_nums:
                bad.append(f"成稿出现了原话没有的数字{num}，可能构成新承诺")
                break
        d_day = {t for t in self._TIME_DAY if t in d}
        o_day = {t for t in self._TIME_DAY if t in o}
        if d_day and o_day - d_day:
            bad.append("时间口径变了：" + "、".join(sorted(o_day - d_day)))
        d_part = {t for t in self._TIME_PART if t in d}
        o_part = {t for t in self._TIME_PART if t in o}
        if d_part and o_part - d_part:
            bad.append("时间段变了：" + "、".join(sorted(o_part - d_part)))
        return bad

    def _violations(self, draft, t):
        """成稿总质检 = 格式 lint + 原意保持度。"""
        return self._lint(t) + self._fact_check(draft, t)

    def _rewrite(self, draft, variation=False, on_token=None, on_note=None):
        """改写主入口：缓存 → 模型 → 清洗 → 质检，不合格带原因重试一次。
        on_token(部分成稿) 边出字边回调、on_note(过程说明) 给预览窗；不传则行为同前。
        返回 (成稿, degraded)；模型不可用时走本地规则降级。"""
        key = (draft, self.mode, self.recipient, self.conv)
        if not variation:
            hit = self._cache.get(key)
            if hit:
                if on_note:
                    on_note("✓ 通过（同输入复用上一版）")
                return hit, False
        user_prompt = (self._ctxblock + "\n\n" if self._ctxblock else "") + "【我要发出去的原话】\n" + draft
        temp = float(self.cfg.get("temperature", 0.5)) + (0.25 if variation else 0.0)
        if on_note:
            on_note("正在改写…")
        try:
            out = self._clean(self._call_llm(
                user_prompt, self._build_system(variation=variation), temp,
                on_token=on_token, on_note=on_note))
        except Exception:
            if on_note:
                on_note("模型不可用 → 已换本地保守版")
            return self._trim_len(self._rule_fallback(draft)), True   # 模型不可用 → 本地规则降级
        reasons = self._violations(draft, out)
        if reasons:
            if on_note:
                on_note("⚠ " + "；".join(reasons[:2]) + " → 自动重写中")
            if on_token:
                on_token("")                          # 清掉第一版，别让两版叠在框里
            try:
                out2 = self._clean(self._call_llm(
                    user_prompt,
                    self._build_system(variation=variation, lint_feedback="、".join(reasons[:3])),
                    temp, on_token=on_token, on_note=on_note))
            except Exception:
                out2 = ""
            if out2 and not self._violations(draft, out2):
                out = out2
                if on_note:
                    on_note("✓ 重写后通过")
            elif out and not any(w in out.lower() for w in self._CURSE) and not self._fact_check(draft, out):
                if on_note:                           # 格式小毛病可容忍：第一版已清洗且没改事实
                    on_note("⚠ " + "；".join(reasons[:2]) + " · 小毛病可容忍，保留这版")
            else:
                out = self._rule_fallback(draft)      # 事实被改 → 宁可退回保守规则版
                if on_note:
                    on_note("⚠ 事实被改 → 已换本地保守版，原意优先")
        elif on_note:
            on_note("✓ 通过")
        if len(out) > 50:      # 重写两次仍超长，或兜底稿本身就长：兜底截断，硬规则必须成立
            out = self._trim_len(out)
            if on_note:
                on_note("⚠ 还是超长 → 已截到50字内")
        if not variation:
            if len(self._cache) > 30:
                self._cache.clear()
            self._cache[key] = out
        return out, False

    def _warm_model(self):
        """启动后台把模型拉进内存并 keep_alive，首次 Alt+Z 不用等冷启动。
        只在本地档（ollama）预热；api 档无需预热。"""
        if self.cfg.get("provider", "api") == "api" and self._api_key():
            return
        try:
            requests.post(
                self.cfg["ollama_host"].rstrip("/") + "/api/generate",
                json={"model": self.cfg["model"], "prompt": "就绪", "num_predict": 1,
                      "keep_alive": str(self.cfg.get("keep_alive", "30m")), "stream": False},
                timeout=300)
        except Exception:
            pass

    def _grab_selection(self):
        # 优先抓选区；没选中就全选再复制（多数情况即输入框内文本）
        try:
            before = pyperclip.paste()
        except Exception:
            before = ""
        self._key("ctrl", "c")
        time.sleep(0.15)
        try:
            sel = pyperclip.paste()
        except Exception:
            sel = ""
        if sel.strip() and sel != before:
            return sel
        self._key("ctrl", "a")
        time.sleep(0.15)
        self._key("ctrl", "c")
        time.sleep(0.15)
        try:
            return pyperclip.paste() or ""
        except Exception:
            return ""

    # ---------------- 引擎：api（云端大模型） / ollama（本地模型） ----------------
    # 同一份提示词喂哪个引擎都行；api 质量好，ollama 隐私好（话不出这台电脑）。
    def _api_key(self):
        return self.cfg.get("api_key") or ""

    def _engine_label(self):
        if self.cfg.get("provider", "api") == "api" and self._api_key():
            return f"api·{self.cfg.get('api_model', 'qwen3.8-flash')}"
        return f"本地·{self.cfg.get('model', '')}"

    def _call_llm(self, prompt, system, temperature=None, on_token=None, on_note=None):
        """按 provider 分发。api 档请求失败自动回本地模型再试一次，都不行才走规则兜底。"""
        if self.cfg.get("provider", "api") == "api" and self._api_key():
            try:
                return self._call_api(prompt, system, temperature, on_token)
            except Exception as e:
                if on_note:
                    on_note(f"api 不可用（{str(e)[:40]}）→ 回本地模型")
                return self._call_ollama(prompt, system, temperature, on_token)
        return self._call_ollama(prompt, system, temperature, on_token)

    def _call_api(self, prompt, system, temperature=None, on_token=None):
        """OpenAI 兼容端点（DashScope/GLM…）+ SSE 流式。"""
        url = self.cfg.get("api_base", DEFAULT_CONFIG["api_base"]).rstrip("/") + "/chat/completions"
        stream = on_token is not None
        payload = {
            "model": self.cfg.get("api_model", "qwen3.8-flash"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "temperature": float(self.cfg.get("temperature", 0.5)) if temperature is None else temperature,
            "max_tokens": int(self.cfg.get("num_predict", 160)),
            "stop": ["\n"],   # 成稿就是一行，与 ollama 档口径一致
            "stream": stream,
        }
        # qwen3 系默认开思考模式：思考先烧掉 max_tokens，50 字成稿等到天荒地老
        # （实测：默认首块是 reasoning、13~66s 还超时掉兜底；关掉 3.5s 出稿）。
        # 真想要思考：删掉下一行，并把 num_predict 调到 1024 以上。
        if "qwen3" in payload["model"]:
            payload["enable_thinking"] = False
        headers = {"Authorization": "Bearer " + self._api_key()}
        resp = requests.post(url, json=payload, headers=headers,
                             timeout=float(self.cfg.get("req_timeout", 60)), stream=stream)
        resp.raise_for_status()
        if not stream:
            return ((resp.json().get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        chunks = []
        for line in resp.iter_lines():
            if not line:
                continue
            s = line.decode("utf-8", "ignore")
            if s.startswith("data:"):
                s = s[5:].strip()
            if not s or s == "[DONE]":
                if s == "[DONE]":
                    break
                continue
            try:
                j = json.loads(s)
            except Exception:
                continue
            try:
                piece = j["choices"][0]["delta"].get("content") or ""
            except Exception:
                piece = ""
            if piece:
                chunks.append(piece)
                on_token("".join(chunks))
        return "".join(chunks).strip()

    def _call_ollama(self, prompt, system, temperature=None, on_token=None):
        """on_token 传了就走流式（每收一段回调一次累计成稿），否则一次拿全。"""
        url = self.cfg["ollama_host"].rstrip("/") + "/api/generate"
        stream = on_token is not None
        payload = {
            "model": self.cfg["model"],
            "system": system,
            "prompt": prompt,
            "stream": stream,
            "keep_alive": str(self.cfg.get("keep_alive", "30m")),
            "options": {
                "temperature": float(self.cfg.get("temperature", 0.5)) if temperature is None else temperature,
                "num_predict": int(self.cfg.get("num_predict", 160)),
                "num_ctx": int(self.cfg.get("num_ctx", 4096)),
                "stop": ["\n"],   # 成稿就是一行；换行截断还能防“版本1/版本2”式输出
            },
        }
        resp = requests.post(url, json=payload, timeout=float(self.cfg.get("req_timeout", 60)),
                             stream=stream)
        resp.raise_for_status()
        if not stream:
            return (resp.json().get("response") or "").strip()
        chunks = []
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                j = json.loads(line.decode("utf-8", "ignore"))
            except Exception:
                continue
            piece = j.get("response") or ""
            if piece:
                chunks.append(piece)
                on_token("".join(chunks))
            if j.get("done"):
                break
        return "".join(chunks).strip()

    # 输出清洗：剥引号/前缀、并空白、去重复标点、卡 50 字
    _LEAD_LABELS = ("回复：", "回复:", "改写：", "改写:", "改写后：", "改写后:", "高情商版：", "高情商版:",
                    "成稿：", "成稿:", "优化：", "优化:", "优化后：", "优化后:",
                    "嘴替：", "嘴替:", "结果：", "结果:", "好的，", "好的,",
                    "以下是改写：", "以下是改写:", "以下是改写后的版本：", "以下是改写后的版本:")

    # 成稿质检黑名单：客服腔/讨好腔（参考 humanizer-zh 的 AI 痕迹清单），命中即带原因重试
    _OUT_BANNED = ["亲，", "在吗", "收到~", "好的呢", "感谢您的理解", "给您带来", "我理解您",
                   "请您放心", "如有任何问题", "高度重视", "深表歉意", "尽快跟进", "及时跟进"]
    _META_WORDS = ("改写", "成稿：", "原话：", "版本1", "版本一", "以下是")

    def _clean(self, text):
        if not text:
            return ""
        t = text.strip()
        q = "\"'“”‘’「」『』《》"
        # 前缀标签和引号可能叠穿（“好的，以下是改写：\n"..."”），剥到稳定为止
        for _ in range(3):
            changed = False
            while len(t) >= 2 and t[0] in q and t[-1] in q:
                t = t[1:-1].strip()
                changed = True
            n = t.strip(q).strip()
            if n != t:
                t, changed = n, True
            for lab in self._LEAD_LABELS:
                if t.startswith(lab):
                    t = t[len(lab):].strip()
                    changed = True
                    break
            if not changed:
                break
        t = re.sub(r"([!！?？~～])\1+", r"\1", t)
        # 连续 emoji 只留一个
        t = re.sub(r"([\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F])\1+", r"\1", t)
        t = " ".join(t.split())
        return t

    def _trim_len(self, t):
        """超长的最后一招：按标点断在 50 字内。
        缩短的正当流程是 _lint 报“超过50字”→ 带原因重写；这里只保证硬规则永远成立
        （重写两次仍超长、或本地规则兜底稿本身就长的情况）。"""
        if len(t) <= 50:
            return t
        cut = t[:50]
        for p in ("。", "！", "？", "，", "、", " ", "；", ";"):
            idx = cut.rfind(p)
            if idx >= 20:
                cut = cut[:idx + 1]
                break
        return cut.strip("，、； ")

    # 模型不可用时的本地规则降级：删脏字、软化反问、收敛感叹号，绝不原样发
    # 与模块级 _CURSE_WORDS 分工：那个是"草稿里的火气"（参与推档），这个是"成稿里不许有"（lint + 兜底）。
    # 坑：lint 会把成稿转小写再比，所以这里别放大写变体（"傻B"比不中"傻b"）；
    # 拼音简写（tm/sb/tmd）必须收——草稿里"他妈"九成打成 tm，漏一个兜底就把脏字原样发出去了。
    _CURSE = ["傻逼", "傻b", "煞笔", "沙雕", "sb", "滚蛋", "滚", "卧槽", "我操", "我草",
              "艹", "尼玛", "他妈的", "他妈", "你妈", "妈的", "tm", "tmd", "特么", "脑子",
              "有病", "废物", "垃圾", "智障", "白痴", "去死", "操", "操你", "fuck", "shit",
              "放屁", "狗东西", "狗屎", "贱人", "犯贱"]

    def _rule_fallback(self, text):
        t = text or ""
        # 长的先删："他妈的"先于"他妈"，不然"他妈的又甩锅"会剩个"的又甩锅"
        for w in sorted(self._CURSE, key=len, reverse=True):
            t = re.sub(re.escape(w), "", t, flags=re.IGNORECASE)   # 忽略大小写：傻B/傻b 一起洗
        t = re.sub(r"到底(是不是|会不会|有没有)[^。！？!?]*[？?]", "这块我需要再确认下。", t)
        t = re.sub(r"(你|您)(是不是|怎么|为什么)[^。！？!?]*[？?]", "这里我有点没对齐，稍后跟你确认。", t)
        t = t.replace("！", "。").replace("!", "。").replace("？？", "？")
        t = re.sub(r"[。]{2,}", "。", t)
        t = " ".join(t.split()).strip()
        if len(t) < 4:      # 删到只剩残片（"傻B玩意"→"玩意"）：给中性的，别发碎片
            t = "这块我确认后答复你。"
        return t

    def _paste(self, text):
        try:
            pyperclip.copy(text)
        except Exception:
            return
        self._key("ctrl", "a")   # 选中刚才的草稿，粘成稿时覆盖
        time.sleep(0.1)
        self._key("ctrl", "v")
        time.sleep(0.15)

    def _press_send(self):
        key = self.cfg.get("send_key", "enter")
        if "+" in key:
            mods, k = key.rsplit("+", 1)
            self._key(mods, k)
        else:
            self._key(key)

    def _key(self, *args):
        pyautogui.hotkey(*args)
        time.sleep(float(self.cfg.get("key_pause", 0.12)))

    def _restore_clip(self, text):
        try:
            if text:
                pyperclip.copy(text)
        except Exception:
            pass

    # ---------------- 预览三件套：说明行 + 流式成稿 + 可编辑发送 ----------------
    # 招式识别走本地规则（与 zuiti-rewrite skill 里“先识别对方招式”同一张表）。
    # 标“疑似”：规则看不出语气，只给预览窗一个交代，不是结论。
    _TACTIC_RULES = (
        ("疑似在甩锅", ("不是我的", "你负责", "你确认过", "当时你们", "需求没写",
                        "需求里没", "归你", "锅")),
        ("疑似在催你", ("什么时候", "好了吗", "几点能", "啥时候", "deadline",
                        "今天能", "明天要", "催什么", "进度")),
        ("疑似在压你", ("必须", "凭什么", "再说一遍", "我说了", "领导说的",
                        "公司规定", "没得商量", "不换人")),
        ("疑似在试探", ("是不是", "能不能", "方便吗", "有没有可能", "帮我个忙", "你看看")),
    )

    def _guess_tactic(self, draft):
        d = draft or ""
        for name, words in self._TACTIC_RULES:
            if any(w in d for w in words):
                return name
        return "正常沟通"

    def _fg_capture(self):
        """记下 Alt+Z 时的前台窗口句柄。预览窗一点就把焦点抢走了，
        发送前不还回去，成稿会粘进预览窗而不是钉钉。"""
        try:
            import ctypes
            self._fg_hwnd = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            self._fg_hwnd = 0

    def _fg_title(self):
        """当前前台窗口的标题。抓不到字时，报“焦点在哪个窗口”比让人瞎猜强——
        演示现场 90% 的失败是热键按下去时前台根本不是钉钉。"""
        try:
            import ctypes
            h = getattr(self, "_fg_hwnd", 0)
            if not h:
                return ""
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetWindowTextW(h, buf, 256)
            return buf.value.strip()
        except Exception:
            return ""

    def _focus_back(self):
        try:
            import ctypes
            if getattr(self, "_fg_hwnd", 0):
                ctypes.windll.user32.SetForegroundWindow(self._fg_hwnd)
                time.sleep(0.12)
        except Exception:
            pass

    def _preview_flow(self, draft):
        """预览模式主流程。三件套：
        ①说明行——对象/语气档（自动推档要交代）/疑似招式；
        ②流式——改写边出字边进文本框，质检不过会当场清框重写，用户全程看得见；
        ③可编辑——出完字框里随便改，Enter 发的是框里当前文字。
        Esc 放弃，Alt+R 拿原始草稿换一版（不在上一版上改，防意思漂移）。
        返回 (发送的文本 or None, 最后一版成稿)。"""
        state = {"busy": True, "closed": False, "sent": None, "last": ""}
        ev = threading.Event()
        built = threading.Event()
        ui = {}

        def safe(fn):
            def apply():
                if state["closed"]:
                    return
                try:
                    fn()
                except (tk.TclError, KeyError):
                    pass          # 窗口已关/控件还没建出来，静默丢掉这次刷新
            self._set(apply)

        def set_text(t):
            txt = ui.get("txt")
            if not txt:
                return
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            txt.insert("1.0", t)
            txt.see("end")
            if state["busy"]:
                txt.configure(state="disabled")

        def on_token(partial):
            safe(lambda: set_text(partial))

        def on_note(msg):
            color = C_OK if msg.startswith("✓") else (C_ERR if "模型不可用" in msg else C_WARN)
            safe(lambda: ui["verdict"].configure(text="质检 " + msg, fg=color))

        def finish(out):
            state["busy"] = False
            state["last"] = out

            def go():
                set_text(out)
                ui["txt"].configure(state="normal")
                ui["txt"].focus_set()
            safe(go)

        def worker(variation):
            try:
                out, _ = self._rewrite(draft, variation=variation,
                                       on_token=on_token, on_note=on_note)
            except Exception:
                out = self._rule_fallback(draft)
                on_note("模型不可用 → 已换本地保守版")
            finish(out)

        def done(send):
            if state["closed"]:
                return
            if send and state["busy"]:
                safe(lambda: ui["verdict"].configure(text="质检 还在改写… 出完字再发", fg=C_WARN))
                return
            state["closed"] = True
            if send:
                txt = ui.get("txt")
                state["sent"] = (txt.get("1.0", "end-1c").strip() if txt else "") or state["last"]
            ev.set()
            try:
                ui["win"].destroy()
            except Exception:
                pass

        def vary():
            if state["busy"] or state["closed"]:
                return
            state["busy"] = True
            safe(lambda: (set_text(""), ui["verdict"].configure(
                text="质检 换个说法，重新改写…", fg=C_INFO)))
            threading.Thread(target=worker, args=(True,), daemon=True).start()

        def head_text():
            mode_lb = MODE_LABELS.get(self.mode, self.mode)
            if getattr(self, "_autopush", False):
                mode_lb += "（检测到火气，自动推档）"
            who = self.conv if (self._ctxblock and self.conv) else "无记忆库上下文"
            tag = RECIPIENTS.get(self.recipient, "")
            if tag and tag != "不指定":
                who += "·" + tag
            return f"{who} ｜ 语气 {mode_lb} ｜ 识别：对方{self._guess_tactic(draft)}"

        def switch_mode(key):
            # 预览窗内换档：拿原始草稿按新档重改（交互参考 openai-translator 卡内模式切换）
            if state["busy"] or state["closed"] or key == self.mode:
                return
            self.set_mode(key)               # 主面板同步 + 落 config；手动选过后不再自动推档
            self._autopush = False
            state["busy"] = True

            def pre():
                h = ui.get("head")
                if h:
                    h.configure(text=head_text())
                for k, b in ui.get("mode_btns", {}).items():
                    b.configure(bg=C_ACCENT if k == key else C_CARD2,
                                fg="#FFFFFF" if k == key else C_TEXT)
                set_text("")
                ui["verdict"].configure(
                    text=f"质检 已切「{MODE_LABELS.get(key, key)}」，拿原始草稿重改…", fg=C_INFO)
            safe(pre)
            threading.Thread(target=worker, args=(False,), daemon=True).start()

        def copy_out():
            # 结果卡复制（参考 pot/openai-translator 的卡上复制键）：拿走文字，不发送
            txt = ui.get("txt")
            if state["busy"] or state["closed"] or not txt:
                return
            s = txt.get("1.0", "end-1c").strip() or state["last"]
            if not s:
                return
            self.root.clipboard_clear()
            self.root.clipboard_append(s)
            safe(lambda: ui["verdict"].configure(text="✓ 已复制到剪贴板（未发送）", fg=C_OK))

        def build():
            win = tk.Toplevel(self.root)
            win.attributes("-topmost", True)
            win.title("嘴替 · 预览")
            win.configure(bg=C_CARD)
            win.geometry("400x345")
            # ① 说明行
            head = tk.Label(win, text=head_text(), bg=C_CARD, fg=C_MUTED, wraplength=372,
                            justify="left", anchor="w", font=self._font(9))
            head.pack(fill="x", padx=14, pady=(12, 4))
            ui["head"] = head
            # 语气档即时切换：当前档高亮，点了拿原始草稿按新档重改
            mrow = tk.Frame(win, bg=C_CARD)
            mrow.pack(fill="x", padx=14, pady=(0, 4))
            tk.Label(mrow, text="换档", bg=C_CARD, fg=C_MUTED, font=self._font(8)).pack(side="left")
            ui["mode_btns"] = {}
            for key in BAR_MODES:
                sel = key == self.mode
                b = tk.Label(mrow, text=MODE_LABELS.get(key, key), cursor="hand2",
                             font=self._font(9), bg=C_ACCENT if sel else C_CARD2,
                             fg="#FFFFFF" if sel else C_TEXT, padx=10, pady=3)
                b.pack(side="left", padx=4)
                b.bind("<Button-1>", lambda e, k=key: switch_mode(k))
                ui["mode_btns"][key] = b
            # ② 流式成稿框（出完字可编辑）
            txt = tk.Text(win, height=5, wrap="word", font=self._font(11),
                          bg=C_CARD2, fg=C_TEXT, insertbackground=C_TEXT,
                          relief="flat", padx=10, pady=8, state="disabled",
                          highlightthickness=1, highlightbackground=C_LINE)
            txt.pack(fill="both", expand=True, padx=14, pady=(0, 4))

            def ret_send(e):
                done(True)
                return "break"        # 别往框里插换行，成稿就是一行
            txt.bind("<Return>", ret_send)
            txt.bind("<KP_Enter>", ret_send)
            ui["txt"] = txt
            # ③ 质检过程行
            ui["verdict"] = tk.Label(win, text="质检 正在改写…", bg=C_CARD, fg=C_INFO,
                                     wraplength=372, justify="left", anchor="w",
                                     font=self._font(9))
            ui["verdict"].pack(fill="x", padx=14)
            tk.Label(win, text="框里可直接改字，Enter 发的就是改过的版本；Alt+1/2/3 快速换档",
                     bg=C_CARD, fg=C_MUTED, font=self._font(8)).pack(fill="x", padx=14)
            fr = tk.Frame(win, bg=C_CARD)
            fr.pack(pady=8)

            def flat(parent, text, cmd, accent=False):
                b = tk.Label(parent, text=text, cursor="hand2", font=self._font(10),
                             bg=C_ACCENT if accent else C_CARD2,
                             fg="#FFFFFF" if accent else C_TEXT, padx=14, pady=6)
                b.pack(side="left", padx=5)
                b.bind("<Button-1>", lambda e: cmd())
                b.bind("<Enter>", lambda e: b.configure(bg=C_ACCENT_D if accent else C_LINE))
                b.bind("<Leave>", lambda e: b.configure(bg=C_ACCENT if accent else C_CARD2))
            flat(fr, "发送  Enter", lambda: done(True), accent=True)
            flat(fr, "算了  Esc", lambda: done(False))
            flat(fr, "换个说法  Alt+R", lambda: vary())
            flat(fr, "复制", copy_out)
            win.bind("<Return>", lambda e: done(True))
            win.bind("<Escape>", lambda e: done(False))
            win.bind("<Alt-r>", lambda e: vary())
            for i, key in enumerate(BAR_MODES, 1):
                win.bind(f"<Alt-Key-{i}>", lambda e, k=key: switch_mode(k))
            win.protocol("WM_DELETE_WINDOW", lambda: done(False))
            # 不设超时自动发：预览模式的意义就是让人拍板，不选就一直等
            ui["win"] = win
            built.set()

        self._set(build)
        if not built.wait(8.0):       # 窗口没建出来（投影仪/资源紧张下 Tk 会慢）：当放弃处理，别空跑改写
            self._status("预览窗没出来，这轮不发；点一下悬浮条重试", C_ERR, state="err")
            return None, ""
        threading.Thread(target=worker, args=(False,), daemon=True).start()
        ev.wait()
        return state["sent"], state["last"]

    def _cancel_send(self):
        self._cancel.set()

    def run(self):
        self.root.mainloop()


def main():
    root = tk.Tk()
    app = ZuitiApp(root)
    try:
        # 反悔缓冲期间的 Esc/Enter 全局监听（仅自动发出模式用得上）
        keyboard.add_hotkey("esc", app._cancel_send)
    except Exception:
        pass
    root.mainloop()


if __name__ == "__main__":
    main()
