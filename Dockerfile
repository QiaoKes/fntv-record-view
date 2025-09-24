# 使用更精简的 Alpine 基础镜像
FROM python:3.13-alpine

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# 复制需求文件并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建数据库目录用于挂载
RUN mkdir -p /app/database

# 暴露端口
EXPOSE 5000

# 启动命令
CMD ["python", "main.py"]