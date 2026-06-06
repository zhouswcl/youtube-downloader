# YouTube Downloader — GitHub Actions 版

基于 yt-dlp 的 YouTube 视频/音频/字幕下载器。通过 GitHub Actions 在海外 Runner 执行下载，完成后自动上传到阿里云盘。

## 功能

| 类型 | 说明 | 格式 |
|------|------|------|
| `video` | 下载视频（MP4） | 支持 best / 1080p / 720p / 480p |
| `audio` | 下载音频（MP3） | 支持 192k / 128k / 64k |
| `subtitle` | 下载字幕（含翻译） | .srt + .txt，自动翻译为中文 |

## 前置准备

### 1. Fork / Clone 此仓库

```bash
git clone https://github.com/<你的用户名>/youtube-downloader.git
cd youtube-downloader
```

### 2. 配置 GitHub Secrets

在 GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret：

| Secret 名 | 说明 | 获取方法 |
|-----------|------|----------|
| `ALIYUNDRIVE_REFRESH_TOKEN` | 阿里云盘 refresh_token | 见下方说明 |
| `ALIYUNDRIVE_PARENT_ID` | 上传目录 ID（可选，默认 root） | 阿里云盘网页版 URL 中获取 |

#### 获取阿里云盘 refresh_token

1. 打开浏览器访问 https://www.aliyundrive.com/ 并登录
2. 按 F12 打开开发者工具 → Application（应用）→ Local Storage
3. 找到 `https://www.aliyundrive.com` 下的 `token` 项
4. 复制 `refresh_token` 字段的值（一串较长的字符串）

#### 获取阿里云盘目录 ID（可选）

1. 在阿里云盘网页版打开目标文件夹
2. 浏览器 URL 中类似 `/file/<文件ID>` 的一串字符

### 3. 触发下载（手动）

在 GitHub 仓库页面 → Actions → YouTube Downloader → Run workflow：

- **url**: YouTube 视频链接（必填）
- **type**: video / audio / subtitle（默认 video）
- **quality**: 画质或音质（默认 best）

### 4. 自动触发（Hermes / 命令行）

需要安装 GitHub CLI (`gh`)：

```bash
# 安装 gh（如未安装）
# macOS: brew install gh
# Linux: 见 https://github.com/cli/cli#installation

# 登录
gh auth login

# 触发下载
gh workflow run download.yml \
  -R <你的用户名>/youtube-downloader \
  -f url="https://youtube.com/watch?v=xxx" \
  -f type="video" \
  -f quality="1080p"
```

#### 查询运行状态

```bash
# 查看最近的运行
gh run list -R <你的用户名>/youtube-downloader --limit 5

# 查看某个运行的日志
gh run view <RUN_ID> -R <你的用户名>/youtube-downloader --log
```

## 注意事项

1. **GitHub Actions 免费额度**: 每月 2000 分钟（约 33 小时），大文件下载会消耗较多
2. **超时限制**: 单次运行最长 6 小时
3. **文件大小**: 注意 GitHub Runner 磁盘空间（约 14GB 可用）
4. **YouTube 地区限制**: 部分视频可能因地区限制无法下载（海外 Runner 可访问绝大多数内容）

## 自定义

修改 `.github/workflows/download.yml` 中的参数或 `download.py` 中的下载逻辑。

## License

MIT
