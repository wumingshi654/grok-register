// ==UserScript==
// @name         Grok & xAI 账号 Token 导出工具 (grok2api / grok-register)
// @namespace    https://github.com/wumingshi654/grok-register
// @version      1.0.0
// @description  手工注册或登录 x.ai / grok.com 后，自动提取 SSO Token、Cookie 及账号信息，一键导出为 grok2api 本地 token.json 片段、远程导入格式或 grok-register 账号文件格式，支持直接一键推送到 grok2api 接口。
// @author       grok-register
// @match        https://grok.com/*
// @match        https://accounts.x.ai/*
// @match        https://x.ai/*
// @match        https://auth.x.ai/*
// @grant        GM_cookie
// @grant        GM_setClipboard
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_xmlhttpRequest
// @grant        GM_notification
// @grant        GM_registerMenuCommand
// @run-at       document-end
// ==/UserScript==

(function () {
    'use strict';

    // 默认配置
    const CONFIG = {
        grok2apiBase: GM_getValue('grok2api_base', 'http://127.0.0.1:8000'),
        poolName: GM_getValue('grok2api_pool_name', 'ssoBasic'),
        appKey: GM_getValue('grok2api_app_key', ''),
        adminUsername: GM_getValue('grok2api_admin_username', 'admin'),
        adminPassword: GM_getValue('grok2api_admin_password', ''),
    };

    // 内存中捕获的表单凭据
    let capturedEmail = GM_getValue('last_captured_email', '');
    let capturedPassword = GM_getValue('last_captured_password', '');

    // 监听页面输入框，自动暂存用户输入的邮箱和密码
    function listenFormInputs() {
        document.addEventListener('input', (e) => {
            const target = e.target;
            if (!target || !target.tagName) return;
            const tag = target.tagName.toLowerCase();
            if (tag !== 'input' && tag !== 'textarea') return;

            const type = (target.getAttribute('type') || '').toLowerCase();
            const name = (target.getAttribute('name') || '').toLowerCase();
            const testId = (target.getAttribute('data-testid') || '').toLowerCase();
            const val = (target.value || '').trim();

            if (type === 'email' || name.includes('email') || testId.includes('email')) {
                if (val && val.includes('@')) {
                    capturedEmail = val;
                    GM_setValue('last_captured_email', val);
                    updateUI();
                }
            } else if (type === 'password' || name.includes('password') || testId.includes('password')) {
                if (val) {
                    capturedPassword = val;
                    GM_setValue('last_captured_password', val);
                    updateUI();
                }
            }
        }, true);
    }

    // 获取当前站点的所有 Cookie（优先使用 GM_cookie 读取 HttpOnly）
    function fetchAllCookies() {
        return new Promise((resolve) => {
            const cookieMap = new Map();

            // 1. 如果支持 GM_cookie.list，则多域名遍历获取完整 Cookie (包含 HttpOnly)
            if (typeof GM_cookie !== 'undefined' && GM_cookie.list) {
                const targetDomains = ['.x.ai', 'accounts.x.ai', 'x.ai', 'grok.com', '.grok.com'];
                let pending = targetDomains.length;
                let settled = false;

                const finish = () => {
                    if (settled) return;
                    settled = true;
                    // 合并 document.cookie 兜底
                    mergeDocumentCookies(cookieMap);
                    resolve(cookieMap);
                };

                targetDomains.forEach((domain) => {
                    try {
                        GM_cookie.list({ domain }, (cookies, error) => {
                            if (!error && Array.isArray(cookies)) {
                                cookies.forEach((c) => {
                                    if (c && c.name && c.value) {
                                        cookieMap.set(c.name, c.value);
                                    }
                                });
                            }
                            pending--;
                            if (pending <= 0) finish();
                        });
                    } catch (e) {
                        pending--;
                        if (pending <= 0) finish();
                    }
                });

                // 超时保护
                setTimeout(finish, 1200);
            } else {
                // 2. 降级模式：读取 document.cookie
                mergeDocumentCookies(cookieMap);
                resolve(cookieMap);
            }
        });
    }

    function mergeDocumentCookies(map) {
        if (!document.cookie) return;
        document.cookie.split(';').forEach((pair) => {
            const idx = pair.indexOf('=');
            if (idx > -1) {
                const k = pair.slice(0, idx).trim();
                const v = pair.slice(idx + 1).trim();
                if (k && !map.has(k)) {
                    map.set(k, v);
                }
            }
        });
    }

    // 格式化展示 Token 掩码
    function maskToken(token) {
        if (!token) return '无';
        if (token.length <= 16) return token;
        return `${token.slice(0, 8)}...${token.slice(-8)} (长度: ${token.length})`;
    }

    // 创建 UI
    let panelEl = null;
    let floatBtn = null;

    function createUI() {
        if (document.getElementById('grok-export-root')) return;

        const root = document.createElement('div');
        root.id = 'grok-export-root';
        root.style.all = 'initial';
        root.style.fontFamily = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';

        // 浮动悬浮球
        floatBtn = document.createElement('button');
        floatBtn.textContent = '🤖 Grok 导出';
        floatBtn.style.cssText = `
            position: fixed;
            bottom: 24px;
            right: 24px;
            z-index: 999999;
            background: linear-gradient(135deg, #10a37f, #0d8a6a);
            color: #ffffff;
            border: none;
            border-radius: 20px;
            padding: 8px 16px;
            font-size: 13px;
            font-weight: 600;
            box-shadow: 0 4px 14px rgba(0,0,0,0.25);
            cursor: pointer;
            transition: all 0.2s ease;
        `;
        floatBtn.onmouseover = () => floatBtn.style.transform = 'scale(1.05)';
        floatBtn.onmouseout = () => floatBtn.style.transform = 'scale(1)';
        floatBtn.onclick = togglePanel;

        // 弹窗面板
        panelEl = document.createElement('div');
        panelEl.style.cssText = `
            position: fixed;
            bottom: 68px;
            right: 24px;
            z-index: 999999;
            width: 380px;
            background: #1e1e24;
            color: #f3f4f6;
            border: 1px solid #374151;
            border-radius: 12px;
            padding: 16px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
            display: none;
            box-sizing: border-box;
            font-size: 13px;
            max-height: 85vh;
            overflow-y: auto;
        `;

        panelEl.innerHTML = `
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; border-bottom:1px solid #374151; padding-bottom:8px;">
                <div style="font-weight:bold; font-size:14px; color:#10b981;">⚡ Grok 账号 / Token 导出</div>
                <button id="grok-close-btn" style="background:none; border:none; color:#9ca3af; font-size:16px; cursor:pointer;">&times;</button>
            </div>

            <div style="margin-bottom:10px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                    <span style="color:#9ca3af; font-size:12px;">SSO Token 状态:</span>
                    <button id="grok-refresh-cookie" style="background:none; border:none; color:#3b82f6; font-size:11px; cursor:pointer; text-decoration:underline;">刷新检测</button>
                </div>
                <div id="grok-sso-status" style="background:#111827; border:1px solid #374151; border-radius:6px; padding:6px 10px; font-family:monospace; font-size:11px; word-break:break-all; color:#e5e7eb;">
                    检测中...
                </div>
            </div>

            <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:10px;">
                <div>
                    <label style="display:block; font-size:11px; color:#9ca3af; margin-bottom:3px;">邮箱 (Email)</label>
                    <input id="grok-input-email" type="text" placeholder="注册邮箱" style="width:100%; box-sizing:border-box; background:#111827; border:1px solid #374151; border-radius:6px; color:#fff; padding:5px 8px; font-size:12px;">
                </div>
                <div>
                    <label style="display:block; font-size:11px; color:#9ca3af; margin-bottom:3px;">密码 (Password)</label>
                    <input id="grok-input-password" type="text" placeholder="注册密码" style="width:100%; box-sizing:border-box; background:#111827; border:1px solid #374151; border-radius:6px; color:#fff; padding:5px 8px; font-size:12px;">
                </div>
            </div>

            <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-bottom:10px;">
                <button id="grok-btn-copy-sso" style="background:#2563eb; color:#fff; border:none; border-radius:6px; padding:7px; font-size:12px; font-weight:500; cursor:pointer;">📋 复制 SSO Token</button>
                <button id="grok-btn-copy-line" style="background:#4b5563; color:#fff; border:none; border-radius:6px; padding:7px; font-size:12px; font-weight:500; cursor:pointer;">📑 复制 账号行</button>
                <button id="grok-btn-copy-json" style="background:#059669; color:#fff; border:none; border-radius:6px; padding:7px; font-size:12px; font-weight:500; cursor:pointer;">📦 复制 grok2api JSON</button>
                <button id="grok-btn-copy-cookie" style="background:#374151; color:#fff; border:none; border-radius:6px; padding:7px; font-size:12px; font-weight:500; cursor:pointer;">🍪 复制 完整 Cookie</button>
            </div>

            <!-- 可折叠: 一键推送到远程/本地 grok2api -->
            <details style="background:#111827; border:1px solid #374151; border-radius:6px; padding:6px 10px; margin-bottom:10px;">
                <summary style="font-size:12px; color:#9ca3af; cursor:pointer; font-weight:500;">🚀 一键导入到 grok2api 服务</summary>
                <div style="margin-top:8px; display:flex; flex-direction:column; gap:6px;">
                    <div>
                        <label style="font-size:11px; color:#9ca3af;">grok2api Base URL:</label>
                        <input id="grok-cfg-base" type="text" value="${CONFIG.grok2apiBase}" placeholder="如 http://127.0.0.1:8000" style="width:100%; box-sizing:border-box; background:#1e1e24; border:1px solid #4b5563; border-radius:4px; color:#fff; padding:4px 6px; font-size:11px;">
                    </div>
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px;">
                        <div>
                            <label style="font-size:11px; color:#9ca3af;">池类型:</label>
                            <select id="grok-cfg-pool" style="width:100%; box-sizing:border-box; background:#1e1e24; border:1px solid #4b5563; border-radius:4px; color:#fff; padding:4px; font-size:11px;">
                                <option value="ssoBasic" ${CONFIG.poolName === 'ssoBasic' ? 'selected' : ''}>ssoBasic</option>
                                <option value="ssoSuper" ${CONFIG.poolName === 'ssoSuper' ? 'selected' : ''}>ssoSuper</option>
                            </select>
                        </div>
                        <div>
                            <label style="font-size:11px; color:#9ca3af;">App Key (旧版):</label>
                            <input id="grok-cfg-key" type="password" value="${CONFIG.appKey}" placeholder="可选" style="width:100%; box-sizing:border-box; background:#1e1e24; border:1px solid #4b5563; border-radius:4px; color:#fff; padding:4px 6px; font-size:11px;">
                        </div>
                    </div>
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px;">
                        <div>
                            <label style="font-size:11px; color:#9ca3af;">管理员账号 (新版):</label>
                            <input id="grok-cfg-user" type="text" value="${CONFIG.adminUsername}" placeholder="admin" style="width:100%; box-sizing:border-box; background:#1e1e24; border:1px solid #4b5563; border-radius:4px; color:#fff; padding:4px 6px; font-size:11px;">
                        </div>
                        <div>
                            <label style="font-size:11px; color:#9ca3af;">管理员密码 (新版):</label>
                            <input id="grok-cfg-pass" type="password" value="${CONFIG.adminPassword}" placeholder="密码" style="width:100%; box-sizing:border-box; background:#1e1e24; border:1px solid #4b5563; border-radius:4px; color:#fff; padding:4px 6px; font-size:11px;">
                        </div>
                    </div>
                    <button id="grok-btn-push" style="margin-top:4px; background:#7c3aed; color:#fff; border:none; border-radius:4px; padding:6px; font-size:11px; font-weight:600; cursor:pointer;">
                        🚀 提交当前 Token 入池
                    </button>
                    <div id="grok-push-status" style="font-size:11px; color:#9ca3af; min-height:16px;"></div>
                </div>
            </details>

            <div id="grok-msg" style="font-size:11px; color:#10b981; text-align:center; min-height:16px;"></div>
        `;

        root.appendChild(floatBtn);
        root.appendChild(panelEl);
        document.body.appendChild(root);

        // 绑定事件
        document.getElementById('grok-close-btn').onclick = togglePanel;
        document.getElementById('grok-refresh-cookie').onclick = () => updateUI(true);
        document.getElementById('grok-btn-copy-sso').onclick = copySSOToken;
        document.getElementById('grok-btn-copy-line').onclick = copyAccountLine;
        document.getElementById('grok-btn-copy-json').onclick = copyGrok2ApiJson;
        document.getElementById('grok-btn-copy-cookie').onclick = copyFullCookie;
        document.getElementById('grok-btn-push').onclick = pushToGrok2api;

        // 监听手动输入
        document.getElementById('grok-input-email').oninput = (e) => {
            capturedEmail = e.target.value.trim();
            GM_setValue('last_captured_email', capturedEmail);
        };
        document.getElementById('grok-input-password').oninput = (e) => {
            capturedPassword = e.target.value.trim();
            GM_setValue('last_captured_password', capturedPassword);
        };

        // 监听配置输入
        document.getElementById('grok-cfg-base').onchange = (e) => GM_setValue('grok2api_base', e.target.value.trim());
        document.getElementById('grok-cfg-pool').onchange = (e) => GM_setValue('grok2api_pool_name', e.target.value.trim());
        document.getElementById('grok-cfg-key').onchange = (e) => GM_setValue('grok2api_app_key', e.target.value.trim());
        document.getElementById('grok-cfg-user').onchange = (e) => GM_setValue('grok2api_admin_username', e.target.value.trim());
        document.getElementById('grok-cfg-pass').onchange = (e) => GM_setValue('grok2api_admin_password', e.target.value.trim());

        updateUI();
    }

    function togglePanel() {
        if (!panelEl) return;
        const isHidden = panelEl.style.display === 'none';
        panelEl.style.display = isHidden ? 'block' : 'none';
        if (isHidden) {
            updateUI(true);
        }
    }

    let currentSSOToken = '';
    let currentCookies = new Map();

    async function updateUI(showFeedback = false) {
        if (!panelEl) return;
        const emailInput = document.getElementById('grok-input-email');
        const passInput = document.getElementById('grok-input-password');
        const statusEl = document.getElementById('grok-sso-status');
        const msgEl = document.getElementById('grok-msg');

        if (emailInput && !emailInput.value && capturedEmail) emailInput.value = capturedEmail;
        if (passInput && !passInput.value && capturedPassword) passInput.value = capturedPassword;

        currentCookies = await fetchAllCookies();
        currentSSOToken = currentCookies.get('sso') || currentCookies.get('sso-rw') || '';

        if (statusEl) {
            if (currentSSOToken) {
                statusEl.innerHTML = `<span style="color:#10b981; font-weight:bold;">🟢 已获取 SSO:</span> <span title="${currentSSOToken}">${maskToken(currentSSOToken)}</span>`;
            } else {
                statusEl.innerHTML = `<span style="color:#ef4444; font-weight:bold;">🔴 未检测到 SSO Cookie</span><br><span style="color:#9ca3af; font-size:10px;">提示: 请完成注册或登录后刷新。若已登录，可在浏览器 F12 Application -> Cookies 中复制 sso 值</span>`;
            }
        }

        if (showFeedback && msgEl) {
            msgEl.textContent = currentSSOToken ? 'Cookie 状态已刷新 (SSO 有效)' : 'Cookie 已刷新 (暂未找到 SSO)';
            setTimeout(() => { if (msgEl) msgEl.textContent = ''; }, 2500);
        }
    }

    function notify(text, isError = false) {
        const msgEl = document.getElementById('grok-msg');
        if (msgEl) {
            msgEl.style.color = isError ? '#ef4444' : '#10b981';
            msgEl.textContent = text;
            setTimeout(() => { if (msgEl) msgEl.textContent = ''; }, 3000);
        }
        if (typeof GM_notification !== 'undefined') {
            try {
                GM_notification({
                    text,
                    title: 'Grok 导出工具',
                    timeout: 2000,
                });
            } catch (e) {}
        }
    }

    function copySSOToken() {
        if (!currentSSOToken) {
            notify('未检测到 SSO Token，无法复制', true);
            return;
        }
        GM_setClipboard(currentSSOToken);
        notify('✅ SSO Token 已复制到剪贴板！');
    }

    function copyAccountLine() {
        if (!currentSSOToken) {
            notify('未检测到 SSO Token，无法复制', true);
            return;
        }
        const email = (document.getElementById('grok-input-email')?.value || capturedEmail || 'unknown@mail.com').trim();
        const pwd = (document.getElementById('grok-input-password')?.value || capturedPassword || 'Password123').trim();
        const line = `${email}----${pwd}----${currentSSOToken}`;
        GM_setClipboard(line);
        notify('✅ 账号行 (email----pwd----sso) 已复制！');
    }

    function copyGrok2ApiJson() {
        if (!currentSSOToken) {
            notify('未检测到 SSO Token，无法复制', true);
            return;
        }
        const email = (document.getElementById('grok-input-email')?.value || capturedEmail || '').trim();
        const obj = {
            token: currentSSOToken,
            tags: ["manual-register"],
            note: email
        };
        const text = JSON.stringify(obj, null, 2);
        GM_setClipboard(text);
        notify('✅ grok2api JSON 条目已复制！');
    }

    function copyFullCookie() {
        if (!currentCookies.size) {
            notify('当前无可用 Cookie', true);
            return;
        }
        const pairs = [];
        currentCookies.forEach((val, key) => pairs.push(`${key}=${val}`));
        const cookieStr = pairs.join('; ');
        GM_setClipboard(cookieStr);
        notify(`✅ 完整 Cookie (${currentCookies.size} 项) 已复制！`);
    }

    // 推送到 grok2api
    async function pushToGrok2api() {
        const pushStatus = document.getElementById('grok-push-status');
        if (!currentSSOToken) {
            if (pushStatus) pushStatus.innerHTML = '<span style="color:#ef4444;">未获取到 SSO Token</span>';
            return;
        }
        const base = (document.getElementById('grok-cfg-base')?.value || '').trim().replace(/\/+$/, '');
        const pool = (document.getElementById('grok-cfg-pool')?.value || 'ssoBasic').trim();
        const appKey = (document.getElementById('grok-cfg-key')?.value || '').trim();
        const username = (document.getElementById('grok-cfg-user')?.value || '').trim();
        const password = (document.getElementById('grok-cfg-pass')?.value || '').trim();
        const email = (document.getElementById('grok-input-email')?.value || capturedEmail || '').trim();

        if (!base) {
            if (pushStatus) pushStatus.innerHTML = '<span style="color:#ef4444;">请填写 grok2api Base URL</span>';
            return;
        }

        if (pushStatus) pushStatus.innerHTML = '<span style="color:#3b82f6;">正在提交入池...</span>';

        // 分支 A: 如果配置了新版管理员账号密码，尝试新版 Go 接口登录并导入
        if (username && password) {
            try {
                // 1. 登录换取 accessToken
                const loginRes = await gmFetch(`${base}/api/admin/v1/auth/login`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    data: JSON.stringify({ username, password })
                });
                if (loginRes.status < 200 || loginRes.status >= 300) {
                    throw new Error(`管理员登录失败: HTTP ${loginRes.status}`);
                }
                const loginData = JSON.parse(loginRes.responseText);
                const accessToken = loginData?.data?.tokens?.accessToken;
                if (!accessToken) throw new Error('登录响应缺少 accessToken');

                // 2. 上传 Token
                const boundary = '----WebKitFormBoundary' + Math.random().toString(36).substring(2);
                const fileBody = `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="grok-web-sso.txt"\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n${currentSSOToken}\n\r\n--${boundary}--\r\n`;

                const importRes = await gmFetch(`${base}/api/admin/v1/accounts/web/import`, {
                    method: 'POST',
                    headers: {
                        'Authorization': `Bearer ${accessToken}`,
                        'Content-Type': `multipart/form-data; boundary=${boundary}`,
                        'Accept': 'text/event-stream, application/json'
                    },
                    data: fileBody
                });

                if (importRes.status >= 200 && importRes.status < 300) {
                    if (pushStatus) pushStatus.innerHTML = '<span style="color:#10b981;">🎉 成功导入新版 grok2api！</span>';
                    notify('🎉 成功导入新版 grok2api！');
                    return;
                } else {
                    throw new Error(`导入接口返回 HTTP ${importRes.status}`);
                }
            } catch (err) {
                // 如果新版失败但配置了 appKey，尝试旧版回退
                if (!appKey) {
                    if (pushStatus) pushStatus.innerHTML = `<span style="color:#ef4444;">新版导入失败: ${err.message}</span>`;
                    return;
                }
            }
        }

        // 分支 B: 旧版 /tokens/add 接口导入
        if (appKey) {
            try {
                const remotePool = pool === 'ssoSuper' ? 'super' : 'basic';
                const payload = {
                    tokens: [currentSSOToken],
                    pool: remotePool,
                    tags: ["manual-register", email].filter(Boolean)
                };
                const addRes = await gmFetch(`${base}/tokens/add?app_key=${encodeURIComponent(appKey)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    data: JSON.stringify(payload)
                });
                if (addRes.status >= 200 && addRes.status < 300) {
                    if (pushStatus) pushStatus.innerHTML = '<span style="color:#10b981;">🎉 成功写入旧版 grok2api 远端池！</span>';
                    notify('🎉 成功写入旧版 grok2api 远端池！');
                    return;
                }
                // 尝试 /admin/api/tokens/add
                const addRes2 = await gmFetch(`${base}/admin/api/tokens/add?app_key=${encodeURIComponent(appKey)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    data: JSON.stringify(payload)
                });
                if (addRes2.status >= 200 && addRes2.status < 300) {
                    if (pushStatus) pushStatus.innerHTML = '<span style="color:#10b981;">🎉 成功写入 grok2api 远端池！</span>';
                    notify('🎉 成功写入 grok2api 远端池！');
                    return;
                }
                throw new Error(`HTTP ${addRes2.status}`);
            } catch (err) {
                if (pushStatus) pushStatus.innerHTML = `<span style="color:#ef4444;">写入失败: ${err.message}</span>`;
                return;
            }
        }

        if (pushStatus) {
            pushStatus.innerHTML = '<span style="color:#ef4444;">请提供新版管理员账号密码或旧版 App Key</span>';
        }
    }

    function gmFetch(url, options = {}) {
        return new Promise((resolve, reject) => {
            if (typeof GM_xmlhttpRequest === 'undefined') {
                reject(new Error('当前脚本管理器不支持 GM_xmlhttpRequest'));
                return;
            }
            GM_xmlhttpRequest({
                url,
                method: options.method || 'GET',
                headers: options.headers || {},
                data: options.data,
                timeout: 30000,
                onload: (res) => resolve(res),
                onerror: (err) => reject(new Error('网络请求异常')),
                ontimeout: () => reject(new Error('请求超时'))
            });
        });
    }

    // 注册油猴菜单
    if (typeof GM_registerMenuCommand !== 'undefined') {
        GM_registerMenuCommand('打开 Grok 导出面板', togglePanel);
        GM_registerMenuCommand('一键复制 SSO Token', copySSOToken);
        GM_registerMenuCommand('一键复制 账号行', copyAccountLine);
    }

    // 初始化
    listenFormInputs();
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', createUI);
    } else {
        createUI();
    }
})();
