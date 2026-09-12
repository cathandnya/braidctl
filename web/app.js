'use strict';
// Rendering only. Every server-derived string goes through textContent: drive models and
// btrfs's own sentences are not fully under our control, so innerHTML is never used.
(function () {
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 10000;

  // Matches fmt_bytes() in braidctl: decimal units, so the page and the CLI never
  // disagree about how big a drive is.
  function bytes(n) {
    if (n === null || n === undefined) return '—';
    n = Number(n);
    for (const [unit, div] of [['TB', 1e12], ['GB', 1e9], ['MB', 1e6], ['KB', 1e3]]) {
      if (n >= div) return (n / div).toFixed(2) + ' ' + unit;
    }
    return Math.round(n) + ' B';
  }

  function duration(sec) {
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m ${sec % 60}s`;
    return `${sec}s`;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function el(tag, cls, text, key) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (key) n.dataset.k = key;                 // the stacked-card layout on phones
    return n;
  }

  function banner(level, text) {
    $('banners').appendChild(el('p', 'banner ' + level, text));
  }

  const STATE_LEVEL = {
    PROTECTED: 'ok', REBUILDING: 'warn', DEGRADED: 'fail', UNPROTECTED: 'fail', UNMOUNTED: 'fail',
  };

  function render(d) {
    clear($('banners'));
    $('label').textContent = d.label || 'braid';
    $('mountpoint').textContent = d.mountpoint || '';
    const state = d.state || '?';
    const chip = $('state');
    chip.textContent = state;
    chip.className = 'chip ' + (STATE_LEVEL[state] || '');
    $('degraded').hidden = !d.mounted_degraded;
    const profs = d.profiles ? [...new Set(Object.values(d.profiles))].join('/') : '';
    $('profiles').hidden = !profs;
    $('profiles').textContent = profs;

    // Never dress up stale data as live: say which it is and how old.
    if (d.source === 'snapshot') {
      banner('warn', `showing the monitor's snapshot, ${duration(d.age_sec)} old — live collection failed`
        + (d.error ? `: ${d.error}` : ''));
    } else if (d.source === 'cache') {
      banner('warn', `showing the last good reading, ${duration(d.age_sec)} old`
        + (d.error ? ` — ${d.error}` : ''));
    }
    if (d.note) banner('fail', d.note);

    const u = d.usage;
    $('usable').textContent = u ? bytes(u.usable) : '—';
    $('raw').textContent = u ? bytes(u.raw) : '—';
    $('used').textContent = u ? bytes(u.used) : '—';
    $('free').textContent = u ? bytes(u.free) : '—';
    const pct = u && u.pct !== null && u.pct !== undefined ? u.pct : 0;
    const bar = $('bar-used');
    bar.style.width = Math.min(100, pct) + '%';
    const warnAt = (d.config && d.config.warn_percent) || 90;
    bar.className = pct >= 95 ? 'fail' : pct >= warnAt ? 'warn' : '';

    // btrfs's own sentence, rendered rather than paraphrased: it carries the arithmetic
    // that justifies the colour.
    if (d.verdict) {
      $('verdict').textContent = d.verdict.text;
      $('verdict').style.color = d.verdict.level === 'fail' ? 'var(--fail)'
        : d.verdict.level === 'warn' ? 'var(--warn)' : '';
    }

    if (d.balance && d.balance.running) {
      const p = d.balance.pct !== null && d.balance.pct !== undefined ? ` ${d.balance.pct}%` : '';
      const c = d.balance.chunks_total ? ` (${d.balance.chunks_done} of about ${d.balance.chunks_total} chunks)` : '';
      banner('info', `balance in progress${p}${c} — performance is reduced until it finishes`);
    }
    if (d.rebuild) banner('warn', `re-protecting the pool, started ${new Date(d.rebuild.started * 1000).toLocaleString()}`);

    // A drive that vanished is on a countdown before the pool re-lays-out without it.
    const debounce = (d.config && d.config.debounce_sec) || 600;
    for (const [devid, since] of Object.entries(d.missing_since || {})) {
      const left = (Number(since) + debounce) - (Date.now() / 1000);
      const removable = (d.removable || {})[devid];
      if (left > 0) banner('warn', `devid ${devid} is missing — re-protection starts in ${duration(left)}`);
      else banner('warn', `devid ${devid} is missing`);
      if (removable && removable.ok === false) banner('fail', `cannot re-protect without devid ${devid}: ${removable.reason}`);
    }

    const tb = $('drives').querySelector('tbody');
    clear(tb);
    for (const dev of d.devices || []) {
      const tr = el('tr', dev.missing ? 'gone' : '');
      tr.appendChild(el('td', null, dev.devid, 'id'));
      tr.appendChild(el('td', null, dev.name, 'device'));
      tr.appendChild(el('td', null, dev.model || '?', 'model'));
      tr.appendChild(el('td', 'num', bytes(dev.size), 'size'));
      tr.appendChild(el('td', 'num', bytes(dev.alloc), 'alloc'));
      tr.appendChild(el('td', 'num', dev.errors_total, 'errors'));
      const health = el('td', null, null, 'health');
      health.appendChild(el('span', 'dot ' + (dev.health_level || 'unknown')));
      health.appendChild(document.createTextNode(dev.health || '?'));
      tr.appendChild(health);
      tb.appendChild(tr);
    }

    const s = d.scrub;
    if (s && s.started) {
      const when = new Date(s.started * 1000).toLocaleString();
      $('scrub').textContent = `${when} (${s.status || '?'}${s.duration ? ', ' + s.duration : ''}): ${s.text || s.error_summary || ''}`;
      $('scrub').style.color = s.level === 'fail' ? 'var(--fail)' : s.level === 'warn' ? 'var(--warn)' : '';
      if (s.status === 'running' && s.pct !== null && s.pct !== undefined) {
        $('scrub').textContent += ` — ${s.pct}%${s.time_left ? ', ' + s.time_left + ' left' : ''}`;
      }
    } else {
      $('scrub').textContent = 'no scrub has run yet';
    }

    $('source').textContent = d.source === 'live' ? 'live' : `${d.source}, ${duration(d.age_sec)} old`;
    $('smart-age').textContent = d.smart_at
      ? `health as of ${new Date(d.smart_at * 1000).toLocaleTimeString()}` : 'health from the last monitor pass';
    $('lock').textContent = d.lock && d.lock.held ? `busy: ${d.lock.cmdline || 'another braidctl'}` : '';
  }

  // --- actions ------------------------------------------------------------
  // Every button opens one dialog showing the exact command the server will run.
  // That is the trust mechanism: this is a thing you could have typed yourself.
  const COMMANDS = {
    scrub: { cmd: 'braidctl scrub',
             note: 'Reads every block and checks it against its checksum. Takes hours and uses every drive at once.' },
    monitor: { cmd: 'braidctl monitor',
               note: 'One detection pass, the same one the five-minute timer runs.' },
  };

  let pending = null;

  function askConfirm(action) {
    const spec = COMMANDS[action];
    pending = action;
    $('confirm-title').textContent = spec.cmd;
    $('confirm-cmd').textContent = 'sudo ' + spec.cmd;
    $('confirm-note').textContent = spec.note;
    $('confirm').showModal();
  }

  async function post(path, body) {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Braid-CSRF': '1' },
      body: JSON.stringify(body || {}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  }

  function wireActions() {
    for (const b of document.querySelectorAll('[data-action]')) {
      b.addEventListener('click', () => askConfirm(b.dataset.action));
    }
    $('confirm-no').addEventListener('click', () => { $('confirm').close(); pending = null; });
    $('confirm-yes').addEventListener('click', async () => {
      $('confirm').close();
      const action = pending;
      pending = null;
      try {
        const job = await post('/api/jobs', { action });
        current = { id: job.id, offset: 0 };
        $('job-log').textContent = '';
        pollJob();
      } catch (e) {
        banner('fail', e.message);
      }
    });
    $('job-cancel').addEventListener('click', async () => {
      if (!current) return;
      try { await post(`/api/jobs/${current.id}/cancel`); } catch (e) { banner('fail', e.message); }
    });
  }

  // --- jobs ---------------------------------------------------------------
  let current = null;

  function renderJob(job) {
    $('job-card').hidden = false;
    const started = job.started ? new Date(job.started * 1000).toLocaleTimeString() : '';
    const elapsed = job.started ? duration((job.finished || Date.now() / 1000) - job.started) : '';
    $('job-head').textContent = `${job.action} — ${job.state}${started ? `, started ${started}` : ''}`
      + (elapsed ? `, ${elapsed}` : '') + (job.exit !== null && job.exit !== undefined ? `, exit ${job.exit}` : '');
    const p = job.progress || {};
    const bar = $('job-bar');
    bar.style.width = (p.pct !== null && p.pct !== undefined ? Math.min(100, p.pct) : 0) + '%';
    $('job-detail').textContent = [p.phase, p.pct !== null && p.pct !== undefined ? p.pct + '%' : null,
      p.detail, p.time_left ? p.time_left + ' left' : null].filter(Boolean).join(' · ');
    // Cancel only where cancelling means anything: btrfs keeps relocating after a
    // device remove is interrupted, so a button there would be a lie.
    $('job-cancel').hidden = !(job.cancellable && job.state === 'running');
    if (job.error) banner('fail', `${job.action}: ${job.error}`);
  }

  async function pollJob() {
    if (!current) return;
    try {
      const job = await (await fetch(`/api/jobs/${current.id}`)).json();
      renderJob(job);
      const log = await (await fetch(`/api/jobs/${current.id}/log?offset=${current.offset}`)).json();
      if (log.text) {
        const pre = $('job-log');
        const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 24;
        pre.textContent += log.text;
        current.offset = log.next;
        if (stick) pre.scrollTop = pre.scrollHeight;
      }
      if (['queued', 'running'].includes(job.state)) setTimeout(pollJob, 2000);
      else { poll(); current = null; }
    } catch (e) {
      setTimeout(pollJob, 5000);
    }
  }

  async function loadJobs() {
    try {
      const d = await (await fetch('/api/jobs')).json();
      const busy = (d.jobs || []).find((j) => ['queued', 'running'].includes(j.state));
      const last = (d.jobs || [])[0];
      for (const b of document.querySelectorAll('[data-action]')) b.disabled = !!busy || d.read_only;
      $('actions-note').textContent = d.read_only ? 'this braidctl web is configured read_only'
        : busy ? `a ${busy.action} job is running` : '';
      if (busy) { current = current || { id: busy.id, offset: 0 }; pollJob(); }
      else if (last) renderJob(last);
    } catch (e) { /* the status poll reports the outage */ }
  }

  let failures = 0;
  async function poll() {
    if (document.hidden) return;
    try {
      const r = await fetch('/api/status', { headers: { Accept: 'application/json' } });
      if (r.status === 401) { document.body.innerHTML = ''; banner('fail', 'unauthorised — open the URL from `braidctl web --url`'); return; }
      const d = await r.json();
      if (!r.ok) throw new Error((d && d.error) || r.statusText);
      failures = 0;
      render(d);
    } catch (e) {
      failures += 1;
      // Keep the last values on screen. A blank page on a NAS reads as "the pool is gone".
      if (failures >= 2) banner('fail', `lost contact with braidctl web (${e.message})`);
    }
  }

  wireActions();
  poll();
  loadJobs();
  setInterval(poll, POLL_MS);
  setInterval(() => { if (!current) loadJobs(); }, POLL_MS * 3);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
}());
