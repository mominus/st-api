/**
 * API Service 管理后台脚本
 */

// API 基础配置
const API_BASE = '/api/admin';
let authToken = localStorage.getItem('authToken');
let currentPage = 'dashboard';

// 数据缓存
let accountsData = [];
let groupsData = [];
let apiKeysData = [];

// ==================== 初始化 ====================

document.addEventListener('DOMContentLoaded', () => {
    // 检查登录状态
    if (authToken) {
        showAdminPage();
        loadDashboard();
    } else {
        showLoginPage();
    }
    
    // 绑定事件
    bindEvents();
});

function bindEvents() {
    // 登录表单
    document.getElementById('login-form').addEventListener('submit', handleLogin);
    
    // 退出登录
    document.getElementById('logout-btn').addEventListener('click', handleLogout);
    
    // 侧边栏导航
    document.querySelectorAll('.sidebar-nav a[data-page]').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            switchPage(link.dataset.page);
        });
    });
    
    // 表单提交
    document.getElementById('account-form').addEventListener('submit', handleAccountSubmit);
    document.getElementById('group-form').addEventListener('submit', handleGroupSubmit);
    document.getElementById('apikey-form').addEventListener('submit', handleApiKeySubmit);
    document.getElementById('apikey-edit-form').addEventListener('submit', handleApiKeyEditSubmit);
}

// ==================== API 调用封装 ====================

async function apiCall(endpoint, options = {}) {
    const url = `${API_BASE}${endpoint}`;
    const headers = {
        'Content-Type': 'application/json',
        ...options.headers
    };
    
    if (authToken) {
        headers['Authorization'] = `Bearer ${authToken}`;
    }
    
    try {
        const response = await fetch(url, {
            ...options,
            headers
        });
        
        if (response.status === 401) {
            // Token 过期，退出登录
            handleLogout();
            throw new Error('登录已过期，请重新登录');
        }
        
        const data = await response.json();
        
        if (!response.ok) {
            throw new Error(data.detail || data.message || '请求失败');
        }
        
        return data;
    } catch (error) {
        console.error('API Error:', error);
        throw error;
    }
}

// ==================== 认证相关 ====================

async function handleLogin(e) {
    e.preventDefault();
    
    const username = document.getElementById('username').value;
    const password = document.getElementById('password').value;
    const errorEl = document.getElementById('login-error');
    
    try {
        errorEl.classList.add('hidden');
        const data = await apiCall('/auth/login', {
            method: 'POST',
            body: JSON.stringify({ username, password })
        });
        
        authToken = data.token;
        localStorage.setItem('authToken', authToken);
        
        showAdminPage();
        loadDashboard();
        showToast('登录成功', 'success');
    } catch (error) {
        errorEl.textContent = error.message;
        errorEl.classList.remove('hidden');
    }
}

function handleLogout() {
    authToken = null;
    localStorage.removeItem('authToken');
    showLoginPage();
    showToast('已退出登录', 'success');
}

function showLoginPage() {
    document.getElementById('login-page').classList.remove('hidden');
    document.getElementById('admin-page').classList.add('hidden');
}

function showAdminPage() {
    document.getElementById('login-page').classList.add('hidden');
    document.getElementById('admin-page').classList.remove('hidden');
}


// ==================== 页面切换 ====================

function switchPage(page) {
    currentPage = page;
    
    // 更新导航高亮
    document.querySelectorAll('.sidebar-nav a').forEach(link => {
        link.classList.remove('active');
        if (link.dataset.page === page) {
            link.classList.add('active');
        }
    });
    
    // 隐藏所有页面
    document.querySelectorAll('.page-content').forEach(el => {
        el.classList.add('hidden');
    });
    
    // 显示目标页面
    document.getElementById(`page-${page}`).classList.remove('hidden');
    
    // 停止仪表板自动同步（如果离开仪表板）
    if (page !== 'dashboard') {
        stopMonitorAutoSync();
    }
    
    // 加载页面数据（使用缓存避免重复加载）
    switch (page) {
        case 'dashboard':
            loadDashboard();
            break;
        case 'accounts':
            // 如果已有数据，直接渲染
            if (accountsData.length > 0) {
                renderAccountsTable();
                loadGroupsForSelect();
            } else {
                loadAccounts();
            }
            break;
        case 'groups':
            if (groupsData.length > 0) {
                renderGroupsTable();
            } else {
                loadGroups();
            }
            break;
        case 'apikeys':
            if (apiKeysData.length > 0) {
                renderApiKeysTable();
            } else {
                loadApiKeys();
            }
            break;
    }
}

// ==================== 仪表板/监控中心 ====================

// 存储账号的分析数据
let analyticsCache = {};
let monitorInterval = null;

async function loadDashboard() {
    try {
        // 如果已有账号数据缓存，先用缓存数据渲染，避免显示加载状态
        if (accountsData.length > 0) {
            renderMonitorTable(accountsData);
            // 用缓存数据更新统计
            const uniqueOrgIds = new Set(accountsData.filter(a => a.status === 'active').map(a => a.org_id));
            let totalTodayTokens = 0;
            accountsData.forEach(account => {
                const analytics = analyticsCache[account.id];
                totalTodayTokens += analytics?.today_tokens ?? account.daily_used;
            });
            updateDashboardStats(uniqueOrgIds.size, totalTodayTokens);
            // 启动30秒自动同步
            startMonitorAutoSync();
            return;
        }
        
        // 首次加载时显示加载中状态
        document.getElementById('stat-active-accounts').textContent = '...';
        document.getElementById('stat-api-keys').textContent = '...';
        document.getElementById('stat-today-requests').textContent = '...';
        document.getElementById('stat-today-tokens').textContent = '...';
        
        // 加载监控表格（会计算统计数据）
        await loadMonitorTable();
        
        // 启动30秒自动同步
        startMonitorAutoSync();
    } catch (error) {
        showToast('加载仪表板失败: ' + error.message, 'error');
        // 显示错误状态
        document.getElementById('stat-active-accounts').textContent = '0';
        document.getElementById('stat-api-keys').textContent = '0';
        document.getElementById('stat-today-requests').textContent = '0';
        document.getElementById('stat-today-tokens').textContent = '0';
    }
}

