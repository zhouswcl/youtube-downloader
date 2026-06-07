#!/usr/bin/env python3
"""音频下载专用脚本：直接调用 yt-dlp 命令行下载音频并上传阿里云盘"""
import os, subprocess, sys, json, requests, tempfile
from pathlib import Path

url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("YT_URL", "")
quality = sys.argv[2] if len(sys.argv) > 2 else "192"

if not url:
    print("Usage: download_audio.py <URL> [quality]")
    sys.exit(1)

# 写出 Cookie（修复 GitHub Actions 中 TAB→空格的问题）
cookie_content = os.environ.get("YT_COOKIES", "")
cookie_file = None
if cookie_content:
    fixed_lines = []
    for line in cookie_content.splitlines():
        if not line.strip() or line.startswith("#"):
            fixed_lines.append(line)
            continue
        parts = line.split(None, 6)
        if len(parts) >= 7:
            fixed_lines.append("\t".join(parts))
        else:
            fixed_lines.append(line)
    
    content = "\n".join(fixed_lines)
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    f.write(content)
    f.close()
    cookie_file = f.name
    print(f"Cookie 已写入: {len(fixed_lines)} 行 ({len(content)} 字节)")

# 直接 yt-dlp 命令行下载（绕过 Python API 格式匹配问题）
output_dir = tempfile.mkdtemp(prefix="yt_audio_")
cmd = ["yt-dlp"]
if cookie_file:
    cmd.extend(["--cookies", cookie_file])
else:
    print("无 Cookie，尝试无认证下载")
cmd.extend([
    "-x", "--audio-format", "mp3", "--audio-quality", f"{quality}K",
    "-o", os.path.join(output_dir, "%(title)s.%(ext)s"),
    url
])

print(f"执行: {' '.join(cmd)}")
result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
if result.returncode != 0:
    print(f"yt-dlp 失败 (exit={result.returncode}):")
    print(result.stderr[:500])

    # 如果 cookie 方式失败，尝试无 cookie 方式
    print("尝试无 Cookie 模式...")
    cmd_no_cookie = [
        "yt-dlp",
        "-x", "--audio-format", "mp3", "--audio-quality", f"{quality}K",
        "-o", os.path.join(output_dir, "%(title)s.%(ext)s"),
        url
    ]
    result = subprocess.run(cmd_no_cookie, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        print(f"无 Cookie 也失败: {result.stderr[:500]}")
        sys.exit(1)

print(result.stdout)
print("yt-dlp 完成!")

# 查找输出文件
for ext in [".mp3", ".m4a", ".opus", ".webm", ".mka"]:
    files = list(Path(output_dir).glob(f"*{ext}"))
    if files:
        local_path = str(files[0])
        break
else:
    print("未找到音频输出文件")
    sys.exit(1)

file_name = os.path.basename(local_path)
file_size = os.path.getsize(local_path)
print(f"音频文件: {file_name} ({file_size/1024/1024:.1f}MB)")

# 上传到阿里云盘
refresh_token = os.environ.get("ALIYUNDRIVE_REFRESH_TOKEN", "")
parent_id = os.environ.get("ALIYUNDRIVE_PARENT_ID") or "root"

if not refresh_token:
    print("未设置 ALIYUNDRIVE_REFRESH_TOKEN，跳过上传")
    print(json.dumps({"success": True, "file_name": file_name, "file_size": file_size, "upload_skipped": True}))
    sys.exit(0)

print("上传到阿里云盘...")
resp = requests.post("https://api.aliyundrive.com/v2/account/token",
    json={"grant_type": "refresh_token", "refresh_token": refresh_token}, timeout=30)
token_data = resp.json()
access_token = token_data["access_token"]
drive_id = token_data["default_drive_id"]
headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

resp = requests.post("https://api.aliyundrive.com/v2/file/create", headers=headers,
    json={"drive_id": drive_id, "name": file_name, "parent_file_id": parent_id,
          "type": "file", "size": file_size, "check_name_mode": "auto_rename"}, timeout=30)
file_data = resp.json()

upload_url = file_data.get("upload_url", "")
if upload_url:
    with open(local_path, "rb") as f:
        r = requests.put(upload_url, data=f, timeout=600)
    print(f"上传完成! HTTP {r.status_code}")
else:
    part_list = file_data.get("part_info_list", [])
    with open(local_path, "rb") as f:
        for part in part_list:
            chunk = f.read(part.get("size", 0)) if part.get("size") else f.read()
            requests.put(part["upload_url"], data=chunk, timeout=300)
    requests.post("https://api.aliyundrive.com/v2/file/complete", headers=headers,
        json={"drive_id": drive_id, "file_id": file_data["file_id"],
              "upload_id": file_data.get("upload_id", "")}, timeout=30)
    print("上传完成!")

print(json.dumps({"success": True, "file_name": file_name, "file_size": file_size}))
