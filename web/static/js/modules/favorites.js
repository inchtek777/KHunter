/**
 * 收藏夹页面模块
 * 支持自选股分组管理：分组增删改、批量查看、成员移除
 */

let currentGroupId = null;      // null 表示"未分组"视图
let groupsCache = [];
let ungroupedCache = [];

/**
 * 加载分组与收藏列表
 */
export async function loadFavorites() {
    const groupsList = document.getElementById('favorites-groups-list');
    if (!groupsList) return;
    groupsList.innerHTML = '<li class="loading">加载中...</li>';

    try {
        const response = await fetch('/api/stock/groups');
        const result = await response.json();

        if (!result.success) {
            groupsList.innerHTML = `<li style="color: #dc2626;">加载失败: ${result.error || ''}</li>`;
            return;
        }

        groupsCache = result.groups || [];
        ungroupedCache = result.ungrouped || [];
        renderGroupsList();

        if (currentGroupId !== null && !groupsCache.some(g => g.id === currentGroupId)) {
            currentGroupId = null;
        }
        if (currentGroupId === null && groupsCache.length > 0 && ungroupedCache.length === 0) {
            currentGroupId = groupsCache[0].id;
        }
        renderCurrentView();
    } catch (error) {
        console.error('加载收藏列表失败:', error);
        groupsList.innerHTML = `<li style="color: #dc2626;">加载失败: ${error.message}</li>`;
    }
}

/**
 * 渲染左侧分组列表
 */
function renderGroupsList() {
    const list = document.getElementById('favorites-groups-list');
    let html = '';

    groupsCache.forEach(g => {
        const active = g.id === currentGroupId ? 'active' : '';
        html += `<li class="group-item ${active}" data-group="${g.id}" onclick="selectFavoriteGroup(${g.id})">
            <span class="group-name">📁 ${escapeHtml(g.name)}</span>
            <span class="group-count">${g.member_count}</span>
        </li>`;
    });

    const ungroupedActive = currentGroupId === null ? 'active' : '';
    html += `<li class="group-item ${ungroupedActive}" data-group="ungrouped" onclick="selectFavoriteGroup(null)">
        <span class="group-name">⭐ 未分组收藏</span>
        <span class="group-count">${ungroupedCache.length}</span>
    </li>`;

    list.innerHTML = html;
}

/**
 * 渲染右侧当前视图
 */
async function renderCurrentView() {
    const container = document.getElementById('favorites-list');
    const title = document.getElementById('favorites-current-title');
    const actions = document.getElementById('favorites-group-actions');
    if (!container) return;

    if (currentGroupId === null) {
        if (title) title.textContent = '⭐ 未分组收藏';
        if (actions) actions.style.display = 'none';
        renderStockTable(ungroupedCache, false);
        return;
    }

    const group = groupsCache.find(g => g.id === currentGroupId);
    if (title) title.textContent = `📁 ${group ? group.name : '分组'}`;
    if (actions) actions.style.display = '';

    container.innerHTML = '<p style="color: #999;">加载中...</p>';
    try {
        const response = await fetch(`/api/stock/group/${currentGroupId}/members`);
        const result = await response.json();
        if (result.success) {
            renderStockTable(result.data, true);
        } else {
            container.innerHTML = `<p style="color: #dc2626;">加载失败: ${result.error || ''}</p>`;
        }
    } catch (error) {
        container.innerHTML = `<p style="color: #dc2626;">加载失败: ${error.message}</p>`;
    }
}

/**
 * 渲染股票表格
 * @param {Array} items - 股票列表
 * @param {boolean} inGroup - 是否为分组视图（决定移除操作）
 */
function renderStockTable(items, inGroup) {
    const container = document.getElementById('favorites-list');
    if (!items || items.length === 0) {
        container.innerHTML = '<p style="color: #999;">暂无股票</p>';
        return;
    }

    let html = `
        <table class="data-table favorites-table">
            <thead>
                <tr>
                    <th>股票代码</th>
                    <th>股票名称</th>
                    <th>选股策略</th>
                    <th>选入日期</th>
                    <th>${inGroup ? '加入时间' : '保存日期'}</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>
    `;

    items.forEach(item => {
        const code = item.stock_code || '';
        const name = item.stock_name || '-';
        const strategy = item.strategy_name || '-';
        const selDate = item.selection_date || '-';
        const dateField = inGroup ? (item.added_at || '') : (item.saved_at || '');
        const time = dateField.replace('T', ' ').substring(0, 19) || '-';
        const removeBtn = inGroup
            ? `<button class="remove-btn" onclick="removeFromCurrentGroup('${code}')">移出分组</button>`
            : `<button class="remove-btn" onclick="removeFavorite('${code}')">取消收藏</button>`;

        html += `
            <tr>
                <td><span class="stock-link" onclick="openFavStockDetail('${code}')">${code}</span></td>
                <td>${escapeHtml(name)}</td>
                <td>${escapeHtml(strategy)}</td>
                <td>${selDate}</td>
                <td>${time}</td>
                <td>${removeBtn}</td>
            </tr>
        `;
    });

    html += '</tbody></table>';
    container.innerHTML = html;
}

