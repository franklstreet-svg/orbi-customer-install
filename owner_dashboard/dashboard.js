/* Orbi owner dashboard — client logic */

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
    document.getElementById('tone-select').value = s.tone || 'friendly_professional';
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

    state.hidden = false;
    state.className = 'owner-chat-state-bar thinking';
    state.textContent = 'Thinking...';
    const thinking = addOwnerThinking();

    try {
      const res = await fetch('/api/owner/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          history: ownerChatHistory.slice(-30)
        })
      });
      if (res.status === 401) { window.location.href = '/owner/login'; return; }
      const data = await res.json();
      thinking.remove();
      const reply = data.reply || "I couldn't reach any AI tier just now.";
      addOwnerBubble('assistant', reply, { tier: data.tier });
      ownerChatHistory.push({ role: 'assistant', content: reply });
      if (window.__orbiSpeakReply) window.__orbiSpeakReply(reply);
    } catch (e) {
      thinking.remove();
      const fallback = "I'm offline right now. Try again in a moment.";
      addOwnerBubble('assistant', fallback, { tier: 'none' });
      if (window.__orbiSpeakReply) window.__orbiSpeakReply(fallback);
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

  async function loadContacts(query = '') {
    const list = document.getElementById('contacts-list');
    try {
      const { contacts } = await api(`/api/owner/pa/contacts${query ? '?q=' + encodeURIComponent(query) : ''}`);
      if (!contacts.length) {
        list.innerHTML = `<div class="empty-state-small">${query ? 'No matches' : 'No contacts yet'}</div>`;
        return;
      }
      list.innerHTML = contacts.map(c => `
        <div class="contact-card" data-id="${esc(c.id)}">
          <div class="contact-main">
            <div class="contact-name">${esc(c.name)}${c.company ? ` <span class="muted">— ${esc(c.company)}</span>` : ''}</div>
            <div class="contact-meta">
              ${c.phone ? `<span>${esc(c.phone)}</span>` : ''}
              ${c.email ? `<span>${esc(c.email)}</span>` : ''}
              ${c.source && c.source !== 'manual' ? `<span class="tag">${esc(c.source)}</span>` : ''}
            </div>
            ${c.notes ? `<div class="contact-notes">${esc(c.notes)}</div>` : ''}
          </div>
          <button class="icon-btn-sm" data-action="contact-delete" title="Remove">×</button>
        </div>
      `).join('');
      list.querySelectorAll('[data-action="contact-delete"]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          const id = e.target.closest('[data-id]').dataset.id;
          if (!confirm('Remove this contact?')) return;
          await api(`/api/owner/pa/contacts/${id}`, { method: 'DELETE' });
          loadContacts(document.getElementById('contacts-search').value);
        });
      });
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
        if (!confirm(`Deactivate ${u}? Their data goes to archive for 90 days. You can transfer their contacts/notes/calendar/tasks to yourself before it purges.`)) return;
        try {
          await api(`/api/owner/users/${u}/deactivate`, { method: 'POST' });
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
          <button class="secondary-btn" data-action="transfer">Transfer</button>
          <button class="secondary-btn" data-action="hold-${a.hold ? 'off' : 'on'}">${a.hold ? 'Release Hold' : 'Hold'}</button>
        </div>
      </div>
    `).join('');
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

    wireMyDayForms();
    wireContacts();
    wirePeople();

    document.querySelectorAll('.tab').forEach(t => {
      t.addEventListener('click', () => {
        const which = t.dataset.tab;
        if (which === 'myday' && !myDayLoaded)     { loadMyDay();   myDayLoaded = true; }
        if (which === 'contacts' && !contactsLoaded) { loadContacts(); contactsLoaded = true; }
        if (which === 'people' && !peopleLoaded)   { loadPeople();  peopleLoaded = true; }
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
  let recognition = null;
  let isListening = false;
  let isSpeaking = false;
  let wantsListening = false;
  let restartTimer = null;
  let currentAudio = null;
  let currentAudioUrl = null;
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

    btn.addEventListener('click', () => setVoiceMode(!voiceOn));
    stopBtn.addEventListener('click', stopSpeaking);

    recognition = new Recognition();
    recognition.lang = 'en-US';
    recognition.continuous = true;
    recognition.interimResults = false;

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
      for (let i = event.resultIndex; i < event.results.length; i++) {
        if (event.results[i].isFinal) finalText += event.results[i][0].transcript;
      }
      if (finalText.trim()) {
        // Drop it into the input and fire send like the user typed it
        const input = document.getElementById('owner-chat-input');
        if (input) {
          input.value = finalText.trim();
          input.dispatchEvent(new Event('input', { bubbles: true }));
          document.getElementById('owner-chat-send')?.click();
        }
      }
    };
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
      stopSpeaking();
      try { recognition && recognition.stop(); } catch {}
      setVoiceState(null);
    }
  }

  function safeStartMic() {
    if (!recognition || isListening || isSpeaking) return;
    try { recognition.start(); } catch {}
  }

  // Uses server-side /tts endpoint (edge_tts, en-US-AvaNeural by default
  // — same voice as orbi_test on twickell.com). Falls back to browser
  // synthesis only if the server endpoint fails.
  async function speakReply(text) {
    if (!voiceOn || !text) return;
    stopSpeaking();
    isSpeaking = true;
    // Mute the mic while Orbi talks so we don't echo-loop her own voice
    try { recognition && recognition.stop(); } catch {}
    document.getElementById('owner-stop-speaking').hidden = false;
    setVoiceState('Orbi is speaking...', 'speaking');

    const cleanText = stripForSpeech(text);

    const finish = () => {
      isSpeaking = false;
      if (currentAudioUrl) {
        URL.revokeObjectURL(currentAudioUrl);
        currentAudioUrl = null;
      }
      currentAudio = null;
      document.getElementById('owner-stop-speaking').hidden = true;
      setVoiceState(null);
      if (wantsListening) safeStartMic();
    };

    try {
      // Use GET so the browser does PROGRESSIVE playback — audio starts within
      // ~300ms as the first MP3 frames arrive, instead of waiting for the whole
      // file (which was the 2-3 sec gap Frank saw).
      const url = '/tts?text=' + encodeURIComponent(cleanText);
      currentAudio = new Audio(url);
      currentAudio.preload = 'auto';
      currentAudio.onended = finish;
      currentAudio.onerror = finish;
      await currentAudio.play();
    } catch (err) {
      console.warn('[Orbi] server TTS failed, falling back to browser voice:', err);
      // Last-resort fallback so something speaks
      if (window.speechSynthesis) {
        const u = new SpeechSynthesisUtterance(cleanText);
        u.onend = finish;
        u.onerror = finish;
        window.speechSynthesis.speak(u);
      } else {
        finish();
      }
    }
  }

  function stopSpeaking() {
    if (currentAudio) {
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


  // ------------------------------------------------------------------
  // Utils
  // ------------------------------------------------------------------
  function esc(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
})();
