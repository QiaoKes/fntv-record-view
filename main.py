from flask import Flask, render_template, request, jsonify
import sqlite3
import os
import sys # ### NEW ###
import logging
from datetime import datetime
import threading
import time
from contextlib import contextmanager
from queue import Queue, Empty, Full
from typing import Iterator
import random
import shutil

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 1. 获取当前文件 (main.py) 所在的目录的绝对路径
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
SRC_DB_PATH = os.path.join(BASE_DIR, 'database', 'trimmedia.db')
TMP_DB_PATH = os.path.join(BASE_DIR, 'trimmedia_tmp.db')

# ### NEW: Configuration for lazy, atomic copy ###
DB_EXPIRATION_SECONDS = 60  # 数据库副本的过期时间（60秒）
_last_copy_time = 0.0       # 上次拷贝成功的时间戳，初始化为0以强制首次拷贝
_db_copy_lock = threading.Lock() # 确保只有一个线程执行拷贝操作

def _atomic_copy_database():
    """
    ### NEW ###
    Performs an atomic copy of the database from the source to the temporary path.
    This is done by copying to a new file first, then renaming it.
    """
    global _last_copy_time
    logger.info("开始执行数据库原子化拷贝...")
    
    # 临时文件名，用于原子化操作
    atomic_tmp_path = TMP_DB_PATH + ".new"

    try:
        if not os.path.exists(SRC_DB_PATH):
            logger.warning(f"源数据库文件不存在: {SRC_DB_PATH}")
            return
        
        if not os.access(SRC_DB_PATH, os.R_OK):
            logger.warning(f"源数据库文件无法读取: {SRC_DB_PATH}")
            return
            
        # 1. 拷贝到带 .new 后缀的临时文件
        shutil.copy2(SRC_DB_PATH, atomic_tmp_path)
        
        # 2. 如果拷贝成功，原子化地重命名文件
        os.rename(atomic_tmp_path, TMP_DB_PATH)
        
        # 3. 仅在完全成功后更新时间戳
        _last_copy_time = time.time()
        logger.info(f"数据库原子化拷贝成功: {SRC_DB_PATH} -> {TMP_DB_PATH}")

    except Exception as e:
        logger.error(f"数据库原子化拷贝失败: {e}")
        # 如果新文件已创建但重命名失败，清理掉
        if os.path.exists(atomic_tmp_path):
            try:
                os.remove(atomic_tmp_path)
            except OSError as rm_err:
                logger.error(f"清理临时文件 {atomic_tmp_path} 失败: {rm_err}")

