#!/bin/bash

# 启动脚本 - 生产环境使用
set -e

echo "=== FNTV Record View 启动 ==="
echo "时间: $(date)"
echo "Python版本: $(python --version)"
echo "工作目录: $(pwd)"
echo "用户: $(whoami)"

# 检查数据库目录是否存在
if [ ! -d "/app/database" ]; then
    echo "❌ 错误: 数据库目录 /app/database 不存在"
    exit 1
fi

# 检查数据库文件是否存在
if [ ! -f "/app/database/trimmedia.db" ]; then
    echo "❌ 错误: 数据库文件 /app/database/trimmedia.db 不存在"
    echo "请确保外部数据库目录正确挂载到 /app/database"
    exit 1
fi

echo "✅ 数据库文件检查通过"

# 设置生产环境变量
export FLASK_ENV=production
export PYTHONPATH=/app

# 启动应用
echo "🚀 启动 Flask 应用..."
exec python main.py