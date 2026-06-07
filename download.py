#!/usr/bin/env python3
"""
YouTube Downloader — CLI Edition
适用于 GitHub Actions 环境，下载 YouTube 视频/音频/字幕后上传到阿里云盘

Usage:
  python download.py --url <YouTube URL> --type video --quality best
  python download.py --url <YouTube URL> --type audio --quality 192
  python download.py --url <YouTube URL> --type subtitle

环境变量（GitHub Secrets 传入）:
  ALIYUNDRIVE_REFRESH_TOKEN  阿里云盘 refresh_token（必填，用于上传）
  ALIYUNDRIVE_PARENT_ID      阿里云盘目标目录 ID（可选，默认 root）
"""

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import yt_dlp

# ── 可选依赖 ────────────────────────────────────────────────────────

WHISPER_AVAILABLE = False
try:
    import whisper

    WHISPER_AVAILABLE = True
except ImportError:
    pass

TRANSLATOR_AVAILABLE = False
try:
    from deep_translator import GoogleTranslator

    TRANSLATOR_AVAILABLE = True
except ImportError:
    pass

# ── 日志 ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("yt-downloader")


# ── 工具函数 ─────────────────────────────────────────────────────────

def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def format_duration(seconds: int) -> str:
    if not seconds:
        return "00:00"
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"


def is_youtube_url(url: str) -> bool:
    return bool(
        re.match(
            r"(https?://)?(www\.|music\.)?(youtube\.com|youtu\.be)/",
            url.strip(),
        )
    )


# ── 字幕相关 ─────────────────────────────────────────────────────────

SUBTITLE_LANG_MAP = {
    "zh-Hans": "中文简体",
    "zh-Hant": "中文繁体",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "pt": "葡萄牙语",
    "ru": "俄语",
    "ar": "阿拉伯语",
    "th": "泰语",
    "vi": "越南语",
    "id": "印尼语",
}

SRT_PATTERN = re.compile(
    r"(\d+)\n"
    r"(\d{2}:\d{2}:\d{2}[.,]\d{3}) --> (\d{2}:\d{2}:\d{2}[.,]\d{3})\n"
    r"(.*?)(?=\n\n|\Z)",
    re.DOTALL,
)


def parse_srt(srt_path: str) -> list:
    segments = []
    try:
        with open(srt_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        for m in SRT_PATTERN.finditer(content):
            text = m.group(4).strip().replace("\n", " ")
            segments.append((int(m.group(1)), m.group(2), m.group(3), text))
    except Exception as e:
        log.error(f"解析字幕文件失败 {srt_path}: {e}")
    return segments


def write_srt(segments: list, output_path: str):
    lines = []
    for seq, start, end, text in segments:
        lines.extend([str(seq), f"{start} --> {end}", text, ""])
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_txt(segments: list, output_path: str):
    texts = [seg[3] for seg in segments]
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(texts))


def translate_segments(segments: list, source_lang: str = "auto") -> list:
    if not TRANSLATOR_AVAILABLE:
        log.warning("翻译模块不可用，保留原文")
        return segments
    if source_lang.startswith("zh"):
        return segments
    try:
        translator = GoogleTranslator(source=source_lang, target="zh-CN")
        translated = []
        for seq, start, end, text in segments:
            try:
                result = translator.translate(text)
                translated.append((seq, start, end, result or text))
            except Exception as e:
                log.warning(f"翻译第 {seq} 条失败: {e}")
                translated.append((seq, start, end, text))
        log.info(f"翻译完成: {len(translated)}/{len(segments)} 条")
        return translated
    except Exception as e:
        log.error(f"翻译过程出错: {e}")
        return segments


def get_available_subtitles(info: dict) -> dict:
    result = {}
    for lang in info.get("subtitles") or {}:
        if lang not in result:
            display = SUBTITLE_LANG_MAP.get(lang, lang)
            result[lang] = {"code": lang, "display": display, "type": "manual"}
    for lang in info.get("automatic_captions") or {}:
        base = lang.split("-raw")[0]
        if base not in result:
            display = SUBTITLE_LANG_MAP.get(base, base)
            result[base] = {"code": base, "display": display, "type": "auto"}
    sorted_result = dict(
        sorted(
            result.items(),
            key=lambda x: (0 if x[0].startswith("zh") else 1, x[1].get("display", x[0])),
        )
    )
    return sorted_result


