/**
 * 股票相关功能模块
 */

/**
 * 加载统计信息
 */
export async function loadStats() {
    try {
        const response = await fetch('/api/stats');
        const result = await response.json();
        
        if (result.success) {
            document.getElementById('stat-stocks').textContent = result.data.total_stocks;
            document.getElementById('stat-date').textContent = result.data.latest_date;
            document.getElementById('stat-strategies').textContent = result.data.strategies;
        }
    } catch (error) {
        console.error('加载统计信息失败:', error);
    }
}

/**
 * 加载我的金股数据
 */
export async function loadMyGoldenStocks() {
    const container = document.getElementById('my-golden-stocks-content');
    try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000); // 5秒超时
        
        const response = await fetch('/api/dashboard/my-golden-stocks', { signal: controller.signal });
        clearTimeout(timeoutId);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        
        const result = await response.json();
        
        // 检查result是否为空或没有success字段
        if (!result || (result.success === false)) {
            container.innerHTML = '<p class="text-muted">暂无金股数据</p>';
            return;
        }
        
        // 如果success为true或result中有stocks数据
        if (result.stocks && result.stocks.length > 0) {
            let html = `
                <div class="table-responsive">
                    <table class="table table-striped">
                        <thead>
                            <tr>
                                <th>排名</th>
                                <th>股票代码</th>
                                <th>股票名称</th>
                                <th>评分</th>
                                <th>行业</th>
                                <th>板块</th>
                            </tr>
                        </thead>
                        <tbody>
            `;
            
            result.stocks.forEach((stock, index) => {
                html += `
                    <tr>
                        <td>${index + 1}</td>
                        <td><a href="javascript:void(0)" onclick="viewStockDetail('${stock.stock_code}')" class="stock-link">${stock.stock_code}</a></td>
                        <td>${stock.stock_name}</td>
                        <td><a href="javascript:void(0)" onclick="showScoreDetail('${stock.stock_code}', '${result.date}')" class="score-link">${(stock.total_score || 0).toFixed(2)}</a></td>
                        <td>${stock.industry || '-'}</td>
                        <td>${stock.area || '-'}</td>
                    </tr>
                `;
            });
            
            html += `
                        </tbody>
                    </table>
                </div>
                <p class="text-muted" style="margin-top: 10px; font-size: 12px;">数据日期: ${result.date}</p>
            `;
            
            container.innerHTML = html;
        } else {
            container.innerHTML = '<p class="text-muted">暂无金股数据</p>';
        }
    } catch (error) {
        console.error('加载我的金股失败:', error);
        if (error.name === 'AbortError') {
            container.innerHTML = '<p class="text-muted">暂无金股数据</p>';
        } else {
            container.innerHTML = '<p class="text-muted">暂无金股数据</p>';
        }
    }
}

/**
 * 加载最热行业数据
 */
export async function loadHotIndustries() {
    const container = document.getElementById('hot-industries-content');
    try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000); // 5秒超时
        
        const response = await fetch('/api/dashboard/hot-industries', { signal: controller.signal });
        clearTimeout(timeoutId);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        
        const result = await response.json();
        
        // 检查result是否为空或没有success字段
        if (!result || (result.success === false)) {
            container.innerHTML = '<p class="text-muted">暂无行业数据</p>';
            return;
        }
        
        // 如果success为true或result中有industries数据
        if (result.industries && result.industries.length > 0) {
            let html = `
                <div class="table-responsive">
                    <table class="table table-striped">
                        <thead>
                            <tr>
                                <th>排名</th>
                                <th>行业</th>
                                <th>股票数量</th>
                                <th>占比</th>
                            </tr>
                        </thead>
                        <tbody>
            `;
            
            // 只显示前5个行业
            const top5Industries = result.industries.slice(0, 5);
            top5Industries.forEach((industry, index) => {
                html += `
                    <tr>
                        <td>${index + 1}</td>
                        <td>${industry.industry}</td>
                        <td><a href="javascript:void(0)" onclick="showIndustryStocks('${industry.industry}', ${industry.count})" class="stock-link">${industry.count}</a></td>
                        <td>${industry.percentage}%</td>
                    </tr>
                `;
            });
            
            html += `
                        </tbody>
                    </table>
                </div>
                <p class="text-muted" style="margin-top: 10px; font-size: 12px;">数据日期: ${result.date}</p>
            `;
            
            container.innerHTML = html;
        } else {
            container.innerHTML = '<p class="text-muted">暂无行业数据</p>';
        }
    } catch (error) {
        console.error('加载最热行业失败:', error);
        if (error.name === 'AbortError') {
            container.innerHTML = '<p class="text-muted">暂无行业数据</p>';
        } else {
            container.innerHTML = '<p class="text-muted">暂无行业数据</p>';
        }
    }
}

