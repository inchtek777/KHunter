"""
东方财富行业板块数据服务

职责：
  - 行业板块列表抓取与本地缓存（industry_board）
  - 关注行业维护（watched_industry）
  - 关注行业指数日 K 抓取与保存（industry_daily_kline）

数据源：东方财富优先，接口不可用时自动降级同花顺
  - 行业列表：AKShare stock_board_industry_name_em；失败时改用
    stock_board_industry_name_ths + stock_board_industry_summary_ths
  - 行业日 K：东方财富 K 线 HTTP 接口（BKxxxx，显式超时）；失败时改用
    stock_board_industry_index_ths（881xxx）

说明：东方财富 push2/push2his 在部分网络/网关下会被拦截，降级同花顺后
行业列表、关注行业和行业指数日 K 仍可正常使用；接口返回的 source 字段
标识每条数据的实际来源（eastmoney / ths）。
"""

import logging
import math
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
import requests

from utils.global_db import get_global_db

logger = logging.getLogger(__name__)

SOURCE = "eastmoney"
SOURCE_THS = "ths"
TABLE_INDUSTRY = "industry_board"
TABLE_WATCHED = "watched_industry"
TABLE_DAILY = "industry_daily_kline"

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS industry_board (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        industry_code TEXT NOT NULL UNIQUE,
        industry_name TEXT NOT NULL,
        latest_price REAL,
        change_amount REAL,
        change_pct REAL,
        total_market_cap REAL,
        turnover_rate REAL,
        up_count INTEGER,
        down_count INTEGER,
        leader_stock TEXT,
        leader_change_pct REAL,
        rank_position INTEGER,
        source TEXT NOT NULL DEFAULT 'eastmoney',
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_industry_board_name ON industry_board(industry_name)",
    "CREATE INDEX IF NOT EXISTS idx_industry_board_rank ON industry_board(rank_position)",
    """
    CREATE TABLE IF NOT EXISTS watched_industry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        industry_code TEXT NOT NULL UNIQUE,
        industry_name TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'eastmoney',
        sort_order INTEGER NOT NULL DEFAULT 0,
        note TEXT,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS industry_daily_kline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        industry_code TEXT NOT NULL,
        industry_name TEXT,
        trade_date DATE NOT NULL,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        change_pct REAL,
        change_amount REAL,
        volume REAL,
        amount REAL,
        amplitude REAL,
        turnover_rate REAL,
        source TEXT NOT NULL DEFAULT 'eastmoney',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(industry_code, trade_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_industry_daily_code_date ON industry_daily_kline(industry_code, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_industry_daily_date ON industry_daily_kline(trade_date)",
]


class IndustryBoardError(Exception):
    """行业数据获取/保存异常"""


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, float) and math.isnan(value):
            return None
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    num = _to_float(value)
    if num is None:
        return None
    try:
        return int(round(num))
    except (TypeError, ValueError):
        return None


def _friendly_error(exc: Exception) -> str:
    """把底层网络异常转成更适合页面展示的提示"""
    detail = str(exc).strip() or exc.__class__.__name__
    lowered = detail.lower()
    network_hints = (
        "remotedisconnected",
        "connection aborted",
        "connection error",
        "max retries",
        "timed out",
        "timeout",
        "proxy",
        "ssl",
        "name or service not known",
    )
    if any(hint in lowered for hint in network_hints):
        return "无法连接东方财富接口，请检查网络或代理设置"
    return detail


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if pd.isna(value):
        return ""
    return str(value).strip()


def _to_date(value: Any, default: date) -> date:
    """把 YYYY-MM-DD / YYYYMMDD / date / datetime 统一成 date"""
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("/", "-")
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"日期格式不正确: {value}，应为 YYYY-MM-DD") from exc


class IndustryBoardService:
    """东方财富行业板块数据服务"""

    def __init__(self, db=None):
        self.db = db or get_global_db()
        self._schema_ready = False

    # ==================== schema ====================

    def ensure_schema(self) -> None:
        """确保行业相关表存在（幂等）"""
        if self._schema_ready:
            return
        # 必须放在事务里执行：DBManager.execute() 不会自动提交，
        # 而 DBManager.commit() 在事务外调用会破坏事务计数器。
        with self.db.transaction():
            for statement in _SCHEMA_STATEMENTS:
                self.db.execute(statement)
        self._schema_ready = True

    # ==================== 行业列表 ====================

    @staticmethod
    def _fetch_industry_list_df() -> pd.DataFrame:
        import akshare as ak

        return ak.stock_board_industry_name_em()

    @staticmethod
    def _normalize_em_industry_rows(df: pd.DataFrame, now: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            code = _to_text(row.get("板块代码")).upper()
            name = _to_text(row.get("板块名称"))
            if not code or not name:
                continue
            rows.append(
                {
                    "industry_code": code,
                    "industry_name": name,
                    "latest_price": _to_float(row.get("最新价")),
                    "change_amount": _to_float(row.get("涨跌额")),
                    "change_pct": _to_float(row.get("涨跌幅")),
                    "total_market_cap": _to_float(row.get("总市值")),
                    "turnover_rate": _to_float(row.get("换手率")),
                    "up_count": _to_int(row.get("上涨家数")),
                    "down_count": _to_int(row.get("下跌家数")),
                    "leader_stock": _to_text(row.get("领涨股票")),
                    "leader_change_pct": _to_float(row.get("领涨股票-涨跌幅")),
                    "rank_position": _to_int(row.get("排名")),
                    "source": SOURCE,
                    "updated_at": now,
                }
            )
        return rows

    @staticmethod
    def _fetch_ths_industry_rows() -> List[Dict[str, Any]]:
        """同花顺行业列表（东方财富接口不可用时的降级数据源）

        THS 行业代码为 881xxx；汇总接口提供涨跌幅/涨跌家数/领涨股，
        但不提供行业指数点位和换手率，对应字段留空。
        """
        import akshare as ak

        names_df = ak.stock_board_industry_name_ths()
        summary_df = ak.stock_board_industry_summary_ths()

        summary: Dict[str, Any] = {}
        if summary_df is not None and len(summary_df) > 0:
            for _, row in summary_df.iterrows():
                name = _to_text(row.get("板块"))
                if name:
                    summary[name] = row

        now = datetime.now().isoformat(timespec="seconds")
        rows: List[Dict[str, Any]] = []
        if names_df is None or len(names_df) == 0:
            return rows
        for _, row in names_df.iterrows():
            code = _to_text(row.get("code")).upper()
            name = _to_text(row.get("name"))
            if not code or not name:
                continue
            quote = summary.get(name)
            rows.append(
                {
                    "industry_code": code,
                    "industry_name": name,
                    "latest_price": None,
                    "change_amount": None,
                    "change_pct": _to_float(quote.get("涨跌幅")) if quote is not None else None,
                    "total_market_cap": None,
                    "turnover_rate": None,
                    "up_count": _to_int(quote.get("上涨家数")) if quote is not None else None,
                    "down_count": _to_int(quote.get("下跌家数")) if quote is not None else None,
                    "leader_stock": _to_text(quote.get("领涨股")) if quote is not None else "",
                    "leader_change_pct": (
                        _to_float(quote.get("领涨股-涨跌幅")) if quote is not None else None
                    ),
                    "rank_position": _to_int(quote.get("序号")) if quote is not None else None,
                    "source": SOURCE_THS,
                    "updated_at": now,
                }
            )
        rows.sort(key=lambda item: (item["rank_position"] is None, item["rank_position"] or 0))
        return rows

    def refresh_industry_list(self) -> Dict[str, Any]:
        """刷新行业板块列表：东方财富优先，失败时自动降级到同花顺"""
        self.ensure_schema()
        now = datetime.now().isoformat(timespec="seconds")
        errors: List[str] = []
        rows: List[Dict[str, Any]] = []
        source: Optional[str] = None

        try:
            df = self._fetch_industry_list_df()
            if df is not None and len(df) > 0:
                rows = self._normalize_em_industry_rows(df, now)
                if rows:
                    source = SOURCE
                else:
                    errors.append("东方财富返回的行业列表缺少有效代码/名称")
            else:
                errors.append("东方财富返回的行业列表为空")
        except Exception as exc:
            message = _friendly_error(exc)
            errors.append(f"东方财富：{message}")
            logger.warning("东方财富行业列表不可用，尝试同花顺降级: %s", message)

        if not rows:
            try:
                ths_rows = self._fetch_ths_industry_rows()
                if ths_rows:
                    rows = ths_rows
                    source = SOURCE_THS
                else:
                    errors.append("同花顺返回的行业列表为空")
            except Exception as exc:
                message = _friendly_error(exc)
                errors.append(f"同花顺：{message}")
                logger.error("同花顺行业列表获取失败: %s", message)

        if not rows or not source:
            raise IndustryBoardError("获取行业列表失败：" + "；".join(errors))

        sql = f"""
            INSERT INTO {TABLE_INDUSTRY} (
                industry_code, industry_name, latest_price, change_amount, change_pct,
                total_market_cap, turnover_rate, up_count, down_count,
                leader_stock, leader_change_pct, rank_position, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(industry_code) DO UPDATE SET
                industry_name = excluded.industry_name,
                latest_price = excluded.latest_price,
                change_amount = excluded.change_amount,
                change_pct = excluded.change_pct,
                total_market_cap = excluded.total_market_cap,
                turnover_rate = excluded.turnover_rate,
                up_count = excluded.up_count,
                down_count = excluded.down_count,
                leader_stock = excluded.leader_stock,
                leader_change_pct = excluded.leader_change_pct,
                rank_position = excluded.rank_position,
                source = excluded.source,
                updated_at = excluded.updated_at
        """
        # 数据源切换时清空旧缓存，避免东财/同花顺两套代码混在同一列表
        with self.db.transaction():
            self.db.execute(f"DELETE FROM {TABLE_INDUSTRY}")
            for item in rows:
                self.db.execute(
                    sql,
                    (
                        item["industry_code"],
                        item["industry_name"],
                        item["latest_price"],
                        item["change_amount"],
                        item["change_pct"],
                        item["total_market_cap"],
                        item["turnover_rate"],
                        item["up_count"],
                        item["down_count"],
                        item["leader_stock"],
                        item["leader_change_pct"],
                        item["rank_position"],
                        item["source"],
                        item["updated_at"],
                    ),
                )

        logger.info("行业列表已更新: %s 个板块（数据源=%s）", len(rows), source)
        return {
            "count": len(rows),
            "rows": rows,
            "source": source,
            "fallback": source != SOURCE,
            "errors": errors,
        }

    def get_industry_list(
        self,
        keyword: Optional[str] = None,
        auto_refresh: bool = True,
    ) -> List[Dict[str, Any]]:
        """查询本地行业列表；为空时可自动刷新一次"""
        self.ensure_schema()
        sql = f"""
            SELECT b.industry_code, b.industry_name, b.latest_price, b.change_amount,
                   b.change_pct, b.total_market_cap, b.turnover_rate,
                   b.up_count, b.down_count, b.leader_stock, b.leader_change_pct,
                   b.rank_position, b.source, b.updated_at,
                   CASE WHEN w.industry_code IS NULL THEN 0 ELSE 1 END AS watched
            FROM {TABLE_INDUSTRY} b
            LEFT JOIN {TABLE_WATCHED} w ON w.industry_code = b.industry_code
        """
        params: List[Any] = []
        keyword = (keyword or "").strip()
        if keyword:
            sql += " WHERE (b.industry_code LIKE ? OR b.industry_name LIKE ?)"
            like = f"%{keyword}%"
            params.extend([like, like])
        sql += """
            ORDER BY CASE WHEN b.rank_position IS NULL THEN 1 ELSE 0 END,
                     b.rank_position, b.industry_code
        """
        rows = self.db.query(sql, tuple(params))
        if not rows and auto_refresh:
            # 只有本地缓存本身为空时才自动刷新；关键字无匹配不应触发网络请求
            total = self.db.query_one(f"SELECT COUNT(*) AS total FROM {TABLE_INDUSTRY}") or {}
            if not total.get("total"):
                self.refresh_industry_list()
                rows = self.db.query(sql, tuple(params))
        return rows

    def get_industry(self, industry_code: str) -> Optional[Dict[str, Any]]:
        self.ensure_schema()
        return self.db.query_one(
            f"SELECT * FROM {TABLE_INDUSTRY} WHERE industry_code = ?",
            (industry_code.strip().upper(),),
        )

    # ==================== 关注行业 ====================

    def get_watched(self) -> List[Dict[str, Any]]:
        self.ensure_schema()
        sql = f"""
            SELECT w.industry_code, w.industry_name, w.source,
                   w.sort_order, w.created_at, w.updated_at,
                   (SELECT COUNT(*) FROM {TABLE_DAILY} k
                     WHERE k.industry_code = w.industry_code) AS kline_count,
                   (SELECT MAX(k.trade_date) FROM {TABLE_DAILY} k
                     WHERE k.industry_code = w.industry_code) AS latest_date,
                   (SELECT k.close FROM {TABLE_DAILY} k
                     WHERE k.industry_code = w.industry_code
                     ORDER BY k.trade_date DESC LIMIT 1) AS latest_close,
                   (SELECT k.change_pct FROM {TABLE_DAILY} k
                     WHERE k.industry_code = w.industry_code
                     ORDER BY k.trade_date DESC LIMIT 1) AS latest_change_pct
            FROM {TABLE_WATCHED} w
            ORDER BY w.sort_order, w.created_at, w.industry_code
        """
        return self.db.query(sql)

    def add_watch(self, industry_code: str, industry_name: Optional[str] = None) -> Dict[str, Any]:
        """添加关注行业；行业不存在时允许显式传入名称"""
        self.ensure_schema()
        code = str(industry_code or "").strip().upper()
        if not code:
            raise ValueError("行业代码不能为空")

        existing = self.db.query_one(
            f"SELECT * FROM {TABLE_WATCHED} WHERE industry_code = ?", (code,)
        )
        if existing:
            return existing

        board = self.get_industry(code)
        name = (industry_name or "").strip()
        if not name and board:
            name = board.get("industry_name") or ""
        if not name:
            raise ValueError(f"行业 {code} 不在本地行业列表中，请先刷新行业列表或传入行业名称")
        source = (board.get("source") if board else None) or SOURCE

        now = datetime.now().isoformat(timespec="seconds")
        try:
            with self.db.transaction():
                self.db.execute(
                    f"""
                    INSERT INTO {TABLE_WATCHED}
                        (industry_code, industry_name, source, sort_order, created_at, updated_at)
                    VALUES (?, ?, ?, 0, ?, ?)
                    """,
                    (code, name, source, now, now),
                )
        except sqlite3.IntegrityError:
            # 并发下已被其他请求插入
            existing = self.db.query_one(
                f"SELECT * FROM {TABLE_WATCHED} WHERE industry_code = ?", (code,)
            )
            if existing:
                return existing
            raise

        logger.info("添加关注行业: %s %s", code, name)
        return self.db.query_one(
            f"SELECT * FROM {TABLE_WATCHED} WHERE industry_code = ?", (code,)
        )

    def remove_watch(self, industry_code: str, delete_data: bool = False) -> bool:
        self.ensure_schema()
        code = (industry_code or "").strip().upper()
        existing = self.db.query_one(
            f"SELECT id FROM {TABLE_WATCHED} WHERE industry_code = ?", (code,)
        )
        if not existing:
            return False
        with self.db.transaction():
            if delete_data:
                self.db.execute(
                    f"DELETE FROM {TABLE_DAILY} WHERE industry_code = ?", (code,)
                )
            self.db.execute(
                f"DELETE FROM {TABLE_WATCHED} WHERE industry_code = ?", (code,)
            )
        logger.info("取消关注行业: %s（删除日K=%s）", code, delete_data)
        return True

    # ==================== 行业日 K ====================

    @staticmethod
    def _fetch_hist_df(symbol: str, start: date, end: date) -> pd.DataFrame:
        """抓取行业指数日 K

        BKxxxx 直接请求东方财富 K 线接口（显式超时，等价于
        akshare.stock_board_industry_hist_em 的底层请求）；非 BK 代码退回 AKShare。
        """
        symbol = str(symbol).strip()
        if not symbol.upper().startswith("BK"):
            import akshare as ak

            return ak.stock_board_industry_hist_em(
                symbol=symbol,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                period="日k",
                adjust="",
            )

        params = {
            "secid": f"90.{symbol.upper()}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "0",
            "beg": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
            "lmt": "1000000",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Referer": "https://quote.eastmoney.com/",
        }
        errors: List[str] = []
        for host in ("7.push2his.eastmoney.com", "push2his.eastmoney.com"):
            try:
                response = requests.get(
                    f"https://{host}/api/qt/stock/kline/get",
                    params=params,
                    headers=headers,
                    timeout=(5, 30),
                )
                response.raise_for_status()
                payload = response.json() or {}
                data = payload.get("data") or {}
                klines = data.get("klines") or []
                if not klines:
                    return pd.DataFrame()

                rows = [str(line).split(",") for line in klines]
                df = pd.DataFrame(
                    rows,
                    columns=[
                        "日期",
                        "开盘",
                        "收盘",
                        "最高",
                        "最低",
                        "成交量",
                        "成交额",
                        "振幅",
                        "涨跌幅",
                        "涨跌额",
                        "换手率",
                    ],
                )
                for column in (
                    "开盘",
                    "收盘",
                    "最高",
                    "最低",
                    "成交量",
                    "成交额",
                    "振幅",
                    "涨跌幅",
                    "涨跌额",
                    "换手率",
                ):
                    df[column] = pd.to_numeric(df[column], errors="coerce")
                return df
            except Exception as exc:
                errors.append(f"{host}: {exc}")

        raise IndustryBoardError("东方财富行业日K接口不可用: " + "；".join(errors))

    @staticmethod
    def _fetch_hist_df_ths(symbol: str, start: date, end: date) -> pd.DataFrame:
        """同花顺行业指数日 K（东方财富日K不可用时的降级数据源）

        THS 接口只返回 OHLC/成交量/成交额，涨跌幅、涨跌额、振幅由收盘价计算；
        换手率无数据留空。多取 20 天用于计算区间首日涨跌幅。
        """
        import akshare as ak

        fetch_start = start - timedelta(days=20)
        df = ak.stock_board_industry_index_ths(
            symbol=symbol,
            start_date=fetch_start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if df is None or len(df) == 0:
            return pd.DataFrame()

        df = df.rename(
            columns={
                "开盘价": "开盘",
                "最高价": "最高",
                "最低价": "最低",
                "收盘价": "收盘",
            }
        ).copy()
        if "日期" not in df.columns:
            return pd.DataFrame()

        for column in ("开盘", "最高", "最低", "收盘", "成交量", "成交额"):
            if column in df.columns:
                df[column] = pd.to_numeric(df[column], errors="coerce")
            else:
                df[column] = None

        df = df.sort_values("日期").reset_index(drop=True)
        close = df["收盘"]
        prev_close = close.shift(1)
        df["涨跌幅"] = (close / prev_close - 1) * 100
        df["涨跌额"] = close - prev_close
        df["振幅"] = (df["最高"] - df["最低"]) / prev_close * 100
        df["换手率"] = None

        return df[
            [
                "日期",
                "开盘",
                "收盘",
                "最高",
                "最低",
                "成交量",
                "成交额",
                "振幅",
                "涨跌幅",
                "涨跌额",
                "换手率",
            ]
        ]

    def fetch_daily(
        self,
        industry_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """抓取并保存单个行业的日 K（行业指数）"""
        self.ensure_schema()
        code = (industry_code or "").strip().upper()
        if not code:
            raise ValueError("行业代码不能为空")

        watched = self.db.query_one(
            f"SELECT * FROM {TABLE_WATCHED} WHERE industry_code = ?", (code,)
        )
        board = self.get_industry(code)
        meta = watched or board
        if not meta:
            raise ValueError(f"未知行业: {code}，请先刷新行业列表并关注")
        industry_name = meta.get("industry_name") or code

        today = date.today()
        start = _to_date(start_date, today - timedelta(days=365))
        end = _to_date(end_date, today)
        if start > end:
            raise ValueError("开始日期不能晚于结束日期")

        source_pref = str(meta.get("source") or "").lower()
        errors: List[str] = []
        df: Optional[pd.DataFrame] = None
        data_source: Optional[str] = None

        # 东方财富行业代码（BKxxxx）优先；接口不可用时按行业名称降级到同花顺
        if code.upper().startswith("BK") and source_pref != SOURCE_THS:
            try:
                df = self._fetch_hist_df(code, start, end)
                data_source = SOURCE
            except Exception as exc:
                message = _friendly_error(exc)
                errors.append(f"东方财富：{message}")
                logger.warning("东方财富行业日K不可用，尝试同花顺降级 %s: %s", code, message)

        if df is None:
            try:
                df = self._fetch_hist_df_ths(industry_name, start, end)
                data_source = SOURCE_THS
            except Exception as exc:
                message = _friendly_error(exc)
                errors.append(f"同花顺：{message}")
                logger.error("抓取行业日K失败 %s %s: %s", code, industry_name, message)

        if df is None:
            raise IndustryBoardError(
                f"抓取 {industry_name}({code}) 日K失败：" + "；".join(errors)
            )

        if df is None or len(df) == 0:
            return {
                "industry_code": code,
                "industry_name": industry_name,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "fetched": 0,
                "saved": 0,
                "latest_date": None,
                "source": data_source,
                "message": "该区间内没有可保存的日K数据",
            }

        now = datetime.now().isoformat(timespec="seconds")
        sql = f"""
            INSERT INTO {TABLE_DAILY} (
                industry_code, industry_name, trade_date, open, high, low, close,
                change_pct, change_amount, volume, amount, amplitude, turnover_rate,
                source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(industry_code, trade_date) DO UPDATE SET
                industry_name = excluded.industry_name,
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                change_pct = excluded.change_pct,
                change_amount = excluded.change_amount,
                volume = excluded.volume,
                amount = excluded.amount,
                amplitude = excluded.amplitude,
                turnover_rate = excluded.turnover_rate,
                source = excluded.source,
                updated_at = excluded.updated_at
        """

        saved = 0
        latest: Optional[str] = None
        with self.db.transaction():
            for _, row in df.iterrows():
                try:
                    trade_date = _to_date(row.get("日期"), start)
                except ValueError:
                    logger.warning("跳过无法解析的行业日K日期: %s", row.get("日期"))
                    continue
                if trade_date < start or trade_date > end:
                    continue
                if latest is None or trade_date.isoformat() > latest:
                    latest = trade_date.isoformat()
                self.db.execute(
                    sql,
                    (
                        code,
                        industry_name,
                        trade_date.isoformat(),
                        _to_float(row.get("开盘")),
                        _to_float(row.get("最高")),
                        _to_float(row.get("最低")),
                        _to_float(row.get("收盘")),
                        _to_float(row.get("涨跌幅")),
                        _to_float(row.get("涨跌额")),
                        _to_float(row.get("成交量")),
                        _to_float(row.get("成交额")),
                        _to_float(row.get("振幅")),
                        _to_float(row.get("换手率")),
                        data_source,
                        now,
                        now,
                    ),
                )
                saved += 1

        logger.info("行业日K保存完成: %s %s，%s 条", code, industry_name, saved)
        return {
            "industry_code": code,
            "industry_name": industry_name,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "fetched": len(df),
            "saved": saved,
            "latest_date": latest,
            "source": data_source,
            "fallback": data_source != (source_pref or SOURCE),
        }

    def fetch_daily_many(
        self,
        codes: Iterable[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """批量抓取多个行业的日 K；单个失败不影响其他行业"""
        if isinstance(codes, str):
            codes = [codes]
        unique_codes: List[str] = []
        for code in codes or []:
            code = str(code or "").strip().upper()
            if code and code not in unique_codes:
                unique_codes.append(code)
        if not unique_codes:
            raise ValueError("没有需要抓取的行业，请先添加关注行业")

        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        for code in unique_codes:
            try:
                results.append(self.fetch_daily(code, start_date, end_date))
            except Exception as exc:
                logger.warning("批量抓取行业日K失败 %s: %s", code, exc)
                errors.append({"industry_code": code, "error": str(exc)})

        return {
            "results": results,
            "errors": errors,
            "saved": sum(item.get("saved", 0) for item in results),
            "requested": len(unique_codes),
        }

    def get_daily(
        self,
        industry_code: str,
        limit: int = 500,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询本地已保存的行业日 K，按日期升序返回"""
        self.ensure_schema()
        code = (industry_code or "").strip().upper()
        if not code:
            raise ValueError("行业代码不能为空")
        try:
            limit = max(1, min(int(limit), 5000))
        except (TypeError, ValueError):
            limit = 500

        sql = f"""
            SELECT industry_code, industry_name, trade_date, open, high, low, close,
                   change_pct, change_amount, volume, amount, amplitude,
                   turnover_rate, source, updated_at
            FROM {TABLE_DAILY}
            WHERE industry_code = ?
        """
        params: List[Any] = [code]
        if start_date:
            sql += " AND trade_date >= ?"
            params.append(_to_date(start_date, date.today()).isoformat())
        if end_date:
            sql += " AND trade_date <= ?"
            params.append(_to_date(end_date, date.today()).isoformat())
        sql += " ORDER BY trade_date DESC LIMIT ?"
        params.append(limit)

        rows = self.db.query(sql, tuple(params))
        rows.reverse()
        return rows

    def clear_daily(self, industry_code: str) -> int:
        self.ensure_schema()
        code = (industry_code or "").strip().upper()
        with self.db.transaction():
            cursor = self.db.execute(
                f"DELETE FROM {TABLE_DAILY} WHERE industry_code = ?", (code,)
            )
        return cursor.rowcount

    # ==================== 汇总 ====================

    def get_summary(self) -> Dict[str, Any]:
        self.ensure_schema()
        row = self.db.query_one(
            f"""
            SELECT
                (SELECT COUNT(*) FROM {TABLE_INDUSTRY}) AS industry_count,
                (SELECT COUNT(*) FROM {TABLE_WATCHED}) AS watched_count,
                (SELECT COUNT(*) FROM {TABLE_DAILY}) AS kline_rows,
                (SELECT MAX(trade_date) FROM {TABLE_DAILY}) AS latest_date,
                (SELECT MAX(updated_at) FROM {TABLE_INDUSTRY}) AS list_updated_at,
                (SELECT source FROM {TABLE_INDUSTRY}
                  GROUP BY source ORDER BY COUNT(*) DESC LIMIT 1) AS source
            """
        ) or {}
        return {
            "industry_count": row.get("industry_count") or 0,
            "watched_count": row.get("watched_count") or 0,
            "kline_rows": row.get("kline_rows") or 0,
            "latest_date": row.get("latest_date"),
            "list_updated_at": row.get("list_updated_at"),
            "source": row.get("source"),
        }


_service: Optional[IndustryBoardService] = None


def get_industry_service() -> IndustryBoardService:
    """进程内单例，避免重复建表检查"""
    global _service
    if _service is None:
        _service = IndustryBoardService()
    return _service
