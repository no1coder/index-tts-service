# IndexTTS2 部署操作手册
## 本机环境：RTX 3080 Ti 12GB / Windows 10 21H2 / NVIDIA 591.55

> 本手册是针对当前机器的**可执行操作步骤**，直接按序操作即可。
>
> 当前状态：NVIDIA 驱动已就绪 ✅ | WSL2 未安装 ⚠️ | Docker 未安装 ⚠️

---

## 阶段一：本机环境准备（需要重启）

### Step 1A：启用 WSL2（以管理员身份运行 PowerShell）

```powershell
# 启用 WSL 功能
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart

# 重启（必须）
Restart-Computer
```

**重启后继续**（管理员 PowerShell）：

```powershell
# 设置 WSL2 为默认版本
wsl --set-default-version 2

# 安装 Ubuntu 22.04
wsl --install -d Ubuntu-24.04

# 验证（应显示 Ubuntu-24.04  Running  2）
wsl --list --verbose
```

> 首次启动 Ubuntu 会提示创建用户名和密码，设置完成后进入 WSL2 终端。

### Step 1B：确认 GPU 在 WSL2 内可见

在 WSL2 Ubuntu 终端中执行：

```bash
nvidia-smi
# 应显示：RTX 3080 Ti，CUDA 13.1
```

> **不需要**在 WSL2 内安装 CUDA，Windows 侧驱动 591.55 已满足要求。

### Step 1C：安装 Docker Desktop

1. 下载 Docker Desktop for Windows（官网）
2. 安装时勾选 **"Use WSL 2 instead of Hyper-V"**
3. 安装完成后：Docker Desktop → Settings → Resources → WSL Integration
4. 开启 **Ubuntu-24.04** 集成
5. Settings → Resources → GPUs → 确认 GPU 可用
6. Apply & Restart

**验证 Docker GPU 支持**（WSL2 Ubuntu 终端）：

```bash
docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
# 能看到 RTX 3080 Ti 信息即成功
```

### Step 1D：安装 Tailscale（Windows 客户端）

1. 下载 Tailscale Windows 客户端（官网）
2. 安装后托盘图标 → Log in → 浏览器认证
3. 记录分配的虚拟 IP（如 `100.100.1.10`）

```powershell
# PowerShell 中查看 Tailscale IP
tailscale ip -4
```

> Tailscale 安装在 Windows 侧，WSL2 和容器自动共享网络，无需额外配置。

---

## 阶段二：下载模型 + 构建服务（WSL2 Ubuntu 终端）

### Step 2A：将项目文件复制到 WSL2

```bash
# 从 Windows 路径访问项目文件（/mnt/e 对应 E 盘）
cp -r /mnt/e/code/index-tts2 ~/indextts2-service
cd ~/indextts2-service
```

### Step 2B：配置环境变量

```bash
cp .env.example .env
# 编辑 .env，设置强随机 API Key
nano .env

# 生成随机 Key（推荐）：
openssl rand -hex 32
# 将输出填入 TTS_API_KEY=
```

### Step 2C：下载模型权重（约 10GB，国内推荐 ModelScope）

```bash
# 方式一：ModelScope（推荐）
sudo apt update && sudo apt install -y python3-pip
pip3 install modelscope
modelscope download --model IndexTeam/IndexTTS-2 --local_dir ./checkpoints

# 方式二：如 ModelScope 超时，用 git lfs
bash scripts/download_model.sh
```

### Step 2D：准备默认音色

```bash
# 放入一段 3-10 秒、清晰无噪音的参考音频
mkdir -p checkpoints/voices
cp /path/to/your/reference_voice.wav checkpoints/voices/default.wav
```

### Step 2E：构建并启动服务

```bash
cd ~/indextts2-service

# 构建镜像（首次约 10-15 分钟）
docker compose up -d --build

# 查看启动日志（模型加载需 30-90 秒）
docker compose logs -f tts

# 验证服务就绪
curl http://localhost:8000/health
# 期望：{"status":"ok","model_loaded":true,...}
```

---

