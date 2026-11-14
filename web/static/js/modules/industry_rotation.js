/**
 * 行业轮动回测模块
 *
 * 流程：关注行业轮动 → 选 2 个行业 → 每行业选 3 只 → 10 年回测 / 参数优化
 */

import { escapeHtml } from './utils.js';

const state = {
    config: null,
    taskId: null,
    timer: null,
    chart: null,
    bound: false,
    submitting: false,
    pollErrors: 0,
};

function $(id) {
    return document.getElementById(id);
}

function pct(value, digits = 2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '--';
    return `${(Number(value) * 100).toFixed(digits)}%`;
}

function pctSigned(value, digits = 2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '--';
    const num = Number(value);
    return `${num > 0 ? '+' : ''}${num.toFixed(digits)}%`;
}

function num(value, digits = 2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '--';
    return Number(value).toFixed(digits);
}

function pctClass(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '';
    const num = Number(value);
    if (num > 0) return 'industry-up';
    if (num < 0) return 'industry-down';
    return '';
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
        if (error.name === 'AbortError') throw new Error('请求超时，请稍后重试');
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
        const err = new Error(message);
        err.status = response.status;
        err.payload = payload;
        throw err;
    }
    if (!payload) throw new Error('接口返回为空');
    if (payload.success === false && !payload.data) {
        throw new Error(payload.error || '请求失败');
    }
    return payload;
}

export async function initIndustryRotationPage() {
    bindEvents();
    await loadConfig();
    if (state.taskId) {
        startPolling();
    }
}

function bindEvents() {
    if (state.bound) return;
    state.bound = true;
    $('rotation-run-btn')?.addEventListener('click', runBacktest);
}

async function loadConfig() {
    try {
        const payload = await requestJson('/api/industry/rotation/config');
        state.config = payload.data || {};
        renderConfig();
    } catch (error) {
        setStatus('rotation-hint', `加载轮动配置失败：${error.message}`, 'error');
    }
}

function renderConfig() {
    const config = state.config || {};
    const watched = config.watched || [];
    const container = $('rotation-watched');
    if (container) {
        if (!watched.length) {
            container.innerHTML = '<span class="rotation-chip missing">尚未关注行业，请先在「行业数据」页关注至少 2 个行业</span>';
        } else {
            container.innerHTML = watched
                .map((item) => {
                    const coverage = item.n ? ` · ${item.n} 日` : ' · 无行业指数';
                    const cls = item.n ? 'rotation-chip' : 'rotation-chip missing';
                    return `<span class="${cls}">${escapeHtml(item.industry_name)}（${escapeHtml(item.industry_code)}${coverage}）</span>`;
                })
                .join('');
        }
    }

    const info = $('rotation-universe-info');
    if (info) {
        info.textContent = `关注 ${config.watched_count || 0} 个行业 · 行业指数最新 ${config.latest_date || '-'}`;
    }

    const runBtn = $('rotation-run-btn');
    if (runBtn) runBtn.disabled = !config.can_run;

    const defaults = config.default_params || {};
    if ($('rotation-rebalance') && defaults.rebalance_days) {
        $('rotation-rebalance').value = String(defaults.rebalance_days);
    }
    if ($('rotation-top-stocks') && defaults.top_stocks) {
        $('rotation-top-stocks').value = String(defaults.top_stocks);
    }
    if ($('rotation-objective') && defaults.objective) {
        $('rotation-objective').value = defaults.objective;
    }

    if (!config.can_run) {
        setStatus('rotation-hint', '请先在「行业数据」页关注至少 2 个行业，再运行轮动回测。', 'warn');
    } else {
        setStatus(
            'rotation-hint',
            '提示：候选池为当前行业成分股，存在幸存者偏差；参数优化可能过拟合，请结合样本外与最大回撤判断。',
            'warn'
        );
    }
}