/**
 * 加载最热板块数据
 */
export async function loadHotAreas() {
    const container = document.getElementById('hot-areas-content');
    try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000); // 5秒超时
        
        const response = await fetch('/api/dashboard/hot-areas', { signal: controller.signal });
        clearTimeout(timeoutId);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        
        const result = await response.json();
        
        // 检查result是否为空或没有success字段
        if (!result || (result.success === false)) {
            container.innerHTML = '<p class="text-muted">暂无板块数据</p>';
            return;
        }
        
        // 如果success为true或result中有areas数据
        if (result.areas && result.areas.length > 0) {
            let html = `
                <div class="table-responsive">
                    <table class="table table-striped">
                        <thead>
                            <tr>
                                <th>排名</th>
                                <th>板块</th>
                                <th>股票数量</th>
                                <th>占比</th>
                            </tr>
                        </thead>
                        <tbody>
            `;
            
            // 只显示前5个板块
            const top5Areas = result.areas.slice(0, 5);
            top5Areas.forEach((area, index) => {
                html += `
                    <tr>
                        <td>${index + 1}</td>
                        <td>${area.area}</td>
                        <td><a href="javascript:void(0)" onclick="showAreaStocks('${area.area}', ${area.count})" class="stock-link">${area.count}</a></td>
                        <td>${area.percentage}%</td>
                    </tr>
                `;
            });
            
            html += `
                        </tbody>
                    </table>
                </div>
                <p class="text-muted" style="margin-top: 10px; font-size: 12px;">数据日期: ${result.date}</p>
            `;
            
            container.innerHTML = html;
        } else {
            container.innerHTML = '<p class="text-muted">暂无板块数据</p>';
        }
    } catch (error) {
        console.error('加载最热板块失败:', error);
        if (error.name === 'AbortError') {
            container.innerHTML = '<p class="text-muted">暂无板块数据</p>';
        } else {
            container.innerHTML = '<p class="text-muted">暂无板块数据</p>';
        }
    }
}

/**
 * 加载股票列表 - 支持分页获取所有股票
 */
export async function loadStocks() {
    const tbody = document.getElementById('stocks-tbody');
    tbody.innerHTML = '<tr><td colspan="8" class="loading">正在加载股票列表...</td></tr>';

    try {
        let allStocks = [];
        let page = 1;
        let totalPages = 1;

        // 分页获取所有股票
        do {
            const response = await fetch(`/api/stocks?page=${page}&per_page=500`);
            const result = await response.json();

            if (result.success) {
                allStocks = allStocks.concat(result.data);
                totalPages = result.total_pages;
                tbody.innerHTML = `<tr><td colspan="8" class="loading">已加载 ${allStocks.length} / ${result.total} 只股票...</td></tr>`;
                page++;
            } else {
                break;
            }
        } while (page <= totalPages);

        await loadFavoriteCodes();
        renderStocks(allStocks);
        loadFavoriteGroupOptions();
        resumeKlineSyncIfRunning();
    } catch (error) {
        tbody.innerHTML = `<tr><td colspan="8" class="loading">加载失败: ${error.message}</td></tr>`;
    }
}

/**
 * 已在自选股中的代码集合，用于标记行内按钮状态
 */
const favoriteCodes = new Set();

/**
 * 加载已自选股票代码集合
 */
async function loadFavoriteCodes() {
    try {
        const response = await fetch('/api/stock/favorites');
        const result = await response.json();
        favoriteCodes.clear();
        if (result.success) {
            (result.data || []).forEach(item => favoriteCodes.add(item.stock_code));
        }
    } catch (error) {
        console.error('加载自选股状态失败:', error);
    }
}

/**
 * 渲染股票列表
 * @param {Array} stocks - 股票列表数据
 */
