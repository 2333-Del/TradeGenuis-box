FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/python:3.12.9-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
# 基础镜像走 docker.aityp.com 镜像站；pip 走清华源加速构建
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# 项目内所有时间均按北京时间硬编码（BJT），不依赖容器时区

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scanner.py server.py resonance.py market_data.py notifications.py em.py ths.py fund.py dashboard.html ./
COPY history_store.py reviews.py ./
COPY migrations ./migrations
COPY static ./static

# 兜底：即使 compose 的 data 卷挂载缺失/宿主目录不存在，扫描结果也能落盘
RUN mkdir -p /app/data

EXPOSE 8808

# 未认证时 GET / 返回登录页（200），可作为健康检查
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8808/',timeout=3)"

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8808"]
