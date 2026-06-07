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
import subprocess
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


REAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

INVIDIOUS_INSTANCES = [
    "https://inv.nadeko.net",
    "https://invidious.fdn.fr",
    "https://yewtu.be",
    "https://invidious.snapdragon.dev",
    "https://inv.bp.projectsegfau.lt",
]


def _build_common_opts(cookie_file: str | None = None) -> list[str]:
    """返回 yt-dlp 通用防爬参数（不含 url）"""
    opts = []
    if cookie_file:
        opts.extend(["--cookies", cookie_file])
    opts.extend([
        "--user-agent", REAL_USER_AGENT,
        "--referer", "https://www.google.com/",
        "--sleep-interval", "10",
        "--max-sleep-interval", "20",
        "--retries", "10",
        "--fragment-retries", "10",
        "--extractor-retries", "10",
        "--throttled-rate", "100K",
        "--geo-bypass",
        "--no-playlist",
    ])
    return opts


def _download_with_ytdlp(cmd: list[str], url_display: str) -> subprocess.CompletedProcess:
    """执行 yt-dlp 命令，带 429 重试循环"""
    log.info(f"执行: {' '.join(cmd)}")
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            return result
        stderr = result.stderr
        if "HTTP Error 429" in stderr:
            wait = attempt * 30
            log.warning(f"收到 429 限流，等待 {wait} 秒后重试 (第 {attempt}/{max_attempts} 次)...")
            time.sleep(wait)
            continue
        # Invidious fallback: 直接 YouTube 失败时，尝试通过 Invidious 镜像下载
        if "Video unavailable" in stderr or "playability status" in stderr:
            log.warning(f"直接 YouTube 下载失败，将尝试 Invidious 镜像...")
            return result  # 交给上层处理 fallback
        # 其他错误直接报
        log.warning(f"yt-dlp 返回 {result.returncode}")
        log.warning(stderr[:300])
        raise Exception(f"yt-dlp 失败 (exit={result.returncode}): {stderr[:200]}")
    raise Exception(f"yt-dlp 失败（所有重试耗尽）: {result.stderr[:500]}")


def _try_invidious(url: str, quality: str, output_dir: str, cookie_file: str | None) -> str | None:
    """通过 Invidious 镜像下载（绕过地区限制）"""
    import random

    # 提取 video_id
    import re
    m = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", url)
    if not m:
        return None
    video_id = m.group(1)

    # 随机尝试镜像列表
    instances = INVIDIOUS_INSTANCES.copy()
    random.shuffle(instances)

    for instance in instances:
        invidious_url = f"{instance}/watch?v={video_id}"
        log.info(f"尝试 Invidious 镜像: {instance}")

        # 先测试镜像是否可用（快速 info 请求）
        test_cmd = ["yt-dlp", "--no-download", "--quiet"]
        if cookie_file:
            test_cmd.extend(["--cookies", cookie_file])
        test_cmd.append(invidious_url)

        test_result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=30)
        if test_result.returncode != 0:
            log.warning(f"  镜像不可用: {test_result.stderr[:100]}")
            continue

        # 镜像可用，执行下载
        dl_cmd = ["yt-dlp"]
        if cookie_file:
            dl_cmd.extend(["--cookies", cookie_file])
        dl_cmd.extend([
            "-x", "--audio-format", "mp3",
            "--audio-quality", quality,
            "-o", os.path.join(output_dir, "%(title)s.%(ext)s"),
            "--no-playlist",
            "--user-agent", REAL_USER_AGENT,
            "--referer", instance + "/",
            "--retries", "5",
            "--fragment-retries", "5",
            invidious_url,
        ])

        log.info(f"通过 Invidious 下载: {instance}")
        result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            # 查找生成的音频文件
            for ext in [".mp3", ".m4a", ".opus", ".webm", ".mka"]:
                candidates = [f for f in os.listdir(output_dir) if f.endswith(ext)]
                if candidates:
                    candidates.sort(key=lambda x: os.path.getmtime(os.path.join(output_dir, x)), reverse=True)
                    final_path = os.path.join(output_dir, candidates[0])
                    log.info(f"Invidious 下载成功: {os.path.basename(final_path)}")
                    return final_path
            log.warning("Invidious 下载完成但未找到文件")
        else:
            log.warning(f"Invidious {instance} 下载失败")

    return None


