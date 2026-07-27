import os
import json
import re
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    RequestBlocked,
    IpBlocked,
    InvalidVideoId,
    CookieInvalid,
)

# --- 配置区域 ---
# OAuth2 用户认证（走你个人 Google 账号的 15GB 配额）
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "")
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")

# 获取频道 ID 字符串，并用逗号拆分成列表
CHANNEL_IDS_STR = os.environ.get("YOUTUBE_CHANNEL_ID", "")
CHANNEL_IDS = [cid.strip() for cid in CHANNEL_IDS_STR.split(",") if cid.strip()]

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
HISTORY_FILE = "processed_videos.txt"

# --- 代理配置（可选）---
# 如果 GitHub Actions 被 YouTube IP 屏蔽，可配置代理绕过。
# 推荐 Webshare 免费套餐：https://www.webshare.io/ (10个免费代理)
WEBSHARE_USERNAME = os.environ.get("WEBSHARE_PROXY_USERNAME", "")
WEBSHARE_PASSWORD = os.environ.get("WEBSHARE_PROXY_PASSWORD", "")
HTTP_PROXY = os.environ.get("HTTP_PROXY", "")
HTTPS_PROXY = os.environ.get("HTTPS_PROXY", "")


def create_ytt_api():
    """初始化 YouTubeTranscriptApi，支持可选代理"""
    # 优先使用 Webshare 代理（内置集成，最简单）
    if WEBSHARE_USERNAME and WEBSHARE_PASSWORD:
        from youtube_transcript_api.proxies import WebshareProxyConfig
        print("使用 Webshare 代理访问 YouTube...")
        proxy_config = WebshareProxyConfig(
            proxy_username=WEBSHARE_USERNAME,
            proxy_password=WEBSHARE_PASSWORD,
        )
        return YouTubeTranscriptApi(proxy_config=proxy_config)

    # 通用代理（兼容各种 HTTP/HTTPS 代理）
    if HTTP_PROXY or HTTPS_PROXY:
        from youtube_transcript_api.proxies import GenericProxyConfig
        print("使用通用代理访问 YouTube...")
        proxy_config = GenericProxyConfig(
            http_url=HTTP_PROXY or None,
            https_url=HTTPS_PROXY or None,
        )
        return YouTubeTranscriptApi(proxy_config=proxy_config)

    return YouTubeTranscriptApi()


# --- API 客户端初始化 ---
# 使用 OAuth2 用户认证（走你个人 Google 账号的 15GB 配额）
scopes = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]
creds = Credentials(
    token=None,
    refresh_token=GOOGLE_OAUTH_REFRESH_TOKEN,
    client_id=GOOGLE_OAUTH_CLIENT_ID,
    client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
    token_uri="https://oauth2.googleapis.com/token",
    scopes=scopes,
)
# 刷新 access token
creds.refresh(GoogleAuthRequest())

drive_service = build("drive", "v3", credentials=creds)
docs_service = build("docs", "v1", credentials=creds)
youtube_service = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

ytt_api = create_ytt_api()


def load_processed_ids():
    """读取已处理过的 video_id 集合"""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_processed_id(video_id):
    """追加写入新的 video_id 到历史文件"""
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(f"{video_id}\n")


def get_latest_video_id(channel_id):
    """获取频道最新发布的一个视频 ID"""
    try:
        response = (
            youtube_service.search()
            .list(
                channelId=channel_id,
                part="id,snippet",
                order="date",
                maxResults=1,
                type="video",
            )
            .execute()
        )

        items = response.get("items", [])
        if not items:
            return None, None
        video_id = items[0]["id"]["videoId"]
        video_title = items[0]["snippet"]["title"]
        return video_id, video_title
    except Exception as e:
        print(f"获取频道 [{channel_id}] 最新视频失败: {e}")
        return None, None


