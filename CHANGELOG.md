# 变更记录（CHANGELOG）

## [v0.4] - 2026-09-21（zuiti 新版本）

- 新增 `zuiti/skill/`：改写 + 人物分析双 skill 入库，与悬浮窗同一套规则（改动两处同步：`~/.claude/skills` ↔ 仓库）
  - `zuiti-rewrite`：候选模式（说"多来几版"给 A/B/C 三版候选）、给选项式强硬第二招（"您看先砍哪个？"）、判型→档位联动（T 强 J 强 + 火气 → 直接推强硬档）
  - `chat-persona`：五维数据拼 MBTI 四维（主动率→E/I · 句长梗→S/N · emoji 语气→T/F · 时间死板→J/P，每维有数撑着）；尺子③ 升级头像网名信号（三原型对照，权重高于词表）；新增「初见陌生人·头像识人」零语料路径（初版分寸 + 初印象衰减曲线：刚加上≈95% → 三五天≈45% → 一周后只按说话来）
- `zuiti/` 目录重构对齐本地项目：`tool/`（悬浮窗代码+assets）+ `skill/`（Claude skill 双件套）+ README，逻辑不改
- README 更新：配套 skill 说明、人物分析方法论（头像网名三原型表 + 初见识人）

## [v0.3.0] - 2026-08-21（develop，未发布）

- 重构目录：`product/` + `skill/` 的 12 个浅层 README 并入 `curriculum/`（深度详解），删除重复骨架
- `deep/` → `curriculum/`，按 ①→⑪ 顺序重排为 6 个文档，折入原 README 独有术语
- 新增 `cases/`（PRD 14 章方法 + 微信 AI 打车助手 BRD/MRD/FSD/PRD 案例）
- 新增 `interview/`（桌面「面试方法论 + 高频题刷题卡」入库）
- `ppt/` 精简：删脚本/图表/预览，只留 2 个 pptx + 内容稿
- 删除 `wechatauto_logs/`、`xmind/` 日志目录

## [v0.2.0] - 2026-08-14（develop，未发布）

- 每个章节文件夹内新增 `思维导图.png`（对应板块导图图片化，嵌入各 README）
- 删除独立 `xmind/` 目录，导图与章节文档合并为一处

## [v0.1.0] - 2026-08-14

- 初始化目录结构：`product/`（通用底座 ①–⑦ + 附录）+ `skill/`（AI 增补层 ⑧–⑪）
- 建立分支规范：`main` = 发布 / `develop` = 开发，tag 基于 main

## 分支规范（git flow）

- `develop`：日常改动都在这，基于 main 拉取
- `main`：只接受 develop 合并，发布后打 tag
- 流程：develop 改 → merge 回 main → main 打 tag