## 阶段三：云端服务器部署

### Step 3A：在云端服务器上安装 Tailscale（同一账号）

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up    # 弹出浏览器，登录同一个 Tailscale 账号
tailscale ip -4      # 记录云端服务器的 Tailscale IP
```

### Step 3B：验证 Tailscale 隧道连通

```bash
# 在云端服务器上 ping 本地机器（替换为实际 Tailscale IP）
ping 100.100.1.10
curl http://100.100.1.10:8000/health
```

### Step 3C：一键部署云端 Nginx + SSL

```bash
# 上传脚本到云端服务器
scp scripts/setup_cloud_server.sh user@your-cloud-server:~/

# 在云端服务器执行（替换域名和 Tailscale IP）
chmod +x ~/setup_cloud_server.sh
sudo ~/setup_cloud_server.sh tts.yourdomain.com 100.100.1.10
```

### Step 3D：配置域名 DNS

在域名服务商控制台添加 A 记录：

| 类型 | 主机记录 | 记录值 | TTL |
|------|----------|--------|-----|
| A | tts | 云端服务器公网 IP | 600 |

验证：
```bash
dig tts.yourdomain.com
# 应返回云端服务器公网 IP
```

---

## 阶段四：全链路验证

```bash
# 健康检查
curl https://tts.yourdomain.com/health

# 完整合成测试
curl -X POST https://tts.yourdomain.com/synthesize_json \
  -H "X-API-Key: your-secret-key-here" \
  -F "text=大家好，IndexTTS2 服务已就绪。" \
  -F "voice_name=default" \
  --output test.wav

# 播放验证
ffplay test.wav
```

---

## 阶段五：开机自启（Windows 重启后自动恢复）

```powershell
# 在管理员 PowerShell 中执行
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
.\scripts\setup_wsl2_autostart.ps1
```

---

## 常用运维命令（WSL2 Ubuntu 终端）

```bash
cd ~/indextts2-service

# 查看服务状态
docker compose ps

# 实时日志
docker compose logs -f tts

# 查看 GPU 显存使用
docker exec indextts2-api nvidia-smi

# 重启服务（不重新构建）
docker compose restart tts

# 更新代码后重建
docker compose up -d --build

# 停止服务
docker compose down
```

---

## 故障排查速查

| 症状 | 排查命令 |
|------|---------|
| 容器启动失败 | `docker compose logs tts` |
| GPU 不可见 | `docker exec indextts2-api nvidia-smi` |
| 503 Model not loaded | 等待 90s 后再试，或查看日志 |
| 429 Server busy | 降低并发或增大 MAX_QUEUE_DEPTH |
| Nginx 502 | `ping 100.100.1.10` 验证 Tailscale |
| Nginx 504 超时 | 增大 nginx `proxy_read_timeout` |
| CUDA OOM | 缩短文本 或 检查其他进程占用显存 |

---

## 项目文件清单

```
e:\code\index-tts2\
├── api_server.py              # FastAPI 服务（生产级，含所有修复）
├── Dockerfile                 # Docker 镜像定义
├── docker-compose.yml         # 服务编排
├── .env.example               # 环境变量模板（复制为 .env 并填写）
├── .gitignore                 # 排除敏感文件和模型权重
├── SETUP.md                   # 本操作手册
├── checkpoints/               # 模型权重目录（下载后存放于此）
│   ├── config.yaml
│   ├── gpt.pth
│   ├── bpe.model
│   └── voices/                # 预存音色
│       └── default.wav
├── nginx/
│   ├── tts.conf               # Nginx 反向代理配置（含速率限制+IP白名单）
│   └── nginx_http_block.conf  # 需添加到 nginx.conf http{} 块的内容
├── scripts/
│   ├── download_model.sh      # 模型下载脚本（WSL2 执行）
│   ├── setup_cloud_server.sh  # 云端服务器一键部署（云端执行）
│   └── setup_wsl2_autostart.ps1  # Windows 开机自启注册（Windows 执行）
└── logs/                      # 日志目录（容器挂载）
```
