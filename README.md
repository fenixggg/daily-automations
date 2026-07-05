# daily-automations 🔄

日常自动化脚本合集，通过 GitHub Actions 定时运行并推送到飞书群。

## 脚本列表

| 脚本 | 定时 | 说明 |
|------|------|------|
| `scripts/trending.py` | 每天 8:00 | GitHub AI 项目热榜（零依赖，LLM 翻译/分类/解读） |

## 配置 Secrets

在仓库 Settings → Secrets and variables → Actions 中配置：

### 飞书推送（必填）

| Secret | 说明 |
|--------|------|
| `FEISHU_APP_ID` | 飞书自建应用 App ID |
| `FEISHU_APP_SECRET` | 飞书自建应用 App Secret |
| `FEISHU_CHAT_ID` | 飞书目标群聊 ID（以 `oc_` 开头） |

### LLM（二选一）

**方式 A - OpenAI 兼容接口（推荐）**

| Secret | 说明 | 小米示例 |
|--------|------|---------|
| `LLM_API_KEY` | API Key | `sk-xxx` |
| `LLM_BASE_URL` | API 地址 | `https://api.mimo.xiaomi.com/v1` |
| `LLM_MODEL` | 模型名 | `miMo-XX` |

**方式 B - Google Gemini（备用）**

| Secret | 说明 |
|--------|------|
| `GEMINI_API_KEY` | Google AI Studio API Key |

## 添加新脚本

1. 在 `scripts/` 下创建 Python 脚本
2. 在 `.github/workflows/` 下创建对应的 workflow YAML 文件
3. Secrets 共用，无需重复配置

## 手动触发

Actions → 选择对应 workflow → Run workflow