# ── Whisper 语音识别 ───────────────────────────────────────────────

WHISPER_MODEL = None
WHISPER_LOCK = False  # Simple flag, no threading needed in CLI


def get_whisper_model():
    global WHISPER_MODEL
    if WHISPER_MODEL is None:
        log.info("正在加载 Whisper 模型（首次加载需下载，约 1.4GB）...")
        WHISPER_MODEL = whisper.load_model("base")
        log.info("Whisper 模型加载完成")
    return WHISPER_MODEL


def whisper_transcribe(audio_path: str) -> list:
    """用 Whisper 语音识别生成字幕段 [(序号, 开始, 结束, 文本), ...]"""
    model = get_whisper_model()
    result = model.transcribe(audio_path, language="zh")
    segments = []
    for i, seg in enumerate(result["segments"], 1):
        start = seg["start"]
        end = seg["end"]
        text = seg["text"].strip()
        if text:
            start_str = (
                f"{int(start//3600):02d}:{int(start%3600//60):02d}:{start%60:06.3f}".replace(
                    ".", ","
                )
            )
            end_str = (
                f"{int(end//3600):02d}:{int(end%3600//60):02d}:{end%60:06.3f}".replace(
                    ".", ","
                )
            )
            segments.append((i, start_str, end_str, text))
    return segments


# ── 阿里云盘上传 ──────────────────────────────────────────────────────

def find_folder_id(folder_name: str, access_token: str, drive_id: str) -> str | None:
    """
    在阿里云盘根目录下递归查找文件夹，返回其 file_id
    支持子路径: "Music/YouTube" 或 "我的资源/视频"
    """
    import requests as req

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # 支持子路径: 按 / 分割逐级查找
    parts = folder_name.strip("/").split("/")
    current_parent = "root"

    for part in parts:
        if not part:
            continue
        found = False
        marker = None
        while True:
            body = {
                "drive_id": drive_id,
                "parent_file_id": current_parent,
                "limit": 100,
            }
            if marker:
                body["marker"] = marker
            resp = req.post(
                "https://api.aliyundrive.com/v2/file/list",
                headers=headers,
                json=body,
                timeout=30,
            )
            if resp.status_code != 200:
                log.error(f"查询文件夹失败: HTTP {resp.status_code}")
                return None

            data = resp.json()
            for item in data.get("items", []):
                if item["type"] == "folder" and item["name"] == part:
                    current_parent = item["file_id"]
                    found = True
                    break
            if found:
                break
            marker = data.get("next_marker", "")
            if not marker:
                break

        if not found:
            log.warning(f"未找到文件夹 '{part}'（在 {current_parent} 下），使用默认目录")
            return None

    return current_parent if current_parent != "root" else None