/**
 * 切换当前分组
 * @param {number|null} groupId
 */
window.selectFavoriteGroup = function(groupId) {
    currentGroupId = groupId;
    renderGroupsList();
    renderCurrentView();
};

/**
 * 新建分组
 */
window.createFavoriteGroup = async function() {
    const input = document.getElementById('new-group-name');
    const name = input ? input.value.trim() : '';
    if (!name) {
        alert('请输入分组名称');
        return;
    }
    try {
        const response = await fetch('/api/stock/group', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name })
        });
        const result = await response.json();
        if (result.success) {
            if (input) input.value = '';
            currentGroupId = result.group_id;
            await loadFavorites();
        } else {
            alert('新建分组失败: ' + (result.error || ''));
        }
    } catch (error) {
        alert('新建分组失败: ' + error.message);
    }
};

/**
 * 重命名当前分组
 */
window.renameCurrentGroup = async function() {
    if (currentGroupId === null) return;
    const group = groupsCache.find(g => g.id === currentGroupId);
    const name = prompt('请输入新的分组名称：', group ? group.name : '');
    if (!name || !name.trim()) return;
    try {
        const response = await fetch(`/api/stock/group/${currentGroupId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name.trim() })
        });
        const result = await response.json();
        if (result.success) {
            await loadFavorites();
        } else {
            alert('重命名失败: ' + (result.error || ''));
        }
    } catch (error) {
        alert('重命名失败: ' + error.message);
    }
};

/**
 * 删除当前分组
 */
window.deleteCurrentGroup = async function() {
    if (currentGroupId === null) return;
    const group = groupsCache.find(g => g.id === currentGroupId);
    if (!confirm(`确定删除分组「${group ? group.name : ''}」吗？组内股票将解除归属，收藏主记录保留。`)) return;
    try {
        const response = await fetch(`/api/stock/group/${currentGroupId}`, { method: 'DELETE' });
        const result = await response.json();
        if (result.success) {
            currentGroupId = null;
            await loadFavorites();
        } else {
            alert('删除分组失败: ' + (result.error || ''));
        }
    } catch (error) {
        alert('删除分组失败: ' + error.message);
    }
};

/**
 * 从当前分组移除股票
 * @param {string} code
 */
window.removeFromCurrentGroup = async function(code) {
    if (currentGroupId === null) return;
    if (!confirm(`确定将 ${code} 移出该分组吗？`)) return;
    try {
        const response = await fetch(`/api/stock/group/${currentGroupId}/stock/${code}`, { method: 'DELETE' });
        const result = await response.json();
        if (result.success) {
            await loadFavorites();
        } else {
            alert('移出分组失败: ' + (result.error || ''));
        }
    } catch (error) {
        alert('移出分组失败: ' + error.message);
    }
};

/**
 * 打开收藏股票详情（穿透到股票详情弹窗）
 * @param {string} code - 股票代码
 */
window.openFavStockDetail = function(code) {
    import('./stocks.js').then(module => module.viewStockDetail(code));
};

/**
 * 取消收藏（彻底移除，含所有分组归属）
 * @param {string} code - 股票代码
 */
window.removeFavorite = async function(code) {
    if (!confirm(`确定取消收藏 ${code} 吗？该股票将从所有分组中移除。`)) return;

    try {
        const response = await fetch(`/api/stock/favorite/${code}`, { method: 'DELETE' });
        const result = await response.json();
        if (result.success) {
            loadFavorites();
        } else {
            alert('取消收藏失败: ' + (result.error || ''));
        }
    } catch (error) {
        alert('取消收藏失败: ' + error.message);
    }
};

/**
 * HTML 文本转义
 * @param {*} value
 * @returns {string}
 */
function escapeHtml(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}