function startMonitorAutoSync() {
    // 清除旧的定时器
    if (monitorInterval) {
        clearInterval(monitorInterval);
    }
    // 每30秒自动同步
    monitorInterval = setInterval(() => {
        if (currentPage === 'dashboard') {
            syncAllAccountsSilent();
        }
    }, 30000);
}

function stopMonitorAutoSync() {
    if (monitorInterval) {
        clearInterval(monitorInterval);
        monitorInterval = null;
    }
}

// 静默同步（不显示提示，使用缓存的账号数据）
async function syncAllAccountsSilent() {
    // 使用缓存的账号数据，避免重复请求
    const accounts = accountsData.filter(a => a.has_private_key);
    if (accounts.length === 0) return;
    
    try {
        for (const account of accounts) {
            try {
                const syncResponse = await apiCall(`/accounts/${account.id}/sync`, { method: 'POST' });
                if (syncResponse.success) {
                    // 更新缓存
                    analyticsCache[account.id] = {
                        ...analyticsCache[account.id],
                        today_tokens: syncResponse.current_used,
                        today_runs: syncResponse.today_runs,
                        total_tokens: syncResponse.total_tokens
                    };
                    // 更新账号数据中的 daily_used
                    const acc = accountsData.find(a => a.id === account.id);
                    if (acc) {
                        acc.daily_used = syncResponse.current_used;
                        acc.last_sync_at = new Date().toISOString();
                    }
                }
            } catch (e) {
                // 静默失败
            }
        }
        
        // 直接重新渲染表格，不重新请求 API
        renderMonitorTable(accountsData);
        updateLastSyncTime();
        
        // 更新统计数据
        const uniqueOrgIds = new Set(accountsData.filter(a => a.status === 'active').map(a => a.org_id));
        let totalTodayTokens = 0;
        accountsData.forEach(account => {
            const analytics = analyticsCache[account.id];
            totalTodayTokens += analytics?.today_tokens ?? account.daily_used;
        });
        document.getElementById('stat-active-accounts').textContent = uniqueOrgIds.size;
        document.getElementById('stat-today-tokens').textContent = formatNumber(totalTodayTokens);
    } catch (error) {
        // 静默失败
    }
}

async function loadMonitorTable() {
    const tbody = document.getElementById('monitor-table-body');
    
    try {
        const response = await apiCall('/accounts');
        const accounts = response.accounts || [];
        accountsData = accounts; // 更新缓存
        
        if (accounts.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">暂无账号，请先添加后端账号</td></tr>';
            await updateDashboardStats(0, 0);
            return;
        }
        
        // 先渲染表格（显示加载中的数据）
        renderMonitorTable(accounts);
        
        // 为有 Private API Key 的账户获取分析数据
        const accountsWithKey = accounts.filter(a => a.has_private_key);
        for (const account of accountsWithKey) {
            try {
                const syncResponse = await apiCall(`/accounts/${account.id}/sync`, { method: 'POST' });
                if (syncResponse.success) {
                    analyticsCache[account.id] = {
                        today_tokens: syncResponse.current_used,
                        today_runs: syncResponse.today_runs,
                        total_tokens: syncResponse.total_tokens
                    };
                    // 更新账号数据
                    const acc = accountsData.find(a => a.id === account.id);
                    if (acc) {
                        acc.daily_used = syncResponse.current_used;
                    }
                }
            } catch (e) {
                console.error(`同步账号 ${account.name} 失败:`, e);
            }
        }
        
        // 重新渲染表格（显示同步后的数据）
        renderMonitorTable(accountsData);
        
        // 计算统计数据：按 org_id 去重计算唯一账号数
        const uniqueOrgIds = new Set(accountsData.filter(a => a.status === 'active').map(a => a.org_id));
        const activeAccountCount = uniqueOrgIds.size;
        
        // 计算今日总 Token 使用量
        let totalTodayTokens = 0;
        accountsData.forEach(account => {
            const analytics = analyticsCache[account.id];
            totalTodayTokens += analytics?.today_tokens ?? account.daily_used;
        });
        
        // 更新顶部统计
        updateDashboardStats(activeAccountCount, totalTodayTokens);
        
        // 更新最后同步时间
        updateLastSyncTime();
    } catch (error) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-danger">加载失败</td></tr>';
    }
}

async function updateDashboardStats(activeAccounts, todayTokens) {
    document.getElementById('stat-active-accounts').textContent = activeAccounts;
    document.getElementById('stat-today-tokens').textContent = formatNumber(todayTokens);
    
    // 获取 API Key 数量（直接从 keys 接口获取）
    try {
        const keysResponse = await apiCall('/keys');
        const keys = keysResponse.keys || [];
        const activeKeys = keys.filter(k => k.status === 'active').length;
        document.getElementById('stat-api-keys').textContent = activeKeys;
    } catch (e) {
        document.getElementById('stat-api-keys').textContent = '0';
    }
    
    // 获取今日请求数和历史累计统计
    try {
        const response = await apiCall('/stats/overview');
        const overview = response.overview || {};
        document.getElementById('stat-today-requests').textContent = formatNumber(overview.today?.requests || 0);
        
        // 更新历史累计统计
        const allTime = overview.all_time || {};
        document.getElementById('stat-all-time-requests').textContent = formatNumber(allTime.requests || 0);
        document.getElementById('stat-all-time-tokens').textContent = formatNumber(allTime.total_tokens || 0);
        document.getElementById('stat-all-time-input').textContent = formatNumber(allTime.input_tokens || 0);
        document.getElementById('stat-all-time-output').textContent = formatNumber(allTime.output_tokens || 0);
    } catch (e) {
        document.getElementById('stat-today-requests').textContent = '0';
        document.getElementById('stat-all-time-requests').textContent = '0';
        document.getElementById('stat-all-time-tokens').textContent = '0';
        document.getElementById('stat-all-time-input').textContent = '0';
        document.getElementById('stat-all-time-output').textContent = '0';
    }
}

