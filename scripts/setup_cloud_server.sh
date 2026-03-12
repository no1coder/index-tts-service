#!/bin/bash
# ── 云端服务器一键部署脚本 ────────────────────────────────────────────────────
# 执行环境：云端服务器（Ubuntu 22.04 / Debian 12）
# 用法：
#   chmod +x setup_cloud_server.sh
#   sudo ./setup_cloud_server.sh <你的域名> <本地机器Tailscale IP>
# 示例：
#   sudo ./setup_cloud_server.sh tts.example.com 100.100.1.10

set -euo pipefail

DOMAIN="${1:?用法: $0 <域名> <本地Tailscale IP>}"
LOCAL_TAILSCALE_IP="${2:?用法: $0 <域名> <本地Tailscale IP>}"

echo "==> 域名: $DOMAIN"
echo "==> 本地 Tailscale IP: $LOCAL_TAILSCALE_IP"

# ── Step 1: 安装依赖 ──────────────────────────────────────────────────────────
echo "==> 安装 Nginx + Certbot..."
apt-get update -qq
apt-get install -y nginx certbot python3-certbot-nginx

# ── Step 2: 安装 Tailscale ────────────────────────────────────────────────────
echo "==> 安装 Tailscale..."
curl -fsSL https://tailscale.com/install.sh | sh

# 配置 Tailscale 断线自动重连（systemd OnFailure，5s 内恢复）
mkdir -p /etc/systemd/system/tailscaled.service.d/
cat > /etc/systemd/system/tailscaled.service.d/restart.conf << 'EOF'
[Service]
Restart=always
RestartSec=5s
EOF
systemctl daemon-reload
systemctl enable tailscaled
systemctl start tailscaled

echo ""
echo ">>> 请在浏览器中完成 Tailscale 认证，认证完成后按 Enter 继续..."
tailscale up
read -r -p "Tailscale 认证完成后按 Enter..."

# ── Step 3: 配置速率限制（加入 nginx.conf http{} 块） ─────────────────────────
echo "==> 配置 Nginx 速率限制..."
RATE_LIMIT_CONF='
    # IndexTTS2 速率限制
    limit_req_zone $binary_remote_addr zone=tts_ip_limit:10m rate=5r/m;
    limit_req_zone $http_x_api_key zone=tts_key_limit:10m rate=30r/m;'

# 检查是否已添加
if ! grep -q "tts_ip_limit" /etc/nginx/nginx.conf; then
    sed -i "/http {/a\\$RATE_LIMIT_CONF" /etc/nginx/nginx.conf
    echo "  已添加速率限制配置"
else
    echo "  速率限制配置已存在，跳过"
fi

# ── Step 4: 申请 SSL 证书（临时 HTTP 配置） ───────────────────────────────────
echo "==> 申请 Let's Encrypt 证书..."
cat > /etc/nginx/conf.d/tts_temp.conf << EOF
server {
    listen 80;
    server_name $DOMAIN;
    location / { return 200 'ok'; add_header Content-Type text/plain; }
}
EOF
nginx -t && systemctl reload nginx
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --email admin@"$DOMAIN"
rm -f /etc/nginx/conf.d/tts_temp.conf

# ── Step 5: 部署 Nginx 反向代理配置 ──────────────────────────────────────────
echo "==> 部署 Nginx 反向代理配置..."
CLOUD_PUBLIC_IP=$(curl -s ifconfig.me)
cat > /etc/nginx/conf.d/tts.conf << EOF
upstream tts_backend {
    server ${LOCAL_TAILSCALE_IP}:8000;
    keepalive 8;
}

server {
    listen 443 ssl http2;
    server_name ${DOMAIN};

    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    add_header Strict-Transport-Security "max-age=63072000" always;

    client_max_body_size 50m;
    proxy_connect_timeout 30s;
    proxy_read_timeout    180s;
    proxy_send_timeout    180s;

    access_log /var/log/nginx/tts_access.log;
    error_log  /var/log/nginx/tts_error.log warn;

    location /health {
        allow 100.0.0.0/8;
        allow 127.0.0.1;
        deny all;
        proxy_pass http://tts_backend;
        proxy_set_header Host \$host;
        proxy_read_timeout 10s;
    }

    location / {
        allow 100.0.0.0/8;
        allow 127.0.0.1;
        allow ${CLOUD_PUBLIC_IP}/32;
        deny all;

        limit_req zone=tts_ip_limit  burst=3  nodelay;
        limit_req zone=tts_key_limit burst=10 nodelay;
        limit_req_status 429;

        if (\$http_x_api_key = "") {
            return 403 '{"error":"Missing X-API-Key header"}';
        }

        proxy_pass http://tts_backend;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
    }
}

server {
    listen 80;
    server_name ${DOMAIN};
    return 301 https://\$host\$request_uri;
}
EOF

# ── Step 6: 验证并启动 ────────────────────────────────────────────────────────
nginx -t && systemctl reload nginx
systemctl enable nginx

# ── Step 7: 健康检查监控（每 2 分钟） ─────────────────────────────────────────
echo "==> 配置健康检查 cron..."
cat > /usr/local/bin/check_tts_health.sh << SCRIPT
#!/bin/bash
HTTP_CODE=\$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://${LOCAL_TAILSCALE_IP}:8000/health)
if [ "\$HTTP_CODE" != "200" ]; then
    echo "\$(date) TTS backend unreachable (HTTP \$HTTP_CODE)" >> /var/log/tts_watchdog.log
fi
SCRIPT
chmod +x /usr/local/bin/check_tts_health.sh
(crontab -l 2>/dev/null; echo "*/2 * * * * /usr/local/bin/check_tts_health.sh") | crontab -

echo ""
echo "✅ 云端服务器部署完成！"
echo "   域名：https://$DOMAIN"
echo "   健康检查：https://$DOMAIN/health"
echo ""
echo "下一步：在本地 GPU 机器（WSL2）中启动 IndexTTS2 服务"
