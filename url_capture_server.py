from __future__ import annotations

import json
import os
import re
import socket
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

import requests
from bs4 import BeautifulSoup


WORKFLOW_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = WORKFLOW_ROOT / "config.ps1"
HTML_PATH = WORKFLOW_ROOT / "url-capture.html"
RUNTIME_ROOT = WORKFLOW_ROOT / "runtime"
HISTORY_PATH = RUNTIME_ROOT / "url_capture_history.json"
NOTION_META_PATH = RUNTIME_ROOT / "notion_capture_meta.json"
INDEX_PATH = RUNTIME_ROOT / "capture_index.json"
CANVAS_PATH = RUNTIME_ROOT / "canvas_board.json"

DEFAULT_VAULT = Path.home() / "Documents" / "Obsidian Vault"
DEFAULT_LLM_MODEL = "configured-model"
LEGACY_ZHIPU_MODEL = "glm-5.1"
LEGACY_ZHIPU_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DEFAULT_PORT = 8765
NOTION_VERSION = "2026-03-11"
NOTION_API_BASE = "https://api.notion.com/v1"
PREVIEW_TTL_MINUTES = 30
CONFIG_KEYS = [
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_API_URL",
    "LLM_API_BASE_URL",
    "LLM_PROVIDER",
    "LLM_AUTH_HEADER",
    "LLM_AUTH_SCHEME",
    "LLM_RESPONSE_FORMAT",
    "LLM_TEMPERATURE",
    "LLM_EXTRA_HEADERS_JSON",
    "LLM_EXTRA_BODY_JSON",
    "ZHIPU_API_KEY",
    "ZHIPU_MODEL",
    "NOTION_API_TOKEN",
    "NOTION_PARENT_PAGE_ID",
    "OBSIDIAN_VAULT_PATH",
    "URL_CAPTURE_PORT",
]

CATEGORY_CONFIG = {
    "ai_tools": {"label": "AI工具", "icon": "🤖", "color": "blue"},
    "product_insights": {"label": "产品思考", "icon": "🎯", "color": "orange"},
    "video_notes": {"label": "视频笔记", "icon": "🎬", "color": "purple"},
    "reference_library": {"label": "资料库", "icon": "📚", "color": "green"},
    "industry_observations": {"label": "行业观察", "icon": "🛰️", "color": "red"},
}

PLATFORM_CONFIG = {
    "website": {"label": "网站", "color": "blue"},
    "video": {"label": "视频", "color": "purple"},
    "wechat": {"label": "公众号", "color": "green"},
    "document": {"label": "文档", "color": "gray"},
}

PLATFORM_CONFIG.update(
    {
        "audio": {"label": "Audio / 音频", "color": "yellow"},
        "image": {"label": "Image / 图片", "color": "pink"},
    }
)

CONTENT_TYPE_CONFIG = {
    "webpage": {"label": "Web Page / 网页", "color": "blue"},
    "online_video": {"label": "Online Video / 在线视频", "color": "purple"},
    "online_audio": {"label": "Online Audio / 在线音频", "color": "yellow"},
    "document": {"label": "Document / 文档", "color": "gray"},
    "image": {"label": "Image / 图片", "color": "pink"},
}

CATEGORY_LABELS = {key: value["label"] for key, value in CATEGORY_CONFIG.items()}
PLATFORM_LABELS = {key: value["label"] for key, value in PLATFORM_CONFIG.items()}
CATEGORY_ICONS = {value["label"]: value["icon"] for value in CATEGORY_CONFIG.values()}

NOTION_DATABASE_TITLE = "网页采集日历"
NOTION_CALENDAR_VIEW_NAME = "采集日历"
NOTION_GALLERY_VIEW_NAME = "精选卡片"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

PREVIEW_CACHE: dict[str, dict[str, Any]] = {}
PREVIEW_CACHE_LOCK = Lock()


def load_config(config_path: Path = CONFIG_PATH) -> dict[str, str]:
    if not config_path.exists():
        return {}

    text = config_path.read_text(encoding="utf-8")
    values: dict[str, str] = {}
    for key in CONFIG_KEYS:
        match = re.search(rf"\$env:{re.escape(key)}\s*=\s*([\"'])(.*?)\1", text)
        values[key] = match.group(2) if match else ""
    return values


def config_value(config: dict[str, str], *keys: str, default: str = "") -> str:
    for key in keys:
        value = os.environ.get(key) or config.get(key, "")
        if str(value).strip():
            return str(value).strip()
    return default


def bool_config_value(config: dict[str, str], *keys: str, default: bool = True) -> bool:
    raw = config_value(config, *keys)
    if not raw:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "none", "disabled"}


def summary_model(config: dict[str, str]) -> str:
    model = config_value(config, "LLM_MODEL", "ZHIPU_MODEL")
    if model:
        return model
    if config_value(config, "ZHIPU_API_KEY"):
        return LEGACY_ZHIPU_MODEL
    return DEFAULT_LLM_MODEL


def summary_provider(config: dict[str, str]) -> str:
    return config_value(config, "LLM_PROVIDER", default="openai-compatible")


def summary_api_url(config: dict[str, str]) -> str:
    explicit_url = config_value(config, "LLM_API_URL")
    if explicit_url:
        return explicit_url

    base_url = config_value(config, "LLM_API_BASE_URL")
    if base_url:
        return base_url.rstrip("/") + "/chat/completions"

    if config_value(config, "ZHIPU_API_KEY", "ZHIPU_MODEL"):
        return LEGACY_ZHIPU_API_URL

    raise ValueError("LLM_API_URL or LLM_API_BASE_URL is missing")


