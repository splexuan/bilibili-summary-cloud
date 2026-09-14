"""
视频解析、字幕提取、音频下载。

相比本地版的改造：
1. 去掉 Windows 硬编码路径（F:\\ffmpeg\\...），FFmpeg 路径可配置
2. 不再写 output/ 目录，音频下载到 temp/ 由调用方处理
3. Cookie 支持「用户自带」与「全局兜底」两种来源
4. 代理可配置
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from app.core.config import settings
from app.core.exceptions import DownloadError, SubtitleUnavailable
from app.core.logging import get_logger

logger = get_logger(__name__)

_SAFE_VID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

# B站 / YouTube 字幕语言优先级
_SUB_LANGS = {
    "bilibili": "zh-Hans,zh-CN,zh,zh-TW,zh-Hant,ai-zh",
    "youtube": "zh-Hans,zh-CN,zh,zh-TW,en",
}

# 支持字幕提取的平台。其余平台没有可靠的字幕接口，试探一轮最多要花
# 180 秒（自动 + 手动各 90 秒）的 Worker 时间，不如直接走 ASR —— 本地版同样只对这两个平台试字幕。
SUBTITLE_PLATFORMS = frozenset(_SUB_LANGS)


def _pick_subtitle_file(tmp: Path, langs: str) -> Path | None:
    """
    按语言优先级挑字幕文件。

    --sub-lang 传的是**列表**：所有匹配的语言都会被下载，而
    Path.glob() 的返回顺序由文件系统决定。原实现直接取 [0]，
    于是「同时有中文和英文字幕」的视频（YouTube 上很常见）可能
    拿到英文而不是中文。这里严格按优先级匹配文件名。
    """
    wanted = [lang.strip() for lang in langs.split(",") if lang.strip()]

    for lang in wanted:
        for pattern in (f"sub.{lang}.srt", f"sub.{lang}*.srt"):
            for candidate in sorted(tmp.glob(pattern)):
                if candidate.stat().st_size > 0:
                    return candidate

    # 优先级都没命中（比如 yt-dlp 用了别的语言代码）→ 退回任意一个
    fallback = [f for f in sorted(tmp.glob("sub.*.srt")) if f.stat().st_size > 0]
    return fallback[0] if fallback else None


# ═══════════════════════════════════════════
# 报错翻译
# ═══════════════════════════════════════════

# yt-dlp 的噪声行：与失败原因无关，却总排在 stderr 最前面，
# 截前 300 字展示时会把真正的错误盖掉（曾经就被 Python 版本弃用警告误导过）
_NOISE_MARKERS = (
    "Deprecated Feature",
    "Support for Python version",
    "You are using an unsupported version",
)

# 关键词 → 人话。顺序有意义：先匹配更具体的场景（本地版只做了前四类）
_ERROR_REASONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("premium", "大会员", "需要付费", "vip"), "此视频需要大会员才能下载"),
    (
        ("private video", "has been removed", "removed by", "稿件不可见", "视频已失效"),
        "视频已失效或被删除",
    ),
    (
        ("not available in your country", "geoblock", "georestrict", "region"),
        "该视频在你所在地区不可用",
    ),
    (
        ("login", "sign in", "需要登录"),
        "需要登录才能下载此视频，请在「个人设置」更新 B站 Cookie",
    ),
    (
        ("412", "precondition failed", "risk control", "风控"),
        "B站触发风控（HTTP 412），通常是 Cookie 失效或请求过于频繁，请重新复制 Cookie 后重试",
    ),
    (
        ("unsupported url", "invalid url", "is not a valid url"),
        "无法识别的链接，请确认链接是否复制完整",
    ),
    (("timed out", "timeout", "connection reset"), "网络超时或连接被重置，请检查网络与代理配置"),
    (("http error 404", "not found"), "视频不存在（404），请确认链接是否有效"),
)


def _clean_output(text: str) -> str:
    """去掉噪声行，保留有信息量的输出"""
    kept = []
    for line in (text or "").split("\n"):
        s = line.strip()
        if not s:
            continue
        if any(marker in s for marker in _NOISE_MARKERS):
            continue
        kept.append(s)
    return "\n".join(kept)


# yt-dlp 明确表示「这些语言没有字幕」时的输出特征。
# 注意它走的是 to_screen（stdout）而不是 report_error，所以没有 "ERROR" 字样。
_NO_SUBTITLE_MARKERS = (
    "there are no subtitles for the requested languages",
    "no subtitles for the requested languages",
    "subtitles are not available",
    "no subtitle",
)

# 命中这些原因说明「换 ASR 也没救」：音频下载会撞上同一堵墙
# （Cookie 失效 / 风控 / 视频已失效 / 地区限制），没必要再花时间下音频
_BLOCKING_REASONS = (
    "需要大会员",
    "需要登录",
    "触发风控",
    "已失效",
    "地区不可用",
    "不存在（404）",
    "无法识别的链接",
)


def is_no_subtitle(output: str) -> bool:
    """yt-dlp 是否明确表示没有字幕（属于正常分支，应转 ASR）"""
    low = (output or "").lower()
    return any(m in low for m in _NO_SUBTITLE_MARKERS)


def is_blocking_reason(text: str) -> bool:
    """
    失败原因是否属于「重试和换 ASR 都没用」的类型。

    text 可以是 friendly_download_error() 的返回值，也可以是已翻译过的
    错误消息 —— 判断依据是原因本身，不是原始输出。
    """
    return any(r in (text or "") for r in _BLOCKING_REASONS)


def friendly_download_error(output: str, fallback: str = "") -> str:
    """
    把 yt-dlp 的原始输出翻成用户能看懂的原因。

    找不到特征时退化为「最后一条 ERROR」，仍比一句笼统的
    「解析失败，请检查链接与 Cookie」更有排查价值。
    """
    text = _clean_output(output)
    low = text.lower()

    for keys, reason in _ERROR_REASONS:
        if any(k in low or k in text for k in keys):
            return reason

    errors = [l for l in text.split("\n") if "ERROR" in l]
    if errors:
        return f"解析失败：{errors[-1][:200]}"

    return fallback or "解析失败，请检查链接与 Cookie"


def validate_vid(vid: str) -> str:
    """校验 vid 合法性，防止目录遍历"""
    if not vid or not _SAFE_VID_RE.match(vid):
        raise ValueError(f"非法的视频 ID: {vid!r}")
    return vid


def find_ffmpeg() -> str:
    """查找可用的 ffmpeg（容器内为 PATH 中的 ffmpeg）"""
    candidates = [
        settings.ffmpeg_path,
        str(settings.base_dir / "tools" / "ffmpeg"),
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "ffmpeg",
    ]
    for fp in candidates:
        if not fp:
            continue
        try:
            subprocess.run(
                [fp, "-version"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            return fp
        except Exception:
            continue
    raise DownloadError("未找到 FFmpeg，请检查配置或镜像")


# ═══════════════════════════════════════════
# URL 清洗
# ═══════════════════════════════════════════

def clean_url(url: str) -> str:
    """去掉跟踪参数，保留必要参数"""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    if "bilibili.com" in parsed.netloc:
        return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))

    if "youtube.com" in parsed.netloc or "youtu.be" in parsed.netloc:
        qs = parse_qs(parsed.query)
        v = qs.get("v", [None])[0]
        clean_qs = urlencode({"v": v}) if v else ""
        return urlunparse((parsed.scheme, parsed.netloc, path, "", clean_qs, ""))

    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "bilibili.com" in host or "b23.tv" in host:
        return "bilibili"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    return "other"


# ═══════════════════════════════════════════
# yt-dlp 调用
# ═══════════════════════════════════════════

class YtDlpRunner:
    """
    yt-dlp 调用封装。

    cookie_file: 用户自带 Cookie 的临时文件路径（可为空）
    proxy: 代理地址（可为空）
    """

    def __init__(self, cookie_file: str | Path | None = None, proxy: str = ""):
        self.cookie_file = str(cookie_file) if cookie_file else ""
        self.proxy = proxy or settings.http_proxy or ""

    def _base_args(self) -> list[str]:
        args = [sys.executable, "-m", "yt_dlp"]
        if self.cookie_file and Path(self.cookie_file).exists():
            args += ["--cookies", self.cookie_file]
        if self.proxy:
            args += ["--proxy", self.proxy]
        return args

    def _run(self, extra: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
        cmd = self._base_args() + extra
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise DownloadError("请求超时，请检查网络或代理配置")

    # ─── 视频信息 ───

    def fetch_info(self, url: str) -> dict:
        """获取视频元数据"""
        args = [
            "--print", "%(title)s",
            "--print", "%(uploader)s",
            "--print", "%(duration)s",
            "--print", "%(thumbnail)s",
            "--print", "%(id)s",
            "--print", "%(extractor)s",
            "--socket-timeout", "30",
            url,
        ]
        result = self._run(args, timeout=90)
        lines = [l.strip() for l in (result.stdout or "").strip().split("\n") if l.strip()]

        if not lines:
            raise DownloadError(friendly_download_error(result.stderr))

        raw_vid = lines[4] if len(lines) > 4 else ""
        extractor = (lines[5] if len(lines) > 5 else "").lower()

        # YouTube 加前缀，避免与 B站 BV 号冲突
        if extractor == "youtube":
            vid = f"YT_{raw_vid}"
            platform = "youtube"
        elif "bilibili" in extractor:
            vid = raw_vid
            platform = "bilibili"
        else:
            vid = raw_vid or "unknown"
            platform = extractor or "other"

        duration_raw = lines[2] if len(lines) > 2 else "0"
        try:
            total = int(float(duration_raw))
            m, s = divmod(total, 60)
            h, m = divmod(m, 60)
            duration_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        except (ValueError, TypeError):
            total = 0
            duration_str = duration_raw

        warnings = [
            l for l in (result.stderr or "").split("\n")
            if l.strip() and "WARNING" in l
        ]

        return {
            "vid": vid,
            "url": clean_url(url),
            "title": lines[0] if lines else "未知标题",
            "uploader": lines[1] if len(lines) > 1 else "未知UP主",
            "duration": str(total),
            "duration_str": duration_str,
            "thumbnail": lines[3] if len(lines) > 3 else "",
            "platform": platform,
            "extractor": extractor,
            "warnings": warnings,
        }

    # ─── 字幕 ───

    def fetch_subtitle(self, url: str, platform: str = "bilibili") -> str:
        """
        提取字幕。先试自动字幕，再试手动字幕；全程 --skip-download，
        **不会下载音视频**。

        三种结果分得很清楚（调用方据此决定要不要付出 ASR 的代价）：
        - 有字幕        → 返回文本
        - 确认没有字幕  → 抛 SubtitleUnavailable，属于正常分支，可直接转 ASR
        - 其他失败      → 抛 DownloadError（含友好原因），可能只是网络抖动
        """
        if platform not in _SUB_LANGS:
            raise SubtitleUnavailable(f"{platform} 平台通常不提供字幕")

        langs = _SUB_LANGS[platform]
        outputs: list[str] = []

        with tempfile.TemporaryDirectory(prefix="bsum_sub_") as tmp:
            out_tmpl = str(Path(tmp) / "sub.%(ext)s")
            base = [
                "--sub-lang", langs,
                "--sub-format", "srt",
                "--skip-download",
                "--output", out_tmpl,
                "--socket-timeout", "30",
            ]

            # 自动字幕优先，其次手动字幕
            for mode in ("--write-auto-subs", "--write-subs"):
                result = self._run([mode] + base + [url], timeout=90)
                outputs.append(f"{result.stdout or ''}\n{result.stderr or ''}")

                picked = _pick_subtitle_file(Path(tmp), langs)
                if picked:
                    text = parse_srt(picked)
                    if text.strip():
                        logger.info(
                            "字幕提取成功：%s（%d 字）", picked.name, len(text)
                        )
                        return text

        combined = "\n".join(outputs)
        reason = friendly_download_error(combined, "")

        if is_no_subtitle(combined):
            raise SubtitleUnavailable("该视频没有可用字幕")

        if is_blocking_reason(reason):
            # Cookie 失效 / 风控 / 视频已失效 —— 换成音频下载也一样会失败
            raise DownloadError(reason)

        raise DownloadError(reason or "字幕提取失败，请稍后重试")

    # ─── 音频下载 ───

    def download_audio(
        self,
        url: str,
        vid: str,
        dest_dir: str | Path,
        on_progress=None,
    ) -> Path:
        """
        下载音频（优先 m4a，体积小、ASR 直接支持）。

        on_progress(dict) — {"percent", "speed", "eta"}
        返回下载后的文件路径。
        """
        validate_vid(vid)
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_tmpl = str(dest_dir / f"{vid}_dl.%(ext)s")

        cmd = self._base_args() + [
            "-f", "bestaudio[ext=m4a]/bestaudio/best",
            "-o", out_tmpl,
            "--no-playlist",
            "--no-mtime",
            "--concurrent-fragments", "5",
            "--newline",
            url,
        ]

        progress_re = re.compile(
            r"\[download\]\s+([\d.]+)%\s+of\s+[~\s]*([\d.]+[KMG]?iB).*?"
            r"at\s+([\d.]+\s*[KMG]?i?B/s).*?ETA\s+(\S+)"
        )

        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            env=env,
            bufsize=1,
        )

        tail: list[str] = []
        for line in proc.stdout:
            line = line.rstrip()
            tail.append(line)
            if len(tail) > 50:
                tail.pop(0)

            m = progress_re.search(line)
            if m and on_progress:
                on_progress({
                    "percent": float(m.group(1)),
                    "size": m.group(2),
                    "speed": m.group(3),
                    "eta": m.group(4),
                })

        proc.wait(timeout=1800)

        # 查找下载结果
        for ext in ("m4a", "mp3", "webm", "opus", "aac", "mp4"):
            candidate = dest_dir / f"{vid}_dl.{ext}"
            if candidate.exists() and candidate.stat().st_size > 1024:
                return candidate

        raise DownloadError(
            friendly_download_error(
                "\n".join(tail), "音频下载失败，请检查 Cookie 是否有效"
            )
        )

    def download_thumbnail(self, url: str, timeout: int = 30) -> bytes | None:
        """下载封面图"""
        import requests

        try:
            headers = {"Referer": "https://www.bilibili.com/", "User-Agent": "Mozilla/5.0"}
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200 and len(resp.content) > 512:
                return resp.content
        except Exception as exc:
            logger.warning("封面下载失败: %s", exc)
        return None


# ═══════════════════════════════════════════
# SRT 解析
# ═══════════════════════════════════════════

def parse_srt(srt_path: Path) -> str:
    """解析 SRT，提取纯文本并去重相邻重复行（自动字幕常见重复）"""
    content = srt_path.read_text(encoding="utf-8", errors="replace")
    content = re.sub(r"^\d+\s*$", "", content, flags=re.MULTILINE)
    content = re.sub(
        r"\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}",
        "", content,
    )
    content = re.sub(r"<[^>]+>", "", content)

    lines = [l.strip() for l in content.split("\n") if l.strip()]

    # 相邻去重（自动字幕会滚动重复）
    deduped = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)

    return "\n".join(deduped)


# ═══════════════════════════════════════════
# FFmpeg 音频处理
# ═══════════════════════════════════════════

def audio_duration(path: str | Path) -> float:
    """读取音频时长（秒）。优先 ffprobe，回退 ffmpeg 解析。"""
    ffmpeg = find_ffmpeg()
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe")

    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except Exception:
        pass

    # 回退：从 ffmpeg 输出解析
    try:
        result = subprocess.run(
            [ffmpeg, "-i", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", result.stderr)
        if m:
            h, mi, s, cs = m.groups()
            return int(h) * 3600 + int(mi) * 60 + int(s) + int(cs) / 100
    except Exception:
        pass

    return 0.0


def transcode_for_asr(src: str | Path, dst: str | Path | None = None) -> Path:
    """
    转码为 ASR 友好格式。

    虽然腾讯云支持 m4a，但统一转 16kHz 单声道 m4a 可以减小体积、
    加快上传速度，也避免个别源文件编码异常导致识别失败。
    """
    src = Path(src)
    if dst is None:
        dst = src.with_name(f"{src.stem}_asr.m4a")
    dst = Path(dst)

    ffmpeg = find_ffmpeg()
    cmd = [
        ffmpeg, "-y",
        "-i", str(src),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "aac",
        "-b:a", "64k",
        str(dst),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0 or not dst.exists():
        raise DownloadError(f"音频转码失败：{(result.stderr or '')[-300:]}")

    return dst