async function runBacktest() {
    if (state.submitting) return;
    if (!state.config || !state.config.can_run) {
        setStatus('rotation-hint', '请先关注至少 2 个行业', 'warn');
        return;
    }

    const candidateLimit = Number($('rotation-candidate-limit')?.value || 20);
    const optimize = Boolean($('rotation-optimize')?.checked);
    const objective = $('rotation-objective')?.value || 'max_return';
    const rebalanceDays = Number($('rotation-rebalance')?.value || 5);
    const topStocks = Number($('rotation-top-stocks')?.value || 2);

    state.submitting = true;
    const button = $('rotation-run-btn');
    if (button) {
        button.disabled = true;
        button.textContent = '回测中...';
    }
    setStatus('rotation-hint', '已提交回测任务，正在准备数据...', 'ok');
    $('rotation-result').style.display = 'none';
    $('rotation-progress-card').style.display = 'block';
    updateProgress(0, '准备', '任务已创建');
    if (state.chart) {
        state.chart.destroy();
        state.chart = null;
    }

    try {
        const payload = await requestJson(
            '/api/industry/rotation/start',
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    candidate_limit: candidateLimit,
                    optimize,
                    objective,
                    rebalance_days: rebalanceDays,
                    top_stocks: topStocks,
                }),
            },
            30000
        );
        state.taskId = payload.data.task_id;
        state.pollErrors = 0;
        startPolling();
    } catch (error) {
        state.submitting = false;
        if (button) {
            button.disabled = false;
            button.textContent = '开始 10 年回测';
        }
        setStatus('rotation-hint', `启动失败：${error.message}`, 'error');
    }
}

function updateProgress(progress, phase, message) {
    const pctValue = Math.max(0, Math.min(100, Number(progress) || 0));
    if ($('rotation-progress-bar')) $('rotation-progress-bar').style.width = `${pctValue}%`;
    if ($('rotation-progress-pct')) $('rotation-progress-pct').textContent = `${pctValue}%`;
    if ($('rotation-progress-phase')) $('rotation-progress-phase').textContent = phase || '处理中';
    if ($('rotation-progress-message')) $('rotation-progress-message').textContent = message || '';
}

function stopPolling() {
    if (state.timer) {
        clearTimeout(state.timer);
        state.timer = null;
    }
}

function startPolling() {
    stopPolling();
    if (!state.taskId) return;
    const poll = async () => {
        if (!state.taskId) return;
        try {
            const payload = await requestJson(`/api/industry/rotation/status/${state.taskId}`);
            const data = payload.data || {};
            state.pollErrors = 0;
            updateProgress(data.progress, data.phase, data.message);

            if (data.status === 'completed') {
                stopPolling();
                await loadResult();
                return;
            }
            if (data.status === 'failed') {
                stopPolling();
                finishRun();
                setStatus('rotation-hint', `回测失败：${data.error || data.message || '未知错误'}`, 'error');
                return;
            }
            state.timer = setTimeout(poll, 2000);
        } catch (error) {
            state.pollErrors += 1;
            if (state.pollErrors >= 5) {
                stopPolling();
                finishRun();
                setStatus('rotation-hint', `查询进度失败：${error.message}`, 'error');
                return;
            }
            state.timer = setTimeout(poll, 3000);
        }
    };
    state.timer = setTimeout(poll, 800);
}

function finishRun() {
    state.submitting = false;
    state.taskId = null;
    const button = $('rotation-run-btn');
    if (button) {
        button.disabled = false;
        button.textContent = '开始 10 年回测';
    }
}

async function loadResult() {
    try {
        const payload = await requestJson(`/api/industry/rotation/result/${state.taskId}`, {}, 60000);
        renderResult(payload.data || {});
        finishRun();
        setStatus('rotation-hint', '回测完成，可调整候选池或参数后重新运行。', 'ok');
    } catch (error) {
        finishRun();
        setStatus('rotation-hint', `获取回测结果失败：${error.message}`, 'error');
    }
}

function metricCard(icon, value, label, className = '') {
    return `
        <div class="stat-card">
            <div class="stat-icon">${icon}</div>
            <div class="stat-info">
                <h3 class="${className}">${value}</h3>
                <p>${label}</p>
            </div>
        </div>
    `;
}

