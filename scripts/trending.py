#!/usr/bin/env python3
"""
GitHub AI 项目热榜 - 爬取热门项目，LLM 翻译/分类/解读，推送到飞书群

用法：
  python scripts/trending.py

环境变量（LLM 二选一）：
  方式 A - OpenAI 兼容接口（推荐）：
    LLM_API_KEY     API Key
    LLM_BASE_URL    API 地址，如 https://api.mimo.xiaomi.com/v1
    LLM_MODEL       模型名，如 miMo-XX
  方式 B - Google Gemini（备用）：
    GEMINI_API_KEY  Google Gemini API Key

  飞书推送（必填）：
    FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_CHAT_ID

依赖：无（仅使用 Python 标准库）
"""

import os
import json
import sys
import re
import html as html_mod
import urllib.request
import urllib.error
from datetime import datetime

# ========== 配置 ==========
FEISHU_APP_ID = os.environ.get('FEISHU_APP_ID')
FEISHU_APP_SECRET = os.environ.get('FEISHU_APP_SECRET')
FEISHU_CHAT_ID = os.environ.get('FEISHU_CHAT_ID')

# LLM 配置：优先用 OpenAI 兼容接口，回退到 Gemini
LLM_API_KEY = os.environ.get('LLM_API_KEY')
LLM_BASE_URL = os.environ.get('LLM_BASE_URL', '').rstrip('/')
LLM_MODEL = os.environ.get('LLM_MODEL', '')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# 爬取页面前 N 条（需要多爬一些，因为后面会过滤掉非 AI 项目）
SCRAPE_COUNT = 25
# 最终推送条数上限
MAX_RESULTS = 10

# 消息底部署名
MODEL_SIGNATURE = "Powered by WorkBuddy / DeepSeek-V4-Flash"


# ========== 通用 HTTP 工具 ==========

def http_request(url, data=None, headers=None, timeout=30):
    """通用 HTTP 请求，返回 (body_bytes, status_code)"""
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent",
                   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if data:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.read(), resp.getcode()
    except urllib.error.HTTPError as e:
        err = e.read()
        return err, e.code
    except urllib.error.URLError as e:
        print(f"  ❌ 网络错误: {e.reason}")
        return None, -1


# ========== GitHub Trending 爬取 ==========

def get_trending():
    """爬取 GitHub Trending 页面，返回 Top N 项目列表"""
    print("  → 请求 https://github.com/trending ...")
    body, code = http_request("https://github.com/trending")
    if code != 200 or body is None:
        print(f"  ❌ 请求失败, HTTP {code}")
        return []

    page = body.decode("utf-8", errors="replace")
    repos = []
    articles = page.split('<article class="Box-row')

    for article in articles[1:]:
        href_match = re.search(
            r'<h2[^>]*>.*?<a[^>]*href="(/[^"]+)"', article, re.DOTALL
        )
        if not href_match:
            href_match = re.search(
                r'href="(/\w+/[\w.-]+)"[^>]*class="[^"]*Link[^"]*"', article
            )
        if not href_match:
            continue
        repo_path = href_match.group(1).strip().lstrip("/")

        desc_match = re.search(
            r'<p class="col-9[^"]*"[^>]*>(.*?)</p>', article, re.DOTALL
        )
        description = ""
        if desc_match:
            raw = re.sub(r'<[^>]+>', '', desc_match.group(1))
            description = html_mod.unescape(raw).strip()

        lang_match = re.search(
            r'itemprop="programmingLanguage">(.*?)</span>', article
        )
        language = lang_match.group(1).strip() if lang_match else ""

        stars_match = re.search(r'([\d,]+)\s*stars?\s*(today|this\s*week)', article)
        if not stars_match:
            stars_match = re.search(r'([\d,]+)\s*stars?\b', article)
        stars_str = stars_match.group(1).replace(",", "") if stars_match else "0"
        try:
            stars_week = int(stars_str)
        except ValueError:
            stars_week = 0

        repos.append({
            "repo": repo_path,
            "url": f"https://github.com/{repo_path}",
            "description": description,
            "language": language,
            "stars_week": stars_week,
        })
        if len(repos) >= SCRAPE_COUNT:
            break

    print(f"  → 解析到 {len(repos)} 个项目")
    return repos


# ========== LLM 调用（通用） ==========

BUILD_PROMPT = """你是一个 GitHub Trending 分析助手。请分析以下 {count} 个热门开源项目。

对每个项目返回：
1. translation: 项目描述的简短中文翻译（1-2句话）
2. category: 分类标签，从以下选一个：
   AI Agent, AI 开发工具, AI 编程助手, AI 应用, AI 多模态, AI 大模型,
   开发者工具, 效率工具, 前端/UI, 系统编程, 数据/ML, 安全, 移动开发, 通用
3. interpretation: 一句中文解读，说明这个项目解决了什么问题，或为什么火（10-20字）

请严格按 JSON 数组格式输出，不要有其他文字：
[
  {{"rank": 1, "translation": "...", "category": "...", "interpretation": "..."}},
  ...
]

项目列表：
{items}"""