export function renderStocks(stocks) {
    const tbody = document.getElementById('stocks-tbody');

    if (stocks.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="loading">暂无数据</td></tr>';
        return;
    }

    tbody.innerHTML = stocks.map(stock => {
        const added = favoriteCodes.has(stock.code);
        return `
        <tr data-code="${stock.code}">
            <td class="col-check"><input type="checkbox" class="stock-select-checkbox" value="${stock.code}" data-name="${escapeAttr(stock.name)}"></td>
            <td><strong>${stock.code}</strong></td>
            <td>${stock.name}</td>
            <td>¥${stock.latest_price}</td>
            <td>${stock.latest_date}</td>
            <td>${stock.market_cap}</td>
            <td>${stock.data_count}</td>
            <td class="col-ops">
                <button class="btn btn-secondary" onclick="viewStockDetail('${stock.code}')">
                    查看
                </button>
                <button class="btn fav-add-btn ${added ? 'btn-added' : 'btn-primary'}"
                        data-code="${stock.code}" data-name="${escapeAttr(stock.name)}"
                        ${added ? 'disabled' : ''}>
                    ${added ? '已自选' : '加自选'}
                </button>
            </td>
        </tr>
    `;}).join('');

    setupStockSelection();
    setupFavoriteButtons();

    // 搜索功能
    document.getElementById('stock-search').addEventListener('input', (e) => {
        const keyword = e.target.value.toLowerCase();
        const rows = tbody.querySelectorAll('tr');
        rows.forEach(row => {
            const text = row.textContent.toLowerCase();
            row.style.display = text.includes(keyword) ? '' : 'none';
        });
        updateSelectedCount();
    });
}

/**
 * 绑定行内"加自选"按钮（委托，避免 5000+ 行逐个绑定）
 */
function setupFavoriteButtons() {
    const tbody = document.getElementById('stocks-tbody');
    if (tbody.dataset.favBound === '1') return;
    tbody.dataset.favBound = '1';
    tbody.addEventListener('click', (e) => {
        const btn = e.target.closest('.fav-add-btn');
        if (btn && !btn.disabled) {
            addStockToFavorite(btn);
        }
    });
}

/**
 * 将单只股票加入自选股
 * 目标分组取工具栏下拉框当前选项；未选择分组时加入"未分组收藏"
 * @param {HTMLElement} btn - 行内按钮
 */
