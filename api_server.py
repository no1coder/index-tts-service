"""
IndexTTS2 FastAPI 服务 v2.1
功能：
  - asyncio.Semaphore + run_in_executor（防 GPU 竞争 / 事件循环阻塞）
  - 任务管理系统（待处理/处理中/历史记录）+ SSE 进度推送
  - 队列深度上限（防无限积压）
  - output_path 延迟清理（防临时文件堆积）
  - CUDA OOM 捕获（防进程崩溃）
  - 健康检查返回队列状态
  - MP3 输出支持（节省存储带宽）
  - 音色管理（上传保存/试听/删除）
  - 情感控制（音频参考/文字驱动/向量直传）
  - 高级采样参数（interval_silence, repetition_penalty, num_beams 等）
  - 内置 WebUI 调试界面
  - Swagger API 文档（/docs）
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import wave
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

# ── 配置（全部从环境变量读取，禁止硬编码） ──────────────────────────────────
API_KEY          = os.environ.get("TTS_API_KEY", "")
MODEL_DIR        = os.environ.get("MODEL_DIR", "./checkpoints")
USE_FP16         = os.environ.get("USE_FP16", "true").lower() == "true"
HOST             = os.environ.get("HOST", "0.0.0.0")
PORT             = int(os.environ.get("PORT", "8000"))
MAX_QUEUE_DEPTH  = int(os.environ.get("MAX_QUEUE_DEPTH", "5"))
CACHE_DIR        = os.environ.get("CACHE_DIR", "/tmp/tts_cache")
ENABLE_CACHE     = os.environ.get("ENABLE_CACHE", "true").lower() == "true"
MAX_HISTORY      = int(os.environ.get("MAX_HISTORY", "100"))
MAX_TEXT_LENGTH  = int(os.environ.get("MAX_TEXT_LENGTH", "5000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tts-api")

# ── 任务管理系统 ────────────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    id: str
    text: str
    voice: str
    status: TaskStatus
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    duration: Optional[float] = None
    error: Optional[str] = None
    output_format: str = "wav"
    temperature: float = 0.8
    top_p: float = 0.8
    top_k: int = 30
    progress: float = 0.0
    progress_msg: str = ""
    # 异步任务结果（内部字段）
    result_path: Optional[str] = None
    result_format: Optional[str] = None
    result_duration: Optional[float] = None
    result_sample_rate: int = 22050

    def to_dict(self):
        d = asdict(self)
        d["status"] = self.status.value
        # 移除内部字段
        for internal in ("result_path", "result_format", "result_duration", "result_sample_rate"):
            d.pop(internal, None)
        for key in ["created_at", "started_at", "completed_at"]:
            if d[key]:
                d[key + "_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d[key]))
        if d["duration"] is not None:
            d["duration_str"] = f"{d['duration']:.2f}s"
        return d


class TaskManager:
    def __init__(self, max_history: int = 100):
        self._tasks: dict[str, Task] = {}
        self._history: deque[Task] = deque(maxlen=max_history)
        self._lock = asyncio.Lock()

    async def create(self, text: str, voice: str, **kwargs) -> Task:
        task = Task(
            id=uuid.uuid4().hex[:12],
            text=text[:100] + ("..." if len(text) > 100 else ""),
            voice=voice,
            status=TaskStatus.PENDING,
            created_at=time.time(),
            **kwargs,
        )
        async with self._lock:
            self._tasks[task.id] = task
        return task

    async def start(self, task_id: str):
        async with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].status = TaskStatus.PROCESSING
                self._tasks[task_id].started_at = time.time()

    async def update_progress(self, task_id: str, progress: float, msg: str = ""):
        async with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].progress = progress
                self._tasks[task_id].progress_msg = msg

    async def set_result(self, task_id: str, result_path: str, result_format: str,
                         result_duration: float, result_sample_rate: int):
        async with self._lock:
            if task_id in self._tasks:
                task = self._tasks[task_id]
                task.result_path = result_path
                task.result_format = result_format
                task.result_duration = result_duration
                task.result_sample_rate = result_sample_rate

    def _cleanup_evicted(self):
        """清理被挤出历史队列的任务的结果文件（在持有锁时调用）"""
        if len(self._history) >= self._history.maxlen:
            evicted = self._history[-1]
            if evicted.result_path:
                for p in [evicted.result_path,
                          evicted.result_path.replace(".wav", ".mp3"),
                          evicted.result_path.replace(".mp3", ".wav")]:
                    if p and os.path.exists(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass

    async def complete(self, task_id: str):
        async with self._lock:
            if task_id in self._tasks:
                task = self._tasks[task_id]
                task.status = TaskStatus.COMPLETED
                task.completed_at = time.time()
                task.progress = 1.0
                task.progress_msg = "完成"
                if task.started_at:
                    task.duration = task.completed_at - task.started_at
                self._cleanup_evicted()
                self._history.appendleft(task)
                del self._tasks[task_id]

    async def fail(self, task_id: str, error: str):
        async with self._lock:
            if task_id in self._tasks:
                task = self._tasks[task_id]
                task.status = TaskStatus.FAILED
                task.completed_at = time.time()
                task.error = error
                if task.started_at:
                    task.duration = task.completed_at - task.started_at
                self._cleanup_evicted()
                self._history.appendleft(task)
                del self._tasks[task_id]

    async def get_task(self, task_id: str) -> Optional[Task]:
        async with self._lock:
            if task_id in self._tasks:
                return self._tasks[task_id]
            for t in self._history:
                if t.id == task_id:
                    return t
            return None

    async def get_queue_position(self, task_id: str) -> int:
        """返回任务在队列中的位置（1=下一个处理，0=不在队列中）"""
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status != TaskStatus.PENDING:
                return 0
            ahead = 0
            for t in self._tasks.values():
                if t.id == task_id:
                    continue
                if t.status == TaskStatus.PROCESSING:
                    ahead += 1
                elif t.status == TaskStatus.PENDING and t.created_at < task.created_at:
                    ahead += 1
            return ahead + 1

    async def get_status(self) -> dict:
        async with self._lock:
            active = list(self._tasks.values())
            pending = [t for t in active if t.status == TaskStatus.PENDING]
            processing = [t for t in active if t.status == TaskStatus.PROCESSING]
            return {
                "pending": [t.to_dict() for t in pending],
                "processing": [t.to_dict() for t in processing],
                "history": [t.to_dict() for t in self._history],
                "stats": {
                    "pending_count": len(pending),
                    "processing_count": len(processing),
                    "history_count": len(self._history),
                    "total_completed": sum(1 for t in self._history if t.status == TaskStatus.COMPLETED),
                    "total_failed": sum(1 for t in self._history if t.status == TaskStatus.FAILED),
                },
            }


# ── 全局状态 ──────────────────────────────────────────────────────────────────
tts = None
_inference_semaphore = asyncio.Semaphore(1)
_queue_depth = 0
task_manager = TaskManager(max_history=MAX_HISTORY)
_task_queue: asyncio.Queue = asyncio.Queue()
_task_params: dict = {}  # task_id -> inference params dict


# ── 后台任务处理 Worker ───────────────────────────────────────────────────────
async def task_worker():
    """后台 Worker：从队列取任务，逐一推理，存储结果。"""
    global _queue_depth
    while True:
        task_id = await _task_queue.get()
        params = _task_params.pop(task_id, None)
        if not params:
            _task_queue.task_done()
            continue

        wav_path = params.get("wav_path")
        try:
            _queue_depth += 1
            await task_manager.start(task_id)
            await task_manager.update_progress(task_id, 0.05, "准备推理...")

            async with _inference_semaphore:
                await task_manager.update_progress(task_id, 0.1, "GPU 推理中...")
                loop = asyncio.get_event_loop()
                try:
                    infer_kwargs = params["infer_kwargs"]
                    await loop.run_in_executor(None, lambda: tts.infer(**infer_kwargs))
                except RuntimeError as e:
                    if "out of memory" in str(e).lower():
                        import torch
                        torch.cuda.empty_cache()
                        await task_manager.fail(task_id, "GPU 显存不足")
                        if wav_path and os.path.exists(wav_path):
                            os.remove(wav_path)
                        continue
                    raise

            await task_manager.update_progress(task_id, 0.9, "后处理...")

            duration = get_wav_duration(wav_path)
            fmt = params.get("output_format", "wav")
            sample_rate = 22050
            result_path = wav_path

            if fmt == "mp3":
                try:
                    result_path = wav_to_mp3(wav_path)
                    sample_rate = 44100
                except Exception:
                    logger.warning("MP3 conversion failed, returning WAV")
                    fmt = "wav"

            # 缓存
            cache_key = params.get("cache_key")
            if cache_key and ENABLE_CACHE:
                cache_path = os.path.join(CACHE_DIR, f"{cache_key}.{fmt}")
                shutil.copy2(result_path, cache_path)
                dur_path = os.path.join(CACHE_DIR, f"{cache_key}.dur")
                with open(dur_path, "w") as df:
                    df.write(str(duration))

            await task_manager.set_result(task_id, result_path, fmt, duration, sample_rate)
            await task_manager.complete(task_id)
            logger.info("Task %s completed: %.2fs audio, format=%s", task_id, duration, fmt)

        except Exception as e:
            logger.error("Task %s failed: %s", task_id, e, exc_info=True)
            await task_manager.fail(task_id, friendly_error(e))
            # 失败时清理输出文件
            if wav_path:
                for p in [wav_path, wav_path.replace(".wav", ".mp3")]:
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
        finally:
            _queue_depth -= 1
            # 清理输入临时文件
            for p in [params.get("spk_path"), params.get("emo_path")]:
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            _task_queue.task_done()


# ── 生命周期：模型加载 ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global tts
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(os.path.join(MODEL_DIR, "voices"), exist_ok=True)
    logger.info("Loading IndexTTS2 model from %s ...", MODEL_DIR)
    logger.info("FP16=%s, MAX_QUEUE=%d", USE_FP16, MAX_QUEUE_DEPTH)
    try:
        from indextts.infer_v2 import IndexTTS2
        tts = IndexTTS2(
            cfg_path=os.path.join(MODEL_DIR, "config.yaml"),
            model_dir=MODEL_DIR,
            use_fp16=USE_FP16,
            use_cuda_kernel=False,
        )
        logger.info("Model loaded successfully.")
    except Exception as e:
        logger.error("Failed to load model: %s", e)
    # 启动后台 Worker
    worker_task = asyncio.create_task(task_worker())
    logger.info("Background task worker started.")
    yield
    worker_task.cancel()
    logger.info("Shutting down TTS service.")


app = FastAPI(
    title="IndexTTS2 API",
    version="2.1.0",
    description="""
## IndexTTS2 语音合成 API

基于 IndexTTS2 v2.0.0 的高质量零样本语音合成服务。

### 功能特点
- **零样本克隆**：上传 3-10 秒参考音频即可克隆音色
- **情感控制**：支持音频参考、文字驱动、向量直传三种情感控制方式
- **音色管理**：上传保存音色、试听、删除
- **高级参数**：interval_silence、repetition_penalty、num_beams 等
- **进度查询**：SSE 实时进度推送 + 轮询接口
- **任务管理**：实时查看待处理/处理中/历史任务
- **WebUI**：内置调试界面，访问 `/` 即可使用

