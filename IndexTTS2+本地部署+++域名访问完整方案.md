# IndexTTS2 本地部署 + 域名访问完整方案

> **架构：自有域名 + Tailscale + Nginx + Docker + IndexTTS2**
>
> 版本：v1.3 | 日期：2026-03-11 | 适用于数字人短视频制作管线 TTS 环节
>
> **v1.1 新增**：Windows GPU 机器部署附录（WSL2 + Docker Desktop 方案）
> **v1.2 新增**：稳定性优化与项目集成指南（附录 B）
> **v1.3 新增**：进阶优化清单，含必做 Bug 修复与延迟对比参考（附录 C）

---

## 目录

1. [架构总览](#1-架构总览)
2. [前置要求](#2-前置要求)
3. [Step 1：本地 GPU 机器 — 安装 NVIDIA Container Toolkit](#3-step-1本地-gpu-机器--安装-nvidia-container-toolkit)
4. [Step 2：本地 GPU 机器 — 构建 IndexTTS2 Docker 镜像](#4-step-2本地-gpu-机器--构建-indextts2-docker-镜像)
5. [Step 3：本地 GPU 机器 — 编写 FastAPI 服务](#5-step-3本地-gpu-机器--编写-fastapi-服务)
6. [Step 4：两端 — 安装并配置 Tailscale](#6-step-4两端--安装并配置-tailscale)
7. [Step 5：域名 DNS 解析配置](#7-step-5域名-dns-解析配置)
8. [Step 6：云端服务器 — Nginx 反向代理 + HTTPS](#8-step-6云端服务器--nginx-反向代理--https)
9. [Step 7：Docker Compose 一键编排](#9-step-7docker-compose-一键编排)
10. [Step 8：验证与测试](#10-step-8验证与测试)
11. [生产加固](#11-生产加固)
12. [故障排查](#12-故障排查)
13. [架构演进建议](#13-架构演进建议)
14. [附录 A：Windows GPU 机器部署指南](#14-附录-a-windows-gpu-机器部署指南)
15. [附录 B：稳定性优化与项目集成指南](#15-附录-b-稳定性优化与项目集成指南)
16. [附录 C：进阶优化清单（已分析代码，按优先级排列）](#16-附录-c-进阶优化清单已分析代码按优先级排列)

---

## 1. 架构总览

### 1.1 网络拓扑

```
互联网请求
    │
    ▼
tts.yourdomain.com（DNS A 记录 → 云端公网 IP）
    │
    ▼
┌───────────────────────────────────────────┐
│  云端服务器                                │
│  Nginx (:443) ── 反向代理 ── HTTPS 终结    │
│       │                                   │
│       ▼                                   │
│  proxy_pass http://100.x.x.x:8000        │
│       │  （Tailscale 虚拟内网 IP）          │
└───────┼───────────────────────────────────┘
        │  Tailscale WireGuard 加密隧道
┌───────┼───────────────────────────────────┐
│  本地 GPU 机器                             │
│       ▼                                   │
│  Docker Container (--gpus all)            │
│  ├─ IndexTTS2 模型                        │
│  └─ FastAPI 服务 (:8000)                  │
└───────────────────────────────────────────┘
```

### 1.2 组件职责

| 组件                    | 部署位置                     | 职责                             |
| ----------------------- | ---------------------------- | -------------------------------- |
| IndexTTS2 + FastAPI     | 本地 GPU 机器（Docker 容器） | 语音合成模型推理 + HTTP API 服务 |
| Docker + NVIDIA Toolkit | 本地 GPU 机器                | 容器运行时 + GPU 透传            |
| Tailscale               | 两端均安装                   | WireGuard 加密隧道，组虚拟内网   |
| Nginx                   | 云端服务器                   | HTTPS 终结 + 反向代理 + 域名绑定 |
| 域名 DNS                | 域名服务商                   | A 记录指向云端公网 IP            |

### 1.3 数据流

```
1. 云端业务项目发起请求 → https://tts.yourdomain.com/synthesize
2. DNS 解析到云端服务器公网 IP
3. Nginx 接收请求，终结 HTTPS，转发到 Tailscale 内网 IP
4. 请求通过 WireGuard 隧道到达本地 GPU 机器
5. Docker 容器内 FastAPI 接收请求，调用 IndexTTS2 推理
6. 生成音频通过原路返回
```

---

## 2. 前置要求

### 2.1 本地 GPU 机器

| 项目                     | 最低要求                    | 推荐配置                |
| ------------------------ | --------------------------- | ----------------------- |
| 显卡                     | NVIDIA 6GB 显存             | RTX 3090 / 4090（24GB） |
| 内存                     | 16GB                        | 32GB                    |
| 硬盘                     | 50GB 可用                   | 100GB SSD               |
| 系统                     | Ubuntu 22.04 / Windows WSL2 | Ubuntu 22.04 LTS        |
| CUDA                     | 11.8+                       | 12.1+                   |
| Docker                   | 24.0+                       | 最新稳定版              |
| NVIDIA Container Toolkit | Linux 必装 / Windows 不需要 | Linux 必装（Windows 见附录 A）|

### 2.2 云端服务器

| 项目     | 要求                              |
| -------- | --------------------------------- |
| 系统     | Ubuntu 22.04 / Debian 12          |
| 公网 IP  | 固定 IP（用于域名解析）           |
| 配置     | 1 核 1GB 内存即可（仅做代理转发） |
| 开放端口 | 80、443                           |

### 2.3 其他

| 项目           | 要求                                      |
| -------------- | ----------------------------------------- |
| 域名           | 已注册，可管理 DNS 记录                   |
| Tailscale 账号 | 免费注册 https://tailscale.com            |
| SSL 证书       | Let's Encrypt 免费获取（或用 Cloudflare） |

---

## 3. Step 1：本地 GPU 机器 — 安装 NVIDIA Container Toolkit

### 3.1 安装 Docker（如未安装）

```bash
# 安装 Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

### 3.2 安装 NVIDIA Container Toolkit

```bash
# 添加 NVIDIA 仓库
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

# 安装
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

# 配置 Docker 运行时
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### 3.3 验证 GPU 可用

```bash
docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
```

看到显卡信息即安装成功。

---

## 4. Step 2：本地 GPU 机器 — 构建 IndexTTS2 Docker 镜像

### 4.1 创建项目目录

```bash
mkdir -p ~/indextts2-service && cd ~/indextts2-service
```

### 4.2 下载模型权重（宿主机下载，后续挂载进容器）

```bash
# 方式一：ModelScope（国内推荐，速度最快）
pip install modelscope
modelscope download --model IndexTeam/IndexTTS-2 --local_dir ./checkpoints

# 方式二：git lfs 直接克隆（ModelScope 超时时的备用方案）
sudo apt install git-lfs
git lfs install
git clone https://www.modelscope.cn/IndexTeam/IndexTTS-2.git ./checkpoints

# 方式三：HuggingFace（需要代理）
# pip install huggingface_hub
# huggingface-cli download IndexTeam/IndexTTS-2 --local-dir ./checkpoints
```

> **下载失败排查**：ModelScope 下载中断可加参数 `--revision master` 重试；git lfs clone 失败可先 `git clone --no-checkout` 再 `git lfs pull`。

### 4.3 编写 Dockerfile

```bash
cat > Dockerfile << 'EOF'
FROM pytorch/pytorch:2.3.1-cuda12.1-cudnn9-runtime

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y \
    git git-lfs ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# 克隆项目源码
RUN git clone https://github.com/index-tts/index-tts.git . \
    && git lfs pull

# 安装 Python 依赖
RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir uv \
    && uv sync --all-extras

# 安装 API 服务依赖
RUN pip install --no-cache-dir fastapi uvicorn python-multipart aiofiles

# 复制 API 服务代码
COPY api_server.py .

# 设置环境变量，只使用一张 GPU
ENV CUDA_VISIBLE_DEVICES=0

EXPOSE 8000

CMD ["python", "api_server.py"]
EOF
```

> **提示**：模型权重不要打包进镜像，通过 volume 挂载，这样镜像体积小、模型可独立更新。

---

## 5. Step 3：本地 GPU 机器 — 编写 FastAPI 服务

### 5.1 创建 api_server.py

```bash
cat > api_server.py << 'PYEOF'
import os
import uuid
import tempfile
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

# ── 配置 ──
API_KEY = os.environ.get("TTS_API_KEY", "your-secret-key-here")
MODEL_DIR = os.environ.get("MODEL_DIR", "./checkpoints")
USE_FP16 = os.environ.get("USE_FP16", "true").lower() == "true"
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tts-api")

tts = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """模型在服务启动时加载一次"""
    global tts
    from indextts.infer_v2 import IndexTTS2
    logger.info("Loading IndexTTS2 model...")
    tts = IndexTTS2(
        cfg_path=os.path.join(MODEL_DIR, "config.yaml"),
        model_dir=MODEL_DIR,
        is_fp16=USE_FP16,
        use_cuda_kernel=False
    )
    logger.info("Model loaded successfully.")
    yield
    logger.info("Shutting down.")

app = FastAPI(title="IndexTTS2 API", lifespan=lifespan)


def verify_key(x_api_key: str = Header(None)):
    """简单的 API Key 认证"""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")


# ── 健康检查 ──
@app.get("/health")
async def health():
    return {"status": "ok", "model_loaded": tts is not None}


# ── 基础合成接口 ──
@app.post("/synthesize")
async def synthesize(
    text: str = Form(..., description="合成文本"),
    spk_audio: UploadFile = File(..., description="音色参考音频"),
    emo_audio: UploadFile = File(None, description="情感参考音频（可选）"),
    emo_alpha: float = Form(1.0, description="情感权重 0.0-1.0"),
    temperature: float = Form(0.8, description="采样温度"),
    top_p: float = Form(0.8, description="Top-P 采样"),
    top_k: int = Form(30, description="Top-K 采样"),
    x_api_key: str = Header(None),
):
    verify_key(x_api_key)

    if tts is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    # 保存上传的音频到临时文件
    spk_path = os.path.join(tempfile.gettempdir(), f"spk_{uuid.uuid4().hex}.wav")
    with open(spk_path, "wb") as f:
        f.write(await spk_audio.read())

    emo_path = None
    if emo_audio:
        emo_path = os.path.join(tempfile.gettempdir(), f"emo_{uuid.uuid4().hex}.wav")
        with open(emo_path, "wb") as f:
            f.write(await emo_audio.read())

    # 生成音频
    output_path = os.path.join(tempfile.gettempdir(), f"out_{uuid.uuid4().hex}.wav")

    try:
        kwargs = {
            "spk_audio_prompt": spk_path,
            "text": text,
            "output_path": output_path,
            "do_sample": True,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k if top_k > 0 else None,
        }
        if emo_path:
            kwargs["emo_audio_prompt"] = emo_path
            kwargs["emo_alpha"] = emo_alpha

        tts.infer(**kwargs)

        return FileResponse(
            output_path,
            media_type="audio/wav",
            filename="synthesized.wav"
        )
    except Exception as e:
        logger.error(f"Synthesis failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # 清理临时文件
        for p in [spk_path, emo_path]:
            if p and os.path.exists(p):
                os.remove(p)


# ── JSON 文本合成接口（使用预存音色） ──
@app.post("/synthesize_json")
async def synthesize_json(
    text: str = Form(...),
    voice_name: str = Form("default", description="预存音色名称"),
    x_api_key: str = Header(None),
):
    verify_key(x_api_key)

    voice_dir = os.path.join(MODEL_DIR, "voices")
    voice_path = os.path.join(voice_dir, f"{voice_name}.wav")

    if not os.path.exists(voice_path):
        raise HTTPException(status_code=404, detail=f"Voice '{voice_name}' not found")

    output_path = os.path.join(tempfile.gettempdir(), f"out_{uuid.uuid4().hex}.wav")

    try:
        tts.infer(
            spk_audio_prompt=voice_path,
            text=text,
            output_path=output_path,
        )
        return FileResponse(output_path, media_type="audio/wav", filename="output.wav")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
PYEOF
```

### 5.2 预存常用音色

```bash
# 在 checkpoints 目录下创建 voices 子目录，放入常用参考音频
mkdir -p checkpoints/voices
cp /path/to/your/voice.wav checkpoints/voices/default.wav
cp /path/to/narrator.wav checkpoints/voices/narrator.wav
```

这样云端调用时只需传 `voice_name=narrator`，不用每次上传音频。

---

## 6. Step 4：两端 — 安装并配置 Tailscale

### 6.1 本地 GPU 机器

```bash
# 安装 Tailscale
curl -fsSL https://tailscale.com/install.sh | sh

# 启动并登录（会弹出浏览器认证链接）
sudo tailscale up

# 查看分配的虚拟 IP
tailscale ip -4
# 输出类似：100.100.1.10

# 设置开机自启
sudo systemctl enable tailscaled
```

### 6.2 云端服务器

```bash
# 安装 Tailscale
curl -fsSL https://tailscale.com/install.sh | sh

# 启动并登录（同一个 Tailscale 账号）
sudo tailscale up

# 查看虚拟 IP
tailscale ip -4
# 输出类似：100.100.1.20

# 设置开机自启
sudo systemctl enable tailscaled
```

### 6.3 验证连通性

```bash
# 在云端服务器上 ping 本地机器
ping 100.100.1.10

# 查看 Tailscale 网络中的所有设备
tailscale status
```

### 6.4 开启 MagicDNS（可选）

在 Tailscale 管理后台 → DNS 设置中开启 MagicDNS，可以用主机名互相访问：

```bash
# 云端 ping 本地（假设本地 hostname 是 gpu-workstation）
ping gpu-workstation
```

> **重要**：记录本地 GPU 机器的 Tailscale IP（如 `100.100.1.10`），后面 Nginx 配置要用。

---

## 7. Step 5：域名 DNS 解析配置

在域名服务商控制台（Cloudflare / 阿里云 DNS / DNSPod 等）添加一条 A 记录：

| 类型 | 主机记录 | 记录值            | TTL |
| ---- | -------- | ----------------- | --- |
| A    | tts      | 云端服务器公网 IP | 600 |

配置后 `tts.yourdomain.com` 将解析到云端服务器。

```bash
# 验证 DNS 生效
dig tts.yourdomain.com
# 或
nslookup tts.yourdomain.com
```

---

## 8. Step 6：云端服务器 — Nginx 反向代理 + HTTPS

### 8.1 安装 Nginx + Certbot

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

### 8.2 申请 SSL 证书（Let's Encrypt 免费证书）

```bash
# 先配置一个简单的 HTTP server 块让 certbot 验证
sudo tee /etc/nginx/conf.d/tts.conf << 'EOF'
server {
    listen 80;
    server_name tts.yourdomain.com;

    location / {
        return 200 'ok';
    }
}
EOF

sudo nginx -t && sudo systemctl reload nginx

# 申请证书
sudo certbot --nginx -d tts.yourdomain.com

# 证书会自动安装，certbot 会自动配置续签定时任务
```

### 8.3 配置 Nginx 反向代理

```bash
sudo tee /etc/nginx/conf.d/tts.conf << 'NGINXEOF'
# ── IndexTTS2 反向代理配置 ──

upstream tts_backend {
    server 100.100.1.10:8000;    # ← 改为你本地机器的 Tailscale IP
    keepalive 8;                 # 保持长连接
}

# HTTPS 主配置
server {
    listen 443 ssl http2;
    server_name tts.yourdomain.com;

    # SSL 证书（certbot 自动生成）
    ssl_certificate     /etc/letsencrypt/live/tts.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/tts.yourdomain.com/privkey.pem;

    # SSL 安全配置
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # 请求体大小（上传音频文件）
    client_max_body_size 50m;

    # 超时设置（TTS 生成可能较慢）
    proxy_connect_timeout 30s;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;

    # 日志
    access_log /var/log/nginx/tts_access.log;
    error_log  /var/log/nginx/tts_error.log;

    # ── 健康检查（无需认证） ──
    location /health {
        proxy_pass http://tts_backend;
        proxy_set_header Host $host;
    }

    # ── API 接口（需要 API Key 认证） ──
    location / {
        # IP 白名单：只允许 Tailscale 内网 + 云端公网 IP（强烈建议开启，见附录 C.3.2）
        # allow 100.0.0.0/8;
        # allow <云端服务器公网IP>/32;
        # deny all;

        # API Key 验证
        if ($http_x_api_key = "") {
            return 403 '{"error": "Missing API Key"}';
        }

        proxy_pass http://tts_backend;

        # 传递真实客户端信息
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 长连接优化
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }
}

# HTTP → HTTPS 自动跳转
server {
    listen 80;
    server_name tts.yourdomain.com;
    return 301 https://$host$request_uri;
}
NGINXEOF
```

### 8.4 启用配置

```bash
# 测试配置语法
sudo nginx -t

# 重载 Nginx
sudo systemctl reload nginx

# 设置开机自启
sudo systemctl enable nginx
```

---

## 9. Step 7：Docker Compose 一键编排

回到本地 GPU 机器，创建 `docker-compose.yml` 统一管理服务。

### 9.1 docker-compose.yml

```yaml
# ~/indextts2-service/docker-compose.yml

services:
  tts:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: indextts2-api
    restart: always # 自动重启
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1 # 使用 1 张 GPU
              capabilities: [gpu]
    ports:
      - "8000:8000"
    volumes:
      - ./checkpoints:/app/checkpoints # 模型权重挂载
      - ./logs:/app/logs # 日志挂载
    environment:
      - TTS_API_KEY=your-secret-key-here # API 密钥
      - USE_FP16=true # 开启半精度
      - CUDA_VISIBLE_DEVICES=0 # 指定 GPU
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s # 模型加载需要时间
    logging:
      driver: "json-file"
      options:
        max-size: "50m"
        max-file: "3"
```

### 9.2 启动服务

```bash
cd ~/indextts2-service

# 构建并启动
docker compose up -d --build

# 查看日志
docker compose logs -f tts

# 查看状态
docker compose ps
```

### 9.3 常用运维命令

```bash
# 停止服务
docker compose down

# 重启服务
docker compose restart tts

# 更新代码后重新构建
docker compose up -d --build

# 查看 GPU 使用情况
docker exec indextts2-api nvidia-smi

# 进入容器调试
docker exec -it indextts2-api bash
```

---

## 10. Step 8：验证与测试

### 10.1 本地直接测试

```bash
# 在本地 GPU 机器上
curl -X POST http://localhost:8000/health
# 期望返回：{"status":"ok","model_loaded":true}
```

### 10.2 Tailscale 内网测试

```bash
# 在云端服务器上，通过 Tailscale IP 调用
curl -X POST http://100.100.1.10:8000/health
# 期望返回：{"status":"ok","model_loaded":true}
```

### 10.3 域名 HTTPS 测试

```bash
# 在任意位置，通过域名调用
curl -X POST https://tts.yourdomain.com/health
# 期望返回：{"status":"ok","model_loaded":true}
```

### 10.4 完整合成测试

```bash
# 上传音频 + 文本，生成语音
curl -X POST https://tts.yourdomain.com/synthesize \
  -H "X-API-Key: your-secret-key-here" \
  -F "text=你好世界，这是一段测试语音。" \
  -F "spk_audio=@/path/to/reference_voice.wav" \
  -F "temperature=0.8" \
  -F "top_p=0.8" \
  --output test_output.wav

# 播放验证
ffplay test_output.wav
```

### 10.5 Python 调用示例（云端业务项目集成）

```python
import requests

TTS_URL = "https://tts.yourdomain.com"
API_KEY = "your-secret-key-here"

def synthesize_speech(text: str, voice_name: str = "default") -> bytes:
    """调用 TTS 服务合成语音"""
    response = requests.post(
        f"{TTS_URL}/synthesize_json",
        headers={"X-API-Key": API_KEY},
        data={"text": text, "voice_name": voice_name},
        timeout=120
    )
    response.raise_for_status()
    return response.content

def synthesize_with_audio(text: str, voice_path: str, emo_path: str = None) -> bytes:
    """上传参考音频合成语音"""
    files = {"spk_audio": open(voice_path, "rb")}
    if emo_path:
        files["emo_audio"] = open(emo_path, "rb")

    response = requests.post(
        f"{TTS_URL}/synthesize",
        headers={"X-API-Key": API_KEY},
        files=files,
        data={"text": text, "temperature": 0.8, "top_p": 0.8},
        timeout=120
    )
    response.raise_for_status()
    return response.content

# ── 使用示例 ──
audio = synthesize_speech("大家好，欢迎来到我的频道。", voice_name="narrator")
with open("output.wav", "wb") as f:
    f.write(audio)
```

---

## 11. 生产加固

### 11.1 Tailscale 稳定性加固

```bash
# 本地机器：确保 Tailscale 开机自启 + 断线重连
sudo systemctl enable tailscaled
sudo systemctl start tailscaled

# 检查 Tailscale 连接状态的脚本
cat > ~/check_tailscale.sh << 'EOF'
#!/bin/bash
if ! tailscale status > /dev/null 2>&1; then
    echo "$(date) Tailscale down, restarting..." >> /var/log/tailscale_watchdog.log
    sudo systemctl restart tailscaled
    sleep 5
    sudo tailscale up
fi
EOF
chmod +x ~/check_tailscale.sh

# 加入 crontab 每 5 分钟检查一次
(crontab -l 2>/dev/null; echo "*/5 * * * * ~/check_tailscale.sh") | crontab -
```

### 11.2 Docker 服务稳定性

```bash
# docker-compose.yml 中已设置 restart: always
# 额外添加 systemd 守护，确保 Docker Compose 服务随系统启动

sudo tee /etc/systemd/system/indextts2.service << EOF
[Unit]
Description=IndexTTS2 TTS Service
After=docker.service tailscaled.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/$USER/indextts2-service
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
User=$USER

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable indextts2
```

### 11.3 Nginx 监控（云端）

```bash
# 添加 Nginx 状态页面用于监控
sudo tee /etc/nginx/conf.d/status.conf << 'EOF'
server {
    listen 127.0.0.1:8080;
    location /nginx_status {
        stub_status;
    }
}
EOF

sudo nginx -t && sudo systemctl reload nginx

# 检测 TTS 后端是否可达的脚本
cat > ~/check_tts.sh << 'BASHEOF'
#!/bin/bash
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  --max-time 10 \
  http://100.100.1.10:8000/health)

if [ "$HTTP_CODE" != "200" ]; then
    echo "$(date) TTS backend unreachable (HTTP $HTTP_CODE)" >> /var/log/tts_watchdog.log
    # 可以在这里发送告警（邮件、钉钉、企业微信等）
fi
BASHEOF
chmod +x ~/check_tts.sh

# 每 2 分钟检查
(crontab -l 2>/dev/null; echo "*/2 * * * * ~/check_tts.sh") | crontab -
```

### 11.4 API Key 安全建议

| 措施         | 说明                                                 |
| ------------ | ---------------------------------------------------- |
| 环境变量存储 | API Key 不要硬编码在代码中，用 `.env` 文件或环境变量 |
| 定期轮换     | 建议每月更换一次 API Key                             |
| 限制来源 IP  | Nginx 中限制只允许 Tailscale 网段访问（见附录 C.3.2，**强烈建议**） |
| HTTPS 强制   | 已配置 HTTP→HTTPS 跳转，密钥在传输中加密             |
| 速率限制     | Nginx 双层限频（按 IP + 按 Key），见附录 C.3.1       |

---

## 12. 故障排查

### 12.1 常见问题速查表

| 症状                              | 可能原因                 | 解决方法                                |
| --------------------------------- | ------------------------ | --------------------------------------- |
| 云端无法 ping 通本地 Tailscale IP | Tailscale 未启动或未登录 | `sudo tailscale up` 重新连接            |
| 域名无法解析                      | DNS 记录未生效           | `dig tts.yourdomain.com` 检查，等待 TTL |
| Nginx 502 Bad Gateway             | 本地 TTS 服务未启动      | `docker compose ps` 检查容器状态        |
| Nginx 504 Gateway Timeout         | TTS 推理超时             | 增大 Nginx `proxy_read_timeout`         |
| CUDA out of memory                | 显存不足                 | 开启 FP16、减少 beam 数量               |
| 生成音频质量差                    | 参考音频问题             | 使用 3-10 秒清晰无噪音的参考音频        |
| API 返回 403                      | API Key 不匹配           | 检查请求头 `X-API-Key`                  |

### 12.2 逐层排查流程

```
1. 本地容器是否运行？
   docker compose ps
   docker compose logs tts

2. 本地 API 是否可访问？
   curl http://localhost:8000/health

3. Tailscale 隧道是否通？
   （在云端）ping 100.100.1.10
   （在云端）curl http://100.100.1.10:8000/health

4. Nginx 是否正常代理？
   sudo nginx -t
   tail -f /var/log/nginx/tts_error.log

5. 域名是否解析正确？
   dig tts.yourdomain.com

6. HTTPS 证书是否有效？
   curl -vI https://tts.yourdomain.com/health
```

### 12.3 日志位置

| 组件           | 日志位置                        |
| -------------- | ------------------------------- |
| Docker 容器    | `docker compose logs tts`       |
| Nginx 访问日志 | `/var/log/nginx/tts_access.log` |
| Nginx 错误日志 | `/var/log/nginx/tts_error.log`  |
| Tailscale      | `journalctl -u tailscaled`      |
| 系统级服务     | `journalctl -u indextts2`       |

---

## 13. 架构演进建议

### 13.1 当前方案的适用边界

| 场景                | 是否适用                         |
| ------------------- | -------------------------------- |
| 日产 10-50 条短视频 | 完全适用                         |
| 日产 100+ 条        | 可能需要优化（排队 / 多 Worker） |
| 实时对话 / 直播     | 延迟可能偏高（穿透 + 推理）      |
| 多人同时请求        | 需排队（单 GPU 串行推理）        |

### 13.2 后续扩展路径

**短期优化**：在 FastAPI 层添加任务队列（Celery / asyncio.Queue），支持并发请求排队处理，避免超时。

**中期扩展**：如果产量上升，将 IndexTTS2 Docker 容器迁移到 AutoDL 等云端 GPU 平台，直接内网调用，去掉 Tailscale 层。Nginx 配置不用改，只改 upstream 地址。

**长期方案**：对接 SiliconFlow 等平台的 IndexTTS2 商业 API，业务代码只需改一个 URL，无需自行运维模型。

### 13.3 关键设计：一个 URL 不变

无论底层方案如何切换，你的业务代码始终调用：

```
https://tts.yourdomain.com/synthesize
```

迁移时只改 Nginx 的 `upstream` 即可，业务代码零修改。

---

## 附录：完整文件清单

```
~/indextts2-service/
├── Dockerfile                # Docker 镜像定义
├── docker-compose.yml        # 服务编排
├── api_server.py             # FastAPI 服务代码
├── checkpoints/              # 模型权重（volume 挂载）
│   ├── config.yaml
│   ├── gpt.pth
│   ├── s2mel.pth
│   ├── bpe.model
│   ├── wav2vec2bert_stats.pt
│   └── voices/              # 预存音色
│       ├── default.wav
│       └── narrator.wav
└── logs/                     # 日志目录
```

```
云端服务器：
/etc/nginx/conf.d/tts.conf   # Nginx 反向代理配置
/etc/letsencrypt/             # SSL 证书（certbot 管理）
```

---

> **总结**：本方案通过 Docker 容器化 IndexTTS2、Tailscale 加密隧道、Nginx HTTPS 反向代理三层架构，实现了本地 GPU 推理 + 域名级远程调用。核心优势是环境可迁移、网络稳定、业务解耦。当需要扩展时，只需调整 Nginx upstream 地址，业务代码无需任何修改。

---

## 14. 附录 A：Windows GPU 机器部署指南

> 本附录适用于 GPU 机器为 Windows 系统（Windows 10/11）的场景。
> Docker 容器、FastAPI 服务、docker-compose.yml 等代码文件与主文档完全相同，只需替换本附录中的 Step 1 操作。

### A.1 前置条件确认

| 项目 | 要求 | 备注 |
|------|------|------|
| Windows 版本 | Windows 10 21H2+ 或 Windows 11 | WSL2 需要此版本以上 |
| NVIDIA 驱动 | 526.98+ | 在 Windows 侧安装，WSL2 内自动生效 |
| 显卡 | RTX 3070+（12GB 以上显存推荐） | 6GB 可运行，FP16 模式 |
| 内存 | 16GB+ | WSL2 会占用部分内存 |
| 硬盘 | 100GB+ 可用空间 | 模型约 10GB，Docker 镜像约 15GB |

### A.2 Step 1A：启用 WSL2

以**管理员身份**打开 PowerShell，依次执行：

```powershell
# 启用 WSL 和虚拟机平台
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart

# 重启电脑
Restart-Computer
```

重启后，继续在 PowerShell 中执行：

```powershell
# 设置 WSL2 为默认版本
wsl --set-default-version 2

# 安装 Ubuntu 22.04
wsl --install -d Ubuntu-22.04

# 验证安装
wsl --list --verbose
# 应显示：Ubuntu-22.04  Running  2
```

> **首次启动 Ubuntu**：会提示创建 Linux 用户名和密码，设置完成后即进入 WSL2 终端。

### A.3 Step 1B：安装 NVIDIA WSL2 驱动

**重要**：WSL2 中不要安装 CUDA 驱动，只需在 **Windows 侧**安装正确的驱动即可，WSL2 会自动识别 GPU。

1. 前往 [NVIDIA 驱动下载页](https://www.nvidia.com/Download/index.aspx)
2. 选择你的显卡型号，下载 **Game Ready Driver 或 Studio Driver**，版本 **526.98 以上**
3. 正常安装（双击 exe，按提示操作）
4. 安装完成后，在 WSL2 Ubuntu 终端验证：

```bash
# 在 WSL2 Ubuntu 终端内执行
nvidia-smi
# 应显示显卡信息，包括驱动版本和 CUDA 版本
```

### A.4 Step 1C：安装 Docker Desktop（启用 WSL2 后端）

1. 下载 [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)
2. 安装时勾选 **"Use WSL 2 instead of Hyper-V"**（推荐，性能更好）
3. 安装完成后，打开 Docker Desktop → **Settings → Resources → WSL Integration**
4. 开启 **"Enable integration with my default WSL distro"**
5. 同时在列表中开启 **Ubuntu-22.04**
6. 点击 **Apply & Restart**

然后，开启 GPU 支持：

- Docker Desktop → **Settings → Resources → GPUs**（若无此选项，确保驱动版本正确）
- 或直接在 WSL2 Ubuntu 中测试：

```bash
# 在 WSL2 Ubuntu 终端内执行
docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
# 能看到显卡信息即成功
```

### A.5 Step 1D：在 WSL2 中准备项目目录

后续所有操作均在 **WSL2 Ubuntu 终端**中执行，与主文档 Linux 步骤完全相同：

```bash
# 打开 WSL2 Ubuntu 终端（Windows 开始菜单搜索 Ubuntu）
# 或在 Windows Terminal 中选择 Ubuntu 标签页

# 创建项目目录
mkdir -p ~/indextts2-service && cd ~/indextts2-service

# 安装 Python（用于下载模型）
sudo apt update && sudo apt install -y python3-pip
pip3 install modelscope

# 下载模型权重
modelscope download --model IndexTeam/IndexTTS-2 --local_dir ./checkpoints
```

> 之后的 **Step 2（Dockerfile）、Step 3（api_server.py）、Step 7（docker-compose.yml）** 完全按主文档操作，无需任何修改。

### A.6 Step 4A：Windows 安装 Tailscale

Windows 侧安装 Tailscale 客户端：

1. 下载 [Tailscale Windows 客户端](https://tailscale.com/download/windows)
2. 安装后，系统托盘会出现 Tailscale 图标
3. 点击图标 → **Log in**，用浏览器完成认证（与云端服务器使用同一个账号）
4. 认证成功后，Tailscale 会分配一个虚拟 IP（如 `100.100.1.10`）

```powershell
# PowerShell 中查看 Tailscale IP
tailscale ip -4
# 输出类似：100.100.1.10
```

> **注意**：Tailscale 装在 Windows 侧即可，不需要在 WSL2 内单独安装。WSL2 与 Windows 共享网络栈，Docker 容器的 8000 端口会自动映射到 Windows 的 8000 端口，Tailscale 可以直接穿透访问。

### A.7 Windows 常用运维命令

在 **WSL2 Ubuntu 终端**中操作 Docker（与 Linux 完全相同）：

```bash
# 启动服务
cd ~/indextts2-service
docker compose up -d --build

# 查看日志
docker compose logs -f tts

# 查看 GPU 使用
docker exec indextts2-api nvidia-smi

# 停止服务
docker compose down
```

**开机自启**：Docker Desktop 默认随 Windows 启动，且 docker-compose.yml 中已设置 `restart: always`，Windows 重启后服务会自动恢复，无需额外配置 systemd（WSL2 不支持 systemd 的完整开机自启）。

若需更可靠的开机自启，可在 Windows 任务计划程序中添加：

```powershell
# PowerShell（管理员）中创建开机任务
$action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d Ubuntu-22.04 -e bash -c 'cd ~/indextts2-service && docker compose up -d'"
$trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "IndexTTS2" -Action $action -Trigger $trigger -RunLevel Highest
```

### A.8 Windows 与 Linux 步骤对照表

| 主文档步骤 | Linux 操作 | Windows 替换操作 |
|-----------|-----------|----------------|
| Step 1：安装 NVIDIA Toolkit | `apt install nvidia-container-toolkit` | 安装 NVIDIA WSL2 驱动（Windows 侧）|
| Step 1：安装 Docker | `curl` 脚本 | 安装 Docker Desktop，启用 WSL2 后端 |
| Step 4：安装 Tailscale | `curl install.sh` | 安装 Windows 客户端 exe |
| Step 4：开机自启 | `systemctl enable tailscaled` | Tailscale 客户端自动随 Windows 启动 |
| Step 2/3/7/8：其余所有步骤 | 在 Linux 终端执行 | **在 WSL2 Ubuntu 终端执行，命令完全相同** |

### A.9 Windows 特有故障排查

| 症状 | 可能原因 | 解决方法 |
|------|---------|---------|
| `docker: Cannot connect to Docker daemon` | Docker Desktop 未启动 | 打开 Docker Desktop，等待启动完成 |
| `nvidia-smi` 在 WSL2 中报错 | Windows NVIDIA 驱动版本过旧 | 更新到 526.98+ 版本 |
| `--gpus all` 报错找不到 GPU | Docker Desktop GPU 未开启 | Settings → Resources → GPUs → 开启 |
| WSL2 内存占用过高 | WSL2 默认内存上限 | 创建 `%USERPROFILE%\.wslconfig` 限制内存 |
| 容器内无法访问外网 | WSL2 网络问题 | Docker Desktop → Settings → General → 关闭再开启 |

**限制 WSL2 内存占用**（可选，避免内存不足）：

```ini
# 创建或编辑 C:\Users\<你的用户名>\.wslconfig
[wsl2]
memory=12GB        # WSL2 最大内存（根据机器总内存调整）
processors=8       # WSL2 最大 CPU 核数
swap=4GB           # 交换空间
```

修改后重启 WSL2：`wsl --shutdown`，再重新打开 Ubuntu 终端。

---

## 15. 附录 B：稳定性优化与项目集成指南

> 本附录面向已完成部署、需要与 **douyinbaokuan** 项目深度集成的场景。

### B.1 稳定性优化清单

#### B.1.1 并发请求排队（最重要）

当前 FastAPI 服务是同步推理，多个请求会竞争 GPU，导致超时。建议在 `api_server.py` 中加入 asyncio 队列：

```python
import asyncio

# 全局推理锁（单 GPU 串行推理）
_inference_semaphore = asyncio.Semaphore(1)

@app.post("/synthesize")
async def synthesize(...):
    async with _inference_semaphore:
        # 在锁内执行推理
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: tts.infer(**kwargs))
```

这样并发请求会自动排队，不会因为 GPU 内存竞争导致 OOM 崩溃。

> **进阶**：在此基础上加入队列深度上限，超限直接返回 429，见 [附录 C.2.1](#c21-请求队列深度上限)。

#### B.1.2 临时文件清理

当前代码在 `finally` 中清理临时文件，但 `output_path` 未被清理（`FileResponse` 发送后文件残留）。添加后台清理：

```python
import asyncio
import os

async def cleanup_file(path: str, delay: float = 5.0):
    """延迟删除临时文件，等 FileResponse 传输完成"""
    await asyncio.sleep(delay)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass

# 在 return FileResponse(...) 之后添加：
asyncio.create_task(cleanup_file(output_path))
```

#### B.1.3 健康检查增加推理验证

当前 `/health` 只检查 `tts is not None`，模型可能加载了但推理已出错。改为：

```python
@app.get("/health")
async def health():
    if tts is None:
        return JSONResponse({"status": "loading"}, status_code=503)
    return {"status": "ok", "model_loaded": True, "queue_size": _inference_semaphore._value}
```

#### B.1.4 音频缓存（高重复文本场景）

对于短视频制作中高频重复的文本（如固定片头语），可加 MD5 缓存：

```python
import hashlib
import os

CACHE_DIR = os.environ.get("CACHE_DIR", "/tmp/tts_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

def get_cache_key(text: str, voice_name: str, **kwargs) -> str:
    content = f"{text}|{voice_name}|{kwargs}"
    return hashlib.md5(content.encode()).hexdigest()

# 在推理前检查缓存
cache_key = get_cache_key(text, voice_name)
cache_path = os.path.join(CACHE_DIR, f"{cache_key}.wav")
if os.path.exists(cache_path):
    return FileResponse(cache_path, media_type="audio/wav")
```

---

### B.2 与项目 AI 通道系统的集成

项目使用 `apps/web/src/lib/ai-channels/` 的槽位路由系统（`TTSEngineConfig`），IndexTTS2 需要以 **新增 provider adapter** 的方式接入，而非绕过通道系统直接调用。

#### B.2.1 第一步：添加 Provider 类型

编辑 [apps/web/src/lib/ai-channels/types.ts](apps/web/src/lib/ai-channels/types.ts)，在 `AIProviderType` 中新增：

```typescript
export type AIProviderType =
  | 'openai-compat'
  | 'dashscope'
  | 'volcengine'
  | 'cosyvoice'
  | 'keling'
  | 'doubao-tts'
  | 'qwen-tts'
  | 'minimax-tts'
  | 'indextts2';   // ← 新增
```

#### B.2.2 第二步：创建 Adapter

新建 `apps/web/src/lib/ai-channels/adapters/indextts2.ts`：

```typescript
/**
 * IndexTTS2 adapter: TTS via self-hosted FastAPI service.
 *
 * Endpoint: POST {baseUrl}/synthesize_json (preset voice)
 *           POST {baseUrl}/synthesize      (upload reference audio)
 */

import type { AIChannel, AIModelDefinition } from "../types";
import { AdapterError, type SynthesizeResult } from "./common";

export interface IndexTTS2Params {
  readonly text: string;
  readonly voiceName?: string;        // preset voice name (voices/*.wav)
  readonly speakerAudioBase64?: string; // reference audio for zero-shot clone
  readonly temperature?: number;
  readonly topP?: number;
  readonly topK?: number;
}

export async function synthesizeSpeech(
  channel: AIChannel,
  _model: AIModelDefinition,
  params: IndexTTS2Params,
  options?: { readonly signal?: AbortSignal },
): Promise<SynthesizeResult> {
  if (channel.provider !== "indextts2") {
    throw new AdapterError(channel.provider, "Not an IndexTTS2 channel");
  }

  const baseUrl = channel.baseUrl.replace(/\/$/, "");
  const startTime = Date.now();

  let response: Response;

  if (params.voiceName) {
    // Use preset voice — simple JSON form
    const formData = new URLSearchParams();
    formData.append("text", params.text);
    formData.append("voice_name", params.voiceName);

    response = await fetch(`${baseUrl}/synthesize_json`, {
      method: "POST",
      headers: {
        "X-API-Key": channel.apiKey,
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: formData.toString(),
      signal: options?.signal,
    });
  } else if (params.speakerAudioBase64) {
    // Reference audio upload
    const form = new FormData();
    form.append("text", params.text);
    const audioBlob = new Blob([Buffer.from(params.speakerAudioBase64, "base64")], { type: "audio/wav" });
    form.append("spk_audio", audioBlob, "reference.wav");
    if (params.temperature != null) form.append("temperature", String(params.temperature));
    if (params.topP != null) form.append("top_p", String(params.topP));
    if (params.topK != null) form.append("top_k", String(params.topK));

    response = await fetch(`${baseUrl}/synthesize`, {
      method: "POST",
      headers: { "X-API-Key": channel.apiKey },
      body: form,
      signal: options?.signal,
    });
  } else {
    throw new AdapterError("indextts2", "Must provide voiceName or speakerAudioBase64");
  }

  if (!response.ok) {
    const text = await response.text();
    throw new AdapterError("indextts2", `HTTP ${response.status}: ${text}`, response.status);
  }

  const audioBuffer = Buffer.from(await response.arrayBuffer());
  const audioBase64 = audioBuffer.toString("base64");
  // Rough duration estimate for WAV (16-bit mono, 22050 Hz)
  const duration = Math.round((audioBuffer.length / (22050 * 2)) * 10) / 10;

  return { audioBase64, duration };
}
```

#### B.2.3 第三步：注册到 adapters.ts

在 [apps/web/src/lib/ai-channels/adapters.ts](apps/web/src/lib/ai-channels/adapters.ts) 的 `synthesizeSpeech` 路由函数中添加分支：

```typescript
import {
  synthesizeSpeech as indextts2Synthesize,
} from "./adapters/indextts2";

// 在 synthesizeSpeech 路由 switch 中添加：
case "indextts2":
  return indextts2Synthesize(channel, model, params as IndexTTS2Params, options);
```

#### B.2.4 第四步：在管理后台配置 TTS Engine

通过管理后台 `/admin/ai-config` → TTS Engines，新增一条：

```json
{
  "id": "indextts2-local",
  "name": "IndexTTS2（本地）",
  "provider": "indextts2",
  "channelId": "indextts2-local",
  "synthesizeModelIds": ["indextts2"],
  "isActive": true,
  "description": "本地 GPU 部署，零成本、无限制",
  "features": ["free", "zero-shot", "emotion"]
}
```

对应 Channel 配置：

```json
{
  "id": "indextts2-local",
  "name": "IndexTTS2 本地服务",
  "provider": "indextts2",
  "baseUrl": "https://tts.yourdomain.com",
  "apiKey": "your-secret-key-here",
  "isActive": true,
  "priority": 10,
  "timeoutMs": 120000
}
```

> **timeoutMs 必须设为 120000（2 分钟）**，IndexTTS2 推理比商业 API 慢，默认的 30s 会超时。

#### B.2.5 VoiceProvider 枚举扩展（可选）

如果需要将 IndexTTS2 克隆的音色存入数据库，在 `packages/db/prisma/schema.prisma` 的 `VoiceProvider` 枚举中新增：

```prisma
enum VoiceProvider {
  VOLCENGINE
  COSYVOICE
  DOUBAO
  QWEN
  MINIMAX
  INDEXTTS2   // ← 新增
}
```

然后在 `tts-engine.ts` 的 `voiceProviderToAIProvider` 中添加：

```typescript
case 'INDEXTTS2': return 'indextts2';
```

---

### B.3 调用流程（集成后）

```
用户请求 POST /api/voices/synthesize
    │
    ▼
resolveTTSEngineByProvider("indextts2")
    │
    ▼
IndexTTS2 Adapter
    │  HTTPS (Tailscale 内网加密)
    ▼
tts.yourdomain.com → 云端 Nginx
    │  WireGuard 隧道
    ▼
本地 GPU → FastAPI → IndexTTS2 推理
    │
    ▼
返回 WAV → 上传 TOS → 返回签名 URL
```

---

### B.4 与商业 TTS 的优先级配置

IndexTTS2 部署成功后，建议在 slot 路由中设置**低优先级**（作为兜底），商业 API 作为主力：

| 优先级 | Provider | 适用场景 |
|--------|----------|---------|
| 1（最高）| CosyVoice / 豆包 | 商业音色库、情感控制 |
| 2 | Qwen TTS | 高质量克隆 |
| 3（兜底）| **IndexTTS2** | 成本敏感、批量生产 |

这样在商业 API 配额耗尽或服务异常时，自动降级到本地 IndexTTS2，零中断。

**各 Provider 典型延迟参考**（30 字中文文本，仅供决策参考，实际受网络和负载影响）：

| Provider | 首字节延迟 | 完整合成耗时 | 成本 |
|----------|-----------|------------|------|
| IndexTTS2（RTX 4080 本地）| ~2s | 3-5s | 零成本 |
| IndexTTS2（RTX 3070 本地）| ~3s | 5-8s | 零成本 |
| CosyVoice（阿里云）| ~0.5s | 1-2s | 按字计费 |
| 豆包 TTS 2.0 | ~1s | 2-3s | 按字计费 |
| Qwen TTS | ~1s | 2-4s | 按字计费 |

> IndexTTS2 推理延迟偏高是本地 GPU 方案的固有特性，适合**批量离线生产**场景（提前合成后存入 TOS），不适合需要实时响应的直播或对话场景。

---

### B.5 监控接入建议

在现有的健康检查脚本基础上，可以把 TTS 后端状态暴露给项目的管理后台：

```typescript
// apps/web/src/app/api/admin/ai-channels/test/route.ts 中
// 对 indextts2 provider 特殊处理，调用 /health 端点
if (channel.provider === 'indextts2') {
  const res = await fetch(`${channel.baseUrl}/health`, {
    headers: { 'X-API-Key': channel.apiKey },
    signal: AbortSignal.timeout(10000),
  });
  const data = await res.json();
  return NextResponse.json({ success: data.status === 'ok', latencyMs: Date.now() - start });
}
```

这样管理后台的「测试连接」按钮就能直接验证 IndexTTS2 服务状态。

---

## 16. 附录 C：进阶优化清单（已分析代码，按优先级排列）

> 本附录基于对 douyinbaokuan 项目源码的深度分析，列出集成后仍需处理的问题，分为**必做 Bug 修复**和**推荐优化**两类。

### C.1 必做 Bug 修复（集成时同步处理）

#### C.1.1 `test/route.ts` — provider 枚举未包含新 TTS providers

**文件**：`apps/web/src/app/api/admin/ai-channels/test/route.ts` 第 42 行

**现象**：Zod schema 的 `provider` 枚举只包含原始 5 个 provider，管理后台对 `doubao-tts`、`qwen-tts`、`minimax-tts`、`indextts2` 等 channel 点「测试连接」会直接返回 422 验证错误。

**当前代码**：
```typescript
provider: z.enum(["openai-compat", "dashscope", "volcengine", "cosyvoice", "keling"]),
```

**需改为**：
```typescript
provider: z.enum([
  "openai-compat", "dashscope", "volcengine", "cosyvoice", "keling",
  "doubao-tts", "qwen-tts", "minimax-tts", "indextts2",
]),
```

同时在 `switch (provider)` 分支中补充新 provider 的测试逻辑（对 `indextts2` 调用 `/health` 端点，对 `doubao-tts`/`qwen-tts`/`minimax-tts` 复用 `testReachability`）。

#### C.1.2 `synthesize/stream/route.ts` — 缺少 `INDEXTTS2` provider 分支

**文件**：`apps/web/src/app/api/voices/synthesize/stream/route.ts` 第 95 行

**现象**：流式合成路由的 provider 判断链没有 `INDEXTTS2`，会走到 cosyvoice 默认分支，用错误的 API 发起请求并报错。

**需在 provider 判断处补充**：
```typescript
const isIndexTTS2 = voiceProvider === "INDEXTTS2";

// preferredModelId 处补充
const preferredModelId = isIndexTTS2
  ? "indextts2"
  : isDoubao ? "seed-icl-2.0"
  : /* ... 原有逻辑 */;

// requiredProvider 处补充
const requiredProvider = isIndexTTS2
  ? "indextts2"
  : isDoubao ? "doubao-tts"
  : /* ... 原有逻辑 */;
```

> IndexTTS2 暂无流式推理端点时，可在 stream 路由中对 `isIndexTTS2` 走非流式降级路径（同 Qwen 的处理方式，返回完整音频）。

---

### C.2 FastAPI 服务优化（推荐，部署稳定后实施）

#### C.2.1 请求队列深度上限

当前 Semaphore 无限排队，高峰期积压几十个请求后用户等待超时、体验极差。需加队列深度上限，超限直接拒绝：

```python
_inference_semaphore = asyncio.Semaphore(1)
_queue_depth = 0
MAX_QUEUE_DEPTH = 5  # 根据实际吞吐调整

@app.post("/synthesize")
async def synthesize(...):
    global _queue_depth
    if _queue_depth >= MAX_QUEUE_DEPTH:
        raise HTTPException(status_code=429, detail="Server busy, please retry later")
    _queue_depth += 1
    try:
        async with _inference_semaphore:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: tts.infer(**kwargs))
    finally:
        _queue_depth -= 1
```

#### C.2.2 MP3 输出支持

当前 FastAPI 只输出 WAV，体积是 MP3 的 6-8 倍，影响 TOS 存储成本和传输速度。Dockerfile 已安装 ffmpeg，直接用：

```python
import subprocess

def wav_to_mp3(wav_path: str, bitrate: str = "128k") -> str:
    mp3_path = wav_path.replace(".wav", ".mp3")
    subprocess.run(
        ["ffmpeg", "-i", wav_path, "-b:a", bitrate, mp3_path, "-y"],
        check=True, capture_output=True,
    )
    return mp3_path

# 在推理完成后：
if format == "mp3":
    output_path = wav_to_mp3(output_path)
    return FileResponse(output_path, media_type="audio/mpeg", filename="output.mp3")
```

#### C.2.3 流式推理端点（提升用户感知速度）

IndexTTS2 支持分句输出，可新增 `/synthesize_stream` 端点，让项目的 `/synthesize/stream` 路由接入后实现边生成边播放：

```python
from fastapi.responses import StreamingResponse

@app.post("/synthesize_stream")
async def synthesize_stream(
    text: str = Form(...),
    voice_name: str = Form("default"),
    x_api_key: str = Header(None),
):
    verify_key(x_api_key)
    voice_path = os.path.join(MODEL_DIR, "voices", f"{voice_name}.wav")
    if not os.path.exists(voice_path):
        raise HTTPException(status_code=404, detail=f"Voice '{voice_name}' not found")

    async def generate():
        async with _inference_semaphore:
            loop = asyncio.get_event_loop()
            # infer_stream 逐句返回 bytes chunk
            chunks = await loop.run_in_executor(
                None,
                lambda: list(tts.infer_stream(spk_audio_prompt=voice_path, text=text))
            )
        for chunk in chunks:
            yield chunk

    return StreamingResponse(generate(), media_type="audio/wav")
```

---

### C.3 Nginx 安全加固（推荐，上线前完成）

#### C.3.1 请求速率限制

当前 Nginx 只有 API Key 认证，无频率控制。Key 一旦泄露 GPU 会被占满：

```nginx
# 在 /etc/nginx/nginx.conf 的 http {} 块中添加
limit_req_zone $binary_remote_addr zone=tts_limit:10m rate=5r/m;
limit_req_zone $http_x_api_key zone=tts_key_limit:10m rate=30r/m;

# 在 tts.conf 的 location / 中添加
limit_req zone=tts_limit burst=3 nodelay;
limit_req zone=tts_key_limit burst=10 nodelay;
limit_req_status 429;
```

说明：按 IP 5 req/min（防公网滥用）+ 按 Key 30 req/min（防单 Key 过载），两层叠加。

#### C.3.2 Tailscale IP 白名单（强烈建议开启）

文档主体标注为「可选」，实际应视为必做。只允许 Tailscale 虚拟网段访问，拒绝所有其他来源：

```nginx
location / {
    # 只允许 Tailscale 100.x.x.x 段（内网隧道）
    allow 100.0.0.0/8;
    # 如有多个云端服务器，逐一添加其公网 IP
    allow <云端服务器公网IP>/32;
    deny all;

    proxy_pass http://tts_backend;
    # ... 其余 proxy 配置不变
}
```

---

### C.4 运维监控补充

#### C.4.1 Tailscale 断线即时恢复（替代 crontab 方案）

主文档中使用 crontab 每 5 分钟检查，断线到恢复最长等 5 分钟。改用 systemd `OnFailure` 实现即时重启：

```bash
# 编辑 tailscaled service override
sudo mkdir -p /etc/systemd/system/tailscaled.service.d/
sudo tee /etc/systemd/system/tailscaled.service.d/restart.conf << 'EOF'
[Service]
Restart=always
RestartSec=5s
EOF

sudo systemctl daemon-reload
sudo systemctl restart tailscaled
```

断线后 5 秒内自动重连，无需 crontab watchdog。

#### C.4.2 GPU 显存 OOM 自动恢复

IndexTTS2 偶发 CUDA OOM（尤其是超长文本），进程会崩溃。依靠 Docker `restart: always` 可自动重启，但模型重新加载需 30-60 秒，期间所有请求 503。

建议在 FastAPI 中捕获 CUDA 错误并主动释放显存，而不是让进程崩溃：

```python
import torch

try:
    result = await loop.run_in_executor(None, lambda: tts.infer(**kwargs))
except RuntimeError as e:
    if "out of memory" in str(e).lower():
        torch.cuda.empty_cache()
        raise HTTPException(status_code=503, detail="GPU out of memory, please retry")
    raise
```

---

### C.5 总优先级汇总

| 优先级 | 类别 | 项目 | 影响 |
|--------|------|------|------|
| 🔴 P0 必做 | Bug | `test/route.ts` zod schema 枚举缺失 | 管理后台测试连接全部 422 |
| 🔴 P0 必做 | Bug | `stream/route.ts` 缺少 INDEXTTS2 分支 | 流式合成直接报错 |
| 🟡 P1 推荐 | 安全 | Nginx 速率限制 | 防 GPU 被滥用 |
| 🟡 P1 推荐 | 安全 | Tailscale IP 白名单（改为必做） | 防 Key 泄露后的公网攻击 |
| 🟡 P1 推荐 | 稳定 | 队列深度上限 | 防无限积压超时 |
| 🟡 P1 推荐 | 稳定 | CUDA OOM 捕获 | 防进程崩溃触发 30s 重载 |
| 🟢 P2 可选 | 性能 | MP3 输出 | 节省存储 + 传输带宽 |
| 🟢 P2 可选 | 体验 | 流式推理端点 | 降低用户感知延迟 |
| 🟢 P2 可选 | 运维 | systemd OnFailure 替代 crontab | 断线恢复从 5min → 5s |
