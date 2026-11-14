/**
 * 行业数据模块
 *
 * 功能：
 *  - 拉取/展示东方财富行业板块列表
 *  - 添加/取消关注行业
 *  - 抓取并保存关注行业的行业指数日K
 *  - 查看已保存的日K明细
 */

import { escapeHtml } from './utils.js';

const state = {
    industries: [],
    watched: [],
    keyword: '',
    selectedCode: null,
    selectedName: '',
    bound: false,
    busy: false,
    loadingList: false,
};

function $(id) {
    return document.getElementById(id);
}

function pad2(value) {
    return String(value).padStart(2, '0');
}

function todayStr() {
    const now = new Date();
    return `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`;
}

function oneYearAgoStr() {
    const now = new Date();
    const past = new Date(now.getFullYear() - 1, now.getMonth(), now.getDate());
    return `${past.getFullYear()}-${pad2(past.getMonth() + 1)}-${pad2(past.getDate())}`;
}

function fmtNum(value, digits = 2) {
    if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) {
        return '--';
    }
    return Number(value).toFixed(digits);
}

function fmtPct(value) {
    if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) {
        return '--';
    }
    const num = Number(value);
    return `${num > 0 ? '+' : ''}${num.toFixed(2)}%`;
}

function pctClass(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
        return '';
    }
    const num = Number(value);
    if (num > 0) return 'industry-up';
    if (num < 0) return 'industry-down';
    return '';
}

function fmtAmountYi(value) {
    if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) {
        return '--';
    }
    return (Number(value) / 1e8).toFixed(2);
}

function fmtCount(value) {
    if (value === null || value === undefined || value === '') return '--';
    return Number(value).toLocaleString('zh-CN');
}

function sourceLabel(source) {
    if (source === 'ths') return '同花顺（备用）';
    if (source === 'eastmoney') return '东方财富';
    return source || '--';
}

function setStatus(id, message, type = '') {
    const el = $(id);
    if (!el) return;
    el.textContent = message || '';
    el.className = 'industry-status' + (type ? ` ${type}` : '');
}

async function requestJson(url, options = {}, timeoutMs = 30000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let response;
    try {
        response = await fetch(url, { ...options, signal: controller.signal });
    } catch (error) {
        if (error.name === 'AbortError') {
            throw new Error('请求超时，请检查网络后重试');
        }
        throw error;
    } finally {
        clearTimeout(timer);
    }

    let payload = null;
    try {
        payload = await response.json();
    } catch (error) {
        payload = null;
    }
    if (!response.ok) {
        const message = (payload && (payload.error || payload.message)) || `HTTP ${response.status}`;
        const error = new Error(message);
        error.status = response.status;
        error.payload = payload;
        throw error;
    }
    if (!payload) {
        throw new Error('接口返回为空');
    }
    if (payload.success === false && !payload.data) {
        throw new Error(payload.error || '请求失败');
    }
    return payload;
}

function setBusy(busy) {
    state.busy = busy;
    document.querySelectorAll('#industry-page button').forEach((button) => {
        button.disabled = busy;
    });
}

function findIndustry(code) {
    return (
        state.industries.find((item) => item.industry_code === code) ||
        state.watched.find((item) => item.industry_code === code) || { industry_code: code, industry_name: code }
    );
}

export async function initIndustryPage() {
    bindEvents();

    const startInput = $('industry-start-date');
    const endInput = $('industry-end-date');
    if (startInput && !startInput.value) startInput.value = oneYearAgoStr();
    if (endInput && !endInput.value) endInput.value = todayStr();

    await Promise.all([loadSummary(), loadWatched(), loadIndustryList(false)]);
}

function bindEvents() {
    if (state.bound) return;
    state.bound = true;

    $('industry-refresh-btn')?.addEventListener('click', () => loadIndustryList(true));
    $('industry-search')?.addEventListener('input', (event) => {
        state.keyword = event.target.value.trim();
        renderIndustries();
    });
    $('industry-list-tbody')?.addEventListener('click', onListClick);
    $('industry-watched-tbody')?.addEventListener('click', onWatchedClick);
    $('industry-fetch-all-btn')?.addEventListener('click', () => {
        const codes = state.watched.map((item) => item.industry_code);
        fetchDaily(codes);
    });
    $('industry-detail-fetch-btn')?.addEventListener('click', () => {
        if (state.selectedCode) fetchDaily([state.selectedCode]);
    });
    $('industry-detail-close-btn')?.addEventListener('click', () => {
        state.selectedCode = null;
        state.selectedName = '';
        const card = $('industry-detail-card');
        if (card) card.style.display = 'none';
    });
}