function renderMonitorTable(accounts) {
    const tbody = document.getElementById('monitor-table-body');
    
    // 按 org_id 合并账号（同一个 org_id 是同一个后端账号，额度不叠加）
    const orgMap = new Map();
    accounts.forEach(account => {
        const orgId = account.org_id;
        if (!orgMap.has(orgId)) {
            orgMap.set(orgId, {
                name: account.name,
                org_id: orgId,
                model_groups: [],
                daily_quota: account.daily_quota,  // 每个账号只有一个额度，不叠加
                daily_used: 0,
                total_tokens: 0,
                status: 'active',
                account_ids: []
            });
        }
        const org = orgMap.get(orgId);
        org.model_groups.push(account.model_group);
        org.account_ids.push(account.id);
        
        // 使用量取最大值（同一个 org_id 的使用量是共享的）
        const analytics = analyticsCache[account.id];
        const accountUsed = analytics?.today_tokens ?? account.daily_used;
        const accountTotal = analytics?.total_tokens ?? 0;
        org.daily_used = Math.max(org.daily_used, accountUsed);
        org.total_tokens = Math.max(org.total_tokens, accountTotal);
        
        // 状态：如果有任何一个是 exhausted 或 disabled，则显示该状态
        if (account.status === 'exhausted' && org.status === 'active') {
            org.status = 'exhausted';
        } else if (account.status === 'disabled') {
            org.status = 'disabled';
        }
    });
    
    // 渲染合并后的账号
    tbody.innerHTML = Array.from(orgMap.values()).map(org => {
        const remaining = Math.max(0, org.daily_quota - org.daily_used);
        const percentage = org.daily_quota > 0 ? Math.round((org.daily_used / org.daily_quota) * 100) : 0;
        
        // 使用率颜色
        let usageClass = 'text-success';
        if (percentage >= 100) usageClass = 'text-danger';
        else if (percentage >= 80) usageClass = 'text-warning';
        
        // 状态
        const statusClass = org.status === 'active' ? 'status-active' : 
                           (org.status === 'exhausted' ? 'status-exhausted' : 'status-disabled');
        
        // 模型组显示（去重）
        const uniqueGroups = [...new Set(org.model_groups)];
        
        return `
            <tr data-org-id="${escapeHtml(org.org_id)}">
                <td>
                    <div class="account-name-cell">
                        ${escapeHtml(org.name)}
                        <span class="account-org">${escapeHtml(org.org_id?.substring(0, 8) || '')}...</span>
                    </div>
                </td>
                <td>
                    <div class="usage-display">
                        <span class="usage-text ${usageClass}">${formatNumber(org.daily_used)}</span>
                    </div>
                </td>
                <td>
                    <span class="${percentage >= 80 ? 'text-warning' : ''}">${formatNumber(remaining)}</span>
                </td>
                <td>
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <div class="usage-bar-mini">
                            <div class="usage-fill ${usageClass}" style="width: ${Math.min(percentage, 100)}%"></div>
                        </div>
                        <span class="${usageClass}" style="font-weight: 600; min-width: 45px;">${percentage}%</span>
                    </div>
                </td>
                <td>${formatNumber(org.total_tokens)}</td>
                <td><span class="status-badge ${statusClass}">${getStatusText(org.status)}</span></td>
            </tr>
        `;
    }).join('');
}

async function loadAccountAnalytics(accountId) {
    try {
        const response = await apiCall(`/accounts/${accountId}/analytics?days=7`);
        if (response.success && response.stats) {
            analyticsCache[accountId] = response.stats;
            // 更新表格行
            updateMonitorRow(accountId);
        }
    } catch (error) {
        console.error(`加载账号 ${accountId} 分析数据失败:`, error);
    }
}

function updateMonitorRow(accountId) {
    // 重新加载表格
    loadMonitorTable();
}

async function syncAllAccounts() {
    const btn = document.getElementById('sync-all-btn');
    const icon = btn.querySelector('.sync-icon');
    btn.disabled = true;
    if (icon) icon.classList.add('spinning');
    
    try {
        const response = await apiCall('/accounts');
        const accounts = (response.accounts || []).filter(a => a.has_private_key);
        
        if (accounts.length === 0) {
            showToast('没有配置 Private API Key 的账号', 'warning');
            return;
        }
        
        let successCount = 0;
        for (const account of accounts) {
            try {
                const syncResponse = await apiCall(`/accounts/${account.id}/sync`, { method: 'POST' });
                if (syncResponse.success) {
                    analyticsCache[account.id] = {
                        today_tokens: syncResponse.current_used,
                        today_runs: syncResponse.today_runs,
                        total_tokens: syncResponse.total_tokens
                    };
                    successCount++;
                }
            } catch (e) {
                console.error(`同步账号 ${account.name} 失败:`, e);
            }
        }
        
        // 重新加载表格
        await loadMonitorTable();
        showToast(`同步完成: ${successCount}/${accounts.length}`, 'success');
    } catch (error) {
        showToast('同步失败: ' + error.message, 'error');
    } finally {
        btn.disabled = false;
        if (icon) icon.classList.remove('spinning');
    }
}

function updateLastSyncTime() {
    const el = document.getElementById('last-sync-time');
    if (el) {
        el.textContent = `最后更新: ${new Date().toLocaleTimeString('zh-CN')}`;
    }
}

function refreshDashboard() {
    loadDashboard();
    showToast('已刷新', 'success');
}

// ==================== 账号管理 ====================

async function loadAccounts() {
    try {
        const response = await apiCall('/accounts');
        accountsData = response.accounts || [];
        await loadGroupsForSelect();
        renderAccountsTable();
    } catch (error) {
        showToast('加载账号列表失败: ' + error.message, 'error');
    }
}

// 存储测试结果状态
let testResults = {};

