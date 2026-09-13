"""业务常量 — 集中定义，避免魔法字符串散落各处"""


class JobStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELED = "canceled"

    ALL = (PENDING, RUNNING, DONE, ERROR, CANCELED)
    TERMINAL = (DONE, ERROR, CANCELED)


class JobStage:
    """任务阶段 — 同时用于进度展示"""
    QUEUED = "queued"
    PARSING = "parsing"          # 解析视频信息
    FETCHING_SUBTITLE = "subtitle"  # 提取字幕
    DOWNLOADING = "downloading"  # 下载音频
    TRANSCODING = "transcoding"  # FFmpeg 转码
    UPLOADING = "uploading"      # 上传 COS
    ASR_SUBMITTING = "asr_submit"
    ASR_POLLING = "asr_polling"
    TRANSCRIBING = "transcribing"  # 本地转写（降级方案）
    SUMMARIZING = "summarizing"
    INDEXING = "indexing"
    DONE = "done"
    ERROR = "error"

    LABELS = {
        QUEUED: "排队中",
        PARSING: "解析视频信息",
        FETCHING_SUBTITLE: "提取字幕",
        DOWNLOADING: "下载音频",
        TRANSCODING: "音频转码",
        UPLOADING: "上传音频",
        ASR_SUBMITTING: "提交识别任务",
        ASR_POLLING: "语音识别中",
        TRANSCRIBING: "本地转写中",
        SUMMARIZING: "AI 总结中",
        INDEXING: "构建索引",
        DONE: "完成",
        ERROR: "失败",
    }

    # 各阶段的进度基准（0-100）
    PROGRESS = {
        QUEUED: 0,
        PARSING: 5,
        FETCHING_SUBTITLE: 15,
        DOWNLOADING: 25,
        TRANSCODING: 40,
        UPLOADING: 48,
        ASR_SUBMITTING: 52,
        ASR_POLLING: 60,
        TRANSCRIBING: 60,
        SUMMARIZING: 80,
        INDEXING: 95,
        DONE: 100,
    }


class JobType:
    SUMMARIZE_VIDEO = "summarize_video"
    SUMMARIZE_ARTICLE = "summarize_article"
    RE_SUMMARIZE = "re_summarize"


class TranscriptSource:
    SUBTITLE = "subtitle"
    ASR = "asr"
    MANUAL = "manual"


class ChatKind:
    KB = "kb"
    VIDEO = "video"


class Platform:
    BILIBILI = "bilibili"
    YOUTUBE = "youtube"


# ASR 状态映射（腾讯云 → 内部）
class ASRStatus:
    PENDING = "pending"
    WAITING = "waiting"    # 腾讯云 Status=0
    DOING = "doing"        # 腾讯云 Status=1
    SUCCESS = "success"    # 腾讯云 Status=2
    FAILED = "failed"      # 腾讯云 Status=3

    # 腾讯云状态码映射
    TENCENT_MAP = {0: WAITING, 1: DOING, 2: SUCCESS, 3: FAILED}


# 任务类型 → 中文名
JOB_TYPE_LABELS = {
    JobType.SUMMARIZE_VIDEO: "视频总结",
    JobType.SUMMARIZE_ARTICLE: "文章总结",
    JobType.RE_SUMMARIZE: "重新总结",
}
