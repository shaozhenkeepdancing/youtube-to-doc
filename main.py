import os
import json
import re
from google.oauth2.service_account import Credentials
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
# Service Account 认证（JSON 密钥）
SERVICE_ACCOUNT_INFO = json.loads(os.environ.get("GCP_SERVICE_ACCOUNT_KEY", "{}"))
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")

# 获取频道 ID 字符串，并用逗号拆分成列表
CHANNEL_IDS_STR = os.environ.get("YOUTUBE_CHANNEL_ID", "")
CHANNEL_IDS = [cid.strip() for cid in CHANNEL_IDS_STR.split(",") if cid.strip()]

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
HISTORY_FILE = "processed_videos.txt"

# --- 代理配置（可选）---
WEBSHARE_USERNAME = os.environ.get("WEBSHARE_PROXY_USERNAME", "")
WEBSHARE_PASSWORD = os.environ.get("WEBSHARE_PROXY_PASSWORD", "")
HTTP_PROXY = os.environ.get("HTTP_PROXY", "")
HTTPS_PROXY = os.environ.get("HTTPS_PROXY", "")


def create_ytt_api():
    """初始化 YouTubeTranscriptApi，支持可选代理"""
    if WEBSHARE_USERNAME and WEBSHARE_PASSWORD:
        from youtube_transcript_api.proxies import WebshareProxyConfig
        print("使用 Webshare 代理访问 YouTube...")
        proxy_config = WebshareProxyConfig(
            proxy_username=WEBSHARE_USERNAME,
            proxy_password=WEBSHARE_PASSWORD,
        )
        return YouTubeTranscriptApi(proxy_config=proxy_config)

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
# 需要 drive（完整权限）才能列出/删除文件进行清理
scopes = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]
creds = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=scopes)
drive_service = build("drive", "v3", credentials=creds)
docs_service = build("docs", "v1", credentials=creds)
youtube_service = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

ytt_api = create_ytt_api()


def cleanup_drive():
    """清理服务账号 Drive 的存储空间：清空回收站 + 删除旧文件。
    Google Doc 本身不占存储配额，但之前用 MediaInMemoryUpload 上传的
    文件会占配额，需要清理掉才能继续创建新文件。
    """
    try:
        # 1. 清空回收站（回收站里的文件也占配额）
        drive_service.files().emptyTrash().execute()
        print("已清空回收站")
    except Exception as e:
        print(f"清空回收站失败（可忽略）: {e}")

    try:
        # 2. 列出服务账号创建的所有文件
        all_files = []
        page_token = None
        while True:
            resp = (
                drive_service.files()
                .list(
                    pageSize=200,
                    pageToken=page_token,
                    q="trashed = false",
                    fields="files(id,name,createdTime,mimeType),nextPageToken",
                    orderBy="createdTime desc",
                )
                .execute()
            )
            all_files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        if not all_files:
            print("Drive 中没有文件，无需清理")
            return

        print(f"Drive 中共有 {len(all_files)} 个文件")

        # 3. 保留最近 10 个文件，删除更早的
        KEEP_COUNT = 10
        if len(all_files) > KEEP_COUNT:
            old_files = all_files[KEEP_COUNT:]
            print(f"保留最近 {KEEP_COUNT} 个文件，删除 {len(old_files)} 个旧文件...")
            for f in old_files:
                try:
                    drive_service.files().delete(
                        fileId=f["id"], supportsAllDrives=True
                    ).execute()
                except Exception:
                    pass
            print("旧文件清理完成")

        # 4. 再次清空回收站（刚删除的文件进回收站了）
        try:
            drive_service.files().emptyTrash().execute()
            print("已再次清空回收站")
        except Exception:
            pass

    except Exception as e:
        print(f"清理 Drive 时出错: {e}")


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
    preferred_languages = ["zh-Hans", "zh-Hant", "zh", "zh-CN", "en", "en-US", "en-GB"]

    try:
        transcript = ytt_api.fetch(video_id, languages=preferred_languages)
        lines = [snippet.text for snippet in transcript]
        full_transcript = "\n".join(lines)
        if full_transcript.strip():
            return full_transcript

    except (TranscriptsDisabled, NoTranscriptFound):
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
        try:
            transcript_list = ytt_api.list(video_id)
            transcript = None

            for lang in preferred_languages:
                try:
                    transcript = transcript_list.find_manually_created_transcript([lang])
                    break
                except NoTranscriptFound:
                    continue

            if transcript is None:
                for lang in preferred_languages:
                    try:
                        transcript = transcript_list.find_generated_transcript([lang])
                        break
                    except NoTranscriptFound:
                        continue

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
    使用 Docs API 写入内容，Google Doc 不占用 Drive 存储配额。
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

    # 第二步：用 Docs API 写入文本内容
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
    # 先清理 Drive 空间，释放配额
    print("=== 清理 Google Drive 存储空间 ===")
    cleanup_drive()
    print()

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
