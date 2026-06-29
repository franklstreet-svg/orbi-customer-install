/* Orbi owner dashboard — client logic */

// First-time password setup overlay. Installer creates the owner user
// with a random temp password that auto-fills the login form via the
// bootstrap query param — the customer never sees it. On the first
// dashboard load after install, we check /api/owner/account/status;
// if must_change_password is true we lock the dashboard behind a modal
// asking for a real password they'll remember. Runs as an IIFE outside
// the main module so it fires before any other init touches the page.
(function () {
  'use strict';

  async function _fetchJson(path, init) {
    const res = await fetch(path, Object.assign({
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
    }, init || {}));
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
    return body;
  }

  function _showSetPasswordModal() {
    if (document.getElementById('orbi-set-password-overlay')) return;
    const overlay = document.createElement('div');
    overlay.id = 'orbi-set-password-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.style.cssText = [
      'position:fixed', 'inset:0', 'background:rgba(0,0,0,0.92)',
      'z-index:99999', 'display:flex', 'align-items:center',
      'justify-content:center', 'backdrop-filter:blur(6px)',
      '-webkit-backdrop-filter:blur(6px)', 'padding:20px',
    ].join(';');
    overlay.innerHTML = `
      <div style="background:#1a2235;border:1px solid #2dd4bf;border-radius:14px;padding:32px;max-width:440px;width:100%;color:#e8eaf0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
        <h2 style="color:#2dd4bf;margin:0 0 8px;font-size:22px">Let's set you up</h2>
        <p style="margin:0 0 18px;font-size:14px;color:#aab0bc;line-height:1.5">Three quick things and you're in.</p>

        <label style="display:block;font-size:13px;color:#aab0bc;margin-bottom:4px">What should Orbi call you?</label>
        <input type="text" id="orbi-display-name" placeholder="Your first name" autocomplete="off" style="width:100%;background:#0b0f1a;border:1px solid #2c3957;color:#eaf0ff;border-radius:8px;padding:12px 14px;font-size:15px;margin-bottom:14px;box-sizing:border-box">

        <label style="display:block;font-size:13px;color:#aab0bc;margin-bottom:4px">What would you like to call her? (default: Orbi)</label>
        <input type="text" id="orbi-assistant-name" placeholder="Orbi" autocomplete="off" style="width:100%;background:#0b0f1a;border:1px solid #2c3957;color:#eaf0ff;border-radius:8px;padding:12px 14px;font-size:15px;margin-bottom:14px;box-sizing:border-box">

        <label style="display:block;font-size:13px;color:#aab0bc;margin-bottom:4px">Set a password you'll remember (6+ characters)</label>
        <input type="password" id="orbi-pw-1" placeholder="New password" autocomplete="new-password" style="width:100%;background:#0b0f1a;border:1px solid #2c3957;color:#eaf0ff;border-radius:8px;padding:12px 14px;font-size:15px;margin-bottom:10px;box-sizing:border-box">
        <input type="password" id="orbi-pw-2" placeholder="Confirm password" autocomplete="new-password" style="width:100%;background:#0b0f1a;border:1px solid #2c3957;color:#eaf0ff;border-radius:8px;padding:12px 14px;font-size:15px;margin-bottom:18px;box-sizing:border-box">

        <div id="orbi-pw-error" style="color:#ffb0b0;font-size:13px;margin-bottom:10px;min-height:18px"></div>
        <button id="orbi-pw-submit" type="button" style="width:100%;background:#2dd4bf;color:#0a0a0a;border:0;border-radius:8px;padding:12px;font-weight:700;font-size:15px;cursor:pointer">Save and continue</button>
      </div>
    `;
    document.body.appendChild(overlay);

    const displayNameEl   = overlay.querySelector('#orbi-display-name');
    const assistantNameEl = overlay.querySelector('#orbi-assistant-name');
    const pw1 = overlay.querySelector('#orbi-pw-1');
    const pw2 = overlay.querySelector('#orbi-pw-2');
    const err = overlay.querySelector('#orbi-pw-error');
    const btn = overlay.querySelector('#orbi-pw-submit');
    setTimeout(() => displayNameEl.focus(), 50);

    async function submit() {
      err.textContent = '';
      const displayName = (displayNameEl.value || '').trim();
      if (!displayName) {
        err.textContent = 'What should Orbi call you?';
        displayNameEl.focus(); return;
      }
      if (pw1.value.length < 6) {
        err.textContent = 'Password must be at least 6 characters.';
        pw1.focus(); return;
      }
      if (pw1.value !== pw2.value) {
        err.textContent = "Passwords don't match.";
        pw2.focus(); return;
      }
      btn.disabled = true;
      btn.textContent = 'Saving…';
      try {
        await _fetchJson('/api/owner/account/setup_initial_password', {
          method: 'POST',
          body: JSON.stringify({
            new_password:    pw1.value,
            display_name:    displayName,
            assistant_name: (assistantNameEl.value || '').trim() || 'Orbi',
          }),
        });
        overlay.remove();
        window.location.reload();
      } catch (e) {
        err.textContent = (e && e.message) || 'Could not save.';
        btn.disabled = false;
        btn.textContent = 'Save and continue';
      }
    }

    btn.addEventListener('click', submit);
    [displayNameEl, assistantNameEl, pw1, pw2].forEach(el => el.addEventListener('keydown', e => {
      if (e.key === 'Enter') submit();
    }));
  }

  async function _checkInitialPasswordSetup() {
    try {
      const status = await _fetchJson('/api/owner/account/status');
      if (status && status.must_change_password) {
        _showSetPasswordModal();
      }
    } catch (_) { /* silent — endpoint may be unavailable */ }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _checkInitialPasswordSetup);
  } else {
    _checkInitialPasswordSetup();
  }
})();

