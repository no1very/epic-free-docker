FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    HOME=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# playwright install --with-deps 会自动安装 Chromium 运行所需系统库（amd64/arm64 均支持）
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /root/.cache /var/lib/apt/lists/*

# 预创建 epic 用户（uid 1000 兜底）；/data 实际属主由入口脚本按 PUID/PGID 修正
RUN useradd -m -u 1000 epic && mkdir -p /data /opt/pw-browsers \
    && chown -R epic:epic /data /opt/pw-browsers

COPY main.py .
COPY epic/ ./epic/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

VOLUME ["/data"]
EXPOSE 8321
ENV DATA_DIR=/data \
    TZ=Asia/Shanghai \
    EPIC_COUNTRY=US \
    EPIC_LOCALE=en-US \
    CLAIM_INTERVAL_HOURS=6 \
    WEB_PORT=8321 \
    NOTIFY_METHOD=none \
    PUID=1000 \
    PGID=1000

# 以 root 启动 → 入口脚本按 PUID/PGID 修正 /data 属主 → gosu 降权运行
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "main.py", "run"]