async function addStockToFavorite(btn) {
    const code = btn.dataset.code;
    const name = btn.dataset.name || '';
    const select = document.getElementById('favorite-group-select');
    const groupId = select && !select.disabled ? select.value : '';

    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '加入中...';
    try {
        let response;
        if (groupId) {
            response = await fetch(`/api/stock/group/${groupId}/stocks`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ stocks: [{ code, name }] })
            });
        } else {
            response = await fetch('/api/stock/favorite', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ stock_code: code })
            });
        }
        const result = await response.json();
        if (result.success) {
            favoriteCodes.add(code);
            btn.textContent = '已自选';
            btn.classList.remove('btn-primary');
            btn.classList.add('btn-added');
            loadFavoriteGroupOptions();
        } else {
            alert('加入自选股失败: ' + (result.error || ''));
            btn.disabled = false;
            btn.textContent = originalText;
        }
    } catch (error) {
        alert('加入自选股失败: ' + error.message);
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

/**
 * HTML 属性转义
 * @param {string} value
 * @returns {string}
 */
function escapeAttr(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

/**
 * 绑定股票多选逻辑
 */
function setupStockSelection() {
    const tbody = document.getElementById('stocks-tbody');
    const selectAll = document.getElementById('stock-select-all');

    tbody.querySelectorAll('.stock-select-checkbox').forEach(cb => {
        cb.addEventListener('change', updateSelectedCount);
    });

    // 全选可见（复用于表头与工具栏）
    const toggleVisible = (checked) => {
        const rows = tbody.querySelectorAll('tr');
        rows.forEach(row => {
            if (row.style.display === 'none') return;
            const cb = row.querySelector('.stock-select-checkbox');
            if (cb) cb.checked = checked;
        });
        updateSelectedCount();
    };

    if (selectAll) {
        selectAll.onchange = () => {
            toggleVisible(selectAll.checked);
            const theadCheck = document.getElementById('stocks-thead-check');
            if (theadCheck) theadCheck.checked = selectAll.checked;
        };
    }
    const theadCheck = document.getElementById('stocks-thead-check');
    if (theadCheck) {
        theadCheck.onchange = () => {
            toggleVisible(theadCheck.checked);
            if (selectAll) selectAll.checked = theadCheck.checked;
        };
    }

    updateSelectedCount();
}

/**
 * 更新已选股票数量
 */
function updateSelectedCount() {
    const count = document.querySelectorAll('#stocks-tbody .stock-select-checkbox:checked').length;
    const el = document.getElementById('stock-selected-count');
    if (el) el.textContent = count;
}

/**
 * 获取当前选中的股票列表
 * @returns {Array<{code: string, name: string}>}
 */
function getSelectedStocks() {
    return Array.from(document.querySelectorAll('#stocks-tbody .stock-select-checkbox:checked'))
        .map(cb => ({ code: cb.value, name: cb.dataset.name || '' }));
}

/**
 * 加载分组下拉选项
 */
export async function loadFavoriteGroupOptions() {
    const select = document.getElementById('favorite-group-select');
    if (!select) return;
    try {
        const previous = select.value;
        const response = await fetch('/api/stock/groups');
        const result = await response.json();
        if (!result.success) return;
        const groups = result.groups || [];
        if (groups.length === 0) {
            select.innerHTML = '<option value="">（暂无分组，请先新建）</option>';
            select.disabled = true;
            return;
        }
        select.disabled = false;
        select.innerHTML = '<option value="">未分组（仅收藏）</option>'
            + groups.map(g => `<option value="${g.id}">${escapeAttr(g.name)} (${g.member_count})</option>`).join('');
        if (previous && groups.some(g => String(g.id) === previous)) {
            select.value = previous;
        }
    } catch (error) {
        console.error('加载分组选项失败:', error);
    }
}

/**
 * 将选中的股票加入所选分组
 */
window.addSelectedToGroup = async function() {
    const select = document.getElementById('favorite-group-select');
    const groupId = select ? select.value : '';
    if (!groupId) {
        alert('请先选择一个分组（或点击"新建分组"）');
        return;
    }
    const stocks = getSelectedStocks();
    if (stocks.length === 0) {
        alert('请先勾选要加入的股票');
        return;
    }
    const btn = document.getElementById('add-to-group-btn');
    if (btn) btn.disabled = true;
    try {
        const response = await fetch(`/api/stock/group/${groupId}/stocks`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ stocks })
        });
        const result = await response.json();
        if (result.success) {
            alert(`已加入分组，新增 ${result.added} 只股票` + (result.skipped ? `，${result.skipped} 只代码无效已跳过` : ''));
            const boxes = document.querySelectorAll('#stocks-tbody .stock-select-checkbox');
            boxes.forEach(cb => {
                cb.checked = false;
                if (favoriteCodes.has(cb.value)) return;
                // 同步行内按钮为"已自选"
                const row = cb.closest('tr');
                const btn = row ? row.querySelector('.fav-add-btn') : null;
                if (btn && !btn.disabled) {
                    favoriteCodes.add(cb.value);
                    btn.textContent = '已自选';
                    btn.classList.remove('btn-primary');
                    btn.classList.add('btn-added');
                    btn.disabled = true;
                }
            });
            const selectAll = document.getElementById('stock-select-all');
            if (selectAll) selectAll.checked = false;
            const theadCheck = document.getElementById('stocks-thead-check');
            if (theadCheck) theadCheck.checked = false;
            updateSelectedCount();
            loadFavoriteGroupOptions();
        } else {
            alert('加入分组失败: ' + (result.error || ''));
        }
    } catch (error) {
        alert('加入分组失败: ' + error.message);
    } finally {
        if (btn) btn.disabled = false;
    }
};

/**
 * 在全量股票页快速新建分组
 */
window.promptCreateGroupFromStocks = async function() {
    const name = prompt('请输入新分组名称：');
    if (!name || !name.trim()) return;
    try {
        const response = await fetch('/api/stock/group', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name.trim() })
        });
        const result = await response.json();
        if (result.success) {
            await loadFavoriteGroupOptions();
            const select = document.getElementById('favorite-group-select');
            if (select) select.value = String(result.group_id);
        } else {
            alert('新建分组失败: ' + (result.error || ''));
        }
    } catch (error) {
        alert('新建分组失败: ' + error.message);
    }
};