async function onListClick(event) {
    const button = event.target.closest('button[data-action]');
    if (!button || state.busy) return;
    const code = button.dataset.code;
    if (!code) return;
    if (button.dataset.action === 'watch') {
        await watchIndustry(code);
    } else if (button.dataset.action === 'unwatch') {
        await unwatchIndustry(code);
    }
}

async function onWatchedClick(event) {
    const button = event.target.closest('button[data-action]');
    if (!button || state.busy) return;
    const code = button.dataset.code;
    if (!code) return;
    const item = findIndustry(code);
    if (button.dataset.action === 'detail') {
        await loadDaily(code, item.industry_name);
    } else if (button.dataset.action === 'fetch') {
        await fetchDaily([code]);
    } else if (button.dataset.action === 'unwatch') {
        await unwatchIndustry(code);
    }
}

async function loadSummary() {
    try {
        const payload = await requestJson('/api/industry/summary');
        const data = payload.data || {};
        if ($('industry-stat-industries')) $('industry-stat-industries').textContent = fmtCount(data.industry_count);
        if ($('industry-stat-watched')) $('industry-stat-watched').textContent = fmtCount(data.watched_count);
        if ($('industry-stat-rows')) $('industry-stat-rows').textContent = fmtCount(data.kline_rows);
        if ($('industry-stat-latest')) $('industry-stat-latest').textContent = data.latest_date || '-';
    } catch (error) {
        console.error('加载行业汇总失败:', error);
    }
}

async function loadIndustryList(refresh) {
    if (state.loadingList) return;
    state.loadingList = true;

    const tbody = $('industry-list-tbody');
    if (refresh && tbody) {
        tbody.innerHTML = '<tr><td colspan="8" class="loading">正在刷新行业列表...</td></tr>';
        setStatus('industry-list-status', '正在获取行业板块列表（东方财富优先，失败自动切换同花顺）...');
    } else if (!state.industries.length) {
        setStatus('industry-list-status', '正在获取行业板块列表（东方财富优先，失败自动切换同花顺）...');
    }

    try {
        const url = refresh ? '/api/industry/list?refresh=1' : '/api/industry/list';
        const payload = await requestJson(url);
        state.industries = payload.data || [];
        renderIndustries();

        const updatedAt = state.industries[0]?.updated_at || '';
        const sourceName = sourceLabel(payload.source);
        const sourceEl = $('industry-source-label');
        if (sourceEl) sourceEl.textContent = `数据源：${sourceName}`;

        const suffix = updatedAt ? ` · 更新于 ${updatedAt.replace('T', ' ')}` : '';
        const base = `共 ${state.industries.length} 个行业 · 数据源：${sourceName}${suffix}`;
        if (payload.stale) {
            setStatus('industry-list-status', `⚠️ ${payload.warning}；已展示本地缓存（${updatedAt}）`, 'warn');
        } else if (payload.warning) {
            setStatus('industry-list-status', `⚠️ ${payload.warning} · ${base}`, 'warn');
        } else {
            setStatus('industry-list-status', `${base}${refresh ? ' · 已刷新' : ''}`, 'ok');
        }
    } catch (error) {
        console.error('加载行业列表失败:', error);
        if (!state.industries.length && tbody) {
            tbody.innerHTML = `<tr><td colspan="8" class="loading">加载失败：${escapeHtml(
                error.message
            )}<br>请检查网络后点击「刷新行业列表」重试。</td></tr>`;
        }
        setStatus('industry-list-status', `加载失败：${error.message}`, 'error');
    } finally {
        state.loadingList = false;
    }
}

function renderIndustries() {
    const tbody = $('industry-list-tbody');
    if (!tbody) return;

    const keyword = state.keyword.toLowerCase();
    const rows = state.industries.filter((item) => {
        if (!keyword) return true;
        return (
            String(item.industry_code || '').toLowerCase().includes(keyword) ||
            String(item.industry_name || '').toLowerCase().includes(keyword)
        );
    });

    if (!rows.length) {
        const message = state.industries.length
            ? '没有匹配的行业，请调整搜索关键字'
            : '暂无行业数据，点击右上角「刷新行业列表」获取';
        tbody.innerHTML = `<tr><td colspan="8" class="loading">${message}</td></tr>`;
        return;
    }

    tbody.innerHTML = rows
        .map((item) => {
            const action = item.watched
                ? `<button class="btn btn-secondary btn-small" data-action="unwatch" data-code="${escapeHtml(
                      item.industry_code
                  )}" type="button">取消关注</button>`
                : `<button class="btn btn-primary btn-small" data-action="watch" data-code="${escapeHtml(
                      item.industry_code
                  )}" type="button">关注</button>`;
            const leaderPct =
                item.leader_change_pct === null || item.leader_change_pct === undefined
                    ? ''
                    : ` <span class="${pctClass(item.leader_change_pct)}">${fmtPct(item.leader_change_pct)}</span>`;
            const upDown = `${fmtCount(item.up_count)} / ${fmtCount(item.down_count)}`;
            return `
                <tr>
                    <td>${escapeHtml(item.industry_code)}</td>
                    <td><strong>${escapeHtml(item.industry_name)}</strong></td>
                    <td>${fmtNum(item.latest_price)}</td>
                    <td class="${pctClass(item.change_pct)}">${fmtPct(item.change_pct)}</td>
                    <td>${item.turnover_rate === null || item.turnover_rate === undefined ? '--' : fmtNum(item.turnover_rate) + '%'}</td>
                    <td>${upDown}</td>
                    <td>${escapeHtml(item.leader_stock || '-')}${leaderPct}</td>
                    <td class="col-ops">${action}</td>
                </tr>
            `;
        })
        .join('');
}

