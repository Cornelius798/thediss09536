# ascs-renew — ASCS Cloud 免费 VPS 自动续期

给 [ASCS Cloud](https://cloud.ascsonoway.com) 免费套餐 **Micro Instances - i5 3470 Micro Free** 用的自动续期机器人。

免费套餐是 **30 天续期制**（"renew every 30 days to confirm active use"）：每 30 天要去面板里点一次 Renew 确认还在用，到期不续服务器会被回收。站点没有公开 API，只能走网页面板，所以用 Playwright 模拟真人操作：登录 → My Servers → 打开这台服务器 → 读取到期/续期日 → 在续期窗口内点击 Renew 并走完确认流 → 回读日期验证顺延。

## 怎么跑

- **自动**：GitHub Actions 每周一、周四 UTC 02:17 跑一次（`.github/workflows/renew.yml`）。脚本有保护：距到期超过 10 天时只读状态、不点续期；两次/周的频率给站点故障留了重试余地。
- **手动**：仓库页 → Actions → “ASCS 免费 VPS 自动续期” → Run workflow。
  - `dry_run`：默认勾选，只登录读状态、截图，**不点续期**。第一次务必先干跑一遍确认能登录、能找到服务器。
  - `upload_screenshots`：默认不勾。公开仓的 artifact 任何人可下载，排障截图默认不上传；实在需要看图排障时再勾，保留 14 天。

## 配置 Secrets（必须，只填两个）

在本仓库：**Settings → Secrets and variables → Actions → New repository secret**，添加：

| 名称 | 值 |
| --- | --- |
| `ASCS_EMAIL` | ASCS Cloud 登录邮箱 |
| `ASCS_PASSWORD` | ASCS Cloud 登录密码 |

> **安全说明**：Secrets 由 GitHub 加密存储，本仓库是公开仓也看不到、读不到，工作流日志里出现时会被自动打码。本仓库的代码与工作流文件本身不含任何凭据——脚本只从环境变量读 `ASCS_EMAIL` / `ASCS_PASSWORD`，且日志里邮箱只打码显示（如 `z***@live.com`）。请**不要**把密码写进代码、issue 或提交记录里。

## 脚本行为（scripts/renew.py）

- 选择器策略：邮箱/密码框多选择器兜底；服务器按名字含 `3470` 模糊定位；Renew 按文本 `/renew/i` 在按钮/链接里找，并尝试 Manage/Details 进入详情页、Actions 菜单兜底。
- 重试与日志：登录带 3 次重试；每一步打时间戳日志并在 `screenshots/` 留截图（本地运行时可直接看）。失败按阶段分开报：登录失败 / 没找到服务器 / 没找到续期入口 / 续期没生效，退出码分别为 2 / 3 / 4 / 5（0 = 成功，6 = 页面或网络错误，7 = 没配 Secrets）。
- 环境变量：`DRY_RUN=1` 干跑；`RENEW_WINDOW_DAYS`（默认 10）距到期多少天内才续；`ASCS_SERVER_MATCH`（默认 `3470`）服务器名匹配串。

## 本地运行（可选）

```bash
pip install -r requirements.txt
python -m playwright install chromium
ASCS_EMAIL='你的邮箱' ASCS_PASSWORD='你的密码' DRY_RUN=1 python scripts/renew.py
```

干跑通过后去掉 `DRY_RUN=1` 即为真实续期。

## 排障

1. 先手动触发一次并勾 `dry_run`；看运行日志卡在哪一步（日志已按阶段写明）。
2. 需要看页面长什么样：手动触发时勾 `upload_screenshots`，跑完在该次运行页面底部下载 `ascs-renew-screenshots`（注意这是公开仓，截图任何人可下载，看完可在 Actions 里删掉该运行记录）。
3. 常见情况：ASCS 站点本身偶有故障/登录无响应（历史上出现过整站宕机），脚本会以退出码 6 或 2 失败，等下一次定时运行自动重试即可；若连续多次登录失败，先确认密码没被改过、Secrets 没填错。
4. 面板改版导致找不到元素时，截图里的页面结构就是改选择器的依据（`scripts/renew.py` 顶部的选择器列表集中可改）。