function renderAccountsTable() {
    const tbody = document.getElementById('accounts-table-body');
    const searchTerm = document.getElementById('account-search').value.toLowerCase();
    const groupFilter = document.getElementById('account-group-filter').value;
    const statusFilter = document.getElementById('account-status-filter').value;
    
    let filtered = accountsData.filter(account => {
        if (searchTerm && !account.name.toLowerCase().includes(searchTerm)) return false;
        if (groupFilter && account.model_group !== groupFilter) return false;
        if (statusFilter && account.status !== statusFilter) return false;
        return true;
    });
    
    if (filtered.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">暂无数据</td></tr>';
        return;
    }
    
    tbody.innerHTML = filtered.map(account => {
        const statusClass = account.status === 'active' ? 'status-active' : 
                           (account.status === 'exhausted' ? 'status-exhausted' : 'status-disabled');
        
        // 获取测试状态标记
        const testStatus = getTestStatusHtml(account.id);
        
        // 格式化最后使用时间
        const lastUsedClass = account.last_used_at && isRecent(account.last_used_at) ? 'recent' : '';
        
        return `
            <tr data-account-id="${account.id}">
                <td>
                    <div class="account-name-cell">
                        ${escapeHtml(account.name)}
                        <span class="account-org">${escapeHtml(account.org_id?.substring(0, 8) || '')}...</span>
                    </div>
                </td>
                <td>
                    <span class="model-group-tag">${escapeHtml(account.model_group)}</span>
                    ${testStatus}
                </td>
                <td>
                    <span class="usage-inline">${formatNumber(account.daily_used)} / ${formatNumber(account.daily_quota)}</span>
                </td>
                <td><span class="status-badge ${statusClass}">${getStatusText(account.status)}</span></td>
                <td><span class="last-used-time ${lastUsedClass}">${account.last_used_at ? formatDateTime(account.last_used_at) : '-'}</span></td>
                <td class="actions">
                    <button class="btn btn-sm btn-info" id="test-btn-${account.id}" onclick="testAccount('${account.id}')" title="测试连接">测试</button>
                    <button class="btn btn-sm btn-secondary" onclick="editAccount('${account.id}')">编辑</button>
                    ${account.status === 'disabled' 
                        ? `<button class="btn btn-sm btn-success" onclick="toggleAccountStatus('${account.id}', 'active')" title="启用此账号">启用</button>`
                        : `<button class="btn btn-sm btn-warning" onclick="toggleAccountStatus('${account.id}', 'disabled')" title="禁用此账号">禁用</button>`
                    }
                    <button class="btn btn-sm btn-danger" onclick="deleteAccount('${account.id}')">删除</button>
                </td>
            </tr>
        `;
    }).join('');
}

// 检查时间是否在最近1小时内
function isRecent(dateStr) {
    if (!dateStr) return false;
    const date = new Date(dateStr);
    const now = new Date();
    return (now - date) < 3600000; // 1小时
}

function getTestStatusHtml(accountId) {
    const result = testResults[accountId];
    if (!result) return '';
    
    if (result.testing) {
        return '<span class="test-status testing"><span class="spinner-sm"></span></span>';
    } else if (result.success) {
        return `<span class="test-status success" title="测试成功 ${result.time}ms">✓</span>`;
    } else {
        return `<span class="test-status failed" title="${escapeHtml(result.error || '测试失败')}">✗</span>`;
    }
}

function filterAccounts() {
    renderAccountsTable();
}

function showAddAccountModal() {
    document.getElementById('account-modal-title').textContent = '添加账号';
    document.getElementById('account-form').reset();
    document.getElementById('account-id').value = '';
    
    // 重置 placeholder
    const apiKeyInput = document.getElementById('account-api-key');
    apiKeyInput.placeholder = '从后端控制台获取';
    apiKeyInput.required = true;
    
    const privateKeyInput = document.getElementById('account-private-key');
    privateKeyInput.placeholder = '从后端控制台获取';
    
    openModal('account-modal');
}

function editAccount(id) {
    const account = accountsData.find(a => a.id === id);
    if (!account) return;
    
    document.getElementById('account-modal-title').textContent = '编辑账号';
    document.getElementById('account-id').value = account.id;
    document.getElementById('account-name').value = account.name;
    document.getElementById('account-org-id').value = account.org_id;
    document.getElementById('account-flow-id').value = account.flow_id;
    
    // 显示脱敏的 API Key 作为占位符提示
    const apiKeyInput = document.getElementById('account-api-key');
    apiKeyInput.value = '';
    apiKeyInput.placeholder = account.api_key_masked || '留空保持不变';
    apiKeyInput.required = false;
    
    document.getElementById('account-model-group').value = account.model_group;
    document.getElementById('account-daily-quota').value = account.daily_quota;
    
    // Private API Key
    const privateKeyInput = document.getElementById('account-private-key');
    privateKeyInput.value = '';
    privateKeyInput.placeholder = account.has_private_key ? '已配置，留空保持不变' : '未配置';
    
    openModal('account-modal');
}

async function handleAccountSubmit(e) {
    e.preventDefault();
    
    const id = document.getElementById('account-id').value;
    const data = {
        name: document.getElementById('account-name').value,
        org_id: document.getElementById('account-org-id').value,
        flow_id: document.getElementById('account-flow-id').value,
        model_group: document.getElementById('account-model-group').value,
        daily_quota: parseInt(document.getElementById('account-daily-quota').value) || 1000000
    };
    
    const apiKey = document.getElementById('account-api-key').value;
    if (apiKey) {
        data.api_key = apiKey;
    }
    
    const privateKey = document.getElementById('account-private-key').value;
    if (privateKey) {
        data.private_api_key = privateKey;
    }
    
    try {
        if (id) {
            await apiCall(`/accounts/${id}`, { method: 'PUT', body: JSON.stringify(data) });
            showToast('账号已更新', 'success');
        } else {
            data.api_key = apiKey;  // 新建时必须有 API Key
            await apiCall('/accounts', { method: 'POST', body: JSON.stringify(data) });
            showToast('账号已添加', 'success');
        }
        
        closeModal('account-modal');
        loadAccounts();
    } catch (error) {
        showToast('保存失败: ' + error.message, 'error');
    }
}