async function loadWatched() {
    const tbody = $('industry-watched-tbody');
    try {
        const payload = await requestJson('/api/industry/watched');
        state.watched = payload.data || [];
        renderWatched();
    } catch (error) {
        console.error('加载关注行业失败:', error);
        if (tbody) {
            tbody.innerHTML = `<tr><td colspan="8" class="loading">加载失败：${escapeHtml(error.message)}</td></tr>`;
        }
    }
}

function renderWatched() {
    const tbody = $('industry-watched-tbody');
    if (!tbody) return;

    if (!state.watched.length) {
        tbody.innerHTML =
            '<tr><td colspan="8" class="loading">暂无关注行业，请在上方行业列表中点击「关注」。</td></tr>';
        return;
    }

    tbody.innerHTML = state.watched
        .map((item) => {
            const code = escapeHtml(item.industry_code);
            return `
                <tr>
                    <td>${code}</td>
                    <td><strong>${escapeHtml(item.industry_name)}</strong></td>
                    <td>${escapeHtml(sourceLabel(item.source))}</td>
                    <td>${item.latest_date || '--'}</td>
                    <td>${fmtCount(item.kline_count)}</td>
                    <td>${fmtNum(item.latest_close)}</td>
                    <td class="${pctClass(item.latest_change_pct)}">${fmtPct(item.latest_change_pct)}</td>
                    <td class="col-ops">
                        <button class="btn btn-secondary btn-small" data-action="detail" data-code="${code}" type="button">查看日K</button>
                        <button class="btn btn-primary btn-small" data-action="fetch" data-code="${code}" type="button">拉取日K</button>
                        <button class="btn btn-secondary btn-small" data-action="unwatch" data-code="${code}" type="button">取消关注</button>
                    </td>
                </tr>
            `;
        })
        .join('');
}

async function watchIndustry(code) {
    if (state.busy) return;
    const item = findIndustry(code);
    setBusy(true);
    setStatus('industry-list-status', `正在关注 ${item.industry_name}...`);
    try {
        await requestJson('/api/industry/watch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ industry_code: code, industry_name: item.industry_name }),
        });
        await Promise.all([loadIndustryList(false), loadWatched(), loadSummary()]);
        setStatus('industry-list-status', `已关注 ${item.industry_name}（${code}）`, 'ok');
    } catch (error) {
        setStatus('industry-list-status', `关注失败：${error.message}`, 'error');
    } finally {
        setBusy(false);
    }
}

async function unwatchIndustry(code) {
    if (state.busy) return;
    const item = findIndustry(code);
    const hasData = Number(item.kline_count || 0) > 0;
    const tip = hasData
        ? `取消关注 ${item.industry_name}？已保存的 ${item.kline_count} 条日K会保留。`
        : `取消关注 ${item.industry_name}？`;
    if (!window.confirm(tip)) return;

    setBusy(true);
    setStatus('industry-list-status', `正在取消关注 ${item.industry_name}...`);
    try {
        await requestJson(`/api/industry/watch/${encodeURIComponent(code)}`, { method: 'DELETE' });
        if (state.selectedCode === code) {
            state.selectedCode = null;
            state.selectedName = '';
            const card = $('industry-detail-card');
            if (card) card.style.display = 'none';
        }
        await Promise.all([loadIndustryList(false), loadWatched(), loadSummary()]);
        setStatus('industry-list-status', `已取消关注 ${item.industry_name}`, 'ok');
    } catch (error) {
        setStatus('industry-list-status', `取消失败：${error.message}`, 'error');
    } finally {
        setBusy(false);
    }
}