def ensure_runtime() -> None:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def sanitize_file_name(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', " ", value or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:90] or "未命名笔记"


def yaml_string(value: str) -> str:
    return json.dumps(value or "", ensure_ascii=False)


def parse_datetime(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def to_date_string(value: str) -> str:
    if not value:
        return ""
    match = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    return match.group(1) if match else ""


def first_meta(soup: BeautifulSoup, *selectors: tuple[str, str]) -> str:
    for attr, key in selectors:
        tag = soup.find("meta", attrs={attr: key})
        if tag and tag.get("content"):
            return normalize_whitespace(tag["content"])
    return ""


def extract_site_label(hostname: str) -> str:
    hostname = (hostname or "").lower().strip()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


def infer_media_provider(url: str, site: str) -> str:
    haystack = f"{url} {site}".lower()
    providers = [
        (r"youtube|youtu\.be", "YouTube"),
        (r"bilibili|b23\.tv", "Bilibili"),
        (r"douyin|iesdouyin", "Douyin"),
        (r"tiktok", "TikTok"),
        (r"vimeo", "Vimeo"),
        (r"kuaishou", "Kuaishou"),
        (r"xiaohongshu|xhslink", "Xiaohongshu"),
        (r"spotify", "Spotify"),
        (r"soundcloud", "SoundCloud"),
        (r"podcasts\.apple|podcasts\.google|podcast", "Podcast"),
        (r"ximalaya|xima", "Ximalaya"),
        (r"weixin|mp\.weixin", "WeChat"),
    ]
    for pattern, label in providers:
        if re.search(pattern, haystack):
            return label
    return site or "unknown"


def infer_content_type(url: str, site: str, og_type: str = "", content_type: str = "", media_url: str = "") -> str:
    haystack = f"{url} {site} {og_type} {content_type} {media_url}".lower()
    path = urlparse(url).path.lower()
    if re.search(r"\.(mp4|mov|m4v|webm|mkv|avi)(?:$|\?)", path):
        return "online_video"
    if re.search(r"\.(mp3|wav|m4a|aac|flac|ogg)(?:$|\?)", path):
        return "online_audio"
    if re.search(r"\.(jpg|jpeg|png|webp|gif|bmp|svg)(?:$|\?)", path):
        return "image"
    if re.search(r"\.(pdf|doc|docx|ppt|pptx|xls|xlsx)(?:$|\?)", path) or "application/pdf" in haystack:
        return "document"
    if re.search(r"og:video|video|youtube|youtu\.be|bilibili|douyin|tiktok|vimeo|kuaishou", haystack):
        return "online_video"
    if re.search(r"og:audio|audio|podcast|spotify|soundcloud|ximalaya", haystack):
        return "online_audio"
    if re.search(r"image/", haystack):
        return "image"
    return "webpage"


def platform_from_content_type(content_type: str, fallback: str = "website") -> str:
    mapping = {
        "online_video": "video",
        "online_audio": "audio",
        "document": "document",
        "image": "image",
        "webpage": fallback or "website",
    }
    return mapping.get(content_type, fallback or "website")


def media_processing_status(content_type: str, content: str, description: str) -> str:
    text_len = len(normalize_whitespace(content))
    if content_type in {"online_video", "online_audio"}:
        if text_len > max(900, len(description) + 500):
            return "metadata_plus_page_text"
        return "metadata_only_needs_transcript"
    if content_type in {"document", "image"} and text_len < 400:
        return "metadata_only"
    return "page_text_extracted"


def title_from_url(url: str, site: str) -> str:
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name or "").strip()
    if name:
        name = re.sub(r"\.[A-Za-z0-9]{2,6}$", "", name)
        name = re.sub(r"[-_]+", " ", name)
        return normalize_whitespace(name) or site
    return site or "Untitled source"


def build_direct_source(url: str, site: str, content_type: str) -> dict[str, str]:
    title = title_from_url(url, site)
    media_provider = infer_media_provider(url, site)
    content_type_label = CONTENT_TYPE_CONFIG.get(content_type, {}).get("label", content_type)
    content = "\n".join(
        [
            f"Direct source link: {url}",
            f"Content type: {content_type_label}",
            f"Media provider: {media_provider}",
            "Extraction status: metadata_only",
            "Transcript/text note: the file itself was not downloaded or transcribed in this preview step.",
        ]
    )
    return {
        "title": title,
        "site": site,
        "description": "",
        "author": "",
        "cover_image": url if content_type == "image" else "",
        "published_at": "",
        "content_type": content_type,
        "content_type_label": content_type_label,
        "media_provider": media_provider,
        "media_url": url if content_type in {"online_video", "online_audio", "image"} else "",
        "processing_status": "metadata_only",
        "og_type": "",
        "response_content_type": "",
        "content": content,
        "url": url,
    }


def score_candidate(node: Any) -> tuple[int, str]:
    pieces: list[str] = []
    paragraph_count = 0
    for tag in node.find_all(["h1", "h2", "h3", "p", "li", "blockquote"]):
        text = normalize_whitespace(tag.get_text(" ", strip=True))
        if len(text) < 12:
            continue
        pieces.append(text)
        if tag.name == "p":
            paragraph_count += 1
    combined = "\n\n".join(pieces)
    score = len(combined) + paragraph_count * 160
    return score, combined


def extract_article(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    site = extract_site_label(parsed.netloc)
    direct_content_type = infer_content_type(url, site)
    if direct_content_type != "webpage" and re.search(
        r"\.(mp4|mov|m4v|webm|mkv|avi|mp3|wav|m4a|aac|flac|ogg|jpg|jpeg|png|webp|gif|bmp|svg|pdf|doc|docx|ppt|pptx|xls|xlsx)(?:$|\?)",
        parsed.path.lower(),
    ):
        return build_direct_source(url, site, direct_content_type)

    response = requests.get(url, headers=REQUEST_HEADERS, timeout=25)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(
        ["script", "style", "nav", "footer", "aside", "form", "button", "noscript", "svg", "iframe"]
    ):
        tag.decompose()

    title = (
        first_meta(soup, ("property", "og:title"), ("name", "twitter:title"))
        or normalize_whitespace(soup.title.get_text(" ", strip=True) if soup.title else "")
        or normalize_whitespace(soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")
        or site
    )
    description = first_meta(soup, ("name", "description"), ("property", "og:description"))
    author = first_meta(
        soup,
        ("name", "author"),
        ("property", "article:author"),
        ("name", "twitter:creator"),
    )
    cover_image = first_meta(soup, ("property", "og:image"), ("name", "twitter:image"))
    published_at = (
        first_meta(soup, ("property", "article:published_time"), ("name", "publish_date"))
        or parse_datetime(first_meta(soup, ("name", "pubdate"), ("name", "date")))
    )
    og_type = first_meta(soup, ("property", "og:type"), ("name", "twitter:card"))
    media_url = first_meta(
        soup,
        ("property", "og:video"),
        ("property", "og:video:url"),
        ("property", "og:audio"),
        ("property", "og:audio:url"),
        ("name", "twitter:player"),
    )
    response_content_type = str(response.headers.get("Content-Type") or "").split(";")[0].lower()

    candidates: list[Any] = []
    seen_ids: set[int] = set()
    selectors = [
        "article",
        "main",
        "[itemprop='articleBody']",
        "[role='main']",
        ".article",
        ".article-content",
        ".post-content",
        ".entry-content",
        ".rich-text",
        ".content",
    ]
    for selector in selectors:
        for node in soup.select(selector):
            if id(node) not in seen_ids:
                candidates.append(node)
                seen_ids.add(id(node))
    if soup.body and id(soup.body) not in seen_ids:
        candidates.append(soup.body)

    best_text = ""
    best_score = -1
    for node in candidates:
        score, text = score_candidate(node)
        if score > best_score:
            best_score = score
            best_text = text

    if not best_text:
        best_text = normalize_whitespace(soup.get_text(" ", strip=True))

    content = best_text[:18000].strip()
    if description and description not in content:
        content = f"{description}\n\n{content}".strip()

    content_type = infer_content_type(url, site, og_type, response_content_type, media_url)
    media_provider = infer_media_provider(url, site)
    processing_status = media_processing_status(content_type, content, description)
    media_context = [
        f"Content type: {CONTENT_TYPE_CONFIG.get(content_type, {}).get('label', content_type)}",
        f"Media provider: {media_provider}",
        f"Extraction status: {processing_status}",
    ]
    if media_url:
        media_context.append(f"Media URL: {media_url}")
    if content_type in {"online_video", "online_audio"}:
        media_context.append(
            "Transcript note: no full transcript is available unless it appears in the extracted page text."
        )
    if media_context and "\n".join(media_context) not in content:
        content = f"{content}\n\n" + "\n".join(media_context)

    return {
        "title": title,
        "site": site,
        "description": description,
        "author": author,
        "cover_image": cover_image,
        "published_at": published_at,
        "content_type": content_type,
        "content_type_label": CONTENT_TYPE_CONFIG.get(content_type, {}).get("label", content_type),
        "media_provider": media_provider,
        "media_url": media_url,
        "processing_status": processing_status,
        "og_type": og_type,
        "response_content_type": response_content_type,
        "content": content,
        "url": url,
    }


def infer_platform(url: str, site: str, content_type: str = "") -> str:
    if content_type:
        inferred = platform_from_content_type(content_type)
        if inferred != "website":
            return inferred
    haystack = f"{url} {site}".lower()
    if re.search(r"youtube|youtu\.be|bilibili|douyin|tiktok|vimeo|kuaishou|xiaohongshu|xhslink", haystack):
        return "video"
    if re.search(r"spotify|soundcloud|podcast|ximalaya|audio", haystack):
        return "audio"
    if re.search(r"mp\.weixin|wechat", haystack):
        return "wechat"
    if re.search(r"github|docs|readthedocs|notion|pdf|docx|pptx|xlsx", haystack):
        return "document"
    return "website"


def infer_category(platform_hint: str, title: str, content: str, url: str) -> str:
    haystack = f"{title}\n{content}\n{url}".lower()
    if platform_hint in {"video", "audio"}:
        return "video_notes"
    if re.search(r"ai|agent|llm|prompt|openai|anthropic|cursor|codex|模型|智能体", haystack):
        return "ai_tools"
    if re.search(r"产品|增长|用户研究|pm|运营|商业模式|体验设计|product", haystack):
        return "product_insights"
    if re.search(r"whitepaper|report|guide|tutorial|manual|paper|资料|教程|指南|手册|报告|研究", haystack):
        return "reference_library"
    return "industry_observations"


def normalize_category(value: str, fallback: str) -> str:
    raw = (value or fallback or "reference_library").strip().lower()
    aliases = {
        "ai_tools": "ai_tools",
        "ai": "ai_tools",
        "ai工具": "ai_tools",
        "product_insights": "product_insights",
        "product": "product_insights",
        "产品思考": "product_insights",
        "video_notes": "video_notes",
        "video": "video_notes",
        "视频笔记": "video_notes",
        "reference_library": "reference_library",
        "reference": "reference_library",
        "document": "reference_library",
        "资料库": "reference_library",
        "industry_observations": "industry_observations",
        "industry": "industry_observations",
        "行业观察": "industry_observations",
    }
    return aliases.get(raw, "reference_library")


def normalize_platform(value: str, fallback: str) -> str:
    raw = (value or fallback or "website").strip().lower()
    aliases = {
        "website": "website",
        "网站": "website",
        "video": "video",
        "视频": "video",
        "wechat": "wechat",
        "公众号": "wechat",
        "document": "document",
        "文档": "document",
    }
    if raw in {"online_video"}:
        return "video"
    if raw in {"audio", "online_audio", "音频"}:
        return "audio"
    if raw in {"image", "图片"}:
        return "image"
    return aliases.get(raw, "website")


def safe_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        text = normalize_whitespace(str(item))
        if text:
            output.append(text)
    return output[:limit]


def build_summary_prompt(source: dict[str, str], title_override: str, category_override: str, notes: str) -> str:
    lines = [
        "You are a knowledge capture assistant.",
        "Return strict JSON only with the exact keys: title_clean, summary, key_points, action_items, tags, category, platform.",
        "title_clean can preserve the source language, but keep it short and filename-safe.",
        "summary, key_points, action_items, and tags must be in concise Simplified Chinese.",
        "category must be one of: ai_tools, product_insights, video_notes, reference_library, industry_observations.",
        "platform must be one of: website, video, audio, wechat, document, image.",
        "For online video or audio, use only extracted page text, metadata, title, description, and user notes unless a transcript is explicitly present.",
        "summary should be 3-5 Chinese sentences.",
        "key_points should have 3-5 items.",
        "action_items should have 0-3 items.",
        "tags should have 2-6 short items.",
        "Do not invent facts that are not supported by the source.",
        "",
        f"Title: {source['title']}",
        f"Source URL: {source['url']}",
        f"Site: {source['site']}",
        f"Platform hint: {source['platform_hint']}",
        f"Content type: {source.get('content_type_label') or source.get('content_type', 'webpage')}",
        f"Media provider: {source.get('media_provider') or 'N/A'}",
        f"Extraction status: {source.get('processing_status') or 'N/A'}",
        f"Suggested category: {category_override or source['suggested_category']}",
        f"Description: {source['description'] or 'N/A'}",
        f"Author: {source['author'] or 'N/A'}",
    ]
    if source.get("media_url"):
        lines.append(f"Media URL: {source['media_url']}")
    if source.get("published_at"):
        lines.append(f"Published time: {source['published_at']}")
    if title_override:
        lines.append(f"Preferred title override: {title_override}")
    if notes:
        lines.extend(["", "User notes:", notes[:1600]])
    lines.extend(["", "Content:", source["content"][:12000]])
    return "\n".join(lines)


def parse_json_config(value: str, label: str) -> dict[str, Any]:
    if not value:
        return {}
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def build_llm_headers(config: dict[str, str]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = config_value(config, "LLM_API_KEY", "ZHIPU_API_KEY")
    auth_header = config_value(config, "LLM_AUTH_HEADER", default="Authorization")
    auth_scheme = config_value(config, "LLM_AUTH_SCHEME", default="Bearer")

    if auth_header.lower() not in {"", "none", "off", "disabled"}:
        if not api_key:
            raise ValueError("LLM_API_KEY is missing")
        if auth_scheme.lower() in {"", "none", "off", "disabled"}:
            headers[auth_header] = api_key
        else:
            headers[auth_header] = f"{auth_scheme} {api_key}".strip()

    extra_headers = parse_json_config(
        config_value(config, "LLM_EXTRA_HEADERS_JSON"),
        "LLM_EXTRA_HEADERS_JSON",
    )
    for key, value in extra_headers.items():
        headers[str(key)] = str(value)
    return headers


def build_llm_body(prompt: str, config: dict[str, str], *, include_response_format: bool = True) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": summary_model(config),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You convert source material into structured JSON. "
                    "Return valid JSON only and do not add markdown or commentary."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }

    temperature = config_value(config, "LLM_TEMPERATURE")
    if temperature:
        body["temperature"] = float(temperature)

    response_format = config_value(config, "LLM_RESPONSE_FORMAT", default="json_object")
    if include_response_format and response_format.lower() not in {"", "none", "off", "disabled"}:
        if response_format.strip().startswith("{"):
            body["response_format"] = json.loads(response_format)
        else:
            body["response_format"] = {"type": response_format}

    extra_body = parse_json_config(config_value(config, "LLM_EXTRA_BODY_JSON"), "LLM_EXTRA_BODY_JSON")
    body.update(extra_body)
    return body


def post_llm_chat_completion(prompt: str, config: dict[str, str]) -> requests.Response:
    api_url = summary_api_url(config)
    headers = build_llm_headers(config)
    body = build_llm_body(prompt, config, include_response_format=True)
    response_format_was_default = not config_value(config, "LLM_RESPONSE_FORMAT")

    response = requests.post(api_url, headers=headers, json=body, timeout=90)
    if (
        response.status_code in {400, 422}
        and "response_format" in body
        and response_format_was_default
    ):
        fallback_body = build_llm_body(prompt, config, include_response_format=False)
        response = requests.post(api_url, headers=headers, json=fallback_body, timeout=90)
    return response


def parse_summary_payload(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        payload = json.loads(match.group(0))

    if not isinstance(payload, dict):
        raise ValueError("Summary response JSON must be an object")
    return payload


def summarize_with_llm(prompt: str, config: dict[str, str]) -> dict[str, Any]:
    response = post_llm_chat_completion(prompt, config)
    response.raise_for_status()
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("Summary API returned no choices")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(item.get("text", "") for item in content if isinstance(item, dict))
    if not isinstance(content, str):
        raise ValueError("Unsupported summary API response format")

    payload = parse_summary_payload(content)
    required = {"title_clean", "summary", "key_points", "action_items", "tags", "category", "platform"}
    missing = required.difference(payload.keys())
    if missing:
        raise ValueError(f"Summary response is missing keys: {', '.join(sorted(missing))}")
    return payload


def notion_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_request(method: str, path: str, token: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.request(
        method=method,
        url=f"{NOTION_API_BASE}{path}",
        headers=notion_headers(token),
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        detail = response.text[:1200]
        raise requests.HTTPError(f"{response.status_code} {detail}", response=response)
    if not response.content:
        return {}
    return response.json()


def load_notion_meta() -> dict[str, str]:
    if not NOTION_META_PATH.exists():
        return {}
    try:
        data = json.loads(NOTION_META_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_notion_meta(meta: dict[str, str]) -> None:
    ensure_runtime()
    NOTION_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_yaml_scalar(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw[0] in {'"', "'"}:
        try:
            return str(json.loads(raw))
        except Exception:
            return raw.strip("\"'")
    return raw


def load_capture_index() -> dict[str, dict[str, Any]]:
    ensure_runtime()
    if not INDEX_PATH.exists():
        return {}
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(key): value for key, value in data.items() if isinstance(value, dict)}
    except Exception:
        pass
    return {}


def save_capture_index(index: dict[str, dict[str, Any]]) -> None:
    ensure_runtime()
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_nonempty(*sources: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in sources:
        for key, value in source.items():
            if value not in ("", None, [], {}):
                merged[key] = value
    return merged


def upsert_capture_index(entry: dict[str, Any]) -> None:
    source_url = normalize_whitespace(str(entry.get("source_url", "")))
    if not source_url:
        return
    index = load_capture_index()
    current = index.get(source_url, {})
    index[source_url] = merge_nonempty(
        current,
        {
            "source_url": source_url,
            "title": entry.get("title", ""),
            "category": entry.get("category", ""),
            "platform": entry.get("platform", ""),
            "site": entry.get("site", ""),
            "obsidian_path": entry.get("obsidian_path", ""),
            "archive_path": entry.get("archive_path", ""),
            "notion_page_id": entry.get("notion_page_id", ""),
            "notion_page_url": entry.get("notion_page_url", entry.get("notion_url", "")),
            "captured_at": entry.get("captured_at", ""),
            "captured_date": entry.get("captured_date", ""),
            "published_at": entry.get("published_at", ""),
            "published_date": entry.get("published_date", ""),
            "content_type": entry.get("content_type", ""),
            "content_type_label": entry.get("content_type_label", ""),
            "media_provider": entry.get("media_provider", ""),
            "media_url": entry.get("media_url", ""),
            "processing_status": entry.get("processing_status", ""),
            "canvas_path": entry.get("canvas_path", ""),
            "last_synced_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        },
    )
    save_capture_index(index)


def extract_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    match = re.search(r"^---\s*\n(.*?)\n---\s*\n", text[:8000], re.S)
    if not match:
        return {}
    result: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        result[key.strip()] = parse_yaml_scalar(raw)
    return result


def find_local_paths_by_url(vault_root: Path, source_url: str) -> dict[str, str]:
    if not source_url or not vault_root.exists():
        return {}

    archive_root = vault_root / "Clippings" / "Archive"
    knowledge_root = vault_root / "信息汇总" / "自动整理"
    found: dict[str, str] = {}
    for root in [knowledge_root, archive_root]:
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            try:
                frontmatter = extract_frontmatter(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if frontmatter.get("source_url") == source_url or frontmatter.get("url") == source_url:
                if "source_url" in frontmatter and "obsidian_path" not in found:
                    found["obsidian_path"] = str(path)
                    found["title"] = frontmatter.get("title", found.get("title", ""))
                    found["category"] = frontmatter.get("category", found.get("category", ""))
                elif "url" in frontmatter and "archive_path" not in found:
                    found["archive_path"] = str(path)
                if found.get("obsidian_path") and found.get("archive_path"):
                    return found
    return found


def notion_plain_text(items: list[dict[str, Any]] | None) -> str:
    output: list[str] = []
    for item in items or []:
        plain = item.get("plain_text")
        if plain:
            output.append(str(plain))
            continue
        text = item.get("text") or {}
        content = text.get("content")
        if content:
            output.append(str(content))
    return normalize_whitespace("".join(output))


def notion_page_title(page: dict[str, Any]) -> str:
    title_property = ((page.get("properties") or {}).get("标题") or {}).get("title") or []
    return notion_plain_text(title_property)


def notion_rich_text_property(page: dict[str, Any], name: str) -> str:
    property_data = (page.get("properties") or {}).get(name) or {}
    return notion_plain_text(property_data.get("rich_text") or [])


def notion_select_property(page: dict[str, Any], name: str) -> str:
    property_data = (page.get("properties") or {}).get(name) or {}
    select = property_data.get("select") or {}
    return str(select.get("name") or "")


def notion_url_property(page: dict[str, Any], name: str) -> str:
    property_data = (page.get("properties") or {}).get(name) or {}
    return str(property_data.get("url") or "")


def notion_date_property(page: dict[str, Any], name: str) -> str:
    property_data = (page.get("properties") or {}).get(name) or {}
    date_value = property_data.get("date") or {}
    return str(date_value.get("start") or "")


def prune_preview_cache() -> None:
    threshold = datetime.now() - timedelta(minutes=PREVIEW_TTL_MINUTES)
    expired: list[str] = []
    for preview_id, entry in PREVIEW_CACHE.items():
        created_at = entry.get("created_at")
        if isinstance(created_at, datetime) and created_at < threshold:
            expired.append(preview_id)
    for preview_id in expired:
        PREVIEW_CACHE.pop(preview_id, None)


def store_preview(preview_payload: dict[str, Any]) -> str:
    preview_id = uuid4().hex
    with PREVIEW_CACHE_LOCK:
        prune_preview_cache()
        PREVIEW_CACHE[preview_id] = {
            "created_at": datetime.now(),
            "payload": preview_payload,
        }
    return preview_id


def load_preview(preview_id: str) -> dict[str, Any]:
    with PREVIEW_CACHE_LOCK:
        prune_preview_cache()
        entry = PREVIEW_CACHE.get(preview_id) or {}
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("预览已过期，请重新提取")
        return payload


def clear_preview(preview_id: str) -> None:
    with PREVIEW_CACHE_LOCK:
        PREVIEW_CACHE.pop(preview_id, None)


def rich_text(text: str, *, link: str = "") -> list[dict[str, Any]]:
    content = normalize_whitespace(text)
    if not content:
        return []
    chunk = content[:1800]
    if link:
        return [{"type": "text", "text": {"content": chunk, "link": {"url": link}}}]
    return [{"type": "text", "text": {"content": chunk}}]


def build_database_payload(parent_page_id: str) -> dict[str, Any]:
    category_options = [
        {"name": config["label"], "color": config["color"]} for config in CATEGORY_CONFIG.values()
    ]
    platform_options = [
        {"name": config["label"], "color": config["color"]} for config in PLATFORM_CONFIG.values()
    ]
    return {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": NOTION_DATABASE_TITLE}}],
        "description": [
            {
                "type": "text",
                "text": {
                    "content": "由本地 URL Capture 页面自动写入。按日期查看会更适合日历视图。"
                },
            }
        ],
        "icon": {"type": "emoji", "emoji": "🗓️"},
        "is_inline": False,
        "initial_data_source": {
            "properties": {
                "标题": {"title": {}},
                "收录日期": {"date": {}},
                "发布日期": {"date": {}},
                "分类": {"select": {"options": category_options}},
                "平台": {"select": {"options": platform_options}},
                "站点": {"rich_text": {}},
                "来源": {"url": {}},
                "标签": {"multi_select": {}},
                "摘要": {"rich_text": {}},
                "Obsidian": {"rich_text": {}},
                "Content Type": {
                    "select": {
                        "options": [
                            {"name": config["label"], "color": config["color"]}
                            for config in CONTENT_TYPE_CONFIG.values()
                        ]
                    }
                },
                "Processing": {
                    "select": {
                        "options": [
                            {"name": "page_text_extracted", "color": "green"},
                            {"name": "metadata_plus_page_text", "color": "blue"},
                            {"name": "metadata_only", "color": "yellow"},
                            {"name": "metadata_only_needs_transcript", "color": "red"},
                        ]
                    }
                },
                "Media Provider": {"rich_text": {}},
                "Media URL": {"url": {}},
                "Canvas": {"rich_text": {}},
            }
        },
    }


def create_notion_view(
    token: str,
    database_id: str,
    data_source_id: str,
    name: str,
    view_type: str,
) -> str:
    payload = {
        "database_id": database_id,
        "data_source_id": data_source_id,
        "name": name,
        "type": view_type,
    }
    result = notion_request("POST", "/views", token, payload=payload)
    return str(result.get("url") or "")


def ensure_data_source_schema(token: str, data_source_id: str) -> None:
    data_source = notion_request("GET", f"/data_sources/{data_source_id}", token)
    properties = data_source.get("properties") or {}
    patch: dict[str, Any] = {}
    if "日期" in properties and "收录日期" not in properties:
        patch["日期"] = {"name": "收录日期"}
    if "收录日期" not in properties and "日期" not in properties:
        patch["收录日期"] = {"date": {}}
    if "发布日期" not in properties:
        patch["发布日期"] = {"date": {}}
    if "Content Type" not in properties:
        patch["Content Type"] = {
            "select": {
                "options": [
                    {"name": config["label"], "color": config["color"]}
                    for config in CONTENT_TYPE_CONFIG.values()
                ]
            }
        }
    if "Processing" not in properties:
        patch["Processing"] = {
            "select": {
                "options": [
                    {"name": "page_text_extracted", "color": "green"},
                    {"name": "metadata_plus_page_text", "color": "blue"},
                    {"name": "metadata_only", "color": "yellow"},
                    {"name": "metadata_only_needs_transcript", "color": "red"},
                ]
            }
        }
    if "Media Provider" not in properties:
        patch["Media Provider"] = {"rich_text": {}}
    if "Media URL" not in properties:
        patch["Media URL"] = {"url": {}}
    if "Canvas" not in properties:
        patch["Canvas"] = {"rich_text": {}}
    if patch:
        notion_request("PATCH", f"/data_sources/{data_source_id}", token, payload={"properties": patch})


def query_data_source_pages(token: str, data_source_id: str) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    next_cursor = ""
    while True:
        payload: dict[str, Any] = {"page_size": 100}
        if next_cursor:
            payload["start_cursor"] = next_cursor
        result = notion_request("POST", f"/data_sources/{data_source_id}/query", token, payload=payload)
        pages.extend(item for item in (result.get("results") or []) if isinstance(item, dict))
        if not result.get("has_more"):
            break
        next_cursor = str(result.get("next_cursor") or "")
        if not next_cursor:
            break
    return pages


def find_existing_notion_entry(token: str, meta: dict[str, str], source_url: str) -> dict[str, str]:
    data_source_id = meta.get("data_source_id", "")
    if not data_source_id or not source_url:
        return {}
    for page in query_data_source_pages(token, data_source_id):
        if notion_url_property(page, "来源") != source_url:
            continue
        return {
            "notion_page_id": str(page.get("id") or ""),
            "notion_page_url": str(page.get("url") or ""),
            "title": notion_page_title(page),
            "category": notion_select_property(page, "分类"),
            "platform": notion_select_property(page, "平台"),
            "obsidian_path": notion_rich_text_property(page, "Obsidian"),
            "captured_date": notion_date_property(page, "收录日期") or notion_date_property(page, "日期"),
            "published_date": notion_date_property(page, "发布日期"),
        }
    return {}


def ensure_notion_database(config: dict[str, str]) -> dict[str, str]:
    notion_token = os.environ.get("NOTION_API_TOKEN") or config.get("NOTION_API_TOKEN", "")
    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID") or config.get("NOTION_PARENT_PAGE_ID", "")
    if not notion_token or not parent_page_id:
        raise ValueError("Notion credentials are missing")

    cached = load_notion_meta()
    database_id = cached.get("database_id", "")
    if database_id:
        try:
            database = notion_request("GET", f"/databases/{database_id}", notion_token)
            data_sources = database.get("data_sources") or []
            cached["database_url"] = str(database.get("url") or cached.get("database_url", ""))
            if not cached.get("data_source_id") and data_sources:
                cached["data_source_id"] = str(data_sources[0].get("id") or "")
            if cached.get("data_source_id"):
                ensure_data_source_schema(notion_token, cached["data_source_id"])
            save_notion_meta(cached)
            return cached
        except requests.HTTPError:
            cached = {}

    created = notion_request("POST", "/databases", notion_token, payload=build_database_payload(parent_page_id))
    data_sources = created.get("data_sources") or []
    data_source_id = str(data_sources[0].get("id") or "") if data_sources else ""
    database_id = str(created.get("id") or "")
    database_url = str(created.get("url") or "")

    meta = {
        "database_id": database_id,
        "data_source_id": data_source_id,
        "database_url": database_url,
        "calendar_url": database_url,
        "gallery_url": database_url,
    }

    if database_id and data_source_id:
        ensure_data_source_schema(notion_token, data_source_id)
        try:
            calendar_url = create_notion_view(
                notion_token,
                database_id,
                data_source_id,
                NOTION_CALENDAR_VIEW_NAME,
                "calendar",
            )
            meta["calendar_url"] = calendar_url or meta["calendar_url"]
        except requests.HTTPError:
            meta["calendar_url"] = meta["database_url"]
        try:
            gallery_url = create_notion_view(
                notion_token,
                database_id,
                data_source_id,
                NOTION_GALLERY_VIEW_NAME,
                "gallery",
            )
            meta["gallery_url"] = gallery_url or meta["gallery_url"]
        except requests.HTTPError:
            meta["gallery_url"] = meta["database_url"]

    save_notion_meta(meta)
    return meta


def build_archive_markdown(source: dict[str, str], archive_title: str) -> str:
    cover_block = f"![cover]({source['cover_image']})\n\n" if source.get("cover_image") else ""
    return "\n".join(
        [
            "---",
            f"title: {yaml_string(archive_title)}",
            f"url: {yaml_string(source['url'])}",
            f"site: {yaml_string(source['site'])}",
            f"author: {yaml_string(source.get('author', ''))}",
            f"cover_image: {yaml_string(source.get('cover_image', ''))}",
            f"published_at: {yaml_string(source.get('published_at', ''))}",
            f"content_type: {yaml_string(source.get('content_type', 'webpage'))}",
            f"media_provider: {yaml_string(source.get('media_provider', ''))}",
            f"media_url: {yaml_string(source.get('media_url', ''))}",
            f"processing_status: {yaml_string(source.get('processing_status', ''))}",
            f"clipped_at: {yaml_string(source['clipped_at'])}",
            'status: "已归档"',
            "---",
            "",
            f"# {archive_title}",
            "",
            cover_block.rstrip(),
            "",
            source["content"],
            "",
        ]
    ).replace("\n\n\n", "\n\n")


def build_final_markdown(
    source: dict[str, str],
    summary: dict[str, Any],
    title_clean: str,
    category_label: str,
    platform_label: str,
    notes: str,
) -> str:
    tags = [sanitize_file_name(str(tag)).replace(" ", "") for tag in summary["tags"] if str(tag).strip()]
    summary_text = normalize_whitespace(str(summary["summary"]))
    key_points = safe_list(summary["key_points"], 5)
    action_items = safe_list(summary["action_items"], 3)
    cover_block = f"![cover]({source['cover_image']})\n\n" if source.get("cover_image") else ""
    tags_block = "tags: []"
    if tags:
        tags_block = "tags:\n" + "\n".join(f"  - {yaml_string(tag)}" for tag in tags)

    note_lines = []
    if notes:
        note_lines = ["", "## 我的补充", notes.strip()]

    published_line = f"> 发布：{source['published_at']}" if source.get("published_at") else ""
    media_lines = [
        f"- Content type: {source.get('content_type_label') or source.get('content_type', 'webpage')}",
        f"- Media provider: {source.get('media_provider', '') or source.get('site', '')}",
        f"- Processing: {source.get('processing_status', '') or 'page_text_extracted'}",
    ]
    if source.get("media_url"):
        media_lines.append(f"- Media URL: {source['media_url']}")
    return "\n".join(
        [
            "---",
            f"title: {yaml_string(title_clean)}",
            f"category: {yaml_string(category_label)}",
            f"platform: {yaml_string(platform_label)}",
            f"site: {yaml_string(source['site'])}",
            f"author: {yaml_string(source.get('author', ''))}",
            f"source_url: {yaml_string(source['url'])}",
            f"cover_image: {yaml_string(source.get('cover_image', ''))}",
            f"published_at: {yaml_string(source.get('published_at', ''))}",
            f"content_type: {yaml_string(source.get('content_type', 'webpage'))}",
            f"media_provider: {yaml_string(source.get('media_provider', ''))}",
            f"media_url: {yaml_string(source.get('media_url', ''))}",
            f"processing_status: {yaml_string(source.get('processing_status', ''))}",
            f"canvas_path: {yaml_string(source.get('canvas_path', ''))}",
            f"clipped_at: {yaml_string(source['clipped_at'])}",
            'status: "已整理"',
            tags_block,
            "---",
            "",
            f"# {title_clean}",
            "",
            cover_block.rstrip(),
            "",
            f"> 来源：{source['url']}",
            f"> 站点：{source['site']}",
            f"> 收录：{source['clipped_at']}",
            published_line,
            "",
            "## Media / Source",
            "\n".join(media_lines),
            "",
            "## 一句话总结",
            summary_text,
            "",
            "## 核心要点",
            "\n".join(f"- {item}" for item in key_points) if key_points else "- 暂无",
            "",
            "## 可执行动作",
            "\n".join(f"- {item}" for item in action_items) if action_items else "- 暂无",
            *note_lines,
            "",
            "## 原文摘录",
            source["content"][:4000].strip() or "暂无正文内容",
            "",
        ]
    ).replace("\n\n\n", "\n\n")


def build_notion_properties(
    source: dict[str, str],
    summary: dict[str, Any],
    title_clean: str,
    category_label: str,
    platform_label: str,
    final_path: Path,
) -> dict[str, Any]:
    tags = [sanitize_file_name(str(tag)).replace(" ", "") for tag in summary["tags"] if str(tag).strip()]
    summary_text = normalize_whitespace(str(summary["summary"]))
    capture_date = to_date_string(source["clipped_at"]) or datetime.now().strftime("%Y-%m-%d")
    properties: dict[str, Any] = {
        "标题": {"title": rich_text(title_clean)},
        "收录日期": {"date": {"start": capture_date}},
        "分类": {"select": {"name": category_label}},
        "平台": {"select": {"name": platform_label}},
        "站点": {"rich_text": rich_text(source["site"])},
        "来源": {"url": source["url"]},
        "标签": {"multi_select": [{"name": tag} for tag in tags[:8]]},
        "摘要": {"rich_text": rich_text(summary_text)},
        "Obsidian": {"rich_text": rich_text(str(final_path))},
        "Content Type": {
            "select": {
                "name": source.get("content_type_label")
                or CONTENT_TYPE_CONFIG.get(source.get("content_type", "webpage"), {}).get("label", "Web Page / 网页")
            }
        },
        "Processing": {"select": {"name": source.get("processing_status", "page_text_extracted")}},
        "Media Provider": {"rich_text": rich_text(source.get("media_provider", ""))},
        "Canvas": {"rich_text": rich_text(source.get("canvas_path", ""))},
    }
    if source.get("media_url"):
        properties["Media URL"] = {"url": source["media_url"]}
    published_date = to_date_string(source.get("published_at", ""))
    if published_date:
        properties["发布日期"] = {"date": {"start": published_date}}
    else:
        properties["发布日期"] = {"date": None}
    return properties


def build_notion_children(
    source: dict[str, str],
    summary: dict[str, Any],
    category_label: str,
    platform_label: str,
    notes: str,
) -> tuple[str, list[dict[str, Any]]]:
    tags = [sanitize_file_name(str(tag)).replace(" ", "") for tag in summary["tags"] if str(tag).strip()]
    summary_text = normalize_whitespace(str(summary["summary"]))
    key_points = safe_list(summary["key_points"], 5)
    action_items = safe_list(summary["action_items"], 3)
    page_icon = CATEGORY_ICONS.get(category_label, "🗂️")

    metadata_lines = [
        f"收录时间：{source['clipped_at']}",
        f"来源站点：{source['site']}",
        f"平台类型：{platform_label}",
        f"内容类型：{source.get('content_type_label') or source.get('content_type', 'webpage')}",
        f"处理状态：{source.get('processing_status', '') or 'page_text_extracted'}",
    ]
    if source.get("media_provider"):
        metadata_lines.append(f"媒体平台：{source['media_provider']}")
    if source.get("published_at"):
        metadata_lines.append(f"发布时间：{source['published_at']}")
    if tags:
        metadata_lines.append(f"标签：{' / '.join(tags)}")

    children: list[dict[str, Any]] = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"emoji": page_icon},
                "color": "gray_background",
                "rich_text": rich_text(summary_text),
            },
        },
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": rich_text("  |  ".join(metadata_lines))},
        },
        {"object": "block", "type": "divider", "divider": {}},
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": rich_text("核心要点")},
        },
    ]

    for item in key_points:
        children.append(
            {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": rich_text(item)},
            }
        )

    children.append(
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": rich_text("可执行动作")},
        }
    )
    if action_items:
        for item in action_items:
            children.append(
                {
                    "object": "block",
                    "type": "to_do",
                    "to_do": {"checked": False, "rich_text": rich_text(item)},
                }
            )
    else:
        children.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": rich_text("暂无")},
            }
        )

    if notes:
        children.extend(
            [
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {"rich_text": rich_text("我的补充")},
                },
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": rich_text(notes[:1800])},
                },
            ]
        )

    children.extend(
        [
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": rich_text("原文入口")},
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": rich_text(source["url"], link=source["url"])},
            },
            {
                "object": "block",
                "type": "quote",
                "quote": {"rich_text": rich_text(source["content"][:900] or "暂无正文内容")},
            },
        ]
    )
    return page_icon, children


