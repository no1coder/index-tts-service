# index-tts-service

基于 [IndexTTS2](https://github.com/indexteam/Index-TTS) 的生产级语音合成 API 服务。Docker 一键部署，GPU 推理，开箱即用。

## 特性

- **FastAPI 异步服务** — 队列管理、并发控制、CUDA OOM 保护
- **音色管理** — 上传参考音频、试听、删除，支持多音色
- **情感控制** — 音频参考 / 文字驱动 / 向量直传
- **任务系统** — 异步任务队列 + SSE 实时进度推送
- **音频缓存** — 相同文本自动命中缓存，减少重复推理
- **WebUI** — 内置调试界面，浏览器直接试用
- **Docker 部署** — GPU 直通、健康检查、日志轮转、一条命令启动

## 快速开始

### 1. 下载模型权重

```bash
bash scripts/download_model.sh
```

或手动从 [ModelScope](https://www.modelscope.cn/models/IndexTeam/IndexTTS-2) 下载到 `~/tts-checkpoints`。

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，至少修改 TTS_API_KEY
```

### 3. 启动服务

```bash
docker compose up -d --build
```

服务启动后（约 2 分钟加载模型）：
- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health
- WebUI：http://localhost:8000/

## API 示例

```bash
curl -X POST http://localhost:8000/tts \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -F "text=你好，世界" \
  -F "voice=default" \
  --output output.wav
```

## 配置说明

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TTS_API_KEY` | — | API 密钥（必填） |
| `USE_FP16` | `true` | FP16 推理（显存减半，推荐开启） |
| `MAX_QUEUE_DEPTH` | `5` | 最大排队深度，超出返回 429 |
| `CHECKPOINTS_HOST_PATH` | `/root/tts-checkpoints` | 模型权重在宿主机的路径 |
| `ENABLE_CACHE` | `true` | 音频缓存（相同文本复用结果） |

完整配置见 [.env.example](.env.example)。

## 项目结构

```
├── api_server.py          # FastAPI 服务主文件
├── Dockerfile             # 镜像构建（PyTorch + CUDA 12.1）
├── docker-compose.yml     # 服务编排（GPU、卷、健康检查）
├── .env.example           # 环境变量模板
├── nginx/                 # Nginx 反向代理配置（SSL）
└── scripts/
    ├── download_model.sh  # 模型下载脚本
    ├── setup_cloud_server.sh
    └── setup_wsl2_autostart.ps1
```

## 系统要求

- NVIDIA GPU（建议 8GB+ 显存，FP16 模式约 6GB）
- Docker + NVIDIA Container Toolkit
- 模型权重约 3GB 磁盘空间

## License

本项目为 IndexTTS2 的部署封装，模型版权归 [IndexTeam](https://github.com/indexteam) 所有。
