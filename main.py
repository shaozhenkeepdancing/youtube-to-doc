import os
import json
import re
from datetime import datetime
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
    """清理服务账号 Drive 的存储空间：只删除占配额的非 Google Doc 文件 + 清空回收站。
    Google Doc/Sheet/Slide 本身不占存储配额，保留它们以便后续追加字幕使用。
    """
    try:
        # 1. 清空回收站（回收站里的文件也占配额）
        drive_service.files().emptyTrash().execute()
        print("已清空回收站")
    except Exception as e:
        print(f"清空回收站失败（可忽略）: {e}")

    try:
        # 2. 只删除服务账号拥有的、真正占存储配额的文件（排除 Google Doc/Sheet/Slide）
        # Google Workspace 文件 mimeType 都以 application/vnd.google-apps. 开头
        all_files = []
        page_token = None
        while True:
            resp = (
                drive_service.files()
                .list(
                    pageSize=200,
                    pageToken=page_token,
                    q="'me' in owners and trashed = false and mimeType != 'application/vnd.google-apps.document' and mimeType != 'application/vnd.google-apps.spreadsheet' and mimeType != 'application/vnd.google-apps.presentation'",
                    fields="files(id,name,createdTime,mimeType,size),nextPageToken",
                    orderBy="createdTime desc",
                )
                .execute()
            )
            all_files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        if not all_files:
            print("没有需要清理的存储文件（Google Doc 已保留）")
        else:
            print(f"发现 {len(all_files)} 个占用存储配额的文件，正在清理：")
            for f in all_files:
                size = f.get("size", "N/A")
                print(f"  - [{f.get('mimeType', '?')}] {f['name']} (size: {size})")
                try:
                    drive_service.files().delete(
                        fileId=f["id"], supportsAllDrives=True
                    ).execute()
                except Exception as e:
                    print(f"    删除失败: {e}")
            print("存储文件清理完成")

        # 3. 再次清空回收站
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
            return None, None, None
        video_id = items[0]["id"]["videoId"]
        video_title = items[0]["snippet"]["title"]
        channel_title = items[0]["snippet"].get("channelTitle", "")
        return video_id, video_title, channel_title
    except Exception as e:
        print(f"获取频道 [{channel_id}] 最新视频失败: {e}")
        return None, None, None


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


def format_transcript(transcript_text, line_width=90):
    """将短字幕行合并为满宽段落。

    YouTube 字幕 API 返回的是短行（约 30-40 字符），直接写入文档会偏左半边。
    此函数将短行合并后按目标宽度重新分行，填满整个页面宽度。
    """
    # 1. 合并所有短行为连续文本
    segments = [s.strip() for s in transcript_text.split("\n") if s.strip()]
    flowing_text = " ".join(segments)

    # 2. 按目标宽度重新分行
    words = flowing_text.split(" ")
    lines = []
    current_line = ""
    for word in words:
        test_line = (current_line + " " + word).strip()
        if len(test_line) <= line_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)

    return "\n".join(lines)


