/* Nutrition & Workout — native-like app behaviour.
   Sheets, action-sheet confirms, in-page views, collapsing title, submit feedback,
   install prompt, toasts and theme-aware chart defaults. No dependencies. */
(function () {
  'use strict';

  const root = document.documentElement;
  const App = (window.App = window.App || {});
  const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));
  const reduceMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  const haptic = (ms = 8) => { try { navigator.vibrate && navigator.vibrate(ms); } catch (_) {} };

  /* ---------- Theme helpers ---------- */
  const token = (name, alpha) => {
    const v = getComputedStyle(root).getPropertyValue(name).trim();
    return alpha == null ? `rgb(${v})` : `rgb(${v} / ${alpha})`;
  };
  App.theme = () => ({
    dark: root.classList.contains('dark'),
    fg: token('--fg'),
    muted: token('--fg-muted'),
    line: token('--line'),
    surface: token('--surface'),
    primary: token('--primary'),
  });

  /* ---------- Chart.js defaults (runs before page scripts) ---------- */
  function applyChartDefaults() {
    if (!window.Chart) return;
    const t = App.theme();
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.font.size = 12;
    Chart.defaults.color = t.muted;
    Chart.defaults.borderColor = t.line;
    Chart.defaults.plugins.legend.labels.usePointStyle = true;
    Chart.defaults.plugins.legend.labels.boxWidth = 8;
    Chart.defaults.plugins.tooltip.backgroundColor = t.dark ? '#2C2C2E' : '#0F172A';
    Chart.defaults.plugins.tooltip.padding = 10;
    Chart.defaults.plugins.tooltip.cornerRadius = 10;
  }
  applyChartDefaults();

  /* ---------- Appearance: System / Light / Dark ---------- */
  function syncThemeControls() {
    const pref = root.dataset.themePref || 'system';
    $$('[data-theme-set]').forEach(b => b.setAttribute('aria-checked', String(b.dataset.themeSet === pref)));
  }
  App.setTheme = function (pref) {
    root.dataset.themePref = pref;
    try { localStorage.setItem('theme', pref); } catch (_) {}
    if (window.__applyTheme) window.__applyTheme();
    syncThemeControls();
  };
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-theme-set]');
    if (!b) return;
    haptic(6);
    App.setTheme(b.dataset.themeSet);
  });
  // Pages rebuild their charts on this event; defaults must be refreshed first.
  document.addEventListener('app:themechange', applyChartDefaults, true);

  /* =====================================================================
     Sheets
     ===================================================================== */
  let openSheetEl = null;
  let lastFocus = null;

  function focusables(el) {
    return $$('a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select, textarea, [tabindex]:not([tabindex="-1"])', el)
      .filter(n => n.offsetParent !== null);
  }

  App.openSheet = function (id, opener) {
    const sheet = typeof id === 'string' ? document.getElementById(id) : id;
    if (!sheet) return;
    if (openSheetEl && openSheetEl !== sheet) App.closeSheet(openSheetEl, true);
    lastFocus = opener || document.activeElement;
    sheet.hidden = false;
    sheet.setAttribute('aria-hidden', 'false');
    root.classList.add('overflow-hidden');
    document.body.classList.add('overflow-hidden');
    requestAnimationFrame(() => requestAnimationFrame(() => sheet.classList.add('is-open')));
    openSheetEl = sheet;
    const first = sheet.querySelector('[autofocus]') || focusables(sheet)[0];
    setTimeout(() => first && first.focus({ preventScroll: true }), reduceMotion() ? 0 : 120);
  };

  App.closeSheet = function (id, instant) {
    const sheet = typeof id === 'string' ? document.getElementById(id) : (id || openSheetEl);
    if (!sheet || sheet.hidden) return;
    sheet.classList.remove('is-open');
    const panel = sheet.querySelector('.sheet-panel');
    if (panel) panel.style.transform = '';
    const done = () => {
      sheet.hidden = true;
      sheet.setAttribute('aria-hidden', 'true');
      if (openSheetEl === sheet) openSheetEl = null;
      if (!openSheetEl) { root.classList.remove('overflow-hidden'); document.body.classList.remove('overflow-hidden'); }
      if (lastFocus && document.contains(lastFocus)) lastFocus.focus({ preventScroll: true });
      sheet.dispatchEvent(new CustomEvent('sheet:closed'));
    };
    if (instant || reduceMotion()) done(); else setTimeout(done, 300);
  };

  document.addEventListener('click', (e) => {
    const opener = e.target.closest('[data-sheet-open]');
    if (opener) { e.preventDefault(); App.openSheet(opener.dataset.sheetOpen, opener); return; }
    const closer = e.target.closest('[data-sheet-close]');
    if (closer) { e.preventDefault(); App.closeSheet(closer.closest('.sheet')); }
  });

  document.addEventListener('keydown', (e) => {
    if (!openSheetEl) return;
    if (e.key === 'Escape') { e.preventDefault(); App.closeSheet(openSheetEl); return; }
    if (e.key === 'Tab') {  // keep focus inside the open sheet
      const f = focusables(openSheetEl);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  /* Drag the handle / header down to dismiss (mobile) */
  document.addEventListener('pointerdown', (e) => {
    const grip = e.target.closest('[data-sheet-drag]');
    if (!grip || e.button > 0 || window.innerWidth >= 640) return;
    const sheet = grip.closest('.sheet');
    const panel = sheet.querySelector('.sheet-panel');
    const startY = e.clientY, startT = performance.now();
    let dy = 0;
    sheet.classList.add('is-dragging');
    const move = (ev) => {
      dy = Math.max(0, ev.clientY - startY);
      panel.style.transform = `translateY(${dy}px)`;
    };
    const up = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      document.removeEventListener('pointercancel', up);
      sheet.classList.remove('is-dragging');
      const velocity = dy / (performance.now() - startT);
      if (dy > 110 || velocity > 0.6) App.closeSheet(sheet);
      else panel.style.transform = '';
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
    document.addEventListener('pointercancel', up);
  });

  /* =====================================================================
     Action-sheet confirm for destructive forms: <form data-confirm="…">
     ===================================================================== */
  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (!navigator.onLine) return;  // the offline guard below blocks the submit instead
    if (!form.matches('form[data-confirm]') || form.dataset.confirmed === '1') return;
    if (e.submitter && e.submitter.hasAttribute('data-no-confirm')) return;  // swipe "Delete" is the confirmation
    const sheet = document.getElementById('confirmSheet');
    if (!sheet) return;  // no sheet → submit normally
    e.preventDefault();
    e.stopImmediatePropagation();
    sheet.querySelector('[data-confirm-message]').textContent = form.dataset.confirm;
    const ok = sheet.querySelector('[data-confirm-ok]');
    ok.textContent = form.dataset.confirmAction || 'Delete';
    ok.onclick = () => {
      form.dataset.confirmed = '1';
      haptic(12);
      App.closeSheet(sheet, true);
      form.requestSubmit ? form.requestSubmit() : form.submit();
    };
    App.openSheet(sheet, e.submitter || form.querySelector('[type="submit"]'));
  }, true);

  /* =====================================================================
     Submit feedback: spinner + prevent double submit
     ===================================================================== */
  document.addEventListener('submit', (e) => {
    if (e.defaultPrevented) return;
    const form = e.target;
    if (form.method.toLowerCase() === 'get') return;
    const btn = e.submitter || form.querySelector('[type="submit"]');
    if (!btn) return;
    btn.style.setProperty('--spin-color', getComputedStyle(btn).color);
    btn.classList.add('is-loading');
    btn.setAttribute('aria-busy', 'true');
    setTimeout(() => { btn.disabled = true; }, 0);  // after the browser has collected form data
    haptic();
  });
  window.addEventListener('pageshow', (e) => {  // back/forward cache restore
    if (!e.persisted) return;
    $$('.is-loading').forEach(b => { b.classList.remove('is-loading'); b.disabled = false; b.removeAttribute('aria-busy'); });
    $$('form[data-confirmed]').forEach(f => delete f.dataset.confirmed);
  });

  /* =====================================================================
     In-page views (dashboard: #today / #insights / #coach)
     ===================================================================== */
  const viewHooks = {};
  const initialised = new Set();
  App.onView = (name, fn) => {
    (viewHooks[name] = viewHooks[name] || []).push(fn);
    if (root.dataset.viewActive === name && document.querySelector(`[data-view="${name}"]`)) run(name);
  };
  function run(name) {
    if (initialised.has(name)) return;
    initialised.add(name);
    (viewHooks[name] || []).forEach(fn => fn());
  }
  const hasViews = () => !!document.querySelector('[data-view]');
  const viewNames = () => $$('[data-view]').map(v => v.dataset.view);

  function showView(name, { scroll = true } = {}) {
    if (!viewNames().includes(name)) name = 'today';
    root.dataset.viewActive = name;
    $$('[data-tab]').forEach(t => {
      if (t.dataset.tab === name) t.setAttribute('aria-current', 'page');
      else if (['today', 'insights', 'coach'].includes(t.dataset.tab)) t.removeAttribute('aria-current');
    });
    const title = document.querySelector(`[data-view="${name}"]`)?.dataset.viewTitle;
    if (title) $$('[data-view-title-target]').forEach(el => { el.textContent = title; });
    if (scroll) window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
    run(name);
  }

  document.addEventListener('click', (e) => {
    const link = e.target.closest('a[data-tab]');
    if (!link || !hasViews() || e.metaKey || e.ctrlKey || e.shiftKey) return;
    const name = link.dataset.tab;
    if (!viewNames().includes(name)) return;
    e.preventDefault();
    haptic(6);
    if (location.hash.slice(1) !== name) history.pushState(null, '', name === 'today' ? location.pathname + location.search : '#' + name);
    showView(name);
  });
  window.addEventListener('popstate', () => { if (hasViews()) showView(location.hash.slice(1) || 'today'); });

  /* =====================================================================
     Segmented control: [data-segments] > button[data-segment="x"] + [data-segment-panel="x"]
     ===================================================================== */
  const segKey = () => 'seg:' + location.pathname;
  function selectSegment(group, name, push) {
    const scope = group.closest('[data-segments-scope]') || document;
    $$('[data-segment]', group).forEach(b => b.setAttribute('aria-selected', String(b.dataset.segment === name)));
    $$('[data-segment-panel]', scope).forEach(p => { p.hidden = p.dataset.segmentPanel !== name; });
    if (push) {
      history.replaceState(null, '', '#' + name);
      // Remember the tab so a POST → redirect back to this page reopens it.
      try { sessionStorage.setItem(segKey(), name); } catch (_) {}
    }
  }
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-segments] [data-segment]');
    if (!b) return;
    haptic(6);
    selectSegment(b.closest('[data-segments]'), b.dataset.segment, true);
  });
  function setupSegments() {
    $$('[data-segments]').forEach(group => {
      const names = $$('[data-segment]', group).map(b => b.dataset.segment);
      let saved = null;
      try { saved = sessionStorage.getItem(segKey()); } catch (_) {}
      const wanted = [location.hash.slice(1), saved].find(n => names.includes(n));
      selectSegment(group, wanted || names[0], false);
    });
  }

  /* =====================================================================
     Swipe-to-reveal rows: .swipe-row > .swipe-actions + .swipe-content
     ===================================================================== */
  function closeSwipeRows(except) {
    $$('.swipe-row.is-open').forEach(r => { if (r !== except) { r.classList.remove('is-open'); r.querySelector('.swipe-content').style.transform = ''; } });
  }
  document.addEventListener('touchstart', (e) => {
    const content = e.target.closest('.swipe-content');
    if (!content) { if (!e.target.closest('.swipe-actions')) closeSwipeRows(); return; }
    const row = content.closest('.swipe-row');
    const width = 88;
    const x0 = e.touches[0].clientX, y0 = e.touches[0].clientY;
    const base = row.classList.contains('is-open') ? -width : 0;
    let dx = 0, decided = false, horizontal = false;
    closeSwipeRows(row);
    const move = (ev) => {
      const mx = ev.touches[0].clientX - x0, my = ev.touches[0].clientY - y0;
      if (!decided) {
        if (Math.abs(mx) < 6 && Math.abs(my) < 6) return;
        decided = true; horizontal = Math.abs(mx) > Math.abs(my);
        if (horizontal) row.classList.add('is-dragging');
      }
      if (!horizontal) return;
      dx = Math.min(0, Math.max(-width * 1.4, base + mx));
      content.style.transform = `translateX(${dx}px)`;
    };
    const end = () => {
      content.removeEventListener('touchmove', move);
      content.removeEventListener('touchend', end);
      content.removeEventListener('touchcancel', end);
      row.classList.remove('is-dragging');
      if (!horizontal) return;
      const open = dx < -width / 2;
      row.classList.toggle('is-open', open);
      content.style.transform = open ? `translateX(-${width}px)` : '';
      if (open) haptic(8);
    };
    content.addEventListener('touchmove', move, { passive: true });
    content.addEventListener('touchend', end);
    content.addEventListener('touchcancel', end);
  }, { passive: true });

  /* =====================================================================
     Collapsing large title → compact title in the app bar
     ===================================================================== */
  function setupLargeTitle() {
    const large = document.querySelector('[data-large-title]');
    const bar = document.querySelector('.app-bar');
    if (!large || !bar || !('IntersectionObserver' in window)) return;
    const offset = bar.offsetHeight;
    new IntersectionObserver(([entry]) => {
      bar.classList.toggle('is-collapsed', !entry.isIntersecting);
    }, { rootMargin: `-${offset}px 0px 0px 0px`, threshold: 0 }).observe(large);
  }

  /* =====================================================================
     Horizontal swipe navigation: <el data-swipe-prev="url" data-swipe-next="url">
     ===================================================================== */
  function setupSwipeNav() {
    $$('[data-swipe-prev], [data-swipe-next]').forEach(el => {
      let x0 = null, y0 = null;
      el.addEventListener('touchstart', (e) => { x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; }, { passive: true });
      el.addEventListener('touchend', (e) => {
        if (x0 == null) return;
        const dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0;
        x0 = null;
        if (Math.abs(dx) < 60 || Math.abs(dy) > Math.abs(dx)) return;
        const url = dx > 0 ? el.dataset.swipePrev : el.dataset.swipeNext;
        if (url) { haptic(6); location.href = url; }
      });
    });
  }

  /* =====================================================================
     Install (PWA)
     ===================================================================== */
  let deferredPrompt = null;
  const isStandalone = () => matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  const isIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} },
  };

  function revealInstall() {
    if (isStandalone()) return;
    $$('[data-install]').forEach(el => {
      if (el.hasAttribute('data-install-banner') && store.get('installDismissed') === '1') return;
      el.hidden = false;
    });
  }
  window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); deferredPrompt = e; revealInstall(); });
  window.addEventListener('appinstalled', () => { $$('[data-install]').forEach(el => { el.hidden = true; }); });

  document.addEventListener('click', async (e) => {
    if (e.target.closest('[data-install-dismiss]')) {
      store.set('installDismissed', '1');
      e.target.closest('[data-install-banner]').hidden = true;
      return;
    }
    const btn = e.target.closest('[data-install-action]');
    if (!btn) return;
    e.preventDefault();
    if (deferredPrompt) {
      deferredPrompt.prompt();
      await deferredPrompt.userChoice.catch(() => null);
      deferredPrompt = null;
    } else {
      App.openSheet('installSheet', btn);
    }
  });

  /* =====================================================================
     Toasts
     ===================================================================== */
  const DURATION = { success: 4500, info: 5000, error: 8000 };
  function dismissToast(toast) {
    if (!toast || toast.classList.contains('is-leaving')) return;
    toast.classList.add('is-leaving');
    toast.addEventListener('animationend', () => toast.remove(), { once: true });
    setTimeout(() => toast.remove(), 300);
  }
  function setupToast(toast) {
    const kind = toast.dataset.kind || 'info';
    const total = DURATION[kind] || DURATION.info;
    const bar = toast.querySelector('.toast-progress');
    let remaining = total, started = Date.now(), timer = null;
    const start = () => { started = Date.now(); timer = setTimeout(() => dismissToast(toast), remaining); if (bar) bar.style.animationPlayState = 'running'; };
    const pause = () => { clearTimeout(timer); remaining -= Date.now() - started; if (bar) bar.style.animationPlayState = 'paused'; };
    if (bar) bar.style.animation = `toast-progress ${total}ms linear forwards`;
    toast.querySelector('[data-toast-close]')?.addEventListener('click', () => dismissToast(toast));
    toast.addEventListener('mouseenter', pause);
    toast.addEventListener('mouseleave', start);
    toast.addEventListener('focusin', pause);
    toast.addEventListener('focusout', start);
    // swipe up / sideways to dismiss
    let sx = 0, sy = 0;
    toast.addEventListener('touchstart', (e) => { sx = e.touches[0].clientX; sy = e.touches[0].clientY; pause(); }, { passive: true });
    toast.addEventListener('touchend', (e) => {
      const dx = e.changedTouches[0].clientX - sx, dy = e.changedTouches[0].clientY - sy;
      if (dy < -30 || Math.abs(dx) > 60) dismissToast(toast); else start();
    });
    start();
  }
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || openSheetEl) return;
    const last = $$('.toast:not(.is-leaving)').pop();
    if (last && !document.querySelector('.fixed.inset-0:not(.hidden)')) dismissToast(last);
  });

  /* JSON POST with the Flask-WTF CSRF token (from <meta name="csrf-token">) */
  App.post = async function (url, data) {
    const token = document.querySelector('meta[name="csrf-token"]')?.content || '';
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'X-CSRFToken': token, 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(data || {}),
      credentials: 'same-origin',
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(body.error || `HTTP ${res.status}`), { status: res.status });
    return body;
  };

  /* Client-side toast (same look as server flash messages) */
  App.toast = function (message, kind = 'info') {
    let region = document.querySelector('.toast-region');
    if (!region) {
      region = document.createElement('div');
      region.className = 'toast-region';
      region.setAttribute('aria-live', 'polite');
      document.body.appendChild(region);
    }
    const icon = kind === 'error'
      ? 'M12 9v3.75m9-.75a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 3.75h.008v.008H12v-.008Z'
      : 'm11.25 11.25.041-.02a.75.75 0 0 1 1.063.852l-.708 2.836a.75.75 0 0 0 1.063.853l.041-.021M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9-3.75h.008v.008H12V8.25Z';
    const el = document.createElement('div');
    el.className = `toast toast--${kind}`;
    el.dataset.kind = kind;
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    el.innerHTML = `<span class="toast-icon"><svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="${icon}"/></svg></span>`
      + '<p class="flex-1 min-w-0 pt-1.5 text-[15px] sm:text-sm font-medium leading-snug"></p>'
      + '<button type="button" data-toast-close aria-label="Dismiss notification" class="-mr-1 -mt-0.5 shrink-0 inline-flex items-center justify-center w-9 h-9 rounded-full text-fg-subtle">✕</button>'
      + '<span class="toast-progress" aria-hidden="true"></span>';
    el.querySelector('p').textContent = message;
    region.appendChild(el);
    setupToast(el);
  };

  /* =====================================================================
     Offline support: service worker, offline pill, no POST while offline
     ===================================================================== */
  if ('serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
    });
  }

  function updateOnlineState() {
    let pill = document.getElementById('offlinePill');
    if (navigator.onLine) { if (pill) pill.hidden = true; return; }
    if (!pill) {
      pill = document.createElement('div');
      pill.id = 'offlinePill';
      pill.setAttribute('role', 'status');
      pill.className = 'fixed left-1/2 -translate-x-1/2 z-[58] inline-flex items-center gap-2 rounded-full bg-fg text-canvas px-4 h-9 text-[13px] font-semibold shadow-float';
      pill.style.top = 'calc(var(--safe-top) + 56px)';
      pill.innerHTML = '<span class="h-2 w-2 rounded-full bg-amber-400" aria-hidden="true"></span>Offline — showing saved data';
      document.body.appendChild(pill);
    }
    pill.hidden = false;
  }
  window.addEventListener('online', updateOnlineState);
  window.addEventListener('offline', updateOnlineState);

  // Capture phase: stops the submit before the spinner handler (the confirm handler skips when offline).
  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (navigator.onLine || form.method.toLowerCase() === 'get') return;
    e.preventDefault();
    e.stopImmediatePropagation();
    App.toast("You're offline. Reconnect to save your changes.", 'error');
  }, true);

  /* =====================================================================
     Boot
     ===================================================================== */
  document.addEventListener('DOMContentLoaded', () => {
    $$('.toast').forEach(setupToast);
    syncThemeControls();
    updateOnlineState();
    setupSegments();
    setupLargeTitle();
    setupSwipeNav();
    if (isIOS() && !isStandalone()) revealInstall();
    if (hasViews()) showView(root.dataset.viewActive || 'today', { scroll: false });
  });
})();
