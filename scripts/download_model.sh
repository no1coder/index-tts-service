#!/bin/bash
# ── 模型权重下载脚本 ──────────────────────────────────────────────────────────
# 执行环境：WSL2 Ubuntu，在 ~/indextts2-service 目录下执行
# 用法：bash download_model.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKPOINTS_DIR="$SCRIPT_DIR/../checkpoints"
mkdir -p "$CHECKPOINTS_DIR"

echo "==> 下载目标目录: $CHECKPOINTS_DIR"
echo ""

# ── 方式一：ModelScope（国内推荐，速度最快） ──────────────────────────────────
download_via_modelscope() {
    echo "==> 尝试 ModelScope 下载..."
    sudo apt-get update -qq
    sudo apt-get install -y python3-pip -qq
    pip3 install -q modelscope
    modelscope download \
        --model IndexTeam/IndexTTS-2 \
        --local_dir "$CHECKPOINTS_DIR"
    echo "✅ ModelScope 下载完成"
}

# ── 方式二：git lfs 克隆（ModelScope 超时时的备用） ──────────────────────────
download_via_git_lfs() {
    echo "==> 尝试 git lfs 克隆..."
    sudo apt-get install -y git-lfs -qq
    git lfs install

    TEMP_DIR=$(mktemp -d)
    git clone --no-checkout https://www.modelscope.cn/IndexTeam/IndexTTS-2.git "$TEMP_DIR"
    cd "$TEMP_DIR"
    git lfs pull
    cp -r . "$CHECKPOINTS_DIR/"
    rm -rf "$TEMP_DIR"
    echo "✅ git lfs 下载完成"
}

# 优先尝试 ModelScope，失败则切换 git lfs
if download_via_modelscope; then
    echo ""
else
    echo "ModelScope 失败，切换 git lfs..."
    download_via_git_lfs
fi

# ── 验证关键文件 ──────────────────────────────────────────────────────────────
echo ""
echo "==> 验证模型文件..."
REQUIRED_FILES=("config.yaml" "gpt.pth" "bpe.model")
ALL_OK=true
for f in "${REQUIRED_FILES[@]}"; do
    if [ -f "$CHECKPOINTS_DIR/$f" ]; then
        SIZE=$(du -sh "$CHECKPOINTS_DIR/$f" | cut -f1)
        echo "  ✅ $f ($SIZE)"
    else
        echo "  ❌ 缺失: $f"
        ALL_OK=false
    fi
done

if $ALL_OK; then
    echo ""
    echo "✅ 所有必要模型文件已就绪"
    echo "   下一步：docker compose up -d --build"
else
    echo ""
    echo "❌ 部分文件缺失，请检查下载是否完整"
    exit 1
fi
