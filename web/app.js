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
    return $('banners').appendChild(el('p', 'banner ' + level, text));
  }

  const STATE_LEVEL = {
    PROTECTED: 'ok', REBUILDING: 'warn', DEGRADED: 'fail', UNPROTECTED: 'fail', UNMOUNTED: 'fail',
  };

  let lastStatus = null;
  // render() rebuilds the banners on every poll, so the outcome of something the operator
  // just did has to be re-added for a while or it is gone before it can be read.
  let notice = null;
  function announce(level, text) {
    notice = { level, text, until: Date.now() + 60000 };
    banner(level, text);
  }

  function render(d) {
    lastStatus = d;
    clear($('banners'));
    if (notice && Date.now() < notice.until) banner(notice.level, notice.text);
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
    renderStale(d);

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

    // What the per-drive bars are measured against. btrfs places a new chunk as one extent
    // on each of the drives with the most unallocated space, so what runs out is bytes on a
    // drive, not a percentage of the pool — the same quantity chunk_headroom() watches.
    // Defaults match DEFAULT_CONFIG, for the status-file fallback that carries no config.
    const headWarn = (d.config && d.config.chunk_headroom_warn) || 16 * 1024 ** 3;
    const headFail = (d.config && d.config.chunk_headroom_fail) || 2 * 1024 ** 3;

    const tb = $('drives').querySelector('tbody');
    clear(tb);
    for (const dev of d.devices || []) {
      const tr = el('tr', dev.missing ? 'gone' : '');
      tr.appendChild(el('td', null, dev.devid, 'id'));
      tr.appendChild(el('td', null, dev.name, 'device'));
      tr.appendChild(el('td', null, dev.model || '?', 'model'));
      tr.appendChild(el('td', 'num', bytes(dev.size), 'size'));
      tr.appendChild(el('td', 'num', bytes(dev.alloc), 'alloc'));
      // How full this drive is, which the headline bar cannot show: that one is the pool's
      // logical capacity, and a pool at 70% can still have one drive with nothing left to
      // allocate. An uneven row of bars is what a balance is for.
      const barCell = el('td', 'barcol');
      if (dev.size > 0 && !dev.missing) {
        const free = dev.size - dev.alloc;
        const pct = Math.min(100, (dev.alloc / dev.size) * 100);
        const fill = el('div', free < headFail ? 'fail' : free < headWarn ? 'warn' : '');
        fill.style.width = pct + '%';
        const box = el('div', 'bar mini');
        box.appendChild(fill);
        barCell.appendChild(box);
        barCell.title = `${bytes(dev.alloc)} of ${bytes(dev.size)} allocated (${pct.toFixed(0)}%), `
          + `${bytes(free)} unallocated`;
      }
      tr.appendChild(barCell);
      // Since the last scrub that left every block good; btrfs's lifetime count is kept for
      // the tooltip, because a drive that was unplugged once carries its failures forever.
      // Zero is the normal case and is worth no ink: the card drops it entirely, and only a
      // count that is actually there gets a line of its own.
      const count = dev.errors_new ?? dev.errors_total;
      const errs = el('td', 'num ' + (Number(count) > 0 ? 'bad' : 'zero'), count, 'errors');
      if (dev.errors_new !== undefined && dev.errors_new !== dev.errors_total) {
        errs.title = `${dev.errors_total} since btrfs started counting`
          + (d.errors_since ? `; counted from the scrub of ${new Date(d.errors_since * 1000).toLocaleString()}` : '');
      }
      tr.appendChild(errs);
      const health = el('td', null, null, 'health');
      health.appendChild(el('span', 'dot ' + (dev.health_level || 'unknown')));
      health.appendChild(document.createTextNode(dev.health || '?'));
      tr.appendChild(health);

      const act = el('td', null, null, '');
      const r = (d.removable || {})[String(dev.devid)] || {};
      if (r.removing) {
        // Already on its way out: no button, just what is happening to it.
        act.appendChild(el('span', 'why', 'removing — data is being relocated'));
      } else if (!dev.missing && !(d.config || {}).read_only) {
        const b = el('button', null, 'Remove');
        b.disabled = r.ok !== true;
        if (r.reason) b.title = r.reason;            // the CLI's own refusal, verbatim
        b.addEventListener('click', () => askConfirm(
          { action: 'remove', target: String(dev.devid) },
          `braidctl remove ${dev.devid}`,
          `${dev.name} ${dev.model || ''} ${bytes(dev.size)}. Data is relocated first, which can take hours. It can be cancelled while it runs; the drive then stays in the pool.`));
        act.appendChild(b);
        if (r.ok !== true && r.reason) act.appendChild(el('div', 'why', r.reason));
      }
      tr.appendChild(act);
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

    renderOperations(d);

    const unmounted = state === 'UNMOUNTED';
    for (const a of ['mount', 'mount-degraded']) {
      const b = document.querySelector(`[data-action="${a}"]`);
      if (b) b.hidden = !unmounted;
    }

    $('source').textContent = d.source === 'live' ? 'live' : `${d.source}, ${duration(d.age_sec)} old`;
    $('smart-age').textContent = d.smart_at
      ? `health as of ${new Date(d.smart_at * 1000).toLocaleTimeString()}` : 'health from the last monitor pass';
    $('lock').textContent = d.lock && d.lock.held ? `busy: ${d.lock.cmdline || 'another braidctl'}` : '';
  }

  // What is running on the pool, however it was started: a job from this page, the
  // scrub timer, a balance, someone at a terminal. Whether each
  // can be stopped is the server's call, from the same check `braidctl cancel` makes.
  let stopping = null;           // the kind whose cancel request is still in flight

  // A drive evicted while it was absent still carries the pool's superblock. braidctl no
  // longer counts it, but it is worth saying so: it looks like a member to anything else.
  function renderStale(d) {
    const cfg = d.config || {};
    for (const s of d.stale_members || []) {
      const what = [s.model, s.serial && `serial ${s.serial}`].filter(Boolean).join(', ');
      const p = banner('warn', `${s.path}${what ? ` (${what})` : ''} was devid ${s.devid} of this pool`
        + (s.evicted_at ? `, evicted ${new Date(s.evicted_at * 1000).toLocaleDateString()}` : '')
        + ' — it is not a member, but still carries the pool\'s signature. Erase it before reusing it.');
      if (cfg.read_only || busy(d)) continue;
      if (!cfg.allow_force_add) {
        p.appendChild(el('div', 'why',
          'erasing it from here needs [web] allow_force_add = true; or run `sudo braidctl wipe-stale ' + s.path + '`'));
        continue;
      }
      const b = el('button', null, 'Erase…');
      b.addEventListener('click', () => {
        // the same string the server compares against (check_wipe_confirmation)
        const want = s.serial || `WIPE ${s.model || s.path.split('/').pop()} ${s.size}`;
        const typed = window.prompt(`This ERASES ${s.path}. Type exactly:\n${want}`);
        if (typed === null) return;
        askConfirm({ action: 'wipe-stale', device: s.path, confirm: typed, confirm_wipe: 'ERASE' },
          `braidctl wipe-stale ${s.path}`,
          'wipefs erases the old pool signature. What was on the drive is gone, and this cannot be undone.');
      });
      p.appendChild(document.createTextNode(' '));
      p.appendChild(b);
    }
  }

  function busy(d) { return !!(d.lock && d.lock.held); }

  function renderOperations(d) {
    const ops = d.operations || [];
    const box = $('ops');
    clear(box);
    $('ops-card').hidden = !ops.length;
    const readOnly = (d.config || {}).read_only;
    for (const op of ops) {
      const row = el('div', 'cand');
      let pct = op.pct;
      if (op.kind === 'scrub' && d.scrub) pct = d.scrub.pct;
      row.appendChild(el('div', null, [op.label, op.phase,
        pct !== null && pct !== undefined ? pct + '%' : null].filter(Boolean).join(' · ')));
      if (!op.cancellable) {
        row.appendChild(el('div', 'why', `can't be cancelled here: ${op.reason}`));
      } else if (!readOnly) {
        row.appendChild(el('div', 'why', op.note || ''));
        const b = el('button', null, stopping === op.kind ? 'Stopping…' : 'Cancel…');
        b.disabled = stopping === op.kind;
        b.addEventListener('click', () => askCancel('/api/cancel', op));
        row.appendChild(b);
      }
      box.appendChild(row);
    }
  }

  function askCancel(path, op) {
    askConfirm({ kind: op.kind }, `braidctl cancel ${op.kind}`,
      `Stops the ${op.label}. ${op.note || ''}`, { path, yes: 'Stop it', no: 'Keep it running' });
  }

  // --- actions ------------------------------------------------------------
  // Every button opens one dialog showing the exact command the server will run.
  // That is the trust mechanism: this is a thing you could have typed yourself.
  const COMMANDS = {
    scrub: { cmd: 'braidctl scrub',
             note: 'Reads every block and checks it against its checksum. Takes hours and uses every drive at once.' },
    balance: { cmd: 'braidctl balance',
               note: 'Rewrites every chunk so the data is spread evenly over all the drives — what an uneven row of bars is for. Takes hours; the pool stays usable and protected, and it can be cancelled.' },
    monitor: { cmd: 'braidctl monitor',
               note: 'One detection pass, the same one the five-minute timer runs.' },
    mount: { cmd: 'braidctl mount', note: 'Mount the pool, waiting for drives to appear.' },
    'mount-degraded': { cmd: 'braidctl mount-degraded',
                        note: 'Mount with a drive missing. Writes go to fewer copies until it is re-protected.' },
  };

  let pending = null;

  function askConfirm(body, cmd, note, opts) {
    opts = opts || {};
    pending = { body, path: opts.path || '/api/jobs' };
    $('confirm-yes').textContent = opts.yes || 'Run it';
    $('confirm-no').textContent = opts.no || 'Cancel';
    $('confirm-title').textContent = cmd;
    $('confirm-cmd').textContent = 'sudo ' + cmd;
    $('confirm-note').textContent = note || '';
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
      b.addEventListener('click', () => {
        const a = b.dataset.action;
        const spec = COMMANDS[a] || { cmd: 'braidctl ' + a, note: '' };
        askConfirm({ action: a }, spec.cmd, spec.note);
      });
    }
    $('show-candidates').addEventListener('click', () => {
      const box = $('candidates');
      box.hidden = !box.hidden;
      if (!box.hidden) loadCandidates();
    });
    $('confirm-no').addEventListener('click', () => { $('confirm').close(); pending = null; });
    $('confirm-yes').addEventListener('click', async () => {
      $('confirm').close();
      const req = pending;
      pending = null;
      if (!req) return;
      if (req.path !== '/api/jobs') {
        // A stop. A balance only stops after the chunk it is moving, so this can take a
        // while; say so rather than leaving a button that looks like it did nothing.
        stopping = req.body.kind;
        if (lastStatus) renderOperations(lastStatus);
        try {
          const res = await post(req.path, req.body);
          announce('info', res.message || `${req.body.kind} cancelled`);
        } catch (e) {
          announce('fail', e.message);
        } finally {
          stopping = null;
          poll();
        }
        return;
      }
      try {
        const res = await post(req.path, req.body);
        current = { id: res.id, offset: 0 };
        $('job-log').textContent = '';
        pollJob();
        refreshCandidates();
      } catch (e) {
        banner('fail', e.message);
      }
    });
    $('job-cancel').addEventListener('click', () => {
      if (!current) return;
      const kind = $('job-cancel').dataset.kind;
      const op = ((lastStatus || {}).operations || []).find((o) => o.kind === kind)
        || { kind, label: kind };
      askCancel(`/api/jobs/${current.id}/cancel`, op);
    });
  }

  // The list is a snapshot of lsblk, so anything that changes the pool (a job from this
  // page, the monitor, someone at a terminal) leaves it stale. Re-read while it is open.
  function refreshCandidates() {
    if (!$('candidates').hidden) loadCandidates();
  }

  async function loadCandidates() {
    const box = $('candidates');
    let d;
    try { d = await (await fetch('/api/devices/candidates')).json(); }
    catch (e) { clear(box); box.appendChild(el('p', 'dim', e.message)); return; }
    // Clear only once the answer is in, so a periodic refresh doesn't flash an empty panel.
    clear(box);
    if (d.busy) {
      box.appendChild(el('p', 'why', `another operation holds the pool, so nothing can be added until it finishes: ${d.busy}`));
    }
    for (const c of d.candidates || []) {
      if (c.status === 'member') continue;
      const row = el('div', 'cand');
      const name = el('div', null, `${c.path} · ${c.model || '?'} · ${bytes(c.size)}`);
      row.appendChild(name);
      // Refused drives stay on screen with the reason braidctl gave. Hiding them sends
      // the operator to a terminal with lsblk, which is where the wrong sdX gets typed.
      row.appendChild(el('div', 'why', c.reason || ''));
      if (d.busy) {
        // listed, with its reason, but no button while the pool is held
      } else if (c.status === 'ok') {
        const b = el('button', null, 'Add');
        b.addEventListener('click', () => askConfirm({ action: 'add', device: c.path },
          `braidctl add ${c.path}`,
          `${c.model || ''} ${bytes(c.size)}. Existing data stays where it is and new writes go to it first; Balance now spreads the rest.`));
        row.appendChild(b);
      } else if (c.status === 'needs_force') {
        const b = el('button', null, 'Erase and add…');
        b.addEventListener('click', () => {
          const want = c.serial || `WIPE ${c.model} ${c.size}`;
          const typed = window.prompt(`This ERASES ${c.path}. Type exactly:\n${want}`);
          if (typed === null) return;
          askConfirm({ action: 'add', device: c.path, confirm: typed, confirm_wipe: 'ERASE' },
            `braidctl add --force ${c.path}`,
            'wipefs runs first. Everything on that drive is gone, and this cannot be undone.');
        });
        row.appendChild(b);
      }
      if (c.status === 'has_data' && !d.busy) {
        row.appendChild(el('div', 'why',
          'erasing it from here needs [web] allow_force_add = true; the page then asks for its serial before it runs'));
      }
      box.appendChild(row);
    }
    if (!box.childElementCount) box.appendChild(el('p', 'dim', 'no drives outside the pool'));
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
    const running = ['queued', 'running'].includes(job.state);
    const bar = $('job-bar');
    // An empty bar under a finished job reads as "stuck at zero".
    bar.parentElement.hidden = !running || p.pct === null || p.pct === undefined;
    bar.style.width = (p.pct !== null && p.pct !== undefined ? Math.min(100, p.pct) : 0) + '%';
    $('job-detail').textContent = [p.phase, p.pct !== null && p.pct !== undefined ? p.pct + '%' : null,
      p.detail, p.time_left ? p.time_left + ' left' : null].filter(Boolean).join(' · ');
    // Cancel only where cancelling means anything: a scrub, a balance or a device remove.
    // add, monitor and mount are over in seconds.
    const cancel = $('job-cancel');
    cancel.hidden = !(job.cancellable && job.state === 'running');
    cancel.disabled = !!job.cancel_requested;
    cancel.textContent = job.cancel_requested ? 'Stopping…' : 'Cancel…';
    cancel.dataset.kind = job.action;
    $('job-log').hidden = !$('job-log').textContent;
    if (job.error) banner('fail', `${job.action}: ${job.error}`);
  }

  async function pollJob() {
    if (!current) return;
    try {
      const job = await (await fetch(`/api/jobs/${current.id}`)).json();
      renderJob(job);
      // Once its command has had a moment to start, refresh the Running card rather than
      // leaving it a status interval behind the job.
      if (job.state === 'running' && !current.seenRunning) {
        current.seenRunning = true;
        setTimeout(poll, 1500);
      }
      const log = await (await fetch(`/api/jobs/${current.id}/log?offset=${current.offset}`)).json();
      if (log.text) {
        const pre = $('job-log');
        const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 24;
        pre.textContent += log.text;
        current.offset = log.next;
        if (stick) pre.scrollTop = pre.scrollHeight;
      }
      if (['queued', 'running'].includes(job.state)) setTimeout(pollJob, 2000);
      else { poll(); refreshCandidates(); current = null; }
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
  setInterval(() => { if (!current) loadJobs(); refreshCandidates(); }, POLL_MS * 3);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
}());
