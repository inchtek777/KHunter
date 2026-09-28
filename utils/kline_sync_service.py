# -*- coding: utf-8 -*-
"""基础数据行情增量同步服务

按每只股票自身的最新日期分档补齐K线。KlineUpdater 只接受单一增量基准，
若直接用全库 MAX(date) 作基准，新入库或长期停牌的股票只会补到最近几日，
留下历史空洞，因此这里按缺口分档后逐档调用。
"""
import logging
import threading
from bisect import bisect_right
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 交易日缺口分档，档位即该批需要回补的交易日数
GAP_TIERS = [5, 20, 60, 120, 250]
# 无K线历史或缺口超过最大档位的股票回补的交易日数（约3年）
HISTORY_TIER = 750
# TickFlow 批量接口每批股票数上限
BATCH_SIZE = 100


class KlineSyncService:
    """K线增量同步服务（后台线程 + 内存进度状态）"""

    def __init__(self, db_manager):
        self.db_manager = db_manager
        self._lock = threading.RLock()
        self._cancel_flag = False
        self._calendar: List[str] = []
        self._status = self._new_status()

    @staticmethod
    def _new_status() -> Dict:
        return {
            'running': False,
            'status': 'idle',
            'target_date': '',
            'total': 0,
            'processed': 0,
            'added': 0,
            'updated': 0,
            'rebuilt': 0,
            'failed': 0,
            'skipped_latest': 0,
            'message': '',
            'logs': [],
            'start_time': None,
            'end_time': None,
        }

    # ==================== 对外接口 ====================

    def start(self) -> Dict:
        """启动增量同步任务"""
        with self._lock:
            if self._status['running']:
                return {'success': False, 'message': '已有增量同步任务在运行中'}
            self._status = self._new_status()
            self._status.update({
                'running': True,
                'status': 'running',
                'start_time': datetime.now().isoformat(),
            })
            self._cancel_flag = False
            thread = threading.Thread(target=self._run_safe, daemon=True)
            thread.start()
        return {'success': True, 'message': '增量同步已启动'}

    def cancel(self) -> Dict:
        """请求取消（在当前档位批次结束后停止）"""
        with self._lock:
            if not self._status['running']:
                return {'success': False, 'message': '当前没有正在运行的同步任务'}
            self._cancel_flag = True
            self._log('已提交取消请求，将在当前批次结束后停止')
        return {'success': True, 'message': '取消请求已提交'}

    def get_status(self) -> Dict:
        with self._lock:
            snapshot = dict(self._status)
            snapshot['logs'] = list(self._status['logs'][-50:])
            return snapshot

    # ==================== 任务执行 ====================

    def _run_safe(self):
        try:
            self._run()
        except Exception as e:
            logger.error(f"K线增量同步异常: {e}", exc_info=True)
            self._finish('failed', f'同步失败: {e}')

    def _run(self):
        rows = self.db_manager.query('SELECT code FROM stock_basic ORDER BY code') or []
        codes = [row['code'] for row in rows]
        if not codes:
            self._finish('failed', 'stock_basic 为空，请先初始化基础数据')
            return
        self._status['total'] = len(codes)

        target_date = self._resolve_target_date(codes)
        if not target_date:
            self._finish('failed', '无法确定目标交易日（交易日历不可用）')
            return

        self._status['target_date'] = target_date
        self._log(f'目标交易日: {target_date}')

        calendar = self._load_calendar(target_date)
        if not calendar:
            self._finish('failed', '交易日历不可用（Tushare、本地缓存与已入库K线均无法提供'
                                   '足够窗口），无法确定增量区间，请稍后重试')
            return
        self._calendar = calendar

        last_dates = self.db_manager.query(
            'SELECT code, MAX(date) AS last_date FROM stock_kline GROUP BY code'
        ) or []
        latest_map = {row['code']: row['last_date'] for row in last_dates}

        tiers = self._group_by_gap(codes, latest_map, target_date)
        if not tiers:
            self._finish('completed', f'数据已是最新（{len(codes)} 只均已到 {target_date}）')
            return

        self._log('缺口分档: ' + '，'.join(
            f'{tier}交易日/{len(tier_codes)}只' for tier, tier_codes in sorted(tiers.items())
        ))

        stats = {'added': 0, 'updated': 0, 'rebuilt': 0, 'failed': 0}
        blocked_tiers = []
        for tier in sorted(tiers):
            if self._cancel_flag:
                self._finish('cancelled', '同步已取消')
                return

            tier_codes = tiers[tier]
            last_update_date = self._shift_trading_day(target_date, tier)
            self._log(f'同步 {len(tier_codes)} 只（基准 {last_update_date} → {target_date}）...')

            result = self._sync_tier(tier, tier_codes, last_update_date, target_date)
            with self._lock:
                self._status['processed'] = min(
                    self._status['processed'] + len(tier_codes), self._status['total']
                )
            if not result.get('success'):
                self._log(f'✗ {tier}交易日档同步失败: {result.get("message", "未知错误")}')
                continue

            stats['added'] += result.get('added', 0)
            stats['updated'] += result.get('updated', 0)
            stats['rebuilt'] += result.get('rebuilt', 0)
            stats['failed'] += result.get('failed', 0)
            with self._lock:
                self._status.update(stats)
            self._log(f'✓ {tier}交易日档完成: 新增 {result.get("added", 0)} 条，'
                      f'更新 {result.get("updated", 0)} 条，重建 {result.get("rebuilt", 0)} 只，'
                      f'失败 {result.get("failed", 0)} 只')

            if '数据源尚未就绪' in (result.get('message') or ''):
                # 该档取样股可能恰好停牌，不代表数据源整体未就绪，继续跑后续档位
                blocked_tiers.append(tier)

        summary = (f'同步完成: 新增 {stats["added"]} 条，更新 {stats["updated"]} 条，'
                   f'除权重建 {stats["rebuilt"]} 只，失败 {stats["failed"]} 只')
        if blocked_tiers:
            blocked = sum(len(tiers[t]) for t in blocked_tiers)
            summary += (f'；{blocked} 只数据源无 {target_date} 行情'
                        f'（多为已退市/长期停牌），本次未更新')
        self._finish('completed', summary)

    def _sync_tier(self, tier: int, tier_codes: List[str],
                   last_update_date: str, target_date: str) -> Dict:
        """同步单个档位，并用监视线程回写批次进度

        每档新建 KlineUpdater：其 self.stats 只在构造时初始化，复用会跨档累加。
        """
        from utils.kline_updater import KlineUpdater
        from utils.stock_data_fetcher import StockDataFetcher

        updater = KlineUpdater(self.db_manager, StockDataFetcher())
        base_processed = self._status['processed']
        tier_total = len(tier_codes)
        stop_event = threading.Event()

        def watch():
            while not stop_event.wait(1.0):
                current = updater.progress.get('current', 0)
                with self._lock:
                    self._status['processed'] = min(base_processed + min(current, tier_total),
                                                    self._status['total'])

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            return updater.update_kline_data(
                stock_codes=tier_codes,
                last_update_date=last_update_date,
                target_date=target_date,
                batch_size=self._batch_size_for(tier),
            )
        finally:
            stop_event.set()
            watcher.join(timeout=2)

    @staticmethod
    def _batch_size_for(tier: int) -> int:
        """按单批需要返回的K线总量限制批量大小，避免回补3年历史时响应过大"""
        # 单批K线条数上限约 2 万条
        return max(min(BATCH_SIZE, 20000 // (tier + 2)), 10)

    # ==================== 缺口分档 ====================

    def _group_by_gap(self, codes: List[str], latest_map: Dict[str, str], target_date: str) -> Dict[int, List[str]]:
        """按股票自身最新日期与目标交易日的交易日缺口分档"""
        tiers: Dict[int, List[str]] = {}
        skipped = 0
        for code in codes:
            last_date = latest_map.get(code)
            if last_date and last_date >= target_date:
                skipped += 1
                continue
            gap = self._gap_in_trading_days(last_date, target_date)
            tier = next((t for t in GAP_TIERS if gap <= t), HISTORY_TIER)
            tiers.setdefault(tier, []).append(code)

        with self._lock:
            self._status['skipped_latest'] = skipped
        if skipped:
            self._log(f'{skipped} 只已到目标交易日，本次跳过')
        return tiers

    def _gap_in_trading_days(self, last_date: Optional[str], target_date: str) -> int:
        """最新日期到目标交易日之间相差的交易日数（无历史按最大档回补）"""
        if not last_date:
            return HISTORY_TIER
        idx = bisect_right(self._calendar, last_date) - 1
        if idx < 0:
            return HISTORY_TIER
        return max(len(self._calendar) - 1 - idx, 0)

    def _shift_trading_day(self, target_date: str, trading_days: int) -> str:
        """取目标交易日往前 trading_days 个交易日对应的日期"""
        index = len(self._calendar) - 1 - trading_days
        if index < 0:
            return self._calendar[0]
        return self._calendar[index]

    def _load_calendar(self, target_date: str) -> List[str]:
        """加载覆盖最大档位所需窗口的交易日历（升序）

        KlineUpdater 内部同样依赖该日历换算回补天数，缺失时它会退化为只取3条，
        导致档位失真，因此日历不足时必须回填而不是近似估算。
        """
        from utils.trade_date_utils import get_trading_days, refresh_trading_calendar_cache

        start_dt = datetime.strptime(target_date, '%Y-%m-%d') - timedelta(days=int(HISTORY_TIER * 1.7) + 60)
        start_date = start_dt.strftime('%Y-%m-%d')

        for attempt in range(2):
            try:
                days = get_trading_days(start_date, target_date)
            except Exception as e:
                logger.warning(f'加载交易日历失败: {e}')
                days = []

            if len(days) >= HISTORY_TIER + 1:
                return days
            if len(days) > 0:
                logger.warning(f'交易日历覆盖不足: 需要 {HISTORY_TIER + 1} 个交易日，实际 {len(days)} 个')
                break
            if attempt == 0:
                # 首次失败可能是缓存文件刚被外部生成，刷新后重试一次
                refresh_trading_calendar_cache()

        return self._rebuild_calendar_from_kline(target_date)

    def _rebuild_calendar_from_kline(self, target_date: str) -> List[str]:
        """Tushare 限频且无本地缓存时，用已入库K线日期回填交易日历

        全市场任一股票有成交的日期必然是交易日，因此 stock_kline 的 DISTINCT date
        是可靠的下界；回填进缓存文件后 KlineUpdater 的换算也能正常工作。
        取全量历史而非最大档位窗口，避免缓存文件对其他调用方截断。
        """
        rows = self.db_manager.query(
            'SELECT DISTINCT date FROM stock_kline WHERE date <= ? ORDER BY date',
            (target_date,)
        ) or []
        dates = {row['date'] for row in rows if row['date']}
        # 目标日有数据源行情即确认为交易日，避免日历末尾缺一天导致基准偏移
        dates.add(target_date)

        if len(dates) < HISTORY_TIER + 1:
            return []

        self._merge_calendar_cache(dates)
        self._log(f'Tushare 不可用，已用入库K线日期回填交易日历 {len(dates)} 个交易日')
        return sorted(dates)

    @staticmethod
    def _merge_calendar_cache(dates) -> None:
        """把确定的交易日合并进交易日历缓存文件并刷新内存缓存"""
        import json
        from pathlib import Path
        from utils.trade_date_utils import _get_cache_file, refresh_trading_calendar_cache

        cache_file = Path(_get_cache_file())
        merged = set(dates)
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    merged.update(json.load(f).get('dates', []))
            except Exception as e:
                logger.warning(f'读取交易日历缓存失败，将覆盖重建: {e}')

        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump({'dates': sorted(merged)}, f, ensure_ascii=False, indent=2)
        refresh_trading_calendar_cache()

    # ==================== 目标日期与状态辅助 ====================

    def _resolve_target_date(self, codes: List[str]) -> str:
        """取本次同步的目标交易日：最近一个可更新的交易日，且不晚于数据源最新日期

        手动同步不受交易时段限制；但数据源发布存在延迟，若目标日落在数据源尚未覆盖
        的一天，KlineUpdater 的就绪检查会判定"数据源尚未就绪"导致整批空转，
        因此这里用数据源实际可提供的最新日期封顶。
        """
        try:
            from utils.trading_time_validator import TradingTimeValidator
            calendar_date = TradingTimeValidator().get_latest_target_date()
        except Exception as e:
            logger.warning(f'解析目标交易日失败: {e}')
            calendar_date = ''

        provider_date = self._probe_provider_date(codes)
        if not calendar_date:
            return provider_date
        if provider_date and provider_date < calendar_date:
            self._log(f'数据源最新只到 {provider_date}（交易日 {calendar_date}），'
                      f'本次以 {provider_date} 为目标')
            return provider_date
        return calendar_date

    def _probe_provider_date(self, codes: List[str]) -> str:
        """探测数据源可提供的最新K线日期（取至少 2 只样本共有的最大日期）

        样本数口径与 KlineUpdater._is_data_source_ready 一致，避免探测到的日期
        在随后的就绪检查中再次被判为未就绪。
        """
        try:
            from utils.kline_updater import KlineUpdater
            from utils.stock_data_fetcher import StockDataFetcher

            updater = KlineUpdater(self.db_manager, StockDataFetcher())
            sample_codes = updater._sample_by_board(codes)
            if not sample_codes:
                return ''

            kline_data, api_ok = updater.kline_fetcher._fetch_kline_tickflow_batch(
                sample_codes, days=7
            )
            if not api_ok or not kline_data:
                self._log('数据源日期探测失败，按交易日历目标同步')
                return ''

            date_count: Dict[str, int] = {}
            for df in kline_data.values():
                if df is None or len(df) == 0:
                    continue
                for d in df['date'].astype(str).str.split(' ').str[0].unique():
                    date_count[d] = date_count.get(d, 0) + 1

            shared = [d for d, n in date_count.items() if n >= 2]
            if shared:
                return max(shared)
            return max(date_count) if date_count else ''
        except Exception as e:
            logger.warning(f'探测数据源最新日期失败: {e}')
            return ''

    def _log(self, message: str):
        with self._lock:
            self._status['logs'].append(f'[{datetime.now().strftime("%H:%M:%S")}] {message}')
            self._status['message'] = message
        logger.info(f'K线增量同步: {message}')

    def _finish(self, status: str, message: str):
        with self._lock:
            self._log(message)
            self._status['status'] = status
            self._status['message'] = message
            self._status['running'] = False
            self._status['end_time'] = datetime.now().isoformat()
            if status == 'completed':
                # 提前返回的完成分支（如数据已是最新）不一定逐档累加过 processed
                self._status['processed'] = self._status['total']


_kline_sync_service: Optional[KlineSyncService] = None


def get_kline_sync_service(db_manager=None) -> KlineSyncService:
    """获取K线增量同步服务单例"""
    global _kline_sync_service

    if _kline_sync_service is None:
        if db_manager is None:
            from utils.global_db import get_global_db
            db_manager = get_global_db()
        _kline_sync_service = KlineSyncService(db_manager)

    return _kline_sync_service
