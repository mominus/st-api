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
let callLogsData = [];
let adminMeta = {
    version: null
};

// ==================== 初始化 ====================

document.addEventListener('DOMContentLoaded', () => {
    // 检查登录状态
    if (authToken) {
        showAdminPage();
        loadAdminMeta();
        loadDashboard();
    } else {
        showLoginPage();
        setSidebarVersion(null);
    }
    
    // 绑定事件
    bindEvents();
});

function bindEvents() {
    // 登录表单
    document.getElementById('login-form').addEventListener('submit', handleLogin);
    
    // 退出登录
    document.getElementById('logout-btn').addEventListener('click', handleLogout);
    
    // 移动端菜单
    const mobileMenuBtn = document.getElementById('mobile-menu-btn');
    const sidebar = document.getElementById('sidebar');
    const sidebarOverlay = document.getElementById('sidebar-overlay');
    
    if (mobileMenuBtn) {
        mobileMenuBtn.addEventListener('click', () => {
            mobileMenuBtn.classList.toggle('active');
            sidebar.classList.toggle('active');
            sidebarOverlay.classList.toggle('active');
            document.body.style.overflow = sidebar.classList.contains('active') ? 'hidden' : '';
        });
    }
    
    if (sidebarOverlay) {
        sidebarOverlay.addEventListener('click', closeMobileMenu);
    }
    
    // 侧边栏导航
    document.querySelectorAll('.sidebar-nav a[data-page]').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            switchPage(link.dataset.page);
            closeMobileMenu();
        });
    });
    
    // 表单提交
    document.getElementById('account-form').addEventListener('submit', handleAccountSubmit);
    document.getElementById('group-form').addEventListener('submit', handleGroupSubmit);
    document.getElementById('apikey-form').addEventListener('submit', handleApiKeySubmit);
    document.getElementById('apikey-edit-form').addEventListener('submit', handleApiKeyEditSubmit);
    document.getElementById('delete-logs-form').addEventListener('submit', handleDeleteLogs);
    
    // 窗口大小变化时关闭移动菜单
    window.addEventListener('resize', () => {
        if (window.innerWidth > 768) {
            closeMobileMenu();
        }
    });
}

// 关闭移动端菜单
function closeMobileMenu() {
    const mobileMenuBtn = document.getElementById('mobile-menu-btn');
    const sidebar = document.getElementById('sidebar');
    const sidebarOverlay = document.getElementById('sidebar-overlay');
    
    if (mobileMenuBtn) mobileMenuBtn.classList.remove('active');
    if (sidebar) sidebar.classList.remove('active');
    if (sidebarOverlay) sidebarOverlay.classList.remove('active');
    document.body.style.overflow = '';
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
        await loadAdminMeta();
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
    adminMeta.version = null;
    setSidebarVersion(null);
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

function setSidebarVersion(version) {
    const versionEl = document.getElementById('sidebar-version');
    if (!versionEl) return;
    
    if (!version) {
        versionEl.textContent = '版本 -';
        return;
    }
    
    const versionText = String(version).trim();
    if (!versionText) {
        versionEl.textContent = '版本 -';
        return;
    }
    
    const normalized = versionText.startsWith('v') ? versionText : `v${versionText}`;
    versionEl.textContent = `版本 ${normalized}`;
}

async function loadAdminMeta() {
    try {
        const data = await apiCall('/meta');
        adminMeta.version = data.version || null;
        setSidebarVersion(adminMeta.version);
    } catch (error) {
        adminMeta.version = null;
        setSidebarVersion(null);
        console.error('加载后台元信息失败:', error);
    }
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
    
    // 停止性能监控自动刷新（如果离开性能页面）
    if (page !== 'performance') {
        stopPerformanceAutoRefresh();
    }
    
    // 加载页面数据（使用缓存避免重复加载）
    switch (page) {
        case 'dashboard':
            loadDashboard();
            break;
        case 'performance':
            loadPerformanceStats();
            startPerformanceAutoRefresh();
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
        case 'logs':
            loadCallLogs();
            break;
    }
}

// ==================== 仪表板/监控中心 ====================

// 存储账号的分析数据
let analyticsCache = {};
let monitorInterval = null;
let isSilentSyncRunning = false;
let isManualSyncRunning = false;
const DASHBOARD_AUTO_SYNC_INTERVAL_MS = 120000; // 2 分钟
const DASHBOARD_SYNC_WINDOW_SIZE = 120;
const DASHBOARD_SYNC_STALE_PRIORITY_SIZE = 80;
const AUTO_SYNC_MAX_CONCURRENCY = 10;
const MANUAL_SYNC_MAX_CONCURRENCY = 20;
let dashboardSyncCursor = (() => {
    const value = parseInt(localStorage.getItem('dashboardSyncCursor') || '0', 10);
    return Number.isInteger(value) && value >= 0 ? value : 0;
})();

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
            // 启动自动同步
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
        
        // 启动自动同步
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
    // 自动同步（默认每 2 分钟一次）
    monitorInterval = setInterval(() => {
        if (currentPage === 'dashboard' && !isSilentSyncRunning && !isManualSyncRunning) {
            syncAllAccountsSilent();
        }
    }, DASHBOARD_AUTO_SYNC_INTERVAL_MS);
}

function stopMonitorAutoSync() {
    if (monitorInterval) {
        clearInterval(monitorInterval);
        monitorInterval = null;
    }
}

function toTimestamp(value) {
    if (!value) return 0;
    const ts = Date.parse(value);
    return Number.isFinite(ts) ? ts : 0;
}

function buildSyncWindowAccountIds(accounts) {
    const eligible = (accounts || []).filter(account => account && account.has_private_key && account.id);
    if (eligible.length === 0) {
        return [];
    }

    if (eligible.length <= DASHBOARD_SYNC_WINDOW_SIZE) {
        return eligible.map(account => account.id);
    }

    const selectedIds = [];
    const seen = new Set();
    const addAccount = (account) => {
        if (!account || !account.id || seen.has(account.id)) {
            return false;
        }
        seen.add(account.id);
        selectedIds.push(account.id);
        return selectedIds.length >= DASHBOARD_SYNC_WINDOW_SIZE;
    };

    // 第一优先级：有调用但尚未同步的账号（last_used_at > last_sync_at）
    const usedButUnsynced = eligible
        .filter(account => {
            const usedTs = toTimestamp(account.last_used_at);
            if (!usedTs) return false;
            const syncTs = toTimestamp(account.last_sync_at);
            return !syncTs || usedTs > syncTs;
        })
        .sort((a, b) => toTimestamp(b.last_used_at) - toTimestamp(a.last_used_at));

    for (const account of usedButUnsynced) {
        if (addAccount(account)) {
            return selectedIds;
        }
    }

    // 第二优先级：最久未同步账号（保证不会长期饿死）
    const staleFirst = [...eligible].sort((a, b) => {
        const aSync = toTimestamp(a.last_sync_at);
        const bSync = toTimestamp(b.last_sync_at);
        if (!aSync && !bSync) return String(a.id).localeCompare(String(b.id));
        if (!aSync) return -1;
        if (!bSync) return 1;
        if (aSync !== bSync) return aSync - bSync;
        return String(a.id).localeCompare(String(b.id));
    });

    const staleTarget = Math.min(DASHBOARD_SYNC_WINDOW_SIZE, DASHBOARD_SYNC_STALE_PRIORITY_SIZE);
    for (const account of staleFirst) {
        if (selectedIds.length >= staleTarget) {
            break;
        }
        addAccount(account);
    }

    // 第三优先级：轮转补齐，覆盖全量账号
    const allSorted = [...eligible].sort((a, b) => String(a.id).localeCompare(String(b.id)));
    if (allSorted.length > 0) {
        dashboardSyncCursor = dashboardSyncCursor % allSorted.length;
    } else {
        dashboardSyncCursor = 0;
    }

    for (let i = 0; i < allSorted.length && selectedIds.length < DASHBOARD_SYNC_WINDOW_SIZE; i++) {
        const index = (dashboardSyncCursor + i) % allSorted.length;
        addAccount(allSorted[index]);
    }

    if (allSorted.length > 0) {
        dashboardSyncCursor = (dashboardSyncCursor + DASHBOARD_SYNC_WINDOW_SIZE) % allSorted.length;
        localStorage.setItem('dashboardSyncCursor', String(dashboardSyncCursor));
    }

    return selectedIds;
}

