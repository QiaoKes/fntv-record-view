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

# ===============================================================
# 配置部分 (Configuration)
# ===============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ### FIX ###: 确保路径包含一个目录，例如 'data/'
# 源数据库路径
SOURCE_DB_PATH = "database/trimmedia.db" 
# 目标数据库路径（在当前目录下的 data 文件夹内）
LOCAL_DB_PATH = "./local_trimmedia.db" 
# 备份频率（秒）
BACKUP_INTERVAL_SECONDS = 60 # 1分钟

# ===============================================================
# 数据库连接池 (Database Connection Pool)
# ===============================================================

# ResettableConnectionPool 类的代码保持不变，它本身没有问题
class ResettableConnectionPool:
    def __init__(self, db_path, max_connections=5):
        self.db_path = db_path
        self.max_connections = max_connections
        self._pool = None
        self._lock = threading.Lock()
        
        # 确保数据库目录存在
        # 这个操作现在是安全的，因为我们保证了 LOCAL_DB_PATH 包含目录
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        self._create_pool()

    def _create_pool(self):
        """(私有) 创建或重新创建连接池。此方法应在锁内调用。"""
        logger.info(f"正在为数据库 {self.db_path} 创建新的连接池...")
        self._pool = Queue(maxsize=self.max_connections)
        try:
            for _ in range(self.max_connections):
                conn = self._create_connection()
                self._pool.put_nowait(conn)
            logger.info("新连接池创建成功。")
        except sqlite3.OperationalError as e:
            logger.error(f"数据库文件 {self.db_path} 无法访问或不存在，连接池创建失败: {e}")
        except Exception as e:
            logger.error(f"创建连接池失败: {e}")

    def _create_connection(self):
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def get_conn(self, timeout=5):
        try:
            return self._pool.get(timeout=timeout)
        except Empty:
            logger.warning("连接池为空，正在创建临时连接。")
            return self._create_connection()

    def return_conn(self, conn):
        try:
            self._pool.put_nowait(conn)
        except Full:
            conn.close()

    def reset(self):
        with self._lock:
            logger.info("请求重置连接池...")
            old_pool = self._pool
            self._create_pool()
            
            logger.info("正在关闭所有旧的数据库连接...")
            while not old_pool.empty():
                try:
                    conn = old_pool.get_nowait()
                    conn.close()
                except Empty:
                    break
            logger.info("连接池重置完成。")

# --- get_db_connection 上下文管理器保持不变 ---
@contextmanager
def get_db_connection() -> Iterator[sqlite3.Connection]:
    conn = None
    try:
        conn = db_pool.get_conn()
        yield conn
    except sqlite3.OperationalError as e:
        logger.error(f"数据库操作失败: {e}")
        raise
    finally:
        if conn:
            db_pool.return_conn(conn)

# ===============================================================
# 后台备份逻辑 (Background Backup Logic)
# ===============================================================

def perform_backup():
    logger.info(f"开始备份: {SOURCE_DB_PATH} -> {LOCAL_DB_PATH}")
    source_conn = None
    for attempt in range(5):
        try:
            source_conn = sqlite3.connect(f"file:{SOURCE_DB_PATH}?mode=ro", uri=True)
            break
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < 2:
                logger.warning(f"源数据库被锁定，10秒后重试...")
                time.sleep(1)
            else:
                logger.error(f"连接源数据库时发生错误: {e}")
                return False
    
    if not source_conn:
        logger.error("备份失败：多次尝试后源数据库仍然被锁定。")
        return False

    dest_conn = sqlite3.connect(LOCAL_DB_PATH)
    try:
        with dest_conn:
            source_conn.backup(dest_conn)
        logger.info("数据库备份成功！")
        return True
    except Exception as e:
        logger.error(f"备份过程中发生错误: {e}")
        return False
    finally:
        source_conn.close()
        dest_conn.close()

# ### MODIFIED ###: 后台任务不再负责首次备份
def background_backup_task(pool: ResettableConnectionPool, interval: int):
    """
    在后台定期执行备份任务的函数。
    """
    logger.info("后台备份线程已启动，将按计划执行后续备份。")
    while True:
        time.sleep(interval)
        logger.info("定时备份时间到，开始执行备份...")
        if perform_backup():
            logger.info("定时备份成功，通知连接池刷新连接。")
            pool.reset()
        else:
            logger.warning("定时备份失败，将在下一个备份周期重新尝试。")

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

    # ### MODIFIED ###: 采用新的、更安全的启动流程

    # 步骤 1: 在主线程中执行首次备份
    logger.info("正在执行首次启动备份...")
    if not perform_backup():
        logger.critical("首次备份失败！应用程序无法启动。")
        sys.exit(1) # 退出程序
    
    logger.info("首次备份成功，本地数据库已就绪。")

    # 步骤 2: 首次备份成功后，安全地创建连接池
    logger.info("正在初始化数据库连接池...")
    db_pool = ResettableConnectionPool(LOCAL_DB_PATH)

    # 步骤 3: 启动后台线程，负责未来的周期性备份
    logger.info(f"启动后台备份线程，备份间隔: {BACKUP_INTERVAL_SECONDS} 秒")
    backup_thread = threading.Thread(
        target=background_backup_task,
        args=(db_pool, BACKUP_INTERVAL_SECONDS),
        name="BackupThread"
    )
    backup_thread.daemon = True
    backup_thread.start()
    
    # 步骤 4: 启动 Flask 应用
    logger.info("=" * 50)
    logger.info("访问地址: http://127.0.0.1:5000")
    logger.info("所有组件已启动，服务运行中...")
    logger.info("=" * 50)
    
    app.run(host='0.0.0.0', port=5000)