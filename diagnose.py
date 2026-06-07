#!/usr/bin/env python3
"""诊断工具: 用 yt-dlp --verbose 检查 YouTube 视频的可访问性"""
import argparse
import os
import subprocess
import sys
import tempfile


def fix_cookie(content: str) -> str:
    fixed = []
    for line in content.splitlines():
        if not line.strip() or line.startswith("#"):
            fixed.append(line)
            continue
        parts = line.split(None, 6)
        if len(parts) >= 7:
            fixed.append("\t".join(parts))
        else:
            fixed.append(line)
    return "\n".join(fixed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--cookies-env", default="YT_COOKIES")
    args = parser.parse_args()

    # 写出 Cookie
    cookie_content = os.environ.get(args.cookies_env, "")
    cookie_file = None
    if cookie_content:
        fixed = fix_cookie(cookie_content)
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        f.write(fixed)
        f.close()
        cookie_file = f.name
        print(f"[DIAG] Cookie written: {len(fixed)} bytes, {len(fixed.splitlines())} lines")

    # 测试1: --geo-bypass
    print("\n=== Test 1: --geo-bypass ===")
    cmd = ["yt-dlp", "--verbose", "--no-download", "--geo-bypass"]
    if cookie_file:
        cmd.extend(["--cookies", cookie_file])
    cmd.append(args.url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    print(result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr)
    print(f"Exit code: {result.returncode}")

    # 测试2: 不同的 player client
    for client in ["web", "tv", "ios", "web_creator"]:
        print(f"\n=== Test 2: player_client={client} ===")
        cmd = [
            "yt-dlp", "--verbose", "--no-download", "--geo-bypass",
            "--extractor-args", f"youtube:player_client={client}",
        ]
        if cookie_file:
            cmd.extend(["--cookies", cookie_file])
        cmd.append(args.url)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        # 只打印关键行
        for line in result.stderr.splitlines():
            if any(kw in line.lower() for kw in ["error", "warning", "unavailable", "geo", "block", "restrict", "signature", "solving"]):
                print(f"  {line}")
        print(f"  Exit code: {result.returncode}")

    # 测试3: 跳过 webpage 和 configs （可能绕过更多限制）
    for skip_flag in ["webpage,configs", "webpage,configs,js", "webpage"]:
        print(f"\n=== Test 3: player_skip={skip_flag} ===")
        cmd = [
            "yt-dlp", "--verbose", "--no-download", "--geo-bypass",
            "--extractor-args", f"youtube:player_skip={skip_flag}",
        ]
        if cookie_file:
            cmd.extend(["--cookies", cookie_file])
        cmd.append(args.url)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        for line in result.stderr.splitlines():
            if any(kw in line.lower() for kw in ["error", "warning", "unavailable", "format", "playability", "success"]):
                print(f"  {line}")
        print(f"  Exit code: {result.returncode}")

    # 测试4: 无 Cookie + android 客户端
    print("\n=== Test 4: no cookies + android client ===")
    cmd = [
        "yt-dlp", "--verbose", "--no-download",
        "--extractor-args", "youtube:player_client=android",
        args.url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    for line in result.stderr.splitlines():
        if any(kw in line.lower() for kw in ["error", "warning", "unavailable", "format", "playability"]):
            print(f"  {line}")
    print(f"  Exit code: {result.returncode}")

    # 清理
    if cookie_file:
        os.unlink(cookie_file)


if __name__ == "__main__":
    main()
