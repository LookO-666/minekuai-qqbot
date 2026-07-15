# 机器人镜像 - 基于 Python 3.11 slim
FROM python:3.11-slim AS runtime

# 设置时区为东八区，日志时间才对得上
ENV TZ=Asia/Shanghai
RUN ln -sf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 先装 Python 依赖，利用 docker 层缓存
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "nonebot2[fastapi]>=2.3.0" \
    "nonebot-adapter-onebot>=2.4.0" \
    "httpx>=0.27.0" \
    "pydantic>=2.0" \
    "pydantic-settings>=2.0" \
    "loguru>=0.7.0" \
    "playwright>=1.40.0" \
    "mcstatus>=11.0.0" \
    "Pillow>=10.0.0"

# 装 Chromium + 系统依赖（用于自动登录刷 token）
# 这一步会让镜像大约多 300-400 MB
RUN playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/* /tmp/*

# 再拷代码（代码改动不会让依赖层缓存失效）
COPY bot.py ./
COPY plugins/ ./plugins/

# 持久化数据目录（操作日志数据库会写到这）
VOLUME ["/app/data"]
WORKDIR /app

# 让 SQLite 写到 data 目录而不是工作目录
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]


# 测试镜像单独安装测试依赖，避免增大生产镜像。
FROM python:3.11-slim AS test

WORKDIR /app

COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY plugins/ ./plugins/
COPY tests/ ./tests/

CMD ["pytest", "-q"]
