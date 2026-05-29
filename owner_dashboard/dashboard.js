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
      addOwnerBubble('assistant', reply, {
        tier: data.tier,
        source: data.source,
        download_url: data.download_url,
      });
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
  // Separate from voiceOn — owner can have Orby READ HER REPLIES aloud
  // without turning on mic listening. Persisted in localStorage so it
  // stays on across sessions.
  let speakRepliesOn = (function() {
    try { return localStorage.getItem('orbi_speak_replies') === '1'; }
    catch { return false; }
  })();
  // iOS PWAs need a "user gesture" context to play audio. Once we play
  // ANY audio inside a click handler, the persistent Audio element is
  // "unlocked" for subsequent .play() calls without further gestures.
  let _persistentAudio = null;
  function _unlockAudio() {
    if (_persistentAudio) return;
    _persistentAudio = new Audio();
    _persistentAudio.preload = 'auto';
    _persistentAudio.playsInline = true;
    _persistentAudio.setAttribute('playsinline', '');
    // 1-frame silent WAV — triggers the iOS gesture-grant without actually
    // playing anything audible. Subsequent src updates + play() work.
    _persistentAudio.src =
      'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
    _persistentAudio.play()
      .then(() => { /* unlocked */ })
      .catch(() => { /* silent fail — fall through to speechSynthesis */ });
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
  });
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
  //
  // Speaks when EITHER:
  //   - voiceOn is true (full voice mode — mic listening + reply spoken)
  //   - speakRepliesOn is true (just speaker — owner types but wants
  //                              Orby to read replies aloud)
  async function speakReply(text) {
    if ((!voiceOn && !speakRepliesOn) || !text) return;
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
      // Echo guard — wait 600ms before re-arming the mic so the audio
      // element's buffer fully drains and the tail of Ava's last word
      // doesn't get picked up by the microphone and treated as user input.
      if (wantsListening) {
        setTimeout(() => {
          if (wantsListening && !isSpeaking) safeStartMic();
        }, 600);
      }
    };

    // iOS PWA strategy: reuse the persistent Audio element that was
    // unlocked by the speaker-toggle click. iOS treats new Audio() as
    // a fresh autoplay attempt and blocks it; reusing the unlocked
    // element preserves the gesture grant.
    const url = '/tts?text=' + encodeURIComponent(cleanText);
    const useExistingUnlocked = !!_persistentAudio;
    try {
      if (useExistingUnlocked) {
        // Reuse — keeps iOS happy. .load() is REQUIRED after changing
        // src on an already-played Audio element, otherwise the browser
        // ignores the new source and stays on the silent unlock WAV.
        _persistentAudio.onended = finish;
        _persistentAudio.onerror = finish;
        _persistentAudio.src = url;
        _persistentAudio.load();
        currentAudio = _persistentAudio;
      } else {
        // Fresh Audio (desktop / non-iOS where this works fine)
        currentAudio = new Audio(url);
        currentAudio.preload = 'auto';
        currentAudio.playsInline = true;
        currentAudio.setAttribute('playsinline', '');
        currentAudio.onended = finish;
        currentAudio.onerror = finish;
      }
      const playPromise = currentAudio.play();
      if (playPromise && typeof playPromise.catch === 'function') {
        playPromise.catch((err) => {
          // iOS PWA blocked it OR /tts failed. Try speechSynthesis as the
          // safety net — iOS's built-in voices work in PWAs.
          console.warn('[Orbi] Audio() blocked or failed:', err);
          if (window.speechSynthesis && window.SpeechSynthesisUtterance) {
            try {
              const u = new SpeechSynthesisUtterance(cleanText);
              u.rate = 1.05;
              u.pitch = 1.0;
              // Prefer a female voice if available (matches Ava on the server)
              const voices = window.speechSynthesis.getVoices();
              const female = voices.find(v =>
                /female|samantha|karen|moira|tessa|ava/i.test(v.name) &&
                /^en/i.test(v.lang));
              if (female) u.voice = female;
              u.onend = finish;
              u.onerror = finish;
              window.speechSynthesis.cancel();
              window.speechSynthesis.speak(u);
              return;
            } catch (e) { /* fall through to error message */ }
          }
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
              <li>Set up your phone receptionist (Twilio)</li>
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
          status.innerHTML = '<span style="color:#4f8cff">Scanning your website…</span>';
          try {
            const r = await api('/api/owner/onboarding/discover', {
              method: 'POST', body: JSON.stringify({url}),
            });
            if (r.error) throw new Error(r.error);
            _wizState.draft = r.draft;
            _wizState.gapQuestions = r.gap_questions || [];
            _wizState.currentGap = 0;
          } catch (err) {
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
              ${row('Hours', d.hours && Object.keys(d.hours).length ? Object.keys(d.hours).length+' day(s) of hours found' : '', 'hours')}
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

      // 6. Phone (Twilio)
      {
        title: "Phone receptionist",
        nextLabel: "Continue",
        showSkip: true,
        render: () => `
          <p style="font-size:14px">Orbi can answer calls 24/7 if you give her a phone number. This uses Twilio — you'll need a Twilio account (~$1/mo per number + a few cents per call).</p>
          <p class="muted" style="font-size:13px;margin-top:8px">You can configure this from Settings → Phone whenever you're ready. Skip for now if you don't have Twilio set up.</p>
        `,
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
    document.getElementById('wiz-skip-btn').hidden = !s.showSkip;
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

    // Auto-open if business_info looks empty on first load
    setTimeout(async () => {
      try {
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
      const a = new Audio('/tts?text=' + encodeURIComponent(text));
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
      custom.hidden = (p.id !== 'custom');
      document.getElementById('imap-host').value = p.imap_host || '';
      document.getElementById('imap-port').value = p.imap_port || '';
      document.getElementById('smtp-host').value = p.smtp_host || '';
      document.getElementById('smtp-port').value = p.smtp_port || '';
    }
    sel.addEventListener('change', syncProvider);
    syncProvider();
    document.getElementById('imap-email').value = '';
    document.getElementById('imap-password').value = '';
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
  let _teamActiveOther = null;    // currently-open thread (other user)
  let _teamPollHandle = null;     // setInterval handle for thread polling

  async function _teamLoadMe() {
    if (_teamMe) return _teamMe;
    try {
      const r = await fetch('/api/owner/whoami');
      if (r.ok) _teamMe = await r.json();
    } catch {}
    return _teamMe;
  }

  async function _teamRenderStaffList() {
    const list = document.getElementById('team-staff-list');
    if (!list) return;
    try {
      await _teamLoadMe();
      const [staffRes, msgRes] = await Promise.all([
        fetch('/api/owner/staff'),
        fetch('/api/owner/internal_messages?limit=500'),
      ]);
      const staffData = await staffRes.json();
      const msgData = await msgRes.json();
      const active = (staffData.active || []).filter(u =>
        u.username !== (_teamMe?.username || ''));
      // Compute unread counts + last-message previews per other user
      const msgs = msgData.messages || [];
      const meta = {};   // other_username → {unread, lastMsg, lastTs}
      for (const m of msgs) {
        const other = m.to === _teamMe?.username ? m.from : m.to;
        if (!other || other === _teamMe?.username) continue;
        if (!meta[other]) meta[other] = { unread: 0, lastMsg: '', lastTs: 0 };
        if (m.created_at > meta[other].lastTs) {
          meta[other].lastTs = m.created_at;
          meta[other].lastMsg = m.body || '';
        }
        if (m.to === _teamMe?.username && !m.read_at) {
          meta[other].unread += 1;
        }
      }
      // Update tab badge with total unread
      const totalUnread = Object.values(meta).reduce((s, v) => s + v.unread, 0);
      const badge = document.getElementById('team-unread-badge');
      if (badge) {
        if (totalUnread > 0) { badge.textContent = totalUnread; badge.hidden = false; }
        else { badge.hidden = true; }
      }
      if (!active.length) {
        list.innerHTML = '<div class="muted" style="padding:12px;font-size:13px">No staff to chat with yet. Add some in Staff tab.</div>';
        return;
      }
      // Sort by last-message time (most recent first), then by name
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
        const active_cls = u.username === _teamActiveOther
          ? 'background:#2a3460' : 'background:transparent';
        html += `
          <div class="team-staff-row" data-other="${_esc(u.username)}"
               style="padding:10px 12px;border-radius:8px;cursor:pointer;${active_cls};margin-bottom:4px">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <div style="font-weight:600;font-size:14px">${_esc(name)}</div>
              ${m.unread > 0 ? `<span style="background:#8b5cf6;color:white;font-size:11px;padding:2px 7px;border-radius:10px">${m.unread}</span>` : ''}
            </div>
            <div style="font-size:12px;color:#9aa4c0;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${_esc(preview)}</div>
          </div>
        `;
      }
      list.innerHTML = html;
      // Wire row clicks
      list.querySelectorAll('.team-staff-row').forEach(row => {
        row.addEventListener('click', () => {
          _teamOpenThread(row.dataset.other,
                           row.querySelector('div > div').textContent.trim());
        });
      });
    } catch (e) {
      list.innerHTML = '<div style="color:#ff7a7a;padding:12px;font-size:13px">' + _esc(e.message) + '</div>';
    }
  }

  async function _teamOpenThread(otherUsername, otherDisplayName) {
    _teamActiveOther = otherUsername;
    const header = document.getElementById('team-chat-header');
    const compose = document.getElementById('team-chat-compose');
    if (header) header.textContent = otherDisplayName || otherUsername;
    if (compose) compose.style.display = 'block';
    await _teamLoadThread();
    _teamRenderStaffList();   // refresh to show active row highlight
    // Start polling every 5s while this thread is open
    if (_teamPollHandle) clearInterval(_teamPollHandle);
    _teamPollHandle = setInterval(() => {
      if (document.getElementById('tab-team')?.classList.contains('active')
          && _teamActiveOther === otherUsername) {
        _teamLoadThread();
      }
    }, 5000);
  }

  async function _teamLoadThread() {
    if (!_teamActiveOther) return;
    const thread = document.getElementById('team-chat-thread');
    if (!thread) return;
    try {
      await _teamLoadMe();
      const r = await fetch(`/api/owner/internal_messages/thread/${encodeURIComponent(_teamActiveOther)}`);
      if (!r.ok) throw new Error('Failed to load thread');
      const data = await r.json();
      const msgs = data.thread || [];
      if (!msgs.length) {
        thread.innerHTML = '<div class="muted" style="text-align:center;font-size:13px;margin:auto">No messages yet. Send the first one.</div>';
        return;
      }
      let html = '';
      for (const m of msgs) {
        const mine = m.from === _teamMe?.username;
        const via = m.via === 'orby' ? ' <span style="font-size:10px;opacity:0.7">via Orby</span>' : '';
        const dt = m.created_at ? new Date(m.created_at * 1000).toLocaleString() : '';
        const align = mine ? 'flex-end' : 'flex-start';
        const bg = mine ? '#8b5cf6' : '#2a3460';
        const color = mine ? 'white' : '#eaf0ff';
        html += `
          <div style="display:flex;justify-content:${align}">
            <div style="max-width:75%;padding:10px 14px;background:${bg};color:${color};border-radius:14px;border-${mine ? 'bottom-right' : 'bottom-left'}-radius:4px">
              <div style="white-space:pre-wrap;font-size:14px">${_esc(m.body)}</div>
              <div style="font-size:10px;opacity:0.7;margin-top:4px">${_esc(dt)}${via}</div>
            </div>
          </div>
        `;
      }
      thread.innerHTML = html;
      thread.scrollTop = thread.scrollHeight;
      // Mark inbound-unread as read (silently, after rendering)
      setTimeout(() => {
        fetch('/api/owner/internal_messages/mark_all_read', { method: 'POST' })
          .catch(() => {});
      }, 1000);
    } catch (e) {
      thread.innerHTML = '<div style="color:#ff7a7a;font-size:13px">' + _esc(e.message) + '</div>';
    }
  }

  async function _teamSendMessage() {
    const input = document.getElementById('team-chat-input');
    if (!input || !_teamActiveOther) return;
    const body = input.value.trim();
    if (!body) return;
    const btn = document.getElementById('team-chat-send');
    btn.disabled = true;
    try {
      const r = await fetch('/api/owner/internal_messages', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to: _teamActiveOther, body })
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'Failed');
      input.value = '';
      await _teamLoadThread();
      _teamRenderStaffList();
    } catch (e) {
      alert('Send failed: ' + e.message);
    } finally {
      btn.disabled = false;
      input.focus();
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
    // Refresh staff list when Team tab opens, plus every 30s while active
    const teamTab = document.querySelector('.tab[data-tab="team"]');
    if (teamTab) teamTab.addEventListener('click', () => _teamRenderStaffList());
    if (document.getElementById('tab-team')?.classList.contains('active')) {
      _teamRenderStaffList();
    }
    // Also load the badge count on initial page load so user knows there's mail
    _teamRenderStaffList();
    setInterval(() => {
      if (document.getElementById('tab-team')?.classList.contains('active')) {
        _teamRenderStaffList();
      } else {
        // Light refresh just for badge count even when tab inactive
        _teamRenderStaffList();
      }
    }, 30000);
  }
  document.addEventListener('DOMContentLoaded', _wireTeamChat);

})();
