---
name: boss-apply
description: BOSS直聘求职全自动流水线 —— 抓岗位 → 生成看板 → 自动「立即沟通/发简历」→ 边投边补新岗、清下架；config 驱动，任何人可用
theme: career-leadership
---

# boss-apply

完整文档（命令/配置/排障/评分规则）看 **README.md**，本文件只留操作要点。

## 用户提这些需求时用本 skill

- 投递 BOSS直聘岗位 / 自动投简历 / 看投递进度
- 抓取 BOSS 岗位 / 更新看板岗位 / 清理下架岗位
- 生成投递看板

## 命令速查

```bash
cd ~/.claude/skills/boss-apply
python apply_boss.py init      # 建配置（用户改 keywords/cities/board_html）
python apply_boss.py doctor    # 体检，任何异常先跑这个
python apply_boss.py login     # 会话失效（need_login）时扫码
python apply_boss.py scrape    # 全量抓岗位 → jobs.json（换方向/换城市后必跑）
python apply_boss.py board     # jobs.json → 全新看板
python apply_boss.py dry-run   # 队列预览，不开浏览器，改配置后先验证
python apply_boss.py auto      # 日常主命令：投递+补新岗+清下架
python apply_boss.py status    # 进度摘要
```

参数：`--limit N`、`--priority S,A`、`--ids`、`--per-city N`、`--min-delay/--max-delay`。

## 硬规则

1. **改任何行为先改配置** `~/.boss-apply/config.json`，别改代码；改完用 `dry-run` 验证
2. **真投递（apply/auto）必须先获用户明确同意**再跑；dry-run/doctor/refresh 随便跑
3. **弹滑块验证要告诉用户去 Chrome 手动滑**，脚本最多等 300s，超时自动中止，别硬闯
4. **中途停止**：建 `~/.boss-apply/STOP` 空文件，当前岗位记录完优雅停，别 kill 进程（会留下点了没记录的悬案）
5. **progress.json 是进度真源**，别手改；看板列位置靠「导入投递」按钮同步（只前进不降级）
6. 单条 3-9 分钟是防风控的真人节奏，不是卡住；看 `status.json` 心跳判断是否卡死