def build_prompt(repos):
    """构建 LLM prompt"""
    lines = []
    for i, r in enumerate(repos, 1):
        lang_info = f"  Language: {r['language']}" if r['language'] else ""
        desc = r['description'][:200] if r['description'] else "(no description)"
        lines.append(f"{i}. Repo: {r['repo']}\n   Description: {desc}{lang_info}")
    return BUILD_PROMPT.format(
        count=len(repos),
        items="\n".join(lines)
    )


def parse_llm_response(text, repos):
    """从 LLM 响应文本中解析 JSON 并合并到 repos"""
    json_match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
    if json_match:
        text = json_match.group(1)
    text = text.strip()
    analysis = json.loads(text)

    for item in analysis:
        rank = item.get('rank', 0)
        if 1 <= rank <= len(repos):
            idx = rank - 1
            repos[idx]['translation'] = item.get('translation', repos[idx]['description'])
            repos[idx]['category'] = item.get('category', '')
            repos[idx]['interpretation'] = item.get('interpretation', '')
    return repos


def llm_analyze(repos):
    """
    调用 LLM（优先 OpenAI 兼容接口，回退 Gemini）对每个项目进行：
      - 中文翻译
      - 分类标签
      - 一句中文解读
    """
    prompt = build_prompt(repos)

    if LLM_API_KEY and LLM_BASE_URL and LLM_MODEL:
        return _call_openai_compat(repos, prompt)
    elif GEMINI_API_KEY:
        return _call_gemini(repos, prompt)
    else:
        print("  ⚠️ 未配置 LLM（需 LLM_API_KEY/BASE_URL/MODEL 或 GEMINI_API_KEY）")
        print("  ⚠️ 降级使用原始英文描述")
        return _fallback(repos)


def _call_openai_compat(repos, prompt):
    """通过 OpenAI 兼容接口调用 LLM"""
    url = f"{LLM_BASE_URL}/chat/completions"
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 2048,
    }).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json; charset=utf-8",
    }

    print(f"  → 调用 OpenAI 兼容接口 ({LLM_BASE_URL}) ...")
    resp_body, code = http_request(url, data=body, headers=headers, timeout=120)

    if code != 200:
        print(f"  ❌ LLM API 返回 HTTP {code}: {(resp_body or b'')[:200]}")
        return _fallback(repos)

    try:
        result = json.loads(resp_body.decode("utf-8"))
        text = result.get('choices', [{}])[0].get('message', {}).get('content', '')
        if not text:
            raise ValueError("响应中无 content")
        repos = parse_llm_response(text, repos)
        print(f"  ✅ OpenAI 兼容接口分析完成")
    except Exception as e:
        print(f"  ⚠️ LLM 解析失败: {e}")
        return _fallback(repos)

    return repos


def _call_gemini(repos, prompt):
    """调用 Google Gemini API"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048},
    }).encode("utf-8")

    print("  → 调用 Gemini API ...")
    resp_body, code = http_request(url, data=body, timeout=60)

    if code != 200:
        print(f"  ❌ Gemini API 返回 HTTP {code}: {(resp_body or b'')[:200]}")
        return _fallback(repos)

    try:
        result = json.loads(resp_body.decode("utf-8"))
        text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
        if not text:
            raise ValueError("响应中无 text")
        repos = parse_llm_response(text, repos)
        print(f"  ✅ Gemini 分析完成")
    except Exception as e:
        print(f"  ⚠️ Gemini 解析失败: {e}")
        return _fallback(repos)

    return repos


def _fallback(repos):
    """LLM 不可用时降级处理"""
    for r in repos:
        r['translation'] = r['description']
        r['category'] = ''
        r['interpretation'] = ''
    return repos


# ========== 飞书推送 ==========

def get_feishu_token():
    """获取飞书 tenant_access_token"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    body = json.dumps({
        "app_id": FEISHU_APP_ID,
        "app_secret": FEISHU_APP_SECRET,
    }).encode("utf-8")
    resp_body, code = http_request(url, data=body, headers={"Content-Type": "application/json"})
    data = json.loads(resp_body.decode("utf-8"))
    if data.get('code') != 0:
        print(f"  ❌ 获取 token 失败: {data}")
        sys.exit(1)
    return data['tenant_access_token']