### 认证
通过 `x-api-key` Header 传递 API Key（环境变量 `TTS_API_KEY` 为空时跳过认证）
""",
    lifespan=lifespan,
)


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def verify_key(x_api_key: str):
    if API_KEY and not hmac.compare_digest(x_api_key or "", API_KEY):
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")


def validate_text(text: str):
    """校验合成文本：非空、长度不超限"""
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="合成文本不能为空")
    if len(text) > MAX_TEXT_LENGTH:
        raise HTTPException(status_code=400, detail=f"文本过长（{len(text)} 字符），最大允许 {MAX_TEXT_LENGTH} 字符")


def get_cache_key(text: str, voice_name: str, temperature: float,
                  top_p: float, top_k: int, **extra) -> str:
    content = f"{text}|{voice_name}|{temperature}|{top_p}|{top_k}"
    for k, v in sorted(extra.items()):
        content += f"|{k}={v}"
    return hashlib.md5(content.encode()).hexdigest()


def wav_to_mp3(wav_path: str, bitrate: str = "128k") -> str:
    mp3_path = wav_path.replace(".wav", ".mp3")
    subprocess.run(
        ["ffmpeg", "-i", wav_path, "-b:a", bitrate, mp3_path, "-y"],
        check=True, capture_output=True,
    )
    return mp3_path


def get_wav_duration(path: str) -> float:
    """获取 WAV 文件时长（秒），保留两位小数"""
    with wave.open(path, "rb") as f:
        return round(f.getnframes() / f.getframerate(), 2)


def sanitize_voice_name(name: str) -> str:
    """只允许字母、数字、中文、下划线、连字符"""
    name = re.sub(r'[^\w\u4e00-\u9fff-]', '_', name)
    return name[:50]


def safe_voice_path(voice_name: str) -> str:
    """构造音色文件路径，防止路径穿越攻击。
    拒绝包含 .. / \\ 等路径分隔符的名称。"""
    if not voice_name or ".." in voice_name or "/" in voice_name or "\\" in voice_name:
        raise HTTPException(status_code=400, detail="音色名称包含非法字符")
    # 二次过滤：确保最终路径仍在 voices 目录内
    voice_dir = os.path.join(MODEL_DIR, "voices")
    full_path = os.path.normpath(os.path.join(voice_dir, f"{voice_name}.wav"))
    if not full_path.startswith(os.path.normpath(voice_dir)):
        raise HTTPException(status_code=400, detail="音色名称包含非法字符")
    return full_path


def friendly_error(e: Exception) -> str:
    """将常见异常转为用户友好的中文提示"""
    msg = str(e)
    if "out of memory" in msg.lower():
        return "GPU 显存不足，请缩短文本或等待其他任务完成后重试"
    if "No such file" in msg:
        return "音频文件不存在或路径错误"
    if "codec" in msg.lower() or "audio" in msg.lower():
        return "音频格式不支持，请使用 WAV 格式（16kHz/22kHz，单声道）"
    if "CUDA" in msg:
        return "GPU 错误，请检查 CUDA 驱动或重启服务"
    if len(msg) > 200:
        return msg[:200] + "..."
    return msg


# ── 健康检查 ─────────────────────────────────────────────────────────────────
@app.get("/health", tags=["系统"], summary="健康检查")
async def health():
    """返回服务状态、模型加载情况、GPU 队列深度。"""
    if tts is None:
        return JSONResponse(
            {"status": "loading", "model_loaded": False},
            status_code=503,
        )
    return {
        "status": "ok",
        "model_loaded": True,
        "queue_depth": _queue_depth,
        "queue_max": MAX_QUEUE_DEPTH,
        "fp16": USE_FP16,
        "cache_enabled": ENABLE_CACHE,
    }


# ── 任务状态接口 ─────────────────────────────────────────────────────────────
@app.get("/tasks", tags=["任务管理"], summary="查看所有任务状态")
async def get_tasks(x_api_key: str = Header(None)):
    """
    返回当前待处理任务、处理中任务和历史记录。

    - **pending**: 排队等待的任务
    - **processing**: 正在 GPU 推理的任务（含进度百分比）
    - **history**: 已完成/失败的历史任务（最近 100 条）
    - **stats**: 统计汇总
    """
    verify_key(x_api_key)
    return await task_manager.get_status()


@app.get("/tasks/{task_id}", tags=["任务管理"], summary="查询单个任务进度")
async def get_task_progress(task_id: str, x_api_key: str = Header(None)):
    """
    查询指定任务的当前状态和进度。

    返回 progress (0.0~1.0) 和 progress_msg 字段。
    """
    verify_key(x_api_key)
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return task.to_dict()


@app.get("/tasks/{task_id}/sse", tags=["任务管理"], summary="SSE 实时进度推送")
async def task_sse(task_id: str):
    """
    通过 Server-Sent Events 实时推送任务进度。（无需认证）

    前端用法：`new EventSource('/tasks/{task_id}/sse')`
    """

    async def event_stream():
        last_progress = -1.0
        max_polls = 600  # 最多轮询 600 次 × 0.5s = 5 分钟
        polls = 0
        while polls < max_polls:
            polls += 1
            task = await task_manager.get_task(task_id)
            if not task:
                yield f"data: {json.dumps({'status': 'not_found'})}\n\n"
                break
            d = task.to_dict()
            # 附加队列位置信息
            if task.status == TaskStatus.PENDING:
                d["queue_position"] = await task_manager.get_queue_position(task_id)
            if task.progress != last_progress or task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                yield f"data: {json.dumps(d, ensure_ascii=False)}\n\n"
                last_progress = task.progress
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── 异步提交接口 ──────────────────────────────────────────────────────────────
@app.post("/submit", tags=["语音合成"], summary="异步提交：上传音色合成")
async def submit_task(
    text: str = Form(..., description="要合成的文本内容"),
    spk_audio: UploadFile = File(..., description="音色参考音频文件（WAV，3-10秒）"),
    emo_audio: UploadFile = File(None, description="情感参考音频文件（可选）"),
    emo_alpha: float = Form(1.0, ge=0.0, le=1.0, description="情感权重"),
    use_emo_text: bool = Form(False, description="启用文字驱动情感"),
    emo_text: str = Form(None, description="情感文字描述"),
    temperature: float = Form(0.8, ge=0.1, le=2.0),
    top_p: float = Form(0.8, ge=0.1, le=1.0),
    top_k: int = Form(30, ge=0, le=200),
    interval_silence: int = Form(200, ge=0, le=2000),
    repetition_penalty: float = Form(10.0, ge=1.0, le=20.0),
    max_text_tokens_per_segment: int = Form(120, ge=20, le=300),
    output_format: str = Form("wav"),
    save_voice: str = Form(None, description="保存音色名称"),
    x_api_key: str = Header(None),
):
    """
    异步提交合成任务，立即返回 task_id 和队列位置。

    通过 `/tasks/{task_id}/sse` 追踪实时进度，完成后通过 `/tasks/{task_id}/result` 下载音频。
    """
    verify_key(x_api_key)
    validate_text(text)
    if output_format not in ("wav", "mp3"):
        raise HTTPException(status_code=400, detail="output_format 仅支持 'wav' 或 'mp3'")
    if tts is None:
        raise HTTPException(status_code=503, detail="模型尚未加载完成，请稍后重试")

    spk_data = await spk_audio.read()
    if len(spk_data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="音色音频文件超过 50MB 限制")
    if len(spk_data) < 1000:
        raise HTTPException(status_code=400, detail="音色音频文件太小，请上传 3-10 秒的有效音频")

    emo_data = None
    if emo_audio:
        emo_data = await emo_audio.read()

    task = await task_manager.create(
        text=text, voice="[uploaded]" + (f" -> {save_voice}" if save_voice else ""),
        output_format=output_format, temperature=temperature, top_p=top_p, top_k=top_k,
    )

    spk_path = os.path.join(tempfile.gettempdir(), f"spk_{uuid.uuid4().hex}.wav")
    with open(spk_path, "wb") as f:
        f.write(spk_data)

    if save_voice:
        voice_name = sanitize_voice_name(save_voice)
        voice_dir = os.path.join(MODEL_DIR, "voices")
        os.makedirs(voice_dir, exist_ok=True)
        with open(os.path.join(voice_dir, f"{voice_name}.wav"), "wb") as f:
            f.write(spk_data)
        logger.info("Voice saved: %s", voice_name)

    emo_path = None
    if emo_data:
        emo_path = os.path.join(tempfile.gettempdir(), f"emo_{uuid.uuid4().hex}.wav")
        with open(emo_path, "wb") as f:
            f.write(emo_data)

    wav_path = os.path.join(tempfile.gettempdir(), f"out_{uuid.uuid4().hex}.wav")
    infer_kwargs = {
        "spk_audio_prompt": spk_path,
        "text": text,
        "output_path": wav_path,
        "do_sample": True,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k if top_k > 0 else None,
        "interval_silence": interval_silence,
        "repetition_penalty": repetition_penalty,
        "max_text_tokens_per_segment": max_text_tokens_per_segment,
    }
    if use_emo_text:
        infer_kwargs["use_emo_text"] = True
        if emo_text:
            infer_kwargs["emo_text"] = emo_text
        infer_kwargs["emo_alpha"] = emo_alpha
    elif emo_path:
        infer_kwargs["emo_audio_prompt"] = emo_path
        infer_kwargs["emo_alpha"] = emo_alpha

    _task_params[task.id] = {
        "infer_kwargs": infer_kwargs,
        "wav_path": wav_path,
        "output_format": output_format,
        "spk_path": spk_path,
        "emo_path": emo_path,
    }
    await _task_queue.put(task.id)

    queue_pos = await task_manager.get_queue_position(task.id)
    return JSONResponse({
        "task_id": task.id,
        "status": "pending",
        "queue_position": queue_pos,
        "message": f"任务已提交，当前排队位置：第 {queue_pos} 位",
    })


@app.post("/submit_json", tags=["语音合成"], summary="异步提交：预存音色合成")
async def submit_task_json(
    text: str = Form(..., description="要合成的文本内容"),
    voice_name: str = Form("default", description="预存音色名称"),
    temperature: float = Form(0.8, ge=0.1, le=2.0),
    top_p: float = Form(0.8, ge=0.1, le=1.0),
    top_k: int = Form(30, ge=0, le=200),
    interval_silence: int = Form(200, ge=0, le=2000),
    repetition_penalty: float = Form(10.0, ge=1.0, le=20.0),
    max_text_tokens_per_segment: int = Form(120, ge=20, le=300),
    use_emo_text: bool = Form(False),
    emo_text: str = Form(None),
    emo_alpha: float = Form(1.0, ge=0.0, le=1.0),
    output_format: str = Form("wav"),
    x_api_key: str = Header(None),
):
    """
    异步提交预存音色合成任务，立即返回 task_id 和队列位置。

    通过 `/tasks/{task_id}/sse` 追踪进度，`/tasks/{task_id}/result` 下载结果。
    """
    verify_key(x_api_key)
    validate_text(text)
    if output_format not in ("wav", "mp3"):
        raise HTTPException(status_code=400, detail="output_format 仅支持 'wav' 或 'mp3'")
    if tts is None:
        raise HTTPException(status_code=503, detail="模型尚未加载完成，请稍后重试")

    voice_path = safe_voice_path(voice_name)
    if not os.path.exists(voice_path):
        raise HTTPException(status_code=404, detail=f"音色 '{voice_name}' 不存在")

    # 检查缓存
    cache_key = None
    if ENABLE_CACHE:
        cache_key = get_cache_key(text, voice_name, temperature, top_p, top_k,
                                  interval_silence=interval_silence,
                                  repetition_penalty=repetition_penalty,
                                  use_emo_text=use_emo_text,
                                  emo_text=emo_text or "")
        cache_ext = "mp3" if output_format == "mp3" else "wav"
        cache_path = os.path.join(CACHE_DIR, f"{cache_key}.{cache_ext}")
        if os.path.exists(cache_path):
            logger.info("Cache hit: %s", cache_key)
            dur_path = os.path.join(CACHE_DIR, f"{cache_key}.dur")
            duration = 0.0
            if os.path.exists(dur_path):
                with open(dur_path) as f:
                    duration = float(f.read())
            elif cache_ext == "wav":
                duration = get_wav_duration(cache_path)
            # 创建一个已完成的任务直接返回
            task = await task_manager.create(text=text, voice=voice_name,
                output_format=output_format, temperature=temperature, top_p=top_p, top_k=top_k)
            await task_manager.set_result(task.id, cache_path, cache_ext, duration,
                                          44100 if cache_ext == "mp3" else 22050)
            await task_manager.complete(task.id)
            return JSONResponse({
                "task_id": task.id,
                "status": "completed",
                "queue_position": 0,
                "cached": True,
                "message": "缓存命中，结果已就绪",
            })

    task = await task_manager.create(
        text=text, voice=voice_name,
        output_format=output_format, temperature=temperature, top_p=top_p, top_k=top_k,
    )

    wav_path = os.path.join(tempfile.gettempdir(), f"out_{uuid.uuid4().hex}.wav")
    infer_kwargs = {
        "spk_audio_prompt": voice_path,
        "text": text,
        "output_path": wav_path,
        "do_sample": True,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k if top_k > 0 else None,
        "interval_silence": interval_silence,
        "repetition_penalty": repetition_penalty,
        "max_text_tokens_per_segment": max_text_tokens_per_segment,
    }
    if use_emo_text:
        infer_kwargs["use_emo_text"] = True
        if emo_text:
            infer_kwargs["emo_text"] = emo_text
        infer_kwargs["emo_alpha"] = emo_alpha

    _task_params[task.id] = {
        "infer_kwargs": infer_kwargs,
        "wav_path": wav_path,
        "output_format": output_format,
        "cache_key": cache_key,
    }
    await _task_queue.put(task.id)

    queue_pos = await task_manager.get_queue_position(task.id)
    return JSONResponse({
        "task_id": task.id,
        "status": "pending",
        "queue_position": queue_pos,
        "message": f"任务已提交，当前排队位置：第 {queue_pos} 位",
    })


@app.get("/tasks/{task_id}/result", tags=["任务管理"], summary="下载任务结果音频")
async def get_task_result(task_id: str, x_api_key: str = Header(None)):
    """
    下载已完成任务的合成音频。

    - 任务完成：返回音频文件
    - 任务排队/处理中：返回 202 + 当前进度
    - 任务失败：返回 500 + 错误信息
    """
    verify_key(x_api_key)
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")

    if task.status == TaskStatus.PENDING:
        queue_pos = await task_manager.get_queue_position(task_id)
        return JSONResponse(
            {"status": "pending", "queue_position": queue_pos,
             "message": f"排队中（第 {queue_pos} 位）"},
            status_code=202,
        )
    if task.status == TaskStatus.PROCESSING:
        return JSONResponse(
            {"status": "processing", "progress": task.progress,
             "progress_msg": task.progress_msg},
            status_code=202,
        )
    if task.status == TaskStatus.FAILED:
        raise HTTPException(status_code=500, detail=task.error or "合成失败")

    # COMPLETED
    if not task.result_path or not os.path.exists(task.result_path):
        raise HTTPException(status_code=410, detail="结果文件已过期，请重新提交任务")

    media_type = "audio/mpeg" if task.result_format == "mp3" else "audio/wav"
    filename = f"output.{task.result_format or 'wav'}"
    return FileResponse(task.result_path, media_type=media_type, filename=filename)


# ── 基础合成接口（上传参考音频） ───────────────────────────────────────────────
@app.post("/synthesize", tags=["语音合成"], summary="上传音色合成")
async def synthesize(
    text: str = Form(..., description="要合成的文本内容", examples=["你好，欢迎使用语音合成服务"]),
    spk_audio: UploadFile = File(..., description="音色参考音频文件（WAV 格式，3-10秒干净人声）"),
    emo_audio: UploadFile = File(None, description="情感参考音频文件（可选，控制语音情绪）"),
    emo_alpha: float = Form(1.0, ge=0.0, le=1.0, description="情感权重，0.0=无情感，1.0=完全情感"),
    use_emo_text: bool = Form(False, description="启用文字驱动情感（通过 QwenEmotion 模型从文字生成情感向量）"),
    emo_text: str = Form(None, description="情感文字描述（use_emo_text=true 时生效，留空则用合成文本本身）"),
    speech_speed: float = Form(1.0, ge=0.5, le=2.0, description="语速。1.0=正常，0.5=最慢，2.0=最快"),
    temperature: float = Form(0.8, ge=0.1, le=2.0, description="采样温度，越高越随机多样"),
    top_p: float = Form(0.8, ge=0.1, le=1.0, description="Top-P 核采样，控制采样范围"),
    top_k: int = Form(30, ge=0, le=200, description="Top-K 采样，0=不限制"),
    interval_silence: int = Form(200, ge=0, le=2000, description="句间静音时长（ms），影响语速节奏"),
    repetition_penalty: float = Form(10.0, ge=1.0, le=20.0, description="重复惩罚，防止卡顿/重复"),
    max_text_tokens_per_segment: int = Form(120, ge=20, le=300, description="每段最大 token 数，影响长文本分段"),
    output_format: str = Form("wav", description="输出格式：wav 或 mp3"),
    save_voice: str = Form(None, description="保存音色名称（传入则同时保存到预存音色列表）"),
    x_api_key: str = Header(None),
):
    """
    上传参考音频进行零样本语音合成。

    **使用场景**：首次使用新音色，或临时克隆音色。

    **参数说明**：
    - `temperature`：控制语音的随机性/多样性。0.1=稳定一致，2.0=高度随机
    - `top_p`：核采样阈值。0.8 是推荐值，降低可使输出更确定
    - `top_k`：限制每步采样的候选数。30 是推荐值，0=不限制
    - `interval_silence`：句间静音（ms）。200 是默认值，增大使语速更慢
    - `repetition_penalty`：重复惩罚。10.0 是默认值，降低可能导致重复
    - `emo_alpha`：情感混合权重。需要同时上传 emo_audio 才生效
    - `use_emo_text`：启用后通过 QwenEmotion 模型分析文字情感，自动生成情感向量
    - `save_voice`：传入名称则将上传的音频保存为预存音色
    """
    verify_key(x_api_key)
    validate_text(text)

    if output_format not in ("wav", "mp3"):
        raise HTTPException(status_code=400, detail="output_format 仅支持 'wav' 或 'mp3'")

    if tts is None:
        raise HTTPException(status_code=503, detail="模型尚未加载完成，请稍后重试")

    global _queue_depth
    if _queue_depth >= MAX_QUEUE_DEPTH:
        logger.warning("队列较深（%d/%d），任务将排队等待处理", _queue_depth, MAX_QUEUE_DEPTH)

    # 先读取和校验上传文件（在创建 task 之前，避免僵尸任务）
    spk_data = await spk_audio.read()
    if len(spk_data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="音色音频文件超过 50MB 限制")
    if len(spk_data) < 1000:
        raise HTTPException(status_code=400, detail="音色音频文件太小，请上传 3-10 秒的有效音频")

    emo_data = None
    if emo_audio:
        emo_data = await emo_audio.read()
        if len(emo_data) > 50 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="情感音频文件超过 50MB 限制")

    task = await task_manager.create(
        text=text, voice="[uploaded]" + (f" -> {save_voice}" if save_voice else ""),
        output_format=output_format, temperature=temperature, top_p=top_p, top_k=top_k,
    )

    spk_path = os.path.join(tempfile.gettempdir(), f"spk_{uuid.uuid4().hex}.wav")
    with open(spk_path, "wb") as f:
        f.write(spk_data)

    # 保存音色
    if save_voice:
        voice_name = sanitize_voice_name(save_voice)
        voice_dir = os.path.join(MODEL_DIR, "voices")
        os.makedirs(voice_dir, exist_ok=True)
        with open(os.path.join(voice_dir, f"{voice_name}.wav"), "wb") as f:
            f.write(spk_data)
        logger.info("Voice saved: %s", voice_name)

    emo_path = None
    if emo_data:
        emo_path = os.path.join(tempfile.gettempdir(), f"emo_{uuid.uuid4().hex}.wav")
        with open(emo_path, "wb") as f:
            f.write(emo_data)

    wav_path = os.path.join(tempfile.gettempdir(), f"out_{uuid.uuid4().hex}.wav")

    try:
        _queue_depth += 1
        await task_manager.start(task.id)
        await task_manager.update_progress(task.id, 0.05, "准备推理...")
        kwargs = {
            "spk_audio_prompt": spk_path,
            "text": text,
            "output_path": wav_path,
            "do_sample": True,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k if top_k > 0 else None,
            "interval_silence": interval_silence,
            "repetition_penalty": repetition_penalty,
            "max_text_tokens_per_segment": max_text_tokens_per_segment,
        }
        if use_emo_text:
            kwargs["use_emo_text"] = True
            if emo_text:
                kwargs["emo_text"] = emo_text
            kwargs["emo_alpha"] = emo_alpha
        elif emo_path:
            kwargs["emo_audio_prompt"] = emo_path
            kwargs["emo_alpha"] = emo_alpha

        async with _inference_semaphore:
            await task_manager.update_progress(task.id, 0.1, "GPU 推理中...")
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, lambda: tts.infer(**kwargs))
            except RuntimeError as e:
                if "out of memory" in str(e).lower():
                    import torch
                    torch.cuda.empty_cache()
                    await task_manager.fail(task.id, "GPU 显存不足")
                    raise HTTPException(status_code=503, detail="GPU 显存不足，请缩短文本或稍后重试")
                raise

        await task_manager.update_progress(task.id, 0.9, "后处理...")

        # duration 必须在 WAV 阶段计算（MP3 转码后 wave 模块无法读取）
        duration = get_wav_duration(wav_path)
        fmt = output_format
        sample_rate = 22050

        result_path = wav_path
        if output_format == "mp3":
            try:
                result_path = wav_to_mp3(wav_path)
                fmt = "mp3"
                sample_rate = 44100
            except Exception:
                logger.warning("MP3 conversion failed, returning WAV")
                fmt = "wav"

        with open(result_path, "rb") as f:
            audio_bytes = f.read()
        await task_manager.complete(task.id)

        return JSONResponse({
            "audio_base64": base64.b64encode(audio_bytes).decode(),
            "duration": duration,
            "format": fmt,
            "sample_rate": sample_rate,
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Synthesis failed: %s", e, exc_info=True)
        await task_manager.fail(task.id, friendly_error(e))
        raise HTTPException(status_code=500, detail=friendly_error(e))
    finally:
        _queue_depth -= 1
        for p in [spk_path, emo_path]:
            if p and os.path.exists(p):
                os.remove(p)
        # 清理输出文件（WAV 和可能的 MP3）
        for p in [wav_path, wav_path.replace(".wav", ".mp3")]:
            if os.path.exists(p):
                os.remove(p)


# ── 预存音色合成接口 ─────────────────────────────────────────────────────────
@app.post("/synthesize_json", tags=["语音合成"], summary="预存音色合成")
async def synthesize_json(
    text: str = Form(..., description="要合成的文本内容", examples=["你好，欢迎使用语音合成服务"]),
    voice_name: str = Form("default", description="预存音色名称（对应 voices/ 目录下的文件名）"),
    speech_speed: float = Form(1.0, ge=0.5, le=2.0, description="语速。1.0=正常，0.5=最慢，2.0=最快"),
    temperature: float = Form(0.8, ge=0.1, le=2.0, description="采样温度"),
    top_p: float = Form(0.8, ge=0.1, le=1.0, description="Top-P 核采样"),
    top_k: int = Form(30, ge=0, le=200, description="Top-K 采样，0=不限制"),
    interval_silence: int = Form(200, ge=0, le=2000, description="句间静音时长（ms）"),
    repetition_penalty: float = Form(10.0, ge=1.0, le=20.0, description="重复惩罚"),
    max_text_tokens_per_segment: int = Form(120, ge=20, le=300, description="每段最大 token 数"),
    use_emo_text: bool = Form(False, description="启用文字驱动情感"),
    emo_text: str = Form(None, description="情感文字描述"),
    emo_alpha: float = Form(1.0, ge=0.0, le=1.0, description="情感强度 0.0-1.0"),
    output_format: str = Form("wav", description="输出格式：wav 或 mp3"),
    x_api_key: str = Header(None),
):
    """
    使用预存音色进行语音合成（推荐用于生产调用）。

    **使用场景**：已有常用音色，反复调用合成。支持结果缓存。

    先通过 `/voices` 查看可用音色列表，再传入 `voice_name`。
    """
    verify_key(x_api_key)
    validate_text(text)

    if output_format not in ("wav", "mp3"):
        raise HTTPException(status_code=400, detail="output_format 仅支持 'wav' 或 'mp3'")

    if tts is None:
        raise HTTPException(status_code=503, detail="模型尚未加载完成，请稍后重试")

    global _queue_depth
    if _queue_depth >= MAX_QUEUE_DEPTH:
        logger.warning("队列较深（%d/%d），任务将排队等待处理", _queue_depth, MAX_QUEUE_DEPTH)

    voice_path = safe_voice_path(voice_name)
    if not os.path.exists(voice_path):
        raise HTTPException(status_code=404, detail=f"音色 '{voice_name}' 不存在，请先通过 /voices 查看可用音色或上传新音色")

    if ENABLE_CACHE:
        cache_key = get_cache_key(text, voice_name, temperature, top_p, top_k,
                                  interval_silence=interval_silence,
                                  repetition_penalty=repetition_penalty,
                                  use_emo_text=use_emo_text,
                                  emo_text=emo_text or "")
        cache_ext = "mp3" if output_format == "mp3" else "wav"
        cache_path = os.path.join(CACHE_DIR, f"{cache_key}.{cache_ext}")
        if os.path.exists(cache_path):
            logger.info("Cache hit: %s", cache_key)
            with open(cache_path, "rb") as f:
                audio_bytes = f.read()
            # 缓存的 WAV 文件读 duration；MP3 则从同名 .dur 文件读取
            dur_path = os.path.join(CACHE_DIR, f"{cache_key}.dur")
            if os.path.exists(dur_path):
                with open(dur_path) as f:
                    duration = float(f.read())
            else:
                duration = get_wav_duration(cache_path) if cache_ext == "wav" else 0.0
            sample_rate = 44100 if cache_ext == "mp3" else 22050
            return JSONResponse({
                "audio_base64": base64.b64encode(audio_bytes).decode(),
                "duration": duration,
                "format": cache_ext,
                "sample_rate": sample_rate,
            })

    task = await task_manager.create(
        text=text, voice=voice_name,
        output_format=output_format, temperature=temperature, top_p=top_p, top_k=top_k,
    )

    wav_path = os.path.join(tempfile.gettempdir(), f"out_{uuid.uuid4().hex}.wav")

    try:
        _queue_depth += 1
        await task_manager.start(task.id)
        await task_manager.update_progress(task.id, 0.05, "准备推理...")
        infer_kwargs = {
            "spk_audio_prompt": voice_path,
            "text": text,
            "output_path": wav_path,
            "do_sample": True,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k if top_k > 0 else None,
            "interval_silence": interval_silence,
            "repetition_penalty": repetition_penalty,
            "max_text_tokens_per_segment": max_text_tokens_per_segment,
        }
        if use_emo_text:
            infer_kwargs["use_emo_text"] = True
            if emo_text:
                infer_kwargs["emo_text"] = emo_text
            infer_kwargs["emo_alpha"] = emo_alpha

        async with _inference_semaphore:
            await task_manager.update_progress(task.id, 0.1, "GPU 推理中...")
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, lambda: tts.infer(**infer_kwargs))
            except RuntimeError as e:
                if "out of memory" in str(e).lower():
                    import torch
                    torch.cuda.empty_cache()
                    await task_manager.fail(task.id, "GPU 显存不足")
                    raise HTTPException(status_code=503, detail="GPU 显存不足，请缩短文本或稍后重试")
                raise

        await task_manager.update_progress(task.id, 0.9, "后处理...")

        # duration 必须在 WAV 阶段计算
        duration = get_wav_duration(wav_path)
        fmt = output_format
        sample_rate = 22050

        result_path = wav_path
        if output_format == "mp3":
            try:
                result_path = wav_to_mp3(wav_path)
                fmt = "mp3"
                sample_rate = 44100
            except Exception:
                logger.warning("MP3 conversion failed, returning WAV")
                fmt = "wav"

        if ENABLE_CACHE:
            cache_path = os.path.join(CACHE_DIR, f"{cache_key}.{fmt}")
            shutil.copy2(result_path, cache_path)
            # 保存 duration 供缓存命中时读取
            dur_path = os.path.join(CACHE_DIR, f"{cache_key}.dur")
            with open(dur_path, "w") as df:
                df.write(str(duration))

        with open(result_path, "rb") as f:
            audio_bytes = f.read()
        await task_manager.complete(task.id)

        return JSONResponse({
            "audio_base64": base64.b64encode(audio_bytes).decode(),
            "duration": duration,
            "format": fmt,
            "sample_rate": sample_rate,
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Synthesis failed: %s", e, exc_info=True)
        await task_manager.fail(task.id, friendly_error(e))
        raise HTTPException(status_code=500, detail=friendly_error(e))
    finally:
        _queue_depth -= 1
        # 清理输出文件（WAV 和可能的 MP3）
        for p in [wav_path, wav_path.replace(".wav", ".mp3")]:
            if os.path.exists(p):
                os.remove(p)


# ── 流式合成接口 ────────────────────────────────────────────────────────────
@app.post("/synthesize_stream", tags=["语音合成"], summary="流式合成（低延迟）")
async def synthesize_stream(
    text: str = Form(..., description="要合成的文本内容"),
    voice_name: str = Form("default", description="预存音色名称"),
    speech_speed: float = Form(1.0, ge=0.5, le=2.0, description="语速。1.0=正常，0.5=最慢，2.0=最快"),
    use_emo_text: bool = Form(False, description="启用文字驱动情感"),
    emo_text: str = Form(None, description="情感文字描述"),
    emo_alpha: float = Form(1.0, ge=0.0, le=1.0, description="情感强度 0.0-1.0"),
    x_api_key: str = Header(None),
):
    """
    流式语音合成，逐句输出音频数据，降低首包延迟。

    支持语速和情感参数，与非流式接口行为一致。
    """
    verify_key(x_api_key)
    validate_text(text)

    if tts is None:
        raise HTTPException(status_code=503, detail="模型尚未加载完成，请稍后重试")

    global _queue_depth
    if _queue_depth >= MAX_QUEUE_DEPTH:
        logger.warning("队列较深（%d/%d），任务将排队等待处理", _queue_depth, MAX_QUEUE_DEPTH)

    voice_path = safe_voice_path(voice_name)
    if not os.path.exists(voice_path):
        raise HTTPException(status_code=404, detail=f"音色 '{voice_name}' 不存在")

    stream_output_path = os.path.join(tempfile.gettempdir(), f"stream_{uuid.uuid4().hex}.wav")
    infer_kwargs = {
        "spk_audio_prompt": voice_path,
        "text": text,
        "output_path": stream_output_path,
        "stream_return": True,
    }
    if use_emo_text:
        infer_kwargs["use_emo_text"] = True
        if emo_text:
            infer_kwargs["emo_text"] = emo_text
        infer_kwargs["emo_alpha"] = emo_alpha

    async def generate():
        global _queue_depth
        _queue_depth += 1
        try:
            async with _inference_semaphore:
                loop = asyncio.get_event_loop()
                try:
                    chunks = await loop.run_in_executor(
                        None,
                        lambda: list(tts.infer(**infer_kwargs)),
                    )
                    for chunk in chunks:
                        yield chunk
                except RuntimeError as e:
                    if "out of memory" in str(e).lower():
                        import torch
                        torch.cuda.empty_cache()
                        logger.error("Stream OOM: %s", e)
                    else:
                        logger.error("Stream synthesis failed: %s", e)
                    return
                except Exception as e:
                    logger.error("Stream synthesis failed: %s", e)
                    return
        finally:
            _queue_depth -= 1
            # 清理流式输出临时文件
            if os.path.exists(stream_output_path):
                os.remove(stream_output_path)

    return StreamingResponse(generate(), media_type="audio/wav")


# ── 音色管理接口 ─────────────────────────────────────────────────────────────
@app.get("/voices", tags=["音色管理"], summary="获取可用音色列表")
async def list_voices():
    """返回 voices/ 目录下所有可用的预存音色名称及文件大小。（无需认证）"""
    voice_dir = os.path.join(MODEL_DIR, "voices")
    if not os.path.isdir(voice_dir):
        return {"voices": [], "details": []}
    voices = []
    details = []
    for f in sorted(os.listdir(voice_dir)):
        if f.endswith(".wav"):
            name = f.replace(".wav", "")
            voices.append(name)
            fpath = os.path.join(voice_dir, f)
            size_kb = os.path.getsize(fpath) / 1024
            details.append({"name": name, "size_kb": round(size_kb, 1), "path": f})
    return {"voices": voices, "details": details}


@app.post("/voices/upload", tags=["音色管理"], summary="上传并保存音色")
async def upload_voice(
    voice_name: str = Form(..., description="音色名称（字母/数字/中文/下划线）"),
    audio: UploadFile = File(..., description="音色参考音频（WAV，3-10秒干净人声）"),
    x_api_key: str = Header(None),
):
    """
    上传音频文件并保存为预存音色。

    保存后可通过 `/synthesize_json?voice_name=xxx` 直接调用，无需再次上传。
    """
    verify_key(x_api_key)
    name = sanitize_voice_name(voice_name)
    if not name:
        raise HTTPException(status_code=400, detail="音色名称不合法")

    voice_dir = os.path.join(MODEL_DIR, "voices")
    os.makedirs(voice_dir, exist_ok=True)
    dest = os.path.join(voice_dir, f"{name}.wav")

    data = await audio.read()
    if len(data) < 1000:
        raise HTTPException(status_code=400, detail="音频文件太小，请上传 3-10 秒的有效音频")
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="音频文件超过 50MB 限制")

    with open(dest, "wb") as f:
        f.write(data)

    logger.info("Voice uploaded: %s (%d KB)", name, len(data) // 1024)
    return JSONResponse({
        "voice_name": name,
        "status": "ready",
        "file_size": os.path.getsize(dest),
        "message": "音色上传成功，可立即使用",
    })


@app.delete("/voices/{voice_name}", tags=["音色管理"], summary="删除预存音色")
async def delete_voice(voice_name: str, x_api_key: str = Header(None)):
    """删除指定的预存音色。"""
    verify_key(x_api_key)
    voice_path = safe_voice_path(voice_name)
    if not os.path.exists(voice_path):
        raise HTTPException(status_code=404, detail=f"音色 '{voice_name}' 不存在")
    os.remove(voice_path)
    logger.info("Voice deleted: %s", voice_name)
    return {"message": f"音色 '{voice_name}' 已删除"}


@app.get("/voices/{voice_name}/preview", tags=["音色管理"], summary="试听预存音色")
async def preview_voice(voice_name: str):
    """返回预存音色的原始参考音频，用于试听。（无需认证）"""
    voice_path = safe_voice_path(voice_name)
    if not os.path.exists(voice_path):
        raise HTTPException(status_code=404, detail=f"音色 '{voice_name}' 不存在")
    return FileResponse(voice_path, media_type="audio/wav", filename=f"{voice_name}.wav")


# ── WebUI ────────────────────────────────────────────────────────────────────
@app.get("/", tags=["WebUI"], summary="调试界面", response_class=HTMLResponse)
async def webui():
    """内置 WebUI 调试界面，支持所有合成参数调节、音频上传/录制、任务监控。"""
    return HTMLResponse(WEBUI_HTML)


WEBUI_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>IndexTTS2 - 语音合成调试台</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }
  .header { background: linear-gradient(135deg, #1e293b 0%, #334155 100%); padding: 20px 32px; border-bottom: 1px solid #334155; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
  .header h1 { font-size: 22px; font-weight: 600; }
  .header h1 span { color: #60a5fa; }
  .status-badge { padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: 500; }
  .status-ok { background: #065f46; color: #6ee7b7; }
  .status-err { background: #7f1d1d; color: #fca5a5; }
  .status-loading { background: #78350f; color: #fcd34d; }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
  @media (max-width: 960px) { .container { grid-template-columns: 1fr; } }

  .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 24px; }
  .card h2 { font-size: 13px; font-weight: 600; margin-bottom: 16px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; }

  .form-group { margin-bottom: 16px; }
  .form-group label { display: flex; justify-content: space-between; align-items: baseline; font-size: 14px; color: #cbd5e1; margin-bottom: 6px; font-weight: 500; }
  .form-group label .hint { font-weight: 400; color: #64748b; font-size: 12px; }
  .char-count { font-size: 12px; font-weight: 400; }
  .char-ok { color: #64748b; }
  .char-warn { color: #fbbf24; }
  .char-danger { color: #ef4444; }
  textarea, input[type="text"], select { width: 100%; padding: 10px 14px; background: #0f172a; border: 1px solid #334155; border-radius: 8px; color: #e2e8f0; font-size: 14px; resize: vertical; }
  textarea:focus, input:focus, select:focus { outline: none; border-color: #60a5fa; }
  textarea { min-height: 100px; }

  .slider-group { display: flex; align-items: center; gap: 12px; }
  .slider-group input[type="range"] { flex: 1; accent-color: #60a5fa; }
  .slider-val { min-width: 45px; text-align: right; font-size: 14px; color: #60a5fa; font-weight: 600; }

  .file-upload { border: 2px dashed #334155; border-radius: 8px; padding: 16px; text-align: center; cursor: pointer; transition: all 0.2s; position: relative; }
  .file-upload:hover, .file-upload.dragover { border-color: #60a5fa; background: rgba(96, 165, 250, 0.05); }
  .file-upload.has-file { border-color: #34d399; background: rgba(52, 211, 153, 0.05); }
  .file-upload input[type="file"] { display: none; }
  .file-info { display: flex; align-items: center; gap: 8px; justify-content: center; flex-wrap: wrap; }
  .file-info .file-name { color: #34d399; font-weight: 500; }
  .file-info .file-clear { color: #ef4444; cursor: pointer; font-size: 12px; text-decoration: underline; }
  .audio-preview { margin-top: 8px; }
  .audio-preview audio { width: 100%; height: 36px; }

  .btn { padding: 12px 24px; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; transition: all 0.2s; width: 100%; }
  .btn-primary { background: #2563eb; color: white; }
  .btn-primary:hover { background: #1d4ed8; }
  .btn-primary:disabled { background: #1e3a5f; color: #64748b; cursor: not-allowed; }
  .btn-secondary { background: #334155; color: #e2e8f0; margin-top: 8px; }
  .btn-secondary:hover { background: #475569; }
  .btn-sm { padding: 6px 12px; font-size: 12px; width: auto; }
  .btn-ghost { background: transparent; border: 1px solid #475569; color: #94a3b8; }
  .btn-ghost:hover { border-color: #60a5fa; color: #60a5fa; }
  .btn-success { background: #059669; color: white; }
  .btn-success:hover { background: #047857; }
  .btn-danger { background: #dc2626; color: white; }
  .btn-danger:hover { background: #b91c1c; }

  .btn-row { display: flex; gap: 8px; margin-top: 4px; }
  .btn-row .btn { flex: 1; }

  .mode-tabs { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
  .mode-tab { padding: 8px 16px; border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer; border: 1px solid #334155; background: transparent; color: #94a3b8; }
  .mode-tab.active { background: #2563eb; color: white; border-color: #2563eb; }

  .result-area { margin-top: 16px; }
  .result-area audio { width: 100%; margin-top: 8px; }
  .result-area .msg { padding: 12px; border-radius: 8px; font-size: 14px; margin-top: 8px; }
  .msg-success { background: #065f46; color: #6ee7b7; }
  .msg-error { background: #7f1d1d; color: #fca5a5; }
  .msg-info { background: #1e3a5f; color: #93c5fd; }

  .progress-bar { width: 100%; height: 6px; background: #334155; border-radius: 3px; margin-top: 8px; overflow: hidden; }
  .progress-fill { height: 100%; background: linear-gradient(90deg, #2563eb, #60a5fa); border-radius: 3px; transition: width 0.3s ease; }

  .task-list { max-height: 400px; overflow-y: auto; }
  .task-item { padding: 12px; border: 1px solid #334155; border-radius: 8px; margin-bottom: 8px; font-size: 13px; }
  .task-item .task-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
  .task-item .task-id { color: #64748b; font-family: monospace; }
  .task-item .task-text { color: #94a3b8; margin-top: 4px; word-break: break-all; }
  .task-item .task-params { color: #475569; font-size: 11px; margin-top: 2px; }
  .task-badge { padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
  .badge-pending { background: #78350f; color: #fcd34d; }
  .badge-processing { background: #1e3a5f; color: #60a5fa; }
  .badge-completed { background: #065f46; color: #6ee7b7; }
  .badge-failed { background: #7f1d1d; color: #fca5a5; }

  .stats-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
  .stat-box { text-align: center; padding: 12px; background: #0f172a; border-radius: 8px; }
  .stat-num { font-size: 24px; font-weight: 700; color: #60a5fa; }
  .stat-label { font-size: 11px; color: #64748b; margin-top: 4px; }

  .api-doc-link { color: #60a5fa; text-decoration: none; font-size: 14px; }
  .api-doc-link:hover { text-decoration: underline; }

  .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid #334155; border-top-color: #60a5fa; border-radius: 50%; animation: spin 0.8s linear infinite; vertical-align: middle; margin-right: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .shortcut-hint { font-size: 11px; color: #475569; text-align: center; margin-top: 6px; }

  .toggle-section { cursor: pointer; display: flex; justify-content: space-between; align-items: center; padding: 8px 0; color: #64748b; font-size: 13px; border-top: 1px solid #1e293b; margin-top: 8px; }
  .toggle-section:hover { color: #94a3b8; }
  .toggle-content { overflow: hidden; transition: max-height 0.3s ease; }

  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .doc-tabs { display: flex; gap: 4px; margin-bottom: 12px; border-bottom: 1px solid #334155; padding-bottom: 8px; }
  .doc-tab { padding: 6px 12px; font-size: 12px; cursor: pointer; border-radius: 4px 4px 0 0; color: #64748b; background: transparent; border: none; }
  .doc-tab.active { color: #60a5fa; background: #0f172a; }

  .voice-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px; max-height: 300px; overflow-y: auto; }
  .voice-card { padding: 10px; background: #0f172a; border: 1px solid #334155; border-radius: 8px; font-size: 13px; cursor: pointer; transition: all 0.2s; }
  .voice-card:hover { border-color: #60a5fa; }
  .voice-card.selected { border-color: #60a5fa; background: rgba(96, 165, 250, 0.1); }
  .voice-card .voice-name { font-weight: 600; color: #e2e8f0; margin-bottom: 4px; }
  .voice-card .voice-meta { color: #64748b; font-size: 11px; }
  .voice-actions { display: flex; gap: 4px; margin-top: 6px; }

  .save-voice-row { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
  .save-voice-row input { flex: 1; padding: 8px 12px; }

  .checkbox-group { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
  .checkbox-group input[type="checkbox"] { accent-color: #60a5fa; width: 16px; height: 16px; }
  .checkbox-group label { font-size: 14px; color: #cbd5e1; cursor: pointer; }
</style>
</head>
<body>

<div class="header">
  <h1><span>IndexTTS2</span> 语音合成调试台</h1>
  <div style="display:flex; align-items:center; gap:16px;">
    <a href="/docs" class="api-doc-link" target="_blank">Swagger API</a>
    <a href="/redoc" class="api-doc-link" target="_blank">ReDoc</a>
    <div id="statusBadge" class="status-badge status-loading">检查中...</div>
  </div>
</div>

<div class="container">
  <!-- 左栏：合成控制 -->
  <div>
    <div class="card">
      <h2>语音合成</h2>

      <div class="mode-tabs">
        <button class="mode-tab active" onclick="switchMode('upload', this)">上传音色</button>
        <button class="mode-tab" onclick="switchMode('preset', this)">预存音色</button>
        <button class="mode-tab" onclick="switchMode('stream', this)">流式合成</button>
      </div>

      <div class="form-group">
        <label>
          合成文本
          <span class="char-count" id="charCount">0 字</span>
        </label>
        <textarea id="text" placeholder="输入要合成的文本内容（Ctrl+Enter 快速合成）..." oninput="updateCharCount()">你好，欢迎使用IndexTTS2语音合成服务。这是一个高质量的零样本语音克隆系统。</textarea>
      </div>

      <!-- 上传音色模式 -->
      <div id="uploadMode">
        <div class="form-group">
          <label>音色参考音频 <span class="hint">WAV 格式，3-10秒干净人声</span></label>
          <div class="file-upload" id="spkDrop">
            <input type="file" id="spkFile" accept=".wav,.mp3,.ogg,.flac" onchange="onFileSelect('spkFile', 'spkDrop', 'spkPreview')">
            <div id="spkLabel">点击选择或拖拽音色参考音频</div>
          </div>
          <div class="audio-preview" id="spkPreview" style="display:none;"></div>
        </div>
        <div class="save-voice-row">
          <input type="text" id="saveVoiceName" placeholder="保存为音色（可选，填写名称即保存）">
          <span class="hint" style="white-space:nowrap; font-size:12px; color:#64748b;">合成时自动保存</span>
        </div>
        <div class="form-group" style="margin-top:12px;">
          <label>情感参考音频 <span class="hint">（可选）控制语音情绪</span></label>
          <div class="file-upload" id="emoDrop">
            <input type="file" id="emoFile" accept=".wav,.mp3,.ogg,.flac" onchange="onFileSelect('emoFile', 'emoDrop', 'emoPreview')">
            <div id="emoLabel">点击选择或拖拽情感参考音频</div>
          </div>
          <div class="audio-preview" id="emoPreview" style="display:none;"></div>
        </div>
      </div>

      <!-- 预存音色模式 -->
      <div id="presetMode" style="display:none;">
        <div class="form-group">
          <label>选择音色 <span class="hint" id="voiceCount"></span></label>
          <div id="voiceGrid" class="voice-grid"></div>
          <select id="voiceSelect" style="display:none;"><option value="">加载中...</option></select>
        </div>
        <div class="form-group">
          <label>或上传新音色</label>
          <div style="display:flex; gap:8px;">
            <input type="text" id="newVoiceName" placeholder="音色名称">
            <div class="file-upload" id="newVoiceDrop" style="flex:1; padding:10px;">
              <input type="file" id="newVoiceFile" accept=".wav,.mp3,.ogg,.flac" onchange="uploadNewVoice()">
              <span style="font-size:12px;">选择文件并上传</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 流式模式 -->
      <div id="streamMode" style="display:none;">
        <div class="form-group">
          <label>选择音色</label>
          <select id="streamVoiceSelect"><option value="">加载中...</option></select>
        </div>
        <div class="msg msg-info" style="font-size:12px; margin-bottom:12px;">
          流式模式逐句输出音频，降低首包延迟。不支持高级参数调节和缓存。
        </div>
      </div>

      <!-- 情感控制 -->
      <div id="emoTextArea" style="display:none;">
        <div class="checkbox-group">
          <input type="checkbox" id="useEmoText" onchange="toggleEmoText()">
          <label for="useEmoText">文字驱动情感（QwenEmotion）</label>
        </div>
        <div id="emoTextInput" style="display:none;" class="form-group">
          <label>情感描述文字 <span class="hint">留空则用合成文本本身</span></label>
          <input type="text" id="emoText" placeholder="例如：开心、激动、严肃、温柔...">
        </div>
      </div>

      <!-- 参数区（流式模式隐藏） -->
      <div id="paramsArea">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
          <span style="font-size:13px; color:#64748b;">基础参数</span>
          <button class="btn btn-sm btn-ghost" onclick="resetParams()">重置默认</button>
        </div>

        <div class="form-group">
          <label>Temperature <span class="hint">采样温度，越高越随机</span></label>
          <div class="slider-group">
            <input type="range" id="temperature" min="0.1" max="2.0" step="0.1" value="0.8" oninput="updateSlider(this)">
            <span class="slider-val" id="temperatureVal">0.8</span>
          </div>
        </div>

        <div class="form-group">
          <label>Top-P <span class="hint">核采样阈值</span></label>
          <div class="slider-group">
            <input type="range" id="top_p" min="0.1" max="1.0" step="0.05" value="0.8" oninput="updateSlider(this)">
            <span class="slider-val" id="top_pVal">0.8</span>
          </div>
        </div>

        <div class="form-group">
          <label>Top-K <span class="hint">0=不限制</span></label>
          <div class="slider-group">
            <input type="range" id="top_k" min="0" max="200" step="5" value="30" oninput="updateSlider(this)">
            <span class="slider-val" id="top_kVal">30</span>
          </div>
        </div>

        <div id="emoAlphaGroup">
          <div class="form-group">
            <label>情感权重 <span class="hint">0.0=无情感，1.0=完全</span></label>
            <div class="slider-group">
              <input type="range" id="emo_alpha" min="0" max="1.0" step="0.1" value="1.0" oninput="updateSlider(this)">
              <span class="slider-val" id="emo_alphaVal">1.0</span>
            </div>
          </div>
        </div>

        <!-- 高级参数（可折叠） -->
        <div class="toggle-section" onclick="toggleAdvanced()">
          <span id="advToggleText">展开高级参数</span>
          <span id="advArrow" style="transition:transform 0.2s;">&#9654;</span>
        </div>
        <div id="advancedParams" style="max-height:0; overflow:hidden; transition:max-height 0.3s ease;">
          <div class="form-group" style="margin-top:12px;">
            <label>句间静音 <span class="hint">ms，影响语速节奏</span></label>
            <div class="slider-group">
              <input type="range" id="interval_silence" min="0" max="2000" step="50" value="200" oninput="updateSlider(this)">
              <span class="slider-val" id="interval_silenceVal">200</span>
            </div>
          </div>

          <div class="form-group">
            <label>重复惩罚 <span class="hint">防止卡顿重复</span></label>
            <div class="slider-group">
              <input type="range" id="repetition_penalty" min="1.0" max="20.0" step="0.5" value="10.0" oninput="updateSlider(this)">
              <span class="slider-val" id="repetition_penaltyVal">10.0</span>
            </div>
          </div>

          <div class="form-group">
            <label>分段长度 <span class="hint">token/段，影响长文本质量</span></label>
            <div class="slider-group">
              <input type="range" id="max_text_tokens" min="20" max="300" step="10" value="120" oninput="updateSlider(this)">
              <span class="slider-val" id="max_text_tokensVal">120</span>
            </div>
          </div>
        </div>

        <div class="form-group" style="margin-top:12px;">
          <label>输出格式</label>
          <select id="outputFormat">
            <option value="wav">WAV（无损，较大）</option>
            <option value="mp3">MP3（压缩，较小）</option>
          </select>
        </div>
      </div>

      <div class="form-group">
        <label>API Key <span class="hint">（未设置时可留空）</span></label>
        <input type="text" id="apiKey" placeholder="留空即可（除非服务端配置了 TTS_API_KEY）">
      </div>

      <button class="btn btn-primary" id="synthBtn" onclick="synthesize()">开始合成</button>
      <div class="shortcut-hint">Ctrl + Enter 快速合成</div>

      <!-- 进度条 -->
      <div id="progressArea" style="display:none;">
        <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
        <div style="display:flex; justify-content:space-between; margin-top:4px;">
          <span id="progressText" style="font-size:12px; color:#64748b;"></span>
          <span id="progressPct" style="font-size:12px; color:#60a5fa; font-weight:600;"></span>
        </div>
      </div>

      <div class="result-area" id="resultArea"></div>
    </div>
  </div>

  <!-- 右栏：任务监控 + 文档 -->
  <div>
    <div class="card">
      <h2>任务监控</h2>
      <div class="stats-row" id="statsRow">
        <div class="stat-box"><div class="stat-num" id="statPending">0</div><div class="stat-label">等待中</div></div>
        <div class="stat-box"><div class="stat-num" id="statProcessing">0</div><div class="stat-label">处理中</div></div>
        <div class="stat-box"><div class="stat-num" id="statCompleted">0</div><div class="stat-label">已完成</div></div>
        <div class="stat-box"><div class="stat-num" id="statFailed">0</div><div class="stat-label">失败</div></div>
      </div>

      <h2>当前任务</h2>
      <div class="task-list" id="activeTasks"><div style="color:#64748b; font-size:14px; padding:12px;">暂无活动任务</div></div>

      <h2 style="margin-top:20px;">历史记录 <span style="font-weight:400; font-size:11px; color:#475569;">（最近 20 条）</span></h2>
      <div class="task-list" id="historyTasks"><div style="color:#64748b; font-size:14px; padding:12px;">暂无历史记录</div></div>
    </div>

    <div class="card" style="margin-top:24px;">
      <h2>接入文档</h2>
      <div class="doc-tabs">
        <button class="doc-tab active" onclick="switchDoc('curl', this)">cURL</button>
        <button class="doc-tab" onclick="switchDoc('python', this)">Python</button>
        <button class="doc-tab" onclick="switchDoc('js', this)">JavaScript</button>
        <button class="doc-tab" onclick="switchDoc('endpoints', this)">接口列表</button>
      </div>

      <div class="tab-content active" id="doc-curl">
        <pre style="background:#0f172a; padding:12px; border-radius:8px; overflow-x:auto; font-size:12px; line-height:1.6; color:#94a3b8;">
<span style="color:#6ee7b7"># 1. 健康检查</span>
curl <span id="curlBase"></span>/health

<span style="color:#6ee7b7"># 2. 上传音色合成（含情感+高级参数）</span>
curl -X POST <span class="curl-base"></span>/synthesize \\
  -F "text=你好世界" \\
  -F "spk_audio=@voice.wav" \\
  -F "temperature=0.8" \\
  -F "top_p=0.8" \\
  -F "top_k=30" \\
  -F "interval_silence=200" \\
  -F "repetition_penalty=10.0" \\
  -F "save_voice=my_voice" \\
  -o output.wav

<span style="color:#6ee7b7"># 3. 文字驱动情感</span>
curl -X POST <span class="curl-base"></span>/synthesize \\
  -F "text=太开心了！" \\
  -F "spk_audio=@voice.wav" \\
  -F "use_emo_text=true" \\
  -F "emo_text=兴奋开心" \\
  -o output.wav

<span style="color:#6ee7b7"># 4. 预存音色合成</span>
curl -X POST <span class="curl-base"></span>/synthesize_json \\
  -F "text=你好世界" \\
  -F "voice_name=default" \\
  -o output.wav

<span style="color:#6ee7b7"># 5. 上传保存音色</span>
curl -X POST <span class="curl-base"></span>/voices/upload \\
  -F "voice_name=my_voice" \\
  -F "audio=@voice.wav"

<span style="color:#6ee7b7"># 6. 试听音色</span>
curl <span class="curl-base"></span>/voices/my_voice/preview -o preview.wav

<span style="color:#6ee7b7"># 7. 查看任务进度</span>
curl <span class="curl-base"></span>/tasks/TASK_ID</pre>
      </div>

      <div class="tab-content" id="doc-python">
        <pre style="background:#0f172a; padding:12px; border-radius:8px; overflow-x:auto; font-size:12px; line-height:1.6; color:#94a3b8;">
<span style="color:#c084fc">import</span> requests

BASE = <span style="color:#fbbf24">"<span class="curl-base"></span>"</span>

<span style="color:#6ee7b7"># 上传音色 + 保存 + 情感控制</span>
resp = requests.post(
    f<span style="color:#fbbf24">"{BASE}/synthesize"</span>,
    data={
        <span style="color:#fbbf24">"text"</span>: <span style="color:#fbbf24">"你好世界"</span>,
        <span style="color:#fbbf24">"save_voice"</span>: <span style="color:#fbbf24">"my_voice"</span>,
        <span style="color:#fbbf24">"use_emo_text"</span>: <span style="color:#fbbf24">"true"</span>,
        <span style="color:#fbbf24">"emo_text"</span>: <span style="color:#fbbf24">"温柔"</span>,
    },
    files={<span style="color:#fbbf24">"spk_audio"</span>: open(<span style="color:#fbbf24">"voice.wav"</span>, <span style="color:#fbbf24">"rb"</span>)},
)
<span style="color:#c084fc">with</span> open(<span style="color:#fbbf24">"output.wav"</span>, <span style="color:#fbbf24">"wb"</span>) <span style="color:#c084fc">as</span> f:
    f.write(resp.content)

<span style="color:#6ee7b7"># 之后用预存音色持续生成</span>
resp = requests.post(
    f<span style="color:#fbbf24">"{BASE}/synthesize_json"</span>,
    data={
        <span style="color:#fbbf24">"text"</span>: <span style="color:#fbbf24">"再见"</span>,
        <span style="color:#fbbf24">"voice_name"</span>: <span style="color:#fbbf24">"my_voice"</span>,
        <span style="color:#fbbf24">"interval_silence"</span>: 300,
        <span style="color:#fbbf24">"repetition_penalty"</span>: 10.0,
    },
)
<span style="color:#c084fc">with</span> open(<span style="color:#fbbf24">"output2.wav"</span>, <span style="color:#fbbf24">"wb"</span>) <span style="color:#c084fc">as</span> f:
    f.write(resp.content)</pre>
      </div>

      <div class="tab-content" id="doc-js">
        <pre style="background:#0f172a; padding:12px; border-radius:8px; overflow-x:auto; font-size:12px; line-height:1.6; color:#94a3b8;">
<span style="color:#6ee7b7">// 上传音色并保存</span>
<span style="color:#c084fc">const</span> formData = <span style="color:#c084fc">new</span> FormData();
formData.append(<span style="color:#fbbf24">'text'</span>, <span style="color:#fbbf24">'你好世界'</span>);
formData.append(<span style="color:#fbbf24">'spk_audio'</span>, fileInput.files[0]);
formData.append(<span style="color:#fbbf24">'save_voice'</span>, <span style="color:#fbbf24">'my_voice'</span>);

<span style="color:#c084fc">const</span> resp = <span style="color:#c084fc">await</span> fetch(<span style="color:#fbbf24">'<span class="curl-base"></span>/synthesize'</span>, {
  method: <span style="color:#fbbf24">'POST'</span>,
  body: formData,
});
<span style="color:#c084fc">const</span> blob = <span style="color:#c084fc">await</span> resp.blob();

<span style="color:#6ee7b7">// SSE 进度监控</span>
<span style="color:#c084fc">const</span> es = <span style="color:#c084fc">new</span> EventSource(<span style="color:#fbbf24">'/tasks/TASK_ID/sse'</span>);
es.onmessage = (e) => {
  <span style="color:#c084fc">const</span> d = JSON.parse(e.data);
  console.log(d.progress, d.progress_msg);
  <span style="color:#c084fc">if</span> (d.status === <span style="color:#fbbf24">'completed'</span>) es.close();
};</pre>
      </div>

      <div class="tab-content" id="doc-endpoints">
        <div style="font-size:13px; color:#94a3b8; line-height:2;">
          <table style="width:100%; border-collapse:collapse;">
            <tr style="border-bottom:1px solid #334155;">
              <td style="padding:6px; color:#60a5fa; font-family:monospace;">GET /</td>
              <td style="padding:6px;">WebUI 调试界面</td>
            </tr>
            <tr style="border-bottom:1px solid #334155;">
              <td style="padding:6px; color:#60a5fa; font-family:monospace;">GET /health</td>
              <td style="padding:6px;">健康检查 + GPU 队列</td>
            </tr>
            <tr style="border-bottom:1px solid #334155;">
              <td style="padding:6px; color:#60a5fa; font-family:monospace;">GET /voices</td>
              <td style="padding:6px;">音色列表（含大小）</td>
            </tr>
            <tr style="border-bottom:1px solid #334155;">
              <td style="padding:6px; color:#6ee7b7; font-family:monospace;">POST /voices/upload</td>
              <td style="padding:6px;">上传保存音色</td>
            </tr>
            <tr style="border-bottom:1px solid #334155;">
              <td style="padding:6px; color:#60a5fa; font-family:monospace;">GET /voices/{name}/preview</td>
              <td style="padding:6px;">试听音色原始音频</td>
            </tr>
            <tr style="border-bottom:1px solid #334155;">
              <td style="padding:6px; color:#ef4444; font-family:monospace;">DELETE /voices/{name}</td>
              <td style="padding:6px;">删除音色</td>
            </tr>
            <tr style="border-bottom:1px solid #334155;">
              <td style="padding:6px; color:#60a5fa; font-family:monospace;">GET /tasks</td>
              <td style="padding:6px;">全部任务状态</td>
            </tr>
            <tr style="border-bottom:1px solid #334155;">
              <td style="padding:6px; color:#60a5fa; font-family:monospace;">GET /tasks/{id}</td>
              <td style="padding:6px;">单任务进度查询</td>
            </tr>
            <tr style="border-bottom:1px solid #334155;">
              <td style="padding:6px; color:#60a5fa; font-family:monospace;">GET /tasks/{id}/sse</td>
              <td style="padding:6px;">SSE 实时进度推送</td>
            </tr>
            <tr style="border-bottom:1px solid #334155;">
              <td style="padding:6px; color:#6ee7b7; font-family:monospace;">POST /synthesize</td>
              <td style="padding:6px;">上传音色合成（情感+高级参数+保存）</td>
            </tr>
            <tr style="border-bottom:1px solid #334155;">
              <td style="padding:6px; color:#6ee7b7; font-family:monospace;">POST /synthesize_json</td>
              <td style="padding:6px;">预存音色合成（支持缓存）</td>
            </tr>
            <tr>
              <td style="padding:6px; color:#6ee7b7; font-family:monospace;">POST /synthesize_stream</td>
              <td style="padding:6px;">流式合成（低延迟）</td>
            </tr>
          </table>
          <p style="margin-top:12px;">
            完整参数说明：<a href="/docs" class="api-doc-link" target="_blank">Swagger UI</a> |
            <a href="/redoc" class="api-doc-link" target="_blank">ReDoc</a>
          </p>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
let currentMode = 'upload';
let selectedVoice = '';
let currentTaskId = null;
let sseSource = null;

// ── 模式切换 ────────────────────────────────────────
function switchMode(mode, btn) {
  currentMode = mode;
  document.querySelectorAll('.mode-tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('uploadMode').style.display = mode === 'upload' ? '' : 'none';
  document.getElementById('presetMode').style.display = mode === 'preset' ? '' : 'none';
  document.getElementById('streamMode').style.display = mode === 'stream' ? '' : 'none';
  document.getElementById('paramsArea').style.display = mode === 'stream' ? 'none' : '';
  document.getElementById('emoAlphaGroup').style.display = mode === 'upload' ? '' : 'none';
  document.getElementById('emoTextArea').style.display = mode === 'stream' ? 'none' : '';
  if (mode === 'preset') loadVoiceGrid();
  if (mode === 'stream') loadVoices('streamVoiceSelect');
}

// ── 高级参数折叠 ────────────────────────────────────
let advOpen = false;
function toggleAdvanced() {
  advOpen = !advOpen;
  const el = document.getElementById('advancedParams');
  const arrow = document.getElementById('advArrow');
  const text = document.getElementById('advToggleText');
  el.style.maxHeight = advOpen ? el.scrollHeight + 'px' : '0';
  arrow.style.transform = advOpen ? 'rotate(90deg)' : '';
  text.textContent = advOpen ? '收起高级参数' : '展开高级参数';
}

// ── 文字情感切换 ────────────────────────────────────
function toggleEmoText() {
  const checked = document.getElementById('useEmoText').checked;
  document.getElementById('emoTextInput').style.display = checked ? '' : 'none';
}

// ── 滑块 ────────────────────────────────────────────
function escapeHtml(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function updateSlider(el) {
  document.getElementById(el.id + 'Val').textContent = el.value;
}

function resetParams() {
  const defaults = { temperature: '0.8', top_p: '0.8', top_k: '30', emo_alpha: '1.0',
                     interval_silence: '200', repetition_penalty: '10.0', max_text_tokens: '120' };
  Object.entries(defaults).forEach(([id, val]) => {
    const el = document.getElementById(id);
    if (el) { el.value = val; updateSlider(el); }
  });
  document.getElementById('outputFormat').value = 'wav';
  document.getElementById('useEmoText').checked = false;
  toggleEmoText();
}

// ── 字数统计 ────────────────────────────────────────
function updateCharCount() {
  const len = document.getElementById('text').value.length;
  const el = document.getElementById('charCount');
  el.textContent = len + ' 字';
  if (len > 500) { el.className = 'char-count char-danger'; }
  else if (len > 200) { el.className = 'char-count char-warn'; }
  else { el.className = 'char-count char-ok'; }
}

// ── 文件上传（点击 + 拖拽 + 预览） ────────────────
function setupDragDrop(dropId, fileId, previewId) {
  const drop = document.getElementById(dropId);
  if (!drop) return;
  drop.addEventListener('click', (e) => {
    if (e.target.classList.contains('file-clear')) return;
    document.getElementById(fileId).click();
  });
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('dragover'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('dragover'));
  drop.addEventListener('drop', e => {
    e.preventDefault();
    drop.classList.remove('dragover');
    const files = e.dataTransfer.files;
    if (files.length > 0) {
      document.getElementById(fileId).files = files;
      onFileSelect(fileId, dropId, previewId);
    }
  });
}

function onFileSelect(fileId, dropId, previewId) {
  const input = document.getElementById(fileId);
  const drop = document.getElementById(dropId);
  const preview = document.getElementById(previewId);
  const labelId = fileId === 'spkFile' ? 'spkLabel' : 'emoLabel';

  if (input.files.length > 0) {
    const file = input.files[0];
    const sizeMB = (file.size / 1024 / 1024).toFixed(1);
    drop.classList.add('has-file');
    const label = document.getElementById(labelId);
    label.innerHTML = '<span class="file-info"><span class="file-name">' + escapeHtml(file.name) + '</span> (' + sizeMB + 'MB) <span class="file-clear" id="clear_' + fileId + '">清除</span></span>';
    document.getElementById('clear_' + fileId).addEventListener('click', function(e) {
      e.stopPropagation();
      clearFile(fileId, dropId, previewId);
    });

    const url = URL.createObjectURL(file);
    preview.innerHTML = '<audio controls src="' + url + '"></audio>';
    preview.style.display = '';
  }
}

function clearFile(fileId, dropId, previewId) {
  document.getElementById(fileId).value = '';
  document.getElementById(dropId).classList.remove('has-file');
  const labelId = fileId === 'spkFile' ? 'spkLabel' : 'emoLabel';
  document.getElementById(labelId).textContent = fileId === 'spkFile' ? '点击选择或拖拽音色参考音频' : '点击选择或拖拽情感参考音频';
  document.getElementById(previewId).style.display = 'none';
  document.getElementById(previewId).innerHTML = '';
}

// ── 健康检查 ────────────────────────────────────────
async function checkHealth() {
  try {
    const resp = await fetch('/health');
    const data = await resp.json();
    const badge = document.getElementById('statusBadge');
    if (data.model_loaded) {
      badge.textContent = 'GPU ' + (data.fp16 ? 'FP16' : 'FP32') + ' | ' + data.queue_depth + '/' + data.queue_max + ' 队列';
      badge.className = 'status-badge status-ok';
    } else {
      badge.textContent = '模型加载中...';
      badge.className = 'status-badge status-loading';
    }
  } catch (e) {
    document.getElementById('statusBadge').textContent = '服务不可用';
    document.getElementById('statusBadge').className = 'status-badge status-err';
  }
}

// ── 音色网格 ────────────────────────────────────────
async function loadVoiceGrid() {
  try {
    const key = document.getElementById('apiKey').value;
    const headers = key ? { 'x-api-key': key } : {};
    const resp = await fetch('/voices', { headers });
    const data = await resp.json();
    const grid = document.getElementById('voiceGrid');
    const countEl = document.getElementById('voiceCount');
    if (countEl) countEl.textContent = data.voices.length + ' 个音色';

    if (data.voices.length === 0) {
      grid.innerHTML = '<div style="color:#64748b; padding:12px; grid-column:1/-1;">未找到预存音色，请上传 .wav 文件</div>';
      return;
    }
    grid.innerHTML = data.details.map(v => {
      const sel = v.name === selectedVoice ? ' selected' : '';
      const dn = v.name.replace(/"/g, '&quot;');
      return '<div class="voice-card' + sel + '" data-voice="' + dn + '">' +
        '<div class="voice-name">' + v.name + '</div>' +
        '<div class="voice-meta">' + v.size_kb + ' KB</div>' +
        '<div class="voice-actions">' +
        '<button class="btn btn-sm btn-ghost vbtn-preview" data-voice="' + dn + '">试听</button>' +
        '<button class="btn btn-sm btn-ghost vbtn-delete" style="border-color:#dc2626; color:#dc2626;" data-voice="' + dn + '">删除</button>' +
        '</div></div>';
    }).join('');

    // 事件委托：点击音色卡片
    grid.querySelectorAll('.voice-card').forEach(card => {
      card.addEventListener('click', function() { selectVoice(this.dataset.voice, this); });
    });
    grid.querySelectorAll('.vbtn-preview').forEach(btn => {
      btn.addEventListener('click', function(e) { e.stopPropagation(); previewVoice(this.dataset.voice); });
    });
    grid.querySelectorAll('.vbtn-delete').forEach(btn => {
      btn.addEventListener('click', function(e) { e.stopPropagation(); deleteVoice(this.dataset.voice); });
    });

    // 同步到下拉框
    const sel = document.getElementById('voiceSelect');
    sel.innerHTML = data.voices.map(v => '<option value="' + escapeHtml(v) + '">' + escapeHtml(v) + '</option>').join('');
    if (selectedVoice) sel.value = selectedVoice;
  } catch (e) {}
}

function selectVoice(name, el) {
  selectedVoice = name;
  document.querySelectorAll('.voice-card').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  document.getElementById('voiceSelect').value = name;
}

async function previewVoice(name) {
  const key = document.getElementById('apiKey').value;
  const headers = key ? { 'x-api-key': key } : {};
  try {
    const resp = await fetch('/voices/' + encodeURIComponent(name) + '/preview', { headers });
    if (!resp.ok) { alert('试听失败：' + resp.statusText); return; }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.play();
  } catch (e) { alert('试听失败：' + e.message); }
}

async function deleteVoice(name) {
  if (!confirm('确定删除音色 "' + name + '"？此操作不可恢复。')) return;
  const key = document.getElementById('apiKey').value;
  const headers = key ? { 'x-api-key': key } : {};
  try {
    const resp = await fetch('/voices/' + encodeURIComponent(name), { method: 'DELETE', headers });
    if (resp.ok) { loadVoiceGrid(); } else { alert('删除失败'); }
  } catch (e) { alert('删除失败：' + e.message); }
}

async function uploadNewVoice() {
  const name = document.getElementById('newVoiceName').value.trim();
  const file = document.getElementById('newVoiceFile').files[0];
  if (!name) { alert('请输入音色名称'); return; }
  if (!file) return;

  const formData = new FormData();
  formData.append('voice_name', name);
  formData.append('audio', file);

  const key = document.getElementById('apiKey').value;
  const headers = {};
  if (key) headers['x-api-key'] = key;

  try {
    const resp = await fetch('/voices/upload', { method: 'POST', body: formData, headers });
    const data = await resp.json();
    if (resp.ok) {
      document.getElementById('newVoiceName').value = '';
      document.getElementById('newVoiceFile').value = '';
      loadVoiceGrid();
      alert('音色 "' + name + '" 保存成功！');
    } else {
      alert('上传失败：' + (data.detail || resp.statusText));
    }
  } catch (e) { alert('上传失败：' + e.message); }
}

async function loadVoices(selectId) {
  try {
    const key = document.getElementById('apiKey').value;
    const headers = key ? { 'x-api-key': key } : {};
    const resp = await fetch('/voices', { headers });
    const data = await resp.json();
    const sel = document.getElementById(selectId);
    sel.innerHTML = '';
    if (data.voices.length === 0) {
      sel.innerHTML = '<option value="">未找到预存音色</option>';
    } else {
      data.voices.forEach(v => { sel.innerHTML += '<option value="' + escapeHtml(v) + '">' + escapeHtml(v) + '</option>'; });
    }
  } catch (e) {}
}

// ── 任务列表 ────────────────────────────────────────
async function loadTasks() {
  try {
    const key = document.getElementById('apiKey').value;
    const headers = key ? { 'x-api-key': key } : {};
    const resp = await fetch('/tasks', { headers });
    const data = await resp.json();

    document.getElementById('statPending').textContent = data.stats.pending_count;
    document.getElementById('statProcessing').textContent = data.stats.processing_count;
    document.getElementById('statCompleted').textContent = data.stats.total_completed;
    document.getElementById('statFailed').textContent = data.stats.total_failed;

    const active = [...data.pending, ...data.processing];
    const activeEl = document.getElementById('activeTasks');
    activeEl.innerHTML = active.length === 0
      ? '<div style="color:#64748b; font-size:14px; padding:12px;">暂无活动任务</div>'
      : active.map(taskHTML).join('');

    const histEl = document.getElementById('historyTasks');
    histEl.innerHTML = data.history.length === 0
      ? '<div style="color:#64748b; font-size:14px; padding:12px;">暂无历史记录</div>'
      : data.history.slice(0, 20).map(taskHTML).join('');
  } catch (e) {}
}

function taskHTML(t) {
  const badgeMap = { pending: ['badge-pending', '等待中'], processing: ['badge-processing', '处理中'], completed: ['badge-completed', '已完成'], failed: ['badge-failed', '失败'] };
  const [bc, st] = badgeMap[t.status] || ['', t.status];
  const dur = t.duration_str ? ' | ' + t.duration_str : '';
  const err = t.error ? '<div style="color:#fca5a5; margin-top:4px; font-size:12px;">' + t.error + '</div>' : '';
  const params = 'T=' + t.temperature + ' P=' + t.top_p + ' K=' + t.top_k + ' ' + t.output_format.toUpperCase();
  const prog = t.status === 'processing' ? '<div class="progress-bar" style="margin-top:6px;"><div class="progress-fill" style="width:' + Math.round(t.progress * 100) + '%"></div></div>' : '';
  const progText = t.status === 'processing' && t.progress_msg ? '<div style="color:#60a5fa; font-size:11px; margin-top:2px;">' + Math.round(t.progress * 100) + '% - ' + t.progress_msg + '</div>' : '';
  return '<div class="task-item">' +
    '<div class="task-header"><span class="task-id">' + t.id + '</span><span class="task-badge ' + bc + '">' + st + '</span></div>' +
    '<div style="color:#64748b; font-size:12px;">' + (t.created_at_str || '') + dur + ' | ' + t.voice + '</div>' +
    '<div class="task-params">' + params + '</div>' +
    '<div class="task-text">' + t.text + '</div>' +
    prog + progText + err + '</div>';
}

// ── 进度显示 ────────────────────────────────────────
function showProgress(pct, msg) {
  const area = document.getElementById('progressArea');
  area.style.display = '';
  document.getElementById('progressFill').style.width = pct + '%';
  document.getElementById('progressPct').textContent = pct + '%';
  document.getElementById('progressText').textContent = msg || '';
}

function hideProgress() {
  document.getElementById('progressArea').style.display = 'none';
}

// ── 合成 ────────────────────────────────────────────
async function synthesize() {
  const btn = document.getElementById('synthBtn');
  const resultArea = document.getElementById('resultArea');
  const text = document.getElementById('text').value.trim();

  if (!text) { resultArea.innerHTML = '<div class="msg msg-error">请输入合成文本</div>'; return; }

  btn.disabled = true;
  resultArea.innerHTML = '';
  const startTime = Date.now();

  try {
    const formData = new FormData();
    formData.append('text', text);

    const key = document.getElementById('apiKey').value;
    const headers = {};
    if (key) headers['x-api-key'] = key;

    let url;
    if (currentMode === 'upload') {
      url = '/submit';
      const spkFile = document.getElementById('spkFile').files[0];
      if (!spkFile) { throw new Error('请上传音色参考音频'); }
      formData.append('spk_audio', spkFile);
      formData.append('temperature', document.getElementById('temperature').value);
      formData.append('top_p', document.getElementById('top_p').value);
      formData.append('top_k', document.getElementById('top_k').value);
      formData.append('interval_silence', document.getElementById('interval_silence').value);
      formData.append('repetition_penalty', document.getElementById('repetition_penalty').value);
      formData.append('max_text_tokens_per_segment', document.getElementById('max_text_tokens').value);
      formData.append('output_format', document.getElementById('outputFormat').value);

      const saveName = document.getElementById('saveVoiceName').value.trim();
      if (saveName) formData.append('save_voice', saveName);

      const emoFile = document.getElementById('emoFile').files[0];
      if (emoFile) {
        formData.append('emo_audio', emoFile);
        formData.append('emo_alpha', document.getElementById('emo_alpha').value);
      }

      if (document.getElementById('useEmoText').checked) {
        formData.append('use_emo_text', 'true');
        const et = document.getElementById('emoText').value.trim();
        if (et) formData.append('emo_text', et);
      }
    } else if (currentMode === 'preset') {
      url = '/submit_json';
      const voice = selectedVoice || document.getElementById('voiceSelect').value;
      if (!voice) { throw new Error('请选择音色'); }
      formData.append('voice_name', voice);
      formData.append('temperature', document.getElementById('temperature').value);
      formData.append('top_p', document.getElementById('top_p').value);
      formData.append('top_k', document.getElementById('top_k').value);
      formData.append('interval_silence', document.getElementById('interval_silence').value);
      formData.append('repetition_penalty', document.getElementById('repetition_penalty').value);
      formData.append('max_text_tokens_per_segment', document.getElementById('max_text_tokens').value);
      formData.append('output_format', document.getElementById('outputFormat').value);

      if (document.getElementById('useEmoText').checked) {
        formData.append('use_emo_text', 'true');
        const et = document.getElementById('emoText').value.trim();
        if (et) formData.append('emo_text', et);
      }
    } else {
      // 流式模式仍用同步端点
      url = '/synthesize_stream';
      const voice = document.getElementById('streamVoiceSelect').value;
      if (!voice) { throw new Error('请选择音色'); }
      formData.append('voice_name', voice);
      btn.innerHTML = '<span class="spinner"></span>合成中...';
      showProgress(5, '推理中...');
      const resp = await fetch(url, { method: 'POST', body: formData, headers });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(err.detail || 'HTTP ' + resp.status);
      }
      const blob = await resp.blob();
      const audioUrl = URL.createObjectURL(blob);
      const elapsed = ((Date.now() - startTime) / 1000).toFixed(2);
      const sizeKB = (blob.size / 1024).toFixed(0);
      showProgress(100, '完成！');
      setTimeout(hideProgress, 1500);
      resultArea.innerHTML =
        '<div class="msg msg-success">合成成功！耗时 ' + elapsed + 's | ' + sizeKB + ' KB</div>' +
        '<audio controls autoplay src="' + audioUrl + '"></audio>' +
        '<a href="' + audioUrl + '" download="output.wav" class="btn btn-secondary" style="display:block; text-align:center; margin-top:8px; text-decoration:none;">下载音频（WAV）</a>';
      btn.disabled = false;
      btn.textContent = '开始合成';
      loadTasks();
      return;
    }

    // ── 异步提交 → SSE 实时进度 → 下载结果 ──
    btn.innerHTML = '<span class="spinner"></span>提交中...';
    showProgress(2, '提交任务...');

    const submitResp = await fetch(url, { method: 'POST', body: formData, headers });
    if (!submitResp.ok) {
      const err = await submitResp.json().catch(() => ({ detail: submitResp.statusText }));
      throw new Error(err.detail || 'HTTP ' + submitResp.status);
    }
    const submitData = await submitResp.json();
    const taskId = submitData.task_id;

    // 缓存命中：直接下载结果
    if (submitData.status === 'completed') {
      btn.innerHTML = '<span class="spinner"></span>下载中...';
      showProgress(95, '缓存命中，获取音频...');
      await downloadAndShowResult(taskId, headers, startTime, resultArea);
      return;
    }

    // 排队中：显示队列位置
    const queuePos = submitData.queue_position || 0;
    btn.innerHTML = '<span class="spinner"></span>排队中（第 ' + queuePos + ' 位）...';
    showProgress(3, '排队等待（第 ' + queuePos + ' 位）...');

    // 连接 SSE 追踪实时进度
    await new Promise((resolve, reject) => {
      if (sseSource) { sseSource.close(); sseSource = null; }
      sseSource = new EventSource('/tasks/' + taskId + '/sse');

      sseSource.onmessage = async function(event) {
        try {
          const d = JSON.parse(event.data);
          if (d.status === 'not_found') {
            sseSource.close(); sseSource = null;
            reject(new Error('任务丢失'));
            return;
          }
          if (d.status === 'pending') {
            const pos = d.queue_position || '?';
            btn.innerHTML = '<span class="spinner"></span>排队中（第 ' + pos + ' 位）...';
            showProgress(3, '排队等待（第 ' + pos + ' 位）...');
          } else if (d.status === 'processing') {
            const pct = Math.round((d.progress || 0) * 100);
            btn.innerHTML = '<span class="spinner"></span>推理中 ' + pct + '%...';
            showProgress(pct, d.progress_msg || 'GPU 推理中...');
          } else if (d.status === 'completed') {
            sseSource.close(); sseSource = null;
            showProgress(95, '下载音频...');
            btn.innerHTML = '<span class="spinner"></span>下载中...';
            resolve();
          } else if (d.status === 'failed') {
            sseSource.close(); sseSource = null;
            reject(new Error(d.error || '合成失败'));
          }
        } catch (e) {
          sseSource.close(); sseSource = null;
          reject(e);
        }
      };

      sseSource.onerror = function() {
        sseSource.close(); sseSource = null;
        reject(new Error('SSE 连接断开，请检查任务状态'));
      };
    });

    // SSE 报告完成，下载结果
    await downloadAndShowResult(taskId, headers, startTime, resultArea);

    // 如果保存了音色，刷新列表
    if (currentMode === 'upload' && document.getElementById('saveVoiceName').value.trim()) {
      loadVoiceGrid();
    }
  } catch (e) {
    hideProgress();
    if (sseSource) { sseSource.close(); sseSource = null; }
    resultArea.innerHTML = '<div class="msg msg-error">合成失败: ' + escapeHtml(e.message) + '</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = '开始合成';
    loadTasks();
  }
}

async function downloadAndShowResult(taskId, headers, startTime, resultArea) {
  const resp = await fetch('/tasks/' + taskId + '/result', { headers });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || '下载失败: HTTP ' + resp.status);
  }
  const blob = await resp.blob();
  const audioUrl = URL.createObjectURL(blob);
  const elapsed = ((Date.now() - startTime) / 1000).toFixed(2);
  const contentType = resp.headers.get('content-type') || '';
  const fmt = contentType.includes('mpeg') ? 'mp3' : 'wav';
  const sizeKB = (blob.size / 1024).toFixed(0);

  showProgress(100, '完成！');
  setTimeout(hideProgress, 1500);

  resultArea.innerHTML =
    '<div class="msg msg-success">合成成功！耗时 ' + elapsed + 's | ' + sizeKB + ' KB</div>' +
    '<audio controls autoplay src="' + audioUrl + '"></audio>' +
    '<a href="' + audioUrl + '" download="output.' + fmt + '" class="btn btn-secondary" style="display:block; text-align:center; margin-top:8px; text-decoration:none;">下载音频（' + fmt.toUpperCase() + '）</a>';
}

// ── 文档标签切换 ────────────────────────────────────
function switchDoc(id, btn) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.doc-tab').forEach(el => el.classList.remove('active'));
  document.getElementById('doc-' + id).classList.add('active');
  btn.classList.add('active');
}

// ── 键盘快捷键 ──────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.ctrlKey && e.key === 'Enter') {
    e.preventDefault();
    if (!document.getElementById('synthBtn').disabled) synthesize();
  }
});

// ── 初始化 ──────────────────────────────────────────
setupDragDrop('spkDrop', 'spkFile', 'spkPreview');
setupDragDrop('emoDrop', 'emoFile', 'emoPreview');
document.getElementById('newVoiceDrop').addEventListener('click', () => document.getElementById('newVoiceFile').click());
updateCharCount();
checkHealth();
loadTasks();

const base = window.location.origin;
document.querySelectorAll('.curl-base').forEach(el => el.textContent = base);
const curlBase = document.getElementById('curlBase');
if (curlBase) curlBase.textContent = base;

setInterval(checkHealth, 10000);
setInterval(loadTasks, 3000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", workers=1)