/**
 * 当前查看的股票代码（用于收藏功能）
 */
let currentStockCode = '';

/**
 * 行情增量同步：轮询定时器
 */
let klineSyncTimer = null;

/**
 * 启动行情增量同步
 */
window.startKlineSync = async function() {
    if (!confirm('将按每只股票自身的最新交易日补齐行情（无历史的股票回补约3年），'
        + '全库同步可能需要数分钟，确定开始？')) {
        return;
    }

    const btn = document.getElementById('kline-sync-btn');
    btn.disabled = true;
    try {
        const response = await fetch('/api/data/kline/sync', { method: 'POST' });
        const result = await response.json();
        if (!result.success) {
            alert('启动增量同步失败: ' + (result.message || ''));
            btn.disabled = false;
            return;
        }
        document.getElementById('kline-sync-panel').style.display = 'block';
        startKlineSyncPolling();
    } catch (error) {
        alert('启动增量同步失败: ' + error.message);
        btn.disabled = false;
    }
};

/**
 * 取消行情增量同步
 */
window.cancelKlineSync = async function() {
    try {
        const response = await fetch('/api/data/kline/sync/cancel', { method: 'POST' });
        const result = await response.json();
        if (!result.success) alert('取消失败: ' + (result.message || ''));
    } catch (error) {
        alert('取消失败: ' + error.message);
    }
};

/**
 * 开始轮询同步进度
 */
function startKlineSyncPolling() {
    if (klineSyncTimer) clearInterval(klineSyncTimer);
    fetchKlineSyncStatus();
    klineSyncTimer = setInterval(fetchKlineSyncStatus, 2000);
}

/**
 * 页面进入时若有同步任务在跑则恢复进度展示
 */
async function resumeKlineSyncIfRunning() {
    try {
        const response = await fetch('/api/data/kline/sync/status');
        const result = await response.json();
        if (result.success && result.data.running) startKlineSyncPolling();
    } catch (error) {
        console.error('查询同步状态失败:', error);
    }
}

/**
 * 拉取并渲染同步进度
 */
async function fetchKlineSyncStatus() {
    try {
        const response = await fetch('/api/data/kline/sync/status');
        const result = await response.json();
        if (!result.success) return;
        renderKlineSyncStatus(result.data);

        if (!result.data.running) {
            clearInterval(klineSyncTimer);
            klineSyncTimer = null;
            document.getElementById('kline-sync-btn').disabled = false;
            document.getElementById('kline-sync-cancel').style.display = 'none';
            if (result.data.total > 0) loadStocks();
        }
    } catch (error) {
        console.error('查询同步状态失败:', error);
    }
}

/**
 * 渲染同步进度面板
 * @param {Object} data - 同步状态
 */
function renderKlineSyncStatus(data) {
    const total = data.total || 0;
    const percent = total > 0 ? Math.floor((data.processed || 0) * 100 / total) : 0;

    document.getElementById('kline-sync-panel').style.display = 'block';
    document.getElementById('kline-sync-bar').style.width = percent + '%';
    document.getElementById('kline-sync-pct').textContent = percent + '%';
    document.getElementById('kline-sync-summary').textContent =
        `目标交易日 ${data.target_date || '-'} | 已处理 ${data.processed || 0}/${total} | ` +
        `新增 ${data.added || 0} 条 | 更新 ${data.updated || 0} 条 | ` +
        `除权重建 ${data.rebuilt || 0} 只 | 失败 ${data.failed || 0} 只`;
    document.getElementById('kline-sync-cancel').style.display = data.running ? '' : 'none';

    const logs = document.getElementById('kline-sync-logs');
    logs.innerHTML = (data.logs || []).slice(-6).map(line => `<li>${escapeAttr(line)}</li>`).join('');
}

/**
 * 查看股票详情
 * @param {string} code - 股票代码
 */
export async function viewStockDetail(code) {
    try {
        const response = await fetch(`/api/stock/${code}`);
        const result = await response.json();
        
        if (result.success) {
            showStockModal(code, result.data);
        } else {
            alert('加载股票详情失败: ' + result.error);
        }
    } catch (error) {
        alert('加载股票详情失败: ' + error.message);
    }
}