def send_feishu_message(token, title, post_content):
    """发送富文本消息到飞书群"""
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    body = json.dumps({
        "receive_id": FEISHU_CHAT_ID,
        "msg_type": "post",
        "content": json.dumps({
            "zh_cn": {"title": title, "content": post_content}
        }, ensure_ascii=False),
    }, ensure_ascii=False).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    resp_body, code = http_request(url, data=body, headers=headers)
    return json.loads(resp_body.decode("utf-8"))


# ========== 消息排版 ==========

def build_post_content(repos):
    """构建飞书 post 消息"""
    lines = []

    lines.append([
        {"tag": "text", "text": f"⭐ GitHub AI 项目热榜（近 7 天新增 Star 排行）\n"
                                 f"共 {len(repos)} 个项目  |  数据来源: GitHub Trending\n"}
    ])

    cats = {}
    for r in repos:
        c = r.get('category', '') or '通用'
        cats[c] = cats.get(c, 0) + 1

    for i, r in enumerate(repos, 1):
        cat_tag = f" — {r['category']}" if r.get('category') else ""
        lang_info = r['language'] if r['language'] else "多语言"
        stars_str = f"{r['stars_week']:,}" if r['stars_week'] > 0 else "N/A"

        lines.append([
            {"tag": "text", "text": f"\n{i}."},
            {"tag": "a", "text": r['repo'], "href": r['url']},
            {"tag": "text", "text": cat_tag},
        ])
        lines.append([
            {"tag": "text", "text": f"🔧 技术栈: {lang_info}"},
        ])
        lines.append([
            {"tag": "text", "text": f"⭐ Stars: {stars_str}（近 7 天新增）"},
        ])

        translation = r.get('translation', '') or r['description']
        if translation:
            lines.append([
                {"tag": "text", "text": f"💡 简介: {translation[:150]}{'...' if len(translation) > 150 else ''}"},
            ])
        interp = r.get('interpretation', '')
        if interp:
            lines.append([
                {"tag": "text", "text": f"📝 解读: {interp}"},
            ])

    cat_summary = "、".join(f"{k}({v})" for k, v in
                           sorted(cats.items(), key=lambda x: -x[1]))
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    lines.append([
        {"tag": "text", "text": f"\n📊 热门领域: {cat_summary}"}
    ])
    lines.append([
        {"tag": "text", "text": f"总项目数: {len(repos)}  |  生成时间: {now}  |  AI 分析"}
    ])
    lines.append([
        {"tag": "text", "text": f"{MODEL_SIGNATURE}"}
    ])

    return lines


# ========== 主流程 ==========

def main():
    print("=" * 50)
    print("⭐ GitHub AI 项目热榜 — 日报生成器")
    print("=" * 50)

    # 1. 检查飞书环境变量
    missing = [k for k, v in [
        ("FEISHU_APP_ID", FEISHU_APP_ID),
        ("FEISHU_APP_SECRET", FEISHU_APP_SECRET),
        ("FEISHU_CHAT_ID", FEISHU_CHAT_ID),
    ] if not v]
    if missing:
        print(f"❌ 缺少飞书环境变量: {', '.join(missing)}")
        sys.exit(1)

    # 2. 爬取 Trending
    print("\n📡 爬取 GitHub Trending...")
    repos = get_trending()
    if not repos:
        print("  ⚠️ 没有抓到数据，跳过推送")
        return

    # 3. LLM 分析
    print("\n🤖 调用 LLM 分析...")
    repos = llm_analyze(repos)

    # 4. 过滤：只保留 AI 相关项目
    ai_categories = {'AI Agent', 'AI 开发工具', 'AI 编程助手', 'AI 应用', 'AI 多模态', 'AI 大模型'}
    ai_repos = [r for r in repos if r.get('category') in ai_categories]
    skipped = len(repos) - len(ai_repos)
    ai_repos = ai_repos[:MAX_RESULTS]
    print(f"  → 过滤后: {len(ai_repos)} 个 AI 项目（已过滤 {skipped} 个非 AI 项目）")

    if not ai_repos:
        print("  ⚠️ 没有 AI 相关项目，跳过推送")
        return

    # 5. 推送飞书
    print("\n📤 获取飞书 Token...")
    token = get_feishu_token()

    print("📤 推送到飞书群...")
    title = f"GitHub AI 项目热榜 | {datetime.now().strftime('%Y-%m-%d')}"
    content = build_post_content(ai_repos)
    result = send_feishu_message(token, title, content)

    if result.get('code') == 0:
        print(f"\n✅ 推送成功！共 {len(ai_repos)} 个 AI 项目")
    else:
        print(f"\n❌ 推送失败: {json.dumps(result, ensure_ascii=False, indent=2)}")
        sys.exit(1)


if __name__ == '__main__':
    main()