async function fetchDaily(codes) {
    if (state.busy) return;
    if (!codes.length) {
        setStatus('industry-fetch-status', '请先添加关注行业', 'warn');
        return;
    }

    const startDate = $('industry-start-date')?.value || '';
    const endDate = $('industry-end-date')?.value || '';
    if (!startDate || !endDate) {
        setStatus('industry-fetch-status', '请先选择起始和结束日期', 'warn');
        return;
    }
    if (startDate > endDate) {
        setStatus('industry-fetch-status', '开始日期不能晚于结束日期', 'warn');
        return;
    }

    setBusy(true);
    setStatus(
        'industry-fetch-status',
        `正在从东方财富拉取 ${codes.length} 个行业的日K（${startDate} ~ ${endDate}），请稍候...`
    );

    try {
        const payload = await requestJson(
            '/api/industry/daily/fetch',
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ codes, start_date: startDate, end_date: endDate }),
            },
            180000
        );
        showFetchResult(payload.data || payload);
        await Promise.all([loadWatched(), loadSummary()]);
        if (state.selectedCode && codes.includes(state.selectedCode)) {
            await loadDaily(state.selectedCode, state.selectedName);
        }
    } catch (error) {
        const data = error.payload && error.payload.data;
        if (data && data.results) {
            showFetchResult(data);
        } else {
            setStatus('industry-fetch-status', `拉取失败：${error.message}`, 'error');
        }
    } finally {
        setBusy(false);
    }
}

function showFetchResult(data) {
    const results = data.results || [];
    const errors = data.errors || [];
    const saved = data.saved || 0;
    let message = `拉取完成：成功 ${results.length} 个行业，保存/更新 ${saved} 条日K`;

    if (results.length) {
        const latest = results
            .map((item) => item.latest_date)
            .filter(Boolean)
            .sort()
            .pop();
        if (latest) message += `，最新日期 ${latest}`;
        const sources = [...new Set(results.map((item) => sourceLabel(item.source)))];
        if (sources.length) message += `，数据源：${sources.join('/')}`;
    }
    if (errors.length) {
        message += `；失败 ${errors.length} 个：` + errors.map((item) => item.industry_code).join('、');
    }
    setStatus('industry-fetch-status', message, errors.length ? 'warn' : 'ok');
}

async function loadDaily(code, name) {
    state.selectedCode = code;
    state.selectedName = name || code;

    const card = $('industry-detail-card');
    const tbody = $('industry-daily-tbody');
    if (card) card.style.display = 'block';
    if ($('industry-detail-title')) {
        $('industry-detail-title').textContent = `📊 ${state.selectedName}（${code}）行业指数日K`;
    }
    if (tbody) {
        tbody.innerHTML = '<tr><td colspan="8" class="loading">加载中...</td></tr>';
    }
    setStatus('industry-detail-status', '正在加载已保存的日K...');

    try {
        const payload = await requestJson(`/api/industry/daily/${encodeURIComponent(code)}?limit=250`);
        renderDaily(payload.data || []);
        const rows = payload.data || [];
        if (rows.length) {
            const latest = rows[rows.length - 1];
            setStatus(
                'industry-detail-status',
                `最近 ${rows.length} 条，最新 ${latest.trade_date} · 数据源：${sourceLabel(latest.source)}`,
                'ok'
            );
        } else {
            setStatus('industry-detail-status', '暂无已保存的日K，点击「拉取日K」获取', 'warn');
        }
    } catch (error) {
        if (tbody) {
            tbody.innerHTML = `<tr><td colspan="8" class="loading">加载失败：${escapeHtml(error.message)}</td></tr>`;
        }
        setStatus('industry-detail-status', `加载失败：${error.message}`, 'error');
    }

    if (card && card.scrollIntoView) {
        card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

function renderDaily(rows) {
    const tbody = $('industry-daily-tbody');
    if (!tbody) return;
    if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="loading">暂无数据</td></tr>';
        return;
    }

    tbody.innerHTML = rows
        .slice()
        .reverse()
        .map(
            (row) => `
            <tr>
                <td>${escapeHtml(row.trade_date)}</td>
                <td>${fmtNum(row.open)}</td>
                <td>${fmtNum(row.high)}</td>
                <td>${fmtNum(row.low)}</td>
                <td><strong>${fmtNum(row.close)}</strong></td>
                <td class="${pctClass(row.change_pct)}">${fmtPct(row.change_pct)}</td>
                <td>${fmtAmountYi(row.amount)}</td>
                <td>${row.turnover_rate === null || row.turnover_rate === undefined ? '--' : fmtNum(row.turnover_rate) + '%'}</td>
            </tr>
        `
        )
        .join('');
}
