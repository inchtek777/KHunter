"""
行业轮动回测服务

流程：
  1. 基于「关注行业」的行业指数计算轮动强度（动量 + 波动惩罚），选出前 2 个行业
  2. 从同花顺抓取行业成分股（当前成分，存在幸存者偏差）
  3. 在成分股中按「动量 + 趋势 + 低波动 + 流动性」综合打分，每个行业选 3 只
  4. 使用最近 10 年数据回测，等权持有，按周期调仓
  5. 网格搜索行业动量窗口 / 个股动量窗口 / 调仓周期，按总收益最大化选参数

数据源：
  - 行业指数：本地 industry_daily_kline（同花顺，已有 10 年）；缺失时用 AKShare 补
  - 成分股：同花顺行业详情页（带 v cookie 分页抓取），缓存 rotation_constituent
  - 个股日线：AKShare stock_zh_a_daily（新浪，前复权），缓存 rotation_stock_daily

风险提示：候选池为「当前行业成分股 + 流通市值前 N」，存在幸存者偏差与前视偏差；
参数在完整区间上优化也可能过拟合。结果仅用于研究，不构成投资建议。
"""

import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

from utils.global_db import get_global_db

logger = logging.getLogger(__name__)

_TASKS: Dict[str, Dict[str, Any]] = {}
_TASK_LOCK = threading.Lock()
_TASK_TTL_SECONDS = 3600
_MAX_WORKERS = 8

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rotation_constituent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    industry_code TEXT NOT NULL,
    industry_name TEXT,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    market_cap REAL,
    source TEXT NOT NULL DEFAULT 'ths',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(industry_code, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_rotation_constituent_industry
    ON rotation_constituent(industry_code);

CREATE TABLE IF NOT EXISTS rotation_stock_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    trade_date DATE NOT NULL,
    open REAL,
    close REAL,
    amount REAL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_rotation_stock_code_date
    ON rotation_stock_daily(code, trade_date);
"""


class RotationError(Exception):
    """轮动回测异常"""


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "" or value == "-":
            return default
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _parse_market_cap(text: Any) -> float:
    """把同花顺的流通市值（如 39.00亿 / 1.2万亿）解析成亿元"""
    if not text:
        return 0.0
    raw = str(text).strip().replace(",", "")
    if raw in ("-", ""):
        return 0.0
    try:
        if "万亿" in raw:
            return float(raw.replace("万亿", "")) * 10000.0
        if "亿" in raw:
            return float(raw.replace("亿", ""))
        if "万" in raw:
            return float(raw.replace("万", "")) / 10000.0
        return float(raw)
    except ValueError:
        return 0.0


def _sina_symbol(code: str) -> Optional[str]:
    code = str(code).strip()
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    # 北交所/其他市场新浪支持不稳定，先排除
    return None


def _is_st_name(name: Any) -> bool:
    text = str(name or "").upper().replace(" ", "")
    return "ST" in text or "退" in text


def _pct(value: float) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), 4)


# ==================== 后台任务 ====================


def _cleanup_tasks() -> None:
    now = time.time()
    for task_id in list(_TASKS.keys()):
        task = _TASKS[task_id]
        if task.get("status") in ("completed", "failed") and now - task.get("updated_at", now) > _TASK_TTL_SECONDS:
            _TASKS.pop(task_id, None)


def start_rotation_task(params: Dict[str, Any]) -> str:
    """启动后台回测任务，返回 task_id"""
    with _TASK_LOCK:
        _cleanup_tasks()
        for task in _TASKS.values():
            if task.get("status") == "running":
                raise RotationError("已有轮动回测任务在运行，请等待完成后再试")

        task_id = uuid.uuid4().hex[:12]
        _TASKS[task_id] = {
            "task_id": task_id,
            "status": "running",
            "progress": 0,
            "phase": "准备",
            "message": "任务已创建",
            "params": params,
            "result": None,
            "error": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    thread = threading.Thread(
        target=_run_task, args=(task_id, params), name=f"rotation-{task_id}", daemon=True
    )
    thread.start()
    return task_id


def _run_task(task_id: str, params: Dict[str, Any]) -> None:
    service = IndustryRotationService()
    try:
        result = service.run(task_id, params)
        _update_task(
            task_id,
            status="completed",
            progress=100,
            phase="完成",
            message="回测完成",
            result=result,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("行业轮动回测失败")
        _update_task(
            task_id,
            status="failed",
            progress=0,
            phase="失败",
            message=str(exc),
            error=str(exc),
        )


def _update_task(task_id: str, **kwargs: Any) -> None:
    kwargs["updated_at"] = time.time()
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if task:
            task.update(kwargs)


def get_rotation_task(task_id: str) -> Optional[Dict[str, Any]]:
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return None
        # 返回浅拷贝，避免调用方修改内部状态
        return dict(task)


# ==================== 服务 ====================


class IndustryRotationService:
    """行业轮动回测服务"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = str(db_path or get_global_db().db_path)
        self._ths_cookie: Optional[str] = None
        self._ths_session: Optional[requests.Session] = None
        self._ensure_schema()

    # ---------- 基础 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()

    def _progress(self, task_id: str, progress: int, phase: str, message: str) -> None:
        _update_task(
            task_id,
            progress=max(0, min(100, int(progress))),
            phase=phase,
            message=message,
        )

    def get_watched_industries(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT industry_code, industry_name, source
                FROM watched_industry
                ORDER BY sort_order, id
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_constituents(self, industry: Dict[str, Any], limit: int = 10000) -> List[Dict[str, Any]]:
        """获取行业成分股（初始化数据用，默认返回全部）"""
        return self._load_constituents(industry, limit)

    def ensure_industry_index_data(
        self, industries: List[Dict[str, Any]], years: int = 10
    ) -> pd.DataFrame:
        """确保关注行业有最近 N 年的行业指数数据（缺失时从同花顺补）"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT MAX(trade_date) AS max_date FROM industry_daily_kline"
            ).fetchone()
        finally:
            conn.close()
        if row and row["max_date"]:
            end = pd.Timestamp(str(row["max_date"])[:10]).date()
        else:
            end = datetime.now().date()
        try:
            start = end.replace(year=end.year - years)
        except ValueError:
            start = end - timedelta(days=365 * years)
        return self._load_industry_indices(industries, start, end)

    def get_config(self) -> Dict[str, Any]:
        """前端初始化信息：关注行业 + 行业指数覆盖 + 默认参数"""
        industries = self.get_watched_industries()
        conn = self._connect()
        try:
            coverage = {}
            for row in conn.execute(
                """
                SELECT industry_code, COUNT(*) AS n,
                       MIN(trade_date) AS min_date, MAX(trade_date) AS max_date
                FROM industry_daily_kline
                GROUP BY industry_code
                """
            ).fetchall():
                coverage[row["industry_code"]] = dict(row)
            latest = conn.execute(
                "SELECT MAX(trade_date) AS max_date FROM industry_daily_kline"
            ).fetchone()
        finally:
            conn.close()

        for item in industries:
            item.update(coverage.get(item["industry_code"], {}))
        latest_date = latest["max_date"] if latest else None
        return {
            "watched": industries,
            "watched_count": len(industries),
            "latest_date": latest_date,
            "can_run": len(industries) >= 2,
            "default_params": {
                "candidate_limit": 20,
                "optimize": True,
                "objective": "max_return",
                "rebalance_days": 5,
                "top_stocks": 2,
                "start_date": None,
                "end_date": latest_date,
            },
            "method": {
                "top_industries": 2,
                "top_stocks_per_industry": 2,
                "rebalance_days": 5,
                "holding": "等权持有，每 5 个交易日调仓",
                "cost_rate": 0.002,
            },
        }

    # ---------- 行业指数 ----------

    def _load_industry_indices(
        self, industries: List[Dict[str, Any]], start: date, end: date
    ) -> pd.DataFrame:
        codes = [item["industry_code"] for item in industries]
        placeholders = ",".join("?" for _ in codes)
        conn = self._connect()
        try:
            df = pd.read_sql_query(
                f"""
                SELECT industry_code, trade_date, close
                FROM industry_daily_kline
                WHERE industry_code IN ({placeholders})
                  AND trade_date BETWEEN ? AND ?
                """,
                conn,
                params=codes + [start.isoformat(), end.isoformat()],
            )
        finally:
            conn.close()

        if df is None or df.empty:
            raise RotationError("关注行业没有行业指数数据，请先在行业数据页拉取日K")

        pivot = df.pivot_table(
            index="trade_date", columns="industry_code", values="close", aggfunc="last"
        )
        pivot.index = pd.to_datetime(pivot.index)
        pivot = pivot.sort_index()

        missing = []
        for item in industries:
            code = item["industry_code"]
            if code not in pivot.columns or pivot[code].dropna().shape[0] < 100:
                missing.append(item)

        for item in missing:
            try:
                self._fetch_industry_index_ths(item, start, end)
            except Exception as exc:  # noqa: BLE001
                logger.warning("补齐行业指数失败 %s: %s", item["industry_name"], exc)

        if missing:
            conn = self._connect()
            try:
                df = pd.read_sql_query(
                    f"""
                    SELECT industry_code, trade_date, close
                    FROM industry_daily_kline
                    WHERE industry_code IN ({placeholders})
                      AND trade_date BETWEEN ? AND ?
                    """,
                    conn,
                    params=codes + [start.isoformat(), end.isoformat()],
                )
            finally:
                conn.close()
            if df is not None and not df.empty:
                pivot = df.pivot_table(
                    index="trade_date", columns="industry_code", values="close", aggfunc="last"
                )
                pivot.index = pd.to_datetime(pivot.index)
                pivot = pivot.sort_index()

        return pivot

    def _fetch_industry_index_ths(
        self, industry: Dict[str, Any], start: date, end: date
    ) -> None:
        import akshare as ak

        fetch_start = start - timedelta(days=400)
        df = ak.stock_board_industry_index_ths(
            symbol=industry["industry_name"],
            start_date=fetch_start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if df is None or df.empty:
            return

        df = df.rename(
            columns={
                "开盘价": "open",
                "最高价": "high",
                "最低价": "low",
                "收盘价": "close",
            }
        )
        df["trade_date"] = pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d")
        conn = self._connect()
        try:
            rows = [
                (
                    industry["industry_code"],
                    industry["industry_name"],
                    row["trade_date"],
                    _to_float(row.get("open")),
                    _to_float(row.get("high")),
                    _to_float(row.get("low")),
                    _to_float(row.get("close")),
                    _to_float(row.get("成交量")),
                    _to_float(row.get("成交额")),
                    "ths",
                    _now_text(),
                )
                for _, row in df.iterrows()
            ]
            conn.executemany(
                """
                INSERT INTO industry_daily_kline
                    (industry_code, industry_name, trade_date, open, high, low, close,
                     change_pct, change_amount, volume, amount, amplitude, turnover_rate,
                     source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, NULL, ?, ?, ?)
                ON CONFLICT(industry_code, trade_date) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    amount = excluded.amount,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                        r[7], r[8], r[9], r[10], r[10],
                    )
                    for r in rows
                ],
            )
            conn.commit()
        finally:
            conn.close()

    # ---------- 同花顺成分股 ----------

    def _ensure_ths_session(self) -> requests.Session:
        if self._ths_session is not None:
            return self._ths_session
        self._ths_session = requests.Session()
        self._refresh_ths_cookie()
        return self._ths_session

    def _refresh_ths_cookie(self) -> None:
        import py_mini_racer
        from akshare.datasets import get_ths_js

        js = py_mini_racer.MiniRacer()
        with open(get_ths_js("ths.js"), encoding="utf-8") as handle:
            js.eval(handle.read())
        self._ths_cookie = js.call("v")
        self._ensure_ths_session_headers()

    def _ensure_ths_session_headers(self) -> None:
        if self._ths_session is None:
            return
        self._ths_session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/89.0.4389.90 Safari/537.36"
                ),
                "Cookie": f"v={self._ths_cookie}",
            }
        )

    def _ths_get(self, url: str, retries: int = 3) -> requests.Response:
        session = self._ensure_ths_session()
        last_error: Optional[Exception] = None
        for attempt in range(retries):
            try:
                response = session.get(url, timeout=20)
                if response.status_code in (401, 403):
                    logger.info("同花顺 cookie 失效，刷新后重试")
                    self._refresh_ths_cookie()
                    time.sleep(0.3)
                    continue
                response.raise_for_status()
                return response
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.5)
        raise RotationError(f"同花顺请求失败: {url} ({last_error})")

    @staticmethod
    def _parse_constituent_rows(html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "lxml")
        rows: List[Dict[str, Any]] = []
        for tr in soup.select("table.m-table tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 13:
                continue
            code = cells[1].strip()
            if not (code.isdigit() and len(code) == 6):
                continue
            rows.append(
                {
                    "stock_code": code,
                    "stock_name": cells[2].strip(),
                    "market_cap": _parse_market_cap(cells[12]),
                }
            )
        return rows

    def _scrape_industry_constituents(
        self, industry_code: str, industry_name: str
    ) -> List[Dict[str, Any]]:
        base = f"http://q.10jqka.com.cn/thshy/detail/code/{industry_code}/"
        response = self._ths_get(base)
        soup = BeautifulSoup(response.text, "lxml")
        page_info = soup.find(class_="page_info")
        page_text = page_info.get_text(strip=True) if page_info else ""
        try:
            total_pages = int(page_text.split("/")[-1])
        except (ValueError, IndexError):
            total_pages = 1
        total_pages = max(1, min(total_pages, 20))

        rows = self._parse_constituent_rows(response.text)
        for page in range(2, total_pages + 1):
            page_url = (
                f"http://q.10jqka.com.cn/thshy/detail/code/{industry_code}/"
                f"page/{page}/ajax/1/"
            )
            try:
                page_response = self._ths_get(page_url)
            except RotationError as exc:
                logger.warning("行业 %s 第 %s 页抓取失败: %s", industry_name, page, exc)
                break
            page_rows = self._parse_constituent_rows(page_response.text)
            if not page_rows:
                break
            rows.extend(page_rows)
            time.sleep(0.15)

        if not rows:
            raise RotationError(f"同花顺行业 {industry_name} 未解析到成分股")

        # 去重，保留流通市值更大的记录
        dedup: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            code = row["stock_code"]
            previous = dedup.get(code)
            if previous is None or row["market_cap"] > previous["market_cap"]:
                dedup[code] = row

        result = []
        for row in dedup.values():
            if _is_st_name(row["stock_name"]):
                continue
            if _sina_symbol(row["stock_code"]) is None:
                continue
            row = dict(row)
            row["industry_code"] = industry_code
            row["industry_name"] = industry_name
            result.append(row)
        return result

    def _load_constituents(
        self,
        industry: Dict[str, Any],
        candidate_limit: int,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        code = industry["industry_code"]
        name = industry["industry_name"]
        conn = self._connect()
        try:
            cached = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT industry_code, industry_name, stock_code, stock_name, market_cap, updated_at
                    FROM rotation_constituent
                    WHERE industry_code = ?
                    ORDER BY market_cap DESC
                    """,
                    (code,),
                ).fetchall()
            ]
        finally:
            conn.close()

        fresh = False
        if cached:
            try:
                updated = datetime.fromisoformat(str(cached[0]["updated_at"]))
                fresh = (datetime.now() - updated).total_seconds() < 7 * 86400
            except ValueError:
                fresh = False

        if cached and fresh and not force:
            return cached[:candidate_limit]

        try:
            scraped = self._scrape_industry_constituents(code, name)
        except Exception as exc:  # noqa: BLE001
            if cached:
                logger.warning("行业 %s 成分股刷新失败，使用缓存: %s", name, exc)
                return cached[:candidate_limit]
            raise RotationError(f"抓取行业 {name} 成分股失败: {exc}") from exc

        now = _now_text()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM rotation_constituent WHERE industry_code = ?", (code,)
                )
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO rotation_constituent
                        (industry_code, industry_name, stock_code, stock_name,
                         market_cap, source, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'ths', ?)
                    """,
                    [
                        (
                            code,
                            name,
                            row["stock_code"],
                            row["stock_name"],
                            row["market_cap"],
                            now,
                        )
                        for row in scraped
                    ],
                )
            conn.commit()
        finally:
            conn.close()

        scraped.sort(key=lambda item: item["market_cap"], reverse=True)
        return scraped[:candidate_limit]

    # ---------- 个股行情 ----------

    def _read_stock_cache(
        self, codes: List[str], start: date, end: date
    ) -> Tuple[pd.DataFrame, List[str]]:
        if not codes:
            return pd.DataFrame(), []
        placeholders = ",".join("?" for _ in codes)
        conn = self._connect()
        try:
            coverage = {
                row["code"]: row
                for row in conn.execute(
                    f"""
                    SELECT code, MIN(trade_date) AS min_date, MAX(trade_date) AS max_date,
                           COUNT(*) AS n
                    FROM rotation_stock_daily
                    WHERE code IN ({placeholders})
                    GROUP BY code
                    """,
                    codes,
                ).fetchall()
            }
            missing = [
                code
                for code in codes
                if code not in coverage
                or coverage[code]["n"] < 200
                or str(coverage[code]["max_date"] or "") < (end - timedelta(days=10)).isoformat()
            ]
            df = pd.read_sql_query(
                f"""
                SELECT code, trade_date, open, close, amount
                FROM rotation_stock_daily
                WHERE code IN ({placeholders})
                  AND trade_date BETWEEN ? AND ?
                """,
                conn,
                params=codes + [start.isoformat(), end.isoformat()],
            )
        finally:
            conn.close()
        return df, missing

    def _fetch_one_stock(
        self, code: str, fetch_start: date, end: date
    ) -> Tuple[str, Optional[pd.DataFrame]]:
        symbol = _sina_symbol(code)
        if symbol is None:
            return code, None
        import akshare as ak

        last_error: Optional[Exception] = None
        for _attempt in range(2):
            try:
                df = ak.stock_zh_a_daily(
                    symbol=symbol,
                    start_date=fetch_start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust="qfq",
                )
                if df is None or df.empty:
                    return code, None
                df = df.copy()
                df["trade_date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                df["open"] = pd.to_numeric(df["open"], errors="coerce")
                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0.0)
                df = df[["trade_date", "open", "close", "amount"]]
                df = df[(df["close"] > 0) & (df["trade_date"] <= end.isoformat())]
                return code, df
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.5)
        logger.warning("抓取个股 %s 历史失败: %s", code, last_error)
        return code, None

    def _load_stock_data(
        self,
        codes: List[str],
        start: date,
        end: date,
        task_id: str,
        progress_base: int,
        progress_span: int,
    ) -> pd.DataFrame:
        fetch_start = start - timedelta(days=400)
        cached_df, missing = self._read_stock_cache(codes, fetch_start, end)

        if missing:
            total = len(missing)
            done = 0
            fetched_frames: List[pd.DataFrame] = []
            with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
                futures = {
                    executor.submit(self._fetch_one_stock, code, fetch_start, end): code
                    for code in missing
                }
                for future in as_completed(futures):
                    code, frame = future.result()
                    done += 1
                    if frame is not None and not frame.empty:
                        fetched_frames.append(frame.assign(code=code))
                    if done == total or done % 5 == 0:
                        pct = progress_base + int(progress_span * done / max(total, 1))
                        self._progress(
                            task_id,
                            pct,
                            "下载个股行情",
                            f"个股行情 {done}/{total}（{code}）",
                        )

            if fetched_frames:
                conn = self._connect()
                try:
                    now = _now_text()
                    with conn:
                        for frame in fetched_frames:
                            code = frame["code"].iloc[0]
                            conn.executemany(
                                """
                                INSERT OR REPLACE INTO rotation_stock_daily
                                    (code, trade_date, open, close, amount, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                [
                                    (
                                        code,
                                        row["trade_date"],
                                        _to_float(row["open"]),
                                        _to_float(row["close"]),
                                        _to_float(row["amount"], 0.0),
                                        now,
                                    )
                                    for _, row in frame.iterrows()
                                ],
                            )
                    conn.commit()
                finally:
                    conn.close()

            # 重新读取缓存，确保使用完整数据
            cached_df, _ = self._read_stock_cache(codes, fetch_start, end)

        if cached_df is None or cached_df.empty:
            raise RotationError("没有可用的个股行情数据")
        return cached_df

    # ---------- 回测 ----------

    @staticmethod
    def _industry_score_matrix(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
        close = close.ffill()
        momentum = close / close.shift(lookback) - 1
        volatility = close.pct_change().rolling(60).std() * math.sqrt(252)
        return momentum - 0.15 * volatility

    @staticmethod
    def _stock_score_matrix(
        close: pd.DataFrame, amount: pd.DataFrame, lookback: int
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        close = close.ffill()
        momentum = close / close.shift(lookback) - 1
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()
        volatility = close.pct_change().rolling(60).std() * math.sqrt(252)
        liquidity = amount.rolling(20).mean()
        trend = ((close > ma20) & (ma20 > ma60)).astype(float)
        score = momentum - 0.15 * volatility + 0.10 * trend
        return score, liquidity

    def _run_combo(
        self,
        ind_close: pd.DataFrame,
        stock_close: pd.DataFrame,
        stock_open: pd.DataFrame,
        stock_amount: pd.DataFrame,
        members: Dict[str, List[Dict[str, Any]]],
        ind_lookback: int,
        stock_lookback: int,
        rebalance_days: int,
        start_dt: pd.Timestamp,
        end_dt: pd.Timestamp,
        top_industries: int = 2,
        top_stocks: int = 2,
        cost_rate: float = 0.002,
    ) -> Optional[Dict[str, Any]]:
        ind_score = self._industry_score_matrix(ind_close, ind_lookback)
        stock_score, liquidity = self._stock_score_matrix(
            stock_close, stock_amount, stock_lookback
        )

        calendar = ind_close.index
        start_pos = int(calendar.searchsorted(start_dt))
        end_pos = int(calendar.searchsorted(end_dt, side="right")) - 1
        if end_pos - start_pos < 150:
            return None

        warmup = max(120, ind_lookback, stock_lookback) + 1
        signal_positions = list(range(start_pos + warmup, end_pos, rebalance_days))
        if len(signal_positions) < 6:
            return None

        nav = 1.0
        curve: List[Tuple[str, float]] = [(calendar[start_pos].strftime("%Y-%m-%d"), 1.0)]
        period_returns: List[float] = []
        trades: List[Dict[str, Any]] = []
        rotations: List[Dict[str, Any]] = []
        latest_picks: List[Dict[str, Any]] = []

        for pos in signal_positions:
            signal_date = calendar[pos]
            entry_pos = pos + 1
            exit_pos = min(pos + rebalance_days + 1, end_pos)
            if entry_pos >= len(calendar) or exit_pos <= entry_pos:
                continue

            # 1) 行业轮动
            ind_row = ind_score.loc[signal_date].dropna()
            if len(ind_row) < 2:
                continue
            selected_industries = list(
                ind_row.sort_values(ascending=False).head(max(1, top_industries)).index
            )

            # 2) 行业内选股
            picks: List[Dict[str, Any]] = []
            for industry_code in selected_industries:
                candidates = [
                    item for item in members.get(industry_code, [])
                    if item["stock_code"] in stock_score.columns
                ]
                if not candidates:
                    continue
                candidate_codes = [item["stock_code"] for item in candidates]
                scores = stock_score.loc[signal_date, candidate_codes].dropna()
                if scores.empty:
                    continue
                liq = liquidity.loc[signal_date, candidate_codes].reindex(scores.index)
                scores = scores[liq.fillna(0) > 1_000_000]
                if scores.empty:
                    continue
                for code in scores.sort_values(ascending=False).head(max(1, top_stocks)).index:
                    name = next(
                        (item["stock_name"] for item in candidates if item["stock_code"] == code),
                        code,
                    )
                    picks.append(
                        {
                            "industry_code": industry_code,
                            "industry_name": industry_code,
                            "stock_code": code,
                            "stock_name": name,
                            "score": _pct(scores[code] * 100),
                        }
                    )

            if not picks:
                continue

            # 行业名称映射
            for pick in picks:
                industry = next(
                    (item for item in members.get(pick["industry_code"], []) if item["stock_code"] == pick["stock_code"]),
                    None,
                )
                if industry:
                    pick["industry_name"] = industry.get("industry_name") or pick["industry_code"]
                else:
                    pick["industry_name"] = pick["industry_code"]

            entry_date = calendar[entry_pos]
            exit_date = calendar[exit_pos]
            nav_before = nav
            nav *= (1.0 - cost_rate)

            # 3) 逐日收益（买入日按开盘价，持有到下一次调仓开盘）
            for day_pos in range(entry_pos, exit_pos):
                day = calendar[day_pos]
                daily_returns: List[float] = []
                for pick in picks:
                    code = pick["stock_code"]
                    try:
                        if day_pos == entry_pos:
                            open_price = stock_open.at[day, code]
                            close_price = stock_close.at[day, code]
                            if pd.isna(open_price) or pd.isna(close_price) or open_price <= 0:
                                continue
                            daily_returns.append(close_price / open_price - 1)
                        else:
                            prev_close = stock_close.at[calendar[day_pos - 1], code]
                            close_price = stock_close.at[day, code]
                            if pd.isna(prev_close) or pd.isna(close_price) or prev_close <= 0:
                                continue
                            daily_returns.append(close_price / prev_close - 1)
                    except KeyError:
                        continue
                if daily_returns:
                    nav *= (1.0 + float(np.mean(daily_returns)))
                curve.append((day.strftime("%Y-%m-%d"), nav))

            period_return = nav / nav_before - 1
            period_returns.append(period_return)

            # 4) 交易记录
            for pick in picks:
                code = pick["stock_code"]
                try:
                    buy_price = stock_open.at[entry_date, code]
                    if pd.isna(buy_price) or buy_price <= 0:
                        buy_price = stock_close.at[entry_date, code]
                    sell_price = stock_open.at[exit_date, code]
                    if pd.isna(sell_price) or sell_price <= 0:
                        sell_price = stock_close.at[exit_date, code]
                except KeyError:
                    continue
                if pd.isna(buy_price) or pd.isna(sell_price) or buy_price <= 0:
                    continue
                trades.append(
                    {
                        "entry_date": entry_date.strftime("%Y-%m-%d"),
                        "exit_date": exit_date.strftime("%Y-%m-%d"),
                        "industry_code": pick["industry_code"],
                        "industry_name": pick["industry_name"],
                        "stock_code": code,
                        "stock_name": pick["stock_name"],
                        "buy_price": round(float(buy_price), 2),
                        "sell_price": round(float(sell_price), 2),
                        "return_pct": _pct((sell_price / buy_price - 1) * 100),
                        "score": pick["score"],
                    }
                )

            latest_picks = []
            for pick in picks:
                code = pick["stock_code"]
                try:
                    entry_price = stock_open.at[entry_date, code]
                    if pd.isna(entry_price) or entry_price <= 0:
                        entry_price = stock_close.at[entry_date, code]
                    current_price = stock_close.at[calendar[exit_pos], code]
                except KeyError:
                    continue
                if pd.isna(entry_price) or pd.isna(current_price):
                    continue
                latest_picks.append(
                    {
                        **pick,
                        "entry_date": entry_date.strftime("%Y-%m-%d"),
                        "entry_price": round(float(entry_price), 2),
                        "current_price": round(float(current_price), 2),
                        "return_pct": _pct((current_price / entry_price - 1) * 100),
                    }
                )
            rotations.append(
                {
                    "date": signal_date.strftime("%Y-%m-%d"),
                    "industries": [
                        {
                            "industry_code": code,
                            "industry_name": code,
                            "score": _pct(ind_row[code] * 100),
                        }
                        for code in selected_industries
                    ],
                    "picks": [f"{p['stock_name']}({p['stock_code']})" for p in picks],
                }
            )

        if len(period_returns) < 3:
            return None

        nav_series = pd.Series(
            [value for _, value in curve],
            index=pd.to_datetime([day for day, _ in curve]),
            dtype=float,
        )
        max_drawdown = float((nav_series / nav_series.cummax() - 1).min())
        daily_returns = nav_series.pct_change().dropna()
        years = max((nav_series.index[-1] - nav_series.index[0]).days / 365.25, 1e-9)
        total_return = float(nav_series.iloc[-1] / nav_series.iloc[0] - 1)
        cagr = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1.0
        sharpe = (
            float(daily_returns.mean() / daily_returns.std() * math.sqrt(252))
            if len(daily_returns) > 1 and daily_returns.std() > 0
            else 0.0
        )
        win_rate = float(np.mean([r > 0 for r in period_returns]))
        turnover = 1.0  # 每期全换仓（选股可能重复，此处按全换仓估算）

        return {
            "params": {
                "industry_lookback": ind_lookback,
                "stock_lookback": stock_lookback,
                "rebalance_days": rebalance_days,
                "top_industries": top_industries,
                "top_stocks": top_stocks,
            },
            "metrics": {
                "total_return": _pct(total_return),
                "cagr": _pct(cagr),
                "max_drawdown": _pct(max_drawdown),
                "sharpe": _pct(sharpe),
                "win_rate": _pct(win_rate),
                "annual_volatility": _pct(float(daily_returns.std() * math.sqrt(252)) if len(daily_returns) > 1 else 0.0),
                "turnover": _pct(turnover),
                "period_count": len(period_returns),
                "trade_count": len(trades),
                "final_nav": _pct(float(nav_series.iloc[-1])),
            },
            "curve": [
                {"date": day, "nav": round(float(value), 4)}
                for day, value in curve
            ],
            "trades": trades,
            "rotations": rotations,
            "latest_picks": latest_picks,
        }

    @staticmethod
    def _metrics_from_curve(curve: List[Dict[str, Any]]) -> Dict[str, float]:
        if len(curve) < 2:
            return {}
        nav = pd.Series(
            [item["nav"] for item in curve],
            index=pd.to_datetime([item["date"] for item in curve]),
            dtype=float,
        )
        ret = nav.pct_change().dropna()
        years = max((nav.index[-1] - nav.index[0]).days / 365.25, 1e-9)
        total = float(nav.iloc[-1] / nav.iloc[0] - 1)
        return {
            "total_return": _pct(total),
            "cagr": _pct((1 + total) ** (1 / years) - 1),
            "max_drawdown": _pct(float((nav / nav.cummax() - 1).min())),
            "sharpe": _pct(
                float(ret.mean() / ret.std() * math.sqrt(252))
                if len(ret) > 1 and ret.std() > 0
                else 0.0
            ),
            "annual_volatility": _pct(float(ret.std() * math.sqrt(252)) if len(ret) > 1 else 0.0),
        }

    def _run_backtest(
        self,
        industries: List[Dict[str, Any]],
        ind_close: pd.DataFrame,
        stock_df: pd.DataFrame,
        members: Dict[str, List[Dict[str, Any]]],
        task_id: str,
        start_dt: pd.Timestamp,
        end_dt: pd.Timestamp,
        candidate_limit: int,
        optimize: bool,
        objective: str = "max_return",
        rebalance_days: int = 5,
        top_stocks: int = 2,
    ) -> Dict[str, Any]:
        stock_close = stock_df.pivot_table(
            index="trade_date", columns="code", values="close", aggfunc="last"
        )
        stock_open = stock_df.pivot_table(
            index="trade_date", columns="code", values="open", aggfunc="last"
        )
        stock_amount = stock_df.pivot_table(
            index="trade_date", columns="code", values="amount", aggfunc="last"
        )
        for frame in (stock_close, stock_open, stock_amount):
            frame.index = pd.to_datetime(frame.index)
            frame.sort_index(inplace=True)
        stock_amount = stock_amount.fillna(0.0)

        # 只保留有行业指数的交易日
        calendar = ind_close.index
        stock_close = stock_close.reindex(calendar)
        stock_open = stock_open.reindex(calendar)
        stock_amount = stock_amount.reindex(calendar).fillna(0.0)

        # 行业名称映射
        industry_names = {item["industry_code"]: item["industry_name"] for item in industries}

        # 调仓周期固定（本轮目标：每 5 个交易日），只优化行业/个股动量窗口
        combos = (
            [
                (ind_lb, stock_lb, rebalance_days)
                for ind_lb in (20, 60, 120)
                for stock_lb in (20, 60, 120)
            ]
            if optimize
            else [(60, 60, rebalance_days)]
        )

        all_results: List[Dict[str, Any]] = []
        for index, (ind_lb, stock_lb, rb) in enumerate(combos):
            result = self._run_combo(
                ind_close=ind_close,
                stock_close=stock_close,
                stock_open=stock_open,
                stock_amount=stock_amount,
                members=members,
                ind_lookback=ind_lb,
                stock_lookback=stock_lb,
                rebalance_days=rb,
                start_dt=start_dt,
                end_dt=end_dt,
                top_industries=2,
                top_stocks=top_stocks,
            )
            if result:
                curve = result.get("curve", [])
                split_index = max(1, int(len(curve) * 0.7))
                result["in_sample_metrics"] = self._metrics_from_curve(curve[:split_index])
                result["oos_metrics"] = self._metrics_from_curve(curve[split_index:])
                all_results.append(result)
            pct = 75 + int(20 * (index + 1) / len(combos))
            self._progress(
                task_id,
                pct,
                "参数优化",
                f"参数组合 {index + 1}/{len(combos)}：行业{ind_lb}日 / 个股{stock_lb}日 / 调仓{rb}日",
            )

        if not all_results:
            raise RotationError("所有参数组合都无法产生有效交易，请扩大关注行业或候选池")

        # 参数选择：
        #   robust     -> 最大化「样本内年化」与「样本外年化」中的较小值，抑制过拟合
        #   max_return -> 直接按全区间总收益最大化（用户明确要最高收益时使用）
        def robust_score(item: Dict[str, Any]) -> float:
            in_cagr = (item.get("in_sample_metrics") or {}).get("cagr")
            oos_cagr = (item.get("oos_metrics") or {}).get("cagr")
            if in_cagr is None or oos_cagr is None:
                return -999.0
            return min(float(in_cagr), float(oos_cagr))

        objective = (objective or "robust").lower()
        if objective == "max_return":
            all_results.sort(
                key=lambda item: item["metrics"].get("total_return") or -1,
                reverse=True,
            )
        else:
            all_results.sort(key=robust_score, reverse=True)
        best = all_results[0]
        oos_metrics = best.get("oos_metrics", {})

        # 同时保留「全区间收益最高」的参数，供用户对照
        max_return_combo = max(
            all_results,
            key=lambda item: (item["metrics"].get("total_return") or -1),
        )

        # 基准：关注行业等权
        bench_close = ind_close.ffill()
        bench_ret = bench_close.pct_change().mean(axis=1)
        bench_curve_series = (1 + bench_ret.fillna(0)).cumprod()
        bench_curve_series = bench_curve_series[
            (bench_curve_series.index >= start_dt) & (bench_curve_series.index <= end_dt)
        ]
        if not bench_curve_series.empty:
            bench_curve_series = bench_curve_series / bench_curve_series.iloc[0]
        benchmark_curve = [
            {"date": day.strftime("%Y-%m-%d"), "nav": round(float(value), 4)}
            for day, value in bench_curve_series.items()
        ]
        benchmark_metrics = self._metrics_from_curve(benchmark_curve)

        # 行业名称补充到轮动记录
        for rotation in best.get("rotations", []):
            for industry in rotation["industries"]:
                industry["industry_name"] = industry_names.get(
                    industry["industry_code"], industry["industry_code"]
                )

        # 精简交易记录，避免响应过大
        trades = best.get("trades", [])
        return {
            "task_id": task_id,
            "generated_at": _now_text(),
            "source": "ths+em",
            "data_range": {
                "start": start_dt.strftime("%Y-%m-%d"),
                "end": end_dt.strftime("%Y-%m-%d"),
                "years": round((end_dt - start_dt).days / 365.25, 2),
            },
            "universe": {
                "watched_industries": industries,
                "candidate_limit": candidate_limit,
                "top_industries": 2,
                "top_stocks_per_industry": top_stocks,
                "rebalance_days": rebalance_days,
                "constituent_count": sum(len(items) for items in members.values()),
                "stock_count": int(stock_close.shape[1]),
            },
            "optimization": {
                "objective": "max_return" if objective == "max_return" else "maximin_cagr",
                "description": (
                    "按全区间总收益最大化"
                    if objective == "max_return"
                    else "最大化样本内(前70%)与样本外(后30%)年化收益的较小值"
                ),
                "split_ratio": 0.7,
            },
            "best_params": best["params"],
            "metrics": best["metrics"],
            "oos_metrics": oos_metrics,
            "benchmark_metrics": benchmark_metrics,
            "equity_curve": best["curve"],
            "benchmark_curve": benchmark_curve,
            "top_combos": [
                {
                    "params": item["params"],
                    "metrics": item["metrics"],
                    "in_sample_metrics": item.get("in_sample_metrics", {}),
                    "oos_metrics": item.get("oos_metrics", {}),
                    "robust_score": _pct(robust_score(item) or 0.0),
                }
                for item in all_results[:5]
            ],
            "max_return_combo": {
                "params": max_return_combo["params"],
                "metrics": max_return_combo["metrics"],
                "in_sample_metrics": max_return_combo.get("in_sample_metrics", {}),
                "oos_metrics": max_return_combo.get("oos_metrics", {}),
            },
            "latest_picks": best.get("latest_picks", [])[:6],
            "rotations": best.get("rotations", [])[-12:],
            "trades": trades[-300:],
            "warnings": [
                "候选池为当前同花顺行业成分股，存在幸存者偏差（历史退市/调出成分未纳入）。",
                f"候选池按当前流通市值取前 {candidate_limit} 只，存在一定前视偏差。",
                (
                    "参数选择目标为「全区间总收益最大化」，过拟合风险更高，请重点对照样本外指标。"
                    if objective == "max_return"
                    else "参数选择目标为最大化「样本内年化」与「样本外年化」的较小值（稳健），避免只挑全区间最高收益的过拟合参数。"
                ),
                "回测结果仍受候选池偏差影响，历史收益不代表未来。",
                "回测按调仓日次日开盘成交，双边交易成本合计 0.2%，未考虑涨跌停/停牌无法成交。",
            ],
        }

    # ---------- 任务入口 ----------

    def run(self, task_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._progress(task_id, 1, "准备", "读取关注行业")
        industries = self.get_watched_industries()
        if len(industries) < 2:
            raise RotationError("请先在「行业数据」页关注至少 2 个行业")

        candidate_limit = int(params.get("candidate_limit", 20) or 20)
        candidate_limit = max(5, min(candidate_limit, 100))
        optimize = bool(params.get("optimize", True))
        objective = str(params.get("objective") or "max_return").lower()
        rebalance_days = max(1, min(int(params.get("rebalance_days", 5) or 5), 60))
        top_stocks = max(1, min(int(params.get("top_stocks", 2) or 2), 5))

        # 最近 10 年（以本地行业指数最新日期为终点）
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT MAX(trade_date) AS max_date FROM industry_daily_kline"
            ).fetchone()
        finally:
            conn.close()
        end_text = params.get("end_date") or (row["max_date"] if row else None)
        if not end_text:
            raise RotationError("没有行业指数数据，请先在行业数据页拉取日K")
        end_dt = pd.Timestamp(str(end_text)[:10])

        start_text = params.get("start_date")
        if start_text:
            start_dt = pd.Timestamp(str(start_text)[:10])
        else:
            try:
                start_dt = end_dt.replace(year=end_dt.year - 10)
            except ValueError:
                start_dt = end_dt - timedelta(days=3650)
        if start_dt >= end_dt:
            raise RotationError("开始日期必须早于结束日期")

        self._progress(task_id, 4, "行业指数", "加载关注行业指数（10 年）")
        ind_close = self._load_industry_indices(industries, start_dt, end_dt)
        available_industries = [
            item for item in industries if item["industry_code"] in ind_close.columns
        ]
        if len(available_industries) < 2:
            raise RotationError("有行业指数数据的关注行业不足 2 个")

        # 成分股
        members: Dict[str, List[Dict[str, Any]]] = {}
        total_industries = len(available_industries)
        for index, industry in enumerate(available_industries):
            pct = 5 + int(10 * index / max(total_industries, 1))
            self._progress(
                task_id,
                pct,
                "行业成分股",
                f"抓取行业成分股 {index + 1}/{total_industries}：{industry['industry_name']}",
            )
            members[industry["industry_code"]] = self._load_constituents(
                industry, candidate_limit
            )

        # 去重候选股
        candidate_codes: List[str] = []
        seen = set()
        for items in members.values():
            for item in items:
                code = item["stock_code"]
                if code not in seen:
                    seen.add(code)
                    candidate_codes.append(code)
        if len(candidate_codes) < 4:
            raise RotationError("候选股票不足 4 只，无法构建组合")

        self._progress(task_id, 16, "个股行情", f"准备下载 {len(candidate_codes)} 只候选股 10 年行情")
        stock_df = self._load_stock_data(
            candidate_codes,
            start_dt,
            end_dt,
            task_id,
            progress_base=16,
            progress_span=58,
        )

        self._progress(task_id, 75, "回测", "开始行业轮动回测与参数优化")
        result = self._run_backtest(
            industries=available_industries,
            ind_close=ind_close,
            stock_df=stock_df,
            members=members,
            task_id=task_id,
            start_dt=start_dt,
            end_dt=end_dt,
            candidate_limit=candidate_limit,
            optimize=optimize,
            objective=objective,
            rebalance_days=rebalance_days,
            top_stocks=top_stocks,
        )
        self._progress(task_id, 99, "整理结果", "生成净值曲线与交易明细")
        _update_task(task_id, result=result, progress=100, phase="完成", message="回测完成")
        return result
