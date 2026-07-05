# daily-automations 🔄

日常自动化脚本合集，通过 GitHub Actions 定时运行并推送到飞书群。

## 脚本列表

| 脚本 | 定时 | 说明 |
|------|------|------|
| `scripts/trending.py` | 每天 8:00 | GitHub Trending 热门项目日报（零依赖，纯标准库） |

## 配置 Secrets

在仓库 Settings → Secrets and variables → Actions 中配置：

| Secret | 说明 |
|--------|------|
| `FEISHU_APP_ID` | 飞书自建应用 App ID |
| `FEISHU_APP_SECRET` | 飞书自建应用 App Secret |
| `FEISHU_CHAT_ID` | 飞书目标群聊 ID（以 `oc_` 开头） |
| `GEMINI_API_KEY` | Google Gemini API Key（免费，用于翻译/分类/解读） |

## 添加新脚本

1. 在 `scripts/` 下创建 Python 脚本
2. 在 `.github/workflows/` 下创建对应的 workflow YAML 文件
3. Secrets 共用，无需重复配置

## 手动触发

Actions → 选择对应 workflow → Run workflow

## 许可证

MIT