def get_transcript_text(video_id):
    """
    获取 YouTube 视频的字幕文本。
    使用 youtube-transcript-api 库，支持多语言回退。
    """
    # 语言优先级：中文 → 英文 → 任意可用
    preferred_languages = ["zh-Hans", "zh-Hant", "zh", "zh-CN", "en", "en-US", "en-GB"]

    try:
        # 方法 1：按偏好语言直接 fetch（最简洁）
        transcript = ytt_api.fetch(video_id, languages=preferred_languages)
        lines = [snippet.text for snippet in transcript]
        full_transcript = "\n".join(lines)
        if full_transcript.strip():
            return full_transcript

    except (TranscriptsDisabled, NoTranscriptFound):
        # 该视频没有字幕，记录一下但不报错
        print(f"[{video_id}] 该视频没有可用的字幕。")
        return None

    except (RequestBlocked, IpBlocked) as e:
        print(f"[{video_id}] YouTube 已屏蔽当前 IP（GitHub Actions 常见）。")
        print(f"  错误详情: {e}")
        print("  解决方案：")
        print("    1. 在 GitHub Secrets 中添加 WEBSHARE_PROXY_USERNAME 和 WEBSHARE_PROXY_PASSWORD")
        print("       推荐 Webshare 免费套餐：https://www.webshare.io/")
        print("    2. 或设置 HTTP_PROXY / HTTPS_PROXY 环境变量")
        print("    3. 或使用自托管 runner（self-hosted runner）")
        return None

    except VideoUnavailable:
        print(f"[{video_id}] 视频不可用（可能被删除或地区限制）。")
        return None

    except InvalidVideoId:
        print(f"[{video_id}] 视频不存在或 ID 无效。")
        return None

    except Exception as e:
        # 兜底：尝试用 list + find_transcript 方式
        try:
            transcript_list = ytt_api.list(video_id)
            transcript = None

            # 先尝试手动上传的字幕
            for lang in preferred_languages:
                try:
                    transcript = transcript_list.find_manually_created_transcript([lang])
                    break
                except NoTranscriptFound:
                    continue

            # 再尝试自动生成的字幕
            if transcript is None:
                for lang in preferred_languages:
                    try:
                        transcript = transcript_list.find_generated_transcript([lang])
                        break
                    except NoTranscriptFound:
                        continue

            # 最后取任意可用字幕
            if transcript is None:
                transcript = next(iter(transcript_list))

            fetched = transcript.fetch()
            lines = [snippet.text for snippet in fetched]
            full_transcript = "\n".join(lines)
            if full_transcript.strip():
                return full_transcript

        except Exception as inner_e:
            print(f"[{video_id}] 获取字幕发生异常: {inner_e}")
            return None

    print(f"[{video_id}] 未能获取到有效的字幕内容")
    return None


def create_file_in_drive_folder(folder_id, title, text):
    """在指定 Google Drive 文件夹内新建字幕文件（Google Doc）。
    使用 Docs API 写入内容，不占用 Drive 存储配额。
    """
    safe_title = "".join(c for c in title if c not in r'/\:*?"<>|')

    # 第一步：用 Drive API 创建空的 Google Doc
    file_metadata = {
        "name": safe_title,
        "parents": [folder_id],
        "mimeType": "application/vnd.google-apps.document",
    }
    file = drive_service.files().create(body=file_metadata, fields="id").execute()
    file_id = file.get("id")

    # 第二步：用 Docs API 写入文本内容（Google Docs 不占存储配额）
    requests = [
        {
            "insertText": {
                "location": {"index": 1},
                "text": text,
            }
        }
    ]
    docs_service.documents().batchUpdate(
        documentId=file_id, body={"requests": requests}
    ).execute()

    print(f"成功创建字幕文件: {title} (File ID: {file_id})")


if __name__ == "__main__":
    processed_ids = load_processed_ids()

    if not CHANNEL_IDS:
        print("未检测到任何 YOUTUBE_CHANNEL_ID，请检查环境变量配置。")

    success_count = 0
    skip_count = 0
    fail_count = 0

    for channel_id in CHANNEL_IDS:
        print(f"\n正在检查频道: {channel_id} ...")
        video_id, title = get_latest_video_id(channel_id)

        if not video_id:
            print(f"频道 [{channel_id}] 未检测到视频。")
            fail_count += 1
            continue

        if video_id in processed_ids:
            print(f"视频 [{title}] ({video_id}) 已处理过，跳过。")
            skip_count += 1
            continue

        print(f"检测到新视频: {title} ({video_id})")
        transcript = get_transcript_text(video_id)
        if transcript:
            create_file_in_drive_folder(GOOGLE_DRIVE_FOLDER_ID, title, transcript)
            save_processed_id(video_id)
            processed_ids.add(video_id)
            print(f"记录已更新至 {HISTORY_FILE}")
            success_count += 1
        else:
            fail_count += 1

    print(f"\n=== 本次运行汇总 ===")
    print(f"成功: {success_count} | 跳过: {skip_count} | 失败: {fail_count}")