def download_audio(url: str, quality: str, output_dir: str) -> str:
    """下载音频并转 MP3，用 yt-dlp 命令行，带防爬 + Invidious Fallback"""
    audio_quality = quality
    if audio_quality in ("best", "0", ""):
        audio_quality = "0"

    cookie_file = _get_cookie_file()
    common_opts = _build_common_opts(cookie_file)

    # 第一步：直接 YouTube 下载
    cmd = ["yt-dlp"] + common_opts + [
        "-x", "--audio-format", "mp3",
        "--audio-quality", audio_quality,
        "-o", os.path.join(output_dir, "%(title)s.%(ext)s"),
        url,
    ]

    result = _download_with_ytdlp(cmd, url)
    if result.returncode == 0:
        # 查找文件
        for ext in [".mp3", ".m4a", ".opus", ".webm", ".mka"]:
            candidates = [f for f in os.listdir(output_dir) if f.endswith(ext)]
            if candidates:
                candidates.sort(key=lambda x: os.path.getmtime(os.path.join(output_dir, x)), reverse=True)
                final_path = os.path.join(output_dir, candidates[0])
                log.info(f"音频下载完成: {os.path.basename(final_path)}")
                return final_path

    # 第二步：YouTube 直接失败 → 尝试 Invidious 镜像
    log.info("YouTube 直连失败，尝试通过 Invidious 镜像下载...")
    invidious_path = _try_invidious(url, audio_quality, output_dir, cookie_file)
    if invidious_path:
        return invidious_path

    # 全部失败
    stderr = result.stderr if result.returncode != 0 else "文件未找到"
    raise Exception(f"yt-dlp 失败 (exit={result.returncode}): {stderr[:500]}")


def download_subtitle(url: str, output_dir: str) -> dict:
    """下载字幕（如有），用 yt-dlp 命令行"""
    log.info("正在获取字幕...")
    cookie_file = _get_cookie_file()

    # 用 yt-dlp CLI 下载字幕（规避 Python API 的格式转换问题）
    cmd = ["yt-dlp"]
    if cookie_file:
        cmd.extend(["--cookies", cookie_file])
    cmd.extend([
        "--skip-download",
        "--write-subs",
        "--sub-langs", "zh-Hans,zh-Hant,en,zh",
        "--convert-subs", "srt",
        "-o", os.path.join(output_dir, "%(title)s.%(ext)s"),
        "--no-playlist",
        "--user-agent", REAL_USER_AGENT,
        "--referer", "https://www.google.com/",
        "--sleep-interval", "3",
        "--geo-bypass",
        # 用 ios 客户端（不需要 JS 运行时）
        "--extractor-args", "youtube:player_client=ios",
        url,
    ])

    log.info(f"执行字幕提取: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    # 如果失败，先列一下可用字幕
    if result.returncode != 0:
        log.warning(f"yt-dlp 返回 {result.returncode}: {result.stderr[:200]}")
        # 用 --list-subs 看看这个视频有哪些字幕
        list_cmd = ["yt-dlp", "--list-subs", "--no-playlist", "--user-agent", REAL_USER_AGENT]
        if cookie_file:
            list_cmd.extend(["--cookies", cookie_file])
        list_cmd.append(url)
        list_result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=30)
        log.info(f"可用字幕: {list_result.stdout[:300]}")
        if "doesn't have any subtitles" in list_result.stdout.lower():
            raise Exception("YouTube 没有提供可用字幕。如需语音识别生成字幕，请安装 openai-whisper 并重新运行。")

    # 查找下载的字幕文件
    srt_files = [f for f in os.listdir(output_dir) if f.endswith(".srt")]
    if srt_files:
        # 选最佳语言
        chosen_srt = None
        chosen_lang = "unknown"
        for target in ["zh-Hans", "zh-Hant", "zh", "en"]:
            matches = [f for f in srt_files if f".{target}." in f]
            if matches:
                chosen_srt = os.path.join(output_dir, matches[0])
                chosen_lang = target
                break
        if not chosen_srt:
            chosen_srt = os.path.join(output_dir, srt_files[0])

        # 解析字幕文件获取标题
        title = None
        for f in srt_files:
            # 从文件名提取标题（去掉语言后缀和 .srt）
            name = f.rsplit(".", 2)[0] if len(f.rsplit(".", 2)) >= 3 else f.rsplit(".", 1)[0]
            if name:
                title = name
                break

        log.info(f"找到 YouTube 字幕 (语言: {chosen_lang})")

        # 保存 .txt 版本（纯文本）
        segments = parse_srt(chosen_srt)
        if segments:
            txt_path = chosen_srt.replace(".srt", ".txt")
            write_txt(segments, txt_path)
        else:
            txt_path = ""

        # 清理多余的字幕文件，只保留最佳语言
        for f in srt_files:
            fp = os.path.join(output_dir, f)
            if fp != chosen_srt:
                try:
                    os.remove(fp)
                except OSError:
                    pass

        return {
            "success": True,
            "title": title or "未知视频",
            "srt_file": chosen_srt,
            "txt_file": txt_path if segments else "",
            "source_lang": chosen_lang,
            "translated": False,
            "from_speech": False,
        }

    # 无字幕
    if result.returncode != 0:
        log.warning(f"yt-dlp 返回 {result.returncode}: {result.stderr[:200]}")
    raise Exception("YouTube 没有提供可用字幕。如需语音识别生成字幕，请安装 openai-whisper 并重新运行。")


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