/**
 * 显示股票详情弹窗
 * @param {string} code - 股票代码
 * @param {Object} data - 股票数据
 */
export function showStockModal(code, data) {
    currentStockCode = code;
    const modal = document.getElementById('stock-modal');
    document.getElementById('stock-detail-modal-title').textContent = `股票详情: ${code}`;
    
    // 检查收藏状态并更新按钮
    updateFavoriteButton(code);
    
    // 显示K线图表容器
    const chartContainer = document.getElementById('stock-chart-container');
    chartContainer.style.display = 'block';
    
    // 清空股票信息区域，只显示K线图表
    document.getElementById('stock-info').innerHTML = '';
    
    // 先显示模态框，让容器获得正确的尺寸
    modal.classList.add('active');
    
    // 使用requestAnimationFrame确保DOM已更新，容器有正确的宽度
    requestAnimationFrame(() => {
        // 初始化K线图表
        // 注意：使用stock-chart-container而不是stock-chart（canvas元素）
        initKlineChart('stock-chart-container', data);
    });
}

/**
 * 更新收藏按钮状态
 * @param {string} code - 股票代码
 */
export async function updateFavoriteButton(code) {
    const btn = document.getElementById('favorite-btn');
    if (!btn) return;
    try {
        const response = await fetch(`/api/stock/favorite/${code}`);
        const result = await response.json();
        if (result.success && result.favorited) {
            btn.textContent = '⭐';
            btn.classList.add('active');
            btn.title = '取消收藏';
        } else {
            btn.textContent = '☆';
            btn.classList.remove('active');
            btn.title = '收藏';
        }
    } catch (error) {
        console.error('检查收藏状态失败:', error);
        btn.textContent = '☆';
        btn.classList.remove('active');
    }
}

/**
 * 切换收藏状态（供 onclick 调用的全局函数）
 */
window.toggleFavorite = async function() {
    const code = currentStockCode;
    if (!code) return;
    const btn = document.getElementById('favorite-btn');
    const isActive = btn.classList.contains('active');
    
    try {
        if (isActive) {
            // 取消收藏
            const response = await fetch(`/api/stock/favorite/${code}`, { method: 'DELETE' });
            const result = await response.json();
            if (result.success) {
                btn.textContent = '☆';
                btn.classList.remove('active');
                btn.title = '收藏';
            } else {
                alert('取消收藏失败: ' + (result.error || ''));
            }
        } else {
            // 添加收藏
            const response = await fetch('/api/stock/favorite', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ stock_code: code }),
            });
            const result = await response.json();
            if (result.success) {
                btn.textContent = '⭐';
                btn.classList.add('active');
                btn.title = '取消收藏';
            } else {
                alert('收藏失败: ' + (result.error || ''));
            }
        }
    } catch (error) {
        alert('操作失败: ' + error.message);
    }
};

/**
 * 关闭弹窗
 */
export function closeModal() {
    document.getElementById('stock-modal').classList.remove('active');
}

/**
 * 加载策略列表到历史记录下拉框
 * 与策略回测页面使用统一的数据源 /api/trading/backtest/strategies
 */
export async function loadHistoryStrategyOptions() {
    const strategySelect = document.getElementById('history-strategy-filter');
    if (!strategySelect) return;
    
    try {
        // 使用与策略回测一致的API端点
        const response = await fetch('/api/trading/backtest/strategies');
        const data = await response.json();
        
        if (data.success && data.data && data.data.strategies) {
            // 保留第一个选项（全部策略）
            strategySelect.innerHTML = '<option value="">全部策略</option>';
            
            data.data.strategies.forEach(strategy => {
                const option = document.createElement('option');
                // 使用中文名称作为value和显示文本，与策略回测页面保持一致
                const chineseName = strategy.display_name || strategy.name;
                option.value = chineseName;
                option.textContent = chineseName;
                strategySelect.appendChild(option);
            });
        }
    } catch (error) {
        console.error('加载策略列表失败:', error);
    }
}

/**
 * 显示行业股票列表
 * @param {string} industry - 行业名称
 * @param {number} limit - 显示数量
 */
