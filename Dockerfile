# ============================================================
# IndexTTS2 Docker 镜像 — 最终确定版
# 策略：
#   1. 基础镜像 Python 3.10 + CUDA 12.1
#   2. PyTorch 官方 cu128 索引装 torch 三件套
#   3. 直接列出 pyproject.toml 中的全部依赖安装（跳过 torch 系列）
#   4. 源码 COPY 到 /app/ 直接 import（不做 pip install .）
# ============================================================

FROM pytorch/pytorch:2.3.1-cuda12.1-cudnn8-runtime

WORKDIR /app

# ── 1. 系统依赖（阿里云 APT 源）──────────────────────────────
RUN sed -i 's|http://archive.ubuntu.com|https://mirrors.aliyun.com|g' /etc/apt/sources.list && \
    sed -i 's|http://security.ubuntu.com|https://mirrors.aliyun.com|g' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
    git ffmpeg libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

# ── 2. 复制源码 ────────────────────────────────────────────────
COPY indextts-src/ /app/

# ── 3. PyTorch CUDA 三件套（官方 cu128 索引）───────────────────
# 已缓存，重建秒过
RUN pip install --timeout=600 --retries=5 \
        "torch==2.8.*" "torchaudio==2.8.*" "torchvision==0.23.*" \
        --index-url https://download.pytorch.org/whl/cu128 && \
    python -c "import torch; print(f'torch {torch.__version__}, CUDA={torch.cuda.is_available()}')"

# ── 4. 安装 indextts 全部依赖（从阿里云 PyPI）─────────────────
# 直接列出 pyproject.toml 中的 dependencies，跳过 torch/torchaudio
# 这样不需要 hatchling，也不会覆盖 CUDA 版 torch
RUN pip install --timeout=600 --retries=5 \
        -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com \
        "accelerate==1.8.1" \
        "cn2an==0.5.22" \
        "cython==3.0.7" \
        "descript-audiotools==0.7.2" \
        "einops>=0.8.1" \
        "ffmpeg-python==0.2.0" \
        "g2p-en==2.1.0" \
        "jieba==0.42.1" \
        "json5==0.10.0" \
        "keras==2.9.0" \
        "librosa==0.10.2.post1" \
        "matplotlib==3.8.2" \
        "modelscope==1.27.0" \
        "munch==4.0.0" \
        "numba==0.58.1" \
        "numpy==1.26.2" \
        "omegaconf>=2.3.0" \
        "opencv-python==4.9.0.80" \
        "pandas==2.3.2" \
        "safetensors==0.5.2" \
        "sentencepiece>=0.2.1" \
        "tensorboard==2.9.1" \
        "textstat>=0.7.10" \
        "tokenizers==0.21.0" \
        "tqdm>=4.67.1" \
        "transformers==4.52.1" \
        "WeTextProcessing" && \
    python -c "\
import librosa; print(f'librosa {librosa.__version__}'); \
import transformers; print(f'transformers {transformers.__version__}'); \
import torch; print(f'torch {torch.__version__}'); \
import torchvision; print(f'torchvision {torchvision.__version__}'); \
print('=== ALL CORE DEPS OK ===')"

# ── 5. API 服务依赖 ──────────────────────────────────────────
RUN pip install --timeout=600 \
        fastapi "uvicorn[standard]" python-multipart aiofiles \
        -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

# ── 6. API 服务代码 + 运行配置 ────────────────────────────────
COPY api_server.py .
RUN mkdir -p /tmp/tts_cache

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENV CUDA_VISIBLE_DEVICES=0
EXPOSE 8000
CMD ["python", "api_server.py"]