function deleteAccount(id) {
    const account = accountsData.find(a => a.id === id);
    showConfirmModal(`确定要删除账号 "${account?.name}" 吗？`, async () => {
        try {
            await apiCall(`/accounts/${id}`, { method: 'DELETE' });
            showToast('账号已删除', 'success');
            loadAccounts();
        } catch (error) {
            showToast('删除失败: ' + error.message, 'error');
        }
    });
}

async function toggleAccountStatus(id, newStatus) {
    const account = accountsData.find(a => a.id === id);
    if (!account) return;
    
    const actionText = newStatus === 'disabled' ? '禁用' : '启用';
    const confirmMsg = newStatus === 'disabled' 
        ? `确定要禁用账号 "${account.name}" 吗？\n\n禁用后，该账号的工作流将不会被 API 调用选中。\n同一后端账号的其他工作流不受影响。`
        : `确定要启用账号 "${account.name}" 吗？`;
    
    showConfirmModal(confirmMsg, async () => {
        try {
            await apiCall(`/accounts/${id}`, { 
                method: 'PUT', 
                body: JSON.stringify({ status: newStatus }) 
            });
            showToast(`账号已${actionText}`, 'success');
            loadAccounts();
        } catch (error) {
            showToast(`${actionText}失败: ` + error.message, 'error');
        }
    });
}

async function testAccount(id, showModal = true) {
    const account = accountsData.find(a => a.id === id);
    if (!account) return null;
    
    // 更新按钮状态
    const btn = document.getElementById(`test-btn-${id}`);
    if (btn) {
        btn.innerHTML = '<span class="spinner-sm"></span>测试中';
        btn.classList.add('btn-testing');
    }
    
    // 更新测试状态
    testResults[id] = { testing: true };
    updateTestStatusInTable(id);
    
    try {
        const response = await apiCall(`/accounts/${id}/test`, {
            method: 'POST',
            body: JSON.stringify({ message: '你好，这是一条测试消息。请简短回复。' })
        });
        
        const result = {
            accountId: id,
            accountName: account.name,
            modelGroup: account.model_group,
            success: response.success,
            time: response.response_time_ms || 0,
            output: response.output || '(无输出)',
            error: response.message
        };
        
        // 更新测试结果
        testResults[id] = {
            success: response.success,
            time: result.time,
            error: response.success ? null : response.message
        };
        updateTestStatusInTable(id);
        
        if (showModal) {
            showTestResultModal([result]);
        }
        
        return result;
        
    } catch (error) {
        const result = {
            accountId: id,
            accountName: account.name,
            modelGroup: account.model_group,
            success: false,
            time: 0,
            output: '',
            error: error.message
        };
        
        testResults[id] = {
            success: false,
            error: error.message
        };
        updateTestStatusInTable(id);
        
        if (showModal) {
            showTestResultModal([result]);
        }
        
        return result;
    } finally {
        // 恢复按钮状态
        if (btn) {
            btn.innerHTML = '测试';
            btn.classList.remove('btn-testing');
        }
    }
}

function updateTestStatusInTable(accountId) {
    const row = document.querySelector(`tr[data-account-id="${accountId}"]`);
    if (!row) return;
    
    const modelGroupCell = row.querySelector('td:nth-child(2)');
    if (!modelGroupCell) return;
    
    const account = accountsData.find(a => a.id === accountId);
    if (!account) return;
    
    const testStatus = getTestStatusHtml(accountId);
    modelGroupCell.innerHTML = `${escapeHtml(account.model_group)}${testStatus}`;
}

async function testAllAccounts() {
    if (accountsData.length === 0) {
        showToast('没有可测试的账号', 'warning');
        return;
    }
    
    // 显示进度条
    const progressContainer = document.getElementById('test-progress-container');
    const progressFill = document.getElementById('test-progress-fill');
    const progressText = document.getElementById('test-progress-text');
    const testAllBtn = document.getElementById('test-all-btn');
    
    progressContainer.classList.remove('hidden');
    testAllBtn.innerHTML = '<span class="spinner-sm"></span>测试中...';
    testAllBtn.classList.add('btn-testing');
    
    // 清除之前的测试结果
    testResults = {};
    renderAccountsTable();
    
    const total = accountsData.length;
    progressText.textContent = `正在并行测试 ${total} 个账号...`;
    progressFill.style.width = '10%';
    
    // 并行执行所有测试
    const testPromises = accountsData.map(account => testAccount(account.id, false));
    
    // 等待所有测试完成
    const results = await Promise.all(testPromises);
    
    // 过滤掉 null 结果
    const validResults = results.filter(r => r !== null);
    
    // 完成
    progressFill.style.width = '100%';
    progressText.textContent = `测试完成！成功: ${validResults.filter(r => r.success).length}/${total}`;
    testAllBtn.innerHTML = '🧪 一键测试全部';
    testAllBtn.classList.remove('btn-testing');
    
    // 3秒后隐藏进度条
    setTimeout(() => {
        progressContainer.classList.add('hidden');
    }, 3000);
    
    // 显示结果模态框
    showTestResultModal(validResults);
}

function showTestResultModal(results) {
    const title = document.getElementById('test-result-title');
    const content = document.getElementById('test-result-content');
    
    const successCount = results.filter(r => r.success).length;
    const failCount = results.length - successCount;
    
    title.textContent = `🧪 测试结果 (成功: ${successCount}, 失败: ${failCount})`;
    
    if (results.length === 0) {
        content.innerHTML = '<div class="empty-state"><p>没有测试结果</p></div>';
    } else {
        content.innerHTML = results.map(result => `
            <div class="test-result-item ${result.success ? 'success' : 'failed'}">
                <div class="test-result-header">
                    <div>
                        <span class="account-name">${escapeHtml(result.accountName)}</span>
                        <span class="status-badge ${result.success ? 'status-active' : 'status-exhausted'}">
                            ${result.success ? '✓ 成功' : '✗ 失败'}
                        </span>
                    </div>
                    <span class="model-group">${escapeHtml(result.modelGroup)}</span>
                </div>
                <div class="test-result-body">
                    ${result.success ? `
                        <div class="result-row">
                            <span class="result-label">响应时间:</span>
                            <span class="result-value">${result.time} ms</span>
                        </div>
                        <div class="result-row">
                            <span class="result-label">输出预览:</span>
                        </div>
                        <div class="output-preview">${escapeHtml(result.output.substring(0, 300))}${result.output.length > 300 ? '...' : ''}</div>
                    ` : `
                        <div class="result-row">
                            <span class="result-label">错误信息:</span>
                            <span class="result-value text-danger">${escapeHtml(result.error || '未知错误')}</span>
                        </div>
                    `}
                </div>
            </div>
        `).join('');
    }
    
    openModal('test-result-modal');
}