def create_or_update_notion_entry(
    source: dict[str, str],
    summary: dict[str, Any],
    config: dict[str, str],
    title_clean: str,
    category_label: str,
    platform_label: str,
    final_path: Path,
    notes: str,
    existing: dict[str, Any],
) -> dict[str, str]:
    notion_token = os.environ.get("NOTION_API_TOKEN") or config.get("NOTION_API_TOKEN", "")
    meta = ensure_notion_database(config)
    if not notion_token:
        raise ValueError("Notion credentials are missing")

    properties = build_notion_properties(source, summary, title_clean, category_label, platform_label, final_path)
    page_icon, children = build_notion_children(source, summary, category_label, platform_label, notes)
    cover_image = source.get("cover_image", "")
    page_id = str(existing.get("notion_page_id") or "")

    if page_id:
        update_payload: dict[str, Any] = {
            "icon": {"type": "emoji", "emoji": page_icon},
            "properties": properties,
            "erase_content": True,
        }
        if cover_image.startswith("http"):
            update_payload["cover"] = {"type": "external", "external": {"url": cover_image}}
        response = notion_request("PATCH", f"/pages/{page_id}", notion_token, payload=update_payload)
        notion_request("PATCH", f"/blocks/{page_id}/children", notion_token, payload={"children": children})
        mode = "updated"
    else:
        body: dict[str, Any] = {
            "icon": {"type": "emoji", "emoji": page_icon},
            "properties": properties,
            "children": children,
        }
        if cover_image.startswith("http"):
            body["cover"] = {"type": "external", "external": {"url": cover_image}}
        try:
            response = notion_request(
                "POST",
                "/pages",
                notion_token,
                payload={"parent": {"type": "data_source_id", "data_source_id": meta["data_source_id"]}, **body},
            )
        except requests.HTTPError:
            response = notion_request(
                "POST",
                "/pages",
                notion_token,
                payload={"parent": {"type": "database_id", "database_id": meta["database_id"]}, **body},
            )
        page_id = str(response.get("id") or "")
        mode = "created"

    return {
        "page_id": page_id,
        "page_url": str(response.get("url") or existing.get("notion_page_url") or ""),
        "database_url": meta.get("database_url", ""),
        "calendar_url": meta.get("calendar_url", meta.get("database_url", "")),
        "gallery_url": meta.get("gallery_url", meta.get("database_url", "")),
        "mode": mode,
    }


