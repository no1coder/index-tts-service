# IndexTTS2 部署踩坑记录与解决方案

> 本文档记录了从零部署 IndexTTS2 Docker 服务过程中遇到的所有问题及解决方案。
> 环境：Windows 10 + WSL2 Ubuntu 24.04 + Docker Desktop + RTX 3080 Ti 12GB

---

## 目录

1. [Docker 基础镜像问题](#1-docker-基础镜像问题)
2. [网络与镜像源问题](#2-网络与镜像源问题)
3. [pip 依赖安装问题](#3-pip-依赖安装问题)
4. [模型加载运行时问题](#4-模型加载运行时问题)
5. [可复用资源清单](#5-可复用资源清单)

---

## 1. Docker 基础镜像问题

### 1.1 cudnn9 镜像不存在

**错误**：`pytorch/pytorch:2.3.1-cuda12.1-cudnn9-runtime` 找不到

**原因**：Docker Hub 上 pytorch 官方镜像只有 `cudnn8` 版本，没有 `cudnn9`

**解决**：使用 `pytorch/pytorch:2.3.1-cuda12.1-cudnn8-runtime`

### 1.2 国内 Docker 镜像源 403

**错误**：DaoCloud (`docker.m.daocloud.io`) 和 NJU (`docker.nju.edu.cn`) 拉取 pytorch 镜像返回 403

**原因**：这些镜像源不缓存/不支持 pytorch 组织的镜像

**解决**：直接使用 `docker.io` 官方源（Docker Desktop 自带代理），首次拉取 ~3.69GB 较慢但可以成功

---

## 2. 网络与镜像源问题

### 2.1 GitHub clone 在 Docker 构建中失败

**错误**：`GnuTLS recv error (-110): The TLS connection was non-properly terminated`

**原因**：Docker 构建环境中访问 GitHub 被 GFW 阻断

**解决**：在宿主机（WSL2）预先克隆源码，再 COPY 进镜像
```bash
# WSL2 宿主机执行
cd ~/indextts2-service
GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1 https://github.com/index-tts/index-tts.git indextts-src

# Dockerfile 中
COPY indextts-src/ /app/
```

备选：使用国内 GitHub 镜像
```bash
GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1 https://gitclone.com/github.com/index-tts/index-tts.git indextts-src
```

### 2.2 Git LFS 预算耗尽

**错误**：`batch response: This repository is over its data quota`

**原因**：GitHub LFS 免费额度用完，大文件无法下载

**解决**：克隆时跳过 LFS 大文件，模型从 ModelScope 单独下载
```bash
GIT_LFS_SKIP_SMUDGE=1 git clone ...
```

### 2.3 清华 PyPI 镜像返回损坏 JSON

**错误**：`json.decoder.JSONDecodeError: Unterminated string starting at...`

**原因**：清华 PyPI 镜像高负载时返回截断的索引页面

**解决**：换阿里云 PyPI 镜像
```
-i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
```

### 2.4 NVIDIA 包下载超时

**错误**：`ReadTimeoutError: HTTPSConnectionPool(host='pypi.nvidia.com'): Read timed out`

**原因**：nvidia-cudnn-cu12（706MB）、nvidia-cublas-cu12（594MB）等大包从 pypi.nvidia.com 下载极慢

**解决**：
1. 增加超时和重试：`pip install --timeout=600 --retries=5`
2. 使用 BuildKit 缓存挂载：`--mount=type=cache,target=/root/.cache/pip`
3. 不要用 `docker compose build --no-cache`（会丢失层缓存），除非 Dockerfile 改了才用
4. 失败后直接重试 `docker compose build tts`，pip 缓存会保留已下载的包

### 2.5 HuggingFace 模型下载被墙

**错误**：模型加载卡在 `SeamlessM4TFeatureExtractor.from_pretrained("facebook/w2v-bert-2.0")` 无响应

**原因**：容器内访问 huggingface.co 被 GFW 阻断

**解决**：docker-compose.yml 中设置 HF 国内镜像
```yaml
environment:
  - HF_ENDPOINT=https://hf-mirror.com
  - HF_HOME=/app/checkpoints/hf_cache
```

首次启动时会自动下载以下 HF 模型（共 ~180MB），之后缓存在 checkpoints/hf_cache/：
- `facebook/w2v-bert-2.0`（preprocessor_config.json，<1MB）
- `amphion/MaskGCT`（semantic_codec/model.safetensors，177MB）
- `funasr/campplus`（campplus_cn_common.bin，自动下载）
- `nvidia/bigvgan_v2_22khz_80band_256x`（BigVGAN vocoder）

---

## 3. pip 依赖安装问题

### 3.1 requirements.txt 中的 hash 校验失败

**错误**：`THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE`

**原因**：IndexTTS2 的 requirements.txt 包含 `--hash=sha256:...` 严格校验，但镜像源的包 hash 不同

**解决**（早期方案，已废弃）：用 sed 去除 hash
```bash
sed '/--hash/d' requirements.txt | sed 's/ *\\$//' > /tmp/req_clean.txt
```

**最终方案**：不使用 requirements.txt，直接列出 pyproject.toml 中的依赖

### 3.2 hatchling 构建系统缺失

**错误**：`ModuleNotFoundError: No module named 'hatchling'`

**原因**：`pip install .` 需要 pyproject.toml 中声明的构建后端 `hatchling >= 1.27.0`

**解决**：**不使用 `pip install .`**，改为直接列出所有依赖安装，源码 COPY 到 /app/ 直接 import
```dockerfile
# 不要这样做：
# RUN pip install .

# 正确做法：直接列出依赖
RUN pip install "accelerate==1.8.1" "librosa==0.10.2.post1" "transformers==4.52.1" ...
```

### 3.3 torch 版本冲突：2.3 vs 2.8

**错误**：`cannot import name 'OffloadedCache' from 'transformers'`

**原因**：`transformers==4.52.1` 需要 PyTorch >= 2.4，而基础镜像自带 torch 2.3.1

**解决**：必须升级到 torch 2.8，从 PyTorch 官方 CUDA 索引安装
```dockerfile
RUN pip install "torch==2.8.*" "torchaudio==2.8.*" "torchvision==0.23.*" \
    --index-url https://download.pytorch.org/whl/cu128
```

### 3.4 torchvision::nms 算子不存在

**错误**：`operator torchvision::nms does not exist`

**原因**：torchvision 未安装，或从 PyPI 安装了 CPU 版（没有 CUDA 算子）

**解决**：torchvision 必须和 torch 一起从 PyTorch CUDA 索引安装
```dockerfile
RUN pip install "torch==2.8.*" "torchaudio==2.8.*" "torchvision==0.23.*" \
    --index-url https://download.pytorch.org/whl/cu128
```

### 3.5 pip install . 时 torch 被 CPU 版覆盖

**原因**：`pip install .` 会解析 pyproject.toml 中的 `torch==2.8.*` 依赖，从阿里云 PyPI 下载 CPU 版覆盖 CUDA 版

**解决**：不使用 `pip install .`，手动列出依赖并排除 torch 系列（参见 3.2）

---

## 4. 模型加载运行时问题

### 4.1 API 参数名错误

**错误**：`IndexTTS2.__init__() got an unexpected keyword argument 'is_fp16'`

**原因**：api_server.py 中写成了 `is_fp16`，但 IndexTTS2 构造函数的参数名是 `use_fp16`

**解决**：修改 api_server.py
```python
# 错误
tts = IndexTTS2(cfg_path=..., model_dir=..., is_fp16=USE_FP16)
# 正确
tts = IndexTTS2(cfg_path=..., model_dir=..., use_fp16=USE_FP16)
```

### 4.2 checkpoints 只读文件系统

**错误**：`[Errno 30] Read-only file system: './checkpoints/hf_cache'`

**原因**：docker-compose.yml 中 checkpoints 卷挂载为 `:ro`（只读），但模型代码需要在里面创建 hf_cache 目录

**解决**：去掉 `:ro` 标记
```yaml
# 错误
- ${CHECKPOINTS_HOST_PATH}:/app/checkpoints:ro
# 正确
- ${CHECKPOINTS_HOST_PATH}:/app/checkpoints
```

### 4.3 日志不实时输出

**症状**：模型加载看似卡住，日志长时间没有新输出

**原因**：Python 默认缓冲 stdout，`print()` 输出不会立即刷新

**解决**：docker-compose.yml 中添加
```yaml
environment:
  - PYTHONUNBUFFERED=1
```

---

## 5. 可复用资源清单

以下资源在首次部署后已缓存，可以**直接复制到云服务器或其他机器**复用：

### 5.1 Docker 镜像（~15GB，包含所有依赖）

导出并传输到其他机器：
```bash
# 在当前机器导出
docker save indextts2-service-tts:latest | gzip > indextts2-image.tar.gz

# 传到云服务器
scp indextts2-image.tar.gz user@cloud-server:/tmp/

# 在云服务器导入
docker load < /tmp/indextts2-image.tar.gz
```

这是**最高效的方式**，镜像包含所有 Python 依赖、torch 2.8、CUDA 库等，不需要重新下载。

### 5.2 模型权重（~6.1GB）

位置：`/root/tts-checkpoints/`

```bash
# 打包模型（包含 HF 缓存）
cd /root
tar czf tts-checkpoints.tar.gz tts-checkpoints/

# 传到云服务器
scp tts-checkpoints.tar.gz user@cloud-server:/tmp/

# 在云服务器解压
cd /root
tar xzf /tmp/tts-checkpoints.tar.gz
```

包含内容：
| 文件 | 大小 | 说明 |
|------|------|------|
| gpt.pth | 3.4GB | GPT 主模型 |
| s2mel.pth | 1.2GB | S2Mel 声学模型 |
| bpe.model | 476KB | BPE 分词器 |
| config.yaml | 3KB | 模型配置 |
| feat1.pt / feat2.pt | ~430KB | 特征文件 |
| wav2vec2bert_stats.pt | 9KB | 语音统计 |
| qwen0.6bemo4-merge/ | ~1.2GB | Qwen 情感模型 |
| hf_cache/ | ~200MB | HuggingFace 缓存（w2v-bert, MaskGCT, campplus, bigvgan） |

### 5.3 pip 下载缓存（Docker BuildKit）

位于 Docker BuildKit 缓存中（`--mount=type=cache,target=/root/.cache/pip`），在同一台机器上 rebuild 时自动复用。**无法直接传输到其他机器**，但导出 Docker 镜像的方式更好。

### 5.4 IndexTTS2 源码

位置：`~/indextts2-service/indextts-src/`（已通过 COPY 打入镜像）

### 5.5 快速部署到新机器的完整流程

```bash
# 1. 传输 Docker 镜像（最大，但包含一切依赖）
scp indextts2-image.tar.gz user@new-server:/tmp/
ssh user@new-server "docker load < /tmp/indextts2-image.tar.gz"

# 2. 传输模型权重
scp tts-checkpoints.tar.gz user@new-server:/root/
ssh user@new-server "cd /root && tar xzf tts-checkpoints.tar.gz"

# 3. 传输项目配置文件
scp docker-compose.yml .env user@new-server:/opt/indextts2/

# 4. 在新服务器启动
ssh user@new-server "cd /opt/indextts2 && docker compose up -d"
```

总传输量约 **20GB**（镜像 ~15GB + 模型 ~6GB，有压缩）。

---

## 附录：最终确认的技术栈版本

| 组件 | 版本 |
|------|------|
| 基础镜像 | pytorch/pytorch:2.3.1-cuda12.1-cudnn8-runtime |
| Python | 3.10 |
| torch | 2.8.x (CUDA cu128) |
| torchaudio | 2.8.x |
| torchvision | 0.23.x |
| transformers | 4.52.1 |
| librosa | 0.10.2.post1 |
| CUDA (容器内) | 12.8 (torch 自带) |
| NVIDIA 驱动 (宿主机) | 591.55+ |
| APT 源 | mirrors.aliyun.com |
| PyPI 源 | mirrors.aliyun.com/pypi/simple/ |
| HuggingFace 镜像 | hf-mirror.com |