@contextmanager
def get_db_connection() -> Iterator[sqlite3.Connection]:
    """
    ### CHANGED: Implemented lazy loading with expiration and thread safety ###
    
    为每个请求创建一个新的只读数据库连接。
    在连接前，会检查临时数据库是否已过期或不存在。如果需要，会触发一次
    线程安全的、原子化的数据库拷贝。
    """
    
    # 检查数据库副本是否过期或不存在
    is_expired = (time.time() - _last_copy_time) > DB_EXPIRATION_SECONDS
    if not os.path.exists(TMP_DB_PATH) or is_expired:
        # 使用锁来防止多个请求同时触发拷贝 (Race Condition)
        with _db_copy_lock:
            # 双重检查：在获取锁后，再次检查是否需要拷贝
            # 因为可能在等待锁的时候，已经有另一个线程完成了拷贝
            is_still_expired = (time.time() - _last_copy_time) > DB_EXPIRATION_SECONDS
            if not os.path.exists(TMP_DB_PATH) or is_still_expired:
                if is_still_expired:
                    logger.info("数据库副本已过期，触发更新。")
                else:
                    logger.info("数据库副本不存在，触发更新。")
                _atomic_copy_database()

    # --- 以下为原始的连接逻辑 ---
    conn = None
    max_retries = 10
    base_delay = 0.05  # 50毫秒

    for attempt in range(max_retries):
        try:
            conn = sqlite3.connect(f"file:{TMP_DB_PATH}?mode=ro", uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            break
        except sqlite3.OperationalError:
            if attempt < max_retries - 1:
                time.sleep(base_delay)
            else:
                logger.error("多次重试后数据库仍然被锁定。")
                raise
    
    if not conn:
        raise sqlite3.OperationalError("无法建立数据库连接。")

    try:
        yield conn
    finally:
        if conn:
            conn.close()

# ===============================================================
# Flask 应用 (Flask Application)
# ===============================================================

app = Flask(__name__)

def get_item_hierarchy(conn, item_guid, cache=None):
    """获取项目的完整层级信息（带缓存）"""
    if cache is None:
        cache = {}
    
    if item_guid in cache:
        return cache[item_guid]
    
    # 一次性查询所有可能的父级项目
    hierarchy_query = '''
        WITH RECURSIVE item_hierarchy(guid, title, original_title, parent_guid, level) AS (
            -- 起始项目
            SELECT guid, title, original_title, parent_guid, 0 as level
            FROM item 
            WHERE guid = ?
            
            UNION ALL
            
            -- 递归查找父级
            SELECT i.guid, i.title, i.original_title, i.parent_guid, ih.level + 1
            FROM item i
            INNER JOIN item_hierarchy ih ON i.guid = ih.parent_guid
            WHERE ih.level < 10 AND i.guid IS NOT NULL  -- 增加递归深度并确保不为NULL
        )
        SELECT * FROM item_hierarchy ORDER BY level ASC
    '''
    
    items = conn.execute(hierarchy_query, (item_guid,)).fetchall()
    
    hierarchy = []
    for item in items:
        hierarchy.append({
            'guid': item['guid'],
            'title': item['title'],
            'original_title': item['original_title'],
            'parent_guid': item['parent_guid'],
            'level': item['level']
        })
    
    # 只缓存当前查询的项目层级信息
    cache[item_guid] = hierarchy
    
    return hierarchy

def format_timestamp(timestamp):
    """将时间戳转换为可读格式"""
    if timestamp:
        return datetime.fromtimestamp(timestamp / 1000).strftime('%Y-%m-%d %H:%M:%S')
    return ''

def format_duration(seconds):
    """将秒数转换为时分秒格式"""
    if not seconds:
        return '00:00:00'
    
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

@app.route('/')
def index():
    """主页面"""
    return render_template('index.html')

@app.route('/api/users')
def get_users():
    """获取所有用户列表"""
    with get_db_connection() as conn:
        users = conn.execute('''
            SELECT guid, username, last_login_time, is_admin, status
            FROM user 
            WHERE status = 1 AND guid != 'default-user-template'
            ORDER BY username
        ''').fetchall()
        
        user_list = []
        for user in users:
            user_list.append({
                'guid': user['guid'],
                'username': user['username'],
                'last_login': format_timestamp(user['last_login_time']),
                'is_admin': user['is_admin'],
                'status': user['status']
            })
        
        return jsonify(user_list)

@app.route('/api/play_history')
def get_play_history():
    """获取播放历史记录"""
    user_guid = request.args.get('user_guid', '')
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search_title = request.args.get('search_title', '').strip()
    start_time = request.args.get('start_time', '')
    end_time = request.args.get('end_time', '')
    
    with get_db_connection() as conn:
        # 构建查询条件
        where_clause = "WHERE iup.visible = 1"
        params = []
        
        if user_guid:
            where_clause += " AND iup.user_guid = ?"
            params.append(user_guid)
        
        # 模糊搜索剧集名称（包括父级项目名称）
        if search_title:
            # 添加调试日志
            logger.info(f"搜索关键词: {search_title}")
            
            # 使用 CTE 递归查询来搜索整个层级结构中的名称
            where_clause += """
                AND EXISTS (
                    WITH RECURSIVE item_hierarchy(guid, title, original_title, parent_guid, level) AS (
                        -- 起始项目
                        SELECT guid, title, original_title, parent_guid, 0 as level
                        FROM item 
                        WHERE guid = i.guid
                        
                        UNION ALL
                        
                        -- 递归查找父级
                        SELECT parent.guid, parent.title, parent.original_title, parent.parent_guid, ih.level + 1
                        FROM item parent
                        INNER JOIN item_hierarchy ih ON parent.guid = ih.parent_guid
                        WHERE ih.level < 10 AND parent.guid IS NOT NULL
                    )
                    SELECT 1 FROM item_hierarchy 
                    WHERE title LIKE ? OR original_title LIKE ?
                )
            """
            search_param = f"%{search_title}%"
            params.extend([search_param, search_param])
            logger.debug(f"搜索参数: {search_param}")
        
        # 播放时间范围筛选
        if start_time:
            try:
                # 解析时间格式 YYYY-MM-DD HH:MM:SS
                start_timestamp = int(datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S').timestamp() * 1000)
                where_clause += " AND iup.update_time >= ?"
                params.append(start_timestamp)
            except ValueError:
                logger.warning(f"无效的开始时间格式: {start_time}")
        
        if end_time:
            try:
                # 解析时间格式 YYYY-MM-DD HH:MM:SS
                end_timestamp = int(datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S').timestamp() * 1000)
                where_clause += " AND iup.update_time <= ?"
                params.append(end_timestamp)
            except ValueError:
                logger.warning(f"无效的结束时间格式: {end_time}")
        
        # 获取总数
        count_query = f'''
            SELECT COUNT(*) as total
            FROM item_user_play iup
            JOIN user u ON iup.user_guid = u.guid
            JOIN item i ON iup.item_guid = i.guid
            {where_clause}
        '''
        
        total = conn.execute(count_query, params).fetchone()['total']
        
        # 获取播放历史数据
        offset = (page - 1) * per_page
        query = f'''
            SELECT 
                iup.item_guid,
                iup.user_guid,
                iup.ts as position,
                iup.watched,
                iup.create_time,
                iup.update_time,
                iup.type as play_type,
                iup.resolution,
                u.username,
                i.title,
                i.original_title,
                i.overview,
                i.type as item_type,
                i.season_number,
                i.episode_number,
                i.parent_guid,
                i.runtime,
                i.release_date
            FROM item_user_play iup
            JOIN user u ON iup.user_guid = u.guid
            JOIN item i ON iup.item_guid = i.guid
            {where_clause}
            ORDER BY iup.update_time DESC
            LIMIT ? OFFSET ?
        '''
        
        params.extend([per_page, offset])
        history = conn.execute(query, params).fetchall()
        
        # 批量获取层级信息
        hierarchy_cache = {}
        history_list = []
        
        for record in history:
            # 获取完整的层级信息
            hierarchy = get_item_hierarchy(conn, record['item_guid'], hierarchy_cache)
            
            # 构建显示标题
            display_title = record['title']
            series_info = ""
            
            if len(hierarchy) > 1:  # 有父级项目
                # 找到最顶层的剧集名称（层级最高的）
                root_item = hierarchy[-1]  # 最后一个是根项目
                series_info = root_item['title']
                
                # 构建完整标题
                if record['season_number'] and record['episode_number']:
                    # 如果有季数和集数，显示完整格式
                    display_title = f"{series_info} - S{record['season_number']:02d}E{record['episode_number']:02d} - {record['title']}"
                elif record['title'] != series_info:
                    # 如果集名和剧名不同，显示剧名 - 集名
                    display_title = f"{series_info} - {record['title']}"
            elif record['season_number'] and record['episode_number']:
                # 没有层级信息但有季集数据，使用集名作为基础
                display_title = f"S{record['season_number']:02d}E{record['episode_number']:02d} - {record['title']}"
            
            # 计算观看进度百分比
            # 注意：item.runtime 是分钟，position 是秒，需要转换
            runtime_seconds = record['runtime'] * 60 if record['runtime'] else 0
            progress = 0
            if runtime_seconds and record['position'] and runtime_seconds > 0:
                # 确保进度不超过100%
                progress = min(100, (record['position'] / runtime_seconds) * 100)
            
            # 判断是否为剧集
            is_episode = record['season_number'] is not None and record['episode_number'] is not None
            
            history_list.append({
                'item_guid': record['item_guid'],
                'user_guid': record['user_guid'],
                'username': record['username'],
                'title': display_title,
                'original_title': record['original_title'],
                'overview': record['overview'],
                'type': record['item_type'],
                'play_type': record['play_type'],
                'season_number': record['season_number'],
                'episode_number': record['episode_number'],
                'series_title': series_info,
                'hierarchy': [{'title': h['title'], 'level': h['level']} for h in hierarchy],
                'position': record['position'],
                'position_formatted': format_duration(record['position']),
                'runtime': runtime_seconds,
                'runtime_formatted': format_duration(runtime_seconds),
                'progress': round(progress, 1),
                'watched': bool(record['watched']),  # 确保是布尔值
                'resolution': record['resolution'],
                'create_time': format_timestamp(record['create_time']),
                'update_time': format_timestamp(record['update_time']),
                'release_date': record['release_date'],
                'is_episode': is_episode
            })
        
        return jsonify({
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page,
            'data': history_list
        })

@app.route('/api/stats')
def get_stats():
    """获取统计数据"""
    with get_db_connection() as conn:
        # 总用户数
        total_users = conn.execute('''
            SELECT COUNT(*) as count 
            FROM user 
            WHERE status = 1 AND guid != 'default-user-template'
        ''').fetchone()['count']
        
        # 总播放记录数（只统计能正确JOIN到item和user表的记录）
        total_plays = conn.execute('''
            SELECT COUNT(*) as count 
            FROM item_user_play iup
            JOIN user u ON iup.user_guid = u.guid
            JOIN item i ON iup.item_guid = i.guid
            WHERE iup.visible = 1
        ''').fetchone()['count']
        
        # 活跃用户数（有播放记录的用户）
        active_users = conn.execute('''
            SELECT COUNT(DISTINCT user_guid) as count 
            FROM item_user_play 
            WHERE visible = 1
        ''').fetchone()['count']
        
        # 最新播放时间
        latest_play = conn.execute('''
            SELECT MAX(update_time) as latest 
            FROM item_user_play 
            WHERE visible = 1
        ''').fetchone()['latest']
        
        # 今日播放数（只统计能正确JOIN到item和user表的记录）
        today_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        today_plays = conn.execute('''
            SELECT COUNT(*) as count 
            FROM item_user_play iup
            JOIN user u ON iup.user_guid = u.guid
            JOIN item i ON iup.item_guid = i.guid
            WHERE iup.visible = 1 AND iup.update_time >= ?
        ''', (today_start,)).fetchone()['count']
        
        return jsonify({
            'total_users': total_users,
            'total_plays': total_plays,
            'active_users': active_users,
            'today_plays': today_plays,
            'latest_play': format_timestamp(latest_play)
        })

@app.route('/api/user_activity')
def get_user_activity():
    """获取用户活动数据"""
    with get_db_connection() as conn:
        # 用户播放次数统计
        user_stats = conn.execute('''
            SELECT 
                u.username,
                u.guid,
                COUNT(iup.item_guid) as play_count,
                SUM(iup.ts) as total_seconds,
                MAX(iup.update_time) as last_play
            FROM user u
            LEFT JOIN item_user_play iup ON u.guid = iup.user_guid AND iup.visible = 1
            WHERE u.status = 1 AND u.guid != 'default-user-template'
            GROUP BY u.guid, u.username
            ORDER BY play_count DESC
            LIMIT 10
        ''').fetchall()
        
        activity_data = []
        for user in user_stats:
            activity_data.append({
                'username': user['username'],
                'play_count': user['play_count'] or 0,
                'total_hours': round((user['total_seconds'] or 0) / 3600, 1),
                'last_play': format_timestamp(user['last_play'])
            })
        
        return jsonify(activity_data)

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("启动飞牛影视观看历史管理系统")
    logger.info("=" * 50)
    logger.info("访问地址: http://127.0.0.1:5000")
    logger.info("Flask 运行模式: 串行处理 (单线程)")
    
    logger.info("所有组件已启动，服务运行中...")
    logger.info("=" * 50)
    
    app.run(host='0.0.0.0', port=5000)