function applyBatchSyncResults(syncResults = []) {
    if (!Array.isArray(syncResults) || syncResults.length === 0) {
        return;
    }

    syncResults.forEach(result => {
        if (!result || result.status !== 'synced' || !result.account_id) {
            return;
        }

        const account = accountsData.find(a => a.id === result.account_id);
        if (account) {
            account.daily_used = result.current_used ?? account.daily_used;
            account.status = result.account_status || account.status;
            account.last_sync_at = result.last_sync_at || new Date().toISOString();
        }

        analyticsCache[result.account_id] = {
            ...analyticsCache[result.account_id],
            today_tokens: result.current_used ?? 0,
            today_runs: result.today_runs ?? 0,
            total_tokens: result.total_tokens ?? 0
        };
    });
}

async function batchSyncAccounts(accountIds, options = {}) {
    const payload = {
        days: options.days || 30,
        max_concurrency: options.maxConcurrency || AUTO_SYNC_MAX_CONCURRENCY
    };

    if (Array.isArray(accountIds) && accountIds.length > 0) {
        payload.account_ids = accountIds;
    }

    const response = await apiCall('/accounts/batch/sync', {
        method: 'POST',
        body: JSON.stringify(payload)
    });

    applyBatchSyncResults(response.results || []);
    return response;
}

