"""
数据采集服务模块
用于管理数据初始化和更新的业务逻辑
"""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from utils.db_initializer import DatabaseInitializer
from utils.akshare_fetcher import AKShareFetcher
from utils.db_manager import DBManager
from utils.trading_time_validator import TradingTimeValidator
from utils.new_stock_detector import NewStockDetector
from utils.stock_data_fetcher import StockDataFetcher
from utils.data_initializer import DataInitializer
from utils.kline_updater import KlineUpdater
from utils.fund_flow_updater import FundFlowUpdater
from utils.fund_flow_fetcher import FundFlowFetcher
from datetime import timedelta

# 配置日志
logger = logging.getLogger(__name__)


class UpdateCancelled(Exception):
    """数据更新被用户取消"""


class DataCollectionService:
    """数据采集服务类"""
    
    def __init__(self, data_dir: str = 'data'):
        """
        初始化数据采集服务
        
        Args:
            data_dir: 数据目录路径
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # 初始化各个管理器
        self.db_initializer = DatabaseInitializer(str(self.data_dir))
        self.akshare_fetcher = AKShareFetcher()
        
        # 数据库路径
        self.selection_db_path = self.data_dir / 'stock_selection.db'
        
        # 初始化数据库管理器
        from utils.global_db import get_global_db
        self.db_manager = get_global_db()
        
        # 初始化股票数据获取器
        self.stock_data_fetcher = StockDataFetcher()
        
        # 初始化状态
        self.init_status = {
            'running': False,
            'paused': False,
            'progress': 0,
            'total': 0,
            'current_task': '',
            'success': 0,
            'failed': 0,
            'message': '',
            'start_time': None,
            'end_time': None,
            'logs': [],
            'status': 'idle',  # idle, running, paused, completed, failed, cancelled
            'statistics': {}
        }
        
        # 更新状态
        self.update_status = {
            'running': False,
            'paused': False,
            'progress': 0,
            'total': 0,
            'current_task': '',
            'scope': 'watched',
            'critical_failure': False,
            'already_latest': False,
            'success': 0,
            'failed': 0,
            'message': '',
            'start_time': None,
            'end_time': None,
            'logs': [],
            'status': 'idle',  # idle, running, paused, completed, failed, cancelled
            'statistics': {},
            'tasks': [],  # 任务列表
            'totalStats': {  # 统计数据
                'added': 0,
                'updated': 0,
                'deleted': 0,
                'processed': 0,
                'new_stock_detected': 0,
                'new_stock_initialized': 0
            }
        }
        
        # 线程锁
        self.init_lock = threading.Lock()
        self.update_lock = threading.Lock()
        # 协作式取消/暂停信号
        self.update_cancel_event = threading.Event()
        self.update_pause_event = threading.Event()
    
    def get_init_config(self) -> Dict[str, Any]:
        """
        获取初始化配置
        
        Returns:
            dict: 初始化配置信息
        """
        watched_count = 0
        try:
            row = self.db_manager.query_one(
                "SELECT COUNT(*) AS count FROM watched_industry"
            )
            watched_count = (row or {}).get("count", 0)
        except Exception:
            watched_count = 0

        return {
            'types': ['full', 'structure_only', 'custom'],
            'defaultType': 'custom',
            'scope': 'watched_industries',
            'scopeDescription': '仅初始化已关注行业、行业指数和行业成分股',
            'watchedCount': watched_count,
            'customOptions': {
                'structure': True,
                'basicData': True,
                'historyData': True,
                'industryData': True,
                'sectorData': True,
                'fundFlowData': True
            },
            'dateRangeDefault': {
                'start': '2024-01-01',
                'end': datetime.now().strftime('%Y-%m-%d')
            }
        }
    
    def get_update_config(self) -> Dict[str, Any]:
        """
        获取更新配置
        
        Returns:
            dict: 更新配置信息
        """
        watched_count = 0
        try:
            row = self.db_manager.query_one(
                "SELECT COUNT(*) AS count FROM watched_industry"
            )
            watched_count = (row or {}).get("count", 0)
        except Exception:
            watched_count = 0

        return {
            'scopes': [
                {'value': 'watched', 'label': '关注行业及成分股'},
                {'value': 'all', 'label': '全市场'},
            ],
            'defaultScope': 'watched',
            'watchedCount': watched_count,
            'updateTypes': [
                'kline',
                'fund_flow',
                'market_cap',
                'new_stock'
            ],
            'defaultUpdateTypes': [
                'kline',
                'fund_flow',
                'market_cap',
                'new_stock'
            ],
            'updateFrequency': {
                'kline': 'daily',
                'fund_flow': 'daily',
                'market_cap': 'daily',
                'new_stock': 'daily'
            }
        }
    
    def _check_data_initialized(self):
        """
        检查股票基础数据和K线数据是否已初始化
        
        Returns:
            bool: 已初始化返回True，否则返回False
        """
        try:
            # 检查stock_basic表是否有数据
            basic_count = 0
            try:
                result = self.db_manager.query_one("SELECT COUNT(*) as count FROM stock_basic")
                if result and result.get('count', 0) > 0:
                    basic_count = result['count']
            except Exception as e:
                logger.debug(f"检查stock_basic表失败: {str(e)}")
            
            # 检查stock_kline表是否有数据
            kline_count = 0
            try:
                result = self.db_manager.query_one("SELECT COUNT(*) as count FROM stock_kline")
                if result and result.get('count', 0) > 0:
                    kline_count = result['count']
            except Exception as e:
                logger.debug(f"检查stock_kline表失败: {str(e)}")
            
            # 如果两个表都有数据，说明已初始化
            if basic_count > 0 and kline_count > 0:
                logger.info(f"数据已初始化: stock_basic={basic_count}条, stock_kline={kline_count}条")
                return True
            
            return False
            
        except Exception as e:
            logger.warning(f"检查数据是否已初始化失败: {str(e)}")
            return False

    def check_data_completeness(self) -> Dict[str, Any]:
        """
        检查数据完整性
        
        Returns:
            dict: 各数据表的完整性信息
        """
        result = {
            'success': True,
            'data': {
                'basic': {'table': 'stock_basic', 'count': 0, 'complete': False},
                'kline': {'table': 'stock_kline', 'count': 0, 'complete': False}
            },
            'lastUpdate': None
        }
        
        try:
            # 检查 stock_basic 表
            r = self.db_manager.query_one("SELECT COUNT(*) as count FROM stock_basic")
            basic_count = r['count'] if r else 0
            result['data']['basic']['count'] = basic_count
            result['data']['basic']['complete'] = basic_count > 0
            
            # 检查 stock_kline 表
            r = self.db_manager.query_one("SELECT COUNT(*) as count FROM stock_kline")
            kline_count = r['count'] if r else 0
            result['data']['kline']['count'] = kline_count
            result['data']['kline']['complete'] = kline_count > 0
            
            # 获取最后更新时间
            r = self.db_manager.query_one(
                "SELECT MAX(date) as last_date FROM stock_kline"
            )
            result['lastUpdate'] = r['last_date'] if r and r.get('last_date') else None
            
            # 获取股票数量
            r = self.db_manager.query_one("SELECT COUNT(DISTINCT code) as count FROM stock_kline")
            stock_count = r['count'] if r else 0
            result['data']['kline']['stockCount'] = stock_count
            
        except Exception as e:
            result['success'] = False
            logger.error(f"检查数据完整性失败: {e}")
        
        return result
    
    def get_data_status(self) -> Dict[str, Any]:
        """
        获取数据状态摘要
        
        Returns:
            dict: 数据状态信息
        """
        completeness = self.check_data_completeness()
        
        return {
            'initialized': completeness['data']['basic']['complete'],
            'basicCount': completeness['data']['basic']['count'],
            'klineCount': completeness['data']['kline']['count'],
            'klineStockCount': completeness['data'].get('kline', {}).get('stockCount', 0),
            'lastUpdate': completeness.get('lastUpdate')
        }
    
    def start_reinit(self, stock_count: int = None, kline_days: int = None) -> Dict[str, Any]:
        """
        强制重新初始化（删除现有数据，重新初始化）
        
        Args:
            stock_count: 初始化股票数量（默认2000）
            kline_days: K线历史天数（默认250）
        
        Returns:
            dict: 任务信息
        """
        if stock_count is None:
            stock_count = 2000
        if kline_days is None:
            kline_days = 250
            
        if self.init_status['running']:
            return {
                'success': False,
                'message': '已有初始化任务正在运行'
            }

        # 重新初始化会清空 stock_basic/stock_kline，先确认有可重建的关注范围
        try:
            row = self.db_manager.query_one(
                "SELECT COUNT(*) AS count FROM watched_industry"
            )
            watched_count = (row or {}).get("count", 0)
        except Exception:
            watched_count = 0
        if watched_count == 0:
            return {
                'success': False,
                'message': '请先在「行业数据」页关注至少 1 个行业，再执行重新初始化'
            }

        task_id = f"REINIT_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        thread = threading.Thread(
            target=self._run_reinit,
            args=(task_id, stock_count, kline_days),
            daemon=True
        )
        thread.start()
        
        return {
            'success': True,
            'message': '重新初始化任务已启动',
            'taskId': task_id
        }
    
    def _run_reinit(self, task_id: str, stock_count: int, kline_days: int):
        """
        执行强制重新初始化（在后台线程中运行）
        
        Args:
            task_id: 任务ID
            stock_count: 股票数量（忽略，使用全量）
            kline_days: K线天数（转换为年数，3年约750天）
        """
        with self.init_lock:
            if self.init_status['running']:
                logger.warning(f"重新初始化任务 {task_id} 已在运行")
                return
            
            try:
                self.init_status['running'] = True
                self.init_status['paused'] = False
                self.init_status['status'] = 'running'
                self.init_status['progress'] = 0
                self.init_status['start_time'] = datetime.now().isoformat()
                self.init_status['logs'] = []
                self.init_status['tasks'] = []
                
                self._add_init_log(f"⚠ 重新初始化任务 {task_id} 已启动")
                self._add_init_log("  - 清空后只重建关注行业及成分股，K线 3 年")
                
                try:
                    from web_server import emit_init_progress
                    emit_init_progress()
                except ImportError:
                    pass
                
                # 步骤1: 删除现有数据
                self._add_init_log("⟳ 正在删除现有数据...")
                self._delete_all_data()
                self._add_init_log("✓ 已删除现有数据")
                self._update_progress(10)
                
                # 步骤2: 只重建关注行业及成分股
                self._add_init_log("⟳ 正在重建关注行业及成分股...")
                self._initialize_watched_scope()
                self._add_init_log("✓ 关注范围重新初始化完成")
                self._update_progress(100)
                
                self.init_status['status'] = 'completed'
                self.init_status['end_time'] = datetime.now().isoformat()
                self._add_init_log("✓ 重新初始化全部完成")
                
            except Exception as e:
                self.init_status['status'] = 'failed'
                self.init_status['end_time'] = datetime.now().isoformat()
                self._add_init_log(f"✗ 重新初始化失败: {str(e)}")
                logger.error(f"重新初始化失败: {e}")
    
    def _delete_all_data(self):
        """删除 stock_kline / stock_basic 表内容并提交。

        注意：DBManager.execute() 不会自动提交。若这里不放进事务，
        DELETE 会在连接上开启隐式事务；后续 DataInitializer 执行
        BEGIN IMMEDIATE 时会触发“脏连接”回滚，把删除操作一并回滚，
        导致重新初始化后旧的全量数据仍留在库中。
        """
        try:
            with self.db_manager.transaction():
                self.db_manager.execute("DELETE FROM stock_kline")
                self.db_manager.execute("DELETE FROM stock_basic")
            logger.info("已清空并提交 stock_kline 和 stock_basic 表")
        except Exception as e:
            logger.error(f"删除数据失败: {e}")
            raise
    
    def _update_progress(self, progress: int):
        """更新进度"""
        self.init_status['progress'] = progress
        try:
            from web_server import emit_init_progress
            emit_init_progress()
        except ImportError:
            pass
    
    def _add_init_log(self, message: str):
        """添加初始化日志"""
        log_entry = f"{datetime.now().strftime('%H:%M:%S')} - {message}"
        self.init_status['logs'].append(log_entry)
        logger.info(message)
    
    def start_initialization(self, init_type: str = 'full', options: Optional[Dict] = None):
        """
        开始数据初始化
        
        Args:
            init_type: 初始化类型 (full, structure_only, custom)
            options: 自定义选项
        
        Returns:
            dict: 初始化任务信息
        """
        # 检查是否已有初始化任务运行
        if self.init_status['running']:
            return {
                'success': False,
                'message': '已有初始化任务正在运行',
                'taskId': None
            }
        
        # 初始化范围收敛为关注行业及成分股：至少要有 1 个关注行业
        try:
            row = self.db_manager.query_one(
                "SELECT COUNT(*) AS count FROM watched_industry"
            )
            watched_count = (row or {}).get("count", 0)
        except Exception:
            watched_count = 0
        if watched_count == 0:
            return {
                'success': False,
                'message': '请先在「行业数据」页关注至少 1 个行业',
                'taskId': None
            }
        
        # 生成任务ID
        task_id = f"INIT_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        # 在后台线程中执行初始化
        thread = threading.Thread(
            target=self._run_initialization,
            args=(task_id, init_type, options),
            daemon=True
        )
        thread.start()
        
        return {
            'success': True,
            'message': '初始化任务已启动',
            'taskId': task_id
        }
    
    def _resolve_watched_universe(self):
        """解析初始化范围：关注行业 + 行业成分股"""
        from trading.industry_rotation import IndustryRotationService

        rotation = IndustryRotationService()
        industries = rotation.get_watched_industries()
        if not industries:
            raise RuntimeError("尚未关注任何行业，请先在「行业数据」页添加关注行业")

        stock_dict = {}
        total = len(industries)
        for index, industry in enumerate(industries):
            self.init_status['current_task'] = (
                f"抓取行业成分股 {index + 1}/{total}：{industry['industry_name']}"
            )
            self._update_progress(10 + int(4 * (index + 1) / max(total, 1)))
            items = rotation.get_constituents(industry, limit=10000)
            for item in items:
                stock_dict[item["stock_code"]] = item["stock_name"]

        try:
            deleted_codes = set(self.db_manager.get_deleted_stock_codes())
        except Exception:
            deleted_codes = set()
        stock_codes = [code for code in stock_dict if code not in deleted_codes]
        if not stock_codes:
            raise RuntimeError("关注行业的成分股为空，无法初始化")

        self._add_init_log(
            f"初始化范围：{len(industries)} 个关注行业，{len(stock_codes)} 只成分股"
        )
        return rotation, industries, stock_codes, stock_dict

    def _initialize_watched_scope(self):
        """只初始化关注行业指数 + 成分股基础数据 + 成分股 K 线"""
        rotation, industries, stock_codes, stock_dict = self._resolve_watched_universe()

        def progress_cb(progress_pct, message):
            self.init_status['current_task'] = message
            self._update_progress(max(10, min(85, int(progress_pct))))

        data_initializer = DataInitializer(
            self.db_manager,
            self.stock_data_fetcher,
            None,
            None,
            progress_callback=progress_cb,
        )

        self.init_status['current_task'] = f"基础数据：{len(stock_codes)} 只成分股"
        self._add_init_log(f"⟳ 初始化成分股基础数据：{len(stock_codes)} 只")
        self._update_progress(10)
        data_initializer._init_basic_data(stock_codes, stock_dict)
        self._update_progress(15)

        self.init_status['current_task'] = f"K线数据：{len(stock_codes)} 只成分股（3年）"
        self._add_init_log(f"⟳ 初始化成分股 K 线：{len(stock_codes)} 只，3 年（强制全量）")
        data_initializer._init_kline_history_data(
            stock_codes,
            years=3,
            progress_range=(15, 85),
            force_full=True,
        )

        self.init_status['current_task'] = "补齐关注行业指数（10年）"
        self._add_init_log(f"⟳ 补齐关注行业指数：{len(industries)} 个行业（10年）")
        self._update_progress(88)
        rotation.ensure_industry_index_data(industries, years=10)
        self._update_progress(100)
        self._add_init_log("✓ 关注行业、行业指数、成分股数据初始化完成")

    def _run_initialization(self, task_id: str, init_type: str, options: Optional[Dict]):
        """
        执行初始化任务（在后台线程中运行）
        
        Args:
            task_id: 任务ID
            init_type: 初始化类型
            options: 自定义选项
        """
        with self.init_lock:
            # 检查是否已有初始化任务在运行
            if self.init_status['running']:
                logger.warning(f"初始化任务 {task_id} 已在运行，跳过执行")
                return
                
            try:
                # 初始化状态
                self.init_status['running'] = True
                self.init_status['paused'] = False
                self.init_status['status'] = 'running'
                self.init_status['progress'] = 0
                self.init_status['success'] = 0
                self.init_status['failed'] = 0
                self.init_status['start_time'] = datetime.now().isoformat()
                self.init_status['logs'] = []
                self.init_status['statistics'] = {}
                
                # 记录日志
                self._add_init_log(f"✓ 初始化任务 {task_id} 已启动")
                
                # 尝试导入并调用WebSocket推送函数
                try:
                    from web_server import emit_init_progress
                    emit_init_progress()
                except ImportError:
                    pass
                
                # 初始化范围：关注行业 + 行业指数 + 行业成分股
                self._add_init_log("⟳ 正在初始化关注行业及成分股...")
                self._initialize_watched_scope()
                
                # 完成
                self.init_status['progress'] = 100
                self.init_status['end_time'] = datetime.now().isoformat()
                self.init_status['message'] = '初始化完成'
                self.init_status['status'] = 'completed'
                self.init_status['success'] = 1
                self._add_init_log("✓ 初始化任务完成")
                
                # 更新统计信息
                self.init_status['statistics'] = self.get_tables_stats()
                
                # 尝试导入并调用WebSocket推送函数
                try:
                    from web_server import emit_init_progress
                    emit_init_progress()
                except ImportError:
                    pass
                
                logger.info(f"初始化任务 {task_id} 完成")
                
            except Exception as e:
                self.init_status['failed'] += 1
                self.init_status['message'] = f'初始化失败: {str(e)}'
                self.init_status['status'] = 'failed'
                self._add_init_log(f"✗ 错误: {str(e)}")
                
                # 尝试导入并调用WebSocket推送函数
                try:
                    from web_server import emit_init_progress
                    emit_init_progress()
                except ImportError:
                    pass
                
                logger.error(f"初始化任务 {task_id} 失败: {str(e)}")
            
            finally:
                self.init_status['running'] = False
                
                # 尝试导入并调用WebSocket推送函数
                try:
                    from web_server import emit_init_progress
                    emit_init_progress()
                except ImportError:
                    pass
    
    def get_init_progress(self) -> Dict[str, Any]:
        """
        获取初始化进度
        
        Returns:
            dict: 初始化进度信息，包含前端期望的所有字段
        """
        # 返回前端期望的格式
        return {
            'status': self.init_status['status'],
            'progress': self.init_status['progress'],
            'currentTask': self.init_status['current_task'],
            'currentTaskName': self.init_status['current_task'],
            'currentTaskProgress': 0,
            'currentTaskTotal': 0,
            'speed': 0,
            'estimatedTime': 0,
            'logs': self.init_status['logs'][-50:],  # 返回最后50条日志
            'statistics': self.init_status['statistics'],
            'message': self.init_status['message'],
            'running': self.init_status['running'],
            'paused': self.init_status['paused']
        }
    
    def cancel_initialization(self) -> Dict[str, Any]:
        """
        取消初始化任务
        
        Returns:
            dict: 取消结果
        """
        if not self.init_status['running']:
            return {
                'success': False,
                'message': '没有正在运行的初始化任务'
            }
        
        # 标记为取消
        self.init_status['running'] = False
        self.init_status['status'] = 'cancelled'
        self.init_status['message'] = '初始化已取消'
        self._add_init_log("✗ 初始化任务已取消")
        
        return {
            'success': True,
            'message': '初始化任务已取消'
        }
    
    def pause_initialization(self) -> Dict[str, Any]:
        """
        暂停初始化任务
        
        Returns:
            dict: 暂停结果
        """
        if not self.init_status['running']:
            return {
                'success': False,
                'message': '没有正在运行的初始化任务'
            }
        
        # 标记为暂停
        self.init_status['paused'] = True
        self.init_status['status'] = 'paused'
        self._add_init_log("⏸ 初始化任务已暂停")
        
        return {
            'success': True,
            'message': '初始化任务已暂停'
        }
    
    def resume_initialization(self) -> Dict[str, Any]:
        """
        恢复初始化任务
        
        Returns:
            dict: 恢复结果
        """
        if not self.init_status['paused']:
            return {
                'success': False,
                'message': '没有暂停的初始化任务'
            }
        
        # 标记为继续运行
        self.init_status['paused'] = False
        self.init_status['status'] = 'running'
        self._add_init_log("▶ 初始化任务已恢复")
        
        return {
            'success': True,
            'message': '初始化任务已恢复'
        }
    
    def start_update(self, update_types: Optional[List[str]] = None,
                     scope: str = 'watched') -> Dict[str, Any]:
        """
        开始数据更新
        
        Args:
            update_types: 更新类型列表
        
        Returns:
            dict: 更新任务信息
        """
        # 检查是否已有更新任务运行
        if self.update_status['running']:
            return {
                'success': False,
                'message': '已有更新任务正在运行',
                'taskId': None
            }
        
        # 关注范围模式要求至少有一个关注行业
        scope = 'all' if str(scope).lower() == 'all' else 'watched'
        if scope == 'watched':
            try:
                row = self.db_manager.query_one(
                    "SELECT COUNT(*) AS count FROM watched_industry"
                )
                watched_count = (row or {}).get("count", 0)
            except Exception:
                watched_count = 0
            if watched_count == 0:
                return {
                    'success': False,
                    'message': '请先在「行业数据」页关注至少 1 个行业',
                    'taskId': None
                }

        # 清理上次取消/暂停信号
        self.update_cancel_event.clear()
        self.update_pause_event.clear()

        # 生成任务ID
        task_id = f"UPDATE_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        # 在后台线程中执行更新
        thread = threading.Thread(
            target=self._run_update,
            args=(task_id, update_types, scope),
            daemon=True
        )
        thread.start()
        
        return {
            'success': True,
            'message': '更新任务已启动',
            'taskId': task_id
        }
    
    def _set_update_progress(self, progress: int, message: str):
        """更新进度与当前任务（轮询接口读取）"""
        self.update_status['progress'] = max(0, min(100, int(progress)))
        self.update_status['current_task'] = message
        self.update_status['message'] = message

    def _check_update_control(self):
        """检查取消/暂停信号；暂停时阻塞，取消时抛异常"""
        if self.update_cancel_event.is_set():
            raise UpdateCancelled("更新已取消")
        while self.update_pause_event.is_set():
            if self.update_cancel_event.is_set():
                raise UpdateCancelled("更新已取消")
            time.sleep(0.5)

    def _resolve_update_stock_codes(self) -> List[str]:
        """关注范围模式的股票池：关注行业 → 行业成分股"""
        from trading.industry_rotation import IndustryRotationService

        rotation = IndustryRotationService()
        industries = rotation.get_watched_industries()
        if not industries:
            raise RuntimeError("尚未关注任何行业，请先在「行业数据」页添加关注行业")

        codes = set()
        total = len(industries)
        for index, industry in enumerate(industries):
            self._check_update_control()
            self._set_update_progress(
                8 + int(7 * (index + 1) / max(total, 1)),
                f"解析关注行业成分股 {index + 1}/{total}：{industry['industry_name']}",
            )
            for item in rotation.get_constituents(industry, limit=10000):
                codes.add(item["stock_code"])

        try:
            deleted_codes = set(self.db_manager.get_deleted_stock_codes())
        except Exception:
            deleted_codes = set()
        codes -= deleted_codes
        self._add_update_log(f"关注范围股票池: {len(codes)} 只（所属 {total} 个行业）")
        return sorted(codes)

    def _get_kline_coverage(self, stock_codes: List[str], target_date: str):
        """返回 (已有目标日期的股票数, 总数)"""
        if not stock_codes:
            return 0, 0
        covered = 0
        for offset in range(0, len(stock_codes), 500):
            chunk = stock_codes[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            try:
                rows = self.db_manager.query(
                    f"""
                    SELECT code, MAX(date) AS max_date
                    FROM stock_kline
                    WHERE code IN ({placeholders})
                    GROUP BY code
                    """,
                    tuple(chunk),
                )
                for row in rows:
                    if str(row.get("max_date") or "") >= target_date:
                        covered += 1
            except Exception as exc:
                logger.warning("检查K线覆盖失败: %s", exc)
        return covered, len(stock_codes)

    def pause_update(self) -> Dict[str, Any]:
        """暂停更新（在步骤/批次边界生效）"""
        if not self.update_status['running']:
            return {'success': False, 'message': '没有正在运行的更新任务'}
        self.update_pause_event.set()
        self.update_status['paused'] = True
        self.update_status['status'] = 'paused'
        self.update_status['message'] = '更新已暂停'
        self._add_update_log("⏸ 更新任务已暂停（将在当前批次结束后生效）")
        return {'success': True, 'message': '更新任务已暂停'}

    def resume_update(self) -> Dict[str, Any]:
        """恢复更新"""
        if not self.update_status.get('paused'):
            return {'success': False, 'message': '没有暂停的更新任务'}
        self.update_pause_event.clear()
        self.update_status['paused'] = False
        self.update_status['status'] = 'running'
        self.update_status['message'] = '更新已恢复'
        self._add_update_log("▶ 更新任务已恢复")
        return {'success': True, 'message': '更新任务已恢复'}

    def _run_update(self, task_id: str, update_types: Optional[List[str]],
                    scope: str = 'watched'):
        """
        执行更新任务（在后台线程中运行）
        
        流程:
        1. 检查交易时间和更新条件（得到 target_date）
        2. 查询上次更新日期（前置）并进行幂等判断：若数据已最新则跳过全部更新步骤
        3. 检测并初始化新股票
        4. 获取所有股票列表
        5. 更新K线数据
        6. 更新资金流向数据
        7. 更新股票市值信息
        8. 刷新减持计划缓存
        9. 记录更新完成
        10. 计算并保存市场温度
        11. 计算并保存风控状态
        
        Args:
            task_id: 任务ID
            update_types: 更新类型列表（可选）
        """
        target_date = None
        validator = None
        
        try:
            # 初始化状态
            with self.update_lock:
                self.update_status['running'] = True
                self.update_status['paused'] = False
                self.update_status['status'] = 'running'
                self.update_status['skipped'] = False
                self.update_status['start_time'] = datetime.now().isoformat()
                self.update_status['logs'] = []
                self.update_status['message'] = ''
                self.update_status['totalStats'] = {
                    'new_stock_detected': 0,
                    'new_stock_initialized': 0,
                    'kline_added': 0,
                    'kline_updated': 0,
                    'kline_failed': 0,
                    'fund_flow_added': 0,
                    'fund_flow_updated': 0,
                    'fund_flow_failed': 0,
                    'market_cap_updated': 0,
                    'market_cap_failed': 0,
                    'reduce_plan_refreshed': 0,
                    'reduce_plan_failed': 0
                }
                self.update_status['progress'] = 0
                self.update_status['current_task'] = '准备'
                self.update_status['success'] = 0
                self.update_status['failed'] = 0
                self.update_status['scope'] = scope
                self.update_status['critical_failure'] = False
                self.update_status['already_latest'] = False
                self.update_status['paused'] = False

            self.update_cancel_event.clear()
            self.update_pause_event.clear()
            stats = {}

            self._add_update_log(f"✓ 更新任务 {task_id} 已启动（范围: {'全市场' if scope == 'all' else '关注行业及成分股'}）")

            # 兼容旧类型名（basic_data/history_data/fund_flow_data）与新类型名（kline/fund_flow）
            if isinstance(update_types, str):
                update_types = [update_types]
            raw_types = update_types or []
            normalized_types = set()
            for type_name in raw_types:
                name = str(type_name).lower()
                if name in ('kline', 'history_data', 'history', 'kline_data'):
                    normalized_types.add('kline')
                elif name in ('fund_flow', 'fund_flow_data', 'moneyflow'):
                    normalized_types.add('fund_flow')
                elif name in ('market_cap', 'marketcap'):
                    normalized_types.add('market_cap')
                elif name in ('new_stock', 'basic_data', 'basic'):
                    normalized_types.add('new_stock')
            all_types = not normalized_types
            do_kline = all_types or 'kline' in normalized_types
            do_fund = all_types or 'fund_flow' in normalized_types
            do_market_cap = all_types or 'market_cap' in normalized_types
            do_new_stock = scope == 'all' and (all_types or 'new_stock' in normalized_types)
            
            # 【第1步】检查交易时间和更新条件
            self._add_update_log("【第1步】检查交易时间和更新条件...")
            validator = TradingTimeValidator()
            is_valid, error_msg, target_date = validator.validate_update_time(check_updated=False)
            
            if not is_valid:
                # 交易时间验证失败
                with self.update_lock:
                    self.update_status['status'] = 'failed'
                    self.update_status['message'] = error_msg
                
                self._add_update_log(f"✗ 错误: {error_msg}")
                logger.warning(f"更新任务 {task_id} 被拒绝: {error_msg}")
                return
            
            # 记录目标更新日期
            self._add_update_log(f"✓ 目标更新日期: {target_date}")
            
            # 【第2步】查询上次更新日期（前置，用于幂等判断）
            self._add_update_log("【第2步】查询上次更新日期...")
            try:
                last_update_date = validator.get_last_update_date()
                
                # 如果没有记录，使用默认日期（3天前），视为需要更新
                if not last_update_date:
                    last_update_date = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
                
                self._add_update_log(f"✓ 上次更新日期: {last_update_date}")
            
            except Exception as e:
                self._add_update_log(f"✗ 查询上次更新日期失败: {str(e)}")
                logger.error(f"查询上次更新日期失败: {str(e)}")
                last_update_date = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
            
            # 注：不再仅凭本地更新日志提前结束；后续按目标股票池的实际 K 线覆盖判断。
            self._set_update_progress(5, "已确定目标日期，解析股票池")

            # 【第3步】检测并初始化新股票（优先级最高）
            self._add_update_log("【第3步】检测并初始化新股票（仅全市场模式）...")
            if scope == 'all' and do_new_stock:
                try:
                    # 创建新股票检测器
                    stock_data_fetcher = StockDataFetcher()
                    data_initializer = DataInitializer(
                        self.db_manager,
                        stock_data_fetcher,
                        None,  # kline_fetcher
                        None   # fund_flow_fetcher
                    )
                
                    detector = NewStockDetector(
                        self.db_manager,
                        stock_data_fetcher,
                        data_initializer
                    )
                
                    # 执行新股票检测和初始化
                    new_stock_result = detector.detect_and_init_new_stocks(years=3, days=30)
                
                    # 更新统计信息
                    with self.update_lock:
                        self.update_status['totalStats']['new_stock_detected'] = new_stock_result.get('detected', 0)
                        self.update_status['totalStats']['new_stock_initialized'] = new_stock_result.get('initialized', 0)
                
                    # 记录结果
                    if new_stock_result['success']:
                        self._add_update_log(f"✓ 新股票检测完成: 检测 {new_stock_result['detected']} 只，初始化 {new_stock_result['initialized']} 只，失败 {new_stock_result['failed']} 只")
                    
                        # 如果有失败的股票，记录警告
                        if new_stock_result['failed'] > 0:
                            failed_stocks = new_stock_result.get('failed_stocks', [])
                            self._add_update_log(f"⚠ 初始化失败的股票: {', '.join(failed_stocks[:5])}")
                    else:
                        # 新股票初始化失败，但不影响后续更新
                        self._add_update_log(f"⚠ 新股票检测失败: {new_stock_result.get('message', '未知错误')}")
                        logger.warning(f"新股票检测失败: {new_stock_result.get('message', '未知错误')}")
            
                except Exception as e:
                    # 新股票初始化失败，但不影响后续更新
                    self._add_update_log(f"⚠ 新股票检测异常: {str(e)}")
                    logger.warning(f"新股票检测异常: {str(e)}")
            
            else:
                self._add_update_log("ℹ 关注范围模式：跳过全市场新股检测")

            # 【第4步】解析更新股票池（关注范围 / 全市场）
            self._add_update_log("【第4步】解析更新股票池...")
            try:
                if scope == 'all':
                    sql = "SELECT DISTINCT code FROM stock_basic WHERE is_deleted = 0 ORDER BY code"
                    result = self.db_manager.query(sql)
                    stock_codes = [row['code'] for row in result] if result else []
                    self._add_update_log(f"✓ 全市场股票池: {len(stock_codes)} 只")
                else:
                    stock_codes = self._resolve_update_stock_codes()
                    self._add_update_log(f"✓ 关注范围股票池: {len(stock_codes)} 只")
            except Exception as e:
                self._add_update_log(f"✗ 获取股票列表失败: {str(e)}")
                logger.error(f"获取股票列表失败: {str(e)}")
                raise

            if not stock_codes:
                raise RuntimeError("更新股票池为空，无法执行更新")

            # 按实际 K 线覆盖判断是否已最新，不再只信任本地更新日志
            covered, total_codes = self._get_kline_coverage(stock_codes, target_date)
            coverage_ratio = covered / max(total_codes, 1)
            self._add_update_log(
                f"✓ K线覆盖检查: {covered}/{total_codes} 只已有 {target_date} 数据"
            )

            effective_last_update = last_update_date
            if last_update_date >= target_date and coverage_ratio >= 0.98:
                self.update_status['already_latest'] = True
                do_kline = False
                self._add_update_log(
                    "ℹ 关注范围K线已是最新，跳过 K 线拉取（资金流/市值等仍继续检查）"
                )
            elif last_update_date >= target_date:
                # 日志显示已更新，但实际覆盖不足：强制回看最近 15 天补齐
                effective_last_update = (
                    datetime.strptime(target_date, '%Y-%m-%d') - timedelta(days=15)
                ).strftime('%Y-%m-%d')
                self._add_update_log(
                    f"⚠ K线覆盖不足，强制从 {effective_last_update} 回补到 {target_date}"
                )
            self._set_update_progress(18, "准备更新K线")
            
            # 【第5步】更新K线数据
            if do_kline:
                self._add_update_log("【第5步】更新K线数据...")
                self._check_update_control()
                try:
                    stock_data_fetcher = StockDataFetcher()
                    kline_updater = KlineUpdater(self.db_manager, stock_data_fetcher)

                    def kline_progress(percent, message):
                        self._check_update_control()
                        self._set_update_progress(18 + int(percent * 0.45), message)

                    kline_result = kline_updater.update_kline_data(
                        stock_codes=stock_codes,
                        last_update_date=effective_last_update,
                        target_date=target_date,
                        batch_size=100,
                        progress_callback=kline_progress,
                        control_callback=self._check_update_control,
                    )

                    with self.update_lock:
                        self.update_status['totalStats']['kline_added'] = kline_result.get('added', 0)
                        self.update_status['totalStats']['kline_updated'] = kline_result.get('updated', 0)
                        self.update_status['totalStats']['kline_failed'] = kline_result.get('failed', 0)

                    if kline_result['success']:
                        self._add_update_log(
                            f"✓ K线数据更新完成: 新增 {kline_result['added']} 条，"
                            f"更新 {kline_result['updated']} 条，失败 {kline_result['failed']} 条，"
                            f"耗时 {kline_result['total_time']:.1f}秒"
                        )
                        if '数据源尚未就绪' in kline_result.get('message', ''):
                            self._add_update_log("⚠ 数据源尚未就绪，跳过本次更新（不执行资金流向更新）")
                            logger.info("数据源尚未就绪，跳过本次更新")
                            self._set_update_progress(100, "数据源尚未就绪，已跳过本次更新")
                            with self.update_lock:
                                self.update_status['status'] = 'completed'
                                self.update_status['skipped'] = True
                                self.update_status['end_time'] = datetime.now().isoformat()
                                self.update_status['message'] = '数据源尚未就绪，已跳过本次更新'
                            return
                        if kline_result.get('failed', 0) > 1000:
                            self.update_status['critical_failure'] = True
                    else:
                        self.update_status['critical_failure'] = True
                        self._add_update_log(f"✗ K线数据更新失败: {kline_result.get('message', '未知错误')}")

                except UpdateCancelled:
                    raise
                except Exception as e:
                    self.update_status['critical_failure'] = True
                    self._add_update_log(f"✗ K线数据更新异常: {str(e)}")
                    logger.error(f"K线数据更新异常: {str(e)}")
                    with self.update_lock:
                        self.update_status['totalStats']['kline_failed'] = len(stock_codes)
            else:
                self._add_update_log("ℹ K线已是最新，跳过 K 线步骤")
            self._set_update_progress(63, "K线步骤完成")
            
            # 【第6步】更新资金流向数据
            if do_fund:
                self._add_update_log("【第6步】更新资金流向数据...")
                self._check_update_control()
                try:
                    fund_flow_fetcher = FundFlowFetcher(self.db_manager)
                    fund_flow_updater = FundFlowUpdater(self.db_manager, fund_flow_fetcher)

                    def fund_progress(percent, message):
                        self._check_update_control()
                        self._set_update_progress(65 + int(percent * 0.10), message)

                    fund_flow_result = fund_flow_updater.update_fund_flow_data(
                        last_update_date=effective_last_update,
                        target_date=target_date,
                        stock_codes=stock_codes if scope == 'watched' else None,
                        progress_callback=fund_progress,
                        control_callback=self._check_update_control,
                    )
                    
                    # 更新统计信息
                    with self.update_lock:
                        self.update_status['totalStats']['fund_flow_added'] = fund_flow_result.get('added', 0)
                        self.update_status['totalStats']['fund_flow_updated'] = fund_flow_result.get('updated', 0)
                        self.update_status['totalStats']['fund_flow_failed'] = fund_flow_result.get('failed', 0)
                    
                    # 记录结果
                    if fund_flow_result['success']:
                        self._add_update_log(f"✓ 资金流向数据更新完成: 新增 {fund_flow_result['added']} 条，更新 {fund_flow_result['updated']} 条，失败 {fund_flow_result['failed']} 条，耗时 {fund_flow_result['total_time']:.1f}秒")
                    else:
                        self.update_status['critical_failure'] = True
                        self._add_update_log(f"✗ 资金流向数据更新失败: {fund_flow_result.get('message', '未知错误')}")
                        logger.warning(f"资金流向数据更新失败: {fund_flow_result.get('message', '未知错误')}")

                except UpdateCancelled:
                    raise
                except Exception as e:
                    self.update_status['critical_failure'] = True
                    self._add_update_log(f"✗ 资金流向数据更新异常: {str(e)}")
                    logger.error(f"资金流向数据更新异常: {str(e)}")
                    with self.update_lock:
                        self.update_status['totalStats']['fund_flow_failed'] = 1
            else:
                self._add_update_log("ℹ 本次不更新资金流向（updateTypes 未包含）")
            self._set_update_progress(75, "资金流步骤完成")
            
            # 【第7步】更新股票市值信息
            if do_market_cap:
                self._add_update_log("【第7步】更新股票市值信息...")
                self._check_update_control()
                try:
                    stock_data_fetcher = StockDataFetcher()

                    # 关注范围模式只更新目标股票，避免全市场更新
                    market_cap_result = stock_data_fetcher.update_stock_market_cap(
                        db_manager=self.db_manager,
                        stock_codes=stock_codes if scope == 'watched' else None,
                        max_retries=3
                    )

                    with self.update_lock:
                        self.update_status['totalStats']['market_cap_updated'] = market_cap_result.get('updated', 0)
                        self.update_status['totalStats']['market_cap_failed'] = market_cap_result.get('failed', 0)

                    if market_cap_result['updated'] > 0:
                        self._add_update_log(
                            f"✓ 股票市值更新完成: 更新 {market_cap_result['updated']} 只，"
                            f"失败 {market_cap_result['failed']} 只"
                        )
                    else:
                        self._add_update_log(
                            f"⚠ 股票市值更新: 更新 {market_cap_result['updated']} 只，"
                            f"失败 {market_cap_result['failed']} 只"
                        )
                        logger.warning("股票市值更新: 未更新任何股票")
                except UpdateCancelled:
                    raise
                except Exception as e:
                    self._add_update_log(f"✗ 股票市值更新异常: {str(e)}")
                    logger.error(f"股票市值更新异常: {str(e)}")
                    with self.update_lock:
                        self.update_status['totalStats']['market_cap_failed'] = len(stock_codes)
            else:
                self._add_update_log("ℹ 本次不更新股票市值（updateTypes 未包含）")
            self._set_update_progress(78, "市值步骤完成")

            # 【第8步】刷新减持计划缓存（离线批量拉取候选池，落盘本地缓存）
            self._add_update_log("【第8步】刷新减持计划缓存...")
            self._check_update_control()
            self._set_update_progress(80, "刷新减持计划缓存")
            try:
                from trading.reduce_plan_cache import refresh_reduce_plan_cache
                # 读取候选池股票代码（结构：pool[].stock.stock_code）
                pool_codes = []
                pool_file = Path(self.data_dir) / "running" / "buy_candidate_pool.json"
                if pool_file.exists():
                    try:
                        with open(pool_file, 'r', encoding='utf-8') as pf:
                            pool_data = json.load(pf)
                        for item in pool_data.get('pool', []):
                            sc = item.get('stock', {}).get('stock_code', '')
                            if sc:
                                pool_codes.append(sc)
                    except Exception as pe:
                        logger.warning("读取候选池失败: %s", pe)
                if pool_codes and scope == 'watched':
                    pool_codes = [code for code in pool_codes if code in set(stock_codes)]
                if pool_codes:
                    # 目标更新日期 YYYY-MM-DD 转 YYYYMMDD 作为刷新日期
                    score_dt = target_date.replace('-', '')
                    rp_stats = refresh_reduce_plan_cache(pool_codes, score_dt)
                    with self.update_lock:
                        self.update_status['totalStats']['reduce_plan_refreshed'] = rp_stats.get('refreshed', 0)
                        self.update_status['totalStats']['reduce_plan_failed'] = rp_stats.get('failed', 0)
                    self._add_update_log(
                        f"✓ 减持计划缓存刷新完成: 刷新 {rp_stats.get('refreshed',0)} 只，失败 {rp_stats.get('failed',0)} 只"
                    )
                else:
                    self._add_update_log("ℹ 候选池为空，跳过减持计划缓存刷新")
            except UpdateCancelled:
                raise
            except Exception as e:
                self._add_update_log(f"⚠ 减持计划缓存刷新异常(不影响其他步骤): {str(e)}")
                logger.warning(f"减持计划缓存刷新异常: {str(e)}")
            self._set_update_progress(84, "减持计划缓存完成")

            # 【第9步】汇总更新统计
            self._add_update_log("【第9步】汇总更新统计...")
            stats = {
                'new_stock_detected': self.update_status['totalStats']['new_stock_detected'],
                'new_stock_initialized': self.update_status['totalStats']['new_stock_initialized'],
                'kline_added': self.update_status['totalStats']['kline_added'],
                'kline_updated': self.update_status['totalStats']['kline_updated'],
                'fund_flow_added': self.update_status['totalStats']['fund_flow_added'],
                'fund_flow_updated': self.update_status['totalStats']['fund_flow_updated'],
                'market_cap_updated': self.update_status['totalStats']['market_cap_updated'],
                'market_cap_failed': self.update_status['totalStats']['market_cap_failed'],
            }
            self._add_update_log("✓ 统计信息已汇总（最终完成记录在全部步骤后写入一次）")
            self._set_update_progress(86, "统计信息已汇总")

            # 【第10步】计算并保存市场温度
            self._add_update_log("【第10步】计算并保存市场温度...")
            self._check_update_control()
            self._set_update_progress(90, "计算市场温度")
            try:
                # 导入市场温度计算器
                from utils.market_temperature import MarketTemperature, DataNotAvailableError
                from trading.market_temperature_dao import MarketTemperatureDAO
                
                # 转换日期格式为 YYYYMMDD
                trade_date_yyyymmdd = target_date.replace('-', '')
                
                # 计算市场温度（不使用缓存，确保获取最新数据）
                mt = MarketTemperature()
                temp_result = mt.calculate(trade_date_yyyymmdd, use_cache=False)
                
                # 保存到数据库
                dao = MarketTemperatureDAO()
                dao.save(temp_result)
                
                # 更新统计信息
                self._add_update_log(
                    f"✓ 市场温度计算完成: {temp_result.get('temperature', 'N/A')}° - "
                    f"{temp_result.get('status', '未知')} - "
                    f"仓位{temp_result.get('position_ratio', 0) * 100:.0f}%"
                )
                logger.info(f"市场温度已保存: {trade_date_yyyymmdd} - {temp_result.get('temperature')}°")
            except DataNotAvailableError as e:
                # 数据不可用（非交易日或API无数据），这是正常的，跳过
                self._add_update_log(f"ℹ 市场温度跳过: {str(e)}")
                logger.info(f"市场温度跳过（非交易日或数据不可用）: {trade_date_yyyymmdd} - {str(e)}")
            
            except Exception as e:
                self._add_update_log(f"⚠ 市场温度计算失败: {str(e)}")
                logger.warning(f"市场温度计算失败: {str(e)}")
            
            # 【第11步】计算并保存风控状态
            self._add_update_log("【第11步】计算并保存风控状态...")
            self._check_update_control()
            self._set_update_progress(94, "计算风控状态")
            try:
                from utils.risk_controller import RiskController
                
                # 获取风控控制器
                controller = RiskController()
                
                # 计算风控状态（不使用缓存，确保获取最新数据）
                risk_status = controller.get_risk_status(
                    date=target_date,
                    force_refresh=True
                )
                
                if risk_status:
                    # 更新统计信息
                    self._add_update_log(
                        f"✓ 风控状态计算完成: VaR(1d)={risk_status.var_1d*100:.2f}% - "
                        f"风险等级={risk_status.risk_level.value} - "
                        f"仓位上限={risk_status.position_limit*100:.0f}%"
                    )
                    logger.info(f"风控状态已保存: {target_date} - VaR={risk_status.var_1d*100:.2f}%")
                else:
                    self._add_update_log(f"⚠ 风控状态计算失败: 返回空值")
                    logger.warning(f"风控状态计算失败: {target_date} - 返回空值")
            
            except Exception as e:
                self._add_update_log(f"⚠ 风控状态计算失败: {str(e)}")
                logger.warning(f"风控状态计算失败: {str(e)}")
            
            # 【第12步】计算并保存全A指数ADX（市场趋势强度，与温度同批次产出）
            self._add_update_log("【第12步】计算并保存全A指数ADX...")
            self._check_update_control()
            self._set_update_progress(98, "计算全A指数ADX")
            try:
                from utils.market_index_adx import MarketIndexADX, DataNotAvailableError
                from trading.market_index_adx_dao import MarketIndexADXDAO

                # 计算全A指数（默认中证全指 000985.CSI）ADX，不使用缓存确保取最新数据
                adx_calculator = MarketIndexADX()
                adx_result = adx_calculator.calculate(trade_date_yyyymmdd, use_cache=False)

                adx_dao = MarketIndexADXDAO()
                adx_dao.save(adx_result)

                self._add_update_log(
                    f"✓ 全A指数ADX计算完成: ADX={adx_result.get('adx')} "
                    f"(强度={adx_result.get('trend_strength')}, "
                    f"方向={adx_result.get('trend_direction')})")
                logger.info(f"全A指数ADX已保存: {trade_date_yyyymmdd} - {adx_result}")

            except DataNotAvailableError as e:
                # 非交易日或接口无数据，属正常情况，跳过（不影响更新结果判定）
                self._add_update_log(f"ℹ 全A指数ADX跳过: {str(e)}")
                logger.info(f"全A指数ADX跳过（非交易日或数据不可用）: "
                            f"{trade_date_yyyymmdd} - {str(e)}")

            except Exception as e:
                self._add_update_log(f"⚠ 全A指数ADX计算失败: {str(e)}")
                logger.warning(f"全A指数ADX计算失败: {str(e)}")
            
            # 汇总结果并做更严格的最终状态判定
            total_added = (
                self.update_status['totalStats']['kline_added']
                + self.update_status['totalStats']['fund_flow_added']
            )
            total_updated = (
                self.update_status['totalStats']['kline_updated']
                + self.update_status['totalStats']['fund_flow_updated']
            )
            total_success = total_added + total_updated
            total_failed = (
                self.update_status['totalStats']['kline_failed']
                + self.update_status['totalStats']['fund_flow_failed']
            )
            critical_failure = bool(self.update_status.get('critical_failure'))
            already_latest = bool(self.update_status.get('already_latest'))

            with self.update_lock:
                if self.update_cancel_event.is_set():
                    self.update_status['status'] = 'cancelled'
                    self.update_status['message'] = '更新已取消'
                    self.update_status['success'] = 0
                elif critical_failure or total_failed > 1000:
                    self.update_status['status'] = 'failed'
                    self.update_status['message'] = (
                        f'更新失败: 关键步骤失败或失败股票数({total_failed})超过1000'
                    )
                    self.update_status['success'] = 0
                elif already_latest or total_success > 0:
                    self.update_status['status'] = 'completed'
                    self.update_status['message'] = (
                        '数据已是最新' if already_latest and total_success == 0 else '更新完成'
                    )
                    self.update_status['success'] = 1
                else:
                    self.update_status['status'] = 'failed'
                    self.update_status['message'] = '更新失败: 没有数据被成功更新'
                    self.update_status['success'] = 0
                self.update_status['end_time'] = datetime.now().isoformat()
                self.update_status['progress'] = 100

            self._add_update_log(
                f"{'✓' if self.update_status['status'] == 'completed' else '✗'} "
                f"更新任务结束: {self.update_status['message']} "
                f"(成功 {total_success}, 失败 {total_failed})"
            )
            if self.update_status['status'] == 'completed':
                logger.info(f"更新任务 {task_id} 完成: {self.update_status['message']}")
            else:
                logger.warning(f"更新任务 {task_id} 失败/取消: {self.update_status['message']}")

            # 完成记录只写一次，避免重复追加 _all.jsonl
            if validator and target_date:
                try:
                    if self.update_status['status'] == 'completed':
                        validator.record_update_complete(target_date, stats)
                    elif self.update_status['status'] == 'failed':
                        validator.record_update_failed(target_date, self.update_status['message'])
                except Exception as log_e:
                    logger.error(f"记录更新状态失败: {str(log_e)}")

        except UpdateCancelled:
            with self.update_lock:
                self.update_status['status'] = 'cancelled'
                self.update_status['message'] = '更新已取消'
                self.update_status['success'] = 0
                self.update_status['end_time'] = datetime.now().isoformat()
                self.update_status['running'] = False
            self._add_update_log("✕ 更新任务已取消")
            logger.info(f"更新任务 {task_id} 已取消")

        except Exception as e:
            total_added = (
                self.update_status['totalStats']['kline_added']
                + self.update_status['totalStats']['fund_flow_added']
            )
            total_updated = (
                self.update_status['totalStats']['kline_updated']
                + self.update_status['totalStats']['fund_flow_updated']
            )
            total_success = total_added + total_updated
            total_failed = (
                self.update_status['totalStats']['kline_failed']
                + self.update_status['totalStats']['fund_flow_failed']
            )

            with self.update_lock:
                if self.update_cancel_event.is_set():
                    self.update_status['status'] = 'cancelled'
                    self.update_status['message'] = '更新已取消'
                else:
                    self.update_status['status'] = 'failed'
                    self.update_status['message'] = f'更新失败: {str(e)}'
                self.update_status['success'] = 0
                self.update_status['end_time'] = datetime.now().isoformat()

            self._add_update_log(f"✗ 更新任务失败: {str(e)}")
            logger.error(f"更新任务 {task_id} 失败: {str(e)}")

            if validator and target_date and not self.update_cancel_event.is_set():
                try:
                    validator.record_update_failed(target_date, str(e))
                except Exception as log_e:
                    logger.error(f"记录更新失败状态失败: {str(log_e)}")

        finally:
            with self.update_lock:
                self.update_status['running'] = False
                self.update_status['paused'] = False
    
    def get_update_progress(self) -> Dict[str, Any]:
        """
        获取更新进度
        
        Returns:
            dict: 更新进度信息
        """
        # 计算已耗时（秒）
        elapsed_time = 0
        if self.update_status['start_time']:
            try:
                start = datetime.fromisoformat(self.update_status['start_time'])
                elapsed_time = int((datetime.now() - start).total_seconds())
            except:
                elapsed_time = 0
        
        # 返回前端期望的格式
        return {
            'running': self.update_status['running'],
            'status': self.update_status['status'],
            'paused': self.update_status.get('paused', False),
            'progress': self.update_status.get('progress', 0),
            'currentTask': self.update_status.get('current_task', ''),
            'scope': self.update_status.get('scope', 'watched'),
            'alreadyLatest': self.update_status.get('already_latest', False),
            'skipped': self.update_status.get('skipped', False),  # 是否因数据源未就绪被跳过
            'message': self.update_status['message'],
            'startTime': self.update_status['start_time'],
            'endTime': self.update_status['end_time'],
            'elapsedTime': elapsed_time,
            'logs': self.update_status['logs'][-50:],  # 返回最后50条日志
            'totalStats': self.update_status['totalStats']  # 统计数据
        }
    
    def cancel_update(self) -> Dict[str, Any]:
        """
        取消更新任务
        立即停止任务执行，清除进度信息
        
        Returns:
            dict: 取消结果
        """
        # 检查是否有正在运行或已暂停的任务
        if not self.update_status['running'] and not self.update_status.get('paused', False):
            return {
                'success': False,
                'message': '没有正在运行的更新任务'
            }
        
        # 协作式取消：设置事件，由工作线程在步骤/批次边界退出
        self.update_cancel_event.set()
        self.update_pause_event.clear()
        self.update_status['paused'] = False
        self.update_status['message'] = '更新已取消'
        self.update_status['status'] = 'cancelled'
        self._add_update_log("✕ 已发出取消信号，等待当前批次结束")
        
        logger.info("更新任务取消信号已发出")
        
        return {
            'success': True,
            'message': '更新任务已取消'
        }
    
    def update_task_stats(self, task_id: str, added: int = 0, updated: int = 0, deleted: int = 0):
        """
        更新任务统计数据
        
        Args:
            task_id: 任务ID
            added: 新增数量
            updated: 更新数量
            deleted: 删除数量
        """
        # 更新总统计
        self.update_status['totalStats']['added'] += added
        self.update_status['totalStats']['updated'] += updated
        self.update_status['totalStats']['deleted'] += deleted
        self.update_status['totalStats']['processed'] = (
            self.update_status['totalStats']['added'] +
            self.update_status['totalStats']['updated'] +
            self.update_status['totalStats']['deleted']
        )
    
    def get_tables_info(self) -> Dict[str, Any]:
        """
        获取新增表的信息
        
        Returns:
            dict: 新增表的信息
        """
        return self.db_initializer.get_new_tables_info()
    
    def get_tables_stats(self) -> Dict[str, Any]:
        """
        获取表数据统计
        
        Returns:
            dict: 表数据统计信息，包含前端需要的 success 和 failed 字段
        """
        stats = {}
        
        try:
            if self.selection_db_path.exists():
                import sqlite3
                conn = sqlite3.connect(str(self.selection_db_path))
                cursor = conn.cursor()
                
                # 查询各个表的行数
                tables = [
                    'stock_basic',
                    'stock_kline',
                    'stock_industry',
                    'stock_sector',
                    'stock_fund_flow',
                    'stock_event',
                    'stock_lhb',
                    'stock_margin_trading'
                ]
                
                for table in tables:
                    try:
                        cursor.execute(f"SELECT COUNT(*) FROM {table}")
                        count = cursor.fetchone()[0]
                        stats[table] = count
                    except Exception as e:
                        logger.warning(f"查询表 {table} 行数失败: {str(e)}")
                        stats[table] = 0
                
                conn.close()
            
            # 前端期望的 success 和 failed 字段
            # success: 成功初始化的股票数量（stock_basic 表行数）
            # failed: 失败数量（股票总数 - 成功数量，或默认0）
            stats['success'] = stats.get('stock_basic', 0)
            stats['failed'] = self.init_status.get('failed', 0)
        
        except Exception as e:
            logger.error(f"获取表数据统计失败: {str(e)}")
            stats['success'] = 0
            stats['failed'] = 0
        
        return stats
    
    def _add_init_log(self, message: str):
        """
        添加初始化日志
        
        Args:
            message: 日志消息
        """
        # 直接保存消息字符串，而不是对象
        self.init_status['logs'].append(message)
        logger.info(f"[初始化] {message}")
    
    def _add_update_log(self, message: str):
        """
        添加更新日志
        
        Args:
            message: 日志消息
        """
        # 直接保存消息字符串，而不是对象
        self.update_status['logs'].append(message)
        logger.info(f"[更新] {message}")


# 全局服务实例
_data_collection_service = None


def get_data_collection_service(data_dir: str = 'data') -> DataCollectionService:
    """
    获取数据采集服务实例（单例模式）
    
    Args:
        data_dir: 数据目录路径
    
    Returns:
        DataCollectionService: 数据采集服务实例
    """
    global _data_collection_service
    
    if _data_collection_service is None:
        _data_collection_service = DataCollectionService(data_dir)
    
    return _data_collection_service