export async function showIndustryStocks(industry, limit = 50) {
    try {
        const response = await fetch(`/api/dashboard/industry-stocks?industry=${encodeURIComponent(industry)}&limit=${limit}`);
        const result = await response.json();
        
        if (result.success) {
            showStocksModal(`${industry}行业股票列表`, result.stocks, result.date || '');
        } else {
            alert('加载行业股票失败: ' + result.error);
        }
    } catch (error) {
        alert('加载行业股票失败: ' + error.message);
    }
}

/**
 * 显示板块股票列表
 * @param {string} area - 板块名称
 * @param {number} limit - 显示数量
 */
export async function showAreaStocks(area, limit = 50) {
    try {
        const response = await fetch(`/api/dashboard/area-stocks?area=${encodeURIComponent(area)}&limit=${limit}`);
        const result = await response.json();
        
        if (result.success) {
            showStocksModal(`${area}板块股票列表`, result.stocks, result.date || '');
        } else {
            alert('加载板块股票失败: ' + result.error);
        }
    } catch (error) {
        alert('加载板块股票失败: ' + error.message);
    }
}

/**
 * 显示股票列表模态框
 * @param {string} title - 模态框标题
 * @param {Array} stocks - 股票列表数据
 * @param {string} date - 评分日期
 */
export function showStocksModal(title, stocks, date) {
    const modal = document.getElementById('stock-modal');
    document.getElementById('stock-detail-modal-title').textContent = title;
    
    // 隐藏K线图表容器，只显示股票列表
    const chartContainer = document.getElementById('stock-chart-container');
    chartContainer.style.display = 'none';
    
    // 清空股票信息区域
    const stockInfo = document.getElementById('stock-info');
    stockInfo.innerHTML = '';
    
    if (stocks.length === 0) {
        stockInfo.innerHTML = '<p class="text-muted">暂无股票数据</p>';
        modal.classList.add('active');
        return;
    }
    
    // 构建表格
    let html = `
        <div class="table-responsive">
            <table class="table table-striped">
                <thead>
                    <tr>
                        <th>排名</th>
                        <th>股票代码</th>
                        <th>股票名称</th>
                        <th>评分</th>
                        <th>行业</th>
                        <th>板块</th>
                        <th>选入价</th>
                        <th>当前价</th>
                        <th>收益率</th>
                        <th>最高价格</th>
                        <th>最高收益</th>
                    </tr>
                </thead>
                <tbody>
    `;
    
    stocks.forEach((item, index) => {
        // 防御性代码，处理可能的undefined值
        const score = item.score || 0;
        const selectionPrice = item.selection_price || 0;
        const currentPrice = item.current_price || 0;
        const currentReturn = item.current_yield || 0;
        const highestPrice = item.highest_price || 0;
        const highestReturn = item.highest_yield || 0;
        
        html += `
            <tr>
                <td>${index + 1}</td>
                <td><a href="javascript:void(0)" onclick="viewStockDetail('${item.stock_code}')" class="stock-link">${item.stock_code}</a></td>
                <td>${item.stock_name}</td>
                <td><a href="javascript:void(0)" onclick="showScoreDetail('${item.stock_code}', '${date}')" class="score-link">${score.toFixed(2)}</a></td>
                <td>${item.industry || '-'}</td>
                <td>${item.sector || '-'}</td>
                <td>¥${selectionPrice.toFixed(2)}</td>
                <td>¥${currentPrice.toFixed(2)}</td>
                <td class="${currentReturn >= 0 ? 'text-success' : 'text-danger'}">${currentReturn.toFixed(2)}%</td>
                <td>¥${highestPrice.toFixed(2)}</td>
                <td class="${highestReturn >= 0 ? 'text-success' : 'text-danger'}">${highestReturn.toFixed(2)}%</td>
            </tr>
        `;
    });
    
    html += `
                </tbody>
            </table>
        </div>
    `;
    
    stockInfo.innerHTML = html;
    modal.classList.add('active');
}

/**
 * 初始化K线图表
 * @param {string} containerId - 容器ID
 * @param {Object} data - K线数据
 */
function initKlineChart(containerId, data) {
    // 调用全局的initKlineChart函数
    if (window.initKlineChart) {
        window.initKlineChart(containerId, data);
    } else {
        console.error('全局initKlineChart函数不存在');
    }
}