// ==================== 模型组管理 ====================

async function loadGroups() {
    try {
        const response = await apiCall('/groups');
        groupsData = response.groups || [];
        renderGroupsTable();
    } catch (error) {
        showToast('加载模型组列表失败: ' + error.message, 'error');
    }
}

function renderGroupsTable() {
    const tbody = document.getElementById('groups-table-body');
    
    if (groupsData.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">暂无数据</td></tr>';
        return;
    }
    
    tbody.innerHTML = groupsData.map(group => `
        <tr>
            <td><strong>${escapeHtml(group.name)}</strong></td>
            <td>${escapeHtml(group.description || '-')}</td>
            <td>${group.account_count || 0}</td>
            <td>${formatDate(group.created_at)}</td>
            <td class="actions">
                <button class="btn btn-sm btn-secondary" onclick="editGroup('${group.id}')">编辑</button>
                <button class="btn btn-sm btn-danger" onclick="deleteGroup('${group.id}')">删除</button>
            </td>
        </tr>
    `).join('');
}

function showAddGroupModal() {
    document.getElementById('group-modal-title').textContent = '添加模型组';
    document.getElementById('group-form').reset();
    document.getElementById('group-id').value = '';
    document.getElementById('group-name').disabled = false; // 新建时启用名称输入
    document.getElementById('group-input-mapping').value = '{"user_input": "in-0"}';
    openModal('group-modal');
}

function editGroup(id) {
    const group = groupsData.find(g => g.id === id);
    if (!group) return;
    
    document.getElementById('group-modal-title').textContent = '编辑模型组';
    document.getElementById('group-id').value = group.id;
    document.getElementById('group-name').value = group.name;
    document.getElementById('group-name').disabled = true; // 编辑时禁用名称修改
    document.getElementById('group-description').value = group.description || '';
    document.getElementById('group-input-mapping').value = JSON.stringify(group.input_mapping || {}, null, 2);
    
    openModal('group-modal');
}

async function handleGroupSubmit(e) {
    e.preventDefault();
    
    const id = document.getElementById('group-id').value;
    let inputMapping = {};
    
    try {
        const mappingStr = document.getElementById('group-input-mapping').value;
        if (mappingStr) {
            inputMapping = JSON.parse(mappingStr);
        }
    } catch (error) {
        showToast('输入字段映射格式错误，请输入有效的 JSON', 'error');
        return;
    }
    
    try {
        if (id) {
            // 编辑时只更新 description 和 input_mapping（name 不可修改）
            const updateData = {
                description: document.getElementById('group-description').value,
                input_mapping: inputMapping
            };
            await apiCall(`/groups/${id}`, { method: 'PUT', body: JSON.stringify(updateData) });
            showToast('模型组已更新', 'success');
        } else {
            // 新建时需要 name
            const createData = {
                name: document.getElementById('group-name').value,
                description: document.getElementById('group-description').value,
                input_mapping: inputMapping
            };
            await apiCall('/groups', { method: 'POST', body: JSON.stringify(createData) });
            showToast('模型组已添加', 'success');
        }
        
        closeModal('group-modal');
        loadGroups();
    } catch (error) {
        showToast('保存失败: ' + error.message, 'error');
    }
}

function deleteGroup(id) {
    const group = groupsData.find(g => g.id === id);
    showConfirmModal(`确定要删除模型组 "${group?.name}" 吗？关联的账号将失去分组。`, async () => {
        try {
            await apiCall(`/groups/${id}`, { method: 'DELETE' });
            showToast('模型组已删除', 'success');
            loadGroups();
        } catch (error) {
            showToast('删除失败: ' + error.message, 'error');
        }
    });
}

// ==================== API Key 管理 ====================

async function loadApiKeys() {
    try {
        console.log('Loading API Keys...');
        const response = await apiCall('/keys?include_revoked=true');
        console.log('API Keys response:', response);
        apiKeysData = response.keys || [];
        console.log('apiKeysData updated:', apiKeysData.length, 'keys');
        await loadGroupsForSelect();
        renderApiKeysTable();
    } catch (error) {
        console.error('Load API Keys error:', error);
        showToast('加载 API Key 列表失败: ' + error.message, 'error');
    }
}