// 静默同步（不显示提示，使用缓存的账号数据）
async function syncAllAccountsSilent() {
    if (isSilentSyncRunning || isManualSyncRunning) {
        return;
    }

    // 使用窗口机制同步，优先有调用未同步和久未同步账号
    const windowAccountIds = buildSyncWindowAccountIds(accountsData);
    if (windowAccountIds.length === 0) return;

    isSilentSyncRunning = true;
    try {
        await batchSyncAccounts(
            windowAccountIds,
            { days: 30, maxConcurrency: AUTO_SYNC_MAX_CONCURRENCY }
        );

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
    } finally {
        isSilentSyncRunning = false;
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
        if (accountsWithKey.length > 0) {
            try {
                const initialWindowIds = buildSyncWindowAccountIds(accountsWithKey);
                await batchSyncAccounts(
                    initialWindowIds,
                    { days: 30, maxConcurrency: MANUAL_SYNC_MAX_CONCURRENCY }
                );
            } catch (e) {
                console.error('批量同步账号失败:', e);
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
        
        // 今日费用
        const todayCost = overview.today?.cost || {};
        const todayCostEl = document.getElementById('stat-today-tokens-cost');
        if (todayCostEl) {
            todayCostEl.innerHTML = `<span class="cost-value">$${parseFloat(todayCost.total || 0).toFixed(3)}</span>`;
        }
        
        // 更新历史累计统计
        const allTime = overview.all_time || {};
        document.getElementById('stat-all-time-requests').textContent = formatNumber(allTime.requests || 0);
        document.getElementById('stat-all-time-tokens').textContent = formatNumber(allTime.total_tokens || 0);
        document.getElementById('stat-all-time-input').textContent = formatNumber(allTime.input_tokens || 0);
        document.getElementById('stat-all-time-output').textContent = formatNumber(allTime.output_tokens || 0);
        
        // 历史费用
        const allTimeCost = allTime.cost || {};
        const allTimeCostEl = document.getElementById('stat-all-time-tokens-cost');
        if (allTimeCostEl) {
            allTimeCostEl.innerHTML = `<span class="cost-value">$${parseFloat(allTimeCost.total || 0).toFixed(3)}</span>`;
        }
        const inputCostEl = document.getElementById('stat-all-time-input-cost');
        if (inputCostEl) {
            inputCostEl.innerHTML = `<span class="cost-value">$${parseFloat(allTimeCost.input || 0).toFixed(3)}</span>`;
        }
        const outputCostEl = document.getElementById('stat-all-time-output-cost');
        if (outputCostEl) {
            outputCostEl.innerHTML = `<span class="cost-value">$${parseFloat(allTimeCost.output || 0).toFixed(3)}</span>`;
        }
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
        org.model_groups.push(...getAccountModels(account));
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
    if (isManualSyncRunning || isSilentSyncRunning) {
        showToast('正在同步中，请稍候', 'warning');
        return;
    }

    const btn = document.getElementById('sync-all-btn');
    const icon = btn.querySelector('.sync-icon');
    btn.disabled = true;
    if (icon) icon.classList.add('spinning');
    isManualSyncRunning = true;
    
    try {
        const response = await apiCall('/accounts');
        accountsData = response.accounts || [];
        const accounts = accountsData.filter(a => a.has_private_key);
        
        if (accounts.length === 0) {
            showToast('没有配置 Private API Key 的账号', 'warning');
            return;
        }

        const syncResponse = await batchSyncAccounts(
            accounts.map(account => account.id),
            { days: 30, maxConcurrency: MANUAL_SYNC_MAX_CONCURRENCY }
        );

        // 重新渲染表格和统计
        renderMonitorTable(accountsData);
        updateLastSyncTime();

        const uniqueOrgIds = new Set(accountsData.filter(a => a.status === 'active').map(a => a.org_id));
        let totalTodayTokens = 0;
        accountsData.forEach(account => {
            const analytics = analyticsCache[account.id];
            totalTodayTokens += analytics?.today_tokens ?? account.daily_used;
        });
        await updateDashboardStats(uniqueOrgIds.size, totalTodayTokens);

        const syncedCount = syncResponse.synced_count || 0;
        const failedCount = syncResponse.failed_count || 0;
        const skippedCount = syncResponse.skipped_count || 0;
        showToast(
            `同步完成: 成功 ${syncedCount}，失败 ${failedCount}，跳过 ${skippedCount}`,
            failedCount > 0 ? 'warning' : 'success'
        );
    } catch (error) {
        showToast('同步失败: ' + error.message, 'error');
    } finally {
        isManualSyncRunning = false;
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

function getAccountModels(account) {
    if (Array.isArray(account?.model_groups) && account.model_groups.length > 0) {
        return account.model_groups.filter(Boolean);
    }
    if (account?.model_group) {
        return [account.model_group];
    }
    return [];
}

function getFilteredAccounts() {
    const searchTerm = document.getElementById('account-search').value.toLowerCase().trim();
    const groupFilter = document.getElementById('account-group-filter').value;
    const statusFilter = document.getElementById('account-status-filter').value;
    
    return accountsData.filter(account => {
        const accountModels = getAccountModels(account);
        if (searchTerm && !account.name.toLowerCase().includes(searchTerm)) return false;
        if (groupFilter && !accountModels.includes(groupFilter)) return false;
        if (statusFilter && account.status !== statusFilter) return false;
        return true;
    });
}

function updateAccountBulkActionState(filteredAccounts) {
    const visibleCount = filteredAccounts.length;
    const totalCount = accountsData.length;
    const enableCandidates = filteredAccounts.filter(account => account.status === 'disabled').length;
    const disableCandidates = filteredAccounts.filter(account => account.status !== 'disabled').length;
    const deleteCandidates = visibleCount;
    
    const summaryEl = document.getElementById('account-filter-summary');
    const enableBtn = document.getElementById('bulk-enable-btn');
    const disableBtn = document.getElementById('bulk-disable-btn');
    const deleteBtn = document.getElementById('bulk-delete-btn');
    
    if (summaryEl) {
        summaryEl.textContent = `当前显示 ${visibleCount} / ${totalCount} 个账号`;
    }
    
    if (enableBtn) {
        enableBtn.disabled = enableCandidates === 0;
        enableBtn.title = enableCandidates > 0 ? `启用当前筛选中的 ${enableCandidates} 个禁用账号` : '当前筛选结果中没有可启用账号';
    }
    
    if (disableBtn) {
        disableBtn.disabled = disableCandidates === 0;
        disableBtn.title = disableCandidates > 0 ? `禁用当前筛选中的 ${disableCandidates} 个账号` : '当前筛选结果中没有可禁用账号';
    }
    
    if (deleteBtn) {
        deleteBtn.disabled = deleteCandidates === 0;
        deleteBtn.title = deleteCandidates > 0 ? `删除当前筛选中的 ${deleteCandidates} 个账号` : '当前没有可删除账号';
    }
}

function renderAccountsTable() {
    const tbody = document.getElementById('accounts-table-body');
    const filtered = getFilteredAccounts();
    updateAccountBulkActionState(filtered);
    
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
        const accountModels = getAccountModels(account);
        const modelTags = accountModels
            .map(model => `<span class="model-group-tag">${escapeHtml(model)}</span>`)
            .join('');
        
        return `
            <tr data-account-id="${account.id}">
                <td>
                    <div class="account-name-cell">
                        ${escapeHtml(account.name)}
                        <span class="account-org">${escapeHtml(account.org_id?.substring(0, 8) || '')}...</span>
                    </div>
                </td>
                <td>
                    ${modelTags}
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

async function bulkSetAccountStatus(newStatus) {
    const actionText = newStatus === 'disabled' ? '禁用' : '启用';
    const filteredAccounts = getFilteredAccounts();
    const targetAccounts = filteredAccounts.filter(account => (
        newStatus === 'active' ? account.status === 'disabled' : account.status !== 'disabled'
    ));
    
    if (targetAccounts.length === 0) {
        showToast(`当前筛选结果中没有可${actionText}账号`, 'warning');
        return;
    }
    
    const confirmMsg = newStatus === 'disabled'
        ? `确定要一键禁用当前筛选结果中的 ${targetAccounts.length} 个账号吗？\n\n禁用后，这些账号的工作流将不会被 API 调用选中。`
        : `确定要一键启用当前筛选结果中的 ${targetAccounts.length} 个账号吗？`;
    
    showConfirmModal(confirmMsg, async () => {
        try {
            const response = await apiCall('/accounts/batch/status', {
                method: 'POST',
                body: JSON.stringify({
                    account_ids: targetAccounts.map(account => account.id),
                    status: newStatus
                })
            });

            const successCount = response.updated_count || 0;
            const failureCount = response.not_found_count || 0;
            if (successCount > 0) {
                showToast(
                    `已${actionText} ${successCount} 个账号${failureCount > 0 ? `，失败 ${failureCount} 个` : ''}`,
                    failureCount > 0 ? 'warning' : 'success'
                );
            } else {
                showToast(`${actionText}失败`, 'error');
            }
            await loadAccounts();
        } catch (error) {
            console.error(`批量${actionText}失败:`, error);
            showToast(`${actionText}失败: ` + error.message, 'error');
        }
    });
}

function bulkDeleteAccounts() {
    const filteredAccounts = getFilteredAccounts();
    
    if (filteredAccounts.length === 0) {
        showToast('当前筛选结果中没有可删除账号', 'warning');
        return;
    }
    
    showConfirmModal(
        `确定要一键删除当前筛选结果中的 ${filteredAccounts.length} 个账号吗？\n\n此操作不可恢复。`,
        async () => {
            try {
                const response = await apiCall('/accounts/batch/delete', {
                    method: 'POST',
                    body: JSON.stringify({
                        account_ids: filteredAccounts.map(account => account.id)
                    })
                });

                const successCount = response.deleted_count || 0;
                const failureCount = response.not_found_count || 0;
                if (successCount > 0) {
                    showToast(
                        `已删除 ${successCount} 个账号${failureCount > 0 ? `，失败 ${failureCount} 个` : ''}`,
                        failureCount > 0 ? 'warning' : 'success'
                    );
                } else {
                    showToast('删除失败', 'error');
                }
                await loadAccounts();
            } catch (error) {
                console.error('批量删除账号失败:', error);
                showToast('删除失败: ' + error.message, 'error');
            }
        }
    );
}

// ==================== 批量导入账号 ====================

let importData = []; // 存储待导入的数据

function parseImportJsonData(rawText) {
    const jsonText = (rawText || '').replace(/^\uFEFF/, '').trim();
    if (!jsonText) {
        return [];
    }
    
    try {
        const parsed = JSON.parse(jsonText);
        return Array.isArray(parsed) ? parsed : [parsed];
    } catch (directError) {
        // 兼容多个对象连续粘贴但未包裹 []
        try {
            const wrapped = JSON.parse(`[${jsonText}]`);
            return Array.isArray(wrapped) ? wrapped : [wrapped];
        } catch (wrappedError) {
            const records = [];
            let cursor = 0;
            
            while (cursor < jsonText.length) {
                while (cursor < jsonText.length && /[\s,]/.test(jsonText[cursor])) {
                    cursor++;
                }
                
                if (cursor >= jsonText.length) {
                    break;
                }
                
                if (jsonText[cursor] !== '{') {
                    throw new Error(directError.message);
                }
                
                let depth = 0;
                let inString = false;
                let escaped = false;
                let end = cursor;
                
                for (; end < jsonText.length; end++) {
                    const ch = jsonText[end];
                    if (inString) {
                        if (escaped) {
                            escaped = false;
                        } else if (ch === '\\') {
                            escaped = true;
                        } else if (ch === '"') {
                            inString = false;
                        }
                        continue;
                    }
                    
                    if (ch === '"') {
                        inString = true;
                    } else if (ch === '{') {
                        depth++;
                    } else if (ch === '}') {
                        depth--;
                        if (depth === 0) {
                            end++;
                            break;
                        }
                    }
                }
                
                if (depth !== 0) {
                    throw new Error(directError.message);
                }
                
                const chunk = jsonText.slice(cursor, end).trim();
                records.push(JSON.parse(chunk));
                cursor = end;
            }
            
            if (records.length === 0) {
                throw new Error(directError.message);
            }
            
            return records;
        }
    }
}

function normalizeClaudeModelName(rawName) {
    const lowered = String(rawName || '').trim().toLowerCase();
    if (!lowered) {
        return '';
    }
    
    let parts = lowered.split(/[^a-z0-9]+/).filter(Boolean);
    if (parts.length === 0) {
        return lowered;
    }
    
    // 兼容导入值中带厂商前缀，例如: anthropic-claude_4_6_opus
    if (parts[0] === 'anthropic') {
        parts = parts.slice(1);
    }
    
    if (parts.length === 0 || parts[0] !== 'claude') {
        return lowered;
    }
    
    const supportedSeries = ['opus', 'sonnet', 'haiku'];
    const series = parts.find(part => supportedSeries.includes(part));
    if (!series) {
        return lowered;
    }
    
    const versionParts = parts
        .filter(part => /^\d+$/.test(part))
        .slice(0, 2);
    if (versionParts.length < 2) {
        return lowered;
    }
    
    return `claude-${series}-${versionParts[0]}-${versionParts[1]}`;
}

function normalizeImportedModelNames(llmModels) {
    if (llmModels === null || llmModels === undefined) {
        return [];
    }
    
    let modelCandidates = [];
    
    if (Array.isArray(llmModels)) {
        modelCandidates = llmModels;
    } else if (typeof llmModels === 'string') {
        const text = llmModels.trim();
        if (!text) {
            return [];
        }
        
        if (text.startsWith('[') && text.endsWith(']')) {
            try {
                const parsed = JSON.parse(text);
                modelCandidates = Array.isArray(parsed) ? parsed : [parsed];
            } catch (e) {
                modelCandidates = text.split(/[\n,]/);
            }
        } else {
            modelCandidates = text.split(/[\n,]/);
        }
    } else {
        modelCandidates = [llmModels];
    }
    
    const normalized = [];
    const seen = new Set();
    
    modelCandidates.forEach(candidate => {
        let rawName = '';
        
        if (typeof candidate === 'string' || typeof candidate === 'number') {
            rawName = String(candidate);
        } else if (candidate && typeof candidate === 'object') {
            rawName = String(candidate.model || candidate.name || candidate.id || '');
        }
        
        const name = normalizeClaudeModelName(rawName);
        if (!name || seen.has(name)) {
            return;
        }
        
        seen.add(name);
        normalized.push(name);
    });
    
    return normalized;
}

function resolveImportModelGroups(item, defaultModelGroup, index) {
    const groupsFromItem = normalizeImportedModelNames(item.llm_models);
    if (groupsFromItem.length > 0) {
        return groupsFromItem;
    }
    
    if (defaultModelGroup) {
        return [defaultModelGroup];
    }
    
    throw new Error(`第 ${index + 1} 条记录缺少 llm_models，且未选择默认模型组`);
}

function buildImportAccountKey(orgId, flowId) {
    return `${orgId}:::${flowId}`;
}

function showImportAccountsModal() {
    // 重置表单
    document.getElementById('import-accounts-form').reset();
    document.getElementById('import-json-text').value = '';
    document.getElementById('import-preview').classList.add('hidden');
    document.getElementById('import-submit-btn').disabled = true;
    importData = [];
    
    // 重置文件上传
    clearImportFile();
    
    // 重置标签页
    document.querySelectorAll('.import-tab').forEach(tab => {
        tab.classList.remove('active');
    });
    document.querySelector('.import-tab[data-tab="json"]').classList.add('active');
    document.getElementById('import-tab-json').classList.remove('hidden');
    document.getElementById('import-tab-file').classList.add('hidden');
    
    // 加载模型组选项
    loadGroupsForImportSelect();
    
    // 绑定标签页切换
    document.querySelectorAll('.import-tab').forEach(tab => {
        tab.onclick = () => switchImportTab(tab.dataset.tab);
    });
    
    // 绑定文件上传
    setupFileUpload();
    
    // 绑定表单提交
    document.getElementById('import-accounts-form').onsubmit = handleImportSubmit;
    
    openModal('import-accounts-modal');
}

function switchImportTab(tab) {
    document.querySelectorAll('.import-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.tab === tab);
    });
    document.getElementById('import-tab-json').classList.toggle('hidden', tab !== 'json');
    document.getElementById('import-tab-file').classList.toggle('hidden', tab !== 'file');
    
    // 切换时清除预览
    document.getElementById('import-preview').classList.add('hidden');
    document.getElementById('import-submit-btn').disabled = true;
    importData = [];
}

async function loadGroupsForImportSelect() {
    try {
        if (groupsData.length === 0) {
            const data = await apiCall('/groups');
            groupsData = data.groups || [];
        }
        
        const select = document.getElementById('import-model-group');
        select.innerHTML = '<option value="">选择模型组</option>';
        groupsData.forEach(group => {
            if (!group || !group.name) {
                return;
            }
            const option = document.createElement('option');
            option.value = group.name;
            option.textContent = group.name;
            select.appendChild(option);
        });
    } catch (error) {
        console.error('加载模型组失败:', error);
    }
}

function setupFileUpload() {
    const fileInput = document.getElementById('import-file');
    const uploadArea = document.getElementById('file-upload-area');
    
    // 点击上传
    uploadArea.onclick = (e) => {
        if (e.target.tagName !== 'BUTTON') {
            fileInput.click();
        }
    };
    
    // 文件选择
    fileInput.onchange = (e) => {
        if (e.target.files.length > 0) {
            handleFileSelect(e.target.files[0]);
        }
    };
    
    // 拖拽上传
    uploadArea.ondragover = (e) => {
        e.preventDefault();
        uploadArea.classList.add('dragover');
    };
    
    uploadArea.ondragleave = () => {
        uploadArea.classList.remove('dragover');
    };
    
    uploadArea.ondrop = (e) => {
        e.preventDefault();
        uploadArea.classList.remove('dragover');
        if (e.dataTransfer.files.length > 0) {
            handleFileSelect(e.dataTransfer.files[0]);
        }
    };
}

function handleFileSelect(file) {
    const uploadArea = document.getElementById('file-upload-area');
    const placeholder = uploadArea.querySelector('.file-upload-placeholder');
    const selected = uploadArea.querySelector('.file-selected');
    
    document.getElementById('selected-file-name').textContent = file.name;
    placeholder.classList.add('hidden');
    selected.classList.remove('hidden');
    
    // 读取文件内容
    const reader = new FileReader();
    reader.onload = (e) => {
        document.getElementById('import-json-text').value = e.target.result;
    };
    reader.readAsText(file);
}

function clearImportFile() {
    const fileInput = document.getElementById('import-file');
    const uploadArea = document.getElementById('file-upload-area');
    
    if (fileInput) fileInput.value = '';
    
    if (uploadArea) {
        const placeholder = uploadArea.querySelector('.file-upload-placeholder');
        const selected = uploadArea.querySelector('.file-selected');
        if (placeholder) placeholder.classList.remove('hidden');
        if (selected) selected.classList.add('hidden');
    }
}

function previewImport() {
    const jsonText = document.getElementById('import-json-text').value.trim();
    const defaultModelGroup = document.getElementById('import-model-group').value.trim();
    
    if (!jsonText) {
        showToast('请输入或上传 JSON 数据', 'warning');
        return;
    }
    
    try {
        const parsed = parseImportJsonData(jsonText);
        
        if (parsed.length === 0) {
            showToast('JSON 数据为空', 'warning');
            return;
        }
        
        const existingAccountKeys = new Set(
            accountsData.map(account => buildImportAccountKey(account.org_id, account.flow_id))
        );
        const pendingAccountKeys = new Set();
        
        // 验证并转换数据（每条记录导入为一个账号，可绑定多个模型）
        const normalizedImportData = [];
        
        parsed.forEach((item, index) => {
            if (!item || typeof item !== 'object' || Array.isArray(item)) {
                throw new Error(`第 ${index + 1} 条记录不是有效对象`);
            }
            
            const orgId = String(item.org_id || '').trim();
            const flowId = String(item.flow_id || '').trim();
            const apiKey = String(item.public_api_key || item.api_key || '').trim();
            
            if (!orgId) {
                throw new Error(`第 ${index + 1} 条记录缺少 org_id`);
            }
            if (!flowId) {
                throw new Error(`第 ${index + 1} 条记录缺少 flow_id`);
            }
            if (!apiKey) {
                throw new Error(`第 ${index + 1} 条记录缺少 public_api_key 或 api_key`);
            }
            
            const modelGroups = resolveImportModelGroups(item, defaultModelGroup, index);
            const email = typeof item.email === 'string' ? item.email.trim() : '';
            const customName = typeof item.name === 'string' ? item.name.trim() : '';
            const baseName = email || customName || `账号_${orgId.substring(0, 8)}`;
            const privateApiKey = item.private_api_key ? String(item.private_api_key).trim() : null;
            
            const accountKey = buildImportAccountKey(orgId, flowId);
            const isDuplicate = existingAccountKeys.has(accountKey) || pendingAccountKeys.has(accountKey);
            pendingAccountKeys.add(accountKey);
            
            normalizedImportData.push({
                name: baseName,
                org_id: orgId,
                flow_id: flowId,
                api_key: apiKey,
                private_api_key: privateApiKey || null,
                email: email || null,
                model_groups: modelGroups,
                model_group: modelGroups[0], // 兼容显示与后端旧字段
                isDuplicate: isDuplicate,
                status: isDuplicate ? 'duplicate' : 'pending'
            });
        });
        
        importData = normalizedImportData;
        
        if (importData.length === 0) {
            showToast('没有可导入的数据', 'warning');
            return;
        }
        
        // 渲染预览
        renderImportPreview();
        
    } catch (error) {
        showToast('JSON 解析失败: ' + error.message, 'error');
        importData = [];
    }
}

function renderImportPreview() {
    const previewEl = document.getElementById('import-preview');
    const listEl = document.getElementById('import-preview-list');
    const countEl = document.getElementById('import-count');
    const submitBtn = document.getElementById('import-submit-btn');
    
    const validCount = importData.filter(d => !d.isDuplicate).length;
    const duplicateCount = importData.filter(d => d.isDuplicate).length;
    
    countEl.textContent = `${validCount} 条有效`;
    if (duplicateCount > 0) {
        countEl.textContent += `，${duplicateCount} 条重复`;
    }
    
    listEl.innerHTML = importData.map((item, index) => `
        <div class="import-preview-item ${item.isDuplicate ? 'duplicate' : ''}">
            <div class="item-info">
                <span class="item-name">${escapeHtml(item.name)}</span>
                <span class="item-detail">models: ${escapeHtml((item.model_groups || [item.model_group]).join(', '))} | org: ${item.org_id.substring(0, 8)}... | flow: ${item.flow_id.substring(0, 8)}...</span>
            </div>
            <span class="item-status ${item.status}" id="import-status-${index}">
                ${item.isDuplicate ? '⚠️ 已存在' : '⏳ 待导入'}
            </span>
        </div>
    `).join('');
    
    previewEl.classList.remove('hidden');
    submitBtn.disabled = validCount === 0;
}

async function handleImportSubmit(e) {
    e.preventDefault();
    
    const dailyQuota = parseInt(document.getElementById('import-daily-quota').value) || 1000000;
    const submitBtn = document.getElementById('import-submit-btn');
    
    // 过滤掉重复的，并保留原始索引
    const toImportEntries = importData
        .map((item, index) => ({ item, index }))
        .filter(entry => !entry.item.isDuplicate);
    
    if (toImportEntries.length === 0) {
        showToast('没有可导入的数据', 'warning');
        return;
    }
    
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<span class="spinner-sm"></span> 导入中...';

    try {
        const response = await apiCall('/accounts/batch/import', {
            method: 'POST',
            body: JSON.stringify({
                daily_quota: dailyQuota,
                skip_existing: true,
                auto_create_groups: true,
                accounts: toImportEntries.map(({ item }) => ({
                    name: item.name,
                    org_id: item.org_id,
                    flow_id: item.flow_id,
                    api_key: item.api_key,
                    private_api_key: item.private_api_key,
                    model_group: item.model_group,
                    model_groups: item.model_groups || [item.model_group],
                    daily_quota: dailyQuota
                }))
            })
        });

        const backendResults = Array.isArray(response.results) ? response.results : [];
        backendResults.forEach(result => {
            const requestIndex = Number(result.index);
            if (!Number.isInteger(requestIndex) || requestIndex < 0 || requestIndex >= toImportEntries.length) {
                return;
            }

            const targetEntry = toImportEntries[requestIndex];
            const item = targetEntry.item;
            const statusEl = document.getElementById(`import-status-${targetEntry.index}`);

            if (result.status === 'created') {
                item.status = 'success';
                if (statusEl) {
                    statusEl.innerHTML = '✅ 成功';
                    statusEl.className = 'item-status success';
                }
                return;
            }

            if (result.status === 'skipped_existing' || result.status === 'skipped_duplicate') {
                item.status = 'duplicate';
                if (statusEl) {
                    statusEl.innerHTML = '⚠️ 跳过';
                    statusEl.className = 'item-status duplicate';
                }
                return;
            }

            item.status = 'error';
            if (statusEl) {
                statusEl.innerHTML = '❌ 失败';
                statusEl.className = 'item-status error';
            }
        });

        const successCount = response.created_count || 0;
        const skipCount = (response.skipped_existing_count || 0) + (response.skipped_duplicate_count || 0);
        const failCount = response.failed_count || 0;
        const createdGroupCount = response.created_group_count || 0;

        if (createdGroupCount > 0) {
            groupsData = [];
            await loadGroupsForImportSelect();
            await loadGroupsForSelect();
        }

        const createGroupTips = createdGroupCount > 0 ? `，自动创建模型组 ${createdGroupCount} 个` : '';
        if (failCount === 0) {
            const skipTips = skipCount > 0 ? `，跳过 ${skipCount} 个` : '';
            showToast(`成功导入 ${successCount} 个账号${skipTips}${createGroupTips}`, 'success');
            closeModal('import-accounts-modal');
        } else {
            showToast(`导入完成: ${successCount} 成功, ${failCount} 失败${skipCount > 0 ? `，跳过 ${skipCount}` : ''}${createGroupTips}`, 'warning');
        }
        await loadAccounts();
    } catch (error) {
        console.error('批量导入失败:', error);
        showToast('导入失败: ' + error.message, 'error');
    } finally {
        submitBtn.innerHTML = '导入';
        submitBtn.disabled = false;
    }
}

// HTML 转义
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
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
    
    const modelSelect = document.getElementById('account-model-group');
    const selectedModels = getAccountModels(account);
    Array.from(modelSelect.options).forEach(option => {
        option.selected = selectedModels.includes(option.value);
    });
    document.getElementById('account-daily-quota').value = account.daily_quota;
    
    // Private API Key
    const privateKeyInput = document.getElementById('account-private-key');
    privateKeyInput.value = '';
    privateKeyInput.placeholder = account.has_private_key ? '已配置，留空保持不变' : '未配置';
    
    openModal('account-modal');
}

async function handleAccountSubmit(e) {
    e.preventDefault();
    
    const selectedModelGroups = Array.from(
        document.getElementById('account-model-group').selectedOptions || []
    )
        .map(option => option.value)
        .filter(Boolean);

    if (selectedModelGroups.length === 0) {
        showToast('请至少选择一个模型组', 'warning');
        return;
    }

    const id = document.getElementById('account-id').value;
    const data = {
        name: document.getElementById('account-name').value,
        org_id: document.getElementById('account-org-id').value,
        flow_id: document.getElementById('account-flow-id').value,
        model_group: selectedModelGroups[0],
        model_groups: selectedModelGroups,
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
            modelGroup: getAccountModels(account).join(', '),
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
            modelGroup: getAccountModels(account).join(', '),
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
    const modelTags = getAccountModels(account)
        .map(model => `<span class="model-group-tag">${escapeHtml(model)}</span>`)
        .join('');
    modelGroupCell.innerHTML = `${modelTags}${testStatus}`;
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
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">暂无数据</td></tr>';
        return;
    }
    
    tbody.innerHTML = groupsData.map(group => {
        // 计价显示
        const inputPrice = group.pricing?.input || 0;
        const outputPrice = group.pricing?.output || 0;
        const pricingDisplay = `<span class="pricing-input">入: $${inputPrice}</span> / <span class="pricing-output">出: $${outputPrice}</span>`;
        
        // 可用额度显示
        const available = group.quota?.available || 0;
        const total = group.quota?.total || 0;
        const used = group.quota?.used || 0;
        const usagePercent = total > 0 ? Math.round((used / total) * 100) : 0;
        
        // 计算可用额度对应的费用（按输出价格估算）
        const availableCost = (available / 1000000) * outputPrice;
        
        return `
        <tr>
            <td><strong>${escapeHtml(group.name)}</strong></td>
            <td>${escapeHtml(group.description || '-')}</td>
            <td class="pricing-cell">${pricingDisplay}</td>
            <td>
                <div class="quota-display">
                    <span class="quota-tokens">${formatNumber(available)} Token</span>
                    <span class="quota-cost">≈ $${availableCost.toFixed(3)}</span>
                    <div class="usage-bar-mini" style="margin-top: 4px;">
                        <div class="usage-fill ${usagePercent >= 80 ? 'text-warning' : 'text-success'}" style="width: ${Math.min(usagePercent, 100)}%"></div>
                    </div>
                </div>
            </td>
            <td>${group.active_account_count || 0} / ${group.account_count || 0}</td>
            <td class="actions">
                <button class="btn btn-sm btn-secondary" onclick="editGroup('${group.id}')">编辑</button>
                <button class="btn btn-sm btn-danger" onclick="deleteGroup('${group.id}')">删除</button>
            </td>
        </tr>
    `}).join('');
}

function showAddGroupModal() {
    document.getElementById('group-modal-title').textContent = '添加模型组';
    document.getElementById('group-form').reset();
    document.getElementById('group-id').value = '';
    document.getElementById('group-name').disabled = false; // 新建时启用名称输入
    document.getElementById('group-input-mapping').value = '{"user_input": "in-0", "model_id": "in-1"}';
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
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">暂无数据</td></tr>';
        return;
    }
    
    tbody.innerHTML = filtered.map(key => {
        const statusClass = key.status === 'active' ? 'status-active' : 
                           (key.status === 'exhausted' ? 'status-exhausted' : 'status-revoked');
        const statusText = key.status === 'active' ? '有效' : 
                          (key.status === 'exhausted' ? '已耗尽' : '已禁用');
        const groups = Array.isArray(key.model_groups) ? key.model_groups : [key.model_groups];
        
        // 限制类型和显示
        const limitInfo = getKeyLimitInfo(key);
        
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
                <td>
                    <div class="limit-info">
                        <span class="limit-type-badge ${limitInfo.class}">${limitInfo.type}</span>
                        <span class="usage-inline">${limitInfo.display}</span>
                    </div>
                </td>
                <td><span class="status-badge ${statusClass}">${statusText}</span></td>
                <td class="actions">
                    <button class="btn btn-sm btn-info" onclick="showApiKeyDetail('${key.id}')">详情</button>
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

// 获取 API Key 限制信息
function getKeyLimitInfo(key) {
    // 优先级：费用限制 > Token限制 > 请求数限制
    if (key.cost_limit) {
        const used = parseFloat(key.total_cost || 0);
        const limit = parseFloat(key.cost_limit);
        const percent = limit > 0 ? Math.round((used / limit) * 100) : 0;
        return {
            type: '费用',
            class: 'limit-cost',
            display: `$${used.toFixed(2)} / $${limit.toFixed(2)} (${percent}%)`
        };
    } else if (key.token_quota) {
        const used = key.total_tokens || 0;
        const limit = key.token_quota;
        const percent = limit > 0 ? Math.round((used / limit) * 100) : 0;
        return {
            type: 'Token',
            class: 'limit-token',
            display: `${formatNumber(used)} / ${formatNumber(limit)} (${percent}%)`
        };
    } else if (key.request_quota) {
        const used = key.total_requests || 0;
        const limit = key.request_quota;
        const percent = limit > 0 ? Math.round((used / limit) * 100) : 0;
        return {
            type: '请求',
            class: 'limit-request',
            display: `${formatNumber(used)} / ${formatNumber(limit)} (${percent}%)`
        };
    } else {
        return {
            type: '无限制',
            class: 'limit-none',
            display: `${formatNumber(key.total_requests || 0)} 次请求`
        };
    }
}

function getKeyModelStatusMeta(status) {
    if (status === 'active') {
        return { text: '可用', className: 'status-active' };
    }
    if (status === 'exhausted') {
        return { text: '已耗尽', className: 'status-exhausted' };
    }
    if (status === 'disabled' || status === 'revoked') {
        return { text: '已禁用', className: 'status-revoked' };
    }
    if (status === 'expired') {
        return { text: '已过期', className: 'status-revoked' };
    }
    return { text: '不可用', className: 'status-exhausted' };
}

function formatUnavailableReasons(reasons) {
    if (!Array.isArray(reasons) || reasons.length === 0) {
        return '无';
    }
    return reasons.map(reason => escapeHtml(reason)).join(' / ');
}

function renderPerModelUsageSection(items) {
    if (!Array.isArray(items) || items.length === 0) {
        return '';
    }

    return `
        <div class="detail-section">
            <h4>🧩 按模型使用情况</h4>
            <div style="display: grid; gap: 12px;">
                ${items.map(item => {
                    const statusMeta = getKeyModelStatusMeta(item.status);
                    return `
                        <div class="detail-item" style="display: block;">
                            <div style="display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 8px; flex-wrap: wrap;">
                                <span class="detail-value" style="font-weight: 700;">${escapeHtml(item.model || '-')}</span>
                                <span class="status-badge ${statusMeta.className}">${statusMeta.text}</span>
                            </div>
                            <div class="detail-grid" style="margin-top: 0;">
                                <div class="detail-item">
                                    <span class="detail-label">不可用原因</span>
                                    <span class="detail-value">${formatUnavailableReasons(item.unavailable_reasons)}</span>
                                </div>
                                <div class="detail-item">
                                    <span class="detail-label">当前已用量</span>
                                    <span class="detail-value">${escapeHtml(item.current_usage || '0 tokens')}</span>
                                </div>
                                <div class="detail-item">
                                    <span class="detail-label">可用额度</span>
                                    <span class="detail-value">${escapeHtml(item.available_tokens || '0 tokens')}</span>
                                </div>
                            </div>
                        </div>
                    `;
                }).join('')}
            </div>
        </div>
    `;
}

// 显示 API Key 详情
async function showApiKeyDetail(id) {
    const content = document.getElementById('apikey-detail-content');
    content.innerHTML = '<div class="text-center text-muted" style="padding: 24px 0;">加载中...</div>';
    openModal('apikey-detail-modal');

    try {
        const response = await apiCall(`/keys/${id}`);
        const key = response.key;
        const groups = Array.isArray(key.model_groups) ? key.model_groups : [key.model_groups];
        const createdAt = key.created_at ? new Date(key.created_at).toLocaleString('zh-CN') : '-';
        const expiresAt = key.expires_at ? new Date(key.expires_at).toLocaleString('zh-CN') : '永不过期';
        const lastUsedAt = key.last_used_at ? new Date(key.last_used_at).toLocaleString('zh-CN') : '从未使用';
        const keyStatusMeta = getKeyModelStatusMeta(key.status);
        
        content.innerHTML = `
            <div class="detail-section">
                <h4>📌 基本信息</h4>
                <div class="detail-grid">
                    <div class="detail-item">
                        <span class="detail-label">名称</span>
                        <span class="detail-value">${escapeHtml(key.name || '未命名')}</span>
                    </div>
                    <div class="detail-item">
                        <span class="detail-label">Key 前缀</span>
                        <span class="detail-value">${escapeHtml(key.key_prefix)}...</span>
                    </div>
                    <div class="detail-item">
                        <span class="detail-label">状态</span>
                        <span class="detail-value">
                            <span class="status-badge ${keyStatusMeta.className}">${keyStatusMeta.text}</span>
                        </span>
                    </div>
                    <div class="detail-item">
                        <span class="detail-label">创建时间</span>
                        <span class="detail-value">${createdAt}</span>
                    </div>
                    <div class="detail-item">
                        <span class="detail-label">过期时间</span>
                        <span class="detail-value">${expiresAt}</span>
                    </div>
                    <div class="detail-item">
                        <span class="detail-label">最后使用</span>
                        <span class="detail-value">${lastUsedAt}</span>
                    </div>
                </div>
            </div>
            
            <div class="detail-section">
                <h4>📁 授权模型组</h4>
                <div class="model-groups-list">
                    ${groups.map(g => `<span class="model-group-tag">${escapeHtml(g)}</span>`).join(' ')}
                </div>
            </div>
            
            <div class="detail-section">
                <h4>📊 使用统计</h4>
                <div class="stats-grid stats-grid-3" style="margin: 0;">
                    <div class="stat-card mini">
                        <div class="stat-label">总请求数</div>
                        <div class="stat-value">${formatNumber(key.total_requests || 0)}</div>
                        ${key.request_quota ? `<div class="stat-sub">限制: ${formatNumber(key.request_quota)}</div>` : ''}
                    </div>
                    <div class="stat-card mini">
                        <div class="stat-label">总 Token</div>
                        <div class="stat-value">${formatNumber(key.total_tokens || 0)}</div>
                        ${key.token_quota ? `<div class="stat-sub">限制: ${formatNumber(key.token_quota)}</div>` : ''}
                    </div>
                    <div class="stat-card mini">
                        <div class="stat-label">总费用</div>
                        <div class="stat-value">$${parseFloat(key.total_cost || 0).toFixed(4)}</div>
                        ${key.cost_limit ? `<div class="stat-sub">限制: $${parseFloat(key.cost_limit).toFixed(2)}</div>` : ''}
                    </div>
                </div>
            </div>

            ${renderPerModelUsageSection(key.per_model_usage)}
        `;
    } catch (error) {
        content.innerHTML = `<div class="text-center text-danger" style="padding: 24px 0;">加载详情失败: ${escapeHtml(error.message || '未知错误')}</div>`;
    }
    
    // 设置查看日志按钮
    const viewLogsBtn = document.getElementById('apikey-view-logs-btn');
    viewLogsBtn.onclick = () => {
        closeModal('apikey-detail-modal');
        viewApiKeyLogs(id);
    };
    
    // 设置重算费用按钮
    const recalcBtn = document.getElementById('apikey-recalc-btn');
    recalcBtn.onclick = () => recalculateApiKeyCost(id);
}

// 重新计算 API Key 费用
async function recalculateApiKeyCost(keyId) {
    if (!confirm('确定要重新计算该 API Key 的费用吗？\n\n这将根据调用日志重新计算从创建日期开始的所有费用。')) {
        return;
    }
    
    try {
        const response = await apiCall(`/keys/${keyId}/recalculate-cost`, {
            method: 'POST'
        });
        
        showToast(`费用重算完成：处理 ${response.logs_processed} 条日志，总费用 $${parseFloat(response.total_cost).toFixed(4)}`, 'success');
        
        // 刷新 API Key 列表
        apiKeysData = [];
        await loadApiKeys();
        
        // 关闭详情模态框
        closeModal('apikey-detail-modal');
    } catch (error) {
        showToast('费用重算失败: ' + error.message, 'error');
    }
}

// 查看 API Key 的调用日志
function viewApiKeyLogs(keyId) {
    // 切换到日志页面
    switchPage('logs');
    
    // 设置 API Key 过滤器
    const keyFilter = document.getElementById('log-apikey-filter');
    if (keyFilter) {
        keyFilter.value = keyId;
    }
    
    // 重新加载日志
    setTimeout(() => loadCallLogs(), 100);
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
    const tokenQuota = document.getElementById('apikey-token-quota').value;
    const costLimit = document.getElementById('apikey-cost-limit').value;
    
    const data = {
        name: document.getElementById('apikey-name').value || null,
        model_groups: selectedGroups,
        expires_at: document.getElementById('apikey-expires').value || null,
        request_quota: requestQuota ? parseInt(requestQuota) : null,
        token_quota: tokenQuota ? parseInt(tokenQuota) : null,
        cost_limit: costLimit ? parseFloat(costLimit) : null
    };
    
    try {
        const result = await apiCall('/keys', { method: 'POST', body: JSON.stringify(data) });
        
        closeModal('apikey-modal');
        
        // 显示新生成的 Key（清理可能的 Unicode 字符）
        document.getElementById('new-apikey-value').textContent = sanitizeApiKey(result.key);
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
    
    // 设置限制
    document.getElementById('apikey-edit-request-quota').value = key.request_quota || '';
    document.getElementById('apikey-edit-token-quota').value = key.token_quota || '';
    document.getElementById('apikey-edit-cost-limit').value = key.cost_limit || '';
    
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
    
    const requestQuotaValue = document.getElementById('apikey-edit-request-quota').value.trim();
    const tokenQuotaValue = document.getElementById('apikey-edit-token-quota').value.trim();
    const costLimitValue = document.getElementById('apikey-edit-cost-limit').value.trim();
    
    // 留空表示清除限制（发送 -1），有值则使用该值
    let requestQuota = requestQuotaValue === '' ? -1 : parseInt(requestQuotaValue);
    let tokenQuota = tokenQuotaValue === '' ? -1 : parseInt(tokenQuotaValue);
    let costLimit = costLimitValue === '' ? -1 : parseFloat(costLimitValue);
    
    const data = {
        name: document.getElementById('apikey-edit-name').value || null,
        model_groups: selectedGroups,
        expires_at: document.getElementById('apikey-edit-expires').value || null,
        request_quota: requestQuota,
        token_quota: tokenQuota,
        cost_limit: costLimit
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
            const currentValues = Array.from(accountGroupSelect.selectedOptions || []).map(
                option => option.value
            );
            accountGroupSelect.innerHTML = '<option value="">选择模型组</option>' + 
                groupsData.map(g => `<option value="${escapeHtml(g.name)}">${escapeHtml(g.name)}</option>`).join('');
            if (currentValues.length > 0) {
                Array.from(accountGroupSelect.options).forEach(option => {
                    option.selected = currentValues.includes(option.value);
                });
            }
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

/**
 * 清理 API Key 中可能的 Unicode 连字符变体
 * 某些字体/系统可能将 ASCII 连字符显示为 Unicode 变体
 */
function sanitizeApiKey(key) {
    return key
        .replace(/\u2013/g, '-')  // EN DASH
        .replace(/\u2014/g, '-')  // EM DASH
        .replace(/\u2212/g, '-')  // MINUS SIGN
        .replace(/\u2010/g, '-')  // HYPHEN
        .replace(/\u2011/g, '-'); // NON-BREAKING HYPHEN
}

async function copyToClipboard(text, button) {
    // 清理可能的 Unicode 连字符
    const cleanText = sanitizeApiKey(text);
    
    try {
        await navigator.clipboard.writeText(cleanText);
        
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
        textarea.value = cleanText;
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

// ==================== 调用日志 ====================

async function loadCallLogs() {
    const tbody = document.getElementById('logs-table-body');
    tbody.innerHTML = '<tr><td colspan="9" class="text-center">加载中...</td></tr>';
    
    try {
        // 加载统计数据
        const statsResponse = await apiCall('/logs/stats?hours=24');
        if (statsResponse.success) {
            const stats = statsResponse.stats;
            document.getElementById('log-stat-calls').textContent = formatNumber(stats.total_calls);
            document.getElementById('log-stat-success-rate').textContent = stats.success_rate + '%';
            document.getElementById('log-stat-tokens').textContent = formatNumber(stats.tokens.total);
            document.getElementById('log-stat-cost').textContent = stats.cost.display;
        }
        
        // 加载日志列表
        const limit = document.getElementById('log-limit')?.value || 50;
        const statusFilter = document.getElementById('log-status-filter')?.value || '';
        const modelFilter = document.getElementById('log-model-filter')?.value || '';
        const apiKeyFilter = document.getElementById('log-apikey-filter')?.value || '';
        
        let url = `/logs/calls?limit=${limit}`;
        if (statusFilter) url += `&status=${statusFilter}`;
        if (modelFilter) url += `&model_group=${modelFilter}`;
        if (apiKeyFilter) url += `&api_key_id=${apiKeyFilter}`;
        
        const response = await apiCall(url);
        callLogsData = response.logs || [];
        
        // 更新过滤器选项
        await updateLogFilters();
        
        renderCallLogsTable();
    } catch (error) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center text-danger">加载失败</td></tr>';
        showToast('加载调用日志失败: ' + error.message, 'error');
    }
}

// 更新日志页面的过滤器选项
async function updateLogFilters() {
    // 更新模型组过滤器
    await updateLogModelFilter();
    
    // 更新 API Key 过滤器
    await updateLogApiKeyFilter();
}

// 更新 API Key 过滤器
async function updateLogApiKeyFilter() {
    const select = document.getElementById('log-apikey-filter');
    if (!select) return;
    
    const currentValue = select.value;
    
    // 确保有 API Key 数据
    if (apiKeysData.length === 0) {
        try {
            const response = await apiCall('/keys?include_revoked=true');
            apiKeysData = response.keys || [];
        } catch (e) {
            console.error('加载 API Key 失败:', e);
        }
    }
    
    select.innerHTML = '<option value="">所有 API Key</option>' + 
        apiKeysData.map(k => `<option value="${k.id}">${escapeHtml(k.name || k.key_prefix + '...')}</option>`).join('');
    
    if (currentValue) select.value = currentValue;
}

// 显示删除日志模态框
function showDeleteLogsModal() {
    // 设置默认日期为7天前
    const defaultDate = new Date();
    defaultDate.setDate(defaultDate.getDate() - 7);
    document.getElementById('delete-logs-date').value = defaultDate.toISOString().split('T')[0];
    
    openModal('delete-logs-modal');
}

// 处理删除日志
async function handleDeleteLogs(e) {
    e.preventDefault();
    
    const dateStr = document.getElementById('delete-logs-date').value;
    if (!dateStr) {
        showToast('请选择日期', 'error');
        return;
    }
    
    showConfirmModal(
        `确定要删除 ${dateStr} 及之前的所有调用日志吗？\n\n⚠️ 此操作不可恢复！`,
        async () => {
            try {
                const response = await apiCall('/logs/calls', {
                    method: 'DELETE',
                    body: JSON.stringify({ before_date: dateStr })
                });
                
                closeModal('delete-logs-modal');
                showToast(`已删除 ${response.deleted_count || 0} 条日志`, 'success');
                
                // 重新加载日志
                loadCallLogs();
            } catch (error) {
                showToast('删除失败: ' + error.message, 'error');
            }
        },
        { title: '⚠️ 确认删除日志', btnText: '确认删除', btnClass: 'btn-danger' }
    );
}

async function updateLogModelFilter() {
    const select = document.getElementById('log-model-filter');
    if (!select) return;
    
    // 保存当前选择
    const currentValue = select.value;
    
    // 确保有模型组数据
    if (groupsData.length === 0) {
        try {
            const response = await apiCall('/groups');
            groupsData = response.groups || [];
        } catch (e) {
            console.error('加载模型组失败:', e);
        }
    }
    
    select.innerHTML = '<option value="">所有模型组</option>' + 
        groupsData.map(g => `<option value="${escapeHtml(g.name)}">${escapeHtml(g.name)}</option>`).join('');
    
    if (currentValue) select.value = currentValue;
}

function filterCallLogs() {
    loadCallLogs();
}

function renderCallLogsTable() {
    const tbody = document.getElementById('logs-table-body');
    
    if (callLogsData.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">暂无调用记录</td></tr>';
        return;
    }
    
    tbody.innerHTML = callLogsData.map((log, index) => {
        const statusClass = log.status === 'success' ? 'status-active' : 'status-exhausted';
        const statusText = log.status === 'success' ? '成功' : '失败';
        
        // 格式化时间
        const timestamp = log.timestamp ? new Date(log.timestamp) : null;
        const timeStr = timestamp ? timestamp.toLocaleString('zh-CN', {
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
        }) : '-';
        
        // API Key 显示
        const keyDisplay = log.api_key?.name || log.api_key?.prefix || '-';
        
        // 账号显示
        const accountDisplay = log.account?.name || '-';
        const modelGroup = log.account?.model_group || '-';
        
        // Token 显示
        const tokenDisplay = `${formatNumber(log.tokens?.input || 0)} / ${formatNumber(log.tokens?.output || 0)}`;
        
        // 费用显示
        const costDisplay = formatCost(log.cost?.total);
        
        // 耗时显示
        const timeMs = log.response_time_ms ? `${log.response_time_ms}ms` : '-';
        
        return `
            <tr class="${log.status === 'error' ? 'row-error' : ''}">
                <td><span class="log-time">${timeStr}</span></td>
                <td>
                    <span class="key-name" title="${escapeHtml(log.api_key?.prefix || '')}">${escapeHtml(keyDisplay)}</span>
                </td>
                <td>
                    <span class="account-name">${escapeHtml(accountDisplay)}</span>
                </td>
                <td><span class="model-name">${escapeHtml(log.request?.model || '-')}</span></td>
                <td><span class="token-display">${tokenDisplay}</span></td>
                <td><span class="cost-display">${costDisplay}</span></td>
                <td><span class="time-display">${timeMs}</span></td>
                <td><span class="status-badge ${statusClass}">${statusText}</span></td>
                <td>
                    <button class="btn btn-sm btn-secondary" onclick="showLogDetail(${index})">查看</button>
                </td>
            </tr>
        `;
    }).join('');
}

function formatCost(costStr) {
    if (!costStr || costStr === '0' || costStr === '0.000000') {
        return '$0';
    }
    
    const cost = parseFloat(costStr);
    if (cost < 0.000001) {
        return '< $0.000001';
    } else if (cost < 0.01) {
        return '$' + cost.toFixed(6);
    } else if (cost < 1) {
        return '$' + cost.toFixed(4);
    } else {
        return '$' + cost.toFixed(2);
    }
}

function showLogDetail(index) {
    const log = callLogsData[index];
    if (!log) return;
    
    const content = document.getElementById('log-detail-content');
    
    // 格式化时间
    const timestamp = log.timestamp ? new Date(log.timestamp).toLocaleString('zh-CN') : '-';
    
    content.innerHTML = `
        <div class="log-detail-section">
            <h4>📌 基本信息</h4>
            <div class="detail-grid">
                <div class="detail-item">
                    <span class="detail-label">时间</span>
                    <span class="detail-value">${timestamp}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">状态</span>
                    <span class="detail-value">
                        <span class="status-badge ${log.status === 'success' ? 'status-active' : 'status-exhausted'}">
                            ${log.status === 'success' ? '成功' : '失败'}
                        </span>
                    </span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">响应时间</span>
                    <span class="detail-value">${log.response_time_ms ? log.response_time_ms + 'ms' : '-'}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">流式</span>
                    <span class="detail-value">${log.request?.is_stream ? '是' : '否'}</span>
                </div>
            </div>
        </div>
        
        <div class="log-detail-section">
            <h4>🔑 调用方</h4>
            <div class="detail-grid">
                <div class="detail-item">
                    <span class="detail-label">API Key</span>
                    <span class="detail-value">${escapeHtml(log.api_key?.name || log.api_key?.prefix || '-')}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Key 前缀</span>
                    <span class="detail-value">${escapeHtml(log.api_key?.prefix || '-')}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">客户端 IP</span>
                    <span class="detail-value">${escapeHtml(log.client_ip || '-')}</span>
                </div>
            </div>
        </div>
        
        <div class="log-detail-section">
            <h4>👤 账号信息</h4>
            <div class="detail-grid">
                <div class="detail-item">
                    <span class="detail-label">账号名称</span>
                    <span class="detail-value">${escapeHtml(log.account?.name || '-')}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">模型组</span>
                    <span class="detail-value">${escapeHtml(log.account?.model_group || '-')}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">请求模型</span>
                    <span class="detail-value">${escapeHtml(log.request?.model || '-')}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">API 类型</span>
                    <span class="detail-value">${escapeHtml(log.request?.api_type || '-')}</span>
                </div>
            </div>
        </div>
        
        <div class="log-detail-section">
            <h4>💎 Token 与费用</h4>
            <div class="detail-grid">
                <div class="detail-item">
                    <span class="detail-label">输入 Token</span>
                    <span class="detail-value">${formatNumber(log.tokens?.input || 0)}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">输出 Token</span>
                    <span class="detail-value">${formatNumber(log.tokens?.output || 0)}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">总 Token</span>
                    <span class="detail-value">${formatNumber(log.tokens?.total || 0)}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">输入费用</span>
                    <span class="detail-value">${formatCost(log.cost?.input)}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">输出费用</span>
                    <span class="detail-value">${formatCost(log.cost?.output)}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">总费用</span>
                    <span class="detail-value highlight">${formatCost(log.cost?.total)}</span>
                </div>
            </div>
        </div>
        
        ${log.input_preview ? `
        <div class="log-detail-section">
            <h4>📥 输入预览</h4>
            <pre class="preview-box">${escapeHtml(log.input_preview)}</pre>
        </div>
        ` : ''}
        
        ${log.output_preview ? `
        <div class="log-detail-section">
            <h4>📤 输出预览 (前10行)</h4>
            <pre class="preview-box">${escapeHtml(log.output_preview)}</pre>
        </div>
        ` : ''}
        
        ${log.error_message ? `
        <div class="log-detail-section">
            <h4>❌ 错误信息</h4>
            <pre class="preview-box error">${escapeHtml(log.error_message)}</pre>
        </div>
        ` : ''}
    `;
    
    openModal('log-detail-modal');
}


// ==================== 性能监控 ====================

let performanceInterval = null;

async function loadPerformanceStats() {
    try {
        const response = await apiCall('/performance/stats');
        if (response.success) {
            updatePerformanceDisplay(response);
        }
    } catch (error) {
        console.error('加载性能统计失败:', error);
        showToast('加载性能统计失败: ' + error.message, 'error');
    }
}

function updatePerformanceDisplay(data) {
    const stats = data.stats || {};
    const config = data.config || {};
    const system = data.system || {};
    const responseTimes = data.response_times || {};
    
    // 更新实时指标
    document.getElementById('perf-active-requests').textContent = stats.active_requests || 0;
    document.getElementById('perf-max-concurrent').textContent = `/ ${stats.max_concurrent_requests || 100}`;
    document.getElementById('perf-current-qps').textContent = stats.current_qps || 0;
    document.getElementById('perf-avg-response').textContent = responseTimes.avg || 0;
    document.getElementById('perf-success-rate').textContent = `${stats.success_rate || 100}%`;
    document.getElementById('perf-rejected').textContent = `拒绝: ${stats.rejected_requests || 0}`;
    
    // 更新限流配置显示
    const rpmLimit = config.rpm_limit || 0;
    document.getElementById('config-rpm-value').textContent = rpmLimit > 0 ? rpmLimit : '无限制';
    document.getElementById('config-rpm-max').textContent = config.max_rpm || 6000;
    document.getElementById('config-concurrent-value').textContent = config.max_concurrent_requests || 100;
    document.getElementById('config-db-concurrent-value').textContent = config.max_concurrent_db_ops || 50;
    document.getElementById('config-http-pool-value').textContent = config.http_max_connections || 100;
    
    // 更新性能统计
    document.getElementById('perf-total-requests').textContent = formatNumber(stats.total_requests || 0);
    document.getElementById('perf-total-rejected').textContent = formatNumber(stats.rejected_requests || 0);
    document.getElementById('perf-p95-response').textContent = `${responseTimes.p95 || 0} ms`;
    document.getElementById('perf-p99-response').textContent = `${responseTimes.p99 || 0} ms`;
    
    // 更新系统配置
    document.getElementById('sys-db-pool').textContent = system.db_pool || '-';
    document.getElementById('sys-db-type').textContent = system.db_type || '-';
    document.getElementById('sys-http-timeout').textContent = system.http_timeout || '-';
    document.getElementById('sys-uptime').textContent = system.uptime || '-';
    
    // 更新最后更新时间
    document.getElementById('perf-last-update').textContent = `最后更新: ${new Date().toLocaleTimeString('zh-CN')}`;
}

function startPerformanceAutoRefresh() {
    // 清除旧的定时器
    if (performanceInterval) {
        clearInterval(performanceInterval);
    }
    // 每 5 秒自动刷新
    performanceInterval = setInterval(() => {
        if (currentPage === 'performance') {
            loadPerformanceStats();
        }
    }, 5000);
}

function stopPerformanceAutoRefresh() {
    if (performanceInterval) {
        clearInterval(performanceInterval);
        performanceInterval = null;
    }
}

function refreshPerformanceStats() {
    loadPerformanceStats();
    showToast('已刷新', 'success');
}

function showPerformanceSettingsModal() {
    // 先加载当前配置
    apiCall('/performance/stats').then(response => {
        if (response.success) {
            const config = response.config || {};
            document.getElementById('setting-rpm').value = config.rpm_limit || '';
            document.getElementById('setting-max-concurrent').value = config.max_concurrent_requests || 100;
            document.getElementById('setting-db-concurrent').value = config.max_concurrent_db_ops || 50;
            document.getElementById('setting-http-connections').value = config.http_max_connections || 100;
            document.getElementById('setting-http-timeout').value = config.http_timeout || 60;
        }
        openModal('performance-settings-modal');
    }).catch(error => {
        console.error('加载配置失败:', error);
        openModal('performance-settings-modal');
    });
}

// 绑定性能配置表单提交
document.addEventListener('DOMContentLoaded', () => {
    const perfForm = document.getElementById('performance-settings-form');
    if (perfForm) {
        perfForm.addEventListener('submit', handlePerformanceSettingsSubmit);
    }
});

async function handlePerformanceSettingsSubmit(e) {
    e.preventDefault();
    
    const data = {
        rpm_limit: parseInt(document.getElementById('setting-rpm').value) || 0,
        max_concurrent_requests: parseInt(document.getElementById('setting-max-concurrent').value) || 100,
        max_concurrent_db_ops: parseInt(document.getElementById('setting-db-concurrent').value) || 50,
        http_max_connections: parseInt(document.getElementById('setting-http-connections').value) || 100,
        http_timeout: parseFloat(document.getElementById('setting-http-timeout').value) || 60
    };
    
    try {
        const response = await apiCall('/performance/config', {
            method: 'POST',
            body: JSON.stringify(data)
        });
        
        if (response.success) {
            closeModal('performance-settings-modal');
            showToast(response.message || '配置已保存', 'success');
            loadPerformanceStats();
        } else {
            showToast(response.message || '保存失败', 'error');
        }
    } catch (error) {
        showToast('保存失败: ' + error.message, 'error');
    }
}

async function resetPerformanceStats() {
    try {
        const response = await apiCall('/performance/reset-stats', { method: 'POST' });
        if (response.success) {
            showToast('统计已重置', 'success');
            loadPerformanceStats();
        }
    } catch (error) {
        showToast('重置失败: ' + error.message, 'error');
    }
}