def upload_to_aliyundrive(local_path: str, parent_id: str | None = None) -> dict:
    """
    使用阿里云盘官方 API 上传文件（不依赖第三方库）
    API 文档: https://www.aliyundrive.com/

    参数:
      parent_id: 目标文件夹 ID，为 None 时从环境变量 ALIYUNDRIVE_PARENT_ID 或 "root"
    """
    import requests as req

    refresh_token = os.environ.get("ALIYUNDRIVE_REFRESH_TOKEN", "")
    if parent_id is None:
        parent_id = os.environ.get("ALIYUNDRIVE_PARENT_ID") or "root"

    if not refresh_token:
        return {"success": False, "error": "ALIYUNDRIVE_REFRESH_TOKEN 未设置"}

    if not os.path.isfile(local_path):
        return {"success": False, "error": f"文件不存在: {local_path}"}

    file_size = os.path.getsize(local_path)
    file_name = os.path.basename(local_path)

    log.info(f"正在上传到阿里云盘: {file_name} ({human_size(file_size)})...")

    try:
        # Step 1: 用 refresh_token 获取 access_token
        resp = req.post(
            "https://api.aliyundrive.com/v2/account/token",
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code != 200:
            return {"success": False, "error": f"获取 token 失败: HTTP {resp.status_code}: {resp.text[:200]}"}

        token_data = resp.json()
        access_token = token_data.get("access_token", "")
        drive_id = token_data.get("default_drive_id", "")
        new_refresh_token = token_data.get("refresh_token", refresh_token)

        if not access_token:
            return {"success": False, "error": f"获取 access_token 失败: {token_data.get('message', 'unknown')}"}

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        # Step 2: 创建文件请求（获取上传地址）
        resp = req.post(
            "https://api.aliyundrive.com/v2/file/create",
            json={
                "drive_id": drive_id,
                "name": file_name,
                "parent_file_id": parent_id,
                "type": "file",
                "size": file_size,
                "check_name_mode": "auto_rename",
            },
            headers=headers,
            timeout=30,
        )
        if resp.status_code != 201:
            return {"success": False, "error": f"创建文件失败: HTTP {resp.status_code}: {resp.text[:200]}"}

        file_data = resp.json()
        file_id = file_data.get("file_id", "")
        upload_url = file_data.get("upload_url", "")
        rapid_upload = file_data.get("rapid_upload", False)

        # 如果秒传成功，直接完成
        if rapid_upload:
            log.info(f"秒传成功! 文件: {file_name}")
            return {"success": True, "file_name": file_name, "file_size": file_size}

        # Step 3: 上传文件内容到 upload_url
        if not upload_url:
            # 可能需要分片上传
            part_info_list = file_data.get("part_info_list", [])
            if not part_info_list:
                return {"success": False, "error": f"未获取到上传地址: {file_data}"}

            # 逐片上传
            with open(local_path, "rb") as f:
                for part in part_info_list:
                    part_url = part.get("upload_url", "")
                    part_number = part.get("part_number", 1)

                    # 读取对应分片
                    if part_number == len(part_info_list):
                        chunk = f.read()
                    else:
                        chunk = f.read(part.get("size", 0))

                    put_resp = req.put(part_url, data=chunk, timeout=300)
                    if put_resp.status_code not in (200, 201):
                        return {
                            "success": False,
                            "error": f"分片 {part_number} 上传失败: HTTP {put_resp.status_code}",
                        }

            # Step 4: 完成上传
            resp = req.post(
                "https://api.aliyundrive.com/v2/file/complete",
                json={"drive_id": drive_id, "file_id": file_id, "upload_id": file_data.get("upload_id", "")},
                headers=headers,
                timeout=30,
            )
            if resp.status_code not in (200, 201):
                return {"success": False, "error": f"完成上传失败: {resp.text[:200]}"}

        else:
            # 单链接上传
            file_size_upload = os.path.getsize(local_path)
            with open(local_path, "rb") as f:
                put_resp = req.put(upload_url, data=f, timeout=600)

            if put_resp.status_code not in (200, 201, 204):
                return {
                    "success": False,
                    "error": f"文件上传失败: HTTP {put_resp.status_code}",
                }

            # 非秒传情况下不需要 complete 步骤
            log.info(f"文件上传 HTTP 状态: {put_resp.status_code}")

        log.info(f"上传成功! 文件: {file_name}")
        return {"success": True, "file_name": file_name, "file_size": file_size}

    except Exception as e:
        log.error(f"阿里云盘上传失败: {e}")
        return {"success": False, "error": str(e)}


# ── 下载核心 ─────────────────────────────────────────────────────────

def report_progress(d: dict, stdout_report: bool = True):
    """yt-dlp 进度钩子，输出 JSON 进度以便 workflow 跟踪"""
    if not stdout_report:
        return
    status = d.get("status", "")
    if status == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
        downloaded = d.get("downloaded_bytes", 0)
        speed = d.get("speed", 0) or 0
        eta = d.get("eta", 0) or 0
        pct = round(downloaded / total * 100, 1) if total > 0 else 0
        sys.stdout.write(
            json.dumps(
                {
                    "event": "progress",
                    "percent": pct,
                    "downloaded": downloaded,
                    "total": total,
                    "speed": speed,
                    "eta": eta,
                }
            )
            + "\n"
        )
        sys.stdout.flush()
    elif status == "finished":
        sys.stdout.write(json.dumps({"event": "finished"}) + "\n")
        sys.stdout.flush()


def _get_cookie_file() -> str | None:
    """返回 cookie 文件路径。
    优先顺序: YT_COOKIE_FILE 环境变量 (文件已存在) → YT_COOKIES 环境变量 (临时写出)
    """
    # 方式1: 直接指定文件路径（推荐）
    cookie_file = os.environ.get("YT_COOKIE_FILE", "")
    if cookie_file and os.path.isfile(cookie_file):
        log.info(f"已加载 Cookie 文件: {cookie_file}")
        return cookie_file

    # 方式2: 从环境变量内容写出（备选）
    cookies = os.environ.get("YT_COOKIES", "")
    if not cookies:
        return None

    import tempfile

    # 修复 GitHub Actions env var TAB→空格的问题：
    # Netscape cookie 文件要求 TAB 分隔，但 env var 可能把 TAB 转成空格。
    # 对每行按任意空白分割前6个字段，再用 TAB 重新连接。
    fixed_lines = []
    for line in cookies.splitlines():
        if not line.strip() or line.startswith("#"):
            fixed_lines.append(line)
            continue
        # split(None, 6) 按任意空白分，最多7段（domain flag path secure expires name value）
        parts = line.split(None, 6)
        if len(parts) >= 7:
            fixed_lines.append("\t".join(parts))
        else:
            fixed_lines.append(line)

    content = "\n".join(fixed_lines)

    f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    f.write(content)
    f.close()
    log.info(f"已加载 Cookie（{len(content)} 字节, {len(fixed_lines)} 行）")
    return f.name


def _build_base_opts(output_dir: str) -> dict:
    """构建 yt-dlp 基础选项"""
    opts = {
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "progress_hooks": [report_progress],
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "retries": 3,
        "fragment_retries": 3,
        "continuedl": True,
    }
    cookie_file = _get_cookie_file()
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts


def download_video(url: str, quality: str, output_dir: str) -> str:
    """下载视频，返回最终文件路径"""
    fmt_map = {
        "best": "best*",
        "1080p": "best*[height<=1080]",
        "720p": "best*[height<=720]",
        "480p": "best*[height<=480]",
    }
    fmt = fmt_map.get(quality, "best*")

    ydl_opts = {
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "progress_hooks": [report_progress],
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "retries": 3,
        "fragment_retries": 3,
        "continuedl": True,
    }
    cookie_file = _get_cookie_file()
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    log.info(f"开始下载视频 (质量: {quality})...")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise Exception("yt-dlp 返回空结果，可能是机器人检测。请确保 YouTube Cookie 有效")
        title = info.get("title", "未知视频")
        safe_title = ydl.prepare_filename(info)
        final_path = safe_title.rsplit(".", 1)[0] + ".mp4"

        # 如果文件不存在，查找实际生成的文件
        if not os.path.isfile(final_path):
            candidates = [
                f
                for f in os.listdir(output_dir)
                if f.startswith(Path(safe_title).stem) and f.endswith(".mp4")
            ]
            if candidates:
                final_path = os.path.join(output_dir, candidates[0])

        log.info(f"下载完成: {title}")
        return final_path


def download_audio(url: str, quality: str, output_dir: str) -> str:
    """下载音频并转 MP3，返回最终文件路径"""
    # 选择最佳音视频格式：从视频中提取音频（兼容无纯音频流的视频）
    fmt = "best[acodec!=none]/best"

    # FFmpegExtractAudio quality: "0"=best, "5"=default, or kbps like "128"
    # 如果传入 "best"，转为 "0"（最佳）
    audio_quality = quality
    if audio_quality in ("best", "0", ""):
        audio_quality = "0"

    ydl_opts = {
        "format": fmt,
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "progress_hooks": [report_progress],
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "retries": 3,
        "fragment_retries": 3,
        "continuedl": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": audio_quality,
            }
        ],
    }
    cookie_file = _get_cookie_file()
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    log.info(f"开始下载音频 (质量: {audio_quality})...")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title", "未知视频")

        # 查找生成的 mp3 文件
        candidates = [
            f
            for f in os.listdir(output_dir)
            if f.endswith(".mp3")
        ]
        if candidates:
            # 按修改时间排序，取最新的
            candidates.sort(
                key=lambda x: os.path.getmtime(os.path.join(output_dir, x)),
                reverse=True,
            )
            final_path = os.path.join(output_dir, candidates[0])
        else:
            final_path = ""

        log.info(f"音频下载完成: {title}")
        return final_path


