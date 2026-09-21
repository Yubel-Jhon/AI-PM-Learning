# 小替（zuiti）

> 帮你把话说得体的打字小助手 —— 真心话先打进输入框，按一下 **Alt+Z**，改成既能发出去、又不吃亏的说法。

钉钉草稿改写悬浮窗 + 人物分析 skill。改得体 · 不丢原意 · 认得人。

| 你想打的 | 小替发出去的 |
|---|---|
| 这锅我不背，是你们需求没写清楚 | 这里和需求确认单对不上，我把记录拉出来对一下，明确了马上排。 |
| 凭什么让我白干，工时也不给算 | 这不在原排期内。要赶就得走加急流程把工时定下来，不然按原计划走。 |
| 催什么催，没看到我忙成什么样吗 | 手头压着两个急活，你的排明天上午；急的话说一声，我调下顺序。 |

## 👀 一眼看懂：输入 → 干了什么 → 输出

| 输入 | 干了什么 | 输出 |
|---|---|---|
| 你在输入框里打的**真心话草稿**（想骂就骂）＋ 可选的**本地记忆库**（聊天记录 / 关系卡 / 人物卡） | 按 Alt+Z 触发：**认人**（读人物卡分寸，没卡就看头像识人）→ **判招式、定语气档**（常规/强硬/带损，火气自动推档）→ **改写** → **两层质检**（格式 lint + 原意保持度，不过自动重写） | **得体成稿**弹进预览窗，你拍板：Enter 发送 / Esc 放弃 / Alt+R 换一版；出稿还能手改再发 |

## ✨ 特性

- **一键改写**：Alt+Z 抓草稿 → 改写 → 预览窗拍板（Enter 发 / Esc 放弃 / Alt+R 换一版）
- **三种语气**：高情商 / 强硬 / 带损，检测到火气自动推强硬档，截图不怕
- **两层质检**：格式 lint + 原意保持度（立场不翻转、数字不新增、时间不漂移），不过就自动重写
- **先认人再开口**：人物分析出「人物卡」，改写自动带分寸（MBTI 四维 / 代际 / 头像识人）
- **双引擎**：云端千问 qwen3.8-flash（默认，2.7~3.7 秒）/ 本地 Ollama（隐私档，草稿不出机），失败自动回退
- **本地记忆库**：context.db 分层记忆（关系卡 / 性格概括 / 近期原文），改写自动带前情

## 📦 安装

1. Python 3.8+，双击 `script/安装依赖.bat`（或 `pip install -r requirements.txt`）
2. 引擎二选一，默认云端：
   - **云端**：把 DashScope key 写进 `script/local_settings.json`（`{"api_key": "sk-…"}`，已 gitignore）
   - **本地隐私档**：装 [Ollama](https://ollama.com) 并 `ollama pull qwen2.5:7b`，`script/config.json` 里 `provider` 改 `ollama`

## 🚀 快速开始

双击 `script/启动嘴替.bat` → 在钉钉输入框打完草稿 → 光标放输入框里按 **Alt+Z** → 预览窗拍板。

没有真实数据也想体验？`python script/build_demo_db.py` 生成演示库后，设置环境变量 `ZUITI_DB=demo_context.db` 再启动，对象下拉里就是虚构人设。

## 🔁 工作流程

**首次使用（4 步）**

1. 双击 `script/安装依赖.bat`
2. 把 DashScope key 写进 `script/local_settings.json`（隐私档则改 `script/config.json` 切 ollama）
3. 要记忆库就建库：钉钉聊天记录导出放 `script/dumps/`，跑 `python script/build_context_db.py`（不建也能用，自动走通用模式）
4. 双击 `script/启动嘴替.bat`，收起成右下角小图标，不挡内容

**每条消息（3 步）**：打真心话 → 按 Alt+Z → 预览窗拍板（处理细节见上面「一眼看懂」）

- 预览窗：**Enter** 发送 / **Esc** 放弃 / **Alt+R** 拿原话换一版；出稿后框里可直接改再发
- 记忆库隔段时间刷新：拉新聊天记录 → 重跑 `python script/build_context_db.py` → 悬浮窗右键「重载上下文库」

**认人闭环（越用越准）**

人物分析出「人物卡」→ 落缓存进关系卡 → 每次改写自动带这套分寸 → 实战实况回写卡的「近期观察」→ 下次分析先读它（证实留用、打脸修正）。

## ⚙️ 配置

`script/config.json` 常用项：

| 键 | 说明 |
|---|---|
| `provider` | `api`（云端，默认）/ `ollama`（本地隐私档） |
| `api_model` | 云端模型，默认 `qwen3.8-flash`（qwen3 系已自动关思考，嫌慢可换 `qwen-plus`） |
| `hotkey` | 全局热键，默认 `alt+z` |
| `auto_enter` | `false`（默认）弹预览窗拍板；`true` 自动发出，`grace_seconds` 秒内按 Esc 可反悔 |
| `send_key` | `enter` / `ctrl+enter` |
| `mimic_me` | 自我卡：从你的历史消息学口吻（默认开；`my_name` 建议写死你的钉钉名） |

API key 也可用环境变量 `DASHSCOPE_API_KEY`。全量配置项见 [docs/设计细节.md](docs/设计细节.md)。

## 🧩 配套 Skill（`skill/` 目录）

- **zuiti-rewrite**：改写规则的手动挡 Claude 版（三档语气示例、硬规则、候选模式）
- **chat-persona**：人物分析（五维拼 MBTI 四维、头像识人、人物卡回灌关系卡）
- **zuiti-evolve**：自我进化——会话收尾记经验、"嘴替复盘"改规则、三副本+桌面引擎同步清单（规则级学习回路，经验库种子随 skill 入库）

拷进 `~/.claude/skills/` 即用；想"替你回复对方发来的消息"也用它（说"嘴替 + 贴那条消息"）。规则改动须三处同步（~/.claude/skills ↔ 嘴替-skill/skill/ ↔ 仓库 zuiti/skill/），清单见 zuiti-evolve。

## 🔒 隐私

`dumps/`（聊天记录导出）与 `context.db`（记忆库）只存本地、不入仓库，演示一律用 `demo_context.db` 虚构人设；介意数据上云就切 ollama 档，全程不出机。

## 🛠 自检

出问题先双击 `script/检查环境.bat`：生效库 / 每会话条数 / 自我卡 / config / Ollama 在线情况一次看全，每个 ✗ 都带"修：xxx"。

人物分析方法论、质检细节、记忆库分层等全量说明见 **[docs/设计细节.md](docs/设计细节.md)**，系统全景（分层/生命线/质检/降级/接口）见 **[docs/架构图.html](docs/架构图.html)**。