(function () {
  'use strict';

  // ------------------------------------------------------------------
  // State
  // ------------------------------------------------------------------
  let businessInfo = null;
  let messages = [];
  let settings = null;
  let activeMessageFilter = 'all';

  // ------------------------------------------------------------------
  // Bootstrap
  // ------------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', () => {
    setupTabs();
    setupLogout();
    setupReportIssue();
    setupMessageFilters();
    setupBusinessForm();
    setupSettingsForm();
    setupOwnerChat();
    setupPushNotifications();

    loadAll();
    setInterval(loadMessages, 30_000); // refresh messages every 30s

    document.getElementById('refresh-messages')?.addEventListener('click', loadMessages);
  });

  // ------------------------------------------------------------------
  // API helpers
  // ------------------------------------------------------------------
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
      credentials: 'same-origin'
    });
    if (res.status === 401) {
      window.location.href = '/owner/login';
      throw new Error('Unauthorized');
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `Request failed (${res.status})`);
    }
    return res.status === 204 ? null : res.json();
  }

  async function loadAll() {
    try {
      const status = await api('/api/owner/status');
      document.getElementById('business-name').textContent =
        status.business_name || '—';
      document.getElementById('tier-label').textContent =
        prettyTier(status.tier);
      document.getElementById('next-billing').textContent =
        status.period_end ? new Date(status.period_end * 1000).toLocaleDateString() : '—';
      updateConnectionPill(status.connection);
    } catch (e) {
      console.warn('status failed', e);
    }
    await Promise.all([loadMessages(), loadBusinessInfo(), loadSettings()]);
  }

  function prettyTier(t) {
    return ({
      chat_only: 'Chat Only',
      standard: 'Standard',
      local_only_premium: 'Local-Only Premium'
    })[t] || '—';
  }

  function updateConnectionPill(conn) {
    const pill = document.getElementById('status-pill');
    if (!pill) return;
    if (conn === 'online') {
      pill.className = 'status-pill';
      pill.textContent = '● Online';
    } else if (conn === 'degraded') {
      pill.className = 'status-pill degraded';
      pill.textContent = '● Backup mode';
    } else {
      pill.className = 'status-pill offline';
      pill.textContent = '● Offline';
    }
  }

  // ------------------------------------------------------------------
  // Tabs
  // ------------------------------------------------------------------
  function setupTabs() {
    document.querySelectorAll('.tab').forEach((tab) => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach((p) => p.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
      });
    });
  }

  function setupLogout() {
    document.getElementById('logout-btn')?.addEventListener('click', async () => {
      try { await api('/api/owner/logout', { method: 'POST' }); } catch {}
      window.location.href = '/owner/login';
    });
  }

  // ------------------------------------------------------------------
  // Report-an-issue modal — POSTs the owner's description to /api/owner/report_issue
  // which forwards it to the brain server's customer_error_report endpoint.
  // ------------------------------------------------------------------
  function setupReportIssue() {
    const btn      = document.getElementById('report-issue-btn');
    const modal    = document.getElementById('report-issue-modal');
    const cancel   = document.getElementById('report-issue-cancel');
    const send     = document.getElementById('report-issue-send');
    const text     = document.getElementById('report-issue-text');
    const result   = document.getElementById('report-issue-result');
    if (!btn || !modal) return;

    const open = () => {
      result.textContent = '';
      text.value = '';
      modal.hidden = false;
      modal.style.display = 'flex';
      text.focus();
    };
    const close = () => {
      modal.hidden = true;
      modal.style.display = 'none';
    };

    btn.addEventListener('click', open);
    cancel?.addEventListener('click', close);
    modal.addEventListener('click', (e) => {
      if (e.target === modal) close();
    });

    send?.addEventListener('click', async () => {
      const description = (text.value || '').trim();
      if (description.length < 5) {
        result.style.color = '#fbbf24';
        result.textContent = 'A few more words would help — what was Orby trying to do?';
        text.focus();
        return;
      }
      send.disabled = true;
      send.textContent = 'Sending...';
      try {
        const res = await api('/api/owner/report_issue', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            description,
            page_url: window.location.href,
            user_agent: navigator.userAgent.slice(0, 200),
          })
        });
        if (res && res.ok !== false) {
          result.style.color = '#4ade80';
          result.textContent = '✓ Sent. Frank usually responds within a few hours.';
          setTimeout(close, 1800);
        } else {
          throw new Error(res?.error || 'unknown');
        }
      } catch (e) {
        result.style.color = '#ef4444';
        result.textContent = "Couldn't send right now — try again or email Frank directly.";
      } finally {
        send.disabled = false;
        send.textContent = 'Send to Frank';
      }
    });
  }

  // ------------------------------------------------------------------
  // Messages tab
  // ------------------------------------------------------------------
  function setupMessageFilters() {
    document.querySelectorAll('.chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        document.querySelectorAll('.chip').forEach((c) => c.classList.remove('active'));
        chip.classList.add('active');
        activeMessageFilter = chip.dataset.filter;
        renderMessages();
      });
    });
  }

  async function loadMessages() {
    try {
      const data = await api('/api/owner/messages');
      messages = data.messages || [];
      renderMessages();
      const unread = messages.filter((m) => !m.read).length;
      const badge = document.getElementById('message-count');
      badge.textContent = unread > 0 ? unread : '';
      badge.dataset.count = unread;
    } catch (e) {
      console.warn('messages load failed', e);
    }
  }

  function renderMessages() {
    const list = document.getElementById('message-list');
    const filtered = filterMessages(messages, activeMessageFilter);
    if (filtered.length === 0) {
      list.innerHTML = emptyStateHtml(activeMessageFilter);
      return;
    }
    list.innerHTML = filtered.map(messageCardHtml).join('');
    list.querySelectorAll('[data-action="mark-read"]').forEach((btn) => {
      btn.addEventListener('click', () => markRead(btn.dataset.id));
    });
    list.querySelectorAll('[data-action="delete"]').forEach((btn) => {
      btn.addEventListener('click', () => deleteMessage(btn.dataset.id));
    });
  }

  function filterMessages(msgs, filter) {
    if (filter === 'all') return msgs;
    if (filter === 'new') return msgs.filter((m) => !m.read);
    if (filter === 'leads') return msgs.filter((m) => m.type === 'lead');
    if (filter === 'voicemails') return msgs.filter((m) => m.type === 'voicemail');
    if (filter === 'orders') return msgs.filter((m) => m.type === 'order');
    return msgs;
  }

  function emptyStateHtml(filter) {
    const titles = {
      all: 'No messages yet',
      new: 'No new messages',
      leads: 'No leads captured yet',
      voicemails: 'No voicemails',
      orders: 'No orders'
    };
    return `<div class="empty-state">
      <div class="empty-icon">&#x1F4ED;</div>
      <div class="empty-title">${titles[filter] || 'Nothing here'}</div>
      <div class="empty-sub">When customers reach out, you'll see them here.</div>
    </div>`;
  }

  function messageCardHtml(m) {
    const when = new Date((m.timestamp || 0) * 1000).toLocaleString();
    return `
      <div class="message-card ${m.read ? '' : 'unread'}">
        <div class="meta">
          <span class="type ${m.type}">${m.type || 'message'}</span>
          <span>${when}</span>
        </div>
        <div class="from">${esc(m.from_name || m.from_phone || 'Unknown')}${
          m.from_phone ? ` <span class="muted">· ${esc(m.from_phone)}</span>` : ''
        }</div>
        <div class="body">${esc(m.body || '')}</div>
        <div class="actions">
          ${m.read ? '' : `<button data-action="mark-read" data-id="${m.id}">Mark read</button>`}
          ${m.from_phone ? `<a href="tel:${esc(m.from_phone)}"><button>Call back</button></a>` : ''}
          <button data-action="delete" data-id="${m.id}">Delete</button>
        </div>
      </div>`;
  }

  async function markRead(id) {
    await api(`/api/owner/messages/${id}/read`, { method: 'POST' });
    loadMessages();
  }

  async function deleteMessage(id) {
    if (!confirm('Delete this message?')) return;
    await api(`/api/owner/messages/${id}`, { method: 'DELETE' });
    loadMessages();
  }

  // ------------------------------------------------------------------
  // Business Info tab
  // ------------------------------------------------------------------
  async function loadBusinessInfo() {
    try {
      businessInfo = await api('/api/owner/business_info');
      populateBusinessForm(businessInfo);
    } catch (e) {
      console.warn('business_info load failed', e);
    }
  }

  function populateBusinessForm(info) {
    const f = document.getElementById('business-form');
    f.name.value = info.name || '';
    f.tagline.value = info.tagline || '';
    f.description.value = info.description || '';
    f.phone.value = info.contact?.phone || '';
    f.email.value = info.contact?.email || '';
    f.website.value = info.contact?.website || '';
    f.address.value = formatAddress(info.address);

    renderHoursGrid(info.hours || {});
    renderFaqList(info.faq || []);
    renderServicesList(info.services || []);
  }

  function formatAddress(a) {
    if (!a) return '';
    return [a.street, a.city, [a.state, a.zip].filter(Boolean).join(' ')]
      .filter(Boolean).join(', ');
  }

  function renderHoursGrid(hours) {
    const grid = document.getElementById('hours-grid');
    const days = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday'];
    grid.innerHTML = days.map((d) => {
      const h = hours[d] || { open: '09:00', close: '17:00', closed: false };
      return `
        <div class="day-label">${d[0].toUpperCase() + d.slice(1)}</div>
        <input type="time" data-day="${d}" data-field="open" value="${h.open}" ${h.closed ? 'disabled' : ''}>
        <input type="time" data-day="${d}" data-field="close" value="${h.close}" ${h.closed ? 'disabled' : ''}>
        <label class="checkbox" style="margin:0;">
          <input type="checkbox" data-day="${d}" data-field="closed" ${h.closed ? 'checked' : ''}> Closed
        </label>`;
    }).join('');
    grid.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
      cb.addEventListener('change', (e) => {
        const day = e.target.dataset.day;
        grid.querySelectorAll(`input[data-day="${day}"][type="time"]`).forEach((t) => {
          t.disabled = e.target.checked;
        });
      });
    });
  }

  function renderFaqList(faqs) {
    const list = document.getElementById('faq-list');
    list.innerHTML = faqs.map((f, i) => faqItemHtml(f, i)).join('');
    attachRemovers(list, 'faq-item');
  }

  function faqItemHtml(f, i) {
    return `<div class="faq-item">
      <button type="button" class="remove" data-remove="faq" data-i="${i}">×</button>
      <input type="text" data-faq-field="question" data-i="${i}" placeholder="Question" value="${esc(f.question || '')}">
      <textarea data-faq-field="answer" data-i="${i}" rows="2" placeholder="Answer">${esc(f.answer || '')}</textarea>
    </div>`;
  }

  function renderServicesList(services) {
    const list = document.getElementById('services-list');
    list.innerHTML = services.map((s, i) => serviceItemHtml(s, i)).join('');
    attachRemovers(list, 'service-item');
  }

  function serviceItemHtml(s, i) {
    return `<div class="service-item">
      <button type="button" class="remove" data-remove="service" data-i="${i}">×</button>
      <input type="text" data-svc-field="name" data-i="${i}" placeholder="Name" value="${esc(s.name || '')}">
      <textarea data-svc-field="description" data-i="${i}" rows="2" placeholder="Description">${esc(s.description || '')}</textarea>
      <div class="row-2">
        <input type="number" step="0.01" data-svc-field="price_from" data-i="${i}" placeholder="Price from" value="${s.price_from ?? ''}">
        <input type="number" step="0.01" data-svc-field="price_to" data-i="${i}" placeholder="Price to" value="${s.price_to ?? ''}">
      </div>
    </div>`;
  }

  function attachRemovers(container, klass) {
    container.querySelectorAll('.remove').forEach((btn) => {
      btn.addEventListener('click', () => btn.closest('.' + klass).remove());
    });
  }

  document.addEventListener('click', (e) => {
    if (e.target.id === 'add-faq') {
      const list = document.getElementById('faq-list');
      const i = list.querySelectorAll('.faq-item').length;
      list.insertAdjacentHTML('beforeend', faqItemHtml({}, i));
      attachRemovers(list, 'faq-item');
    }
    if (e.target.id === 'add-service') {
      const list = document.getElementById('services-list');
      const i = list.querySelectorAll('.service-item').length;
      list.insertAdjacentHTML('beforeend', serviceItemHtml({}, i));
      attachRemovers(list, 'service-item');
    }
  });

  function setupBusinessForm() {
    const form = document.getElementById('business-form');
    form?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const status = document.getElementById('save-status');
      status.className = 'save-status'; status.textContent = 'Saving...';
      try {
        const payload = collectBusinessForm();
        await api('/api/owner/business_info', {
          method: 'PUT',
          body: JSON.stringify(payload)
        });
        status.textContent = 'Saved ✓';
        setTimeout(() => { status.textContent = ''; }, 2500);
      } catch (e) {
        status.classList.add('error');
        status.textContent = e.message;
      }
    });
  }

  function collectBusinessForm() {
    const f = document.getElementById('business-form');
    const hours = {};
    document.querySelectorAll('#hours-grid input').forEach((el) => {
      const day = el.dataset.day;
      if (!day) return;
      if (!hours[day]) hours[day] = { open: '09:00', close: '17:00', closed: false };
      if (el.dataset.field === 'closed') hours[day].closed = el.checked;
      else hours[day][el.dataset.field] = el.value;
    });

    const faq = [];
    document.querySelectorAll('#faq-list .faq-item').forEach((item) => {
      const q = item.querySelector('[data-faq-field="question"]').value;
      const a = item.querySelector('[data-faq-field="answer"]').value;
      if (q || a) faq.push({ question: q, answer: a });
    });

    const services = [];
    document.querySelectorAll('#services-list .service-item').forEach((item) => {
      const name = item.querySelector('[data-svc-field="name"]').value;
      const desc = item.querySelector('[data-svc-field="description"]').value;
      const pf = item.querySelector('[data-svc-field="price_from"]').value;
      const pt = item.querySelector('[data-svc-field="price_to"]').value;
      if (name) services.push({
        name, description: desc,
        price_from: pf ? parseFloat(pf) : null,
        price_to:   pt ? parseFloat(pt) : null
      });
    });

    return {
      name: f.name.value,
      tagline: f.tagline.value,
      description: f.description.value,
      contact: {
        phone: f.phone.value,
        email: f.email.value,
        website: f.website.value
      },
      address: parseAddress(f.address.value),
      hours, faq, services
    };
  }

  function parseAddress(s) {
    if (!s) return { street: '', city: '', state: '', zip: '' };
    const parts = s.split(',').map((x) => x.trim());
    const street = parts[0] || '';
    const city = parts[1] || '';
    const stateZip = (parts[2] || '').split(/\s+/);
    return { street, city, state: stateZip[0] || '', zip: stateZip[1] || '' };
  }

  // ------------------------------------------------------------------
  // Settings tab
  // ------------------------------------------------------------------
  async function loadSettings() {
    try {
      settings = await api('/api/owner/settings');
      populateSettingsForm(settings);
    } catch (e) {
      console.warn('settings load failed', e);
    }
  }

  function populateSettingsForm(s) {
    document.getElementById('tone-select').value = s.tone || 'friend';
    document.querySelector('[name="topics_to_avoid"]').value =
      (s.topics_to_avoid || []).join('\n');
    [
      'public_can_take_orders', 'public_can_book_appointments',
      'public_can_request_quotes', 'public_can_request_callbacks',
      'notify_on_new_lead', 'notify_on_new_message', 'notify_on_failed_billing',
      'owner_pwa_push', 'owner_email', 'owner_sms'
    ].forEach((field) => {
      const el = document.querySelector(`[name="${field}"]`);
      if (el) el.checked = !!s[field];
    });
  }

  function setupSettingsForm() {
    document.getElementById('save-settings-btn')?.addEventListener('click', async () => {
      const status = document.getElementById('settings-save-status');
      status.className = 'save-status'; status.textContent = 'Saving...';
      try {
        const payload = collectSettingsForm();
        await api('/api/owner/settings', {
          method: 'PUT',
          body: JSON.stringify(payload)
        });
        status.textContent = 'Saved ✓';
        setTimeout(() => { status.textContent = ''; }, 2500);
      } catch (e) {
        status.classList.add('error');
        status.textContent = e.message;
      }
    });

    document.getElementById('change-password-btn')?.addEventListener('click', async () => {
      const current = prompt('Current password:');
      if (!current) return;
      const next = prompt('New password (8+ characters):');
      if (!next || next.length < 8) { alert('Password must be at least 8 characters.'); return; }
      try {
        await api('/api/owner/change_password', {
          method: 'POST',
          body: JSON.stringify({ current, next })
        });
        alert('Password changed.');
      } catch (e) {
        alert(e.message);
      }
    });
  }

  function collectSettingsForm() {
    const payload = {
      tone: document.getElementById('tone-select').value,
      topics_to_avoid: document.querySelector('[name="topics_to_avoid"]').value
        .split('\n').map((s) => s.trim()).filter(Boolean)
    };
    [
      'public_can_take_orders', 'public_can_book_appointments',
      'public_can_request_quotes', 'public_can_request_callbacks',
      'notify_on_new_lead', 'notify_on_new_message', 'notify_on_failed_billing',
      'owner_pwa_push', 'owner_email', 'owner_sms'
    ].forEach((field) => {
      const el = document.querySelector(`[name="${field}"]`);
      if (el) payload[field] = el.checked;
    });
    return payload;
  }

  // ------------------------------------------------------------------
  // Push notifications — opt-in, fires when leads come in
  // ------------------------------------------------------------------
  async function setupPushNotifications() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
    // Register service worker if not already
    try {
      const reg = await navigator.serviceWorker.register('/pwa/service-worker.js');
      // Already subscribed?
      const sub = await reg.pushManager.getSubscription();
      if (sub) return;  // good
      // Get VAPID public key from server
      const keyRes = await fetch('/api/push/vapid_public_key');
      if (!keyRes.ok) return;  // push not configured on this install
      const { public_key } = await keyRes.json();
      // Ask permission (gently — only once per session)
      if (Notification.permission === 'default') {
        const granted = await Notification.requestPermission();
        if (granted !== 'granted') return;
      } else if (Notification.permission !== 'granted') {
        return;
      }
      // Subscribe
      const newSub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(public_key),
      });
      await fetch('/api/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newSub.toJSON()),
      });
      console.log('[Orbi] push subscribed');
    } catch (e) {
      console.warn('[Orbi] push setup failed:', e);
    }
  }

  function urlBase64ToUint8Array(base64) {
    const padding = '='.repeat((4 - (base64.length % 4)) % 4);
    const b64 = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(b64);
    return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
  }

  // ------------------------------------------------------------------
  // Owner chat tab — like ChatGPT, but knows the business
  // ------------------------------------------------------------------
  let ownerChatHistory = [];
  let ownerChatSending = false;

  function setupOwnerChat() {
    const input = document.getElementById('owner-chat-input');
    const send  = document.getElementById('owner-chat-send');
    const sugg  = document.getElementById('owner-chat-suggestions');

    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 140) + 'px';
      send.disabled = !input.value.trim();
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        sendOwnerChat(input.value);
      }
    });
    send.addEventListener('click', () => sendOwnerChat(input.value));

    sugg?.querySelectorAll('.quick-chip').forEach((chip) => {
      chip.addEventListener('click', () => sendOwnerChat(chip.dataset.text));
    });
  }

  async function sendOwnerChat(text) {
    text = (text || '').trim();
    if (!text || ownerChatSending) return;
    ownerChatSending = true;

    // ECHO FIX: stop recognition the INSTANT a message is submitted (text
    // OR voice). Without this, recognition auto-restarts during the
    // network roundtrip to /api/owner/chat, so the mic is hot when
    // Orbi's TTS starts playing the reply — and the mic captures her
    // own voice as if it were the user's next input. The 400ms-after-
    // speak restart in speakReply's finish() will turn it back on.
    try { if (typeof recognition !== 'undefined' && recognition) recognition.stop(); } catch {}
    try { if (typeof clearPendingSpeech === 'function') clearPendingSpeech(); } catch {}

    const input  = document.getElementById('owner-chat-input');
    const send   = document.getElementById('owner-chat-send');
    const msgs   = document.getElementById('owner-chat-messages');
    const state  = document.getElementById('owner-chat-state-bar');
    const welcome = msgs.querySelector('.owner-chat-welcome');

    welcome?.remove();
    addOwnerBubble('user', text);
    ownerChatHistory.push({ role: 'user', content: text });
    input.value = ''; input.style.height = 'auto';
    send.disabled = true;
    // Frank 2026-06-23: prime the audio session synchronously inside the
    // gesture that triggered this send (Send-button click, Enter key, or
    // voice-mode auto-click from speech recognition). Guarantees the
    // AudioContext is resumed BEFORE the LLM fetch returns, so the very
    // first reply has voice.
    try { _unlockAudio(); } catch {}

    state.hidden = false;
    state.className = 'owner-chat-state-bar thinking';
    state.textContent = 'Thinking...';
    const thinking = addOwnerThinking();

    try {
      // Email/inbox requests can take 60-90s on slow IMAP providers (Yahoo).
      // Everything else gets 45s. Use AbortController so we get a clean error
      // rather than a browser-level "failed to fetch" that shows offline mode.
      const isEmailMsg = /\b(?:email|inbox|gmail|yahoo mail|check mail|my mail)\b/i.test(text);
      const timeoutMs = isEmailMsg ? 120000 : 45000;
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      const res = await fetch('/api/owner/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          history: ownerChatHistory.slice(-30)
        }),
        signal: ctrl.signal
      });
      clearTimeout(timer);
      if (res.status === 401) { window.location.href = '/owner/login'; return; }
      const data = await res.json();
      thinking.remove();
      const reply = data.reply || "I couldn't reach any AI tier just now.";
      addOwnerBubble('assistant', reply, {
        tier: data.tier,
        source: data.source,
        download_url: data.download_url,
      });
      ownerChatHistory.push({ role: 'assistant', content: reply });
      if (window.__orbiSpeakReply) window.__orbiSpeakReply(reply);
    } catch (e) {
      thinking.remove();
      // Frank 2026-06-23: was 'I'm offline right now' which read robotic AND
      // wrong (the server might not be offline — just slow, e.g. image gen
      // hitting Cloudflare's 100s ceiling). Honest one-liner, no TTS read
      // (silence is better than reciting an error message aloud).
      const fallback = e.name === 'AbortError'
        ? "That took too long — Yahoo mail can be slow. Try again and it'll use the cached inbox."
        : "Something hung on my end — try that again.";
      addOwnerBubble('assistant', fallback, { tier: 'none' });
    } finally {
      state.hidden = true;
      ownerChatSending = false;
      send.disabled = !input.value.trim();
    }
  }

  function addOwnerBubble(role, text, opts = {}) {
    const msgs = document.getElementById('owner-chat-messages');
    const div = document.createElement('div');
    // 'huggingface' and 'local' are NORMAL tiers for the customer install —
    // only flag truly degraded states (no LLM reachable at all, or fast-path
    // fallbacks that the user might want to know about).
    const isOffline = opts.tier === 'none';
    div.className = 'owner-chat-bubble ' + role + (isOffline ? ' degraded' : '');
    div.textContent = text;
    // Inline preview for PNG-producing tools (image_gen / chart_gen).
    // Owner asked "draw me a picture" — they expect to SEE it in the chat,
    // not click a download link. Decks (pptx_gen) stay as link-only.
    if (opts.download_url && (opts.source === 'image_gen' || opts.source === 'chart_gen' || opts.source === 'ad_gen')) {
      const wrap = document.createElement('div');
      wrap.className = 'owner-chat-image-wrap';
      wrap.style.cssText = 'margin-top:8px;';
      const img = document.createElement('img');
      img.src = opts.download_url;
      img.alt = opts.source === 'chart_gen' ? 'chart' : 'generated image';
      img.style.cssText = 'max-width:100%;border-radius:8px;display:block;cursor:zoom-in;';
      img.addEventListener('click', () => window.open(opts.download_url, '_blank'));
      wrap.appendChild(img);
      div.appendChild(wrap);
    }
    if (isOffline) {
      const hint = document.createElement('div');
      hint.className = 'owner-chat-tier-hint';
      hint.textContent = '— offline mode —';
      div.appendChild(hint);
    }
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
    return div;
  }

  function addOwnerThinking() {
    const msgs = document.getElementById('owner-chat-messages');
    const div = document.createElement('div');
    div.className = 'owner-chat-bubble assistant';
    div.innerHTML = '<span class="owner-typing"><span></span><span></span><span></span></span>';
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
    return div;
  }

  // ==================================================================
  // PERSONAL ASSISTANT — My Day tab (calendar + tasks + reminders)
  // ==================================================================

  async function loadMyDay() {
    const today = new Date();
    document.getElementById('myday-today-label').textContent =
      today.toLocaleDateString(undefined, { weekday:'long', month:'short', day:'numeric' });
    await Promise.all([loadMyCalendar(), loadMyTasks(), loadMyReminders()]);
  }

  async function loadMyCalendar() {
    const list = document.getElementById('myday-calendar-list');
    try {
      const { events } = await api('/api/owner/pa/calendar');
      const today = new Date().toISOString().slice(0, 10);
      const todayEvents = (events || []).filter(e => (e.start || '').slice(0, 10) === today);
      if (!todayEvents.length) {
        list.innerHTML = '<div class="empty-state-small">Nothing on your calendar today.</div>';
        return;
      }
      list.innerHTML = todayEvents.map(e => `
        <div class="myday-item" data-id="${esc(e.id)}">
          <div class="myday-when">${esc((e.start || '').slice(11, 16))}</div>
          <div class="myday-text">${esc(e.title)}${e.location ? ` <span class="muted">@ ${esc(e.location)}</span>` : ''}</div>
          <button class="icon-btn-sm" data-action="delete-event" title="Remove">×</button>
        </div>
      `).join('');
      list.querySelectorAll('[data-action="delete-event"]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          const id = e.target.closest('[data-id]').dataset.id;
          await api(`/api/owner/pa/calendar/${id}`, { method: 'DELETE' });
          loadMyCalendar();
        });
      });
    } catch (err) {
      list.innerHTML = `<div class="empty-state-small">Couldn't load calendar (${esc(err.message)})</div>`;
    }
  }

  async function loadMyTasks() {
    const list = document.getElementById('myday-task-list');
    try {
      const { tasks } = await api('/api/owner/pa/tasks');
      document.getElementById('myday-task-count').textContent = `${tasks.length} open`;
      if (!tasks.length) {
        list.innerHTML = '<div class="empty-state-small">No open tasks.</div>';
        return;
      }
      list.innerHTML = tasks.map(t => `
        <div class="myday-item" data-id="${esc(t.id)}">
          <input type="checkbox" data-action="task-done" title="Mark done">
          <div class="myday-text">${esc(t.text)}</div>
          <button class="icon-btn-sm" data-action="task-delete" title="Remove">×</button>
        </div>
      `).join('');
      list.querySelectorAll('[data-action="task-done"]').forEach(box => {
        box.addEventListener('change', async (e) => {
          const id = e.target.closest('[data-id]').dataset.id;
          await api(`/api/owner/pa/tasks/${id}/done`, { method: 'POST' });
          loadMyTasks();
        });
      });
      list.querySelectorAll('[data-action="task-delete"]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          const id = e.target.closest('[data-id]').dataset.id;
          await api(`/api/owner/pa/tasks/${id}`, { method: 'DELETE' });
          loadMyTasks();
        });
      });
    } catch (err) {
      list.innerHTML = `<div class="empty-state-small">Couldn't load tasks</div>`;
    }
  }

  async function loadMyReminders() {
    const list = document.getElementById('myday-reminder-list');
    try {
      const { reminders } = await api('/api/owner/pa/reminders');
      document.getElementById('myday-reminder-count').textContent = `${reminders.length} pending`;
      if (!reminders.length) {
        list.innerHTML = '<div class="empty-state-small">No pending reminders.</div>';
        return;
      }
      list.innerHTML = reminders.map(r => `
        <div class="myday-item" data-id="${esc(r.id)}">
          <div class="myday-when">${esc((r.due || '').slice(0, 16).replace('T', ' '))}</div>
          <div class="myday-text">${esc(r.text)}</div>
          <button class="icon-btn-sm" data-action="reminder-done" title="Done">✓</button>
        </div>
      `).join('');
      list.querySelectorAll('[data-action="reminder-done"]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          const id = e.target.closest('[data-id]').dataset.id;
          await api(`/api/owner/pa/reminders/${id}/done`, { method: 'POST' });
          loadMyReminders();
        });
      });
    } catch (err) {
      list.innerHTML = `<div class="empty-state-small">Couldn't load reminders</div>`;
    }
  }

  function wireMyDayForms() {
    document.getElementById('myday-add-event')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const startISO = new Date(fd.get('start')).toISOString();
      await api('/api/owner/pa/calendar', {
        method: 'POST',
        body: JSON.stringify({ title: fd.get('title'), start: startISO }),
      });
      e.target.reset();
      loadMyCalendar();
    });
    document.getElementById('myday-add-task')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      await api('/api/owner/pa/tasks', {
        method: 'POST',
        body: JSON.stringify({ text: fd.get('text') }),
      });
      e.target.reset();
      loadMyTasks();
    });
    document.getElementById('myday-add-reminder')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const dueISO = new Date(fd.get('due')).toISOString();
      await api('/api/owner/pa/reminders', {
        method: 'POST',
        body: JSON.stringify({ text: fd.get('text'), due: dueISO }),
      });
      e.target.reset();
      loadMyReminders();
    });
  }

  // ==================================================================
  // CONTACTS tab
  // ==================================================================

  // Module-scoped state for contact selection (used by mail-merge)
  const selectedContactIds = new Set();
  let lastLoadedContacts = [];

  function updateMergeButton() {
    const btn = document.getElementById('contacts-merge-btn');
    if (!btn) return;
    const n = selectedContactIds.size;
    btn.textContent = `📨 Mail merge (${n})`;
    btn.disabled = n === 0;
  }

  async function loadContacts(query = '') {
    const list = document.getElementById('contacts-list');
    try {
      const { contacts } = await api(`/api/owner/pa/contacts${query ? '?q=' + encodeURIComponent(query) : ''}`);
      lastLoadedContacts = contacts || [];
      // Prune selections that no longer exist in the visible list
      const visibleIds = new Set(lastLoadedContacts.map(c => String(c.id)));
      for (const id of [...selectedContactIds]) {
        if (!visibleIds.has(String(id))) selectedContactIds.delete(id);
      }
      if (!contacts.length) {
        list.innerHTML = `<div class="empty-state-small">${query ? 'No matches' : 'No contacts yet'}</div>`;
        updateMergeButton();
        return;
      }
      list.innerHTML = contacts.map(c => {
        const checked = selectedContactIds.has(String(c.id)) ? 'checked' : '';
        return `
        <div class="contact-card" data-id="${esc(c.id)}">
          <input type="checkbox" class="contact-select" data-id="${esc(c.id)}" ${checked} title="Select for mail merge">
          <div class="contact-main">
            <div class="contact-name" data-action="contact-open" data-id="${esc(c.id)}" title="View thread">${esc(c.name)}${c.company ? ` <span class="muted">— ${esc(c.company)}</span>` : ''}</div>
            <div class="contact-meta">
              ${c.phone ? `<span>${esc(c.phone)}</span>` : ''}
              ${c.email ? `<span>${esc(c.email)}</span>` : ''}
              ${c.source && c.source !== 'manual' ? `<span class="tag">${esc(c.source)}</span>` : ''}
            </div>
            ${c.notes ? `<div class="contact-notes">${esc(c.notes)}</div>` : ''}
          </div>
          <button class="icon-btn-sm" data-action="contact-delete" title="Remove">×</button>
        </div>`;
      }).join('');

      list.querySelectorAll('[data-action="contact-delete"]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          const id = e.target.closest('[data-id]').dataset.id;
          if (!confirm('Remove this contact?')) return;
          await api(`/api/owner/pa/contacts/${id}`, { method: 'DELETE' });
          selectedContactIds.delete(String(id));
          loadContacts(document.getElementById('contacts-search').value);
        });
      });

      list.querySelectorAll('[data-action="contact-open"]').forEach(el => {
        el.addEventListener('click', (e) => {
          const id = e.currentTarget.dataset.id;
          openThreadDialog(id);
        });
      });

      list.querySelectorAll('.contact-select').forEach(cb => {
        cb.addEventListener('click', (e) => e.stopPropagation());
        cb.addEventListener('change', (e) => {
          const id = String(e.target.dataset.id);
          if (e.target.checked) selectedContactIds.add(id);
          else selectedContactIds.delete(id);
          updateMergeButton();
          // Sync "select all"
          const sa = document.getElementById('contacts-select-all');
          if (sa) {
            const all = list.querySelectorAll('.contact-select');
            const checkedEls = list.querySelectorAll('.contact-select:checked');
            sa.checked = all.length > 0 && all.length === checkedEls.length;
          }
        });
      });

      updateMergeButton();
    } catch (err) {
      list.innerHTML = `<div class="empty-state-small">Couldn't load contacts</div>`;
    }
  }

  function wireContacts() {
    let searchTimer;
    document.getElementById('contacts-search')?.addEventListener('input', (e) => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => loadContacts(e.target.value), 200);
    });
    document.getElementById('contacts-add-btn')?.addEventListener('click', () => {
      document.getElementById('contact-form').reset();
      document.getElementById('contact-dialog').showModal();
    });
    document.getElementById('contact-form')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      await api('/api/owner/pa/contacts', {
        method: 'POST',
        body: JSON.stringify({
          name: fd.get('name'), phone: fd.get('phone'),
          email: fd.get('email'), company: fd.get('company'),
          notes: fd.get('notes'),
        }),
      });
      document.getElementById('contact-dialog').close();
      loadContacts(document.getElementById('contacts-search').value);
    });

    // Select-all toggles every visible checkbox
    document.getElementById('contacts-select-all')?.addEventListener('change', (e) => {
      const checked = e.target.checked;
      document.querySelectorAll('#contacts-list .contact-select').forEach(cb => {
        cb.checked = checked;
        const id = String(cb.dataset.id);
        if (checked) selectedContactIds.add(id);
        else selectedContactIds.delete(id);
      });
      updateMergeButton();
    });

    // Open merge dialog
    document.getElementById('contacts-merge-btn')?.addEventListener('click', () => {
      if (selectedContactIds.size === 0) return;
      openMergeDialog();
    });

    // Map view — toggle Leaflet contact map in the panel above the list
    document.getElementById('contacts-map-btn')?.addEventListener('click', async () => {
      const panel = document.getElementById('contacts-map-panel');
      if (!panel) return;
      const visible = panel.style.display !== 'none';
      if (visible) {
        panel.style.display = 'none';
        document.getElementById('contacts-map-btn').textContent = '🗺 Map';
        return;
      }
      panel.style.display = 'block';
      document.getElementById('contacts-map-btn').textContent = '✕ Hide Map';
      if (panel.innerHTML.trim()) return; // already loaded
      panel.innerHTML = '<div style="padding:20px;color:#888">Loading map…</div>';
      try {
        const resp = await fetch('/api/owner/map', { credentials: 'include' });
        if (!resp.ok) throw new Error(await resp.text());
        const html = await resp.text();
        // Extract just the body content from the full page
        const bodyMatch = html.match(/<body[^>]*>([\s\S]*)<\/body>/i);
        panel.innerHTML = bodyMatch ? bodyMatch[1] : html;
      } catch (e) {
        panel.innerHTML = `<div style="padding:20px;color:#e74c3c">Could not load map: ${e.message}</div>`;
      }
    });

    wireMailMerge();
  }

  // ==================================================================
  // PEOPLE tab (owner-only — manage users + archive)
  // ==================================================================

  async function loadPeople() {
    try {
      const { users, archived } = await api('/api/owner/users');
      renderActiveUsers(users);
      renderArchivedUsers(archived);
    } catch (err) {
      document.getElementById('people-active-list').innerHTML =
        `<div class="empty-state-small">Couldn't load users (${esc(err.message)})</div>`;
    }
  }

  function renderActiveUsers(users) {
    const list = document.getElementById('people-active-list');
    if (!users || !users.length) {
      list.innerHTML = '<div class="empty-state-small">No active users.</div>';
      return;
    }
    list.innerHTML = users.map(u => `
      <div class="person-card" data-username="${esc(u.username)}">
        <div class="person-main">
          <div class="person-name">${esc(u.display_name || u.username)}</div>
          <div class="person-meta">
            <span class="tag tag-${esc(u.role)}">${esc(u.role)}</span>
            <span class="muted">@${esc(u.username)}</span>
            <span class="muted">since ${esc((u.created_at || '').slice(0, 10))}</span>
          </div>
        </div>
        ${u.role !== 'owner' ? `<button class="secondary-btn" data-action="deactivate">Deactivate</button>` : '<span class="muted">protected</span>'}
      </div>
    `).join('');
    list.querySelectorAll('[data-action="deactivate"]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        const u = e.target.closest('[data-username]').dataset.username;
        const reason = prompt(
          `Deactivate ${u}? Their data archives for 90 days then auto-purges.\n\n` +
          `Reason (optional — e.g. "leave of absence", "departed company"):`
        );
        if (reason === null) return;   // owner clicked Cancel
        try {
          await api(`/api/owner/users/${u}/deactivate`, {
            method: 'POST',
            body: JSON.stringify({ reason })
          });
          loadPeople();
        } catch (err) { alert(err.message); }
      });
    });
  }

  function renderArchivedUsers(archived) {
    const list = document.getElementById('people-archived-list');
    if (!archived || !archived.length) {
      list.innerHTML = '<div class="empty-state-small">No archived users.</div>';
      return;
    }
    list.innerHTML = archived.map(a => `
      <div class="person-card archived" data-username="${esc(a.username)}">
        <div class="person-main">
          <div class="person-name">${esc(a.username)}</div>
          <div class="person-meta">
            <span class="muted">archived ${esc((a.archived_at || '').slice(0, 10))}</span>
            <span class="muted">purges ${esc((a.purge_after || '').slice(0, 10))}</span>
            ${a.hold ? '<span class="tag">HOLD</span>' : ''}
          </div>
          <div class="person-summary">
            ${Object.entries(a.summary || {}).map(([k, v]) => `<span class="tag">${esc(k)}: ${esc(v)}</span>`).join('')}
          </div>
        </div>
        <div class="person-actions">
          <button class="primary-btn" data-action="reactivate">Reactivate</button>
          <button class="secondary-btn" data-action="transfer">Transfer</button>
          <button class="secondary-btn" data-action="hold-${a.hold ? 'off' : 'on'}">${a.hold ? 'Release Hold' : 'Hold'}</button>
        </div>
      </div>
    `).join('');
    list.querySelectorAll('[data-action="reactivate"]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        const u = e.target.closest('[data-username]').dataset.username;
        if (!confirm(`Reactivate ${u}? Their archived data will be restored and they'll be able to log in again with their existing password.`)) return;
        try {
          await api(`/api/owner/users/${encodeURIComponent(u)}/reactivate`, { method: 'POST' });
          loadPeople();
        } catch (err) { alert(err.message); }
      });
    });
    list.querySelectorAll('[data-action^="hold"]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        const u = e.target.closest('[data-username]').dataset.username;
        const setHold = e.target.dataset.action === 'hold-on';
        await api(`/api/owner/users/${u}/hold`, {
          method: 'POST',
          body: JSON.stringify({ hold: setHold }),
        });
        loadPeople();
      });
    });
    list.querySelectorAll('[data-action="transfer"]').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const u = e.target.closest('[data-username]').dataset.username;
        openTransferDialog(u, archived.find(a => a.username === u));
      });
    });
  }

  function openTransferDialog(username, archiveData) {
    const dlg = document.getElementById('transfer-dialog');
    document.getElementById('transfer-title').textContent = `Transfer from ${username}`;
    document.getElementById('transfer-subtitle').textContent =
      `Pick what to claim. Items move to your folder; the rest stays in archive until purge.`;
    const summary = archiveData.summary || {};
    document.getElementById('transfer-summary').innerHTML = `
      <p>To transfer items, use the API for now (one source at a time):</p>
      <ul>${Object.entries(summary).map(([src, count]) => `
        <li><code>POST /api/owner/users/${esc(username)}/transfer/${esc(currentUsername || 'owner')}</code>
          with <code>{"source":"${esc(src)}.json","ids":[…]}</code> — ${esc(count)} item(s) available</li>
      `).join('')}</ul>
      <p class="muted">A full per-item picker UI is a future enhancement; the JSON files in <code>${esc(archiveData.folder)}</code> show the item ids.</p>
    `;
    dlg.showModal();
  }

  function wirePeople() {
    document.getElementById('people-add-btn')?.addEventListener('click', () => {
      document.getElementById('user-form').reset();
      document.getElementById('user-dialog').showModal();
    });
    document.getElementById('user-form')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      try {
        await api('/api/owner/users', {
          method: 'POST',
          body: JSON.stringify({
            username: fd.get('username'),
            display_name: fd.get('display_name'),
            password: fd.get('password'),
            role: fd.get('role'),
          }),
        });
        document.getElementById('user-dialog').close();
        loadPeople();
      } catch (err) { alert(err.message); }
    });
  }

  // ==================================================================
  // Tab-load wiring: each PA tab loads on first activation
  // ==================================================================
  let currentUsername = null;
  let currentRole = null;
  let myDayLoaded = false, contactsLoaded = false, peopleLoaded = false;

  let marketingLoaded = false;
  let legalLoaded = false;
  let contractorLoaded = false;

  document.addEventListener('DOMContentLoaded', async () => {
    // Discover who's logged in (role determines People-tab visibility)
    try {
      const sess = await api('/api/owner/status');
      currentUsername = sess.username || null;
      currentRole = sess.role || null;
      if (currentRole === 'owner') {
        document.querySelectorAll('.owner-only').forEach(el => el.hidden = false);
      }
    } catch {}

    // Discover entitlements — show paid-add-on tabs / buttons if active.
    try {
      const health = await fetch('/health').then(r => r.json());
      const mods = (health.enabled_modules || []).map(m => String(m).toLowerCase());
      if (mods.includes('marketing')) {
        document.querySelectorAll('.module-marketing').forEach(el => el.hidden = false);
      }
      if (mods.includes('marketing_image')) {
        document.querySelectorAll('.module-marketing-image').forEach(el => el.hidden = false);
      }
      if (mods.includes('legal')) {
        document.querySelectorAll('.module-legal').forEach(el => el.hidden = false);
      }
      if (mods.includes('contractor')) {
        document.querySelectorAll('.module-contractor').forEach(el => el.hidden = false);
      }
    } catch {}

    wireMyDayForms();
    wireContacts();
    wirePeople();
    wireMarketing();
    wireLegal();
    wireContractor();

    document.querySelectorAll('.tab').forEach(t => {
      t.addEventListener('click', () => {
        const which = t.dataset.tab;
        if (which === 'myday' && !myDayLoaded)     { loadMyDay();   myDayLoaded = true; }
        if (which === 'contacts' && !contactsLoaded) { loadContacts(); contactsLoaded = true; }
        if (which === 'people' && !peopleLoaded)   { loadPeople();  peopleLoaded = true; }
        if (which === 'marketing' && !marketingLoaded) { loadMarketingLibrary(); marketingLoaded = true; }
        if (which === 'legal'    && !legalLoaded)    { loadLegal();           legalLoaded = true; }
        if (which === 'contractor' && !contractorLoaded) { loadContractor(); contractorLoaded = true; }
      });
    });
  });

  // ==================================================================
  // VOICE MODE — one button toggles mic + speaker together
  // Tap on  → Orbi listens to you AND reads her replies aloud
  // Tap off → text-only
  // The stop button (square) appears while she's speaking — tap to interrupt
  // ==================================================================

  let voiceOn = false;
  // Separate from voiceOn — owner can have Orby READ HER REPLIES aloud
  // without turning on mic listening. Persisted in localStorage so it
  // stays on across sessions.
  // Default ON. Frank 2026-06-23: PurBlum's embed widget defaults speaker
  // ON and customers expect the dashboard chat to behave the same way.
  // Only treat a saved '0' as off; missing/anything-else = on.
  let speakRepliesOn = (function() {
    try { return localStorage.getItem('orbi_speak_replies') !== '0'; }
    catch { return true; }
  })();
  // Match the My Orby voice used on twickell.com.
  const ORBI_TTS_VOICE = 'en-US-AvaNeural';
  // One-time banner suggesting Chrome on iOS Safari users. Apple's
  // standalone PWA audio sandbox blocks Orby's voice; Chrome's shortcut
  // opens as a regular tab and works fully. Banner dismissible.
  function _showIosChromeBanner() {
    try {
      const ua = navigator.userAgent || '';
      const isIos = /iPad|iPhone|iPod/.test(ua) || (/Mac/.test(ua) && 'ontouchend' in document);
      const isSafari = isIos && /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS/.test(ua);
      const isStandalone = window.matchMedia('(display-mode: standalone)').matches
        || window.navigator.standalone === true;
      const dismissed = localStorage.getItem('orbi_ios_chrome_dismissed') === '1';
      if (!isIos || !isSafari || dismissed) return;
      const banner = document.createElement('div');
      banner.style.cssText = 'position:fixed;bottom:12px;left:12px;right:12px;'
        + 'background:#8b5cf6;color:white;padding:12px 14px;border-radius:10px;'
        + 'z-index:9999;font-size:13px;line-height:1.4;'
        + 'box-shadow:0 6px 24px rgba(0,0,0,0.4)';
      banner.innerHTML =
        '<div style="display:flex;gap:10px;align-items:flex-start">'
        + '<div style="flex:1">'
          + '<b>Heads up:</b> for voice on iPhone, use Orby in <b>Chrome</b>'
          + ' (not Safari). Apple restricts audio in Safari home-screen apps.'
        + '</div>'
        + '<button type="button" style="background:rgba(0,0,0,0.2);color:white;'
          + 'border:none;border-radius:6px;padding:4px 8px;cursor:pointer"'
          + ' id="_orbi-iossafari-dismiss">Got it</button>'
        + '</div>';
      document.body.appendChild(banner);
      banner.querySelector('#_orbi-iossafari-dismiss').addEventListener('click', () => {
        banner.remove();
        try { localStorage.setItem('orbi_ios_chrome_dismissed', '1'); } catch {}
      });
    } catch {}
  }
  document.addEventListener('DOMContentLoaded', _showIosChromeBanner);

  // iOS PWAs need a "user gesture" context to play audio. Once we play
  // ANY audio inside a click handler, the persistent Audio element is
  // "unlocked" for subsequent .play() calls without further gestures.
  let _persistentAudio = null;

  // speechSynthesis fallback — used when Audio() rejects (iOS PWA block,
  // network failure, etc). iOS loads voices asynchronously: the first
  // getVoices() call sometimes returns []. We pre-warm + retry on
  // voiceschanged.
  let _ssVoices = [];
  function _refreshSpeechVoices() {
    if (window.speechSynthesis && window.speechSynthesis.getVoices) {
      _ssVoices = window.speechSynthesis.getVoices() || [];
    }
  }
  if (window.speechSynthesis) {
    _refreshSpeechVoices();
    window.speechSynthesis.onvoiceschanged = _refreshSpeechVoices;
  }
  function _speakViaSpeechSynthesis(text, onDone) {
    if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) {
      setVoiceState('Voice playback unavailable.', 'error');
      setTimeout(() => { try { setVoiceState(null); } catch {} }, 4000);
      onDone && onDone();
      return;
    }
    // Voices may still be empty on iOS at this moment — give it 1 retry
    if (!_ssVoices.length) _refreshSpeechVoices();
    try {
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(text);
      u.rate = 1.05;
      u.pitch = 1.0;
      u.lang = 'en-US';
      // Prefer a US English female voice that matches Ava as closely as
      // possible. Karen/Moira/Tessa/Nicky are Australian/NZ/UK voices —
      // explicitly EXCLUDED so Windows installs that lack better voices
      // don't get a wrong-accent fallback. Order matters: try US voices
      // first, fall back to en-* female only as a last resort, never to
      // a non-female or non-English voice.
      const blocked = /david|mark|daniel|alex|fred|tom|guy|brian|andrew|male/i;
      const enUS = _ssVoices.filter(v => /^en-US/i.test(v.lang) && !blocked.test(v.name));
      const female = enUS.find(v => /samantha|ava|aria|nora|jenny|zira|female/i.test(v.name))
                  || _ssVoices.find(v => /samantha|ava|aria|nora|jenny|zira|female/i.test(v.name) && /^en/i.test(v.lang) && !blocked.test(v.name));
      if (female) u.voice = female;
      u.onend = onDone || (() => {});
      u.onerror = () => {
        setVoiceState('Voice playback unavailable — text reply still works.', 'error');
        setTimeout(() => { try { setVoiceState(null); } catch {} }, 4000);
        onDone && onDone();
      };
      window.speechSynthesis.speak(u);
    } catch (e) {
      setVoiceState('Voice playback unavailable.', 'error');
      setTimeout(() => { try { setVoiceState(null); } catch {} }, 4000);
      onDone && onDone();
    }
  }
  // Persistent unlock element is only needed in iOS standalone PWA mode
  // (Safari "Add to Home Screen" — Apple's strictest sandbox). For Chrome
  // on iPhone, Chrome shortcut on iPhone, Android Chrome, desktop, and
  // Safari browser tabs, fresh new Audio() per reply works fine and is
  // the documented happy path. Using the persistent element on those
  // platforms caused subsequent plays to fail silently.
  function _isIosStandalonePwa() {
    const ua = navigator.userAgent || '';
    const isIos = /iPad|iPhone|iPod/.test(ua) || (/Mac/.test(ua) && 'ontouchend' in document);
    const standalone = window.matchMedia('(display-mode: standalone)').matches
      || window.navigator.standalone === true;
    return isIos && standalone;
  }
  function _isMobile() {
    return /iPhone|iPad|iPod|Android/.test(navigator.userAgent || '');
  }
  // Universal audio unlock — the FIRST tap or touch ANYWHERE on the
  // page unlocks audio playback for the whole session. iOS/Android
  // browsers require an active user gesture before any audio plays;
  // this guarantees that gesture happens early no matter what the
  // user actually clicks.
  // Frank 2026-06-23: flake fix. Was removing the listener after the first
  // tap, which broke after iOS suspended the AudioContext (tab switch,
  // backgrounding, idle). Subsequent gestures had no rehook, so Orby went
  // silent until enough button toggles accidentally tripped a different
  // code path. Now: every user gesture re-runs _unlockAudio (idempotent,
  // cheap — silent WAV + ctx.resume()). Keeps the session armed for the
  // life of the page.
  function _keepAudioPrimed() {
    const handler = () => { try { _unlockAudio(); } catch {} };
    document.addEventListener('click', handler, { capture: true, passive: true });
    document.addEventListener('touchstart', handler, { capture: true, passive: true });
    document.addEventListener('keydown', handler, { capture: true, passive: true });
  }
  _keepAudioPrimed();
  // Mobile browsers (iOS Safari AND iOS Chrome AND Android Chrome) all
  // require a user gesture to unlock audio playback for the session.
  // Without the unlock, the first Audio().play() in a fetch callback
  // (i.e. inside speakReply triggered by an LLM response) gets blocked.
  // We unlock by playing a silent WAV inside the user's tap on either
  // toggle button — that "primes" the audio session. Subsequent fresh
  // Audio() plays work without further gestures.
  let _audioUnlocked = false;
  let _audioCtx = null;
  let _currentAudioSource = null;   // Web Audio source node currently playing
  function _ensureAudioCtx() {
    if (_audioCtx) return _audioCtx;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    try { _audioCtx = new AC(); } catch { return null; }
    return _audioCtx;
  }
  // Frank 2026-06-23: in voice mode there are no keystrokes, so the
  // _keepAudioPrimed gesture listeners never fire between Orby's replies.
  // iOS suspends the AudioContext after ~10s of silence, and the next
  // reply lands on a suspended context with no fresh gesture to revive
  // it. Heartbeat: once the context has been resumed by a real gesture,
  // play a 1-sample silent buffer every 3 seconds. iOS treats this as
  // continuous output and keeps the context 'running' for the whole
  // session. Cost: ~0.0001ms of CPU and zero audible output.
  let _heartbeatTimer = null;
  function _startAudioHeartbeat() {
    if (_heartbeatTimer || !_audioCtx) return;
    _heartbeatTimer = setInterval(() => {
      const ctx = _audioCtx;
      if (!ctx || ctx.state !== 'running') return;
      try {
        const buf = ctx.createBuffer(1, 1, 22050);
        const src = ctx.createBufferSource();
        src.buffer = buf;
        src.connect(ctx.destination);
        src.start(0);
      } catch {}
    }, 3000);
  }
  function _unlockAudio() {
    // Resume the AudioContext inside the user gesture. Once resumed, it
    // stays "running" for the whole session and we can play arbitrary
    // audio from any callback context (fetch responses, timers, etc.)
    // without needing another user gesture. This is the only iOS-Chrome-
    // PWA-compatible way to autoplay chat replies.
    const ctx = _ensureAudioCtx();
    if (ctx && ctx.state === 'suspended') {
      ctx.resume().then(() => { _audioUnlocked = true; _startAudioHeartbeat(); }).catch(() => {});
    } else if (ctx) {
      _audioUnlocked = true;
      _startAudioHeartbeat();
    }
    // Also play a silent WAV via plain <audio> as a belt-and-suspenders
    // unlock — some browsers prefer one path or the other.
    if (_isMobile()) {
      try {
        const a = new Audio();
        a.preload = 'auto';
        a.playsInline = true;
        a.setAttribute('playsinline', '');
        a.src = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
        a.play().catch(() => {});
      } catch {}
    }
  }

  function setSpeakReplies(on) {
    speakRepliesOn = !!on;
    try { localStorage.setItem('orbi_speak_replies', on ? '1' : '0'); } catch {}
    const btn = document.getElementById('owner-speaker-toggle');
    if (btn) {
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.title = on
        ? "Reading Orby's replies out loud (on) — tap to mute"
        : "Read Orby's replies out loud (off) — tap to turn on";
      btn.style.background = on ? '#8b5cf6' : 'transparent';
      btn.style.color      = on ? 'white'    : '#9aa4c0';
      btn.style.borderColor = on ? '#8b5cf6' : '#2c3756';
    }
    // Unlock iOS audio on the user gesture (this click event)
    if (on) _unlockAudio();
  }
  document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('owner-speaker-toggle');
    if (btn) {
      btn.addEventListener('click', () => setSpeakReplies(!speakRepliesOn));
      setSpeakReplies(speakRepliesOn);   // apply initial state styling
    }
    // Voice diagnostic test button — bypasses chat flow, plays a fixed
    // greeting directly. If this works, audio path is fine and the chat
    // wiring is the issue. If it doesn't, browser is blocking audio.
    const testBtn = document.getElementById('voice-test-btn');
    const debugEl = document.getElementById('voice-debug');
    function debug(msg, kind) {
      if (!debugEl) return;
      debugEl.style.display = 'block';
      debugEl.style.color = kind === 'err' ? '#ff7a7a' : (kind === 'ok' ? '#4ade80' : '#9aa4c0');
      debugEl.textContent = msg;
    }
    if (testBtn) {
      testBtn.addEventListener('click', async () => {
        const ua = navigator.userAgent || '';
        const standalone = window.matchMedia('(display-mode: standalone)').matches
          || window.navigator.standalone === true;
        debug(`Testing audio... UA: ${(/iPhone/.test(ua) ? 'iPhone' : /Android/.test(ua) ? 'Android' : 'Desktop')}, standalone PWA: ${standalone}`, 'info');
        // Try server /tts first
        const url = '/tts?voice=' + encodeURIComponent(ORBI_TTS_VOICE)
          + '&text=' + encodeURIComponent("Hi Frank, this is Orby. Can you hear me?");

        // After playback, restore mic if voice mode was on — iOS routes
        // audio to OUTPUT during playback and the SpeechRecognition can
        // get stuck. Rebuild + restart gives it a fresh input channel.
        const restoreMic = () => {
          if (!voiceOn || !wantsListening) return;
          setTimeout(() => {
            try {
              if (window._orbiRebuildRecognition) window._orbiRebuildRecognition();
            } catch {}
            try { safeStartMic(); } catch {}
          }, 400);
        };

        try {
          const a = new Audio(url);
          a.playsInline = true;
          a.setAttribute('playsinline', '');
          a.onerror = () => { debug('❌ Audio() error event fired — /tts may have failed', 'err'); restoreMic(); };
          a.onended = () => { debug('✓ Audio played end-to-end. Voice path works.', 'ok'); restoreMic(); };
          const p = a.play();
          if (p && typeof p.then === 'function') {
            p.then(() => debug('▶ Server audio started playing — you should hear Orby now', 'ok'))
              .catch((err) => {
                debug(`❌ Server audio rejected: ${err.name}. I will not use the browser's built-in voice.`, 'err');
                restoreMic();
              });
          }
        } catch (e) {
          debug(`❌ Audio() throw: ${e.message}`, 'err');
          restoreMic();
        }
      });
    }
  });
  let recognition = null;
  let isListening = false;
  let isSpeaking = false;
  let wantsListening = false;
  let restartTimer = null;
  let speechSendTimer = null;
  let speechBuffer = '';
  let currentAudio = null;
  let currentAudioUrl = null;
  const VOICE_SILENCE_BEFORE_SEND_MS = 700;
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;

  function setupOwnerVoice() {
    const btn = document.getElementById('owner-voice-toggle');
    const stopBtn = document.getElementById('owner-stop-speaking');
    if (!btn) return;

    if (!Recognition) {
      btn.disabled = true;
      btn.title = 'Voice not supported in this browser';
      btn.style.opacity = 0.4;
      return;
    }

    btn.addEventListener('click', () => {
      // Frank 2026-06-23: tapping Voice = "open a full conversation with
      // Orby." Always inside the user gesture, do all three things at once:
      //   1. Unlock iOS audio session (must be in this gesture)
      //   2. Turn the speaker ON so her replies actually play
      //   3. Toggle mic mode (the on/off behavior of this button)
      // Speaker stays sticky after voice mode is later turned off — if
      // Frank only wanted text + voice in this moment, he can mute the
      // speaker separately. Not auto-muting on voice-off avoids surprising
      // typed-chat users who liked the speaker on.
      _unlockAudio();
      const turningOn = !voiceOn;
      if (turningOn && !speakRepliesOn) setSpeakReplies(true);
      setVoiceMode(turningOn);
    });
    stopBtn.addEventListener('click', stopSpeaking);

    // Wrapped in a function so we can RECREATE the recognition object
    // after Orby finishes speaking — iOS Safari often refuses subsequent
    // .start() on the same instance once audio has played in the session.
    // Recreating is the workaround.
    function _buildRecognition() {
      const r = new Recognition();
      r.lang = 'en-US';
      r.continuous = true;
      r.interimResults = true;
      r.onstart = recognition?.onstart;
      r.onend = recognition?.onend;
      r.onerror = recognition?.onerror;
      r.onresult = recognition?.onresult;
      return r;
    }
    window._orbiRebuildRecognition = function() {
      try { recognition && recognition.stop(); } catch {}
      recognition = _buildRecognition();
    };

    recognition = new Recognition();
    recognition.lang = 'en-US';
    recognition.continuous = true;
    recognition.interimResults = true;

    recognition.onstart = () => {
      isListening = true;
      btn.classList.add('listening');
      setVoiceState('Listening — speak any time...', 'listening');
    };
    recognition.onend = () => {
      isListening = false;
      btn.classList.remove('listening');
      // Chrome stops mic after silence — auto-restart if user still wants it on
      if (wantsListening && !isSpeaking) {
        clearTimeout(restartTimer);
        restartTimer = setTimeout(() => {
          if (wantsListening && !isSpeaking) safeStartMic();
        }, 300);
      } else {
        setVoiceState(null);
      }
    };
    recognition.onerror = (e) => {
      if (e.error === 'aborted' || e.error === 'no-speech') return;
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        setVoiceMode(false);
        alert('Microphone permission denied. Allow microphone access for this site to use voice.');
      }
    };
    recognition.onresult = (event) => {
      let finalText = '';
      let interimText = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        if (event.results[i].isFinal) finalText += event.results[i][0].transcript;
        else interimText += event.results[i][0].transcript;
      }
      if (finalText.trim()) {
        speechBuffer = (speechBuffer + ' ' + finalText.trim()).trim();
        scheduleVoiceSend();
      }
      const preview = (speechBuffer + ' ' + interimText.trim()).trim();
      if (preview) setVoiceState(`Listening — ${preview}`, 'listening');
    };
  }

  function scheduleVoiceSend() {
    clearTimeout(speechSendTimer);
    speechSendTimer = setTimeout(() => {
      const text = speechBuffer.trim();
      speechBuffer = '';
      if (!text || isSpeaking) return;
      const input = document.getElementById('owner-chat-input');
      if (!input) return;
      input.value = text;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      document.getElementById('owner-chat-send')?.click();
    }, VOICE_SILENCE_BEFORE_SEND_MS);
  }

  function clearPendingSpeech() {
    clearTimeout(speechSendTimer);
    speechSendTimer = null;
    speechBuffer = '';
  }

  function setVoiceMode(on) {
    voiceOn = on;
    const btn = document.getElementById('owner-voice-toggle');
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.title = 'Voice mode (' + (on ? 'on — tap to turn off' : 'off — tap to turn on') + ')';
    if (on) {
      wantsListening = true;
      safeStartMic();
    } else {
      wantsListening = false;
      clearPendingSpeech();
      stopSpeaking();
      try { recognition && recognition.stop(); } catch {}
      setVoiceState(null);
    }
  }

  function safeStartMic() {
    if (!recognition || isListening || isSpeaking) return;
    try { recognition.start(); }
    catch (e) {
      // InvalidStateError happens when recognition "thinks" it's already
      // started. Force-stop and retry once after a short pause.
      if (e && e.name === 'InvalidStateError') {
        try { recognition.stop(); } catch {}
        setTimeout(() => {
          if (wantsListening && !isSpeaking && !isListening) {
            try { recognition.start(); } catch {}
          }
        }, 500);
      }
    }
  }

  // (Watchdog removed — was firing every 1.5s and fighting the natural
  // recognition lifecycle. Simpler flow now: speakReply stops mic when
  // Orby starts talking, finish() restarts it when she ends. No polling.)

  // Uses server-side /tts endpoint (edge_tts, en-US-AvaNeural by default
  // — same voice as orbi_test on twickell.com). Falls back to browser
  // synthesis only if the server endpoint fails.
  //
  // Speaks when EITHER:
  //   - voiceOn is true (full voice mode — mic listening + reply spoken)
  //   - speakRepliesOn is true (just speaker — owner types but wants
  //                              Orby to read replies aloud)
  async function speakReply(text) {
    if ((!voiceOn && !speakRepliesOn) || !text) return;
    stopSpeaking();
    clearPendingSpeech();
    isSpeaking = true;
    // Mute the mic while Orbi talks so we don't echo-loop her own voice
    try {
      if (recognition && typeof recognition.abort === 'function') recognition.abort();
      else if (recognition) recognition.stop();
    } catch {}
    document.getElementById('owner-stop-speaking').hidden = false;
    setVoiceState('Orbi is speaking...', 'speaking');

    const cleanText = stripForSpeech(text);

    let _finishCalled = false;
    const finish = () => {
      if (_finishCalled) return;
      _finishCalled = true;
      isSpeaking = false;
      if (currentAudioUrl) {
        URL.revokeObjectURL(currentAudioUrl);
        currentAudioUrl = null;
      }
      currentAudio = null;
      _currentAudioSource = null;
      document.getElementById('owner-stop-speaking').hidden = true;
      setVoiceState(null);
      // Echo guard — short wait so the audio tail doesn't get picked up
      // by the mic. On mobile, recreate the recognition object first:
      // iOS routes audio differently after playback and a plain
      // recognition.start() silently no-ops — start() resolves but
      // onstart never fires and no audio is captured. Rebuilding is
      // the proven workaround (Test button uses the same trick).
      if (wantsListening) {
        setTimeout(() => {
          if (!wantsListening || isSpeaking) return;
          if (_isMobile()) {
            try { window._orbiRebuildRecognition && window._orbiRebuildRecognition(); } catch {}
          }
          safeStartMic();
        }, 400);
      }
    };

    // (Removed safety timeout — onended/onerror/promise.catch should fire
    // reliably enough that we don't need a forced finish. The complexity
    // was causing timing conflicts with the rebuild logic.)

    const url = '/tts?voice=' + encodeURIComponent(ORBI_TTS_VOICE)
      + '&text=' + encodeURIComponent(cleanText);

    // Strategy 1 (preferred): Web Audio API. Once AudioContext is resumed
    // inside a user gesture (handled by _unlockAudio on first tap), it
    // stays "running" for the whole session and can play arbitrary audio
    // from any callback — including fetch responses like /tts replies.
    // This is the ONLY reliable autoplay path on iOS Chrome PWA.
    const ctx = _ensureAudioCtx();
    // If iOS suspended the context between the last gesture and now,
    // try a non-gesture resume. It frequently succeeds when the user's
    // tap on Send is still inside the browser's "recent activation"
    // window. Costs nothing if it fails — we fall through to Strategy 2.
    if (ctx && ctx.state === 'suspended') {
      try { await ctx.resume(); } catch {}
    }
    if (ctx && ctx.state === 'running') {
      try {
        const res = await fetch(url);
        if (!res.ok) throw new Error('tts ' + res.status);
        const arr = await res.arrayBuffer();
        const decoded = await new Promise((resolve, reject) => {
          // Use callback form for Safari compatibility (older webkit
          // AudioContext returns nothing from the Promise overload).
          ctx.decodeAudioData(arr, resolve, reject);
        });
        const source = ctx.createBufferSource();
        source.buffer = decoded;
        source.connect(ctx.destination);
        source.onended = finish;
        _currentAudioSource = source;
        currentAudio = source;   // so stopSpeaking can pause it
        source.start(0);
        return;
      } catch (err) {
        console.warn('[Orbi] Web Audio path failed, falling back:', err);
        // Fall through to plain Audio() below.
      }
    }

    // Strategy 2 (fallback): plain <audio> element. Works on desktop and
    // in user-gesture contexts on mobile. Same path the Test button uses.
    try {
      currentAudio = new Audio(url);
      currentAudio.preload = 'auto';
      currentAudio.playsInline = true;
      currentAudio.setAttribute('playsinline', '');
      currentAudio.onended = finish;
      currentAudio.onerror = finish;
      const playPromise = currentAudio.play();
      if (playPromise && typeof playPromise.catch === 'function') {
        playPromise.catch((err) => {
          console.warn('[Orbi] Audio() blocked or failed:', err);
          setVoiceState('Voice playback unavailable — text reply still works.', 'error');
          setTimeout(() => { try { setVoiceState(null); } catch {} }, 5000);
          finish();
        });
      }
    } catch (err) {
      console.warn('[Orbi] /tts failed:', err);
      setVoiceState('Voice playback unavailable — text reply still works.', 'error');
      setTimeout(() => { try { setVoiceState(null); } catch {} }, 5000);
      finish();
    }
  }

  function stopSpeaking() {
    if (_currentAudioSource) {
      try { _currentAudioSource.onended = null; _currentAudioSource.stop(0); } catch {}
      _currentAudioSource = null;
    }
    if (currentAudio && typeof currentAudio.pause === 'function') {
      try { currentAudio.pause(); currentAudio.src = ''; } catch {}
    }
    if (window.speechSynthesis) {
      try { window.speechSynthesis.cancel(); } catch {}
    }
    if (currentAudioUrl) {
      URL.revokeObjectURL(currentAudioUrl);
      currentAudioUrl = null;
    }
    currentAudio = null;
    isSpeaking = false;
    const stopBtn = document.getElementById('owner-stop-speaking');
    if (stopBtn) stopBtn.hidden = true;
  }

  // Strip markdown/links/code-fences before sending text to TTS so we don't
  // hear "asterisk asterisk" or read URLs character-by-character.
  function stripForSpeech(text) {
    return String(text || '')
      .replace(/```[\s\S]*?```/g, ' code block ')
      .replace(/`([^`]+)`/g, '$1')
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')   // markdown link → just the label
      // Strip LLM stage directions like "(pause)", "(whispers)", "(softly)",
      // "(excited)", "(sigh)" — TTS reads these literally otherwise.
      .replace(/\(\s*(pause|pauses|paused|breath|breathe|sigh|sighs|laugh|laughs|whisper|whispers|softly|loudly|slowly|quickly|excited|gentle|gently|smile|smiles|warm|warmly)\s*\)/gi, '')
      // Strip square-bracket source citations like "[Tahoe Tourism Board]" that
      // the LLM sometimes invents — they sound weird out loud.
      .replace(/\[[^\]]+\]/g, '')
      .replace(/[*_#>~]/g, '')
      .replace(/https?:\/\/\S+/g, 'a link')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function setVoiceState(text, cls) {
    const bar = document.getElementById('owner-voice-state');
    if (!bar) return;
    if (!text) { bar.hidden = true; bar.textContent = ''; bar.className = 'owner-voice-state'; return; }
    bar.hidden = false;
    bar.textContent = text;
    bar.className = 'owner-voice-state ' + (cls || '');
  }

  // Expose for the chat-send code path (which is inside the original IIFE)
  window.__orbiSpeakReply = speakReply;

  document.addEventListener('DOMContentLoaded', setupOwnerVoice);


  // ==================================================================
  // GOOGLE CALENDAR — Settings tab integration row
  // ==================================================================

  async function loadGcalStatus() {
    const statusEl   = document.getElementById('gcal-status');
    const connectBtn = document.getElementById('gcal-connect-btn');
    const disconBtn  = document.getElementById('gcal-disconnect-btn');
    const syncBtn    = document.getElementById('gcal-sync-btn');
    if (!statusEl) return;
    try {
      const s = await api('/api/owner/gcal/status');
      if (s.connected) {
        statusEl.innerHTML = `Connected as <strong>${esc(s.email || '')}</strong>`
          + (s.last_sync ? ` · last sync ${esc(s.last_sync.slice(0,16).replace('T',' '))}` : '')
          + (s.last_error ? ` · <span style="color:#ffb0b0">${esc(s.last_error.slice(0,60))}</span>` : '')
          + (s.events_count ? ` · ${s.events_count} events (${s.gcal_events||0} from Google)` : '');
        connectBtn.hidden = true;
        disconBtn.hidden  = false;
        syncBtn.hidden    = false;
      } else {
        statusEl.textContent = 'Not connected. Click Connect to link your Google Calendar — '
          + 'events flow both ways.';
        connectBtn.hidden = false;
        disconBtn.hidden  = true;
        syncBtn.hidden    = true;
      }
    } catch (err) {
      statusEl.innerHTML = `<span style="color:#ffb0b0">Could not check status (${esc(err.message)})</span>`;
    }
  }

  function wireGcal() {
    const connectBtn = document.getElementById('gcal-connect-btn');
    const disconBtn  = document.getElementById('gcal-disconnect-btn');
    const syncBtn    = document.getElementById('gcal-sync-btn');
    const statusEl   = document.getElementById('gcal-status');
    if (!connectBtn) return;

    connectBtn.addEventListener('click', async () => {
      try {
        const r = await api('/api/owner/gcal/connect', { method: 'POST' });
        if (r.auth_url) {
          // Open Google's consent page. After approval, Google redirects back
          // to /api/owner/gcal/callback which then sends user to /owner#gcal.
          window.location.href = r.auth_url;
        } else {
          alert('Could not start connection: ' + (r.error || 'unknown'));
        }
      } catch (err) {
        alert('Connect failed: ' + err.message);
      }
    });

    disconBtn.addEventListener('click', async () => {
      if (!confirm('Disconnect Google Calendar? Events already pulled stay in Orbi; new changes stop syncing.')) return;
      try {
        await api('/api/owner/gcal/disconnect', { method: 'POST' });
        loadGcalStatus();
      } catch (err) {
        alert('Disconnect failed: ' + err.message);
      }
    });

    syncBtn.addEventListener('click', async () => {
      const label = syncBtn.textContent;
      syncBtn.disabled = true;
      syncBtn.textContent = 'Syncing…';
      try {
        const r = await api('/api/owner/gcal/sync_now', { method: 'POST' });
        statusEl.innerHTML = `Synced: pulled ${r.pulled||0} new, pushed ${r.pushed||0}`;
        setTimeout(loadGcalStatus, 1200);
      } catch (err) {
        statusEl.innerHTML = `<span style="color:#ffb0b0">Sync failed: ${esc(err.message)}</span>`;
      } finally {
        syncBtn.disabled = false;
        syncBtn.textContent = label;
      }
    });

    // Auto-load whenever Settings tab is opened
    document.querySelectorAll('.tab').forEach(t => {
      if (t.dataset.tab === 'settings') {
        t.addEventListener('click', loadGcalStatus);
      }
    });
    // Also load now, and if URL hash is #gcal (post-OAuth-callback redirect)
    loadGcalStatus();
    if (window.location.hash === '#gcal') {
      // Pop them to the settings tab so they see the result
      const settingsTab = document.querySelector('.tab[data-tab="settings"]');
      if (settingsTab) settingsTab.click();
    }
  }

  document.addEventListener('DOMContentLoaded', wireGcal);


  // ==================================================================
  // FILES tab — upload + drag/drop + list + delete
  // ==================================================================

  function fmtBytes(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
    return (n/(1024*1024)).toFixed(1) + ' MB';
  }

  async function loadFiles() {
    const list = document.getElementById('files-list');
    if (!list) return;
    try {
      const r = await api('/api/owner/workspace');
      const files = r.files || [];
      if (!files.length) {
        list.innerHTML = `<div class="empty-state-small">No files uploaded yet. Drop something above to get started.</div>
          <p class="muted" style="margin-top:8px;font-size:12px">Folder on your computer: <code>${esc(r.path||'')}</code></p>`;
        return;
      }
      list.innerHTML = `
        <p class="muted" style="margin:0 0 10px;font-size:12px">${files.length} file(s) · Folder: <code>${esc(r.path||'')}</code></p>
        ${files.map(f => `
          <div class="file-row" data-name="${esc(f.name||'')}">
            <div class="file-icon">${esc(fileEmoji(f.name||''))}</div>
            <div class="file-body">
              <div class="file-name">${esc(f.name||'(unnamed)')}</div>
              <div class="file-meta">
                ${esc(fmtBytes(f.size||0))} ·
                ${f.indexed_chars ? (f.indexed_chars + ' chars indexed') : 'not indexed'} ·
                ${f.mtime ? new Date(f.mtime*1000).toLocaleDateString() : ''}
              </div>
            </div>
            <button class="secondary-btn" data-action="file-convert" title="Clean &amp; convert to another format">✨ Convert</button>
            <button class="icon-btn-sm" data-action="file-delete" title="Remove">×</button>
          </div>
        `).join('')}
      `;
      list.querySelectorAll('[data-action="file-delete"]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          const name = e.target.closest('[data-name]').dataset.name;
          if (!confirm(`Remove "${name}" from your workspace? Orbi will forget about it.`)) return;
          try {
            await api('/api/owner/workspace/' + encodeURIComponent(name), { method: 'DELETE' });
            loadFiles();
          } catch (err) { alert('Delete failed: ' + err.message); }
        });
      });
      list.querySelectorAll('[data-action="file-convert"]').forEach(btn => {
        btn.addEventListener('click', (e) => {
          const name = e.target.closest('[data-name]').dataset.name;
          openConvertDialog(name);
        });
      });
    } catch (err) {
      list.innerHTML = `<div class="empty-state-small">Couldn't load files (${esc(err.message)})</div>`;
    }
  }

  function fileEmoji(name) {
    const ext = (name.split('.').pop() || '').toLowerCase();
    return ({
      pdf:'📄', docx:'📝', doc:'📝', txt:'📃', md:'📋',
      csv:'📊', xlsx:'📊', xls:'📊',
      html:'🌐', htm:'🌐', json:'⚙', log:'📜',
      png:'🖼', jpg:'🖼', jpeg:'🖼', gif:'🖼',
    })[ext] || '📁';
  }

  async function uploadFiles(fileList) {
    const status = document.getElementById('files-upload-status');
    if (!fileList || !fileList.length) return;
    const fd = new FormData();
    for (const f of fileList) fd.append('files', f);
    status.hidden = false;
    status.className = 'files-upload-status uploading';
    status.textContent = `Uploading ${fileList.length} file(s)…`;
    try {
      const res = await fetch('/api/owner/workspace/upload', {
        method: 'POST',
        body: fd,
        credentials: 'same-origin',
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const okN = (data.saved || []).length;
      const badN = (data.rejected || []).length;
      let msg = `✓ Uploaded ${okN}`;
      if (data.indexed) msg += `, ${data.indexed} indexed for search`;
      if (badN) {
        const reasons = data.rejected.map(r => `${r.name} (${r.reason})`).join('; ');
        msg += ` · ${badN} rejected: ${reasons}`;
        status.className = 'files-upload-status partial';
      } else {
        status.className = 'files-upload-status ok';
      }
      status.textContent = msg;
      loadFiles();
      setTimeout(() => { status.hidden = true; }, 6000);
    } catch (err) {
      status.className = 'files-upload-status err';
      status.textContent = 'Upload failed: ' + err.message;
    }
  }

  function wireFiles() {
    const dz = document.getElementById('files-dropzone');
    const input = document.getElementById('files-input');
    const pickBtn = document.getElementById('files-pick-btn');
    if (!dz || !input) return;

    pickBtn?.addEventListener('click', () => input.click());
    dz.addEventListener('click', () => input.click());
    input.addEventListener('change', () => {
      uploadFiles(input.files);
      input.value = ''; // allow re-pick of same file
    });

    ['dragenter','dragover'].forEach(ev => {
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('drag-over'); });
    });
    ['dragleave','drop'].forEach(ev => {
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('drag-over'); });
    });
    dz.addEventListener('drop', (e) => {
      e.preventDefault();
      const files = e.dataTransfer?.files;
      if (files && files.length) uploadFiles(files);
    });

    // Auto-load on tab click
    document.querySelectorAll('.tab').forEach(t => {
      if (t.dataset.tab === 'files') {
        t.addEventListener('click', loadFiles);
      }
    });
  }

  document.addEventListener('DOMContentLoaded', wireFiles);


  // ==================================================================
  // Clean & Convert — LLM-cleaned version of a file, in target format
  // ==================================================================

  let convertSourceName = null;

  function openConvertDialog(name) {
    convertSourceName = name;
    const dlg = document.getElementById('convert-dialog');
    document.getElementById('convert-title').textContent = `Clean & convert: ${name}`;
    document.getElementById('convert-status').innerHTML = '';
    document.getElementById('convert-go').disabled = false;
    document.getElementById('convert-go').textContent = 'Clean & Convert';

    // Pre-select PDF for prose-y files, xlsx for tabular files
    const ext = (name.split('.').pop() || '').toLowerCase();
    const target = document.getElementById('convert-target');
    if (['csv','xlsx','xls'].includes(ext)) {
      target.value = 'xlsx';
    } else {
      target.value = 'pdf';
    }
    dlg.showModal();
  }

  function wireConvert() {
    const form = document.getElementById('convert-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!convertSourceName) return;
      const fd = new FormData(form);
      const payload = {
        target: fd.get('target'),
        hint:   (fd.get('hint') || '').toString().trim(),
        clean:  fd.get('clean') === 'on',
      };
      const statusEl = document.getElementById('convert-status');
      const goBtn = document.getElementById('convert-go');
      goBtn.disabled = true;
      goBtn.textContent = 'Working… (5-30s for LLM cleanup)';
      statusEl.innerHTML = '<span style="color:#4f8cff">Cleaning and converting…</span>';
      try {
        const res = await fetch(
          '/api/owner/workspace/' + encodeURIComponent(convertSourceName) + '/convert',
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            credentials: 'same-origin',
          }
        );
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.error || `HTTP ${res.status}`);
        }
        const data = await res.json();
        statusEl.innerHTML = `<span style="color:#6fdc94">✓ Saved as <strong>${esc(data.output_name)}</strong></span>`
          + (data.download_url
            ? ` · <a href="${esc(data.download_url)}" download style="color:#4f8cff">Download</a>`
            : '');
        goBtn.textContent = 'Done — convert another?';
        goBtn.disabled = false;
        loadFiles();
      } catch (err) {
        statusEl.innerHTML = `<span style="color:#ffb0b0">✗ ${esc(err.message)}</span>`;
        goBtn.disabled = false;
        goBtn.textContent = 'Try again';
      }
    });
  }

  document.addEventListener('DOMContentLoaded', wireConvert);


  // ==================================================================
  // GENERIC CONNECTORS — auto-render all registered connectors in Settings
  // ==================================================================

  const CONNECTOR_ICONS = {
    gmail:          {emoji: '✉️',  color: '#ea4335'},
    outlook:        {emoji: '📧',  color: '#0078d4'},
    google_reviews: {emoji: '⭐',  color: '#4285f4'},
    yelp:           {emoji: '★',   color: '#d32323'},
    stripe:         {emoji: '💳',  color: '#635bff'},
    slack:          {emoji: '💬',  color: '#4a154b'},
    notion:         {emoji: '📝',  color: '#000000'},
  };

  async function loadConnectors() {
    const list = document.getElementById('connectors-list');
    if (!list) return;
    try {
      const r = await api('/api/owner/connectors');
      const items = r.connectors || [];
      if (!items.length) {
        list.innerHTML = '<div class="empty-state-small">No additional integrations available yet.</div>';
        return;
      }
      list.innerHTML = items.map(c => renderConnectorRow(c)).join('');
      list.querySelectorAll('[data-action]').forEach(btn => {
        btn.addEventListener('click', (e) => handleConnectorAction(e.currentTarget));
      });
    } catch (err) {
      list.innerHTML = `<div class="empty-state-small">Couldn't load integrations (${esc(err.message)})</div>`;
    }
  }

  function renderConnectorRow(c) {
    const icon = CONNECTOR_ICONS[c.id] || {emoji: '🔌', color: '#666'};
    const isConnected = !!c.connected;
    const isApiKey = c.auth_kind === 'api_key';
    const statusLine = isConnected
      ? `Connected${c.account ? ' as <strong>'+esc(c.account)+'</strong>' : ''}`
        + (c.last_sync ? ` · last sync ${esc(c.last_sync.slice(0,16).replace('T',' '))}` : '')
        + (c.last_error ? ` · <span style="color:#ffb0b0">${esc(c.last_error.slice(0,60))}</span>` : '')
      : esc(c.blurb || 'Not connected.');
    return `
      <div class="integration-row" data-connector-id="${esc(c.id)}">
        <div class="integration-icon" style="background:${icon.color};color:#fff;font-size:18px;line-height:40px;text-align:center">${icon.emoji}</div>
        <div class="integration-body">
          <div class="integration-title">${esc(c.label || c.id)}</div>
          <div class="integration-status">${statusLine}</div>
        </div>
        <div class="integration-actions">
          ${isConnected
            ? `<button class="secondary-btn" data-action="${isApiKey ? 'edit-key' : 'reconnect'}" data-id="${esc(c.id)}">${isApiKey ? 'Update Key' : 'Reconnect'}</button>
               <button class="secondary-btn" data-action="disconnect" data-id="${esc(c.id)}">Disconnect</button>`
            : `<button class="primary-btn" data-action="${isApiKey ? 'show-key-dialog' : 'oauth-connect'}" data-id="${esc(c.id)}">Connect</button>`
          }
        </div>
      </div>
    `;
  }

  async function handleConnectorAction(btn) {
    const id = btn.dataset.id;
    const action = btn.dataset.action;
    if (action === 'oauth-connect' || action === 'reconnect') {
      try {
        const r = await api(`/api/owner/connectors/${id}/connect`, { method: 'POST' });
        if (r.auth_url) window.location.href = r.auth_url;
        else alert('Could not start connection: ' + JSON.stringify(r));
      } catch (err) { alert('Connect failed: ' + err.message); }
    } else if (action === 'show-key-dialog' || action === 'edit-key') {
      openApiKeyDialog(id);
    } else if (action === 'disconnect') {
      if (!confirm('Disconnect this integration?')) return;
      try {
        await api(`/api/owner/connectors/${id}/disconnect`, { method: 'POST' });
        loadConnectors();
      } catch (err) { alert('Disconnect failed: ' + err.message); }
    }
  }

  async function openApiKeyDialog(connectorId) {
    // Fetch connector metadata so we can show the setup steps the connector specifies
    let connector = null;
    try {
      const r = await api('/api/owner/connectors');
      connector = (r.connectors || []).find(c => c.id === connectorId);
    } catch {}
    const dlg = document.getElementById('apikey-dialog');
    document.getElementById('apikey-title').textContent =
      `Connect ${connector?.label || connectorId}`;
    document.getElementById('apikey-blurb').textContent = connector?.blurb || '';
    // Setup steps are pulled from the connector class metadata via /connectors
    // (the base.status() helper doesn't currently expose requires_owner_setup;
    // we surface that via a separate endpoint — fall back to no steps if absent).
    const stepsEl = document.getElementById('apikey-setup-steps');
    stepsEl.innerHTML = connector?.requires_owner_setup?.length
      ? '<ol style="margin:0 0 10px 16px;padding:0;color:#b8c6e0;font-size:13px">' +
        connector.requires_owner_setup.map(s => `<li>${esc(s)}</li>`).join('') + '</ol>'
      : '';
    document.getElementById('apikey-input').value = '';
    document.getElementById('apikey-status').textContent = '';
    document.getElementById('apikey-save-btn').dataset.connectorId = connectorId;

    // Show extra fields (some connectors need both api_key AND a business_id, etc.)
    const extra = document.getElementById('apikey-extra-fields');
    extra.innerHTML = '';
    if (connectorId === 'yelp') {
      extra.innerHTML = `
        <label style="margin-top:8px;display:block">Yelp business ID
          <input type="text" name="business_id" placeholder="the slug after /biz/ in your Yelp URL" required>
        </label>`;
    }
    dlg.showModal();
  }

  function wireApiKeyDialog() {
    const form = document.getElementById('apikey-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const id = document.getElementById('apikey-save-btn').dataset.connectorId;
      const fd = new FormData(form);
      const payload = { key: (fd.get('key') || '').toString().trim() };
      // Pull any extra fields rendered for this connector
      document.querySelectorAll('#apikey-extra-fields input').forEach(inp => {
        payload[inp.name] = inp.value;
      });
      const statusEl = document.getElementById('apikey-status');
      const saveBtn = document.getElementById('apikey-save-btn');
      saveBtn.disabled = true;
      saveBtn.textContent = 'Saving…';
      statusEl.innerHTML = '<span style="color:#4f8cff">Verifying key…</span>';
      try {
        const r = await api(`/api/owner/connectors/${id}/save_key`, {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        statusEl.innerHTML = `<span style="color:#6fdc94">✓ Saved</span>`;
        setTimeout(() => {
          document.getElementById('apikey-dialog').close();
          loadConnectors();
          saveBtn.disabled = false;
          saveBtn.textContent = 'Save';
        }, 800);
      } catch (err) {
        statusEl.innerHTML = `<span style="color:#ffb0b0">${esc(err.message)}</span>`;
        saveBtn.disabled = false;
        saveBtn.textContent = 'Try again';
      }
    });
  }

  function wireConnectors() {
    wireApiKeyDialog();
    // Auto-load on Settings tab click
    document.querySelectorAll('.tab').forEach(t => {
      if (t.dataset.tab === 'settings') {
        t.addEventListener('click', loadConnectors);
      }
    });
    loadConnectors();
    if (window.location.hash === '#integrations') {
      const tab = document.querySelector('.tab[data-tab="settings"]');
      if (tab) tab.click();
    }
  }

  document.addEventListener('DOMContentLoaded', wireConnectors);


  // ==================================================================
  // UNIVERSAL SEARCH — top-bar input, debounced, all-sources
  // ==================================================================

  let searchTimer = null;
  function wireTopbarSearch() {
    const input = document.getElementById('topbar-search-input');
    const dropdown = document.getElementById('topbar-search-results');
    if (!input || !dropdown) return;

    input.addEventListener('input', () => {
      clearTimeout(searchTimer);
      const q = input.value.trim();
      if (q.length < 2) { dropdown.hidden = true; return; }
      searchTimer = setTimeout(() => runTopbarSearch(q), 250);
    });
    input.addEventListener('focus', () => {
      if (input.value.trim().length >= 2 && dropdown.children.length) {
        dropdown.hidden = false;
      }
    });
    document.addEventListener('click', (e) => {
      if (!e.target.closest('.topbar-search')) dropdown.hidden = true;
    });
  }

  async function runTopbarSearch(query) {
    const dropdown = document.getElementById('topbar-search-results');
    dropdown.innerHTML = '<div class="search-loading">Searching…</div>';
    dropdown.hidden = false;
    try {
      const r = await api(`/api/owner/search?q=${encodeURIComponent(query)}&limit=3`);
      const total = r.total_hits || 0;
      if (!total) {
        dropdown.innerHTML = '<div class="search-empty">No matches across any source.</div>';
        return;
      }
      const sections = [];
      for (const [src, hits] of Object.entries(r.by_source || {})) {
        if (!hits || !hits.length) continue;
        sections.push(`
          <div class="search-section">
            <div class="search-section-head">${esc(hits[0].source_label || src)}</div>
            ${hits.map(h => `
              <a class="search-hit" ${h.link ? `href="${esc(h.link)}"` : ''}>
                <div class="search-hit-title">${esc(h.title || '(no title)')}</div>
                ${h.snippet ? `<div class="search-hit-snippet">${esc(h.snippet)}</div>` : ''}
              </a>
            `).join('')}
          </div>
        `);
      }
      const errMsg = Object.entries(r.errors || {}).filter(([k,v]) => v).map(([k,v]) => `${k}: ${v}`).join(', ');
      dropdown.innerHTML = sections.join('')
        + (errMsg ? `<div class="search-errors">Some sources failed: ${esc(errMsg)}</div>` : '');
    } catch (err) {
      dropdown.innerHTML = `<div class="search-empty">Search failed: ${esc(err.message)}</div>`;
    }
  }
  document.addEventListener('DOMContentLoaded', wireTopbarSearch);


  // ==================================================================
  // Settings → Public booking widget
  // ==================================================================

  async function loadBookingSettings() {
    const enabled = document.getElementById('booking-enabled');
    const urlRow  = document.getElementById('booking-url-row');
    const urlIn   = document.getElementById('booking-url');
    const dur     = document.getElementById('booking-duration');
    const days    = document.getElementById('booking-days-ahead');
    if (!enabled) return;
    try {
      const cfg = await api('/api/owner/booking/config');
      enabled.checked = !!cfg.enabled;
      dur.value  = cfg.duration_minutes || 30;
      days.value = cfg.days_ahead || 14;
      if (currentUsername) {
        const origin = window.location.origin;
        urlIn.value = `${origin}/book?u=${encodeURIComponent(currentUsername)}`;
      }
      urlRow.hidden = !cfg.enabled;
    } catch (err) {
      console.warn('booking config load failed', err);
    }
  }

  function wireBookingSettings() {
    const enabledEl = document.getElementById('booking-enabled');
    const urlRow    = document.getElementById('booking-url-row');
    const saveBtn   = document.getElementById('booking-save-btn');
    const copyBtn   = document.getElementById('booking-copy-btn');
    const statusEl  = document.getElementById('booking-save-status');
    if (!enabledEl) return;

    enabledEl.addEventListener('change', () => { urlRow.hidden = !enabledEl.checked; });
    copyBtn?.addEventListener('click', async () => {
      const urlIn = document.getElementById('booking-url');
      try {
        await navigator.clipboard.writeText(urlIn.value);
        copyBtn.textContent = '✓ Copied';
        setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500);
      } catch { urlIn.select(); }
    });
    saveBtn?.addEventListener('click', async () => {
      const payload = {
        enabled: enabledEl.checked,
        duration_minutes: parseInt(document.getElementById('booking-duration').value, 10) || 30,
        days_ahead: parseInt(document.getElementById('booking-days-ahead').value, 10) || 14,
      };
      try {
        await api('/api/owner/booking/config', {
          method: 'PUT', body: JSON.stringify(payload),
        });
        statusEl.textContent = '✓ Saved';
        statusEl.style.color = '#6fdc94';
        setTimeout(() => { statusEl.textContent = ''; }, 2000);
        loadBookingSettings();
      } catch (err) {
        statusEl.textContent = 'Save failed: ' + err.message;
        statusEl.style.color = '#ffb0b0';
      }
    });
    document.querySelectorAll('.tab').forEach(t => {
      if (t.dataset.tab === 'settings') t.addEventListener('click', loadBookingSettings);
    });
    setTimeout(loadBookingSettings, 800);
  }
  document.addEventListener('DOMContentLoaded', wireBookingSettings);


  // ==================================================================
  // Settings → Style learner (refresh corpus + show status)
  // ==================================================================

  async function loadStyleStatus() {
    const el = document.getElementById('style-status');
    if (!el) return;
    try {
      const s = await api('/api/owner/style/status');
      const count = s.count || 0;
      const last  = s.last_indexed || s.last_refresh || '';
      if (!count) {
        el.innerHTML = 'No style corpus yet. Click below to read your sent emails and learn your voice. ' +
                       '(Connect Gmail or Outlook in Integrations first.)';
      } else {
        el.innerHTML = `Corpus: <strong>${count}</strong> messages indexed` +
                       (last ? ` · last refresh ${esc(last.slice(0,16).replace('T',' '))}` : '');
      }
    } catch (err) {
      el.textContent = 'Style status unavailable.';
    }
  }

  function wireStyleLearner() {
    const btn = document.getElementById('style-refresh-btn');
    const statusEl = document.getElementById('style-refresh-status');
    if (!btn) return;
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.textContent = 'Reading your sent mail…';
      statusEl.textContent = '';
      try {
        const r = await api('/api/owner/style/refresh', { method: 'POST' });
        statusEl.textContent = `✓ Indexed ${r.indexed || 0} messages`;
        statusEl.style.color = '#6fdc94';
        loadStyleStatus();
      } catch (err) {
        statusEl.textContent = 'Refresh failed: ' + err.message;
        statusEl.style.color = '#ffb0b0';
      } finally {
        btn.disabled = false;
        btn.textContent = '🎓 Refresh from my sent mail';
      }
    });
    document.querySelectorAll('.tab').forEach(t => {
      if (t.dataset.tab === 'settings') t.addEventListener('click', loadStyleStatus);
    });
    setTimeout(loadStyleStatus, 1000);
  }
  document.addEventListener('DOMContentLoaded', wireStyleLearner);


  // ==================================================================
  // MORNING BRIEFING — banner at top of Messages tab
  // ==================================================================

  async function loadBriefingBanner() {
    const banner = document.getElementById('briefing-banner');
    const summaryEl = document.getElementById('briefing-banner-summary');
    if (!banner) return;
    try {
      const r = await api('/api/owner/briefing/now');
      const summary = r.summary_text || '';
      if (!summary || summary.length < 30) { banner.hidden = true; return; }
      summaryEl.textContent = summary;
      banner.hidden = false;
    } catch (err) {
      banner.hidden = true;
    }
  }

  function wireBriefingBanner() {
    document.getElementById('briefing-banner-refresh')?.addEventListener('click', loadBriefingBanner);
    document.getElementById('briefing-banner-collapse')?.addEventListener('click', () => {
      document.getElementById('briefing-banner').hidden = true;
    });
    loadBriefingBanner();
  }
  document.addEventListener('DOMContentLoaded', wireBriefingBanner);


  // ==================================================================
  // FOLLOW-UP — stale items needing attention
  // ==================================================================

  async function loadFollowUp() {
    const card = document.getElementById('follow-up-card');
    const list = document.getElementById('follow-up-list');
    const count = document.getElementById('follow-up-count');
    if (!card) return;
    try {
      const r = await api('/api/owner/follow_up');
      const items = r.items || [];
      if (!items.length) { card.hidden = true; return; }
      card.hidden = false;
      count.textContent = `${items.length} item${items.length === 1 ? '' : 's'}`;
      list.innerHTML = items.map((it, idx) => `
        <div class="follow-up-row" data-idx="${idx}">
          <div class="follow-up-meta">
            <span class="follow-up-source">${esc(it.source)}</span>
            <span class="follow-up-age">${it.days_stale}d</span>
            ${(it.tags||[]).map(t => `<span class="tag tag-${esc(t)}">${esc(t)}</span>`).join('')}
          </div>
          <div class="follow-up-title">${esc(it.title)}</div>
          <div class="follow-up-from muted">from ${esc(it.from)} · suggested: ${esc(it.suggested_action)}</div>
          <button class="secondary-btn" data-action="draft-nudge">✍ Draft a nudge</button>
          <div class="follow-up-draft" data-draft-for="${idx}" hidden></div>
        </div>
      `).join('');
      list.querySelectorAll('[data-action="draft-nudge"]').forEach((btn, i) => {
        btn.addEventListener('click', async () => {
          const idx = btn.closest('[data-idx]').dataset.idx;
          const draftEl = list.querySelector(`[data-draft-for="${idx}"]`);
          btn.disabled = true; btn.textContent = 'Drafting…';
          try {
            const r2 = await api('/api/owner/follow_up/draft', {
              method: 'POST', body: JSON.stringify(items[idx])
            });
            draftEl.textContent = r2.text || '(no draft)';
            draftEl.hidden = false;
            btn.textContent = '✓ Drafted';
          } catch (err) {
            draftEl.textContent = 'Draft failed: ' + err.message;
            draftEl.hidden = false;
            btn.disabled = false; btn.textContent = '✍ Draft a nudge';
          }
        });
      });
    } catch (err) {
      card.hidden = true;
    }
  }
  document.addEventListener('DOMContentLoaded', loadFollowUp);


  // ==================================================================
  // VOICEMAILS — own tab
  // ==================================================================

  async function loadVoicemails() {
    const list = document.getElementById('voicemails-list');
    const badge = document.getElementById('voicemail-count');
    const hint = document.getElementById('voicemails-empty-hint');
    if (!list) return;
    try {
      const r = await api('/api/owner/voicemails');
      const vms = r.voicemails || r.items || [];  // tolerate either field name
      const unhandled = vms.filter(v => !v.handled).length;
      if (badge) {
        if (unhandled) { badge.textContent = unhandled; badge.hidden = false; }
        else badge.hidden = true;
      }
      if (!vms.length) {
        list.innerHTML = '';
        if (hint) hint.hidden = false;
        return;
      }
      if (hint) hint.hidden = true;
      list.innerHTML = vms.map(v => `
        <div class="vm-row ${v.handled ? 'handled' : 'unhandled'}" data-id="${esc(v.id)}">
          <div class="vm-head">
            <div>
              <strong>${esc(v.caller_name || v.from || 'Unknown caller')}</strong>
              ${v.callback_number ? `<span class="muted"> — ${esc(v.callback_number)}</span>` : ''}
            </div>
            <span class="muted">${esc((v.received_at || '').slice(0,16).replace('T',' '))}</span>
          </div>
          ${v.summary ? `<div class="vm-summary">${esc(v.summary)}</div>` : ''}
          ${v.transcript ? `<details class="vm-transcript"><summary>Show transcript</summary><pre>${esc(v.transcript)}</pre></details>` : ''}
          ${v.audio_url ? `<audio src="${esc(v.audio_url)}" controls preload="none" class="vm-audio"></audio>` : ''}
          <div class="vm-actions">
            ${!v.handled ? `<button class="secondary-btn" data-action="vm-handled">Mark handled</button>` : ''}
            <button class="icon-btn-sm" data-action="vm-delete" title="Delete">×</button>
          </div>
        </div>
      `).join('');
      list.querySelectorAll('[data-action="vm-handled"]').forEach(b => {
        b.addEventListener('click', async (e) => {
          const id = e.target.closest('[data-id]').dataset.id;
          await api(`/api/owner/voicemails/${id}/handled`, { method: 'POST' });
          loadVoicemails();
        });
      });
      list.querySelectorAll('[data-action="vm-delete"]').forEach(b => {
        b.addEventListener('click', async (e) => {
          if (!confirm('Delete this voicemail?')) return;
          const id = e.target.closest('[data-id]').dataset.id;
          await api(`/api/owner/voicemails/${id}`, { method: 'DELETE' });
          loadVoicemails();
        });
      });
    } catch (err) {
      list.innerHTML = `<div class="empty-state-small">Couldn't load voicemails (${esc(err.message)})</div>`;
    }
  }

  function wireVoicemailsTab() {
    document.querySelectorAll('.tab').forEach(t => {
      if (t.dataset.tab === 'voicemails') t.addEventListener('click', loadVoicemails);
    });
    loadVoicemails();
  }
  document.addEventListener('DOMContentLoaded', wireVoicemailsTab);


  // ==================================================================
  // RECEIPTS — OCR'd from photos in Files tab
  // ==================================================================

  async function loadReceipts() {
    const section = document.getElementById('receipts-section');
    const list = document.getElementById('receipts-list');
    if (!section || !list) return;
    try {
      const r = await api('/api/owner/receipts');
      const items = r.receipts || [];
      if (!items.length) { section.hidden = true; return; }
      section.hidden = false;
      list.innerHTML = items.map(rc => `
        <div class="receipt-row" data-id="${esc(rc.id)}">
          <div class="receipt-vendor">${esc(rc.vendor || 'Unknown vendor')}</div>
          <div class="receipt-meta">
            ${rc.total ? `<strong>$${parseFloat(rc.total).toFixed(2)}</strong>` : ''}
            ${rc.date ? ` · ${esc(rc.date)}` : ''}
            ${rc.payment_method ? ` · ${esc(rc.payment_method)}` : ''}
          </div>
          <button class="icon-btn-sm" data-action="receipt-delete" title="Remove">×</button>
        </div>
      `).join('');
      list.querySelectorAll('[data-action="receipt-delete"]').forEach(b => {
        b.addEventListener('click', async (e) => {
          if (!confirm('Delete this receipt?')) return;
          const id = e.target.closest('[data-id]').dataset.id;
          await api(`/api/owner/receipts/${id}`, { method: 'DELETE' });
          loadReceipts();
        });
      });
    } catch (err) {
      section.hidden = true;
    }
  }

  // When an image is uploaded, add a "Scan with OCR" hint to its row
  // (hook into the existing file list — add an OCR button per image file)
  const originalLoadFiles = window.loadFiles;
  // We rebind via re-rendering; the file-row buttons get patched in here:
  function injectOcrButtons() {
    document.querySelectorAll('.file-row').forEach(row => {
      const name = row.dataset.name || '';
      const ext = (name.split('.').pop() || '').toLowerCase();
      if (!['png','jpg','jpeg','gif'].includes(ext)) return;
      if (row.querySelector('[data-action="ocr-scan"]')) return;
      const btn = document.createElement('button');
      btn.className = 'secondary-btn';
      btn.dataset.action = 'ocr-scan';
      btn.textContent = '🔍 Scan';
      btn.title = 'Extract text via OCR (receipt or business card)';
      btn.addEventListener('click', async () => {
        btn.disabled = true; btn.textContent = 'Scanning…';
        try {
          const r = await api('/api/owner/ocr/process', {
            method: 'POST', body: JSON.stringify({filename: name}),
          });
          if (r.error) throw new Error(r.error);
          const kind = r.kind || 'document';
          alert(`Found a ${kind}.\n\n${r.parsed?.vendor || r.parsed?.name || '(no key fields detected)'}\n\n${r.action || ''}`);
          loadFiles();
          loadReceipts();
        } catch (err) {
          alert('OCR failed: ' + err.message);
        } finally {
          btn.disabled = false; btn.textContent = '🔍 Scan';
        }
      });
      const convertBtn = row.querySelector('[data-action="file-convert"]');
      if (convertBtn) row.insertBefore(btn, convertBtn);
      else row.appendChild(btn);
    });
  }
  // Watch for files list re-renders
  const filesListEl = document.getElementById('files-list');
  if (filesListEl) {
    new MutationObserver(injectOcrButtons).observe(filesListEl, {childList: true, subtree: false});
  }
  document.addEventListener('DOMContentLoaded', () => {
    setTimeout(() => { loadReceipts(); injectOcrButtons(); }, 500);
    document.querySelectorAll('.tab').forEach(t => {
      if (t.dataset.tab === 'files') t.addEventListener('click', loadReceipts);
    });
  });


  // ------------------------------------------------------------------
  // Utils
  // ------------------------------------------------------------------
  function esc(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }


  // ==================================================================
  // FILES TAB — content generation tools (chart, pptx, image, translate)
  // ==================================================================

  function showToolResult(el, html, kind) {
    if (!el) return;
    el.hidden = false;
    el.className = 'tool-result' + (kind ? ' tool-result-' + kind : '');
    el.innerHTML = html;
  }

  function busyButton(btn, label) {
    btn.disabled = true;
    btn.dataset.originalLabel = btn.dataset.originalLabel || btn.textContent;
    btn.textContent = label;
  }

  function resetButton(btn, label) {
    btn.disabled = false;
    btn.textContent = label || btn.dataset.originalLabel || btn.textContent;
  }

  function wireToolButtons() {
    const chartBtn = document.getElementById('tool-chart-btn');
    const pptxBtn  = document.getElementById('tool-pptx-btn');
    const imageBtn = document.getElementById('tool-image-btn');
    const transBtn = document.getElementById('tool-translate-btn');
    if (!chartBtn) return; // not on this page

    chartBtn.addEventListener('click', () => openToolDialog('chart-dialog', 'chart-result', 'chart-request'));
    pptxBtn .addEventListener('click', () => openToolDialog('pptx-dialog',  'pptx-result',  'pptx-topic'));
    imageBtn.addEventListener('click', () => openToolDialog('image-dialog', 'image-result', 'image-prompt'));
    transBtn.addEventListener('click', () => {
      openToolDialog('translate-dialog', 'translate-result', 'translate-source');
      const rt = document.getElementById('translate-result-text');
      if (rt) rt.value = '';
    });

    wireChartForm();
    wirePptxForm();
    wireImageForm();
    wireTranslateForm();
  }

  function openToolDialog(dlgId, resultId, focusId) {
    const dlg = document.getElementById(dlgId);
    const result = document.getElementById(resultId);
    if (result) { result.hidden = true; result.innerHTML = ''; }
    if (dlg) dlg.showModal();
    const f = focusId && document.getElementById(focusId);
    if (f) setTimeout(() => f.focus(), 50);
  }

  function wireChartForm() {
    const form = document.getElementById('chart-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = document.getElementById('chart-go');
      const result = document.getElementById('chart-result');
      const request = (document.getElementById('chart-request').value || '').trim();
      if (!request) return;
      busyButton(btn, 'Working…');
      showToolResult(result, '<span style="color:#4f8cff">Generating chart…</span>');
      try {
        const data = await api('/api/owner/chart/from_request', {
          method: 'POST',
          body: JSON.stringify({ request }),
        });
        const url = data.download_url || '';
        const name = data.filename || 'chart.png';
        showToolResult(result,
          `<div style="color:#6fdc94">✓ Chart ready</div>`
          + (url ? `<img class="tool-preview-img" src="${esc(url)}" alt="chart preview">` : '')
          + (url ? `<a href="${esc(url)}" download="${esc(name)}" class="tool-download-link">⬇ Download ${esc(name)}</a>` : '')
        , 'ok');
        resetButton(btn, 'Generate another');
        loadFiles && loadFiles();
      } catch (err) {
        showToolResult(result, `<span style="color:#ffb0b0">✗ ${esc(err.message)}</span>`, 'err');
        resetButton(btn, 'Try again');
      }
    });
  }

  function wirePptxForm() {
    const form = document.getElementById('pptx-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = document.getElementById('pptx-go');
      const result = document.getElementById('pptx-result');
      const topic = (document.getElementById('pptx-topic').value || '').trim();
      const slide_count = parseInt(document.getElementById('pptx-slides').value, 10) || 7;
      const theme = document.getElementById('pptx-theme').value || 'modern';
      if (!topic) return;
      busyButton(btn, 'Working… (10-30s)');
      showToolResult(result, '<span style="color:#4f8cff">Writing outline and building slides…</span>');
      try {
        const data = await api('/api/owner/pptx/build', {
          method: 'POST',
          body: JSON.stringify({ topic, slide_count, theme }),
        });
        const url = data.download_url || '';
        const name = data.filename || 'deck.pptx';
        const outline = Array.isArray(data.outline) ? data.outline : [];
        const outlineHtml = outline.length
          ? `<ol class="tool-outline">${outline.map(s => `<li>${esc(s)}</li>`).join('')}</ol>`
          : '';
        showToolResult(result,
          `<div style="color:#6fdc94">✓ ${data.slide_count || slide_count} slides built</div>`
          + outlineHtml
          + (url ? `<a href="${esc(url)}" download="${esc(name)}" class="tool-download-link">⬇ Download ${esc(name)}</a>` : '')
        , 'ok');
        resetButton(btn, 'Build another');
        loadFiles && loadFiles();
      } catch (err) {
        showToolResult(result, `<span style="color:#ffb0b0">✗ ${esc(err.message)}</span>`, 'err');
        resetButton(btn, 'Try again');
      }
    });
  }

  function wireImageForm() {
    const form = document.getElementById('image-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = document.getElementById('image-go');
      const result = document.getElementById('image-result');
      const prompt = (document.getElementById('image-prompt').value || '').trim();
      const kind = document.getElementById('image-kind').value || 'social_post';
      if (!prompt) return;
      busyButton(btn, 'Working… (can be slow)');
      showToolResult(result, '<span style="color:#4f8cff">Generating image…</span>');
      try {
        const data = await api('/api/owner/image_gen', {
          method: 'POST',
          body: JSON.stringify({ prompt, kind }),
        });
        const url = data.download_url || '';
        const name = data.filename || 'image.png';
        showToolResult(result,
          `<div style="color:#6fdc94">✓ Image ready</div>`
          + (url ? `<img class="tool-preview-img" src="${esc(url)}" alt="generated image">` : '')
          + (url ? `<a href="${esc(url)}" download="${esc(name)}" class="tool-download-link">⬇ Download ${esc(name)}</a>` : '')
        , 'ok');
        resetButton(btn, 'Generate another');
        loadFiles && loadFiles();
      } catch (err) {
        showToolResult(result, `<span style="color:#ffb0b0">✗ ${esc(err.message)}</span>`, 'err');
        resetButton(btn, 'Try again');
      }
    });
  }

  function wireTranslateForm() {
    const form = document.getElementById('translate-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = document.getElementById('translate-go');
      const result = document.getElementById('translate-result');
      const out = document.getElementById('translate-result-text');
      const text = (document.getElementById('translate-source').value || '').trim();
      const target_lang = document.getElementById('translate-target').value || 'es';
      if (!text) return;
      busyButton(btn, 'Translating…');
      if (out) out.value = '';
      showToolResult(result, '<span style="color:#4f8cff">Translating…</span>');
      try {
        const data = await api('/api/owner/translate', {
          method: 'POST',
          body: JSON.stringify({ text, target_lang }),
        });
        const translated = data.translated || '';
        if (out) out.value = translated;
        showToolResult(result,
          `<div style="color:#6fdc94">✓ Translated to ${esc(target_lang)}</div>`
        , 'ok');
        resetButton(btn, 'Translate again');
      } catch (err) {
        showToolResult(result, `<span style="color:#ffb0b0">✗ ${esc(err.message)}</span>`, 'err');
        resetButton(btn, 'Try again');
      }
    });
  }

  document.addEventListener('DOMContentLoaded', wireToolButtons);

  // ==================================================================
  // CUSTOMER THREAD dialog + MAIL MERGE dialog
  // ==================================================================

  // Relative-time formatter.
  // Accepts ISO 8601 strings, JS Date objects, or unix timestamps (sec or ms).
  // Returns "just now", "5 min ago", "2 hours ago", "yesterday", "3 days ago",
  // "last week", "2 weeks ago", or a compact date for items > ~45 days.
  function relTime(input) {
    if (input == null || input === '') return '';
    let ts;
    if (input instanceof Date) ts = input.getTime();
    else if (typeof input === 'number') ts = input < 1e12 ? input * 1000 : input;
    else {
      const parsed = Date.parse(input);
      if (isNaN(parsed)) return String(input);
      ts = parsed;
    }
    const now = Date.now();
    const diff = now - ts;            // ms, +ve = past
    const abs = Math.abs(diff);
    const sec = 1000, min = 60*sec, hour = 60*min, day = 24*hour, week = 7*day;
    const future = diff < 0;

    if (abs < 45*sec) return future ? 'in a moment' : 'just now';
    if (abs < 90*sec) return future ? 'in 1 min' : '1 min ago';
    if (abs < 45*min) {
      const m = Math.round(abs/min);
      return future ? `in ${m} min` : `${m} min ago`;
    }
    if (abs < 90*min) return future ? 'in 1 hour' : '1 hour ago';
    if (abs < 22*hour) {
      const h = Math.round(abs/hour);
      return future ? `in ${h} hours` : `${h} hours ago`;
    }
    if (abs < 36*hour) return future ? 'tomorrow' : 'yesterday';
    if (abs < 7*day) {
      const d = Math.round(abs/day);
      return future ? `in ${d} days` : `${d} days ago`;
    }
    if (abs < 2*week) return future ? 'next week' : 'last week';
    if (abs < 45*day) {
      const w = Math.round(abs/week);
      return future ? `in ${w} weeks` : `${w} weeks ago`;
    }
    // Older: compact date; add year only if not current year.
    const d = new Date(ts);
    const sameYear = d.getFullYear() === new Date().getFullYear();
    return d.toLocaleDateString(undefined, sameYear
      ? { month: 'short', day: 'numeric' }
      : { month: 'short', day: 'numeric', year: 'numeric' });
  }

  const THREAD_ICONS = {
    call:     '📞',
    voicemail:'📞',
    email:    '✉',
    chat:     '💬',
    sms:      '💬',
    message:  '💬',
    calendar: '📅',
    event:    '📅',
    booking:  '📅',
    payment:  '💳',
    order:    '💳',
    invoice:  '💳',
    note:     '📝',
    task:     '📝',
  };

  function threadIconFor(kind) {
    const k = String(kind || '').toLowerCase();
    return THREAD_ICONS[k] || '•';
  }

  async function openThreadDialog(contactId) {
    const dlg = document.getElementById('thread-dialog');
    const nameEl = document.getElementById('thread-name');
    const sumEl  = document.getElementById('thread-summary');
    const evEl   = document.getElementById('thread-events');
    if (!dlg) return;
    nameEl.textContent = '—';
    sumEl.textContent  = 'Loading…';
    evEl.innerHTML     = '<div class="empty-state-small">Loading timeline…</div>';
    if (typeof dlg.showModal === 'function') dlg.showModal();
    else dlg.setAttribute('open', '');

    try {
      const data = await api(`/api/owner/customer_thread/${encodeURIComponent(contactId)}`);
      const contact = data.contact || {};
      const events  = Array.isArray(data.events) ? data.events : [];
      nameEl.textContent = contact.name || contact.email || contact.phone || 'Contact';
      const subBits = [];
      if (contact.company) subBits.push(esc(contact.company));
      if (contact.email)   subBits.push(esc(contact.email));
      if (contact.phone)   subBits.push(esc(contact.phone));
      sumEl.innerHTML = data.summary
        ? esc(data.summary)
        : (subBits.length ? subBits.join(' • ') : `${events.length} events`);

      if (!events.length) {
        evEl.innerHTML = '<div class="empty-state-small">No history yet for this contact.</div>';
        return;
      }
      evEl.innerHTML = events.map(ev => {
        const kind = String(ev.kind || ev.type || 'note').toLowerCase();
        const icon = threadIconFor(kind);
        const when = ev.when || ev.timestamp || ev.created_at || ev.date || ev.ts;
        const title = ev.title || ev.subject || ev.summary || ev.headline ||
                      (kind ? kind.charAt(0).toUpperCase() + kind.slice(1) : 'Event');
        const snippet = ev.snippet || ev.body || ev.text || ev.preview || '';
        const source  = ev.source || ev.channel || '';
        return `
        <div class="thread-event" data-kind="${esc(kind)}">
          <div class="icon" aria-hidden="true">${icon}</div>
          <div class="body">
            <div class="thread-event-title">${esc(title)}</div>
            <div class="thread-event-meta">
              <span class="when" title="${esc(when || '')}">${esc(relTime(when))}</span>
              ${source ? `<span class="tag">${esc(source)}</span>` : ''}
            </div>
            ${snippet ? `<div class="thread-event-snippet">${esc(snippet)}</div>` : ''}
          </div>
        </div>`;
      }).join('');
    } catch (err) {
      evEl.innerHTML = `<div class="empty-state-small">Couldn't load thread (${esc(err.message)})</div>`;
    }
  }

  function openMergeDialog() {
    const dlg = document.getElementById('merge-dialog');
    if (!dlg) return;
    const n = selectedContactIds.size;
    const countEl = document.getElementById('merge-selected-count');
    if (countEl) countEl.textContent = `${n} contact${n === 1 ? '' : 's'} selected`;
    const prev = document.getElementById('merge-preview');
    const res  = document.getElementById('merge-result');
    if (prev) { prev.hidden = true; prev.innerHTML = ''; }
    if (res)  { res.hidden  = true; res.innerHTML  = ''; }
    // Default sender name from owner profile (best-effort).
    const senderInput = document.getElementById('merge-sender');
    if (senderInput && !senderInput.value) {
      const guess = (businessInfo && (businessInfo.owner_name || businessInfo.name)) ||
                    document.getElementById('business-name')?.textContent || '';
      if (guess && guess !== '—') senderInput.value = guess;
    }
    if (typeof dlg.showModal === 'function') dlg.showModal();
    else dlg.setAttribute('open', '');
  }

  function wireMailMerge() {
    const previewBtn = document.getElementById('merge-preview-btn');
    const runBtn     = document.getElementById('merge-run-btn');
    const tplEl      = document.getElementById('merge-template');
    const tgtEl      = document.getElementById('merge-target');
    const senderEl   = document.getElementById('merge-sender');
    const personEl   = document.getElementById('merge-personalize');
    const prevEl     = document.getElementById('merge-preview');
    const resEl      = document.getElementById('merge-result');

    if (!previewBtn || !runBtn) return; // dialog not present

    function gatherExtras() {
      return {
        sender_name: senderEl?.value?.trim() || '',
        today: new Date().toLocaleDateString(),
      };
    }

    previewBtn.addEventListener('click', async () => {
      if (!tplEl.value.trim()) {
        prevEl.hidden = false;
        prevEl.innerHTML = '<div class="muted">Type a template first.</div>';
        return;
      }
      const ids = [...selectedContactIds];
      if (!ids.length) {
        prevEl.hidden = false;
        prevEl.innerHTML = '<div class="muted">No contacts selected.</div>';
        return;
      }
      prevEl.hidden = false;
      prevEl.innerHTML = '<div class="muted">Rendering…</div>';
      previewBtn.disabled = true;
      try {
        const r = await api('/api/owner/mail_merge/preview', {
          method: 'POST',
          body: JSON.stringify({
            template: tplEl.value,
            contact_id: ids[0],
            extras: gatherExtras(),
          }),
        });
        const rendered = r.rendered || '';
        prevEl.innerHTML =
          `<div class="merge-preview-label muted">Preview (first selected contact):</div>
           <pre class="merge-preview-body">${esc(rendered)}</pre>`;
      } catch (err) {
        prevEl.innerHTML = `<div class="muted">Preview failed: ${esc(err.message)}</div>`;
      } finally {
        previewBtn.disabled = false;
      }
    });

    runBtn.addEventListener('click', async () => {
      if (!tplEl.value.trim()) {
        resEl.hidden = false;
        resEl.innerHTML = '<div class="muted">Type a template first.</div>';
        return;
      }
      const ids = [...selectedContactIds];
      if (!ids.length) {
        resEl.hidden = false;
        resEl.innerHTML = '<div class="muted">No contacts selected.</div>';
        return;
      }
      resEl.hidden = false;
      resEl.innerHTML = `<div class="muted">Generating ${ids.length} letter${ids.length === 1 ? '' : 's'}…</div>`;
      runBtn.disabled = true; previewBtn.disabled = true;
      try {
        const r = await api('/api/owner/mail_merge/run', {
          method: 'POST',
          body: JSON.stringify({
            template: tplEl.value,
            contact_ids: ids,
            target_format: tgtEl?.value || 'pdf',
            extras: gatherExtras(),
            llm_personalize: !!personEl?.checked,
          }),
        });
        const merged = r.merged ?? ids.length;
        const dl = r.download_url || '';
        resEl.innerHTML = `
          <div class="merge-success">✓ ${esc(merged)} letter${merged === 1 ? '' : 's'} generated.</div>
          ${dl ? `<a class="merge-download" href="${esc(dl)}" target="_blank" rel="noopener">⬇ Download zip</a>` : ''}
          ${r.zip_path && !dl ? `<div class="muted">Saved to: <code>${esc(r.zip_path)}</code></div>` : ''}
        `;
      } catch (err) {
        resEl.innerHTML = `<div class="muted">Merge failed: ${esc(err.message)}</div>`;
      } finally {
        runBtn.disabled = false; previewBtn.disabled = false;
      }
    });
  }


  // ==================================================================
  // EMAIL TAB — unified Gmail + Outlook inbox
  // ==================================================================

  let _emailState = { messages: [], filter: "all", currentMessageId: null };
  const EMAIL_FILTERS = [
    { id: "all",        label: "All",        emoji: "📥" },
    { id: "unread",     label: "Unread",     emoji: "🔵" },
    { id: "flagged",    label: "Flagged",    emoji: "🚩" },
    { id: "urgent",     label: "Urgent",     emoji: "🔥" },
    { id: "lead",       label: "Leads",      emoji: "💼" },
    { id: "complaint",  label: "Complaints", emoji: "⚠️" },
    { id: "question",   label: "Questions",  emoji: "❓" },
  ];

  function renderEmailFilterRow() {
    const row = document.getElementById('email-filter-row');
    if (!row) return;
    row.innerHTML = EMAIL_FILTERS.map(f => `
      <button class="chip ${_emailState.filter === f.id ? 'active' : ''}" data-email-filter="${esc(f.id)}">
        ${esc(f.emoji)} ${esc(f.label)} <span class="chip-count" data-count-for="${esc(f.id)}"></span>
      </button>`).join('');
    row.querySelectorAll('[data-email-filter]').forEach(btn => {
      btn.addEventListener('click', () => {
        _emailState.filter = btn.dataset.emailFilter;
        renderEmailFilterRow(); renderEmailList();
      });
    });
    const counts = {};
    for (const m of _emailState.messages) {
      counts.all = (counts.all || 0) + 1;
      if (m.unread) counts.unread = (counts.unread || 0) + 1;
      if (m.flagged) counts.flagged = (counts.flagged || 0) + 1;
      for (const t of m.tags || []) counts[t] = (counts[t] || 0) + 1;
    }
    row.querySelectorAll('[data-count-for]').forEach(s => {
      const n = counts[s.dataset.countFor] || 0; s.textContent = n ? n : '';
    });
  }

  function filterEmails() {
    const f = _emailState.filter;
    if (f === "all") return _emailState.messages;
    if (f === "unread") return _emailState.messages.filter(m => m.unread);
    if (f === "flagged") return _emailState.messages.filter(m => m.flagged);
    return _emailState.messages.filter(m => (m.tags || []).includes(f));
  }

  function renderEmailList() {
    const list = document.getElementById('email-list');
    if (!list) return;
    const filtered = filterEmails();
    if (!filtered.length) {
      list.innerHTML = `<div class="empty-state-small">No ${_emailState.filter === 'all' ? '' : _emailState.filter + ' '}emails.</div>`;
      return;
    }
    list.innerHTML = filtered.map(m => `
      <div class="email-row ${m.unread ? 'unread' : ''} ${m.flagged ? 'flagged' : ''}" data-id="${esc(m.id)}">
        <div class="email-row-icons">
          <span class="email-provider email-provider-${esc(m.provider)}">${esc(m.provider === 'gmail' ? '✉' : '📧')}</span>
          ${m.flagged ? `<span class="email-flag" title="${esc(m.flag_reason||'flagged')}">🚩</span>` : ''}
        </div>
        <div class="email-row-body">
          <div class="email-row-head">
            <span class="email-from">${esc(m.from || '(unknown)')}</span>
            <span class="email-date muted">${esc((m.date || '').slice(0,16).replace('T',' '))}</span>
          </div>
          <div class="email-subject">${esc(m.subject || '(no subject)')}</div>
          <div class="email-snippet">${esc(m.snippet || '')}</div>
          <div class="email-tags">${(m.tags || []).map(t => `<span class="tag tag-${esc(t)}">${esc(t)}</span>`).join('')}</div>
        </div>
        <div class="email-row-actions">
          <button class="icon-btn-sm" data-action="email-flag" title="${m.flagged ? 'Unflag' : 'Flag'}">${m.flagged ? '🚩' : '⚐'}</button>
          <button class="icon-btn-sm" data-action="email-archive" title="Archive">📁</button>
        </div>
      </div>`).join('');
    list.querySelectorAll('.email-row .email-row-body').forEach(body => {
      body.addEventListener('click', () => openEmailDetail(body.parentElement.dataset.id));
    });
    list.querySelectorAll('[data-action="email-flag"]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const id = btn.closest('[data-id]').dataset.id;
        const m = _emailState.messages.find(x => x.id === id);
        if (!m) return;
        try {
          await api(`/api/owner/email/${encodeURIComponent(id)}/flag`, {
            method: 'POST', body: JSON.stringify({flagged: !m.flagged}),
          });
          m.flagged = !m.flagged;
          renderEmailList(); renderEmailFilterRow();
        } catch (err) { alert(err.message); }
      });
    });
    list.querySelectorAll('[data-action="email-archive"]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!confirm('Archive this email?')) return;
        const id = btn.closest('[data-id]').dataset.id;
        try {
          await api(`/api/owner/email/${encodeURIComponent(id)}/archive`, { method: 'POST' });
          _emailState.messages = _emailState.messages.filter(x => x.id !== id);
          renderEmailList(); renderEmailFilterRow();
        } catch (err) { alert(err.message); }
      });
    });
  }

  async function loadEmails(force = false, query = "") {
    const list = document.getElementById('email-list');
    if (!list) return;
    if (!_emailState.messages.length) list.innerHTML = '<div class="empty-state-small">Loading…</div>';
    try {
      const q = query ? '&q=' + encodeURIComponent(query) : '';
      const r = await api(`/api/owner/email/inbox?source=all&limit=50${q}${force ? '&refresh=1' : ''}`);
      _emailState.messages = r.messages || [];
      const badge = document.getElementById('email-count');
      const unread = _emailState.messages.filter(m => m.unread).length;
      if (badge) {
        if (unread) { badge.textContent = unread; badge.hidden = false; }
        else badge.hidden = true;
      }
      renderEmailFilterRow(); renderEmailList();
    } catch (err) {
      list.innerHTML = `<div class="empty-state-small">Couldn't load email (${esc(err.message)}). Connect Gmail or Outlook in Settings → Integrations.</div>`;
    }
  }

  async function openEmailDetail(messageId) {
    _emailState.currentMessageId = messageId;
    const m = _emailState.messages.find(x => x.id === messageId);
    if (!m) return;
    const dlg = document.getElementById('email-detail-dialog');
    document.getElementById('email-detail-body').innerHTML = `
      <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:8px">
        <h2 style="margin:0;font-size:18px">${esc(m.subject || '(no subject)')}</h2>
        <span class="muted">${esc(m.provider)} · ${esc((m.date || '').slice(0,16).replace('T',' '))}</span>
      </div>
      <div class="muted" style="margin-bottom:8px">from <strong>${esc(m.from || '(unknown)')}</strong></div>
      <div style="background:#0b0f1a;border:1px solid #1f2942;border-radius:8px;padding:12px;margin-bottom:14px;max-height:340px;overflow:auto">
        <div id="email-detail-fullbody" style="white-space:pre-wrap">Loading body…</div>
      </div>`;
    document.getElementById('email-reply-text').value = '';
    document.getElementById('email-detail-status').textContent = '';
    dlg.showModal();
    if (m.unread) {
      api(`/api/owner/email/${encodeURIComponent(messageId)}/mark_read`, { method: 'POST' })
        .then(() => { m.unread = false; renderEmailList(); renderEmailFilterRow(); }).catch(() => {});
    }
    try {
      const full = await api(`/api/owner/email/${encodeURIComponent(messageId)}`);
      document.getElementById('email-detail-fullbody').textContent =
        full.body || full.body_text || full.snippet || '(no body)';
    } catch { document.getElementById('email-detail-fullbody').textContent = 'Could not load full body.'; }
  }

  function wireEmailDialog() {
    document.getElementById('email-save-draft')?.addEventListener('click', async () => {
      const id = _emailState.currentMessageId;
      const text = document.getElementById('email-reply-text').value.trim();
      const status = document.getElementById('email-detail-status');
      if (!text) { status.textContent = 'Type a reply first.'; return; }
      status.textContent = 'Saving draft…';
      try {
        const r = await api(`/api/owner/email/${encodeURIComponent(id)}/reply`, {
          method: 'POST', body: JSON.stringify({reply_text: text}),
        });
        if (r.error) throw new Error(r.error);
        status.innerHTML = '<span style="color:#6fdc94">✓ Saved to Drafts. Open Gmail/Outlook to review and send.</span>';
      } catch (err) { status.innerHTML = `<span style="color:#ffb0b0">${esc(err.message)}</span>`; }
    });
    document.getElementById('email-suggest-reply')?.addEventListener('click', async () => {
      const id = _emailState.currentMessageId;
      const m = _emailState.messages.find(x => x.id === id);
      if (!m) return;
      const status = document.getElementById('email-detail-status');
      status.textContent = 'Drafting in your voice…';
      try {
        const r = await api('/api/owner/style/draft', {
          method: 'POST',
          body: JSON.stringify({
            draft_context: `Incoming email from ${m.from}: "${m.subject}"\n${m.snippet || ''}`,
            what_to_say:   `Write a short, friendly reply.`,
          }),
        });
        document.getElementById('email-reply-text').value = r.draft || '';
        status.innerHTML = '<span style="color:#6fdc94">Drafted — edit and click Save to Drafts.</span>';
      } catch (err) { status.innerHTML = `<span style="color:#ffb0b0">Could not draft: ${esc(err.message)}</span>`; }
    });
  }

  function wireEmailSettings() {
    const open = document.getElementById('email-settings-btn');
    const save = document.getElementById('email-settings-save');
    const dlg = document.getElementById('email-settings-dialog');
    open?.addEventListener('click', async () => {
      try {
        const s = await api('/api/owner/email/settings');
        document.getElementById('email-flag-keywords').value = (s.flag_keywords || []).join(', ');
        document.getElementById('email-fetch-limit').value = s.fetch_limit || 50;
      } catch {}
      dlg.showModal();
    });
    save?.addEventListener('click', async () => {
      const status = document.getElementById('email-settings-status');
      try {
        const kw = document.getElementById('email-flag-keywords').value
                     .split(',').map(s => s.trim()).filter(Boolean);
        const limit = parseInt(document.getElementById('email-fetch-limit').value, 10) || 50;
        await api('/api/owner/email/settings', {
          method: 'PUT', body: JSON.stringify({flag_keywords: kw, fetch_limit: limit}),
        });
        status.innerHTML = '<span style="color:#6fdc94">✓ Saved. Refreshing inbox…</span>';
        loadEmails(true);
        setTimeout(() => dlg.close(), 800);
      } catch (err) { status.innerHTML = `<span style="color:#ffb0b0">${esc(err.message)}</span>`; }
    });
  }

  function wireEmailTab() {
    document.querySelectorAll('.tab').forEach(t => {
      if (t.dataset.tab === 'email') t.addEventListener('click', () => loadEmails(false));
    });
    document.getElementById('email-refresh-btn')?.addEventListener('click', () => loadEmails(true));
    let st;
    document.getElementById('email-search')?.addEventListener('input', (e) => {
      clearTimeout(st);
      st = setTimeout(() => loadEmails(false, e.target.value.trim()), 250);
    });
    wireEmailDialog();
    wireEmailSettings();
  }
  document.addEventListener('DOMContentLoaded', wireEmailTab);


  // ==================================================================
  // MY DAY — Schedule a meeting button
  // ==================================================================

  let _scheduleState = { attendee_name: "", attendee_email: "", duration_minutes: 30 };

  function wireScheduleMeeting() {
    const open = document.getElementById('myday-schedule-meeting-btn');
    const dlg  = document.getElementById('schedule-meeting-dialog');
    const find = document.getElementById('schedule-find-slots-btn');
    if (!open || !dlg || !find) return;

    open.addEventListener('click', () => {
      document.getElementById('schedule-meeting-form').reset();
      document.getElementById('schedule-slots-list').innerHTML = '';
      document.getElementById('schedule-status').textContent = '';
      find.textContent = 'Find open times';
      dlg.showModal();
    });

    find.addEventListener('click', async () => {
      const form = document.getElementById('schedule-meeting-form');
      const fd = new FormData(form);
      _scheduleState.attendee_name = fd.get('attendee_name') || '';
      _scheduleState.attendee_email = fd.get('attendee_email') || '';
      _scheduleState.duration_minutes = parseInt(fd.get('duration_minutes'), 10) || 30;
      const days = parseInt(fd.get('days_ahead'), 10) || 7;
      const slotsList = document.getElementById('schedule-slots-list');
      const status = document.getElementById('schedule-status');
      if (!_scheduleState.attendee_name || !_scheduleState.attendee_email) {
        status.textContent = 'Need both name and email.';
        return;
      }
      find.disabled = true;
      slotsList.innerHTML = '<div class="muted">Looking for open times…</div>';
      status.textContent = '';
      try {
        const r = await api('/api/owner/scheduler/find_slots', {
          method: 'POST',
          body: JSON.stringify({
            duration_minutes: _scheduleState.duration_minutes,
            days_ahead: days,
          }),
        });
        const slots = (r.slots || []).slice(0, 6);
        if (!slots.length) {
          slotsList.innerHTML = '<div class="muted">No open slots found in that window. Try a longer window.</div>';
        } else {
          slotsList.innerHTML = `
            <div style="font-weight:600;font-size:14px;margin-bottom:6px">Pick a time to send to ${esc(_scheduleState.attendee_name)}:</div>
            <div style="display:flex;flex-direction:column;gap:6px">
              ${slots.map(s => `
                <button type="button" class="secondary-btn schedule-slot" data-start="${esc(s.start_iso)}" data-end="${esc(s.end_iso)}" style="text-align:left">
                  ${esc(s.day_label || '')} · ${esc(s.time_label || '')}
                </button>
              `).join('')}
            </div>`;
          slotsList.querySelectorAll('.schedule-slot').forEach(btn => {
            btn.addEventListener('click', () => bookSlot(btn.dataset.start, btn.dataset.end));
          });
        }
      } catch (err) {
        slotsList.innerHTML = `<div class="muted" style="color:#ffb0b0">${esc(err.message)}</div>`;
      } finally {
        find.disabled = false;
        find.textContent = 'Find again';
      }
    });
  }

  async function bookSlot(startIso, endIso) {
    const status = document.getElementById('schedule-status');
    status.innerHTML = 'Booking…';
    try {
      const r = await api('/api/owner/scheduler/book', {
        method: 'POST',
        body: JSON.stringify({
          attendee_name:  _scheduleState.attendee_name,
          attendee_email: _scheduleState.attendee_email,
          start_iso: startIso,
          end_iso:   endIso,
          title: `Meeting with ${_scheduleState.attendee_name}`,
        }),
      });
      const when = (startIso || '').slice(0,16).replace('T',' ');
      status.innerHTML = `<span style="color:#6fdc94">✓ Booked ${esc(when)}. Calendar event created${r.calendar_synced ? ' + synced to Google Calendar' : ''}.</span>`;
      // Refresh the calendar card
      if (typeof loadMyCalendar === 'function') loadMyCalendar();
      setTimeout(() => document.getElementById('schedule-meeting-dialog').close(), 1500);
    } catch (err) {
      status.innerHTML = `<span style="color:#ffb0b0">Could not book: ${esc(err.message)}</span>`;
    }
  }

  document.addEventListener('DOMContentLoaded', wireScheduleMeeting);


  // ==================================================================
  // ONBOARDING WIZARD — 7-screen setup walkthrough for new owners
  // ==================================================================

  let _wizState = {
    step: 0,
    website: "",
    draft: null,
    gapAnswers: {},
    currentGap: 0,
    gapQuestions: [],
  };

  // Each screen: {title, render() → HTML, onNext() async → void or string error,
  //               nextLabel?, showBack?, showSkip?}
  // Compact human-readable rendering of a hours dict from the scraper.
  // Collapses consecutive identical days into a single range ("Mon-Fri
  // 7:00 AM - 8:00 PM") and converts 24h times to AM/PM. Returns "" if
  // nothing useful, so the wizard's "missing" indicator can take over.
  function _formatHoursForDisplay(hours) {
    if (!hours || typeof hours !== 'object') return '';
    const order = ['monday','tuesday','wednesday','thursday',
                    'friday','saturday','sunday'];
    const abbr  = {monday:'Mon', tuesday:'Tue', wednesday:'Wed',
                    thursday:'Thu', friday:'Fri', saturday:'Sat', sunday:'Sun'};
    function to12h(t) {
      if (!t || typeof t !== 'string') return '';
      const m = t.match(/^(\d{1,2}):?(\d{2})?/);
      if (!m) return t;
      let h = parseInt(m[1], 10);
      const mins = m[2] || '00';
      const ampm = h >= 12 ? 'PM' : 'AM';
      h = h % 12; if (h === 0) h = 12;
      return mins === '00' ? `${h} ${ampm}` : `${h}:${mins} ${ampm}`;
    }
    function fmtDay(d) {
      const v = hours[d];
      if (!v) return 'closed';
      if (typeof v === 'string') return v;
      if (v.closed === true) return 'closed';
      const open = to12h(v.open);
      const close = to12h(v.close);
      if (!open || !close) return 'closed';
      return `${open} - ${close}`;
    }
    // Build per-day strings then collapse runs of identical adjacent days.
    const dayStrs = order.map(d => ({day: d, str: fmtDay(d)}));
    const groups = [];
    let i = 0;
    while (i < dayStrs.length) {
      let j = i;
      while (j + 1 < dayStrs.length && dayStrs[j + 1].str === dayStrs[i].str) j++;
      groups.push({
        label: i === j ? abbr[dayStrs[i].day]
                       : `${abbr[dayStrs[i].day]}-${abbr[dayStrs[j].day]}`,
        str: dayStrs[i].str,
      });
      i = j + 1;
    }
    // Drop groups that are all-closed unless every day is closed
    const allClosed = groups.every(g => g.str === 'closed');
    const visible = allClosed ? groups : groups.filter(g => g.str !== 'closed');
    if (!visible.length) return '';
    return visible.map(g => `${g.label} ${g.str}`).join(', ');
  }

  function wizardScreens() {
    return [
      // 0. Welcome
      {
        title: "Welcome to Orbi",
        nextLabel: "Get started",
        showSkip: false,
        showBack: false,
        render: () => `
          <div style="text-align:center;padding:20px 0">
            <div style="font-size:48px;margin-bottom:12px">👋</div>
            <h3 style="margin-top:0">Let's get you set up — it takes about 10 minutes.</h3>
            <p class="muted" style="font-size:14px;line-height:1.6;max-width:480px;margin:14px auto">
              I'll walk you through 7 quick steps:
            </p>
            <ol style="text-align:left;color:#b8c6e0;font-size:13px;max-width:380px;margin:0 auto;line-height:1.8">
              <li>Connect your website (I'll learn your business)</li>
              <li>Fill in anything I missed</li>
              <li>Connect your email (Gmail or Outlook)</li>
              <li>Confirm your business phone number (already included)</li>
              <li>Pick your tunnel URL (so customers can reach you)</li>
              <li>Add your staff (optional)</li>
              <li>Done — you're live</li>
            </ol>
          </div>`,
        onNext: async () => {}
      },

      // 1. Website
      {
        title: "What's your business website?",
        nextLabel: "Scan my website",
        render: () => `
          <p class="muted" style="font-size:13px">I'll read your homepage, About, Contact, and Services pages to pull your business name, address, phone, email, hours, and what you sell.</p>
          <label style="display:block;margin-top:10px">Website URL
            <input type="url" id="wiz-website" placeholder="https://yourbusiness.com" required
              style="width:100%;background:#0b0f1a;border:1px solid #2c3957;color:#eaf0ff;border-radius:8px;padding:10px 14px;font-size:15px;margin-top:4px"
              value="${esc(_wizState.website)}">
          </label>
          <div id="wiz-scan-status" style="margin-top:14px;font-size:13px"></div>
        `,
        onNext: async () => {
          const url = document.getElementById('wiz-website').value.trim();
          if (!url) return "Please enter your website URL.";
          _wizState.website = url;
          const status = document.getElementById('wiz-scan-status');
          const nextBtn = document.getElementById('wiz-next-btn');
          // Tell the customer what's happening — the disabled cursor on
          // the button alone reads as "blocked", and the scan can take
          // 30-90 seconds. Spinner + button text change makes it clear
          // Orbi is working, not stuck.
          if (nextBtn) {
            nextBtn.textContent = '⏳ Scanning your website…';
          }
          status.innerHTML = '<div style="color:#4f8cff;font-size:14px;padding:10px;background:rgba(79,140,255,0.08);border:1px solid rgba(79,140,255,0.3);border-radius:8px;">⏳ <strong>Reading your homepage, About page, services, hours…</strong><br><span style="font-size:12px;opacity:0.85">Usually 30-90 seconds. The button stays grey until I\'m done — that\'s normal.</span></div>';
          try {
            const r = await api('/api/owner/onboarding/discover', {
              method: 'POST', body: JSON.stringify({url}),
            });
            if (r.error) throw new Error(r.error);
            _wizState.draft = r.draft;
            _wizState.gapQuestions = r.gap_questions || [];
            _wizState.currentGap = 0;
          } catch (err) {
            if (nextBtn) nextBtn.textContent = 'Scan my website';
            return `Couldn't scan that site: ${err.message}. You can still continue and fill in everything manually.`;
          }
        }
      },

      // 2. Confirm + fill gaps
      {
        title: "Here's what I found",
        nextLabel: _wizState.gapQuestions.length ? "Fill in the gaps" : "Looks good — continue",
        render: () => {
          const d = _wizState.draft || {};
          const sources = d._sources || {};
          const conf = d._confidence || {};
          const row = (label, value, key) => {
            const c = conf[key] || (value ? "high" : "missing");
            const dot = c === "high" ? '🟢' : (c === "medium" ? '🟡' : '⚪');
            const src = sources[key] ? `<span class="muted" style="font-size:11px">  (from ${esc(sources[key].slice(0,40))})</span>` : '';
            return `<div style="margin-bottom:6px">${dot} <strong>${esc(label)}:</strong> ${esc(value || '(not found)')}${src}</div>`;
          };
          const addr = d.address || {};
          const addrStr = [addr.street, addr.city, addr.state, addr.zip].filter(Boolean).join(', ');
          const contact = d.contact || {};
          const svc = (d.services || []).map(s => `<li>${esc(s.name||'')}${s.price ? ' — '+esc(s.price) : ''}</li>`).join('');
          return `
            <p class="muted" style="font-size:13px">I scanned ${esc(_wizState.website)} and pulled these. Anything wrong? Click "Fix something" — otherwise continue and I'll ask about the missing pieces.</p>
            <div style="background:#0b0f1a;border:1px solid #1f2942;border-radius:10px;padding:14px;margin-top:12px;font-size:14px;color:#eaf0ff">
              ${row('Business name', d.name, 'name')}
              ${row('Tagline', d.tagline || d.description, 'tagline')}
              ${row('Address', addrStr, 'address')}
              ${row('Phone', contact.phone, 'phone')}
              ${row('Email', contact.email, 'email')}
              ${row('Hours', _formatHoursForDisplay(d.hours), 'hours')}
              <div style="margin-top:8px">🟢 <strong>Services found:</strong> ${(d.services||[]).length}</div>
              ${svc ? `<ul style="margin:4px 0 0 16px;color:#b8c6e0;font-size:13px">${svc}</ul>` : ''}
            </div>
            <p class="muted" style="font-size:12px;margin-top:10px">🟢 = found · 🟡 = partial · ⚪ = missing — I'll ask about the missing ones next.</p>
          `;
        },
        onNext: async () => {
          // Move to next screen (gap-filling or skip ahead if no gaps)
        }
      },

      // 3. Gap-filling questions (renders ONE at a time, multi-pass)
      {
        title: "Just a few quick questions",
        nextLabel: "Save my answer",
        showSkip: true,
        render: () => {
          const qs = _wizState.gapQuestions || [];
          if (!qs.length || _wizState.currentGap >= qs.length) {
            return `<p class="muted">All set — I have everything I need from this step. Click Continue.</p>`;
          }
          const q = qs[_wizState.currentGap];
          return `
            <p class="muted" style="font-size:12px">Question ${_wizState.currentGap+1} of ${qs.length}</p>
            <h3 style="margin:8px 0 14px">${esc(q.question)}</h3>
            ${
              q.type === 'textarea'
              ? `<textarea id="wiz-answer" rows="4" style="width:100%;background:#0b0f1a;border:1px solid #2c3957;color:#eaf0ff;border-radius:8px;padding:10px 14px;font-size:14px"></textarea>`
              : `<input type="${esc(q.type)}" id="wiz-answer" style="width:100%;background:#0b0f1a;border:1px solid #2c3957;color:#eaf0ff;border-radius:8px;padding:10px 14px;font-size:15px">`
            }
            <div id="wiz-answer-status" style="margin-top:10px;font-size:13px"></div>
          `;
        },
        onNext: async () => {
          const qs = _wizState.gapQuestions || [];
          if (!qs.length || _wizState.currentGap >= qs.length) return;
          const q = qs[_wizState.currentGap];
          const ans = document.getElementById('wiz-answer')?.value.trim();
          if (!ans) return; // Skip empty
          try {
            const r = await api('/api/owner/onboarding/answer', {
              method: 'POST', body: JSON.stringify({field: q.field, answer: ans}),
            });
            // Merge the patch into the draft
            _wizState.draft = deepMerge(_wizState.draft || {}, r.patch || {});
          } catch (err) {
            return `Couldn't save: ${err.message}`;
          }
          _wizState.currentGap++;
          // If still have more gap questions, stay on this step
          if (_wizState.currentGap < qs.length) {
            // Re-render same screen with next question
            renderWizStep();
            return "STAY";
          }
        }
      },

      // 4. Save business profile
      {
        title: "Saving your business profile…",
        nextLabel: "Continue to email setup",
        render: () => `
          <p style="font-size:14px">I'm saving everything to your local business profile. From now on I can answer any customer question about your business using the real facts you just gave me.</p>
          <div id="wiz-save-status" style="margin-top:12px;font-size:13px;color:#4f8cff">Saving…</div>
        `,
        onNext: async () => {},
        onShow: async () => {
          const status = document.getElementById('wiz-save-status');
          try {
            await api('/api/owner/onboarding/apply', {
              method: 'POST',
              body: JSON.stringify({draft: _wizState.draft, overwrite: true}),
            });
            status.innerHTML = '<span style="color:#6fdc94">✓ Saved. Your business profile is loaded.</span>';
          } catch (err) {
            status.innerHTML = `<span style="color:#ffb0b0">Save failed: ${esc(err.message)}</span>`;
          }
        }
      },

      // 5. Email connection
      {
        title: "Connect your email",
        nextLabel: "Continue",
        showSkip: true,
        render: () => `
          <p style="font-size:14px">Connecting your email lets me read incoming customer emails, draft replies in your voice, and surface urgent ones.</p>
          <p class="muted" style="font-size:13px;margin-top:8px">You can connect Gmail or Outlook from Settings → Integrations whenever you're ready. The wizard isn't blocking — skip this if you'll do it later.</p>
          <div style="margin-top:14px;display:flex;gap:8px">
            <button type="button" class="secondary-btn" onclick="document.querySelector('.tab[data-tab=settings]').click(); document.getElementById('onboarding-wizard').close()">Open Integrations now</button>
          </div>
        `,
        onNext: async () => {}
      },

      // 6. Phone receptionist — auto-provisioned, no setup required.
      // The brain creates the customer's Twilio number on Stripe
      // checkout and includes the cost in their monthly subscription.
      // Owner just sees the number Orbi will answer on.
      {
        title: "Your phone receptionist",
        nextLabel: "Continue",
        showSkip: false,
        render: () => `
          <p style="font-size:14px">Orbi answers your business calls 24/7 — already set up, already paid for, no Twilio account on your end.</p>
          <div id="wiz-phone-number" style="background:#0b0f1a;border:1px solid #2dd4bf;border-radius:8px;padding:14px 16px;margin:14px 0;font-size:18px;color:#fff;text-align:center;font-weight:600">Looking up your number…</div>
          <p class="muted" style="font-size:13px;margin-top:8px">Give that number to customers, put it on your business cards, your website, your menus. Orbi answers every call — captures leads, takes orders, books appointments — whatever you've taught her.</p>
        `,
        onShow: async () => {
          const target = document.getElementById('wiz-phone-number');
          if (!target) return;
          try {
            const info = await api('/api/owner/business_info');
            const number = (info.phone || (info.contact && info.contact.phone) || '').trim();
            target.textContent = number || 'Number provisioning — check Settings → Phone in a few minutes';
          } catch {
            target.textContent = 'Open Settings → Phone to see your number';
          }
        },
        onNext: async () => {}
      },

      // 7. Done
      {
        title: "You're set up 🎉",
        nextLabel: "Finish",
        showBack: false,
        showSkip: false,
        render: () => `
          <div style="text-align:center;padding:14px 0">
            <div style="font-size:48px;margin-bottom:12px">🎉</div>
            <h3 style="margin-top:0">Orbi knows your business.</h3>
            <p style="font-size:14px;max-width:480px;margin:14px auto;color:#b8c6e0">
              Click any tab to start. Try Ask Orbi → type "tell me about my business" — she should now know everything you just taught her.
            </p>
            <p class="muted" style="font-size:13px;margin-top:14px">
              You can re-run this wizard any time from Settings → First-time setup.
            </p>
          </div>
        `,
        onNext: async () => {
          document.getElementById('onboarding-wizard').close();
        }
      },
    ];
  }

  function deepMerge(a, b) {
    const out = {...a};
    for (const k of Object.keys(b)) {
      if (b[k] && typeof b[k] === 'object' && !Array.isArray(b[k])) {
        out[k] = deepMerge(out[k] || {}, b[k]);
      } else if (b[k] !== undefined && b[k] !== null && b[k] !== "") {
        out[k] = b[k];
      }
    }
    return out;
  }

  async function renderWizStep() {
    const screens = wizardScreens();
    const i = _wizState.step;
    const s = screens[i];
    if (!s) return;
    document.getElementById('wiz-step-num').textContent = (i+1);
    document.getElementById('wiz-title').textContent = s.title;
    document.getElementById('wiz-body').innerHTML = s.render();
    const nextBtn = document.getElementById('wiz-next-btn');
    nextBtn.textContent = s.nextLabel || "Next →";
    nextBtn.disabled = false;
    document.getElementById('wiz-back-btn').hidden = (s.showBack === false) || i === 0;
    // Skip button: default to VISIBLE on every screen — Frank 2026-06-23.
    // A screen can opt out by explicitly setting showSkip: false.
    document.getElementById('wiz-skip-btn').hidden = s.showSkip === false;
    if (s.onShow) {
      try { await s.onShow(); } catch (e) { console.warn(e); }
    }
  }

  function wireWizard() {
    const open = document.getElementById('open-onboarding-btn');
    const dlg  = document.getElementById('onboarding-wizard');
    const next = document.getElementById('wiz-next-btn');
    const back = document.getElementById('wiz-back-btn');
    const skip = document.getElementById('wiz-skip-btn');
    const close = document.getElementById('wiz-close-btn');
    if (!dlg) return;

    open?.addEventListener('click', () => {
      _wizState = {step: 0, website: "", draft: null, gapAnswers: {},
                   currentGap: 0, gapQuestions: []};
      renderWizStep();
      dlg.showModal();
    });

    close?.addEventListener('click', () => dlg.close());

    next.addEventListener('click', async () => {
      next.disabled = true;
      const screens = wizardScreens();
      const s = screens[_wizState.step];
      const errOrStay = await s.onNext?.();
      if (errOrStay === "STAY") {
        next.disabled = false;
        return;
      }
      if (typeof errOrStay === 'string' && errOrStay) {
        // Show error inline
        const body = document.getElementById('wiz-body');
        body.insertAdjacentHTML('beforeend',
          `<div style="color:#ffb0b0;margin-top:10px;font-size:13px">${esc(errOrStay)}</div>`);
        next.disabled = false;
        return;
      }
      _wizState.step++;
      if (_wizState.step >= screens.length) {
        dlg.close();
        return;
      }
      renderWizStep();
    });

    back.addEventListener('click', () => {
      if (_wizState.step > 0) {
        _wizState.step--;
        renderWizStep();
      }
    });

    skip.addEventListener('click', () => {
      _wizState.step++;
      const screens = wizardScreens();
      if (_wizState.step >= screens.length) {
        dlg.close();
        return;
      }
      renderWizStep();
    });

    // Auto-open if business_info looks empty on first load.
    // BUT — the first-time password-set modal (rendered by the IIFE at
    // the top of this file) needs to land first. HTML <dialog>'s top-
    // layer rendering would cover our overlay regardless of z-index,
    // so we defer auto-opening the wizard while the password overlay
    // is up. Once the customer submits, the page reloads and this
    // check passes on the next pass.
    setTimeout(async () => {
      try {
        if (document.getElementById('orbi-set-password-overlay')) return;
        const biz = await api('/api/owner/business_info');
        const looksEmpty = !biz.name || biz.name === "REPLACE_WITH_BUSINESS_NAME";
        if (looksEmpty && !sessionStorage.getItem('orbi-wiz-shown')) {
          sessionStorage.setItem('orbi-wiz-shown', '1');
          _wizState = {step: 0, website: "", draft: null, gapAnswers: {},
                       currentGap: 0, gapQuestions: []};
          renderWizStep();
          dlg.showModal();
        }
      } catch {}
    }, 1500);
  }
  document.addEventListener('DOMContentLoaded', wireWizard);

  // ------------------------------------------------------------------
  // Web Tasks tab — Orbi drives a real Chrome for the owner.
  // Polls each running task for status. When a task lands in
  // status=awaiting_confirmation, pops the confirmation modal so the
  // owner can approve or decline the pending action.
  // ------------------------------------------------------------------
  function wireWebTasks() {
    const goalEl   = document.getElementById('web-agent-goal');
    const urlEl    = document.getElementById('web-agent-url');
    const recipeEl = document.getElementById('web-agent-recipe');
    const startBtn = document.getElementById('web-agent-start-btn');
    const runsEl   = document.getElementById('web-agent-runs');
    const dlg      = document.getElementById('web-agent-confirm-dialog');
    const actionEl = document.getElementById('web-agent-confirm-action');
    const approveBtn = document.getElementById('web-agent-confirm-approve');
    const denyBtn  = document.getElementById('web-agent-confirm-deny');

    if (!goalEl || !startBtn) return;

    const pollers = {};        // task_id → setTimeout id
    let activeConfirmTaskId = null;

    async function loadRecipes() {
      try {
        const r = await api('/api/owner/web_agent/recipes');
        const list = (r && r.recipes) || [];
        // Clear all but the "no recipe" placeholder
        recipeEl.innerHTML = '<option value="">No recipe (free-form)</option>';
        list.forEach(name => {
          const opt = document.createElement('option');
          opt.value = name;
          opt.textContent = name;
          recipeEl.appendChild(opt);
        });
      } catch (e) { /* silent — endpoint may not be up yet */ }
    }

    async function loadRecent() {
      try {
        const r = await api('/api/owner/web_agent/recent');
        const runs = (r && r.runs) || [];
        if (!runs.length) {
          runsEl.innerHTML = '<p class="muted" style="font-size:13px">No tasks yet. Start one above.</p>';
          return;
        }
        runsEl.innerHTML = '';
        runs.forEach(renderRun);
      } catch {}
    }

    function statusBadge(status) {
      const colors = {
        running:                {bg:'#1e3a5f', fg:'#9ad6ff', label:'Running…'},
        awaiting_confirmation:  {bg:'#5f4f1e', fg:'#ffe39a', label:'Needs your OK'},
        done:                   {bg:'#1e5f3a', fg:'#9affb5', label:'Done'},
        failed:                 {bg:'#5f1e2e', fg:'#ff9ab5', label:'Failed'},
        declined:               {bg:'#3a3a3a', fg:'#b8b8b8', label:'Declined'},
        declined_timeout:       {bg:'#3a3a3a', fg:'#b8b8b8', label:'Timed out'},
        crashed:                {bg:'#5f1e2e', fg:'#ff9ab5', label:'Crashed'},
      };
      const c = colors[status] || {bg:'#2c3957', fg:'#aab0bc', label:status||'unknown'};
      return `<span style="background:${c.bg};color:${c.fg};padding:3px 10px;border-radius:99px;font-size:11px;font-weight:600">${c.label}</span>`;
    }

    function renderRun(task) {
      const id = task.task_id;
      const existing = document.getElementById('run-' + id);
      const startedFmt = task.started_at
        ? new Date(task.started_at * 1000).toLocaleString()
        : '';
      const elapsed = task.result ? `${task.result.elapsed_seconds || ''}s` : '';
      const stopped = task.result ? (task.result.stopped_reason || '') : '';
      const downloads = (task.result && task.result.downloaded_files) || [];
      const dlList = downloads.length
        ? `<div style="margin-top:8px;font-size:12px;color:#9ad6ff">Saved to your Orbi folder: ${downloads.map(p => p.split(/[/\\]/).pop()).join(', ')}</div>`
        : '';
      const html = `
        <div id="run-${id}" style="background:#0b1224;border:1px solid #1f2c4a;border-radius:10px;padding:14px;">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
            <div style="flex:1;min-width:0">
              <div style="font-size:14px;color:#eaf0ff;word-break:break-word">${esc(task.goal || '')}</div>
              <div class="muted" style="font-size:12px;margin-top:3px">${startedFmt} ${elapsed ? ' · ' + esc(elapsed) : ''} ${stopped ? ' · ' + esc(stopped) : ''}</div>
            </div>
            <div>${statusBadge(task.status)}</div>
          </div>
          ${dlList}
        </div>
      `;
      if (existing) {
        existing.outerHTML = html;
      } else {
        runsEl.insertAdjacentHTML('afterbegin', html);
      }
    }

    async function pollTask(taskId) {
      try {
        const r = await api(`/api/owner/web_agent/status/${encodeURIComponent(taskId)}`);
        renderRun(r);
        if (r.status === 'awaiting_confirmation' && activeConfirmTaskId !== taskId) {
          activeConfirmTaskId = taskId;
          const a = r.pending_action || {};
          actionEl.textContent = JSON.stringify(a, null, 2);
          if (typeof dlg.showModal === 'function') dlg.showModal();
        }
        if (['done','failed','declined','declined_timeout','crashed'].includes(r.status)) {
          delete pollers[taskId];
          return; // stop polling
        }
      } catch (e) { /* keep trying */ }
      pollers[taskId] = setTimeout(() => pollTask(taskId), 1500);
    }

    startBtn.addEventListener('click', async () => {
      const goal = (goalEl.value || '').trim();
      if (!goal) { goalEl.focus(); return; }
      startBtn.disabled = true;
      startBtn.textContent = 'Starting…';
      try {
        const body = {
          goal,
          start_url: (urlEl.value || '').trim() || null,
          recipe:    recipeEl.value || null,
          recipe_params: {},
        };
        const r = await api('/api/owner/web_agent/run', {
          method: 'POST', body: JSON.stringify(body),
        });
        goalEl.value = '';
        urlEl.value = '';
        recipeEl.value = '';
        renderRun({task_id: r.task_id, goal, status: 'running',
                    started_at: Date.now()/1000});
        pollTask(r.task_id);
      } catch (e) {
        alert('Could not start: ' + (e.message || 'unknown error'));
      } finally {
        startBtn.disabled = false;
        startBtn.textContent = 'Run';
      }
    });

    async function decideConfirm(approve) {
      const taskId = activeConfirmTaskId;
      if (!taskId) return;
      try {
        await api(`/api/owner/web_agent/confirm/${encodeURIComponent(taskId)}`, {
          method: 'POST', body: JSON.stringify({approve}),
        });
      } catch (e) { /* still close the modal */ }
      activeConfirmTaskId = null;
      try { dlg.close(); } catch {}
    }
    approveBtn.addEventListener('click', () => decideConfirm(true));
    denyBtn.addEventListener('click',    () => decideConfirm(false));

    // Lazy load when the tab is opened
    document.querySelectorAll('.tab[data-tab="web_tasks"]').forEach(btn => {
      btn.addEventListener('click', () => {
        loadRecipes();
        loadRecent();
      });
    });
  }
  document.addEventListener('DOMContentLoaded', wireWebTasks);

  // ------------------------------------------------------------------
  // Help tab — renders orbi_capabilities.md
  // ------------------------------------------------------------------

  // Tiny markdown → HTML. Handles only what the capabilities doc uses:
  // h1/h2/h3, paragraphs, bold/italic, bullet lists, pipe tables, hr.
  // Deliberately not pulling in a markdown lib — zero new deps.
  function mdToHtml(md) {
    const escape = (s) => s.replace(/[&<>]/g,
      (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    const inline = (s) => escape(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

    const lines = md.replace(/\r\n/g, '\n').split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*$/.test(line)) { i++; continue; }
      if (/^---+\s*$/.test(line)) { out.push('<hr>'); i++; continue; }
      let m;
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        const level = m[1].length;
        out.push(`<h${level}>${inline(m[2])}</h${level}>`);
        i++; continue;
      }
      // Pipe table
      if (line.includes('|') && i + 1 < lines.length && /^\s*\|?[-:\s|]+\|?\s*$/.test(lines[i + 1])) {
        const header = line.split('|').map(s => s.trim()).filter(Boolean);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes('|')) {
          rows.push(lines[i].split('|').map(s => s.trim()).filter((s, idx, arr) => !(idx === 0 && s === '') && !(idx === arr.length - 1 && s === '')));
          i++;
        }
        out.push('<table><thead><tr>'
          + header.map(h => `<th>${inline(h)}</th>`).join('')
          + '</tr></thead><tbody>'
          + rows.map(r => '<tr>' + r.map(c => `<td>${inline(c)}</td>`).join('') + '</tr>').join('')
          + '</tbody></table>');
        continue;
      }
      // Bullet list
      if (/^\s*[-*]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*[-*]\s+/, ''));
          i++;
        }
        out.push('<ul>' + items.map(it => `<li>${inline(it)}</li>`).join('') + '</ul>');
        continue;
      }
      // Ordered list
      if (/^\s*\d+\.\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*\d+\.\s+/, ''));
          i++;
        }
        out.push('<ol>' + items.map(it => `<li>${inline(it)}</li>`).join('') + '</ol>');
        continue;
      }
      // Paragraph (consume until blank line or block start)
      const para = [];
      while (i < lines.length
             && !/^\s*$/.test(lines[i])
             && !/^#{1,6}\s/.test(lines[i])
             && !/^---+\s*$/.test(lines[i])
             && !/^\s*[-*]\s+/.test(lines[i])
             && !/^\s*\d+\.\s+/.test(lines[i])
             && !lines[i].includes('|')) {
        para.push(lines[i]);
        i++;
      }
      if (para.length) out.push('<p>' + inline(para.join(' ')) + '</p>');
    }
    return out.join('\n');
  }

  let _helpLoaded = false;
  async function loadHelpDoc() {
    if (_helpLoaded) return;
    const target = document.getElementById('help-content');
    if (!target) return;
    try {
      // redirect:'error' = treat 302 as a failure instead of silently
      // following to "/" and rendering the landing page as raw text in
      // the Help tab (happens when the server hasn't picked up the new
      // /api/help/capabilities route yet).
      const resp = await fetch('/api/help/capabilities', {redirect: 'error'});
      if (!resp.ok) throw new Error('http ' + resp.status);
      const ctype = (resp.headers.get('Content-Type') || '').toLowerCase();
      const md = await resp.text();
      // Guard against HTML being served when the route is missing.
      if (md.trim().startsWith('<!doctype') || md.trim().startsWith('<html')
          || (!ctype.includes('markdown') && !ctype.includes('text/plain'))) {
        throw new Error('unexpected content-type: ' + ctype);
      }
      target.innerHTML = mdToHtml(md);
      _helpLoaded = true;
    } catch (e) {
      target.innerHTML = '<p class="help-error">'
        + 'The help guide isn\'t loaded on this Orbi yet. '
        + 'Restart Orbi to pick up the new help content '
        + '(or wait for your next auto-update).'
        + '</p>';
    }
  }

  function wireHelpTab() {
    const tab = document.querySelector('.tab[data-tab="help"]');
    if (tab) tab.addEventListener('click', loadHelpDoc);

    const search = document.getElementById('help-search');
    if (search) {
      search.addEventListener('input', () => {
        const q = search.value.trim().toLowerCase();
        const content = document.getElementById('help-content');
        if (!content) return;
        // Show/hide rows in tables + list items + paragraphs by match
        content.querySelectorAll('tr, li, p, h2, h3').forEach((el) => {
          if (!q) { el.style.display = ''; return; }
          el.style.display = el.textContent.toLowerCase().includes(q) ? '' : 'none';
        });
      });
    }

    const tourBtn = document.getElementById('help-tour-btn');
    if (tourBtn) {
      tourBtn.addEventListener('click', () => {
        const chatTab = document.querySelector('.tab[data-tab="chat"]');
        if (chatTab) chatTab.click();
        const input = document.getElementById('chat-input')
                   || document.querySelector('#tab-chat textarea, #tab-chat input[type=text]');
        if (input) {
          input.value = 'Walk me through what you can do.';
          input.focus();
          // Submit if there's a send button
          const send = document.getElementById('chat-send')
                    || document.querySelector('#tab-chat button[type=submit], #tab-chat .send-btn');
          if (send) send.click();
        }
      });
    }
  }
  document.addEventListener('DOMContentLoaded', wireHelpTab);

  // ------------------------------------------------------------------
  // In-app notification toasts
  // ------------------------------------------------------------------
  // Polls the inbox every 15s. New notifications appear as toasts in
  // the bottom-right. This is the SAFETY-NET channel that fires when
  // push/email/sms aren't configured — without it, reminders fire
  // silently in the background and the user never sees them.

  const _shownToasts = new Set();

  function _toastIcon(event) {
    if (event === 'reminder_due') return '⏰';
    if (event === 'new_lead')     return '⚡';
    if (event === 'new_message')  return '✉️';
    if (event === 'new_voicemail')return '☎️';
    if (event && event.startsWith('watchdog')) return '\u{1F6E0}';
    return '\u{1F514}';
  }
  function _toastClass(event) {
    if (event === 'reminder_due')   return 'reminder';
    if (event === 'new_lead')       return 'lead';
    if (event === 'new_voicemail')  return 'message';
    if (event === 'new_message')    return 'message';
    return '';
  }

  function _showToast(n) {
    const stack = document.getElementById('toast-stack');
    if (!stack) return;
    // Reminders get the LOUD path — persistent, spoken, repeats until ack.
    if (n.event === 'reminder_due') {
      _showReminderBanner(n);
      return;
    }
    const el = document.createElement('div');
    el.className = 'toast ' + _toastClass(n.event);
    el.setAttribute('role', 'alert');
    el.innerHTML =
      '<div class="toast-icon">' + _toastIcon(n.event) + '</div>' +
      '<div class="toast-body">' +
        '<div class="toast-title"></div>' +
        '<div class="toast-text"></div>' +
      '</div>' +
      '<button class="toast-close" aria-label="Dismiss">×</button>';
    el.querySelector('.toast-title').textContent = n.title || 'Notification';
    el.querySelector('.toast-text').textContent  = n.body  || '';
    stack.appendChild(el);
    const dismiss = function () {
      if (!el.parentNode) return;
      el.classList.add('dismissing');
      setTimeout(function () { el.remove(); }, 220);
      fetch('/api/owner/notifications/' + n.id + '/seen', {method: 'POST'})
        .catch(function () {});
    };
    el.querySelector('.toast-close').addEventListener('click', function (e) {
      e.stopPropagation();
      dismiss();
    });
    el.addEventListener('click', function () {
      if (n.url) {
        const tabName = n.url.replace(/^\/+|\/+$/g, '').split('/').pop();
        const tab = document.querySelector('.tab[data-tab="' + tabName + '"]');
        if (tab) tab.click();
      }
      dismiss();
    });
    setTimeout(dismiss, 12000);
  }

  // ────────────────────────────────────────────────────────────────────
  // Reminder banner — the LOUD path: persistent, spoken via TTS,
  // re-fires every 3 minutes until the user clicks "Got it". Maxes out
  // after 3 nag-cycles so we don't infinite-loop on a forgotten reminder.
  // ────────────────────────────────────────────────────────────────────
  const _REMINDER_NAG_INTERVAL_MS = 3 * 60 * 1000;  // 3 min between nags
  const _REMINDER_MAX_NAGS = 3;                      // 3 nag cycles total

  function _speakReminder(text) {
    try {
      const a = new Audio('/tts?voice=' + encodeURIComponent(ORBI_TTS_VOICE)
        + '&text=' + encodeURIComponent(text));
      a.volume = 1.0;
      // Browsers can block auto-play until the user has interacted with
      // the page. A user who's been clicking around the dashboard will
      // satisfy that; a fresh-load no-interact case will silently fail
      // and the chime + visual banner are still there as the safety net.
      const p = a.play();
      if (p && typeof p.catch === 'function') p.catch(function () {});
    } catch (e) { /* ignore */ }
  }

  function _showReminderBanner(n) {
    const stack = document.getElementById('toast-stack');
    if (!stack) return;
    const el = document.createElement('div');
    el.className = 'toast reminder reminder-banner';
    el.setAttribute('role', 'alertdialog');
    el.innerHTML =
      '<div class="toast-icon">⏰</div>' +
      '<div class="toast-body">' +
        '<div class="toast-title"></div>' +
        '<div class="toast-text"></div>' +
        '<div class="reminder-actions">' +
          '<button class="primary-btn reminder-ack-btn">Got it</button>' +
          '<button class="secondary-btn reminder-snooze-btn">Remind me in 5 min</button>' +
        '</div>' +
      '</div>';
    el.querySelector('.toast-title').textContent = n.title || 'Reminder';
    el.querySelector('.toast-text').textContent  = n.body  || '';
    stack.appendChild(el);

    let nagCount = 0;
    let nagTimer = null;
    let dismissed = false;
    const spokenText = 'Hey Frank, this is your reminder. ' + (n.body || '');

    // Also drop the reminder into the chat so it's part of the conversation
    // record — when Frank comes back later he can see what Orbi said and
    // when, even if he missed the live ding/voice. Done only on the FIRST
    // fire (not on every nag repeat) so we don't spam the chat.
    try {
      const now = new Date();
      const stamp = now.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
      addOwnerBubble('assistant',
                     '⏰ ' + stamp + ' — ' + spokenText);
    } catch (e) { /* chat scope not ready — banner + chime are still up */ }

    function fireRound() {
      if (dismissed) return;
      try { _playChime(); } catch (e) {}
      _speakReminder(spokenText);
      nagCount += 1;
      if (nagCount >= _REMINDER_MAX_NAGS) {
        // Last nag — turn off auto-re-fire but keep banner on screen.
        return;
      }
      nagTimer = setTimeout(fireRound, _REMINDER_NAG_INTERVAL_MS);
    }
    fireRound();

    function gotIt() {
      if (dismissed) return;
      dismissed = true;
      if (nagTimer) clearTimeout(nagTimer);
      el.classList.add('dismissing');
      setTimeout(function () { el.remove(); }, 220);
      fetch('/api/owner/notifications/' + n.id + '/ack', {method: 'POST'})
        .catch(function () {});
    }
    function snooze5() {
      if (dismissed) return;
      dismissed = true;
      if (nagTimer) clearTimeout(nagTimer);
      el.classList.add('dismissing');
      setTimeout(function () { el.remove(); }, 220);
      // Mark seen so the poll doesn't refire it; the 5-min snooze is
      // purely a client-side re-toast after the wait.
      fetch('/api/owner/notifications/' + n.id + '/seen', {method: 'POST'})
        .catch(function () {});
      setTimeout(function () {
        // re-toast with a fresh banner (server-side already marked seen,
        // but client can synthesize a new banner with the same body)
        _shownToasts.delete(n.id);  // allow the same id to nag again
        _showReminderBanner(Object.assign({}, n, {title: '⏰ Snoozed reminder'}));
      }, 5 * 60 * 1000);
    }

    el.querySelector('.reminder-ack-btn').addEventListener('click', function (e) {
      e.stopPropagation();
      gotIt();
    });
    el.querySelector('.reminder-snooze-btn').addEventListener('click', function (e) {
      e.stopPropagation();
      snooze5();
    });
  }

  let _chimeCtx = null;
  function _playChime() {
    if (!_chimeCtx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      _chimeCtx = new AC();
    }
    const ctx = _chimeCtx;
    const t = ctx.currentTime;
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.type = 'sine'; o.frequency.value = 880;
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(0.18, t + 0.02);
    g.gain.linearRampToValueAtTime(0, t + 0.4);
    o.start(t); o.stop(t + 0.45);
  }

  // Anchor "newness" to when this tab loaded. Items with ts <= this are
  // pre-existing and won't toast in this session, but stay UNSEEN in the
  // DB so we don't lose them. Items with ts > this DO toast — including
  // ones that fire while the dashboard is open. CRITICAL: never call
  // mark_all_seen on mount — that races with brand-new fires and silently
  // eats them, which is what burned us in the 5:34/5:38 PM reminders.
  const _sessionStartTs = Date.now() / 1000;

  async function _pollInbox() {
    try {
      const resp = await fetch('/api/owner/notifications/inbox?unseen=1');
      if (!resp.ok) return;
      const body = await resp.json();
      const items = body.items || [];
      // Process oldest-first within this poll so toasts stack naturally
      items.sort(function (a, b) { return (a.ts || 0) - (b.ts || 0); });
      for (const n of items) {
        if (_shownToasts.has(n.id)) continue;
        _shownToasts.add(n.id);
        if ((n.ts || 0) <= _sessionStartTs) {
          // Pre-existing at mount — skip the toast but remember we
          // saw it so a later poll doesn't double-process it.
          continue;
        }
        _showToast(n);
      }
    } catch (e) { /* offline / paused — try again next tick */ }
  }

  function wireInboxPolling() {
    // Run the first poll immediately so a reminder that fired in the
    // last 15s isn't missed.
    _pollInbox();
    setInterval(_pollInbox, 15000);
  }
  document.addEventListener('DOMContentLoaded', wireInboxPolling);

  // ------------------------------------------------------------------
  // IMAP/SMTP email accounts (Yahoo, iCloud, AOL, Fastmail, custom)
  // ------------------------------------------------------------------
  let _imapProviders = null;

  async function _loadImapProviders() {
    if (_imapProviders) return _imapProviders;
    try {
      const r = await fetch('/api/owner/email/imap/providers');
      _imapProviders = (await r.json()).providers || [];
    } catch { _imapProviders = []; }
    return _imapProviders;
  }

  function _renderImapAccount(a) {
    const div = document.createElement('div');
    div.className = 'imap-account-row';
    div.innerHTML =
      '<div class="imap-account-info">' +
        '<div class="imap-account-email"></div>' +
        '<div class="imap-account-meta muted"></div>' +
      '</div>' +
      '<div class="imap-account-actions">' +
        '<button type="button" class="secondary-btn imap-test">Test</button>' +
        '<button type="button" class="secondary-btn imap-remove">Remove</button>' +
      '</div>';
    div.querySelector('.imap-account-email').textContent = a.email;
    div.querySelector('.imap-account-meta').textContent =
      (a.label && a.label !== a.email ? a.label + ' • ' : '') +
      (a.provider || 'custom') + ' • ' + a.imap_host;
    div.querySelector('.imap-test').addEventListener('click', async () => {
      const btn = div.querySelector('.imap-test');
      btn.disabled = true; btn.textContent = 'Testing…';
      try {
        const r = await fetch('/api/owner/email/imap/accounts/' + a.id + '/test',
                              {method: 'POST'});
        const j = await r.json();
        btn.textContent = j.ok ? 'OK ✓' : 'Failed';
        if (!j.ok) alert('Test failed: ' + (j.error || 'unknown'));
      } finally {
        setTimeout(() => { btn.disabled = false; btn.textContent = 'Test'; }, 2500);
      }
    });
    div.querySelector('.imap-remove').addEventListener('click', async () => {
      if (!confirm('Disconnect ' + a.email + '?')) return;
      await fetch('/api/owner/email/imap/accounts/' + a.id, {method: 'DELETE'});
      _refreshImapAccounts();
    });
    return div;
  }

  async function _refreshImapAccounts() {
    const list = document.getElementById('imap-accounts-list');
    if (!list) return;
    try {
      const r = await fetch('/api/owner/email/imap/accounts');
      const accounts = (await r.json()).accounts || [];
      list.innerHTML = '';
      if (accounts.length === 0) {
        list.innerHTML = '<p class="muted" style="margin:6px 0">No email accounts connected yet.</p>';
        return;
      }
      for (const a of accounts) list.appendChild(_renderImapAccount(a));
    } catch (e) {
      list.innerHTML = '<p class="muted">Failed to load accounts.</p>';
    }
  }

  async function _openImapDialog() {
    const providers = await _loadImapProviders();
    const dlg      = document.getElementById('imap-dialog');
    const sel      = document.getElementById('imap-provider');
    const helpEl   = document.getElementById('imap-help');
    const custom   = document.getElementById('imap-custom-fields');
    if (!dlg || !sel) return;
    sel.innerHTML = '';
    for (const p of providers) {
      const opt = document.createElement('option');
      opt.value = p.id; opt.textContent = p.label;
      sel.appendChild(opt);
    }
    function syncProvider() {
      const p = providers.find(x => x.id === sel.value) || providers[0];
      if (!p) return;
      helpEl.innerHTML = '';
      // Top warning block — only shown for providers that require an
      // app password. Customers consistently miss the one-line help
      // text and try their normal email password, so make it loud and
      // unmissable.
      if (p.warning) {
        const warn = document.createElement('div');
        warn.style.cssText =
          'background:#3a2a16; border:1px solid #f0a23a; color:#ffd9a3;' +
          'padding:10px 12px; border-radius:8px; font-size:13px;' +
          'margin-bottom:10px; line-height:1.4;';
        warn.textContent = '⚠ ' + p.warning;
        helpEl.appendChild(warn);
      }
      // Provider-specific setup walkthrough — numbered list rendered
      // from the array in PROVIDER_PRESETS. Falls back to the single-
      // line `help` blurb when the preset doesn't ship steps yet.
      if (p.setup_steps && p.setup_steps.length) {
        if (p.help_url) {
          const linkP = document.createElement('p');
          linkP.style.cssText = 'margin:0 0 8px';
          const a = document.createElement('a');
          a.href = p.help_url; a.target = '_blank'; a.rel = 'noopener';
          a.textContent = 'Open settings ↗';
          linkP.appendChild(document.createTextNode('Step 0: '));
          linkP.appendChild(a);
          linkP.appendChild(document.createTextNode(' (opens in a new tab)'));
          helpEl.appendChild(linkP);
        }
        const ol = document.createElement('ol');
        ol.style.cssText = 'margin:6px 0 8px 18px; padding:0; font-size:13px; line-height:1.45;';
        for (const step of p.setup_steps) {
          const li = document.createElement('li');
          li.style.marginBottom = '4px';
          li.textContent = step;
          ol.appendChild(li);
        }
        helpEl.appendChild(ol);
      } else {
        const helpText = document.createElement('span');
        helpText.textContent = p.help || '';
        helpEl.appendChild(helpText);
        if (p.help_url) {
          helpEl.appendChild(document.createTextNode(' '));
          const a = document.createElement('a');
          a.href = p.help_url; a.target = '_blank'; a.rel = 'noopener';
          a.textContent = 'Open settings ↗';
          helpEl.appendChild(a);
        }
      }
      // Update the password field's label so customers see "App
      // Password (16 characters from Yahoo)" instead of the generic
      // "Password" prompt that confused everyone in early tests.
      const pwLabel = document.getElementById('imap-password-label');
      if (pwLabel && p.password_label) {
        const labelText = p.password_label +
          (p.needs_app_pw ? ' — NOT your normal email password' : '');
        const pwInput = pwLabel.querySelector('input, div');
        pwLabel.firstChild && (pwLabel.firstChild.nodeValue = labelText + ' ');
      }
      custom.hidden = (p.id !== 'custom');
      document.getElementById('imap-host').value = p.imap_host || '';
      document.getElementById('imap-port').value = p.imap_port || '';
      document.getElementById('smtp-host').value = p.smtp_host || '';
      document.getElementById('smtp-port').value = p.smtp_port || '';
    }
    sel.addEventListener('change', syncProvider);
    syncProvider();
    document.getElementById('imap-email').value = '';
    const pwField = document.getElementById('imap-password');
    pwField.value = '';
    // Reset the password field to masked every time the dialog opens —
    // never persist "show" state across openings, so a forgotten "show"
    // doesn't leave the password visible on the next account add.
    pwField.type = 'password';
    const toggleBtn = document.getElementById('imap-password-toggle');
    if (toggleBtn && !toggleBtn.dataset.wired) {
      toggleBtn.addEventListener('click', () => {
        const field = document.getElementById('imap-password');
        if (field.type === 'password') {
          field.type = 'text';
          toggleBtn.textContent = '🙈';
          toggleBtn.title = 'Hide password';
        } else {
          field.type = 'password';
          toggleBtn.textContent = '👁';
          toggleBtn.title = 'Show password';
        }
      });
      toggleBtn.dataset.wired = '1';
    }
    if (toggleBtn) { toggleBtn.textContent = '👁'; toggleBtn.title = 'Show password'; }
    document.getElementById('imap-save-status').textContent = '';
    if (typeof dlg.showModal === 'function') dlg.showModal();
    else dlg.setAttribute('open', '');
  }

  async function _saveImapAccount() {
    const status = document.getElementById('imap-save-status');
    status.textContent = 'Testing connection…';
    status.style.color = '';
    const provider = document.getElementById('imap-provider').value;
    const payload = {
      provider:  provider,
      email:     document.getElementById('imap-email').value.trim(),
      password:  document.getElementById('imap-password').value,
    };
    if (provider === 'custom') {
      payload.imap_host = document.getElementById('imap-host').value.trim();
      payload.imap_port = parseInt(document.getElementById('imap-port').value, 10);
      payload.smtp_host = document.getElementById('smtp-host').value.trim();
      payload.smtp_port = parseInt(document.getElementById('smtp-port').value, 10);
    }
    try {
      const r = await fetch('/api/owner/email/imap/accounts', {
        method:  'POST',
        headers: {'Content-Type': 'application/json'},
        body:    JSON.stringify(payload),
      });
      const j = await r.json();
      if (j.ok) {
        status.style.color = '#82e9a5';
        status.textContent = 'Connected ✓';
        setTimeout(() => {
          document.getElementById('imap-dialog').close();
          _refreshImapAccounts();
        }, 800);
      } else {
        status.style.color = '#ff8b8b';
        status.textContent = j.error || 'Failed';
      }
    } catch (e) {
      status.style.color = '#ff8b8b';
      status.textContent = String(e);
    }
  }

  function wireImap() {
    const addBtn = document.getElementById('imap-add-btn');
    if (addBtn) addBtn.addEventListener('click', _openImapDialog);
    const saveBtn = document.getElementById('imap-save-btn');
    if (saveBtn) saveBtn.addEventListener('click', _saveImapAccount);
    const cancelBtn = document.getElementById('imap-cancel-btn');
    if (cancelBtn) cancelBtn.addEventListener('click', () => {
      document.getElementById('imap-dialog').close();
    });
    // Lazy-load accounts when Settings tab opens
    const settingsTab = document.querySelector('.tab[data-tab="settings"]');
    if (settingsTab) settingsTab.addEventListener('click', _refreshImapAccounts);
    // Initial load if Settings is already the active tab
    if (document.getElementById('tab-settings')?.classList.contains('active')) {
      _refreshImapAccounts();
    }
  }
  document.addEventListener('DOMContentLoaded', wireImap);


  // ====================================================================
  // STAFF MANAGEMENT (Settings tab)
  // ====================================================================
  function _esc(s) { return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

  async function _loadStaff() {
    const list = document.getElementById('staff-list');
    if (!list) return;
    try {
      const r = await fetch('/api/owner/staff');
      if (!r.ok) throw new Error('Failed to load staff');
      const data = await r.json();
      const active = data.active || [];
      const archived = data.archived || [];
      if (!active.length && !archived.length) {
        list.innerHTML = '<div class="muted" style="font-size:13px">No staff users yet. Add one below.</div>';
        return;
      }
      let html = '';
      if (active.length) {
        html += '<div style="font-size:12px;color:#9aa4c0;margin-bottom:6px">Active staff</div>';
        for (const u of active) {
          html += _renderStaffRow(u, false);
        }
      }
      if (archived.length) {
        html += '<div style="font-size:12px;color:#9aa4c0;margin:14px 0 6px">Archived (auto-purges in 90 days)</div>';
        for (const u of archived) {
          html += _renderStaffRow(u, true);
        }
      }
      list.innerHTML = html;
      _wireStaffRowActions();
    } catch (e) {
      list.innerHTML = '<div style="color:#ff7a7a;font-size:13px">' + _esc(e.message) + '</div>';
    }
  }

  function _renderStaffRow(u, archived) {
    const name = u.username || '(no name)';
    const email = u.email || '';
    const role = u.role || 'staff';
    const created = u.created_at ? new Date(u.created_at * 1000).toLocaleDateString() : '';
    const archivedAt = u.archived_at ? new Date(u.archived_at * 1000).toLocaleDateString() : '';
    const hold = u.purge_hold ? ' · 🔒 hold' : '';
    return `
      <div class="staff-row" data-username="${_esc(name)}" data-archived="${archived ? 1 : 0}"
           style="display:flex;gap:12px;align-items:center;padding:10px;background:#1a2240;
                  border-radius:8px;margin-bottom:6px;flex-wrap:wrap">
        <div style="flex:1;min-width:160px">
          <div style="font-weight:600">${_esc(name)} <span style="font-weight:400;font-size:12px;color:#9aa4c0">· ${_esc(role)}</span></div>
          ${email ? `<div style="font-size:12px;color:#9aa4c0">${_esc(email)}</div>` : ''}
          <div style="font-size:11px;color:#6c7592">
            ${archived ? `archived ${_esc(archivedAt)}${hold}` : (created ? `added ${_esc(created)}` : '')}
          </div>
        </div>
        <div style="display:flex;gap:6px">
          ${!archived ? `
            <button class="secondary-btn staff-reset-btn" type="button">Reset password</button>
            <button class="secondary-btn staff-deactivate-btn" type="button" style="color:#ff7a7a">Deactivate</button>
          ` : `
            <button class="primary-btn staff-reactivate-btn" type="button">Reactivate</button>
            <button class="secondary-btn staff-hold-btn" type="button">${u.purge_hold ? 'Release hold' : 'Hold from purge'}</button>
          `}
        </div>
      </div>
    `;
  }

  function _wireStaffRowActions() {
    document.querySelectorAll('.staff-row').forEach(row => {
      const username = row.dataset.username;
      row.querySelector('.staff-reset-btn')?.addEventListener('click', async () => {
        if (!confirm(`Generate a password reset link for ${username}? (24h expiry; you'll share the URL with them.)`)) return;
        try {
          const r = await fetch(`/api/owner/staff/${encodeURIComponent(username)}/reset_link`, { method: 'POST' });
          const data = await r.json();
          if (!r.ok) throw new Error(data.error || 'Failed');
          prompt(`Reset link for ${username} (expires in 24h):\n\nCopy and share via text/email:`, data.reset_url);
        } catch (e) { alert('Error: ' + e.message); }
      });
      row.querySelector('.staff-deactivate-btn')?.addEventListener('click', async () => {
        const reason = prompt(`Deactivate ${username}? Their data archives for 90 days then auto-purges. Reason (optional):`);
        if (reason === null) return;
        try {
          const r = await fetch(`/api/owner/staff/${encodeURIComponent(username)}/deactivate`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason })
          });
          const data = await r.json();
          if (!r.ok) throw new Error(data.error || 'Failed');
          _loadStaff();
        } catch (e) { alert('Error: ' + e.message); }
      });
      row.querySelector('.staff-reactivate-btn')?.addEventListener('click', async () => {
        if (!confirm(`Reactivate ${username}? Their archived data will be restored and they'll be able to log in again with their existing password.`)) return;
        try {
          const r = await fetch(`/api/owner/staff/${encodeURIComponent(username)}/reactivate`, { method: 'POST' });
          const data = await r.json();
          if (!r.ok) throw new Error(data.error || 'Failed');
          _loadStaff();
        } catch (e) { alert('Error: ' + e.message); }
      });
      row.querySelector('.staff-hold-btn')?.addEventListener('click', async () => {
        const current = row.querySelector('.staff-hold-btn').textContent.includes('Release');
        try {
          const r = await fetch(`/api/owner/staff/${encodeURIComponent(username)}/purge_hold`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hold: !current })
          });
          if (!r.ok) throw new Error('Failed');
          _loadStaff();
        } catch (e) { alert('Error: ' + e.message); }
      });
    });
  }

  function _wireStaffForm() {
    const addBtn = document.getElementById('add-staff-btn');
    if (!addBtn) return;
    addBtn.addEventListener('click', async () => {
      const username = document.getElementById('new-staff-username').value.trim();
      const email = document.getElementById('new-staff-email').value.trim();
      const password = document.getElementById('new-staff-password').value;
      const msg = document.getElementById('add-staff-msg');
      if (!username || username.length < 2) {
        msg.style.color = '#ff7a7a'; msg.textContent = 'Username required (2+ chars)'; return;
      }
      if (!password || password.length < 8) {
        msg.style.color = '#ff7a7a'; msg.textContent = 'Password must be 8+ characters'; return;
      }
      msg.style.color = '#9aa4c0'; msg.textContent = 'Adding...';
      try {
        const r = await fetch('/api/owner/staff', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, email, password, role: 'staff' })
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || 'Failed');
        msg.style.color = '#4ade80'; msg.textContent = 'Added ✓';
        document.getElementById('new-staff-username').value = '';
        document.getElementById('new-staff-email').value = '';
        document.getElementById('new-staff-password').value = '';
        _loadStaff();
      } catch (e) {
        msg.style.color = '#ff7a7a'; msg.textContent = e.message;
      }
    });
  }

  function wireStaff() {
    _wireStaffForm();
    const settingsTab = document.querySelector('.tab[data-tab="settings"]');
    if (settingsTab) settingsTab.addEventListener('click', _loadStaff);
    if (document.getElementById('tab-settings')?.classList.contains('active')) {
      _loadStaff();
    }
  }
  document.addEventListener('DOMContentLoaded', wireStaff);

  // QR install URL — populate the URL textbox + Copy button on Settings load
  function _wireInstallQr() {
    const refresh = async () => {
      const img = document.getElementById('install-qr-img');
      const input = document.getElementById('install-url-input');
      const hint = document.getElementById('install-url-hint');
      if (!input || !hint) return;
      try {
        const r = await fetch('/api/owner/install_url');
        const data = await r.json();
        input.value = data.url || '';
        hint.textContent = data.hint || '';
        hint.style.color = data.stable ? '#9aa4c0' : '#fbbf24';
        if (img) img.src = '/api/owner/install_qr.png?ts=' + Date.now();
      } catch (e) {
        hint.textContent = 'Could not load install URL.';
        hint.style.color = '#ff7a7a';
      }
    };
    const copyBtn = document.getElementById('install-copy-btn');
    const refreshBtn = document.getElementById('install-refresh-btn');
    if (copyBtn) copyBtn.addEventListener('click', () => {
      const input = document.getElementById('install-url-input');
      input.select();
      navigator.clipboard?.writeText(input.value);
      copyBtn.textContent = 'Copied ✓';
      setTimeout(() => copyBtn.textContent = 'Copy', 1500);
    });
    if (refreshBtn) refreshBtn.addEventListener('click', refresh);

    // "Add Orby to this device's home screen" button — visible only when
    // it makes sense (not when running standalone, not when there's no
    // way to install). Uses orbiInstallPrompt / orbiInstallState helpers
    // exposed by /pwa/register-sw.js.
    const installBtn = document.getElementById('install-on-device-btn');
    const alreadyHint = document.getElementById('install-already');
    function refreshInstallButtonState() {
      if (!installBtn) return;
      const state = (typeof window.orbiInstallState === 'function')
        ? window.orbiInstallState()
        : { installed: false, ios: false, promptReady: false };
      if (state.installed) {
        installBtn.style.display = 'none';
        if (alreadyHint) alreadyHint.style.display = 'block';
        return;
      }
      if (alreadyHint) alreadyHint.style.display = 'none';
      // Show on iOS (manual steps modal) OR when Chrome's prompt is ready
      if (state.ios || state.promptReady) {
        installBtn.style.display = 'block';
      } else {
        // No install API + not iOS — desktop browser without install support.
        // Still show the button — clicking will show a fallback message.
        installBtn.style.display = 'block';
        installBtn.textContent = '📱 How to install Orby on your phone';
      }
    }
    if (installBtn) {
      installBtn.addEventListener('click', () => {
        const state = (typeof window.orbiInstallState === 'function')
          ? window.orbiInstallState()
          : { installed: false, ios: false, promptReady: false };
        if (state.ios) {
          const modal = document.getElementById('ios-install-modal');
          if (modal && typeof modal.showModal === 'function') {
            modal.showModal();
          } else if (typeof window.orbiInstallPrompt === 'function') {
            window.orbiInstallPrompt();
          }
        } else if (typeof window.orbiInstallPrompt === 'function') {
          window.orbiInstallPrompt();
        }
      });
    }
    refreshInstallButtonState();
    window.addEventListener('orbi:install-available', refreshInstallButtonState);
    window.addEventListener('orbi:installed', refreshInstallButtonState);

    const settingsTab = document.querySelector('.tab[data-tab="settings"]');
    if (settingsTab) settingsTab.addEventListener('click', () => {
      refresh();
      refreshInstallButtonState();
    });
    if (document.getElementById('tab-settings')?.classList.contains('active')) {
      refresh();
    }
  }
  document.addEventListener('DOMContentLoaded', _wireInstallQr);


  // ====================================================================
  // TEAM CHAT — dedicated tab with iMessage-style per-staff threads
  // ====================================================================
  let _teamMe = null;             // logged-in user's identity (cached)
  // Active selection: { type: 'person' | 'group',
  //                     id:   username  | group_id,
  //                     name: display name,
  //                     members?: [usernames]  (group only),
  //                     member_names?: [display names] (group only) }
  let _teamActive = null;
  let _teamPollHandle = null;     // setInterval handle for thread polling
  let _teamGroupsCache = [];      // last-loaded groups (for sidebar render)
  let _teamStaffCache = [];       // last-loaded active staff

  async function _teamLoadMe() {
    if (_teamMe) return _teamMe;
    try {
      const r = await fetch('/api/owner/whoami');
      if (r.ok) _teamMe = await r.json();
    } catch {}
    return _teamMe;
  }

  async function _teamRefreshSidebar() {
    await _teamLoadMe();
    try {
      const [staffRes, msgRes, groupsRes] = await Promise.all([
        fetch('/api/owner/staff'),
        fetch('/api/owner/internal_messages?limit=500'),
        fetch('/api/owner/groups'),
      ]);
      const staffData = await staffRes.json();
      const msgData   = await msgRes.json();
      const groupsData = await groupsRes.json();
      _teamStaffCache  = staffData.active || [];
      _teamGroupsCache = groupsData.groups || [];
      const allMsgs = msgData.messages || [];
      _teamRenderGroups(allMsgs);
      _teamRenderStaffList(allMsgs);
      _teamRefreshBadge(msgData.unread_count);
    } catch (e) {
      const list = document.getElementById('team-staff-list');
      if (list) list.innerHTML = '<div style="color:#ff7a7a;padding:12px;font-size:13px">' + _esc(e.message) + '</div>';
    }
  }

  function _teamRefreshBadge(serverUnreadCount) {
    const badge = document.getElementById('team-unread-badge');
    if (!badge) return;
    if (serverUnreadCount > 0) {
      badge.textContent = serverUnreadCount;
      badge.hidden = false;
    } else {
      badge.hidden = true;
    }
  }

  function _teamRenderGroups(allMsgs) {
    const wrap = document.getElementById('team-groups-list');
    if (!wrap) return;
    const me = _teamMe?.username || '';
    // For each group, compute unread + last-message preview from the message
    // stream we already loaded.
    const meta = {};   // group_id → { unread, lastMsg, lastTs, lastFromName }
    for (const m of allMsgs) {
      if (!m.group_id) continue;
      const id = m.group_id;
      if (!meta[id]) meta[id] = { unread: 0, lastMsg: '', lastTs: 0, lastFromName: '' };
      if (m.created_at > meta[id].lastTs) {
        meta[id].lastTs = m.created_at;
        meta[id].lastMsg = m.body || '';
        meta[id].lastFromName = m.from_name || m.from || '';
      }
      if (!(m.read_by || []).includes(me)) {
        meta[id].unread += 1;
      }
    }
    if (!_teamGroupsCache.length) {
      wrap.innerHTML = '<div class="muted" style="padding:8px 12px;font-size:12px">No groups yet.</div>';
      return;
    }
    // Sort: Whole Team always first, then by last-message-time, then by name
    const sorted = [..._teamGroupsCache].sort((a, b) => {
      if (a.id === '__all__') return -1;
      if (b.id === '__all__') return 1;
      const ta = meta[a.id]?.lastTs || 0;
      const tb = meta[b.id]?.lastTs || 0;
      if (ta !== tb) return tb - ta;
      return (a.name || '').localeCompare(b.name || '');
    });
    let html = '';
    for (const g of sorted) {
      const mm = meta[g.id] || { unread: 0, lastMsg: '', lastFromName: '' };
      const memberCount = (g.members || []).length;
      const preview = mm.lastMsg
        ? `${mm.lastFromName ? mm.lastFromName + ': ' : ''}${mm.lastMsg.slice(0, 40).replace(/\n/g, ' ')}`
        : `${memberCount} ${memberCount === 1 ? 'person' : 'people'}`;
      const isActive = _teamActive && _teamActive.type === 'group' && _teamActive.id === g.id;
      const bg = isActive ? 'background:#2a3460' : 'background:transparent';
      const icon = g.id === '__all__' ? '🌐' : '👥';
      html += `
        <div class="team-group-row" data-group-id="${_esc(g.id)}"
              style="padding:10px 12px;border-radius:8px;cursor:pointer;${bg};margin-bottom:4px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="font-weight:600;font-size:14px">${icon} ${_esc(g.name)}</div>
            ${mm.unread > 0 ? `<span style="background:#8b5cf6;color:white;font-size:11px;padding:2px 7px;border-radius:10px">${mm.unread}</span>` : ''}
          </div>
          <div style="font-size:12px;color:#9aa4c0;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${_esc(preview)}</div>
        </div>
      `;
    }
    wrap.innerHTML = html;
    wrap.querySelectorAll('.team-group-row').forEach(row => {
      row.addEventListener('click', () => {
        const id = row.dataset.groupId;
        const g = _teamGroupsCache.find(x => x.id === id);
        if (g) _teamOpenGroup(g);
      });
    });
  }

  function _teamRenderStaffList(allMsgs) {
    const list = document.getElementById('team-staff-list');
    if (!list) return;
    const me = _teamMe?.username || '';
    const active = _teamStaffCache.filter(u => u.username !== me);
    // Compute per-person unread / last-msg meta from 1-on-1 messages only
    const meta = {};
    for (const m of allMsgs) {
      if (m.group_id) continue;
      const other = m.to === me ? m.from : m.to;
      if (!other || other === me) continue;
      if (!meta[other]) meta[other] = { unread: 0, lastMsg: '', lastTs: 0 };
      if (m.created_at > meta[other].lastTs) {
        meta[other].lastTs = m.created_at;
        meta[other].lastMsg = m.body || '';
      }
      if (m.to === me && !m.read_at) meta[other].unread += 1;
    }
    if (!active.length) {
      list.innerHTML = '<div class="muted" style="padding:8px 12px;font-size:12px">No staff to chat with yet.</div>';
      return;
    }
    active.sort((a, b) => {
      const ta = meta[a.username]?.lastTs || 0;
      const tb = meta[b.username]?.lastTs || 0;
      if (ta !== tb) return tb - ta;
      return (a.display_name || a.username).localeCompare(b.display_name || b.username);
    });
    let html = '';
    for (const u of active) {
      const name = u.display_name || u.username;
      const m = meta[u.username] || { unread: 0, lastMsg: '', lastTs: 0 };
      const preview = m.lastMsg ? m.lastMsg.slice(0, 50).replace(/\n/g, ' ')
                                  : 'No messages yet';
      const isActive = _teamActive && _teamActive.type === 'person'
                        && _teamActive.id === u.username;
      const bg = isActive ? 'background:#2a3460' : 'background:transparent';
      html += `
        <div class="team-staff-row" data-other="${_esc(u.username)}"
              data-name="${_esc(name)}"
              style="padding:10px 12px;border-radius:8px;cursor:pointer;${bg};margin-bottom:4px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="font-weight:600;font-size:14px">${_esc(name)}</div>
            ${m.unread > 0 ? `<span style="background:#8b5cf6;color:white;font-size:11px;padding:2px 7px;border-radius:10px">${m.unread}</span>` : ''}
          </div>
          <div style="font-size:12px;color:#9aa4c0;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${_esc(preview)}</div>
        </div>
      `;
    }
    list.innerHTML = html;
    list.querySelectorAll('.team-staff-row').forEach(row => {
      row.addEventListener('click', () => {
        _teamOpenPerson(row.dataset.other, row.dataset.name);
      });
    });
  }

  async function _teamOpenPerson(username, displayName) {
    _teamActive = { type: 'person', id: username, name: displayName || username };
    const header = document.getElementById('team-chat-header');
    const compose = document.getElementById('team-chat-compose');
    if (header) header.textContent = displayName || username;
    if (compose) compose.style.display = 'block';
    await _teamLoadThread();
    _teamRefreshSidebar();   // refresh highlights + unread counts
    _teamRestartPolling();
  }

  async function _teamOpenGroup(group) {
    _teamActive = {
      type: 'group',
      id: group.id,
      name: group.name,
      members: group.members || [],
      member_names: group.member_names || [],
      virtual: !!group.virtual,
    };
    const header = document.getElementById('team-chat-header');
    const compose = document.getElementById('team-chat-compose');
    if (header) {
      const count = (group.members || []).length;
      const icon = group.id === '__all__' ? '🌐' : '👥';
      const editBtn = (group.id === '__all__')
        ? ''
        : `<button type="button" id="team-group-edit-btn"
                    style="background:transparent;border:1px solid #2c3756;color:#9aa4c0;font-size:11px;padding:3px 10px;border-radius:6px;cursor:pointer;margin-left:8px">Edit</button>`;
      header.innerHTML = `
        <div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px">
          <div>${icon} ${_esc(group.name)}</div>
          <div style="font-weight:400;font-size:12px;color:#9aa4c0">${count} ${count === 1 ? 'member' : 'members'}</div>
          ${editBtn}
        </div>
        <div style="font-weight:400;font-size:11px;color:#9aa4c0;margin-top:4px">
          ${(group.member_names || []).slice(0, 8).map(_esc).join(' · ')}${(group.member_names || []).length > 8 ? ' …' : ''}
        </div>
      `;
      const eb = document.getElementById('team-group-edit-btn');
      if (eb) eb.addEventListener('click', () => _teamOpenGroupEditor(group));
    }
    if (compose) compose.style.display = 'block';
    await _teamLoadThread();
    _teamRefreshSidebar();
    _teamRestartPolling();
  }

  function _teamRestartPolling() {
    if (_teamPollHandle) clearInterval(_teamPollHandle);
    const activeAtStart = _teamActive ? `${_teamActive.type}:${_teamActive.id}` : null;
    _teamPollHandle = setInterval(() => {
      if (!document.getElementById('tab-team')?.classList.contains('active')) return;
      const nowActive = _teamActive ? `${_teamActive.type}:${_teamActive.id}` : null;
      if (nowActive === activeAtStart) _teamLoadThread();
    }, 5000);
  }

  async function _teamLoadThread() {
    if (!_teamActive) return;
    const thread = document.getElementById('team-chat-thread');
    if (!thread) return;
    try {
      await _teamLoadMe();
      const url = _teamActive.type === 'group'
        ? `/api/owner/internal_messages/group_thread/${encodeURIComponent(_teamActive.id)}`
        : `/api/owner/internal_messages/thread/${encodeURIComponent(_teamActive.id)}`;
      const r = await fetch(url);
      if (!r.ok) throw new Error('Failed to load thread');
      const data = await r.json();
      const msgs = data.thread || [];
      if (!msgs.length) {
        const hint = _teamActive.type === 'group'
          ? `No messages yet. Send the first one — everyone in <b>${_esc(_teamActive.name)}</b> will see it.`
          : 'No messages yet. Send the first one.';
        thread.innerHTML = `<div class="muted" style="text-align:center;font-size:13px;margin:auto">${hint}</div>`;
        return;
      }
      const me = _teamMe?.username || '';
      let html = '';
      let lastFrom = null;
      for (const m of msgs) {
        const mine = m.from === me;
        const via = m.via === 'orby' ? ' <span style="font-size:10px;opacity:0.7">via Orby</span>' : '';
        const dt = m.created_at ? new Date(m.created_at * 1000).toLocaleString() : '';
        const align = mine ? 'flex-end' : 'flex-start';
        const bg = mine ? '#8b5cf6' : '#2a3460';
        const color = mine ? 'white' : '#eaf0ff';
        const showSender = _teamActive.type === 'group' && !mine
                            && m.from !== lastFrom;
        const senderLine = showSender
          ? `<div style="font-size:11px;color:#9aa4c0;margin-bottom:3px;margin-left:4px">${_esc(m.from_name || m.from)}</div>`
          : '';
        html += `
          <div style="display:flex;flex-direction:column;align-items:${mine ? 'flex-end' : 'flex-start'}">
            ${senderLine}
            <div style="max-width:75%;padding:10px 14px;background:${bg};color:${color};border-radius:14px;border-${mine ? 'bottom-right' : 'bottom-left'}-radius:4px">
              <div style="white-space:pre-wrap;font-size:14px">${_esc(m.body)}</div>
              <div style="font-size:10px;opacity:0.7;margin-top:4px">${_esc(dt)}${via}</div>
            </div>
          </div>
        `;
        lastFrom = m.from;
      }
      thread.innerHTML = html;
      thread.scrollTop = thread.scrollHeight;
      // Mark unread as read (silently, after rendering)
      setTimeout(() => {
        const markUrl = _teamActive.type === 'group'
          ? `/api/owner/internal_messages/group_thread/${encodeURIComponent(_teamActive.id)}/mark_read`
          : '/api/owner/internal_messages/mark_all_read';
        fetch(markUrl, { method: 'POST' }).catch(() => {});
      }, 1000);
    } catch (e) {
      thread.innerHTML = '<div style="color:#ff7a7a;font-size:13px">' + _esc(e.message) + '</div>';
    }
  }

  async function _teamSendMessage() {
    const input = document.getElementById('team-chat-input');
    if (!input || !_teamActive) return;
    const body = input.value.trim();
    if (!body) return;
    const btn = document.getElementById('team-chat-send');
    btn.disabled = true;
    try {
      const url = _teamActive.type === 'group'
        ? `/api/owner/internal_messages/group/${encodeURIComponent(_teamActive.id)}`
        : '/api/owner/internal_messages';
      const payload = _teamActive.type === 'group'
        ? { body }
        : { to: _teamActive.id, body };
      const r = await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'Failed');
      input.value = '';
      await _teamLoadThread();
      _teamRefreshSidebar();
    } catch (e) {
      alert('Send failed: ' + e.message);
    } finally {
      btn.disabled = false;
      input.focus();
    }
  }

  // ── Group create / edit modal ───────────────────────────────────────────
  let _teamGroupModalMode = 'create';   // 'create' or 'edit'
  let _teamGroupModalEditingId = null;

  function _teamOpenGroupCreator() {
    _teamGroupModalMode = 'create';
    _teamGroupModalEditingId = null;
    document.getElementById('team-group-modal-title').textContent = 'New group';
    document.getElementById('team-group-name').value = '';
    document.getElementById('team-group-msg').textContent = '';
    _teamRenderGroupMemberChecks([]);
    const modal = document.getElementById('team-group-modal');
    modal.style.display = 'flex';
    setTimeout(() => document.getElementById('team-group-name').focus(), 50);
  }

  function _teamOpenGroupEditor(group) {
    _teamGroupModalMode = 'edit';
    _teamGroupModalEditingId = group.id;
    document.getElementById('team-group-modal-title').textContent = 'Edit group';
    document.getElementById('team-group-name').value = group.name || '';
    document.getElementById('team-group-msg').textContent = '';
    _teamRenderGroupMemberChecks(group.members || []);
    const modal = document.getElementById('team-group-modal');
    modal.style.display = 'flex';
    // Inject a Delete button on edit (rebuilt each open so we don't dup it)
    let saveBtn = document.getElementById('team-group-save');
    let delBtn = document.getElementById('team-group-delete');
    if (!delBtn) {
      delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.id = 'team-group-delete';
      delBtn.textContent = 'Delete';
      delBtn.style.cssText = 'flex:1;padding:10px;background:transparent;border:1px solid #6b2a3a;color:#ff7a7a;border-radius:6px;cursor:pointer;font-weight:600';
      saveBtn.parentNode.insertBefore(delBtn, saveBtn);
      delBtn.addEventListener('click', _teamDeleteGroup);
    }
    delBtn.style.display = '';
  }

  function _teamRenderGroupMemberChecks(preselected) {
    const wrap = document.getElementById('team-group-member-checks');
    const sel = new Set((preselected || []).map(x => x.toLowerCase()));
    const me = _teamMe?.username || '';
    if (!_teamStaffCache.length) {
      wrap.innerHTML = '<div class="muted" style="padding:8px;font-size:12px">No active staff. Add some in the Staff tab first.</div>';
      return;
    }
    let html = '';
    for (const u of _teamStaffCache) {
      const name = u.display_name || u.username;
      const checked = sel.has(u.username) || u.username === me;
      const isSelf = u.username === me;
      html += `
        <label style="display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:6px;cursor:pointer;font-size:14px">
          <input type="checkbox" class="team-group-member-cb" value="${_esc(u.username)}"
                  ${checked ? 'checked' : ''}>
          <span>${_esc(name)}${isSelf ? ' <span style="color:#9aa4c0;font-size:11px">(you)</span>' : ''}</span>
        </label>
      `;
    }
    wrap.innerHTML = html;
  }

  async function _teamSaveGroup() {
    const name = document.getElementById('team-group-name').value.trim();
    const msg = document.getElementById('team-group-msg');
    const members = Array.from(document.querySelectorAll('.team-group-member-cb'))
      .filter(cb => cb.checked).map(cb => cb.value);
    msg.style.color = '#9aa4c0';
    if (!name) { msg.style.color = '#ff7a7a'; msg.textContent = 'Name required.'; return; }
    if (members.length < 2) { msg.style.color = '#ff7a7a'; msg.textContent = 'Pick at least 2 members.'; return; }
    msg.textContent = 'Saving…';
    const btn = document.getElementById('team-group-save');
    btn.disabled = true;
    try {
      const r = _teamGroupModalMode === 'edit'
        ? await fetch(`/api/owner/groups/${encodeURIComponent(_teamGroupModalEditingId)}`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, members })
          })
        : await fetch('/api/owner/groups', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, members })
          });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'Save failed');
      document.getElementById('team-group-modal').style.display = 'none';
      await _teamRefreshSidebar();
      if (data.group) _teamOpenGroup(data.group);
    } catch (e) {
      msg.style.color = '#ff7a7a';
      msg.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function _teamDeleteGroup() {
    if (!_teamGroupModalEditingId) return;
    if (!confirm('Delete this group? Message history stays for audit but the group disappears from your sidebar.')) return;
    try {
      const r = await fetch(`/api/owner/groups/${encodeURIComponent(_teamGroupModalEditingId)}`,
                             { method: 'DELETE' });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        throw new Error(data.error || 'Delete failed');
      }
      document.getElementById('team-group-modal').style.display = 'none';
      _teamActive = null;
      const header = document.getElementById('team-chat-header');
      const compose = document.getElementById('team-chat-compose');
      if (header) header.textContent = 'Pick a teammate or group on the left to start chatting';
      if (compose) compose.style.display = 'none';
      document.getElementById('team-chat-thread').innerHTML =
        '<div class="muted" style="text-align:center;font-size:13px;margin:auto">No conversation selected.</div>';
      await _teamRefreshSidebar();
    } catch (e) {
      alert(e.message);
    }
  }

  function _wireTeamChat() {
    const sendBtn = document.getElementById('team-chat-send');
    const input = document.getElementById('team-chat-input');
    if (sendBtn) sendBtn.addEventListener('click', _teamSendMessage);
    if (input) {
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          _teamSendMessage();
        }
      });
    }
    // New-group modal wiring
    const newBtn = document.getElementById('team-new-group-btn');
    if (newBtn) newBtn.addEventListener('click', _teamOpenGroupCreator);
    const cancelBtn = document.getElementById('team-group-cancel');
    if (cancelBtn) cancelBtn.addEventListener('click', () => {
      document.getElementById('team-group-modal').style.display = 'none';
    });
    const saveBtn = document.getElementById('team-group-save');
    if (saveBtn) saveBtn.addEventListener('click', _teamSaveGroup);
    const modal = document.getElementById('team-group-modal');
    if (modal) modal.addEventListener('click', (e) => {
      if (e.target === modal) modal.style.display = 'none';
    });
    // Tab + interval refresh
    const teamTab = document.querySelector('.tab[data-tab="team"]');
    if (teamTab) teamTab.addEventListener('click', _teamRefreshSidebar);
    _teamRefreshSidebar();
    setInterval(_teamRefreshSidebar, 30000);
  }
  document.addEventListener('DOMContentLoaded', _wireTeamChat);

  // ════════════════════════════════════════════════════════════════════
  //   FORMS TAB — upload labeled form templates, auto-detect fields
  // ════════════════════════════════════════════════════════════════════

  // Display labels for the canonical kinds (must match modules/forms.VALID_KINDS)
  const _FORM_KIND_LABELS = {
    change_order: 'Change Order',
    contract: 'Contract',
    msa: 'Master Service Agreement',
    lien_waiver_partial_cond: 'Lien Waiver (Partial Conditional)',
    lien_waiver_partial_uncond: 'Lien Waiver (Partial Unconditional)',
    lien_waiver_final_cond: 'Lien Waiver (Final Conditional)',
    lien_waiver_final_uncond: 'Lien Waiver (Final Unconditional)',
    w9: 'W-9',
    coi_request: 'COI Request',
    subcontractor_agreement: 'Subcontractor Agreement',
    punch_list: 'Punch List',
    proposal: 'Proposal',
    invoice_custom: 'Custom Invoice',
    custom: 'Custom',
  };

  // The Orby data fields that can be mapped from template fields.
  // Mirror of the synonym table in modules/forms.py.
  const _MAPPED_TO_OPTIONS = [
    ['', '— skip this field —'],
    ['customer_name', 'Customer name'],
    ['customer_phone', 'Customer phone'],
    ['customer_email', 'Customer email'],
    ['address', 'Project address'],
    ['city', 'City'],
    ['state', 'State'],
    ['zip', 'ZIP'],
    ['project_label', 'Project name / label'],
    ['co_number', 'Change-order number'],
    ['contract_amount', 'Original contract amount'],
    ['amount', 'CO amount (this change)'],
    ['new_contract_amount', 'New total after CO'],
    ['description', 'Description / scope'],
    ['today', 'Today\'s date'],
    ['contracted_at', 'Contract date'],
    ['started_at', 'Start date'],
    ['est_complete', 'Estimated completion'],
    ['biz_name', 'Contractor (your business) name'],
    ['biz_license', 'Contractor license #'],
    ['biz_address', 'Contractor address'],
    ['biz_phone', 'Contractor phone'],
    ['biz_email', 'Contractor email'],
    ['signature_pad', 'Customer signature'],
    ['biz_signature_pad', 'Contractor signature'],
  ];

  async function formsLoad() {
    const listEl = document.getElementById('forms-list');
    const emptyEl = document.getElementById('forms-empty-state');
    if (!listEl) return;
    try {
      const r = await fetch('/api/owner/forms');
      if (!r.ok) {
        listEl.innerHTML = '<div class="empty-state-small">Failed to load. ' + r.status + '</div>';
        return;
      }
      const { templates } = await r.json();
      if (!templates || templates.length === 0) {
        listEl.innerHTML = '';
        if (emptyEl) emptyEl.hidden = false;
        return;
      }
      if (emptyEl) emptyEl.hidden = true;
      // Group by kind
      const byKind = {};
      templates.forEach((t) => { (byKind[t.kind] = byKind[t.kind] || []).push(t); });
      const html = [];
      Object.keys(byKind).sort().forEach((kind) => {
        const label = _FORM_KIND_LABELS[kind] || kind;
        html.push(`<div class="forms-group" style="margin-bottom:18px">
          <h3 style="margin:0 0 6px;font-size:14px;color:#555">${label}</h3>`);
        byKind[kind].forEach((t) => {
          const fieldCount = (t.detected_fields || []).length;
          const mappedCount = (t.detected_fields || []).filter((f) => f.mapped_to).length;
          const isDefault = t.is_default;
          const uploaded = new Date((t.uploaded_at || 0) * 1000).toLocaleDateString();
          html.push(`
            <div class="files-row" style="display:flex;align-items:center;gap:10px;padding:10px;border:1px solid #e6e6e6;border-radius:6px;margin-bottom:6px;background:#fafafa">
              <div style="flex:1">
                <div style="font-weight:600">${escapeHtml(t.display_name || t.filename)}
                  ${isDefault ? '<span style="background:#daf3da;color:#28732e;border-radius:3px;font-size:11px;padding:2px 6px;margin-left:6px">DEFAULT</span>' : ''}
                </div>
                <div style="font-size:12px;color:#888;margin-top:2px">
                  ${escapeHtml(t.filename)} · v${t.version} · uploaded ${uploaded} ·
                  ${fieldCount} fields (${mappedCount} mapped)
                </div>
              </div>
              <button class="secondary-btn" onclick="window.formsEditMapping('${t.id}')">Edit fields</button>
              ${isDefault ? '' : `<button class="secondary-btn" onclick="window.formsSetDefault('${t.id}')">Make default</button>`}
              <button class="secondary-btn" style="color:#a00" onclick="window.formsDelete('${t.id}', '${escapeHtml(t.display_name)}')">Delete</button>
            </div>
          `);
        });
        html.push(`</div>`);
      });
      listEl.innerHTML = html.join('');
    } catch (e) {
      listEl.innerHTML = '<div class="empty-state-small">Load failed: ' + (e.message || e) + '</div>';
    }
  }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, (c) =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  async function formsUpload(evt) {
    evt.preventDefault();
    const form = document.getElementById('forms-upload-form');
    const goBtn = document.getElementById('forms-upload-go');
    if (!form || !goBtn) return;
    const fileInput = document.getElementById('forms-upload-file');
    if (!fileInput.files || fileInput.files.length === 0) {
      alert('Pick a PDF or .docx file first');
      return;
    }
    goBtn.disabled = true;
    goBtn.textContent = 'Uploading…';
    const fd = new FormData();
    fd.append('file', fileInput.files[0]);
    fd.append('kind', document.getElementById('forms-upload-kind').value);
    fd.append('display_name', document.getElementById('forms-upload-name').value || '');
    fd.append('is_default', document.getElementById('forms-upload-default').checked ? '1' : '0');
    try {
      const r = await fetch('/api/owner/forms/upload', { method: 'POST', body: fd });
      const j = await r.json();
      if (!r.ok) {
        alert('Upload failed: ' + (j.error || r.status));
        return;
      }
      document.getElementById('forms-upload-dialog').close();
      form.reset();
      await formsLoad();
      // Auto-open the mapping review so owner can fix any unmapped fields
      if (j.template && j.template.id) {
        formsEditMapping(j.template.id);
      }
    } catch (e) {
      alert('Upload failed: ' + (e.message || e));
    } finally {
      goBtn.disabled = false;
      goBtn.textContent = 'Upload & analyze';
    }
  }

  window.formsEditMapping = async function (templateId) {
    try {
      const r = await fetch('/api/owner/forms/' + templateId);
      if (!r.ok) { alert('Failed to load template'); return; }
      const tpl = await r.json();
      const dlg = document.getElementById('forms-mapping-dialog');
      const titleEl = document.getElementById('forms-mapping-title');
      const fieldsEl = document.getElementById('forms-mapping-fields');
      titleEl.textContent = 'Field mapping — ' + (tpl.display_name || tpl.filename);
      const fields = tpl.detected_fields || [];
      if (fields.length === 0) {
        fieldsEl.innerHTML = `<div class="empty-state-small">
          No fillable fields found in this template. If you uploaded a flat
          scanned PDF, Orby can\'t auto-fill it (needs PDF AcroForm fields
          or Word {{placeholders}}). Recreate the form in Word with
          placeholders like <code>{{customer_name}}</code> and re-upload.
        </div>`;
      } else {
        const html = fields.map((f) => {
          const opts = _MAPPED_TO_OPTIONS.map(([v, lbl]) =>
            `<option value="${v}"${f.mapped_to === v ? ' selected' : ''}>${escapeHtml(lbl)}</option>`
          ).join('');
          return `
            <div style="display:flex;gap:10px;align-items:center;margin-bottom:8px;padding:8px;background:#f7f7f9;border-radius:4px">
              <div style="flex:1">
                <div style="font-weight:600">${escapeHtml(f.name)}</div>
                <div style="font-size:11px;color:#888">type: ${f.type || 'text'}</div>
              </div>
              <select style="flex:1;max-width:280px"
                      data-template="${templateId}"
                      data-field="${escapeHtml(f.name)}"
                      onchange="window.formsSaveMapping(this)">
                ${opts}
              </select>
            </div>`;
        }).join('');
        fieldsEl.innerHTML = html;
      }
      dlg.showModal();
    } catch (e) {
      alert('Failed: ' + (e.message || e));
    }
  };

  window.formsSaveMapping = async function (selectEl) {
    const templateId = selectEl.dataset.template;
    const fieldName = selectEl.dataset.field;
    const mappedTo = selectEl.value;
    try {
      await fetch('/api/owner/forms/' + templateId + '/mapping', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ field_name: fieldName, mapped_to: mappedTo }),
      });
      // Visual confirmation
      selectEl.style.borderColor = '#28732e';
      setTimeout(() => { selectEl.style.borderColor = ''; }, 1000);
    } catch (e) {
      alert('Save failed: ' + (e.message || e));
    }
  };

  window.formsSetDefault = async function (templateId) {
    try {
      await fetch('/api/owner/forms/' + templateId + '/default', { method: 'POST' });
      await formsLoad();
    } catch (e) { alert('Failed: ' + e.message); }
  };

  window.formsDelete = async function (templateId, displayName) {
    if (!confirm('Delete template "' + displayName + '"? Filled forms already saved are unaffected.')) return;
    try {
      await fetch('/api/owner/forms/' + templateId, { method: 'DELETE' });
      await formsLoad();
    } catch (e) { alert('Failed: ' + e.message); }
  };

  function _wireFormsTab() {
    const formsTab = document.querySelector('.tab[data-tab="forms"]');
    if (formsTab) formsTab.addEventListener('click', formsLoad);
    const uploadBtn = document.getElementById('forms-upload-btn');
    if (uploadBtn) uploadBtn.addEventListener('click', () => {
      document.getElementById('forms-upload-dialog').showModal();
    });
    const form = document.getElementById('forms-upload-form');
    if (form) form.addEventListener('submit', formsUpload);
  }
  document.addEventListener('DOMContentLoaded', _wireFormsTab);

  // ==================================================================
  // MARKETING module — multi-platform ad copy + image generation
  // ==================================================================
  let _currentCampaign = null;  // { id, title, brief, assets, images }
  const _PLATFORM_LABELS = {
    facebook_post:    'Facebook post',
    instagram_post:   'Instagram caption',
    tiktok_caption:   'TikTok caption',
    linkedin_post:    'LinkedIn post',
    google_search_ad: 'Google Search ad',
    email_newsletter: 'Email newsletter',
    print_flyer:      'Print flyer',
  };

  function _esc(s) {
    return String(s || '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function _copyButton(label, text) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'text-btn marketing-copy-btn';
    btn.textContent = label || 'Copy';
    btn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(text);
        const orig = btn.textContent;
        btn.textContent = '✓ Copied';
        setTimeout(() => { btn.textContent = orig; }, 1400);
      } catch {
        btn.textContent = 'Copy failed';
      }
    });
    return btn;
  }

  function _renderPlatformCard(key, payload) {
    const card = document.createElement('div');
    card.className = 'marketing-card';
    const header = document.createElement('div');
    header.className = 'marketing-card-header';
    const h = document.createElement('h4');
    h.textContent = _PLATFORM_LABELS[key] || key;
    header.appendChild(h);

    // Build the rendered text + plain text for clipboard
    let plain = '';
    const body = document.createElement('div');
    body.className = 'marketing-card-body';

    if (key === 'google_search_ad' && typeof payload === 'object') {
      const lines = [];
      ['headline_1','headline_2','headline_3'].forEach(k => {
        if (payload[k]) lines.push(`Headline: ${payload[k]}`);
      });
      ['description_1','description_2'].forEach(k => {
        if (payload[k]) lines.push(`Description: ${payload[k]}`);
      });
      plain = lines.join('\n');
      body.innerHTML = lines.map(l => `<div>${_esc(l)}</div>`).join('');
    } else if (key === 'email_newsletter' && typeof payload === 'object') {
      const parts = [];
      if (payload.subject)   parts.push(`Subject: ${payload.subject}`);
      if (payload.preheader) parts.push(`Preheader: ${payload.preheader}`);
      if (payload.body)      parts.push('', payload.body);
      plain = parts.join('\n');
      body.innerHTML = parts.map(p => `<div>${_esc(p).replace(/\n/g,'<br>')}</div>`).join('');
    } else {
      plain = String(payload || '');
      body.innerHTML = _esc(plain).replace(/\n/g,'<br>');
    }

    header.appendChild(_copyButton('Copy', plain));
    card.appendChild(header);
    card.appendChild(body);
    return card;
  }

  function _renderCampaignResults(title, assets, images) {
    const root = document.getElementById('marketing-results');
    if (!root) return;
    root.hidden = false;
    root.innerHTML = '';

    const title_h = document.createElement('h3');
    title_h.textContent = title || '(untitled campaign)';
    root.appendChild(title_h);

    // Save / new actions
    const actions = document.createElement('div');
    actions.className = 'marketing-actions';
    const saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'primary-btn';
    saveBtn.textContent = _currentCampaign && _currentCampaign.id
                            ? 'Update saved campaign' : 'Save campaign';
    saveBtn.addEventListener('click', () => _saveCurrentCampaign());
    actions.appendChild(saveBtn);
    root.appendChild(actions);

    // Platform cards
    const grid = document.createElement('div');
    grid.className = 'marketing-grid';
    const order = ['facebook_post','instagram_post','tiktok_caption',
                    'linkedin_post','google_search_ad','email_newsletter',
                    'print_flyer'];
    order.forEach(k => {
      if (assets && assets[k]) {
        grid.appendChild(_renderPlatformCard(k, assets[k]));
      }
    });
    root.appendChild(grid);

    // Images section (if any attached)
    if (images && images.length) {
      const ih = document.createElement('h3');
      ih.textContent = `Images (${images.length})`;
      ih.style.marginTop = '24px';
      root.appendChild(ih);
      const igrid = document.createElement('div');
      igrid.className = 'marketing-image-grid';
      images.forEach(img => {
        const wrap = document.createElement('div');
        wrap.className = 'marketing-image-wrap';
        const i = document.createElement('img');
        i.src = img.url;
        i.alt = img.prompt || 'generated';
        wrap.appendChild(i);
        const cap = document.createElement('div');
        cap.className = 'muted';
        cap.style.fontSize = '12px';
        cap.style.padding = '6px 4px';
        cap.textContent = (img.platform || '') + (img.prompt ? ' — ' + img.prompt.slice(0,120) + (img.prompt.length>120?'…':'') : '');
        wrap.appendChild(cap);
        igrid.appendChild(wrap);
      });
      root.appendChild(igrid);
    }
  }

  async function _saveCurrentCampaign() {
    if (!_currentCampaign) return;
    const status = document.getElementById('marketing-status');
    if (status) status.textContent = 'Saving…';
    try {
      const r = await api('/api/owner/marketing/save', {
        method: 'POST',
        body: JSON.stringify({
          id:     _currentCampaign.id || undefined,
          brief:  _currentCampaign.brief || '',
          title:  _currentCampaign.title || '',
          assets: _currentCampaign.assets || {},
        }),
      });
      _currentCampaign = r.campaign;
      if (status) status.textContent = '✓ Saved';
      setTimeout(() => { if (status) status.textContent = ''; }, 1600);
      await loadMarketingLibrary();
    } catch (e) {
      if (status) status.textContent = 'Save failed';
      console.error('marketing save failed', e);
    }
  }

  async function _generateCampaign() {
    const ta = document.getElementById('marketing-brief');
    const status = document.getElementById('marketing-status');
    const btn = document.getElementById('marketing-generate');
    const brief = (ta && ta.value || '').trim();
    if (brief.length < 8) {
      if (status) status.textContent = 'Tell me a bit more about the campaign first.';
      return;
    }
    if (status) status.textContent = 'Generating… (10-20 seconds)';
    if (btn) btn.disabled = true;
    try {
      const r = await api('/api/owner/marketing/generate', {
        method: 'POST',
        body: JSON.stringify({ brief }),
      });
      _currentCampaign = {
        id:     null,
        title:  r.title || '',
        brief:  r.brief || brief,
        assets: r.assets || {},
        images: [],
      };
      _renderCampaignResults(_currentCampaign.title,
                              _currentCampaign.assets,
                              _currentCampaign.images);
      if (status) status.textContent = '';
    } catch (e) {
      console.error('marketing generate failed', e);
      if (status) status.textContent = 'Generation failed — try again or rephrase.';
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function _generateImage() {
    if (!_currentCampaign) {
      const status = document.getElementById('marketing-status');
      if (status) status.textContent = 'Generate copy first, then I can add an image.';
      return;
    }
    const brief = _currentCampaign.brief || '';
    const status = document.getElementById('marketing-status');
    const btn = document.getElementById('marketing-image');
    if (status) status.textContent = 'Painting image… (10-20 seconds)';
    if (btn) btn.disabled = true;
    try {
      // Save the campaign first if not yet — image attaches to it.
      if (!_currentCampaign.id) {
        await _saveCurrentCampaign();
      }
      const r = await api('/api/owner/marketing/image', {
        method: 'POST',
        body: JSON.stringify({
          brief, platform: 'instagram',
          campaign_id: _currentCampaign.id,
        }),
      });
      if (!_currentCampaign.images) _currentCampaign.images = [];
      _currentCampaign.images.push(r.image);
      _renderCampaignResults(_currentCampaign.title,
                              _currentCampaign.assets,
                              _currentCampaign.images);
      if (status) status.textContent = '';
    } catch (e) {
      console.error('marketing image generation failed', e);
      if (status) status.textContent = 'Image generation failed.';
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function loadMarketingLibrary() {
    const lib = document.getElementById('marketing-library');
    if (!lib) return;
    lib.innerHTML = '<div class="muted" style="font-size:12px">Loading…</div>';
    try {
      const r = await api('/api/owner/marketing/campaigns');
      const items = (r && r.campaigns) || [];
      if (!items.length) {
        lib.innerHTML = '<div class="muted" style="font-size:12px">No saved campaigns yet. Generate one to start a library.</div>';
        return;
      }
      lib.innerHTML = '';
      items.forEach(c => {
        const row = document.createElement('div');
        row.className = 'marketing-library-row';
        row.dataset.id = c.id;
        row.innerHTML = `
          <div class="marketing-library-title">${_esc(c.title || '(untitled)')}</div>
          <div class="muted" style="font-size:11px">${_esc((c.created_at || '').slice(0,10))} · ${c.image_count || 0} image${c.image_count===1?'':'s'}</div>
        `;
        row.addEventListener('click', () => _openCampaign(c.id));
        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'text-btn';
        del.style.float = 'right';
        del.style.fontSize = '11px';
        del.textContent = '✕';
        del.title = 'Delete';
        del.addEventListener('click', async (ev) => {
          ev.stopPropagation();
          if (!confirm(`Delete "${c.title || 'this campaign'}"?`)) return;
          try {
            await api(`/api/owner/marketing/campaigns/${c.id}`, { method: 'DELETE' });
            await loadMarketingLibrary();
          } catch (e) { console.error('delete failed', e); }
        });
        row.appendChild(del);
        lib.appendChild(row);
      });
    } catch (e) {
      console.error('library load failed', e);
      lib.innerHTML = '<div class="muted" style="font-size:12px;color:#a44">Could not load library.</div>';
    }
  }

  async function _openCampaign(id) {
    try {
      const r = await api(`/api/owner/marketing/campaigns/${id}`);
      const c = r.campaign;
      _currentCampaign = {
        id:     c.id,
        title:  c.title,
        brief:  c.brief,
        assets: c.assets || {},
        images: c.images || [],
      };
      const ta = document.getElementById('marketing-brief');
      if (ta) ta.value = c.brief || '';
      _renderCampaignResults(c.title, c.assets, c.images);
    } catch (e) {
      console.error('open failed', e);
    }
  }

  function _newCampaign() {
    _currentCampaign = null;
    const ta = document.getElementById('marketing-brief');
    if (ta) ta.value = '';
    const root = document.getElementById('marketing-results');
    if (root) { root.hidden = true; root.innerHTML = ''; }
    const status = document.getElementById('marketing-status');
    if (status) status.textContent = '';
  }

  function wireMarketing() {
    const gen = document.getElementById('marketing-generate');
    const img = document.getElementById('marketing-image');
    const fresh = document.getElementById('marketing-new');
    if (gen)   gen.addEventListener('click', _generateCampaign);
    if (img)   img.addEventListener('click', _generateImage);
    if (fresh) fresh.addEventListener('click', _newCampaign);
  }

  // ==================================================================
  // LEGAL MODULE
  // ==================================================================

  const _lesc = s => String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

  function _legalSwitchSec(name) {
    document.querySelectorAll('.legal-snav').forEach(b => b.classList.toggle('active', b.dataset.sec === name));
    document.querySelectorAll('.legal-sec').forEach(s => s.hidden = s.id !== 'lsec-' + name);
  }

  let _legalMattersCache = [];

  function _legalPopulateMattersDatalist(ids) {
    ids.forEach(id => {
      const dl = document.getElementById(id);
      if (!dl) return;
      dl.innerHTML = _legalMattersCache.map(m =>
        `<option value="${_lesc(m.matter_number)} ${_lesc(m.title)}">${_lesc(m.matter_number)} — ${_lesc(m.title)}</option>`
      ).join('');
    });
  }

  function _legalMatterByQuery(q) {
    if (!q) return null;
    q = q.toLowerCase();
    return _legalMattersCache.find(m =>
      m.id === q ||
      (m.matter_number || '').toLowerCase() === q ||
      (m.matter_number + ' ' + m.title).toLowerCase().includes(q) ||
      m.title.toLowerCase().includes(q) ||
      (m.client_name || '').toLowerCase().includes(q)
    ) || null;
  }

  async function loadLegal() {
    try {
      const data = await api('/api/owner/legal/matters');
      _legalMattersCache = data.matters || [];
      _legalRenderMatters(_legalMattersCache);
      _legalPopulateMattersDatalist(['dl-matters-list','draft-matters-list','time-matters-list']);
      // populate time filter select
      const sel = document.getElementById('time-filter-matter');
      if (sel) {
        sel.innerHTML = '<option value="">All matters</option>' +
          _legalMattersCache.map(m => `<option value="${_lesc(m.id)}">${_lesc(m.matter_number)} — ${_lesc(m.title)}</option>`).join('');
      }
    } catch(e) {
      const el = document.getElementById('legal-matters-list');
      if (el) el.innerHTML = '<p class="muted">Could not load matters.</p>';
    }
  }

  function _legalRenderMatters(matters) {
    const el = document.getElementById('legal-matters-list');
    if (!el) return;
    if (!matters.length) { el.innerHTML = '<p class="muted">No matters yet. Click "+ Open Matter" to start one.</p>'; return; }
    const statusColor = { active:'#4cbb70', closed:'#8fa3c7', archived:'#666' };
    el.innerHTML = matters.map(m => `
      <div class="legal-matter-card" data-id="${_lesc(m.id)}">
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">
            <strong style="font-size:14px">${_lesc(m.title)}</strong>
            <span style="font-size:11px;color:${statusColor[m.status]||'#6ea2ff'};text-transform:uppercase;font-weight:600">${_lesc(m.status||'active')}</span>
          </div>
          <div style="font-size:12px;color:#8fa3c7;display:flex;gap:16px;flex-wrap:wrap">
            <span>${_lesc(m.matter_number)}</span>
            <span>${_lesc(m.client_name||'')}</span>
            <span>${_lesc((m.practice_area||m.matter_type||'').replace(/_/g,' '))}</span>
            ${m.rate ? `<span>$${Number(m.rate).toFixed(2)}/hr</span>` : ''}
          </div>
        </div>
        <button class="text-btn legal-close-matter-btn" data-id="${_lesc(m.id)}" style="font-size:12px;color:#8fa3c7" title="Close matter">Close</button>
      </div>
    `).join('');
    el.querySelectorAll('.legal-close-matter-btn').forEach(btn => {
      btn.addEventListener('click', async e => {
        e.stopPropagation();
        if (!confirm('Close this matter?')) return;
        try {
          await api(`/api/owner/legal/matters/${btn.dataset.id}/close`, { method:'POST' });
          loadLegal();
        } catch { alert('Could not close matter.'); }
      });
    });

    // Matter card click → open detail overlay
    el.addEventListener('click', e => {
      const card = e.target.closest('.legal-matter-card');
      if (!card || e.target.closest('.legal-close-matter-btn')) return;
      _openMatterDetail(card.dataset.id);
    });
  }

  let _mdMatterId = null;

  async function _openMatterDetail(matterId) {
    _mdMatterId = matterId;
    const overlay = document.getElementById('legal-matter-detail');
    if (!overlay) return;
    overlay.hidden = false;
    document.getElementById('md-title').textContent = 'Loading…';
    document.getElementById('md-info').innerHTML = '';
    ['deadlines','time','docs'].forEach(s => {
      const el = document.getElementById('md-sec-' + s);
      if (el) el.innerHTML = '<p class="muted" style="font-size:13px">Loading…</p>';
    });
    _mdSwitchTab('deadlines');

    const [matterData, dlData, timeData, draftData] = await Promise.allSettled([
      api(`/api/owner/legal/matters/${matterId}`),
      api(`/api/owner/legal/deadlines?matter_id=${matterId}&days=365`),
      api(`/api/owner/legal/time?matter_id=${matterId}`),
      api(`/api/owner/legal/drafts?matter_id=${matterId}`),
    ]);

    const m = matterData.status === 'fulfilled' ? matterData.value : {};
    document.getElementById('md-title').textContent = m.title || 'Matter Detail';
    document.getElementById('md-meta').textContent =
      [m.matter_number, (m.status||'').toUpperCase(), m.practice_area ? m.practice_area.replace(/_/g,' ') : ''].filter(Boolean).join(' · ');

    const infoEl = document.getElementById('md-info');
    const infoRows = [
      ['Client', m.client_name], ['Phone', m.client_phone], ['Email', m.client_email],
      ['Opposing Party', m.opposing_party], ['Opposing Counsel', m.opposing_counsel],
      ['Court', m.court], ['Case #', m.case_number], ['Judge', m.judge],
      ['Jurisdiction', m.jurisdiction], ['Rate', m.rate ? `$${Number(m.rate).toFixed(2)}/hr` : null],
      ['Retainer', m.retainer ? `$${Number(m.retainer).toFixed(2)}` : null],
    ].filter(([,v]) => v);
    infoEl.innerHTML = infoRows.map(([k,v]) =>
      `<div><span style="color:#6a7a9a;font-size:11px;text-transform:uppercase;letter-spacing:.5px">${_lesc(k)}</span><br><span>${_lesc(v)}</span></div>`
    ).join('');

    // Deadlines tab
    const dls = dlData.status === 'fulfilled' ? (dlData.value.deadlines || []) : [];
    const today = new Date(); today.setHours(0,0,0,0);
    document.getElementById('md-sec-deadlines').innerHTML = dls.length ? dls.map(d => {
      const due = new Date(d.due_date + 'T00:00:00');
      const diff = Math.ceil((due - today) / 86400000);
      const urgency = diff < 0 ? 'color:#dc3545' : diff <= 7 ? 'color:#ffc107' : 'color:#4cbb70';
      const label = diff < 0 ? `${Math.abs(diff)}d overdue` : diff === 0 ? 'Today' : `${diff}d`;
      return `<div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #1a2040">
        <div>
          <div style="font-size:13px;font-weight:600">${_lesc(d.title)}</div>
          <div style="font-size:11px;color:#6a7a9a">${_lesc(d.due_date)} · ${_lesc((d.type||'').replace(/_/g,' '))}</div>
        </div>
        <span style="font-size:12px;font-weight:600;${urgency}">${label}</span>
      </div>`;
    }).join('') : '<p class="muted" style="font-size:13px">No deadlines yet — click "+ Deadline" to add one.</p>';

    // Time tab
    const entries = timeData.status === 'fulfilled' ? (timeData.value.entries || []) : [];
    const total = entries.reduce((s,e) => s + (e.hours||0), 0);
    const unbilled = entries.filter(e => !e.billed).reduce((s,e) => s + (e.amount||0), 0);
    document.getElementById('md-sec-time').innerHTML = entries.length ?
      `<div style="display:flex;gap:24px;margin-bottom:14px;font-size:13px">
        <span>Total: <strong>${total.toFixed(2)}h</strong></span>
        <span>Unbilled: <strong>$${unbilled.toFixed(2)}</strong></span>
      </div>` +
      entries.map(e => `<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1a2040;font-size:12px">
        <div>
          <div style="color:#d0d8f0">${_lesc(e.description||'')}</div>
          <div style="color:#6a7a9a">${_lesc(e.date||'')} · ${e.hours}h · $${Number(e.rate||0).toFixed(2)}/hr</div>
        </div>
        <span style="color:${e.billed ? '#4cbb70' : '#ffc107'};font-size:11px;font-weight:600">${e.billed ? 'Billed' : 'Unbilled'}</span>
      </div>`).join('')
      : '<p class="muted" style="font-size:13px">No time logged yet — say "log X hours to [matter]" in chat.</p>';

    // Docs tab
    const drafts = draftData.status === 'fulfilled' ? (draftData.value.drafts || []) : [];
    document.getElementById('md-sec-docs').innerHTML = drafts.length ? drafts.map(d =>
      `<div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #1a2040">
        <div>
          <div style="font-size:13px;font-weight:600">${_lesc(d.template_name||'Document')}</div>
          <div style="font-size:11px;color:#6a7a9a">${new Date(d.updated_at*1000).toLocaleDateString()}</div>
        </div>
        <span class="legal-status-${_lesc(d.status||'draft')}" style="font-size:11px">${_lesc(d.status||'draft')}</span>
      </div>`
    ).join('') : '<p class="muted" style="font-size:13px">No documents yet — click "+ Document" to draft one.</p>';

    // Close matter button state
    document.getElementById('md-close-matter-btn').dataset.matterId = matterId;
  }

  function _mdSwitchTab(name) {
    document.querySelectorAll('.md-tab').forEach(b => {
      const active = b.dataset.sec === name;
      b.style.borderBottomColor = active ? '#7b5cf0' : 'transparent';
      b.style.color = active ? '#c0c8e8' : '#6a7a9a';
      b.style.fontWeight = active ? '600' : '400';
    });
    ['deadlines','time','docs'].forEach(s => {
      const el = document.getElementById('md-sec-' + s);
      if (el) el.hidden = s !== name;
    });
  }

  // Wire matter detail overlay controls (run once)
  (function() {
    document.getElementById('md-close')?.addEventListener('click', () => {
      document.getElementById('legal-matter-detail').hidden = true;
      _mdMatterId = null;
    });
    document.querySelectorAll('.md-tab').forEach(btn => {
      btn.addEventListener('click', () => _mdSwitchTab(btn.dataset.sec));
    });
    document.getElementById('md-add-dl-btn')?.addEventListener('click', () => {
      document.getElementById('md-add-dl-form').hidden = false;
      document.getElementById('md-dl-due').valueAsDate = new Date();
    });
    document.getElementById('md-dl-cancel')?.addEventListener('click', () => {
      document.getElementById('md-add-dl-form').hidden = true;
    });
    document.getElementById('md-dl-save')?.addEventListener('click', async () => {
      if (!_mdMatterId) return;
      const title = document.getElementById('md-dl-title').value.trim();
      const due   = document.getElementById('md-dl-due').value;
      if (!title || !due) { document.getElementById('md-dl-status').textContent = 'Title and date required.'; return; }
      const matter = (_legalMattersCache || []).find(m => m.id === _mdMatterId) || {};
      try {
        await api('/api/owner/legal/deadlines', { method:'POST', body: JSON.stringify({
          matter_id: _mdMatterId, matter_title: matter.title || '', title,
          due_date: due, type: document.getElementById('md-dl-type').value || 'other',
        })});
        document.getElementById('md-add-dl-form').hidden = true;
        document.getElementById('md-dl-title').value = '';
        _openMatterDetail(_mdMatterId);
      } catch { document.getElementById('md-dl-status').textContent = 'Could not save.'; }
    });
    document.getElementById('md-log-time-btn')?.addEventListener('click', () => {
      document.getElementById('legal-matter-detail').hidden = true;
      _legalSwitchSec('time');
      const matter = (_legalMattersCache || []).find(m => m.id === _mdMatterId);
      const matterInput = document.getElementById('time-matter');
      if (matterInput && matter) matterInput.value = matter.title || '';
    });
    document.getElementById('md-draft-btn')?.addEventListener('click', () => {
      document.getElementById('legal-matter-detail').hidden = true;
      _legalSwitchSec('drafts');
      document.getElementById('legal-draft-form').hidden = false;
      document.getElementById('legal-new-draft-btn').hidden = true;
      const matter = (_legalMattersCache || []).find(m => m.id === _mdMatterId);
      const matterInput = document.getElementById('draft-matter');
      if (matterInput && matter) matterInput.value = matter.title || '';
    });
    document.getElementById('md-close-matter-btn')?.addEventListener('click', async () => {
      const id = document.getElementById('md-close-matter-btn').dataset.matterId;
      if (!id || !confirm('Close this matter?')) return;
      try {
        await api(`/api/owner/legal/matters/${id}/close`, { method:'POST' });
        document.getElementById('legal-matter-detail').hidden = true;
        loadLegal();
      } catch { alert('Could not close matter.'); }
    });
    // Click outside overlay to close
    document.getElementById('legal-matter-detail')?.addEventListener('click', e => {
      if (e.target === document.getElementById('legal-matter-detail')) {
        document.getElementById('legal-matter-detail').hidden = true;
        _mdMatterId = null;
      }
    });
  })();

  function _legalRenderDeadlines(deadlines) {
    const el = document.getElementById('legal-deadlines-list');
    if (!el) return;
    if (!deadlines.length) { el.innerHTML = '<p class="muted">No upcoming deadlines.</p>'; return; }
    const today = new Date(); today.setHours(0,0,0,0);
    el.innerHTML = deadlines.map(d => {
      const due = new Date(d.due_date + 'T00:00:00');
      const diff = Math.ceil((due - today) / 86400000);
      const cls = diff < 0 ? 'legal-dl-overdue' : diff <= 7 ? 'legal-dl-soon' : '';
      const label = diff < 0 ? `<span style="color:#dc3545;font-weight:700">${Math.abs(diff)}d overdue</span>`
                  : diff === 0 ? `<span style="color:#ffc107;font-weight:700">Today</span>`
                  : `<span style="color:#8fa3c7">${diff}d</span>`;
      return `
        <div class="legal-dl-card ${cls}" data-id="${_lesc(d.id)}">
          <div style="flex:1;min-width:0">
            <div style="font-size:14px;font-weight:600;margin-bottom:2px">${_lesc(d.title)}</div>
            <div style="font-size:12px;color:#8fa3c7">${_lesc(d.matter_title||'')} &nbsp;·&nbsp; Due ${_lesc(d.due_date)}</div>
          </div>
          ${label}
          ${d.status !== 'done' ? `<button class="primary-btn legal-dl-done" data-id="${_lesc(d.id)}" style="font-size:12px;padding:5px 12px">Done</button>` : '<span style="color:#4cbb70;font-size:12px">✓ Done</span>'}
        </div>`;
    }).join('');
    el.querySelectorAll('.legal-dl-done').forEach(btn => {
      btn.addEventListener('click', async () => {
        try {
          await api(`/api/owner/legal/deadlines/${btn.dataset.id}/complete`, { method:'POST' });
          _loadDeadlines();
        } catch { alert('Could not mark deadline done.'); }
      });
    });
  }

  async function _loadDeadlines() {
    const days = document.getElementById('legal-dl-filter')?.value || '30';
    try {
      const data = await api(`/api/owner/legal/deadlines${days !== '0' ? `?upcoming_days=${days}` : ''}`);
      _legalRenderDeadlines(data.deadlines || []);
    } catch {
      const el = document.getElementById('legal-deadlines-list');
      if (el) el.innerHTML = '<p class="muted">Could not load deadlines.</p>';
    }
  }

  function _legalStatusBadge(status) {
    return `<span class="legal-status-${_lesc(status)}">${_lesc((status||'').toUpperCase())}</span>`;
  }

  function _legalRenderDrafts(drafts) {
    const el = document.getElementById('legal-drafts-list');
    if (!el) return;
    if (!drafts.length) { el.innerHTML = '<p class="muted">No documents yet. Click "+ Draft Document" to generate one for attorney review.</p>'; return; }
    el.innerHTML = drafts.map(d => `
      <div class="legal-draft-card">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:10px">
          <div style="flex:1;min-width:0">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
              <strong style="font-size:14px">${_lesc(d.title||d.template_key||'Document')}</strong>
              ${_legalStatusBadge(d.status)}
            </div>
            <div style="font-size:12px;color:#8fa3c7">${_lesc(d.matter_title||'')} &nbsp;·&nbsp; ${_lesc(d.created_at ? d.created_at.slice(0,10) : '')}</div>
          </div>
          <div style="display:flex;gap:8px;flex-shrink:0">
            <button class="text-btn legal-view-draft" data-id="${_lesc(d.id)}" style="font-size:12px">View</button>
            ${d.status !== 'approved' ? `<button class="primary-btn legal-approve-draft" data-id="${_lesc(d.id)}" style="font-size:12px;padding:5px 12px">Approve</button>` : ''}
            <button class="secondary-btn legal-revise-draft" data-id="${_lesc(d.id)}" style="font-size:12px;padding:5px 12px">Edit</button>
          </div>
        </div>
      </div>
    `).join('');
    el.querySelectorAll('.legal-view-draft').forEach(btn => btn.addEventListener('click', () => _openDocViewer(btn.dataset.id, false)));
    el.querySelectorAll('.legal-approve-draft').forEach(btn => btn.addEventListener('click', async () => {
      try {
        await api(`/api/owner/legal/drafts/${btn.dataset.id}/approve`, { method:'POST' });
        _loadDrafts();
      } catch { alert('Could not approve document.'); }
    }));
    el.querySelectorAll('.legal-revise-draft').forEach(btn => btn.addEventListener('click', () => _openDocViewer(btn.dataset.id, true)));
  }

  async function _loadDrafts() {
    const filter = document.getElementById('draft-filter')?.value || '';
    try {
      const data = await api('/api/owner/legal/drafts' + (filter ? `?status=${filter}` : ''));
      _legalRenderDrafts(data.drafts || []);
    } catch {
      const el = document.getElementById('legal-drafts-list');
      if (el) el.innerHTML = '<p class="muted">Could not load documents.</p>';
    }
  }

  let _currentDocId = null;

  async function _openDocViewer(draftId, startInRevise) {
    _currentDocId = draftId;
    const viewer = document.getElementById('legal-doc-viewer');
    viewer.hidden = false;
    document.getElementById('doc-viewer-content').textContent = 'Loading…';
    document.getElementById('doc-viewer-actions').innerHTML = '';
    document.getElementById('doc-revise-form').hidden = true;
    try {
      const data = await api(`/api/owner/legal/drafts/${draftId}`);
      const d = data.draft;
      document.getElementById('doc-viewer-title').textContent = d.title || d.template_key || 'Document';
      const statusEl = document.getElementById('doc-viewer-status');
      statusEl.className = 'legal-status-' + (d.status||'draft');
      statusEl.textContent = (d.status||'draft').toUpperCase();
      document.getElementById('doc-viewer-content').textContent = d.content || '(empty)';
      // Build action buttons
      const actions = document.getElementById('doc-viewer-actions');
      actions.innerHTML = '';
      if (d.status !== 'approved') {
        const appBtn = document.createElement('button');
        appBtn.className = 'primary-btn'; appBtn.style.fontSize = '13px';
        appBtn.textContent = '✓ Approve';
        appBtn.addEventListener('click', async () => {
          try {
            await api(`/api/owner/legal/drafts/${draftId}/approve`, { method:'POST' });
            viewer.hidden = true; _loadDrafts();
          } catch { alert('Could not approve.'); }
        });
        actions.appendChild(appBtn);
      }
      const revBtn = document.createElement('button');
      revBtn.className = 'secondary-btn'; revBtn.style.fontSize = '13px';
      revBtn.textContent = '✏ Edit / Revise';
      revBtn.addEventListener('click', () => {
        const rf = document.getElementById('doc-revise-form');
        rf.hidden = false;
        document.getElementById('doc-revise-text').value = d.content || '';
        document.getElementById('doc-revise-notes').value = '';
      });
      actions.appendChild(revBtn);
      const copyBtn = document.createElement('button');
      copyBtn.className = 'text-btn'; copyBtn.style.fontSize = '12px';
      copyBtn.textContent = 'Copy text';
      copyBtn.addEventListener('click', () => navigator.clipboard.writeText(d.content || ''));
      actions.appendChild(copyBtn);
      if (startInRevise) revBtn.click();
    } catch {
      document.getElementById('doc-viewer-content').textContent = 'Could not load document.';
    }
  }

  function _legalRenderTimeEntries(entries) {
    const el = document.getElementById('legal-time-list');
    if (!el) return;
    if (!entries.length) { el.innerHTML = '<p class="muted">No time entries yet.</p>'; return; }
    const totalH = entries.reduce((s,e) => s + (e.hours||0), 0);
    const totalAmt = entries.reduce((s,e) => s + ((e.hours||0)*(e.rate||0)), 0);
    el.innerHTML = `
      <div style="display:flex;justify-content:space-between;padding:8px 14px;background:#0d1626;border-radius:8px;margin-bottom:8px;font-size:13px;font-weight:600">
        <span>${entries.length} entries &nbsp;·&nbsp; ${totalH.toFixed(2)}h total</span>
        <span>${totalAmt > 0 ? '$'+totalAmt.toFixed(2) : ''}</span>
      </div>
      ${entries.map(e => `
        <div class="legal-time-row">
          <div style="flex:1;min-width:0">
            <div style="font-size:13px;font-weight:500">${_lesc(e.description||'')}</div>
            <div style="font-size:11px;color:#8fa3c7">${_lesc(e.matter_title||'')} &nbsp;·&nbsp; ${_lesc(e.date||'')}</div>
          </div>
          <span style="font-size:13px;font-weight:600;white-space:nowrap">${Number(e.hours||0).toFixed(2)}h</span>
          ${e.rate ? `<span style="font-size:12px;color:#8fa3c7;white-space:nowrap">$${(Number(e.hours||0)*Number(e.rate||0)).toFixed(2)}</span>` : ''}
          ${e.billed ? '<span style="font-size:11px;color:#4cbb70">Billed</span>' : '<span style="font-size:11px;color:#8fa3c7">Unbilled</span>'}
        </div>`).join('')}`;
  }

  async function _loadTimeEntries() {
    const matterId = document.getElementById('time-filter-matter')?.value || '';
    const unbilled = document.getElementById('time-filter-unbilled')?.checked;
    let url = '/api/owner/legal/time';
    const params = [];
    if (matterId) params.push(`matter_id=${encodeURIComponent(matterId)}`);
    if (unbilled) params.push('billed=false');
    if (params.length) url += '?' + params.join('&');
    try {
      const data = await api(url);
      _legalRenderTimeEntries(data.entries || []);
    } catch {
      const el = document.getElementById('legal-time-list');
      if (el) el.innerHTML = '<p class="muted">Could not load time entries.</p>';
    }
  }

  function wireLegal() {
    // Sub-nav section switching
    document.querySelectorAll('.legal-snav').forEach(btn => {
      btn.addEventListener('click', () => {
        const sec = btn.dataset.sec;
        _legalSwitchSec(sec);
        if (sec === 'deadlines') _loadDeadlines();
        if (sec === 'drafts')    _loadDrafts();
        if (sec === 'time')      _loadTimeEntries();
      });
    });

    // === MATTERS ===
    const newMatterBtn = document.getElementById('legal-new-matter-btn');
    const newMatterForm = document.getElementById('legal-new-matter-form');
    if (newMatterBtn) newMatterBtn.addEventListener('click', () => { newMatterForm.hidden = false; newMatterBtn.hidden = true; });
    const nmCancel = document.getElementById('nm-cancel');
    if (nmCancel) nmCancel.addEventListener('click', () => { newMatterForm.hidden = true; newMatterBtn.hidden = false; });
    const nmSubmit = document.getElementById('nm-submit');
    if (nmSubmit) nmSubmit.addEventListener('click', async () => {
      const title = document.getElementById('nm-title').value.trim();
      const client = document.getElementById('nm-client').value.trim();
      if (!title || !client) { document.getElementById('nm-status').textContent = 'Title and client name are required.'; return; }
      const status = document.getElementById('nm-status');
      status.textContent = 'Opening…';
      nmSubmit.disabled = true;
      try {
        await api('/api/owner/legal/matters', {
          method: 'POST',
          body: JSON.stringify({
            title,
            client_name: client,
            matter_type: document.getElementById('nm-type').value || 'other',
            rate: parseFloat(document.getElementById('nm-rate').value) || null,
            retainer: parseFloat(document.getElementById('nm-retainer').value) || null,
            client_email: document.getElementById('nm-email').value || '',
            client_phone: document.getElementById('nm-phone').value || '',
            opposing_party: document.getElementById('nm-opposing').value || '',
            opposing_counsel: document.getElementById('nm-opp-counsel').value || '',
            opposing_firm: document.getElementById('nm-opp-firm').value || '',
            court: document.getElementById('nm-court').value || '',
            case_number: document.getElementById('nm-case-number').value || '',
            judge: document.getElementById('nm-judge').value || '',
            jurisdiction: document.getElementById('nm-jurisdiction').value || '',
            notes: document.getElementById('nm-notes').value || '',
          })
        });
        newMatterForm.hidden = true; newMatterBtn.hidden = false;
        ['nm-title','nm-client','nm-email','nm-phone','nm-notes',
         'nm-opposing','nm-opp-counsel','nm-opp-firm','nm-court',
         'nm-case-number','nm-judge','nm-jurisdiction','nm-retainer']
          .forEach(id => { const el = document.getElementById(id); if(el) el.value=''; });
        document.getElementById('nm-rate').value = '';
        document.getElementById('nm-type').value = '';
        status.textContent = '';
        loadLegal();
      } catch(e) {
        status.textContent = 'Error opening matter.';
      }
      nmSubmit.disabled = false;
    });

    // === DEADLINES ===
    const dlFilter = document.getElementById('legal-dl-filter');
    if (dlFilter) dlFilter.addEventListener('change', _loadDeadlines);
    const addDlBtn = document.getElementById('legal-add-dl-btn');
    const addDlForm = document.getElementById('legal-add-dl-form');
    if (addDlBtn) addDlBtn.addEventListener('click', () => { addDlForm.hidden = false; addDlBtn.hidden = true; });
    const dlCancel = document.getElementById('dl-cancel');
    if (dlCancel) dlCancel.addEventListener('click', () => { addDlForm.hidden = true; addDlBtn.hidden = false; });
    const dlSubmit = document.getElementById('dl-submit');
    if (dlSubmit) dlSubmit.addEventListener('click', async () => {
      const title = document.getElementById('dl-title').value.trim();
      const matterQ = document.getElementById('dl-matter').value.trim();
      const due = document.getElementById('dl-due').value;
      if (!title || !due) { document.getElementById('dl-status').textContent = 'Title and due date are required.'; return; }
      const matter = _legalMatterByQuery(matterQ);
      document.getElementById('dl-status').textContent = 'Saving…';
      dlSubmit.disabled = true;
      try {
        await api('/api/owner/legal/deadlines', {
          method: 'POST',
          body: JSON.stringify({
            title, due_date: due,
            deadline_type: document.getElementById('dl-type').value || 'other',
            matter_id: matter?.id || null,
            matter_title: matter?.title || matterQ || '',
          })
        });
        addDlForm.hidden = true; addDlBtn.hidden = false;
        ['dl-title','dl-matter','dl-due'].forEach(id => { const el = document.getElementById(id); if(el) el.value=''; });
        document.getElementById('dl-status').textContent = '';
        _loadDeadlines();
      } catch { document.getElementById('dl-status').textContent = 'Error adding deadline.'; }
      dlSubmit.disabled = false;
    });

    // === DRAFTS / DOCUMENTS ===
    const newDraftBtn = document.getElementById('legal-new-draft-btn');
    const draftForm = document.getElementById('legal-draft-form');
    if (newDraftBtn) newDraftBtn.addEventListener('click', () => { draftForm.hidden = false; newDraftBtn.hidden = true; });
    const draftCancel = document.getElementById('draft-cancel');
    if (draftCancel) draftCancel.addEventListener('click', () => { draftForm.hidden = true; newDraftBtn.hidden = false; });
    const draftFilter = document.getElementById('draft-filter');
    if (draftFilter) draftFilter.addEventListener('change', _loadDrafts);
    const draftSubmit = document.getElementById('draft-submit');
    if (draftSubmit) draftSubmit.addEventListener('click', async () => {
      const template = document.getElementById('draft-template').value;
      const matterQ = document.getElementById('draft-matter').value.trim();
      if (!template) { document.getElementById('draft-status').textContent = 'Choose a template.'; return; }
      const matter = _legalMatterByQuery(matterQ);
      const status = document.getElementById('draft-status');
      status.textContent = 'Generating draft — this may take 20–40 seconds…';
      draftSubmit.disabled = true;
      try {
        await api('/api/owner/legal/draft', {
          method: 'POST',
          body: JSON.stringify({
            template_key: template,
            matter_id: matter?.id || null,
            matter_title: matter?.title || matterQ || '',
            field_values: {
              special_instructions: document.getElementById('draft-instructions').value || '',
              client_name: matter?.client_name || '',
            }
          })
        });
        draftForm.hidden = true; newDraftBtn.hidden = false;
        document.getElementById('draft-template').value = '';
        document.getElementById('draft-matter').value = '';
        document.getElementById('draft-instructions').value = '';
        status.textContent = '';
        _loadDrafts();
      } catch { status.textContent = 'Error generating draft.'; }
      draftSubmit.disabled = false;
    });

    // Document viewer overlay
    const docViewer = document.getElementById('legal-doc-viewer');
    const docViewerClose = document.getElementById('doc-viewer-close');
    if (docViewerClose) docViewerClose.addEventListener('click', () => { docViewer.hidden = true; });
    docViewer?.addEventListener('click', e => { if (e.target === docViewer) docViewer.hidden = true; });
    const docReviseSave = document.getElementById('doc-revise-save');
    if (docReviseSave) docReviseSave.addEventListener('click', async () => {
      const content = document.getElementById('doc-revise-text').value;
      const notes = document.getElementById('doc-revise-notes').value;
      const st = document.getElementById('doc-revise-status');
      st.textContent = 'Saving…';
      try {
        await api(`/api/owner/legal/drafts/${_currentDocId}/revise`, {
          method: 'POST',
          body: JSON.stringify({ content, notes })
        });
        docViewer.hidden = true;
        _loadDrafts();
      } catch { st.textContent = 'Error saving revision.'; }
    });
    const docReviseCancel = document.getElementById('doc-revise-cancel');
    if (docReviseCancel) docReviseCancel.addEventListener('click', () => { document.getElementById('doc-revise-form').hidden = true; });

    // === TIME ===
    const timeToday = document.getElementById('time-date');
    if (timeToday) timeToday.valueAsDate = new Date();
    const timeSubmit = document.getElementById('time-submit');
    if (timeSubmit) timeSubmit.addEventListener('click', async () => {
      const matterQ = document.getElementById('time-matter').value.trim();
      const hours = parseFloat(document.getElementById('time-hours').value);
      const desc = document.getElementById('time-desc').value.trim();
      if (!matterQ || !hours || !desc) { document.getElementById('time-status').textContent = 'Matter, hours, and description are required.'; return; }
      const matter = _legalMatterByQuery(matterQ);
      if (!matter) { document.getElementById('time-status').textContent = `Matter "${matterQ}" not found.`; return; }
      document.getElementById('time-status').textContent = 'Logging…';
      timeSubmit.disabled = true;
      try {
        await api('/api/owner/legal/time', {
          method: 'POST',
          body: JSON.stringify({
            matter_id: matter.id,
            matter_title: matter.title,
            hours,
            description: desc,
            rate: document.getElementById('time-rate').value || matter.rate || null,
            date: document.getElementById('time-date').value || null,
          })
        });
        document.getElementById('time-status').textContent = `Logged ${hours}h to ${matter.title}.`;
        ['time-hours','time-desc','time-rate','time-matter'].forEach(id => { const el=document.getElementById(id); if(el) el.value=''; });
        document.getElementById('time-date').valueAsDate = new Date();
        _loadTimeEntries();
      } catch { document.getElementById('time-status').textContent = 'Error logging time.'; }
      timeSubmit.disabled = false;
    });
    const timeMatterFilter = document.getElementById('time-filter-matter');
    const timeUnbilledFilter = document.getElementById('time-filter-unbilled');
    if (timeMatterFilter) timeMatterFilter.addEventListener('change', _loadTimeEntries);
    if (timeUnbilledFilter) timeUnbilledFilter.addEventListener('change', _loadTimeEntries);

    // === RESEARCH ===
    const researchBtn = document.getElementById('legal-research-btn');
    if (researchBtn) researchBtn.addEventListener('click', async () => {
      const q = document.getElementById('legal-research-q').value.trim();
      if (!q) { document.getElementById('legal-research-status').textContent = 'Enter a research question.'; return; }
      const status = document.getElementById('legal-research-status');
      const result = document.getElementById('legal-research-result');
      status.textContent = 'Researching — this may take 30–60 seconds…';
      researchBtn.disabled = true; result.hidden = true;
      try {
        const data = await api('/api/owner/legal/research', {
          method: 'POST',
          body: JSON.stringify({
            question: q,
            practice_area: document.getElementById('legal-research-area').value || '',
            jurisdiction: document.getElementById('legal-research-jurisdiction').value || '',
          })
        });
        document.getElementById('legal-research-text').textContent = data.memo || '';
        result.hidden = false;
        status.textContent = '';
      } catch { status.textContent = 'Research failed — try again.'; }
      researchBtn.disabled = false;
    });
    const researchCopy = document.getElementById('legal-research-copy');
    if (researchCopy) researchCopy.addEventListener('click', () => {
      const text = document.getElementById('legal-research-text').textContent;
      navigator.clipboard.writeText(text);
      researchCopy.textContent = 'Copied!';
      setTimeout(() => { researchCopy.textContent = 'Copy'; }, 2000);
    });

    // === CONFLICT CHECK ===
    const conflictBtn = document.getElementById('conflict-check-btn');
    if (conflictBtn) conflictBtn.addEventListener('click', async () => {
      const name = document.getElementById('conflict-name').value.trim();
      if (!name) { document.getElementById('conflict-status').textContent = 'Enter a name to check.'; return; }
      const status = document.getElementById('conflict-status');
      const result = document.getElementById('conflict-result');
      status.textContent = 'Checking…'; conflictBtn.disabled = true; result.hidden = true;
      try {
        const data = await api('/api/owner/legal/conflict_check', {
          method: 'POST',
          body: JSON.stringify({ name })
        });
        const hits = data.conflicts || [];
        if (hits.length === 0) {
          result.style.borderColor = '#4cbb70';
          result.innerHTML = `<span style="color:#4cbb70;font-weight:600">✓ No conflicts found</span> — "${_lesc(name)}" is clear to open a new matter.`;
        } else {
          result.style.borderColor = '#dc3545';
          result.innerHTML = `<span style="color:#dc3545;font-weight:700">⚠ CONFLICT — ${hits.length} match(es)</span><br><br>` +
            hits.map(h => `<div style="margin-bottom:8px;padding:10px;background:#1a0d0d;border-radius:8px">
              <strong>${_lesc(h.matter_number||'')} ${_lesc(h.title)}</strong><br>
              <span style="font-size:12px;color:#8fa3c7">${_lesc(h.conflict_type)} &nbsp;·&nbsp; ${_lesc(h.status)}</span>
            </div>`).join('');
        }
        result.hidden = false; status.textContent = '';
      } catch { status.textContent = 'Check failed.'; }
      conflictBtn.disabled = false;
    });
    const conflictInput = document.getElementById('conflict-name');
    if (conflictInput) conflictInput.addEventListener('keydown', e => { if (e.key === 'Enter') conflictBtn?.click(); });

    // === BILLING ===
    async function _loadBillingMatters() {
      const sel = document.getElementById('billing-matter-select');
      if (!sel) return;
      try {
        const data = await api('/api/owner/legal/matters');
        const active = (data.matters || []).filter(m => !['closed','settled'].includes(m.status));
        sel.innerHTML = '<option value="">Select matter…</option>' +
          active.map(m => `<option value="${m.id}">${_lesc(m.matter_number||'')} ${_lesc(m.title)}</option>`).join('');
      } catch {}
    }
    _loadBillingMatters();

    const billingBtn = document.getElementById('billing-generate-btn');
    if (billingBtn) billingBtn.addEventListener('click', async () => {
      const matterId = document.getElementById('billing-matter-select').value;
      if (!matterId) { document.getElementById('billing-status').textContent = 'Select a matter first.'; return; }
      const status = document.getElementById('billing-status');
      status.textContent = 'Generating…'; billingBtn.disabled = true;
      try {
        const data = await api(`/api/owner/legal/billing/invoice/${matterId}`);
        document.getElementById('billing-invoice-title').textContent = data.matter_title || 'Invoice';
        document.getElementById('billing-invoice-text').textContent = data.invoice_text || '';
        document.getElementById('billing-invoice-panel').hidden = false;
        status.textContent = data.entry_count ? `${data.entry_count} entries, $${data.total.toFixed(2)} total` : '';
        document.getElementById('billing-mark-billed-btn').dataset.matterId = matterId;
      } catch (e) {
        status.textContent = 'Could not generate invoice.';
      }
      billingBtn.disabled = false;
    });

    const billingCopy = document.getElementById('billing-copy-btn');
    if (billingCopy) billingCopy.addEventListener('click', () => {
      navigator.clipboard.writeText(document.getElementById('billing-invoice-text').textContent);
      billingCopy.textContent = 'Copied!';
      setTimeout(() => { billingCopy.textContent = 'Copy'; }, 2000);
    });

    const markBilledBtn = document.getElementById('billing-mark-billed-btn');
    if (markBilledBtn) markBilledBtn.addEventListener('click', async () => {
      const matterId = markBilledBtn.dataset.matterId;
      if (!matterId) return;
      if (!confirm('Mark all displayed time entries as billed? This cannot be undone.')) return;
      try {
        await api(`/api/owner/legal/time/bill/${matterId}`, { method: 'POST' });
        document.getElementById('billing-status').textContent = 'All entries marked billed.';
        document.getElementById('billing-invoice-panel').hidden = true;
      } catch { document.getElementById('billing-status').textContent = 'Error marking billed.'; }
    });

    // === CONTRACT REVIEW ===
    const contractBtn = document.getElementById('contract-analyze-btn');
    if (contractBtn) contractBtn.addEventListener('click', async () => {
      const text = document.getElementById('contract-text').value.trim();
      if (!text) { document.getElementById('contract-status').textContent = 'Paste contract text first.'; return; }
      const status = document.getElementById('contract-status');
      const result = document.getElementById('contract-result');
      status.textContent = 'Analyzing — this takes 60–90 seconds…';
      contractBtn.disabled = true; result.hidden = true;
      try {
        const data = await api('/api/owner/legal/contract_review', {
          method: 'POST',
          body: JSON.stringify({
            contract_text: text,
            client_side: document.getElementById('contract-client-side').value || '',
            jurisdiction: document.getElementById('contract-jurisdiction').value || '',
          })
        });
        document.getElementById('contract-result-text').textContent = data.memo || '';
        result.hidden = false;
        status.textContent = '';
      } catch { status.textContent = 'Analysis failed — try again.'; }
      contractBtn.disabled = false;
    });

    const contractCopy = document.getElementById('contract-copy-btn');
    if (contractCopy) contractCopy.addEventListener('click', () => {
      navigator.clipboard.writeText(document.getElementById('contract-result-text').textContent);
      contractCopy.textContent = 'Copied!';
      setTimeout(() => { contractCopy.textContent = 'Copy'; }, 2000);
    });
  }

  // ==================================================================
  // CONTRACTOR / CONSTRUCTION MODULE
  // ==================================================================

  function showToast(msg) {
    const stack = document.getElementById('toast-stack') || document.body;
    const el = document.createElement('div');
    el.style.cssText = 'background:#222;color:#fff;padding:10px 16px;border-radius:8px;margin-top:8px;font-size:14px;opacity:0;transition:opacity .3s';
    el.textContent = msg;
    stack.appendChild(el);
    requestAnimationFrame(() => { el.style.opacity = '1'; });
    setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 400); }, 3000);
  }

  async function loadContractor() {
    await _gcLoadSummary();
    await _gcLoadProjects();
  }

  async function _gcLoadSummary() {
    const el = document.getElementById('gc-summary');
    if (!el) return;
    try {
      const d = await api('/api/owner/gc/summary');
      el.innerHTML = `
        <div class="gc-stat-grid">
          <div class="gc-stat"><span class="gc-stat-num">${d.active_jobs}</span><span class="gc-stat-label">Active Jobs</span></div>
          <div class="gc-stat"><span class="gc-stat-num">$${_fmt$(d.active_revenue)}</span><span class="gc-stat-label">Job Revenue</span></div>
          <div class="gc-stat"><span class="gc-stat-num">${d.unpaid_invoices}</span><span class="gc-stat-label">Unpaid Invoices</span></div>
          <div class="gc-stat"><span class="gc-stat-num">$${_fmt$(d.unpaid_amount)}</span><span class="gc-stat-label">Outstanding</span></div>
          <div class="gc-stat"><span class="gc-stat-num">${d.pending_co_approval}</span><span class="gc-stat-label">COs Pending</span></div>
          <div class="gc-stat"><span class="gc-stat-num">${d.cos_awaiting_sig}</span><span class="gc-stat-label">Awaiting Sig</span></div>
        </div>`;
    } catch { el.innerHTML = '<p class="dim">Could not load summary.</p>'; }
  }

  function _fmt$(n) {
    const num = parseFloat(n) || 0;
    return num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  async function _gcLoadProjects(statusFilter) {
    const el = document.getElementById('gc-projects-list');
    if (!el) return;
    el.innerHTML = '<p class="dim">Loading…</p>';
    try {
      const qs = statusFilter ? `?status=${statusFilter}` : '';
      const d = await api('/api/owner/gc/projects' + qs);
      if (!d.projects.length) { el.innerHTML = '<p class="dim">No projects found.</p>'; return; }
      el.innerHTML = d.projects.map(p => {
        const badge = { estimate: 'badge-gray', active: 'badge-green', on_hold: 'badge-yellow',
                        completed: 'badge-blue', cancelled: 'badge-red' }[p.status] || 'badge-gray';
        const co = parseFloat(p.co_signed_total || 0);
        const contract = parseFloat(p.contract_amount || 0);
        const total = contract + co;
        const collected = parseFloat(p.amount_collected || 0);
        const pct = total > 0 ? Math.round(collected / total * 100) : 0;
        return `<div class="gc-project-card" data-pid="${p.id}">
          <div class="gc-proj-header">
            <span class="gc-proj-name">${_esc(p.label || p.address || 'Untitled')}</span>
            <span class="badge ${badge}">${p.status}</span>
          </div>
          <div class="gc-proj-addr dim">${_esc(p.address || '')}</div>
          <div class="gc-proj-money">
            <span>Contract: <strong>$${_fmt$(contract)}</strong></span>
            ${co ? `<span>+COs: <strong>$${_fmt$(co)}</strong></span>` : ''}
            <span>Collected: <strong>$${_fmt$(collected)}</strong></span>
          </div>
          <div class="gc-prog-bar-wrap"><div class="gc-prog-bar" style="width:${pct}%"></div></div>
          <div class="gc-proj-actions">
            <button class="btn-sm" onclick="gcOpenProject('${p.id}')">Open</button>
          </div>
        </div>`;
      }).join('');
    } catch { el.innerHTML = '<p class="dim">Failed to load projects.</p>'; }
  }

  async function _gcLoadInvoices(projectId) {
    const el = document.getElementById('gc-invoices-list');
    if (!el) return;
    el.innerHTML = '<p class="dim">Loading…</p>';
    try {
      const qs = projectId ? `?project_id=${projectId}` : '?unpaid=1';
      const d = await api('/api/owner/gc/invoices' + qs);
      if (!d.invoices.length) { el.innerHTML = '<p class="dim">No invoices.</p>'; return; }
      el.innerHTML = `<table class="gc-table"><thead><tr>
        <th>#</th><th>Project</th><th>Amount Due</th><th>Status</th><th></th>
      </tr></thead><tbody>${d.invoices.map(inv => {
        const statusClass = { paid: 'badge-green', overdue: 'badge-red', sent: 'badge-blue',
          draft: 'badge-gray', partial: 'badge-yellow', void: 'badge-red' }[inv.status] || 'badge-gray';
        return `<tr>
          <td>${_esc(inv.invoice_number || inv.id.slice(0,8))}</td>
          <td>${_esc(inv.project_id || '')}</td>
          <td>$${_fmt$(inv.amount_due)}</td>
          <td><span class="badge ${statusClass}">${inv.status}</span></td>
          <td>${inv.status !== 'paid' && inv.status !== 'void'
            ? `<button class="btn-sm" onclick="gcMarkInvoicePaid('${inv.id}',${inv.amount_due})">Mark Paid</button>`
            : ''}</td>
        </tr>`;
      }).join('')}</tbody></table>`;
    } catch { el.innerHTML = '<p class="dim">Failed to load invoices.</p>'; }
  }

  async function _gcLoadChangeOrders(projectId) {
    const el = document.getElementById('gc-cos-list');
    if (!el) return;
    el.innerHTML = '<p class="dim">Loading…</p>';
    try {
      const qs = projectId ? `?project_id=${projectId}` : '';
      const d = await api('/api/owner/gc/change_orders' + qs);
      if (!d.change_orders.length) { el.innerHTML = '<p class="dim">No change orders.</p>'; return; }
      el.innerHTML = `<table class="gc-table"><thead><tr>
        <th>CO#</th><th>Description</th><th>Amount</th><th>Status</th><th></th>
      </tr></thead><tbody>${d.change_orders.map(co => {
        const statusClass = { draft: 'badge-gray', awaiting_approval: 'badge-yellow',
          approved: 'badge-blue', sent_for_signature: 'badge-blue',
          signed: 'badge-green', declined: 'badge-red' }[co.status] || 'badge-gray';
        return `<tr>
          <td>CO-${co.co_number || co.id.slice(0,6)}</td>
          <td>${_esc(co.description || '')}</td>
          <td>$${_fmt$(co.amount)}</td>
          <td><span class="badge ${statusClass}">${co.status.replace(/_/g,' ')}</span></td>
          <td>${co.status === 'awaiting_approval'
            ? `<button class="btn-sm" onclick="gcApproveCO('${co.id}')">Approve</button>`
            : ''}</td>
        </tr>`;
      }).join('')}</tbody></table>`;
    } catch { el.innerHTML = '<p class="dim">Failed to load change orders.</p>'; }
  }

  async function _gcLoadDailyLogs(projectId) {
    const el = document.getElementById('gc-logs-list');
    if (!el) return;
    el.innerHTML = '<p class="dim">Loading…</p>';
    try {
      const qs = projectId ? `?project_id=${projectId}` : '';
      const d = await api('/api/owner/gc/daily_logs' + qs);
      if (!d.logs.length) { el.innerHTML = '<p class="dim">No logs this week.</p>'; return; }
      el.innerHTML = d.logs.map(log => `
        <div class="gc-log-card">
          <div class="gc-log-header"><strong>${_esc(log.date || '')}</strong> — ${_esc(log.project_id || '')}</div>
          <div>${_esc(log.work_done || '')}</div>
          ${log.crew && log.crew.length ? `<div class="dim">Crew: ${log.crew.map(_esc).join(', ')}</div>` : ''}
          ${log.weather ? `<div class="dim">Weather: ${_esc(log.weather)}</div>` : ''}
          ${log.notes ? `<div class="dim">${_esc(log.notes)}</div>` : ''}
        </div>`).join('');
    } catch { el.innerHTML = '<p class="dim">Failed to load logs.</p>'; }
  }

  async function _gcLoadSubs() {
    const el = document.getElementById('gc-subs-list');
    if (!el) return;
    el.innerHTML = '<p class="dim">Loading…</p>';
    try {
      const d = await api('/api/owner/gc/subcontractors');
      if (!d.subcontractors.length) { el.innerHTML = '<p class="dim">No subcontractors on file.</p>'; return; }
      el.innerHTML = `<table class="gc-table"><thead><tr>
        <th>Name</th><th>Trade</th><th>Phone</th><th>Ins. Expires</th><th>Rating</th>
      </tr></thead><tbody>${d.subcontractors.map(s => `<tr>
        <td>${_esc(s.name)}</td>
        <td>${_esc(s.trade || '—')}</td>
        <td>${_esc(s.phone || '—')}</td>
        <td>${_esc(s.insurance_expires || '—')}</td>
        <td>${'★'.repeat(Math.min(s.rating || 0, 5))}</td>
      </tr>`).join('')}</tbody></table>`;
    } catch { el.innerHTML = '<p class="dim">Failed to load subs.</p>'; }
  }

  // Global helpers called from onclick attributes
  window.gcOpenProject = async function(pid) {
    const panel = document.getElementById('gc-project-detail');
    if (!panel) return;
    panel.innerHTML = '<p class="dim">Loading project…</p>';
    panel.hidden = false;
    try {
      const d = await api(`/api/owner/gc/projects/${pid}`);
      const p = d.project;
      const co = parseFloat(p.co_signed_total || 0);
      const contract = parseFloat(p.contract_amount || 0);
      panel.innerHTML = `
        <button class="btn-sm" onclick="document.getElementById('gc-project-detail').hidden=true">← Back</button>
        <h3>${_esc(p.label || p.address)}</h3>
        <p>${_esc(p.address || '')}</p>
        <p>Contract: <strong>$${_fmt$(contract)}</strong>${co ? ` + COs: <strong>$${_fmt$(co)}</strong>` : ''}</p>
        <p>Status: <strong>${p.status}</strong></p>
        ${p.notes ? `<p class="dim">${_esc(p.notes)}</p>` : ''}
        <h4>Invoices</h4>
        <div id="gc-detail-invoices">${(p.invoices||[]).length ? p.invoices.map(inv =>
          `<div class="gc-row-item"><span>${_esc(inv.invoice_number||inv.id.slice(0,8))}</span>
           <span>$${_fmt$(inv.amount_due)}</span>
           <span class="badge ${ {paid:'badge-green',overdue:'badge-red',draft:'badge-gray',sent:'badge-blue'}[inv.status]||'badge-gray' }">${inv.status}</span>
           ${inv.status!=='paid'&&inv.status!=='void' ? `<button class="btn-sm" onclick="gcMarkInvoicePaid('${inv.id}',${inv.amount_due})">Mark Paid</button>` : ''}
           </div>`).join('') : '<p class="dim">None yet.</p>'}</div>
        <h4>Change Orders</h4>
        <div>${(p.change_orders||[]).length ? p.change_orders.map(co =>
          `<div class="gc-row-item"><span>CO-${co.co_number||co.id.slice(0,6)}</span>
           <span>${_esc(co.description||'')}</span>
           <span>$${_fmt$(co.amount)}</span>
           <span>${co.status.replace(/_/g,' ')}</span>
           ${co.status==='awaiting_approval' ? `<button class="btn-sm" onclick="gcApproveCO('${co.id}')">Approve</button>` : ''}
           </div>`).join('') : '<p class="dim">None yet.</p>'}</div>
        <h4>Recent Daily Logs</h4>
        <div>${(p.daily_logs||[]).length ? p.daily_logs.slice(0,5).map(log =>
          `<div class="gc-log-card"><strong>${_esc(log.date||'')}</strong> — ${_esc(log.work_done||log.notes||'')}</div>`
        ).join('') : '<p class="dim">No logs yet.</p>'}</div>`;
    } catch { panel.innerHTML = '<p class="dim">Failed to load project.</p>'; }
  };

  window.gcMarkInvoicePaid = async function(invId, amount) {
    if (!confirm(`Mark invoice paid for $${_fmt$(amount)}?`)) return;
    try {
      await api(`/api/owner/gc/invoices/${invId}/mark_paid`, { method: 'POST', body: JSON.stringify({ amount }) });
      showToast('Invoice marked paid.');
      contractorLoaded = false;
      loadContractor();
    } catch { showToast('Failed to mark paid.'); }
  };

  window.gcApproveCO = async function(coId) {
    if (!confirm('Approve this change order?')) return;
    try {
      await api(`/api/owner/gc/change_orders/${coId}/approve`, { method: 'POST', body: JSON.stringify({}) });
      showToast('Change order approved.');
      contractorLoaded = false;
      loadContractor();
    } catch { showToast('Failed to approve CO.'); }
  };

  function wireContractor() {
    // Sub-tab switching inside the Contractor panel
    document.querySelectorAll('[data-gc-tab]').forEach(btn => {
      btn.addEventListener('click', () => {
        const tab = btn.dataset.gcTab;
        document.querySelectorAll('[data-gc-tab]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        document.querySelectorAll('[data-gc-panel]').forEach(p => p.hidden = true);
        const panel = document.querySelector(`[data-gc-panel="${tab}"]`);
        if (panel) panel.hidden = false;
        // Lazy-load each sub-tab
        if (tab === 'projects') _gcLoadProjects();
        if (tab === 'invoices') _gcLoadInvoices();
        if (tab === 'change_orders') _gcLoadChangeOrders();
        if (tab === 'daily_logs') _gcLoadDailyLogs();
        if (tab === 'subcontractors') _gcLoadSubs();
      });
    });

    // Status filter for jobs
    const statusFilter = document.getElementById('gc-status-filter');
    if (statusFilter) statusFilter.addEventListener('change', () => {
      _gcLoadProjects(statusFilter.value || undefined);
    });

    // New Project form
    const npForm = document.getElementById('gc-new-project-form');
    if (npForm) npForm.addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(npForm);
      const body = { label: fd.get('label'), address: fd.get('address'),
        contract_amount: parseFloat(fd.get('contract_amount')) || 0,
        status: fd.get('status') || 'estimate', notes: fd.get('notes') || '' };
      try {
        await api('/api/owner/gc/projects', { method: 'POST', body: JSON.stringify(body) });
        npForm.reset();
        showToast('Project created.');
        _gcLoadProjects();
        _gcLoadSummary();
      } catch { showToast('Failed to create project.'); }
    });

    // New Invoice form
    const niForm = document.getElementById('gc-new-invoice-form');
    if (niForm) niForm.addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(niForm);
      const subtotal = parseFloat(fd.get('subtotal')) || 0;
      const body = {
        project_id: fd.get('project_id'),
        line_items: [{ label: fd.get('description') || 'Services', amount: subtotal }],
        subtotal,
        due_days: parseInt(fd.get('due_days')) || 30,
        memo: fd.get('memo') || '',
        status: 'draft',
      };
      try {
        await api('/api/owner/gc/invoices', { method: 'POST', body: JSON.stringify(body) });
        niForm.reset();
        showToast('Invoice created (draft).');
        _gcLoadInvoices();
        _gcLoadSummary();
      } catch { showToast('Failed to create invoice.'); }
    });

    // New Change Order form
    const coForm = document.getElementById('gc-new-co-form');
    if (coForm) coForm.addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(coForm);
      const body = {
        project_id: fd.get('project_id'),
        description: fd.get('description'),
        amount: parseFloat(fd.get('amount')) || 0,
        reason: fd.get('reason') || '',
        status: 'awaiting_approval',
      };
      try {
        await api('/api/owner/gc/change_orders', { method: 'POST', body: JSON.stringify(body) });
        coForm.reset();
        showToast('Change order submitted.');
        _gcLoadChangeOrders();
        _gcLoadSummary();
      } catch { showToast('Failed to create change order.'); }
    });

    // New Daily Log form
    const dlForm = document.getElementById('gc-new-log-form');
    if (dlForm) dlForm.addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(dlForm);
      const crewRaw = fd.get('crew') || '';
      const body = {
        project_id: fd.get('project_id'),
        date_iso: fd.get('date') || new Date().toISOString().slice(0,10),
        work_done: fd.get('work_done'),
        crew: crewRaw.split(',').map(s => s.trim()).filter(Boolean),
        hours: parseFloat(fd.get('hours')) || 0,
        weather: fd.get('weather') || '',
        notes: fd.get('notes') || '',
      };
      try {
        await api('/api/owner/gc/daily_logs', { method: 'POST', body: JSON.stringify(body) });
        dlForm.reset();
        showToast('Log saved.');
        _gcLoadDailyLogs();
      } catch { showToast('Failed to save log.'); }
    });

    // New Sub form
    const subForm = document.getElementById('gc-new-sub-form');
    if (subForm) subForm.addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(subForm);
      const body = {
        name: fd.get('name'), trade: fd.get('trade'),
        phone: fd.get('phone'), email: fd.get('email'),
        license: fd.get('license'), insurance_expires: fd.get('insurance_expires'),
        notes: fd.get('notes') || '',
      };
      try {
        await api('/api/owner/gc/subcontractors', { method: 'POST', body: JSON.stringify(body) });
        subForm.reset();
        showToast('Subcontractor added.');
        _gcLoadSubs();
      } catch { showToast('Failed to add subcontractor.'); }
    });
  }

})();