function renderApiKeysTable() {
    const tbody = document.getElementById('apikeys-table-body');
    const searchTerm = document.getElementById('apikey-search').value.toLowerCase();
    const statusFilter = document.getElementById('apikey-status-filter').value;
    
    let filtered = apiKeysData.filter(key => {
        if (searchTerm && !(key.name || '').toLowerCase().includes(searchTerm)) return false;
        if (statusFilter && key.status !== statusFilter) return false;
        return true;
    });
    
    if (filtered.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted">暂无数据</td></tr>';
        return;
    }
    
    tbody.innerHTML = filtered.map(key => {
        const statusClass = key.status === 'active' ? 'status-active' : 
                           (key.status === 'exhausted' ? 'status-exhausted' : 'status-revoked');
        const statusText = key.status === 'active' ? '有效' : 
                          (key.status === 'exhausted' ? '已耗尽' : '已禁用');
        const groups = Array.isArray(key.model_groups) ? key.model_groups : [key.model_groups];
        
        // 请求限制显示
        const quotaDisplay = key.request_quota 
            ? `${formatNumber(key.total_requests || 0)} / ${formatNumber(key.request_quota)}`
            : `${formatNumber(key.total_requests || 0)} / ∞`;
        
        return `
            <tr>
                <td>
                    <div class="account-name-cell">
                        ${escapeHtml(key.name || '未命名')}
                    </div>
                </td>
                <td>
                    <span class="api-key-masked" title="完整 Key 仅在创建时显示一次">
                        ${escapeHtml(key.key_prefix)}...${escapeHtml(key.key_suffix || '****')}
                    </span>
                </td>
                <td>
                    ${groups.map(g => `<span class="model-group-tag" style="margin-right: 6px; margin-bottom: 4px;">${escapeHtml(g)}</span>`).join('')}
                </td>
                <td><span class="usage-inline">${quotaDisplay}</span></td>
                <td><span class="usage-inline">${formatNumber(key.total_tokens || 0)}</span></td>
                <td><span class="status-badge ${statusClass}">${statusText}</span></td>
                <td class="actions">
                    <button class="btn btn-sm btn-secondary" onclick="editApiKey('${key.id}')">编辑</button>
                    ${key.status === 'active' 
                        ? `<button class="btn btn-sm btn-warning" onclick="revokeApiKey('${key.id}')">禁用</button>` 
                        : `<button class="btn btn-sm btn-success" onclick="enableApiKey('${key.id}')">启用</button>`}
                    <button class="btn btn-sm btn-danger" onclick="deleteApiKey('${key.id}')">删除</button>
                </td>
            </tr>
        `;
    }).join('');
}

function filterApiKeys() {
    renderApiKeysTable();
}

async function showAddApiKeyModal() {
    await loadGroupsForSelect();
    
    const select = document.getElementById('apikey-groups');
    select.innerHTML = groupsData.map(g => 
        `<option value="${escapeHtml(g.name)}">${escapeHtml(g.name)}</option>`
    ).join('');
    
    document.getElementById('apikey-form').reset();
    openModal('apikey-modal');
}

async function handleApiKeySubmit(e) {
    e.preventDefault();
    
    const groupsSelect = document.getElementById('apikey-groups');
    const selectedGroups = Array.from(groupsSelect.selectedOptions).map(opt => opt.value);
    
    if (selectedGroups.length === 0) {
        showToast('请至少选择一个模型组', 'error');
        return;
    }
    
    const requestQuota = document.getElementById('apikey-request-quota').value;
    
    const data = {
        name: document.getElementById('apikey-name').value || null,
        model_groups: selectedGroups,
        expires_at: document.getElementById('apikey-expires').value || null,
        request_quota: requestQuota ? parseInt(requestQuota) : null
    };
    
    try {
        const result = await apiCall('/keys', { method: 'POST', body: JSON.stringify(data) });
        
        closeModal('apikey-modal');
        
        // 显示新生成的 Key
        document.getElementById('new-apikey-value').textContent = result.key;
        openModal('new-apikey-modal');
        
        loadApiKeys();
    } catch (error) {
        showToast('生成失败: ' + error.message, 'error');
    }
}

async function editApiKey(id) {
    const key = apiKeysData.find(k => k.id === id);
    if (!key) return;
    
    await loadGroupsForSelect();
    
    // 填充编辑表单
    document.getElementById('apikey-edit-id').value = key.id;
    document.getElementById('apikey-edit-name').value = key.name || '';
    
    // 设置模型组选择
    const select = document.getElementById('apikey-edit-groups');
    select.innerHTML = groupsData.map(g => 
        `<option value="${escapeHtml(g.name)}">${escapeHtml(g.name)}</option>`
    ).join('');
    
    // 选中当前的模型组
    const groups = Array.isArray(key.model_groups) ? key.model_groups : [key.model_groups];
    Array.from(select.options).forEach(opt => {
        opt.selected = groups.includes(opt.value);
    });
    
    // 设置请求限制
    document.getElementById('apikey-edit-request-quota').value = key.request_quota || '';
    
    // 设置过期时间
    if (key.expires_at) {
        const date = new Date(key.expires_at);
        document.getElementById('apikey-edit-expires').value = date.toISOString().slice(0, 16);
    } else {
        document.getElementById('apikey-edit-expires').value = '';
    }
    
    openModal('apikey-edit-modal');
}

async function handleApiKeyEditSubmit(e) {
    e.preventDefault();
    
    const id = document.getElementById('apikey-edit-id').value;
    const groupsSelect = document.getElementById('apikey-edit-groups');
    const selectedGroups = Array.from(groupsSelect.selectedOptions).map(opt => opt.value);
    
    if (selectedGroups.length === 0) {
        showToast('请至少选择一个模型组', 'error');
        return;
    }
    
    const requestQuotaValue = document.getElementById('apikey-edit-request-quota').value;
    let requestQuota = null;
    if (requestQuotaValue !== '') {
        requestQuota = parseInt(requestQuotaValue);
        // -1 表示清除限制
        if (requestQuota === -1) {
            requestQuota = null;
        }
    }
    
    const data = {
        name: document.getElementById('apikey-edit-name').value || null,
        model_groups: selectedGroups,
        expires_at: document.getElementById('apikey-edit-expires').value || null,
        request_quota: requestQuota
    };
    
    try {
        await apiCall(`/keys/${id}`, { method: 'PUT', body: JSON.stringify(data) });
        
        closeModal('apikey-edit-modal');
        showToast('API Key 已更新', 'success');
        
        // 清空缓存，强制重新加载
        apiKeysData = [];
        loadApiKeys();
    } catch (error) {
        showToast('更新失败: ' + error.message, 'error');
    }
}

function copyNewApiKey() {
    const key = document.getElementById('new-apikey-value').textContent;
    copyToClipboard(key, document.querySelector('#new-apikey-modal .copy-btn'));
}