def append_history(entry: dict[str, Any]) -> None:
    ensure_runtime()
    history: list[dict[str, Any]] = []
    if HISTORY_PATH.exists():
        try:
            history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            history = []
    source_url = normalize_whitespace(str(entry.get("source_url", "")))
    if source_url:
        history = [
            item
            for item in history
            if normalize_whitespace(str(item.get("source_url", ""))) != source_url
        ]
    history.insert(0, entry)
    HISTORY_PATH.write_text(json.dumps(history[:18], ensure_ascii=False, indent=2), encoding="utf-8")


def read_history() -> list[dict[str, Any]]:
    ensure_runtime()
    if not HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_canvas_board() -> dict[str, Any]:
    ensure_runtime()
    if not CANVAS_PATH.exists():
        return {"cards": [], "updated_at": ""}
    try:
        data = json.loads(CANVAS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cards = data.get("cards")
            if isinstance(cards, list):
                return {"cards": [card for card in cards if isinstance(card, dict)], "updated_at": data.get("updated_at", "")}
    except Exception:
        pass
    return {"cards": [], "updated_at": ""}


def save_canvas_board(board: dict[str, Any]) -> None:
    ensure_runtime()
    board["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    CANVAS_PATH.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")


def build_canvas_card(entry: dict[str, Any], existing: dict[str, Any] | None, index: int) -> dict[str, Any]:
    current = existing or {}
    return merge_nonempty(
        {
            "id": current.get("id") or uuid4().hex,
            "x": current.get("x", 80 + (index % 3) * 320),
            "y": current.get("y", 80 + (index // 3) * 220),
        },
        current,
        {
            "source_url": entry.get("source_url", ""),
            "title": entry.get("title", ""),
            "summary": entry.get("summary", ""),
            "category": entry.get("category", ""),
            "platform": entry.get("platform", ""),
            "content_type": entry.get("content_type", ""),
            "content_type_label": entry.get("content_type_label", ""),
            "media_provider": entry.get("media_provider", ""),
            "processing_status": entry.get("processing_status", ""),
            "notion_page_url": entry.get("notion_page_url", entry.get("notion_url", "")),
            "obsidian_path": entry.get("obsidian_path", ""),
            "captured_at": entry.get("captured_at", ""),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        },
    )


def upsert_canvas_card(entry: dict[str, Any]) -> None:
    source_url = normalize_whitespace(str(entry.get("source_url", "")))
    if not source_url:
        return
    board = load_canvas_board()
    cards = board.get("cards") or []
    existing_index = next(
        (index for index, card in enumerate(cards) if normalize_whitespace(str(card.get("source_url", ""))) == source_url),
        -1,
    )
    if existing_index >= 0:
        cards[existing_index] = build_canvas_card(entry, cards[existing_index], existing_index)
    else:
        cards.insert(0, build_canvas_card(entry, None, len(cards)))
    board["cards"] = cards[:80]
    save_canvas_board(board)


def update_canvas_positions(updates: list[dict[str, Any]]) -> dict[str, Any]:
    board = load_canvas_board()
    cards = board.get("cards") or []
    update_map = {str(item.get("id")): item for item in updates if isinstance(item, dict) and item.get("id")}
    for card in cards:
        update = update_map.get(str(card.get("id")))
        if not update:
            continue
        for key in ("x", "y"):
            try:
                card[key] = int(float(update.get(key, card.get(key, 0))))
            except (TypeError, ValueError):
                pass
    board["cards"] = cards
    save_canvas_board(board)
    return board


def history_entry_for_url(source_url: str) -> dict[str, str]:
    for item in read_history():
        if normalize_whitespace(str(item.get("source_url", ""))) != source_url:
            continue
        return {
            "source_url": source_url,
            "title": str(item.get("title") or ""),
            "category": str(item.get("category") or ""),
            "platform": str(item.get("platform") or ""),
            "site": str(item.get("site") or ""),
            "obsidian_path": str(item.get("obsidian_path") or ""),
            "archive_path": str(item.get("archive_path") or ""),
            "notion_page_id": str(item.get("notion_page_id") or ""),
            "notion_page_url": str(item.get("notion_page_url") or item.get("notion_url") or ""),
            "captured_at": str(item.get("captured_at") or ""),
            "captured_date": str(item.get("captured_date") or ""),
            "published_at": str(item.get("published_at") or ""),
            "published_date": str(item.get("published_date") or ""),
            "content_type": str(item.get("content_type") or ""),
            "content_type_label": str(item.get("content_type_label") or ""),
            "media_provider": str(item.get("media_provider") or ""),
            "media_url": str(item.get("media_url") or ""),
            "processing_status": str(item.get("processing_status") or ""),
            "canvas_path": str(item.get("canvas_path") or ""),
        }
    return {}


def resolve_existing_capture(source_url: str, config: dict[str, str], vault_root: Path) -> dict[str, Any]:
    existing = load_capture_index().get(source_url, {})
    existing = merge_nonempty(existing, history_entry_for_url(source_url))

    notion_token = os.environ.get("NOTION_API_TOKEN") or config.get("NOTION_API_TOKEN", "")
    if notion_token and (not existing.get("notion_page_id") or not existing.get("obsidian_path")):
        try:
            notion_meta = ensure_notion_database(config)
            notion_existing = find_existing_notion_entry(notion_token, notion_meta, source_url)
            existing = merge_nonempty(existing, notion_existing)
        except Exception:
            pass

    if not existing.get("obsidian_path") or not existing.get("archive_path"):
        existing = merge_nonempty(existing, find_local_paths_by_url(vault_root, source_url))

    if existing:
        existing["exists"] = True
    return existing


def build_capture_payload(
    url: str,
    *,
    title_override: str = "",
    category_override: str = "",
    notes: str = "",
) -> dict[str, Any]:
    config = load_config()
    vault_root = Path(config.get("OBSIDIAN_VAULT_PATH") or DEFAULT_VAULT)
    archive_root = vault_root / "Clippings" / "Archive"
    knowledge_root = vault_root / "信息汇总" / "自动整理"

    source = extract_article(url)
    clipped_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    source["clipped_at"] = clipped_at
    source["canvas_path"] = str(CANVAS_PATH)
    source["platform_hint"] = infer_platform(source["url"], source["site"], source.get("content_type", ""))
    source["suggested_category"] = normalize_category(category_override, "")
    if not category_override:
        source["suggested_category"] = infer_category(
            source["platform_hint"],
            source["title"],
            source["content"],
            source["url"],
        )

    summary = summarize_with_llm(
        build_summary_prompt(source, title_override, category_override, notes),
        config,
    )
    if title_override:
        summary["title_clean"] = title_override
    if category_override:
        summary["category"] = category_override

    category_key = normalize_category(str(summary["category"]), source["suggested_category"])
    platform_key = normalize_platform(str(summary["platform"]), source["platform_hint"])
    category_label = CATEGORY_LABELS[category_key]
    platform_label = PLATFORM_LABELS[platform_key]
    title_clean = sanitize_file_name(str(summary["title_clean"]))
    archive_title = sanitize_file_name(source["title"])
    captured_date = clipped_at[:10]
    published_date = to_date_string(source.get("published_at", ""))

    existing = resolve_existing_capture(source["url"], config, vault_root)

    desired_archive_path = archive_root / f"{captured_date} {archive_title}.md"
    desired_final_path = knowledge_root / category_label / f"{captured_date} {title_clean}.md"
    archive_path = Path(existing.get("archive_path") or desired_archive_path)
    final_path = Path(existing.get("obsidian_path") or desired_final_path)

    notion_meta = load_notion_meta()
    if (config.get("NOTION_API_TOKEN") or os.environ.get("NOTION_API_TOKEN")) and (
        not notion_meta.get("database_url") or not notion_meta.get("calendar_url")
    ):
        try:
            notion_meta = ensure_notion_database(config)
        except Exception:
            notion_meta = load_notion_meta()

    return {
        "config": config,
        "source": source,
        "summary": summary,
        "notes": notes.strip(),
        "title_clean": title_clean,
        "archive_title": archive_title,
        "category_key": category_key,
        "platform_key": platform_key,
        "category_label": category_label,
        "platform_label": platform_label,
        "archive_path": str(archive_path),
        "final_path": str(final_path),
        "captured_at": clipped_at,
        "captured_date": captured_date,
        "published_at": source.get("published_at", ""),
        "published_date": published_date,
        "site": source["site"],
        "source_url": source["url"],
        "content_type": source.get("content_type", "webpage"),
        "content_type_label": source.get("content_type_label", "Web Page / 网页"),
        "media_provider": source.get("media_provider", ""),
        "media_url": source.get("media_url", ""),
        "processing_status": source.get("processing_status", ""),
        "canvas_path": source.get("canvas_path", ""),
        "summary_text": normalize_whitespace(str(summary["summary"])),
        "key_points": safe_list(summary["key_points"], 5),
        "action_items": safe_list(summary["action_items"], 3),
        "tags": safe_list(summary["tags"], 8),
        "existing": existing,
        "duplicate_found": bool(existing),
        "sync_action": "update" if existing else "create",
        "notion_database_url": notion_meta.get("database_url", ""),
        "notion_calendar_url": notion_meta.get("calendar_url", ""),
        "notion_gallery_url": notion_meta.get("gallery_url", ""),
    }


def build_preview_response(prepared: dict[str, Any], *, preview_id: str) -> dict[str, Any]:
    existing = prepared.get("existing") or {}
    return {
        "ok": True,
        "stage": "preview",
        "preview_id": preview_id,
        "title": prepared["title_clean"],
        "category": prepared["category_label"],
        "platform": prepared["platform_label"],
        "site": prepared["site"],
        "summary": prepared["summary_text"],
        "key_points": prepared["key_points"],
        "action_items": prepared["action_items"],
        "tags": prepared["tags"],
        "captured_at": prepared["captured_at"],
        "captured_date": prepared["captured_date"],
        "published_at": prepared["published_at"],
        "published_date": prepared["published_date"],
        "source_url": prepared["source_url"],
        "content_type": prepared.get("content_type", "webpage"),
        "content_type_label": prepared.get("content_type_label", "Web Page / 网页"),
        "media_provider": prepared.get("media_provider", ""),
        "media_url": prepared.get("media_url", ""),
        "processing_status": prepared.get("processing_status", ""),
        "canvas_path": prepared.get("canvas_path", ""),
        "notes": prepared["notes"],
        "duplicate_found": prepared["duplicate_found"],
        "sync_action": prepared["sync_action"],
        "sync_action_label": "更新已有条目" if prepared["duplicate_found"] else "创建新条目",
        "existing_title": existing.get("title", ""),
        "existing_category": existing.get("category", ""),
        "existing_obsidian_path": existing.get("obsidian_path", ""),
        "existing_notion_url": existing.get("notion_page_url", ""),
        "notion_page_url": existing.get("notion_page_url", ""),
        "notion_page_id": existing.get("notion_page_id", ""),
        "obsidian_path": prepared["final_path"],
        "archive_path": prepared["archive_path"],
        "notion_database_url": prepared["notion_database_url"],
        "notion_calendar_url": prepared["notion_calendar_url"],
        "notion_gallery_url": prepared["notion_gallery_url"],
    }


def finalize_capture(prepared: dict[str, Any]) -> dict[str, Any]:
    config = prepared["config"]
    source = prepared["source"]
    summary = prepared["summary"]
    notes = prepared["notes"]
    category_label = prepared["category_label"]
    platform_label = prepared["platform_label"]
    title_clean = prepared["title_clean"]
    archive_title = prepared["archive_title"]
    archive_path = Path(prepared["archive_path"])
    final_path = Path(prepared["final_path"])

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.parent.mkdir(parents=True, exist_ok=True)

    archive_path.write_text(build_archive_markdown(source, archive_title), encoding="utf-8")
    final_path.write_text(
        build_final_markdown(source, summary, title_clean, category_label, platform_label, notes),
        encoding="utf-8",
    )

    notion_entry = create_or_update_notion_entry(
        source,
        summary,
        config,
        title_clean,
        category_label,
        platform_label,
        final_path,
        notes,
        prepared.get("existing") or {},
    )

    duplicate_found = bool(prepared.get("duplicate_found"))
    result = {
        "ok": True,
        "stage": "synced",
        "title": title_clean,
        "category": category_label,
        "platform": platform_label,
        "site": source["site"],
        "obsidian_path": str(final_path),
        "archive_path": str(archive_path),
        "notion_url": notion_entry["page_url"],
        "notion_page_id": notion_entry["page_id"],
        "notion_page_url": notion_entry["page_url"],
        "notion_calendar_url": notion_entry["calendar_url"],
        "notion_gallery_url": notion_entry["gallery_url"],
        "notion_database_url": notion_entry["database_url"],
        "cover_image": source.get("cover_image", ""),
        "summary": prepared["summary_text"],
        "key_points": prepared["key_points"],
        "action_items": prepared["action_items"],
        "tags": prepared["tags"],
        "captured_at": prepared["captured_at"],
        "captured_date": prepared["captured_date"],
        "published_at": prepared["published_at"],
        "published_date": prepared["published_date"],
        "source_url": source["url"],
        "content_type": prepared.get("content_type", "webpage"),
        "content_type_label": prepared.get("content_type_label", "Web Page / 网页"),
        "media_provider": prepared.get("media_provider", ""),
        "media_url": prepared.get("media_url", ""),
        "processing_status": prepared.get("processing_status", ""),
        "canvas_path": prepared.get("canvas_path", ""),
        "notes": notes,
        "duplicate_found": duplicate_found,
        "sync_action": "update" if duplicate_found else "create",
        "sync_action_label": "更新已有条目" if duplicate_found else "创建新条目",
        "notion_sync_mode": notion_entry["mode"],
    }
    append_history(result)
    upsert_capture_index(result)
    upsert_canvas_card(result)
    return result


def write_capture(
    url: str,
    *,
    title_override: str = "",
    category_override: str = "",
    notes: str = "",
) -> dict[str, Any]:
    prepared = build_capture_payload(
        url,
        title_override=title_override,
        category_override=category_override,
        notes=notes,
    )
    return finalize_capture(prepared)


class CaptureHandler(BaseHTTPRequestHandler):
    server_version = "CodexUrlCapture/2.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        request_path = urlparse(self.path).path
        if request_path in ("/", "/index.html", "/url-capture.html"):
            self._send_html(HTML_PATH.read_text(encoding="utf-8"))
            return
        if request_path == "/api/recent":
            self._send_json({"ok": True, "items": read_history()})
            return
        if request_path == "/api/canvas":
            board = load_canvas_board()
            self._send_json({"ok": True, **board})
            return
        if request_path == "/api/health":
            config = load_config()
            meta = load_notion_meta()
            self._send_json(
                {
                    "ok": True,
                    "host": socket.gethostname(),
                    "vault_path": config.get("OBSIDIAN_VAULT_PATH") or str(DEFAULT_VAULT),
                    "model": summary_model(config),
                    "summary_provider": summary_provider(config),
                    "notion_calendar_url": meta.get("calendar_url", ""),
                }
            )
            return
        if request_path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self._send_json({"ok": False, "error": "Not found"}, status=404)

    def do_POST(self) -> None:
        request_path = urlparse(self.path).path
        if request_path not in ("/api/preview", "/api/capture", "/api/canvas"):
            self._send_json({"ok": False, "error": "Not found"}, status=404)
            return

        try:
            raw_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(raw_length).decode("utf-8"))
            if request_path == "/api/canvas":
                updates = payload.get("cards") or []
                if not isinstance(updates, list):
                    raise ValueError("Canvas cards must be a list")
                board = update_canvas_positions(updates)
                self._send_json({"ok": True, **board})
                return

            url = normalize_whitespace(str(payload.get("url", "")))
            title_override = normalize_whitespace(str(payload.get("title_override", "")))
            category_override = normalize_whitespace(str(payload.get("category_override", "")))
            notes = normalize_whitespace(str(payload.get("notes", "")))
            preview_id = normalize_whitespace(str(payload.get("preview_id", "")))

            if request_path == "/api/preview":
                if not url or not re.match(r"^https?://", url):
                    raise ValueError("请输入有效的网页地址")
                prepared = build_capture_payload(
                    url,
                    title_override=title_override,
                    category_override=category_override,
                    notes=notes,
                )
                result = build_preview_response(prepared, preview_id=store_preview(prepared))
                self._send_json(result)
                return

            if preview_id:
                prepared = load_preview(preview_id)
                result = finalize_capture(prepared)
                clear_preview(preview_id)
                self._send_json(result)
                return

            if not url or not re.match(r"^https?://", url):
                raise ValueError("请输入有效的网页地址")
            result = write_capture(
                url,
                title_override=title_override,
                category_override=category_override,
                notes=notes,
            )
            self._send_json(result)
        except requests.HTTPError as exc:
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
            status_code = exc.response.status_code if exc.response is not None else 500
            if request_path == "/api/preview":
                error = f"预览失败：网页抓取或模型接口返回错误。{detail}"
                hint = "如果错误里出现 401、403、404、model 或 quota，优先检查模型名称、API key 和额度。"
                stage = "preview_failed"
            elif request_path == "/api/capture":
                error = f"同步失败：预览已生成，但写入 Obsidian、Notion 或画布时失败。{detail}"
                hint = "如果错误里出现 Notion validation，请检查数据库字段；如果是连接错误，稍后重试。"
                stage = "sync_failed"
            else:
                error = f"请求失败：{detail}"
                hint = ""
                stage = "request_failed"
            self._send_json({"ok": False, "stage": stage, "error": error, "hint": hint}, status=status_code)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except Exception as exc:
            if request_path == "/api/capture":
                self._send_json(
                    {
                        "ok": False,
                        "stage": "sync_failed",
                        "error": f"同步失败：{exc}",
                        "hint": "预览已经完成，但落地写入没有确认成功。",
                    },
                    status=500,
                )
            elif request_path == "/api/preview":
                self._send_json(
                    {
                        "ok": False,
                        "stage": "preview_failed",
                        "error": f"预览失败：{exc}",
                        "hint": "这一步通常和网页抓取、模型接口或返回格式有关。",
                    },
                    status=500,
                )
            else:
                self._send_json({"ok": False, "error": str(exc)}, status=500)


def main() -> None:
    ensure_runtime()
    config = load_config()
    port = int(config.get("URL_CAPTURE_PORT") or DEFAULT_PORT)
    server = ThreadingHTTPServer(("127.0.0.1", port), CaptureHandler)
    print(f"URL capture page: http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