function renderResult(result) {
    const metrics = result.metrics || {};
    const benchmark = result.benchmark_metrics || {};
    const oos = result.oos_metrics || {};
    const params = result.best_params || {};

    const metricsContainer = $('rotation-metrics');
    if (metricsContainer) {
        metricsContainer.innerHTML = [
            metricCard('💰', pct(metrics.total_return), '策略总收益', pctClass(metrics.total_return)),
            metricCard('📈', pct(metrics.cagr), '年化收益', pctClass(metrics.cagr)),
            metricCard('📉', pct(metrics.max_drawdown), '最大回撤', 'industry-down'),
            metricCard('⚖️', num(metrics.sharpe), '夏普比率'),
            metricCard('🎯', pct(metrics.win_rate), '调仓胜率'),
            metricCard('🏁', num(metrics.final_nav, 2), '期末净值'),
        ].join('');
    }

    const objective = (result.optimization && result.optimization.objective) || 'maximin_cagr';
    const isMaxReturn = objective === 'max_return';
    if ($('rotation-best-params')) {
        $('rotation-best-params').textContent =
            `${isMaxReturn ? '收益最大化参数' : '稳健最优参数'}：行业动量 ${params.industry_lookback} 日 · ` +
            `个股动量 ${params.stock_lookback} 日 · 调仓 ${params.rebalance_days} 日 · ` +
            `每行业 ${params.top_stocks || 2} 只`;
    }
    if ($('rotation-combos-sort-label')) {
        $('rotation-combos-sort-label').textContent = isMaxReturn
            ? '按全区间总收益排序'
            : '按样本内/外较小年化（稳健）排序';
    }
    if ($('rotation-latest-date')) {
        const latest = result.latest_picks && result.latest_picks[0];
        $('rotation-latest-date').textContent = latest ? `入选日 ${latest.entry_date}` : '';
    }

    renderChart(result);
    renderLatestPicks(result.latest_picks || []);
    renderCombos(result.top_combos || []);
    renderTrades(result.trades || []);
    renderWarnings(result.warnings || []);

    if ($('rotation-chart-note')) {
        $('rotation-chart-note').textContent =
            `基准（关注行业等权）总收益 ${pct(benchmark.total_return)} · 年化 ${pct(benchmark.cagr)} · 最大回撤 ${pct(benchmark.max_drawdown)}；` +
            `样本外（最后 30%）策略总收益 ${pct(oos.total_return)} · 年化 ${pct(oos.cagr)} · 最大回撤 ${pct(oos.max_drawdown)}` +
            (result.max_return_combo
                ? `；全区间最高收益参数（行业 ${result.max_return_combo.params.industry_lookback} 日 / 个股 ${result.max_return_combo.params.stock_lookback} 日 / 调仓 ${result.max_return_combo.params.rebalance_days} 日）总收益 ${pct(result.max_return_combo.metrics.total_return)}，样本外 ${pct(result.max_return_combo.oos_metrics && result.max_return_combo.oos_metrics.total_return)}，${isMaxReturn ? '已采用' : '未采用'}`
                : '');
    }

    $('rotation-result').style.display = 'block';
    $('rotation-progress-card').style.display = 'none';
    $('rotation-result').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderChart(result) {
    const container = $('rotation-chart');
    if (!container) return;

    const equity = result.equity_curve || [];
    const benchmark = result.benchmark_curve || [];
    if (equity.length < 2) {
        container.innerHTML = '<p class="text-muted">暂无净值数据</p>';
        return;
    }

    const sample = (items, maxPoints = 700) => {
        if (items.length <= maxPoints) return items;
        const step = Math.ceil(items.length / maxPoints);
        return items.filter((item, index) => index % step === 0 || index === items.length - 1);
    };

    const points = sample(equity);
    const benchmarkMap = new Map(benchmark.map((item) => [item.date, Number(item.nav)]));
    const navValues = points.map((item) => Number(item.nav));
    const benchmarkValues = points.map((item) =>
        benchmarkMap.has(item.date) ? benchmarkMap.get(item.date) : null
    );

    const allValues = navValues.concat(benchmarkValues.filter((value) => value !== null));
    let minValue = Math.min(...allValues);
    let maxValue = Math.max(...allValues);
    if (!Number.isFinite(minValue) || !Number.isFinite(maxValue)) {
        container.innerHTML = '<p class="text-muted">净值数据无效</p>';
        return;
    }
    const padding = Math.max((maxValue - minValue) * 0.06, 0.05);
    minValue = Math.max(0, minValue - padding);
    maxValue += padding;

    const width = 1200;
    const height = 360;
    const margin = { left: 64, right: 24, top: 28, bottom: 38 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;

    const xAt = (index) =>
        margin.left + (points.length <= 1 ? 0 : (index / (points.length - 1)) * plotWidth);
    const yAt = (value) =>
        margin.top + ((maxValue - value) / (maxValue - minValue || 1)) * plotHeight;

    const parts = [];
    parts.push(
        `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="行业轮动净值曲线">`
    );
    parts.push(
        `<rect x="0" y="0" width="${width}" height="${height}" fill="#ffffff"></rect>`
    );

    // y 轴网格与刻度
    const ticks = 5;
    for (let i = 0; i < ticks; i += 1) {
        const value = minValue + ((maxValue - minValue) * i) / (ticks - 1);
        const y = yAt(value);
        parts.push(
            `<line x1="${margin.left}" y1="${y.toFixed(1)}" x2="${(width - margin.right).toFixed(1)}" y2="${y.toFixed(1)}" stroke="#e5e7eb" stroke-width="1"></line>`
        );
        parts.push(
            `<text x="${(margin.left - 10).toFixed(1)}" y="${(y + 4).toFixed(1)}" text-anchor="end" font-size="12" fill="#64748b">${value.toFixed(2)}</text>`
        );
    }
    parts.push(
        `<text x="18" y="${(margin.top + plotHeight / 2).toFixed(1)}" text-anchor="middle" font-size="12" fill="#64748b" transform="rotate(-90 18 ${(margin.top + plotHeight / 2).toFixed(1)})">净值</text>`
    );

    // x 轴日期
    const labelCount = 6;
    for (let i = 0; i < labelCount; i += 1) {
        const index = Math.min(
            points.length - 1,
            Math.round((i / (labelCount - 1)) * (points.length - 1))
        );
        const x = xAt(index);
        const date = String(points[index].date || '');
        parts.push(
            `<line x1="${x.toFixed(1)}" y1="${margin.top + plotHeight}" x2="${x.toFixed(1)}" y2="${(margin.top + plotHeight + 5).toFixed(1)}" stroke="#cbd5e1" stroke-width="1"></line>`
        );
        parts.push(
            `<text x="${x.toFixed(1)}" y="${(height - 12).toFixed(1)}" text-anchor="middle" font-size="11" fill="#64748b">${date.slice(0, 7)}</text>`
        );
    }

    // 策略面积
    const strategyPath = points
        .map((item, index) => `${index === 0 ? 'M' : 'L'}${xAt(index).toFixed(1)},${yAt(Number(item.nav)).toFixed(1)}`)
        .join(' ');
    parts.push(
        `<path d="${strategyPath} L${xAt(points.length - 1).toFixed(1)},${(margin.top + plotHeight).toFixed(1)} L${xAt(0).toFixed(1)},${(margin.top + plotHeight).toFixed(1)} Z" fill="rgba(239, 68, 68, 0.08)" stroke="none"></path>`
    );

    // 基准曲线
    const benchmarkPath = points
        .map((item, index) => {
            const value = benchmarkMap.get(item.date);
            if (value === undefined || value === null) return null;
            return { x: xAt(index), y: yAt(value), first: false };
        })
        .filter(Boolean);
    if (benchmarkPath.length > 1) {
        const d = benchmarkPath
            .map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x.toFixed(1)},${point.y.toFixed(1)}`)
            .join(' ');
        parts.push(
            `<path d="${d}" fill="none" stroke="#64748b" stroke-width="1.5" vector-effect="non-scaling-stroke"></path>`
        );
    }

    // 策略曲线
    parts.push(
        `<path d="${strategyPath}" fill="none" stroke="#ef4444" stroke-width="2" vector-effect="non-scaling-stroke"></path>`
    );

    // 图例
    parts.push(
        `<line x1="${margin.left}" y1="14" x2="${margin.left + 22}" y2="14" stroke="#ef4444" stroke-width="2"></line>`
    );
    parts.push(
        `<text x="${margin.left + 28}" y="18" font-size="12" fill="#334155">策略净值</text>`
    );
    parts.push(
        `<line x1="${margin.left + 110}" y1="14" x2="${margin.left + 132}" y2="14" stroke="#64748b" stroke-width="1.5"></line>`
    );
    parts.push(
        `<text x="${margin.left + 138}" y="18" font-size="12" fill="#334155">关注行业等权</text>`
    );

    parts.push('</svg>');
    container.innerHTML = parts.join('');
}

function renderLatestPicks(picks) {
    const tbody = $('rotation-latest-picks');
    if (!tbody) return;
    if (!picks.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="loading">暂无持仓</td></tr>';
        return;
    }
    tbody.innerHTML = picks
        .map(
            (item) => `
            <tr>
                <td>${escapeHtml(item.industry_name || item.industry_code)}</td>
                <td>${escapeHtml(item.stock_code)}</td>
                <td><strong>${escapeHtml(item.stock_name)}</strong></td>
                <td>${escapeHtml(item.entry_date || '--')}</td>
                <td>${num(item.entry_price)}</td>
                <td>${num(item.current_price)}</td>
                <td class="${pctClass(item.return_pct)}">${pctSigned(item.return_pct)}</td>
                <td>${num(item.score)}</td>
            </tr>
        `
        )
        .join('');
}

function renderCombos(combos) {
    const tbody = $('rotation-combos-body');
    if (!tbody) return;
    if (!combos.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="loading">暂无数据</td></tr>';
        return;
    }
    tbody.innerHTML = combos
        .map((item, index) => {
            const params = item.params || {};
            const metrics = item.metrics || {};
            const best = index === 0 ? 'style="background:#fff7ed;"' : '';
            return `
                <tr ${best}>
                    <td>${params.industry_lookback} 日</td>
                    <td>${params.stock_lookback} 日</td>
                    <td>${params.rebalance_days} 日</td>
                    <td class="${pctClass(metrics.total_return)}">${pct(metrics.total_return)}</td>
                    <td class="${pctClass(metrics.cagr)}">${pct(metrics.cagr)}</td>
                    <td class="industry-down">${pct(metrics.max_drawdown)}</td>
                    <td>${num(metrics.sharpe)}</td>
                    <td>${pct(metrics.win_rate)}</td>
                    <td class="${pctClass(item.oos_metrics && item.oos_metrics.cagr)}">${pct(item.oos_metrics && item.oos_metrics.cagr)}</td>
                </tr>
            `;
        })
        .join('');
}

function renderTrades(trades) {
    const tbody = $('rotation-trades-body');
    if (!tbody) return;
    if (!trades.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="loading">暂无交易</td></tr>';
        return;
    }
    tbody.innerHTML = trades
        .slice()
        .reverse()
        .map(
            (item) => `
            <tr>
                <td>${escapeHtml(item.entry_date)}</td>
                <td>${escapeHtml(item.industry_name || item.industry_code)}</td>
                <td>${escapeHtml(item.stock_code)}</td>
                <td>${escapeHtml(item.stock_name)}</td>
                <td>${num(item.buy_price)}</td>
                <td>${escapeHtml(item.exit_date)}</td>
                <td>${num(item.sell_price)}</td>
                <td class="${pctClass(item.return_pct)}">${pctSigned(item.return_pct)}</td>
            </tr>
        `
        )
        .join('');
}

function renderWarnings(warnings) {
    const list = $('rotation-warnings');
    if (!list) return;
    list.innerHTML = warnings.map((text) => `<li>${escapeHtml(text)}</li>`).join('');
}