function revokeApiKey(id) {
    showConfirmModal(`确定要禁用此 API Key 吗？禁用后将立即失效，但可以重新启用。`, async () => {
        try {
            await apiCall(`/keys/${id}/revoke`, { method: 'POST' });
            showToast('API Key 已禁用', 'success');
            loadApiKeys();
        } catch (error) {
            showToast('禁用失败: ' + error.message, 'error');
        }
    }, { title: '⚠️ 确认禁用', btnText: '确认禁用', btnClass: 'btn-warning' });
}

async function enableApiKey(id) {
    try {
        await apiCall(`/keys/${id}/enable`, { method: 'POST' });
        showToast('API Key 已启用', 'success');
        loadApiKeys();
    } catch (error) {
        showToast('启用失败: ' + error.message, 'error');
    }
}

function deleteApiKey(id) {
    showConfirmModal(`确定要永久删除此 API Key 吗？此操作不可恢复！`, async () => {
        try {
            console.log('Deleting API Key:', id);
            const result = await apiCall(`/keys/${id}`, { method: 'DELETE' });
            console.log('Delete result:', result);
            showToast('API Key 已删除', 'success');
            // 清空缓存，强制重新加载
            apiKeysData = [];
            await loadApiKeys();
            console.log('API Keys reloaded');
        } catch (error) {
            console.error('Delete error:', error);
            showToast('删除失败: ' + error.message, 'error');
        }
    }, { title: '⚠️ 确认删除', btnText: '确认删除', btnClass: 'btn-danger' });
}

// ==================== 辅助函数 ====================

async function loadGroupsForSelect() {
    try {
        if (groupsData.length === 0) {
            const response = await apiCall('/groups');
            groupsData = response.groups || [];
        }
        
        // 更新账号管理页面的模型组下拉框
        const accountGroupSelect = document.getElementById('account-model-group');
        if (accountGroupSelect) {
            const currentValue = accountGroupSelect.value;
            accountGroupSelect.innerHTML = '<option value="">选择模型组</option>' + 
                groupsData.map(g => `<option value="${escapeHtml(g.name)}">${escapeHtml(g.name)}</option>`).join('');
            if (currentValue) accountGroupSelect.value = currentValue;
        }
        
        // 更新账号筛选的模型组下拉框
        const accountGroupFilter = document.getElementById('account-group-filter');
        if (accountGroupFilter) {
            const currentValue = accountGroupFilter.value;
            accountGroupFilter.innerHTML = '<option value="">所有模型组</option>' + 
                groupsData.map(g => `<option value="${escapeHtml(g.name)}">${escapeHtml(g.name)}</option>`).join('');
            if (currentValue) accountGroupFilter.value = currentValue;
        }
    } catch (error) {
        console.error('加载模型组失败:', error);
    }
}

// ==================== 模态框操作 ====================

function openModal(modalId) {
    document.getElementById(modalId).classList.add('active');
    document.body.style.overflow = 'hidden';
}

function closeModal(modalId) {
    document.getElementById(modalId).classList.remove('active');
    document.body.style.overflow = '';
}

function showConfirmModal(message, onConfirm, options = {}) {
    const title = options.title || '⚠️ 确认操作';
    const btnText = options.btnText || '确认';
    const btnClass = options.btnClass || 'btn-danger';
    
    document.getElementById('confirm-title').textContent = title;
    document.getElementById('confirm-message').textContent = message;
    const confirmBtn = document.getElementById('confirm-btn');
    
    // 移除旧的事件监听器
    const newConfirmBtn = confirmBtn.cloneNode(true);
    confirmBtn.parentNode.replaceChild(newConfirmBtn, confirmBtn);
    
    // 设置按钮样式和文字
    newConfirmBtn.textContent = btnText;
    newConfirmBtn.className = `btn ${btnClass}`;
    
    // 添加新的事件监听器
    newConfirmBtn.addEventListener('click', async () => {
        closeModal('confirm-modal');
        if (onConfirm) await onConfirm();
    });
    
    openModal('confirm-modal');
}

// ==================== Toast 提示 ====================

function showToast(message, type = 'info') {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.className = `toast ${type} show`;
    
    setTimeout(() => {
        toast.classList.remove('show');
    }, 3000);
}

// ==================== 一键复制功能 ====================

async function copyToClipboard(text, button) {
    try {
        await navigator.clipboard.writeText(text);
        
        // 显示复制成功反馈
        const originalText = button.textContent;
        button.textContent = '✓';
        button.classList.add('copied');
        
        setTimeout(() => {
            button.textContent = originalText;
            button.classList.remove('copied');
        }, 1500);
        
        showToast('已复制到剪贴板', 'success');
    } catch (error) {
        // 降级方案：使用传统方法
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        
        try {
            document.execCommand('copy');
            showToast('已复制到剪贴板', 'success');
        } catch (e) {
            showToast('复制失败，请手动复制', 'error');
        }
        
        document.body.removeChild(textarea);
    }
}

// ==================== 格式化函数 ====================

function formatNumber(num) {
    if (num === null || num === undefined) return '0';
    if (num >= 1000000) {
        return (num / 1000000).toFixed(1) + 'M';
    } else if (num >= 1000) {
        return (num / 1000).toFixed(1) + 'K';
    }
    return num.toString();
}

function formatDate(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleDateString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit'
    });
}

function formatTime(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleTimeString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit'
    });
}

function formatDateTime(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function getStatusText(status) {
    const statusMap = {
        'active': '正常',
        'exhausted': '耗尽',
        'disabled': '禁用',
        'revoked': '已撤销'
    };
    return statusMap[status] || status;
}

// ==================== 实时刷新（已移至仪表板的 monitorInterval）====================

// 页面可见性变化时控制自动刷新
document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        stopMonitorAutoSync();
    } else if (authToken && currentPage === 'dashboard') {
        startMonitorAutoSync();
    }
});

// ==================== 键盘快捷键 ====================

document.addEventListener('keydown', (e) => {
    // ESC 关闭模态框
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal-overlay.active').forEach(modal => {
            closeModal(modal.id);
        });
    }
});

// 点击模态框背景关闭
document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) {
            closeModal(overlay.id);
        }
    });
});
