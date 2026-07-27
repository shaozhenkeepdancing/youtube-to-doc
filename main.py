import os
import urllib.request
import json
import re
import xml.etree.ElementTree as ET
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# --- 配置区域 ---
SERVICE_ACCOUNT_INFO = json.loads(os.environ.get("GCP_SERVICE_ACCOUNT_KEY", "{}"))
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")

# 获取频道 ID 字符串，并用逗号拆分成列表
CHANNEL_IDS_STR = os.environ.get("YOUTUBE_CHANNEL_ID", "")
CHANNEL_IDS = [cid.strip() for cid in CHANNEL_IDS_STR.split(",") if cid.strip()]

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
HISTORY_FILE = "processed_videos.txt"

# --- API 客户端初始化 ---
scopes = ["https://www.googleapis.com/auth/drive.file"]
creds = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=scopes)
drive_service = build("drive", "v3", credentials=creds)
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

import urllib.request
import json

def get_transcript_text(video_id):
    # 使用 YouTube 官方 Web 端 InnerTube 接口，格式 100% 规范
    url = "https://www.youtube.com/youtubei/v1/player"
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20231201.00.00",
                "hl": "en",
                "gl": "US"
            }
        },
        "videoId": video_id
    }
    
    try:
        data_bytes = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data_bytes, headers=headers)
        response = urllib.request.urlopen(req, timeout=10)
        res_data = json.loads(response.read().decode('utf-8'))
        
        # 解析字幕数据
        captions = res_data.get('captions', {}).get('playerCaptionsTracklistRenderer', {}).get('captionTracks', [])
        
        if not captions:
            print(f"[{video_id}] 该视频未提供可提取的字幕轨")
            return None

        # 优先提取英/中文字幕轨
        target_track = captions[0]
        for track in captions:
            lang = track.get('languageCode', '')
            if lang in ['en', 'zh-Hans', 'zh-Hant', 'zh']:
                target_track = track
                break

        # 拿到字幕 URL 并请求 json3 格式
        sub_url = target_track['baseUrl']
        if 'fmt=' not in sub_url:
            sub_url += '&fmt=json3'
            
        sub_req = urllib.request.Request(sub_url, headers={'User-Agent': 'Mozilla/5.0'})
        sub_data = json.loads(urllib.request.urlopen(sub_req, timeout=10).read().decode('utf-8'))

        lines = []
        for event in sub_data.get('events', []):
            if 'segs' in event:
                text = "".join([seg.get('utf8', '') for seg in event['segs']]).strip()
                if text and text != '\n':
                    lines.append(text)

        full_transcript = "\n".join(lines)
        return full_transcript if full_transcript else None

    except Exception as e:
        print(f"[{video_id}] 获取字幕发生异常: {e}")
        return None

def create_file_in_drive_folder(folder_id, title, text):
    """在指定 Google Drive 文件夹内新建字幕文件"""
    safe_title = "".join(c for c in title if c not in r'/\:*?"<>|')
    
    file_metadata = {
        "name": safe_title,
        "parents": [folder_id],
        "mimeType": "application/vnd.google-apps.document"
    }
    
    from googleapiclient.http import MediaInMemoryUpload
    media = MediaInMemoryUpload(text.encode("utf-8"), mimetype="text/plain", resumable=True)

    file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()
    
    print(f"成功创建字幕文件: {title} (File ID: {file.get('id')})")


if __name__ == "__main__":
    processed_ids = load_processed_ids()
    
    if not CHANNEL_IDS:
        print("未检测到任何 YOUTUBE_CHANNEL_ID，请检查环境变量配置。")
    
    # 循环遍历每一个频道
    for channel_id in CHANNEL_IDS:
        print(f"\n正在检查频道: {channel_id} ...")
        video_id, title = get_latest_video_id(channel_id)

        if not video_id:
            print(f"频道 [{channel_id}] 未检测到视频。")
            continue

        if video_id in processed_ids:
            print(f"视频 [{title}] ({video_id}) 已处理过，跳过。")
            continue

        print(f"检测到新视频: {title} ({video_id})")
        transcript = get_transcript_text(video_id)
        if transcript:
            create_file_in_drive_folder(GOOGLE_DRIVE_FOLDER_ID, title, transcript)
            save_processed_id(video_id)
            # 及时更新内存中的已处理集合，防止单次运行中有重复视频
            processed_ids.add(video_id)
            print(f"记录已更新至 {HISTORY_FILE}")
