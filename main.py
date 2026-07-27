import os
import json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi

# --- 配置区域 ---
SERVICE_ACCOUNT_INFO = json.loads(os.environ.get("GCP_SERVICE_ACCOUNT_KEY", "{}"))
GOOGLE_DOC_ID = os.environ.get("GOOGLE_DOC_ID")
CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
HISTORY_FILE = "processed_videos.txt"  # 记录已处理 video_id 的文件名

# --- API 客户端初始化 ---
scopes = ["https://www.googleapis.com/auth/documents"]
creds = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=scopes)
docs_service = build("docs", "v1", credentials=creds)
youtube_service = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


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


def get_transcript(video_id):
    """提取视频字幕文本"""
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(
            video_id, languages=["zh-Hans", "zh", "en"]
        )
        text = "\n".join([item["text"] for item in transcript_list])
        return text
    except Exception as e:
        print(f"获取字幕失败或该视频无可用字幕: {e}")
        return None


def append_to_google_doc(doc_id, title, text):
    """将文本追加到指定 Google Doc 末尾"""
    content_to_insert = f"\n\n=== {title} ===\n\n{text}\n"

    doc = docs_service.documents().get(documentId=doc_id).execute()
    end_index = doc.get("body").get("content")[-1].get("endIndex") - 1

    requests = [
        {
            "insertText": {
                "location": {"index": end_index},
                "text": content_to_insert,
            }
        }
    ]

    docs_service.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests}
    ).execute()
    print(f"成功写入文档: {title}")


if __name__ == "__main__":
    processed_ids = load_processed_ids()
    video_id, title = get_latest_video_id(CHANNEL_ID)

    if not video_id:
        print("未检测到频道视频。")
    elif video_id in processed_ids:
        print(f"视频 [{title}] ({video_id}) 已处理过，跳过。")
    else:
        print(f"检测到新视频: {title} ({video_id})")
        transcript = get_transcript(video_id)
        if transcript:
            append_to_google_doc(GOOGLE_DOC_ID, title, transcript)
            save_processed_id(video_id)
            print(f"记录已更新至 {HISTORY_FILE}")