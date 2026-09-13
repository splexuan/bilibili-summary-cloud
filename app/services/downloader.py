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
            stderr = (result.stderr or "")[:300]
            raise DownloadError(f"解析失败，请检查链接与 Cookie。{stderr}")

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

    def fetch_subtitle(self, url: str, platform: str = "bilibili") -> str | None:
        """
        提取字幕。先试自动字幕，再试手动字幕。
        无字幕返回 None（由调用方决定是否走 ASR）。
        """
        langs = _SUB_LANGS.get(platform, "zh-Hans,zh-CN,zh,en")

        with tempfile.TemporaryDirectory(prefix="bsum_sub_") as tmp:
            out_tmpl = str(Path(tmp) / "sub.%(ext)s")
            base = [
                "--sub-lang", langs,
                "--sub-format", "srt",
                "--skip-download",
                "--output", out_tmpl,
                "--socket-timeout", "30",
            ]

            # 自动字幕优先
            self._run(["--write-auto-subs"] + base + [url], timeout=90)
            srt_files = list(Path(tmp).glob("sub.*.srt"))

            if not srt_files:
                self._run(["--write-subs"] + base + [url], timeout=90)
                srt_files = list(Path(tmp).glob("sub.*.srt"))

            if srt_files:
                text = parse_srt(srt_files[0])
                if text.strip():
                    logger.info("字幕提取成功，共 %d 字", len(text))
                    return text

        return None

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

        errors = [l for l in tail if "ERROR" in l]
        raise DownloadError(
            f"音频下载失败。{errors[-1][:200] if errors else '未生成文件，请检查 Cookie 是否有效'}"
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