def download_subtitle(url: str, output_dir: str) -> dict:
    """下载字幕（如有），否则用 Whisper 语音识别，返回结果信息"""
    log.info("正在获取字幕...")

    # 第一步：尝试下载 YouTube 字幕
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "writesubtitles": True,
        "writeautomaticsubs": True,
        "subtitleslangs": ["zh-Hans", "zh-Hant", "en"],
        "convertsubtitles": "srt",
        "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["web", "android"]}},
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
    }
    cookie_file = _get_cookie_file()
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    title = "未知视频"
    title_base = None
    yt_subtitles_found = False

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                title = info.get("title", "未知视频")
                safe_title = yt_dlp.YoutubeDL(ydl_opts).prepare_filename(info)
                title_base = os.path.basename(safe_title).rsplit(".", 1)[0]

        # 查找下载的字幕文件
        if title_base:
            srt_files = [
                f for f in os.listdir(output_dir) if f.endswith(".srt") and f.startswith(title_base)
            ]

            if srt_files:
                yt_subtitles_found = True
                # 有字幕，选择最佳语言
                chosen_srt = None
                chosen_lang = "unknown"
                for target in ["zh-Hans", "zh-Hant", "en"]:
                    matches = [f for f in srt_files if f".{target}." in f]
                    if matches:
                        chosen_srt = os.path.join(output_dir, matches[0])
                        chosen_lang = target
                        break
                if not chosen_srt:
                    chosen_srt = os.path.join(output_dir, srt_files[0])
                    parts = os.path.basename(chosen_srt).rsplit(".", 2)
                    chosen_lang = parts[-2] if len(parts) >= 3 else "unknown"

                log.info(f"找到 YouTube 字幕 (语言: {chosen_lang})")

                # 解析 + 翻译
                segments = parse_srt(chosen_srt)
                if not segments:
                    raise Exception(f"字幕文件解析失败: {chosen_srt}")

                translated = False
                if not chosen_lang.startswith("zh"):
                    log.info("正在翻译字幕为中文...")
                    segments = translate_segments(segments, chosen_lang)
                    translated = True

                # 保存 .srt
                srt_output = os.path.join(output_dir, f"{title_base}.zh-Hans.srt")
                write_srt(segments, srt_output)

                # 保存 .txt
                txt_output = os.path.join(output_dir, f"{title_base}.zh-Hans.txt")
                write_txt(segments, txt_output)

                # 清理临时字幕文件
                for f in srt_files:
                    fp = os.path.join(output_dir, f)
                    if fp != srt_output:
                        try:
                            os.remove(fp)
                        except OSError:
                            pass

                return {
                    "title": title,
                    "srt_file": srt_output,
                    "txt_file": txt_output,
                    "source_lang": chosen_lang,
                    "translated": translated,
                    "from_speech": False,
                }
    except Exception as e:
        log.warning(f"YouTube 字幕提取失败: {e}，尝试 Whisper 语音识别...")
        # 回退到 Whisper

    # 第二步：没有字幕 → 尝试 Whisper 语音识别
    if WHISPER_AVAILABLE:
        log.info("YouTube 无字幕，尝试 Whisper 语音识别...")

        # 下载音频
        audio_path = os.path.join(output_dir, f"tmp_audio_{int(time.time())}.mp3")
        audio_opts = {
            "format": "bestaudio/best",
            "outtmpl": audio_path,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "128",
                }
            ],
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(audio_opts) as ydl:
            ydl.extract_info(url, download=True)

        # 查找实际生成的音频文件
        actual_audio = audio_path
        if not os.path.isfile(actual_audio):
            candidates = [f for f in os.listdir(output_dir) if f.endswith(".mp3")]
            if candidates:
                actual_audio = os.path.join(output_dir, candidates[0])

        if not os.path.isfile(actual_audio):
            raise Exception("音频文件下载失败")

        log.info("正在进行语音识别...")
        segments = whisper_transcribe(actual_audio)

        if not segments:
            raise Exception("语音识别未能生成有效文本")

        # 保存字幕
        safe_title = re.sub(r"[^\w\s-]", "", title).strip()[:80]
        safe_title = re.sub(r"[-\s]+", "_", safe_title)
        title_base = safe_title or f"whisper_{int(time.time())}"

        srt_output = os.path.join(output_dir, f"{title_base}.zh-Hans.srt")
        write_srt(segments, srt_output)

        txt_output = os.path.join(output_dir, f"{title_base}.zh-Hans.txt")
        write_txt(segments, txt_output)

        # 清理临时音频
        if os.path.isfile(actual_audio):
            try:
                os.remove(actual_audio)
            except OSError:
                pass

        return {
            "title": title,
            "srt_file": srt_output,
            "txt_file": txt_output,
            "source_lang": "zh-Hans",
            "translated": False,
            "from_speech": True,
        }

    raise Exception(
        "YouTube 没有提供可用字幕。"
        "如需语音识别生成字幕，请安装 openai-whisper 并重新运行。"
    )