def append_to_existing_doc(folder_id, title, text):
    """回退方案：找到共享文件夹里最新的 Google Doc，把格式化字幕追加到末尾。
    用于 Service Account 无法创建新文件（配额为 0）的情况。
    标题使用 Heading 1 样式，正文满宽排列并标注每行字数。
    """
    MAX_DOC_CHARS = 900000  # Google Doc 上限约 102 万字符，留余量

    # 查找文件夹内已有的 Google Doc
    resp = (
        drive_service.files()
        .list(
            q=f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.document' and trashed = false",
            fields="files(id,name)",
            orderBy="createdTime desc",
            pageSize=50,
        )
        .execute()
    )
    docs = resp.get("files", [])

    if not docs:
        print("文件夹中没有已有的 Google Doc，无法追加。")
        print("请手动在 Google Drive 文件夹中创建一个空的 Google Doc，然后重新运行。")
        return False

    # 格式化字幕内容
    formatted_body = format_transcript(text)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    separator = "═" * 40
    timestamp_str = f"采集时间: {now}"

    for doc in docs:
        try:
            doc_info = docs_service.documents().get(documentId=doc["id"]).execute()
            # 计算当前文档末尾索引
            current_length = 1  # Docs API 从 index 1 开始
            for element in doc_info.get("body", {}).get("content", []):
                end_index = element.get("endIndex", 1)
                if end_index > current_length:
                    current_length = end_index

            # 构建追加文本
            append_text = f"\n{separator}\n{title}\n{timestamp_str}\n\n{formatted_body}\n"

            remaining = MAX_DOC_CHARS - current_length
            if remaining < len(append_text):
                print(f"  文档 [{doc['name']}] 空间不足（剩余 {remaining} 字符），尝试下一个...")
                continue

            # 计算插入点和各部分索引
            insert_index = current_length - 1
            idx = insert_index
            idx += 1  # \n
            idx += len(separator)  # separator
            idx += 1  # \n
            title_start = idx; idx += len(title); title_end = idx
            idx += 1  # \n
            idx += len(timestamp_str)  # timestamp
            idx += 2  # \n\n
            body_start = idx; idx += len(formatted_body); body_end = idx

            # 插入文本并应用样式
            requests = [
                {
                    "insertText": {
                        "location": {"index": insert_index},
                        "text": append_text,
                    }
                },
                # 标题：Heading 1 + 居中
                {
                    "updateParagraphStyle": {
                        "range": {"startIndex": title_start, "endIndex": title_end},
                        "paragraphStyle": {
                            "namedStyle": "HEADING_1",
                            "alignment": "CENTER",
                        },
                        "fields": "namedStyle,alignment",
                    }
                },
                # 正文：两端对齐
                {
                    "updateParagraphStyle": {
                        "range": {"startIndex": body_start, "endIndex": body_end},
                        "paragraphStyle": {
                            "alignment": "JUSTIFIED",
                        },
                        "fields": "alignment",
                    }
                },
            ]
            docs_service.documents().batchUpdate(
                documentId=doc["id"], body={"requests": requests}
            ).execute()

            print(f"成功追加字幕到已有文档: {doc['name']} (File ID: {doc['id']})")
            return True

        except Exception as e:
            print(f"  追加到文档 [{doc['name']}] 失败: {e}")
            continue

    print("所有已有文档都已满，请手动创建新的空 Google Doc 放入文件夹后重试。")
    return False


def create_file_in_drive_folder(folder_id, title, text):
    """在指定 Google Drive 文件夹内新建字幕文件（Google Doc）。
    标题使用 Heading 1 样式居中显示，正文满宽排列并标注每行字数。
    如果创建失败（配额不足），自动回退到追加到已有文档。
    """
    safe_title = "".join(c for c in title if c not in r'/\:*?"<>|')
    formatted_body = format_transcript(text)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    try:
        # 第一步：创建空的 Google Doc
        file_metadata = {
            "name": safe_title,
            "parents": [folder_id],
            "mimeType": "application/vnd.google-apps.document",
        }
        file = drive_service.files().create(body=file_metadata, fields="id").execute()
        file_id = file.get("id")

        # 第二步：构建格式化文本
        separator = "═" * 40
        timestamp_str = f"采集时间: {now}"
        full_text = f"{separator}\n{title}\n{timestamp_str}\n\n{formatted_body}\n"

        # 计算各部分在文档中的索引（1-based）
        idx = 1
        idx += len(separator)  # separator
        idx += 1  # \n
        title_start = idx; idx += len(title); title_end = idx
        idx += 1  # \n
        idx += len(timestamp_str)  # timestamp
        idx += 2  # \n\n
        body_start = idx; idx += len(formatted_body); body_end = idx

        # 第三步：插入文本并应用样式
        requests = [
            {
                "insertText": {
                    "location": {"index": 1},
                    "text": full_text,
                }
            },
            # 标题：Heading 1 样式 + 居中
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": title_start, "endIndex": title_end},
                    "paragraphStyle": {
                        "namedStyle": "HEADING_1",
                        "alignment": "CENTER",
                    },
                    "fields": "namedStyle,alignment",
                }
            },
            # 正文：两端对齐
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": body_start, "endIndex": body_end},
                    "paragraphStyle": {
                        "alignment": "JUSTIFIED",
                    },
                    "fields": "alignment",
                }
            },
        ]
        docs_service.documents().batchUpdate(
            documentId=file_id, body={"requests": requests}
        ).execute()

        print(f"成功创建字幕文件: {title} (File ID: {file_id})")

    except Exception as e:
        error_str = str(e)
        if "storageQuotaExceeded" in error_str or "quota" in error_str.lower():
            print(f"创建新文件失败（配额不足），切换到追加模式...")
            print(f"  错误: {e}")
            success = append_to_existing_doc(folder_id, title, text)
            if not success:
                raise
        else:
            raise


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
        video_id, video_title, channel_title = get_latest_video_id(channel_id)

        if not video_id:
            print(f"频道 [{channel_id}] 未检测到视频。")
            fail_count += 1
            continue

        # 组合标题：频道名 + 空格 + 视频标题
        title = f"{channel_title} {video_title}".strip() if channel_title else video_title

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
