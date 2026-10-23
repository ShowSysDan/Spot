// Shared browser helpers for every Spot page. Loaded once from base.html and
// exposed as window.Spot so page scripts stay short.
(function () {
  'use strict';

  const pad = n => String(n).padStart(2, '0');

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // Number formatting: honour a monitor's decimal-places setting when known,
  // otherwise show up to 4 significant decimals without trailing zeros.
  function fmtValue(v, decimals) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    if (typeof v !== 'number') return String(v);
    if (decimals !== null && decimals !== undefined && decimals !== '') {
      return v.toFixed(Number(decimals));
    }
    const abs = Math.abs(v);
    const digits = abs >= 1000 ? 1 : abs >= 1 ? 2 : 4;
    return Number(v.toFixed(digits)).toLocaleString(undefined, { maximumFractionDigits: digits });
  }

  // "12s ago", "4m ago", "3h ago", "2d ago".
  function timeAgo(ts, now) {
    if (!ts) return 'never';
    const sec = Math.max(0, Math.round(((now || Date.now()) - new Date(ts).valueOf()) / 1000));
    if (sec < 5) return 'just now';
    if (sec < 60) return `${sec}s ago`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    return `${Math.floor(sec / 86400)}d ago`;
  }

  // "15 min", "24 h", "7 days" for a window length in seconds.
  function fmtDuration(sec) {
    if (sec < 60) return `${sec} s`;
    if (sec < 3600) return `${Math.round(sec / 60)} min`;
    if (sec < 2 * 86400) return `${Math.round(sec / 3600)} h`;
    return `${Math.round(sec / 86400)} days`;
  }

  // datetime-local inputs speak browser-local time with no offset.
  function toLocalInput(d) {
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
           `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  function toIso(s) { return s ? new Date(s).toISOString() : ''; }

  // Named quick ranges shared by the Query and Reports pages.
  function presetRange(name) {
    const now = new Date();
    const startOfDay = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const hours = h => [new Date(now - h * 3600e3), now];
    switch (name) {
      case '1h': return hours(1);
      case '6h': return hours(6);
      case '24h': return hours(24);
      case '7d': return hours(24 * 7);
      case '30d': return hours(24 * 30);
      case 'today': return [startOfDay(now), now];
      case 'yesterday': {
        const end = startOfDay(now);
        return [new Date(end.getFullYear(), end.getMonth(), end.getDate() - 1), end];
      }
      case 'week': {
        const sod = startOfDay(now);
        const monday = new Date(sod.getFullYear(), sod.getMonth(),
                                sod.getDate() - ((sod.getDay() + 6) % 7));
        return [monday, now];
      }
      default: return null;
    }
  }

  // Wire a row of <button data-preset="..."> chips to a form's start/end inputs.
  function bindPresets(container, form, onPick) {
    if (!container) return;
    container.addEventListener('click', e => {
      const btn = e.target.closest('[data-preset]');
      if (!btn) return;
      const r = presetRange(btn.dataset.preset);
      if (!r) return;
      form.start.value = toLocalInput(r[0]);
      form.end.value = toLocalInput(r[1]);
      container.querySelectorAll('[data-preset]').forEach(b => b.classList.toggle('active', b === btn));
      if (onPick) onPick();
    });
    // Editing a date by hand means no preset is active any more.
    [form.start, form.end].forEach(inp => inp.addEventListener('input', () =>
      container.querySelectorAll('[data-preset]').forEach(b => b.classList.remove('active'))));
  }

  // localStorage can throw (private mode, blocked storage) — never let that break a page.
  const store = {
    get(k, dflt) { try { const v = localStorage.getItem(k); return v === null ? dflt : v; } catch (_) { return dflt; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* ignore */ } },
  };

  // Chart.js plugin: vertical dashed markers for label-only events.
  // Configure with options.plugins.spotEvents = { events: [{x, label}] }.
  const eventLinesPlugin = {
    id: 'spotEvents',
    afterDatasetsDraw(chart, _args, opts) {
      const events = (opts && opts.events) || [];
      const x = chart.scales.x;
      if (!events.length || !x) return;
      const { top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = 'rgba(248, 81, 73, 0.75)';
      ctx.fillStyle = '#ff9b94';
      ctx.setLineDash([4, 3]);
      ctx.lineWidth = 1;
      ctx.font = '11px system-ui, sans-serif';
      for (const ev of events) {
        const px = x.getPixelForValue(ev.x);
        if (px < chart.chartArea.left || px > chart.chartArea.right) continue;
        ctx.beginPath(); ctx.moveTo(px, top); ctx.lineTo(px, bottom); ctx.stroke();
        if (ev.label) {
          ctx.save();
          ctx.translate(px + 3, top + 4);
          ctx.rotate(Math.PI / 2);
          ctx.fillText(String(ev.label).slice(0, 40), 0, 0);
          ctx.restore();
        }
      }
      ctx.restore();
    },
  };
  if (window.Chart) window.Chart.register(eventLinesPlugin);

  // Tiny dependency-free sparkline; points are [[epochMs, value], ...].
  function sparkline(canvas, points, opts) {
    const o = Object.assign({ color: '#58a6ff', fill: 'rgba(88,166,255,0.15)', start: null, end: null }, opts);
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr; canvas.height = h * dpr;
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (!points || points.length < 2) return;
    const x0 = o.start ?? points[0][0], x1 = o.end ?? points[points.length - 1][0];
    let lo = Infinity, hi = -Infinity;
    for (const p of points) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
    if (hi === lo) { hi += 1; lo -= 1; }
    const X = t => ((t - x0) / Math.max(1, x1 - x0)) * (w - 2) + 1;
    const Y = v => h - 2 - ((v - lo) / (hi - lo)) * (h - 4);
    ctx.beginPath();
    points.forEach((p, i) => (i ? ctx.lineTo(X(p[0]), Y(p[1])) : ctx.moveTo(X(p[0]), Y(p[1]))));
    ctx.strokeStyle = o.color; ctx.lineWidth = 1.5; ctx.lineJoin = 'round'; ctx.stroke();
    ctx.lineTo(X(points[points.length - 1][0]), h); ctx.lineTo(X(points[0][0]), h); ctx.closePath();
    ctx.fillStyle = o.fill; ctx.fill();
  }

  // Poll `fn` every `ms` without ever overlapping requests, and pause while
  // the tab is hidden so background tabs don't hammer the server.
  // Returns { setInterval(ms), pause(), resume(), now(), stop() }.
  function poller(fn, ms) {
    let timer = null, running = false, paused = false, interval = ms, stopped = false;
    async function run() {
      timer = null;
      if (stopped || paused || document.hidden) return;
      if (running) return;
      running = true;
      try { await fn(); } catch (e) { console.warn('Spot poll failed:', e); }
      running = false;
      schedule();
    }
    function schedule() {
      if (stopped || paused || document.hidden || timer) return;
      timer = setTimeout(run, interval);
    }
    function clear() { if (timer) { clearTimeout(timer); timer = null; } }
    const onVis = () => { if (document.hidden) clear(); else { clear(); run(); } };
    document.addEventListener('visibilitychange', onVis);
    run();
    return {
      setInterval(n) { interval = n; clear(); schedule(); },
      pause() { paused = true; clear(); },
      resume() { paused = false; clear(); run(); },
      now() { clear(); run(); },
      stop() { stopped = true; clear(); document.removeEventListener('visibilitychange', onVis); },
      get paused() { return paused; },
    };
  }

  // Forms with data-confirm="message" ask before submitting. Reading the
  // message from a data attribute (instead of inline JS) keeps monitor
  // names with quotes from breaking — or injecting into — the handler.
  document.addEventListener('submit', e => {
    const f = e.target;
    if (f instanceof HTMLFormElement && f.dataset.confirm && !window.confirm(f.dataset.confirm)) {
      e.preventDefault();
    }
  });

  // Copy-to-clipboard buttons: <button data-copy="text">.
  document.addEventListener('click', e => {
    const btn = e.target.closest('[data-copy]');
    if (!btn) return;
    const text = btn.dataset.copy;
    const done = () => { const t = btn.textContent; btn.textContent = 'Copied'; setTimeout(() => (btn.textContent = t), 1200); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, () => {});
    } else {
      // Fallback for plain-http deployments where the async clipboard API is unavailable.
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } catch (_) { /* ignore */ }
      ta.remove();
    }
  });

  window.Spot = { escapeHtml, fmtValue, timeAgo, fmtDuration, toLocalInput, toIso,
                  presetRange, bindPresets, store, sparkline, poller };
})();