# ── 主入口 ───────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="YouTube 下载器 — CLI 版，支持下载后上传到阿里云盘",
    )
    parser.add_argument("--url", required=True, help="YouTube 视频链接")
    parser.add_argument(
        "--type",
        choices=["video", "audio", "subtitle"],
        default="video",
        help="下载类型: video=视频, audio=音频, subtitle=字幕",
    )
    parser.add_argument(
        "--quality",
        default="best",
        help="质量: video=best/1080p/720p/480p, audio=192/128/64 (kbps), subtitle 忽略此项",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="下载目录（默认: 自动创建临时目录）",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="不上传到阿里云盘（仅下载到本地）",
    )
    parser.add_argument(
        "--upload-folder",
        type=str,
        default="",
        help="上传到指定文件夹（名称，如 'YouTube下载'），支持子路径 'Music/YouTube'",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果（供 workflow 解析）",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    url = args.url.strip()
    if not is_youtube_url(url):
        log.error("链接格式不正确，请输入有效的 YouTube 链接")
        sys.exit(1)

    # 创建输出目录
    if args.output_dir:
        output_dir = args.output_dir
        os.makedirs(output_dir, exist_ok=True)
    else:
        output_dir = tempfile.mkdtemp(prefix="yt_download_")

    log.info(f"输出目录: {output_dir}")
    log.info(f"URL: {url}")
    log.info(f"类型: {args.type}")
    log.info(f"质量: {args.quality}")

    # ── 执行下载 ──────────────────────────────────────────────
    result = {}

    try:
        if args.type == "video":
            file_path = download_video(url, args.quality, output_dir)
            if file_path and os.path.isfile(file_path):
                file_size = os.path.getsize(file_path)
                log.info(f"✓ 视频下载成功: {os.path.basename(file_path)} ({human_size(file_size)})")
                result = {
                    "success": True,
                    "type": "video",
                    "file_path": file_path,
                    "file_name": os.path.basename(file_path),
                    "file_size": file_size,
                    "human_size": human_size(file_size),
                }
            else:
                raise Exception("下载失败，未找到输出文件")

        elif args.type == "audio":
            file_path = download_audio(url, args.quality, output_dir)
            if file_path and os.path.isfile(file_path):
                file_size = os.path.getsize(file_path)
                log.info(f"✓ 音频下载成功: {os.path.basename(file_path)} ({human_size(file_size)})")
                result = {
                    "success": True,
                    "type": "audio",
                    "file_path": file_path,
                    "file_name": os.path.basename(file_path),
                    "file_size": file_size,
                    "human_size": human_size(file_size),
                }
            else:
                raise Exception("下载失败，未找到输出文件")

        elif args.type == "subtitle":
            sub_result = download_subtitle(url, output_dir)
            log.info(f"✓ 字幕获取成功: {os.path.basename(sub_result['srt_file'])}")
            sub_result["success"] = True
            sub_result["type"] = "subtitle"
            result = sub_result

        # ── 上传到阿里云盘 ──────────────────────────────────
        if not args.no_upload:

            if args.type in ("video", "audio"):
                upload_targets = []
                fp = result.get("file_path", "")
                if fp and os.path.isfile(fp):
                    upload_targets.append(fp)
            elif args.type == "subtitle":
                # 字幕类型：上传 .srt 和 .txt 两个文件
                upload_targets = []
                srt_file = result.get("srt_file", "")
                txt_file = result.get("txt_file", "")
                if srt_file and os.path.isfile(srt_file):
                    upload_targets.append(srt_file)
                if txt_file and os.path.isfile(txt_file):
                    upload_targets.append(txt_file)

            if upload_targets:
                # 处理 --upload-folder: 根据名称查找文件夹 ID
                resolved_parent_id = None
                if args.upload_folder:
                    log.info(f"📂 查找阿里云盘文件夹: {args.upload_folder}")
                    import requests as req
                    refresh_token = os.environ.get("ALIYUNDRIVE_REFRESH_TOKEN", "")
                    if refresh_token:
                        resp = req.post(
                            "https://api.aliyundrive.com/v2/account/token",
                            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
                            headers={"Content-Type": "application/json"},
                            timeout=30,
                        )
                        if resp.status_code == 200:
                            token_data = resp.json()
                            access_token = token_data.get("access_token", "")
                            drive_id = token_data.get("default_drive_id", "")
                            if access_token and drive_id:
                                folder_id = find_folder_id(args.upload_folder, access_token, drive_id)
                                if folder_id:
                                    resolved_parent_id = folder_id
                                    log.info(f"✓ 找到文件夹 '{args.upload_folder}' (id: {folder_id})")
                                else:
                                    log.warning(f"⚠ 未找到文件夹 '{args.upload_folder}'，上传到默认目录")

                for target in upload_targets:
                    upload_result = upload_to_aliyundrive(target, parent_id=resolved_parent_id)
                    result["upload"] = result.get("upload", [])
                    if isinstance(result["upload"], dict):
                        result["upload"] = [result["upload"]]
                    result["upload"].append(upload_result)
                    if upload_result.get("success"):
                        log.info(f"✓ 上传成功: {os.path.basename(target)}")
                    else:
                        log.warning(f"⚠ 上传失败: {os.path.basename(target)}: {upload_result.get('error', '')}")
            else:
                log.warning("⚠ 未找到可上传的文件")
                result["upload"] = [{"success": False, "error": "文件不存在"}]
        else:
            result["upload"] = {"skipped": True}

        # ── 输出结果 ─────────────────────────────────────────
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            log.info("=" * 40)
            log.info("下载完成！")
            log.info(f"  类型: {args.type}")
            if args.type in ("video", "audio"):
                log.info(f"  文件: {result.get('file_path', '')}")
                log.info(f"  大小: {result.get('human_size', '')}")
            else:
                log.info(f"  字幕: {result.get('srt_file', '')}")
                log.info(f"  文本: {result.get('txt_file', '')}")
            uploads = result.get("upload", [])
            if isinstance(uploads, dict):
                uploads = [uploads]
            success_count = sum(1 for u in uploads if isinstance(u, dict) and u.get("success"))
            skip = any(u.get("skipped") for u in uploads if isinstance(u, dict))
            fail_count = sum(1 for u in uploads if isinstance(u, dict) and u.get("error"))
            if success_count > 0:
                log.info(f"  上传: ✓ {success_count} 个文件已上传到阿里云盘")
            elif skip:
                log.info("  上传: - 已跳过 (--no-upload)")
            elif fail_count > 0:
                errors = [u.get("error","") for u in uploads if isinstance(u, dict) and u.get("error")]
                log.info(f"  上传: ✗ {'; '.join(errors)}")

    except Exception as e:
        log.error(f"任务失败: {e}")
        error_result = {"success": False, "error": str(e), "type": args.type}
        if args.json:
            print(json.dumps(error_result, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
