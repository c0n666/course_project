/* Log Food screen: product picker (recent / favorites / my foods / search incl. Open Food Facts),
   portion stepper with live macros, barcode scanner. Posts the unchanged form contract:
   product_id, meal_type, portion_grams, date. */
(function () {
  'use strict';

  const data = JSON.parse(document.getElementById('pickerData').textContent);
  const form = document.getElementById('logFoodForm');
  const hiddenId = document.getElementById('product_id');
  const portion = document.getElementById('portion_grams');
  const tpl = document.getElementById('productRowTpl');
  const search = document.getElementById('productSearch');
  const results = document.getElementById('searchResults');
  const tabsScope = document.querySelector('#picker [data-segments-scope]');
  const selectedCard = document.getElementById('selectedCard');

  /* One registry of every product we know about, so favorites/selection stay in sync. */
  const byId = new Map();
  const remember = (p) => { const known = byId.get(p.id); if (known) { Object.assign(known, p); return known; } byId.set(p.id, p); return p; };
  const lists = {
    recent: data.recent.map(remember),
    favorites: data.favorites.map(remember),
    mine: data.mine.map(remember),
  };
  const catalogue = data.catalogue.map(remember);
  let selected = data.preselected ? remember(data.preselected) : null;

  const fmt = (n) => (Math.round(n * 10) / 10).toString();
  const meta = (p) => `${Math.round(p.kcal)} kcal · P ${fmt(p.p)} · F ${fmt(p.f)} · C ${fmt(p.c)}`
    + (p.portion ? ` · last ${Math.round(p.portion)} g` : ' / 100 g');

  /* ---------------------------------------------------------------- rendering */
  function row(p) {
    const el = tpl.content.firstElementChild.cloneNode(true);
    el.dataset.id = p.id;
    el.querySelector('[data-name]').textContent = p.label;
    el.querySelector('[data-meta]').textContent = meta(p);
    const pick = el.querySelector('[data-pick]');
    pick.setAttribute('aria-checked', String(selected && selected.id === p.id));
    if (selected && selected.id === p.id) {
      const check = el.querySelector('[data-check]');
      check.classList.add('bg-primary', 'border-primary', 'text-on-primary');
    }
    const star = el.querySelector('[data-star]');
    star.setAttribute('aria-pressed', String(!!p.favorite));
    star.setAttribute('aria-label', (p.favorite ? 'Remove ' : 'Add ') + p.label + (p.favorite ? ' from favorites' : ' to favorites'));
    star.querySelector('svg').setAttribute('fill', p.favorite ? 'currentColor' : 'none');
    return el;
  }

  function renderList(container, items, emptyText, heading) {
    container.replaceChildren();
    if (heading) {
      const h = document.createElement('p');
      h.className = 'px-1 mt-4 mb-2 text-[13px] font-semibold uppercase tracking-wide text-fg-muted';
      h.textContent = heading;
      container.appendChild(h);
    }
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'rounded-[20px] bg-surface shadow-card px-6 py-8 text-center text-sm text-fg-muted';
      empty.textContent = emptyText;
      container.appendChild(empty);
      return;
    }
    const list = document.createElement('div');
    list.className = 'bg-surface rounded-[20px] shadow-card divide-y divide-line overflow-hidden';
    list.setAttribute('role', 'radiogroup');
    items.forEach(p => list.appendChild(row(p)));
    container.appendChild(list);
  }

  function renderTabs() {
    renderList(tabsScope.querySelector('[data-list="recent"]'), lists.recent,
      'Foods you log will show up here with your usual portion. Search or scan to get started.');
    renderList(tabsScope.querySelector('[data-list="favorites"]'), lists.favorites,
      'Tap the star next to a food to keep it here.');
    renderList(tabsScope.querySelector('[data-list="mine"]'), lists.mine,
      'Foods you create appear here. Use “Create food” for home recipes.');
  }

  function renderSelected() {
    hiddenId.value = selected ? selected.id : '';
    selectedCard.hidden = !selected;
    if (selected) {
      document.getElementById('selectedName').textContent = selected.label;
      document.getElementById('selectedMeta').textContent =
        `${Math.round(selected.kcal)} kcal · P ${fmt(selected.p)} · F ${fmt(selected.f)} · C ${fmt(selected.c)} / 100 g`;
    }
    updatePreview();
  }

  function refresh() {
    renderTabs();
    if (!results.hidden) runLocalSearch();
    renderSelected();
  }

  /* ---------------------------------------------------------------- selection */
  function select(p, { scroll = true } = {}) {
    selected = remember(p);
    if (p.portion) portion.value = Math.round(p.portion);
    search.value = '';
    showSearch(false);
    refresh();
    if (scroll) document.getElementById('portionBlock').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  App.selectProduct = select;

  document.getElementById('picker').addEventListener('click', async (e) => {
    const rowEl = e.target.closest('.product-row');
    if (!rowEl) return;
    const p = byId.get(Number(rowEl.dataset.id));
    if (!p) return;
    if (e.target.closest('[data-pick]')) { select(p); return; }
    if (e.target.closest('[data-star]')) {
      try {
        const res = await App.post(data.urls.favorite.replace('/0/', `/${p.id}/`));
        p.favorite = res.favorite;
        lists.favorites = res.favorite ? [p, ...lists.favorites.filter(x => x.id !== p.id)] : lists.favorites.filter(x => x.id !== p.id);
        refresh();
      } catch (err) {
        App.toast(err.message || 'Could not update favorites.', 'error');
      }
    }
  });

  document.getElementById('clearSelected').addEventListener('click', () => {
    selected = null;
    renderSelected();
    refresh();
    search.focus();
  });

  /* ---------------------------------------------------------------- search */
  let remoteTimer = null;
  let remoteSeq = 0;

  function showSearch(on) {
    results.hidden = !on;
    tabsScope.hidden = on;
  }

  function localMatches(q) {
    const seen = new Set();
    const out = [];
    for (const p of [...lists.favorites, ...lists.recent, ...lists.mine, ...catalogue]) {
      if (seen.has(p.id)) continue;
      const hay = p.terms || `${p.name} ${p.brand || ''}`.toLowerCase();  // includes Ukrainian names
      if (hay.includes(q)) { seen.add(p.id); out.push(p); }
    }
    return out.sort((a, b) => a.name.length - b.name.length).slice(0, 30);
  }

  let lastRemote = [];
  let lastRemoteQuery = '';
  function runLocalSearch() {
    const q = search.value.trim().toLowerCase();
    if (!q) { showSearch(false); return []; }
    showSearch(true);
    const local = localMatches(q);
    const localIds = new Set(local.map(p => p.id));
    // OFF also matches categories, so trust its hits for the exact query; filter them while typing on.
    const extra = lastRemote.filter(p => !localIds.has(p.id) && (q === lastRemoteQuery || (p.terms || '').includes(q)));
    results.replaceChildren();
    const box1 = document.createElement('div');
    renderList(box1, local, navigator.onLine ? 'No matches in your foods yet…' : 'No matches. You are offline, so online search is unavailable.');
    results.appendChild(box1);
    if (extra.length) {
      const box2 = document.createElement('div');
      renderList(box2, extra, '', 'Open Food Facts');
      results.appendChild(box2);
    }
    return local;
  }

  async function runRemoteSearch(q, seq) {
    const status = document.createElement('p');
    status.className = 'mt-3 px-1 text-sm text-fg-muted';
    status.textContent = 'Searching Open Food Facts…';
    results.appendChild(status);
    try {
      const res = await fetch(`${data.urls.search}?q=${encodeURIComponent(q)}`, { headers: { Accept: 'application/json' } });
      if (!res.ok) throw new Error();
      const body = await res.json();
      if (seq !== remoteSeq) return;  // a newer query is running
      lastRemote = body.results.map(remember);
      lastRemoteQuery = q;
      runLocalSearch();
      if (!results.querySelector('.product-row')) {
        const none = document.createElement('p');
        none.className = 'mt-3 px-1 text-sm text-fg-muted';
        none.textContent = 'Nothing found. Try another name, scan the barcode, or create the food.';
        results.appendChild(none);
      }
    } catch (_) {
      if (seq === remoteSeq) status.textContent = 'Online search is unavailable right now.';
    }
  }

  search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    const local = runLocalSearch();
    clearTimeout(remoteTimer);
    const seq = ++remoteSeq;
    if (q.length >= 3 && local.length < 8 && navigator.onLine) {
      remoteTimer = setTimeout(() => runRemoteSearch(q, seq), 450);
    }
  });
  search.addEventListener('keydown', (e) => { if (e.key === 'Enter') e.preventDefault(); });

  /* ---------------------------------------------------------------- portion & preview */
  function updatePreview() {
    const g = parseFloat(portion.value) || 0;
    document.querySelectorAll('[data-portion]').forEach(b => b.setAttribute('aria-pressed', String(parseFloat(b.dataset.portion) === g)));
    const box = document.getElementById('macros');
    if (!selected) { box.hidden = true; return; }
    box.hidden = false;
    const k = g / 100;
    document.getElementById('m-p').textContent = fmt(selected.p * k);
    document.getElementById('m-f').textContent = fmt(selected.f * k);
    document.getElementById('m-c').textContent = fmt(selected.c * k);
    document.getElementById('est-cal').textContent = g > 0 ? `≈ ${Math.round(selected.kcal * k)} kcal` : '';
  }
  portion.addEventListener('input', updatePreview);
  document.querySelectorAll('[data-portion]').forEach(b => b.addEventListener('click', () => { portion.value = b.dataset.portion; updatePreview(); }));
  document.querySelectorAll('[data-step]').forEach(b => b.addEventListener('click', () => {
    portion.value = Math.max(1, (parseFloat(portion.value) || 0) + parseFloat(b.dataset.step));
    updatePreview();
  }));

  /* The hidden product_id cannot carry `required`, so validate before submit. */
  form.addEventListener('submit', (e) => {
    if (!hiddenId.value) {
      e.preventDefault();
      e.stopImmediatePropagation();
      App.toast('Choose a food first.', 'error');
      document.getElementById('picker').scrollIntoView({ behavior: 'smooth', block: 'start' });
      search.focus({ preventScroll: true });
    } else if (!(parseFloat(portion.value) > 0)) {
      e.preventDefault();
      e.stopImmediatePropagation();
      portion.reportValidity();
    }
  });

  /* Pre-select the meal by time of day when logging for today */
  if (data.isToday) {
    const h = new Date().getHours();
    const guess = h < 11 ? 'breakfast' : h < 16 ? 'lunch' : h < 21 ? 'dinner' : 'snack';
    document.querySelectorAll(`input[name="meal_type"][value="${guess}"]`).forEach(r => { r.checked = true; });
  }

  /* ---------------------------------------------------------------- barcode scanner */
  const video = document.getElementById('scanVideo');
  const scanStatus = document.getElementById('scanStatus');
  const notFound = document.getElementById('scanNotFound');
  let stream = null, zxing = null, loopTimer = null, lastCode = null, busy = false;

  function stopScanner() {
    clearTimeout(loopTimer);
    if (zxing) { try { zxing.reset(); } catch (_) {} zxing = null; }
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
    video.srcObject = null;
  }

  function loadZXing() {
    if (window.ZXing) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/@zxing/library@0.21.3/umd/index.min.js';
      s.onload = resolve; s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  async function lookup(code) {
    if (busy) return;
    busy = true;
    notFound.hidden = true;
    scanStatus.textContent = `Looking up ${code}…`;
    try {
      const res = await fetch(data.urls.barcode.replace('CODE', encodeURIComponent(code)), { headers: { Accept: 'application/json' } });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        stopScanner();
        App.closeSheet('scanSheet');
        select(body.product);
        App.toast(`Found: ${body.product.label}`, 'success');
      } else if (res.status === 404) {
        lastCode = code;
        scanStatus.textContent = `Barcode ${code}`;
        notFound.hidden = false;
      } else {
        scanStatus.textContent = body.error || 'Could not look up this barcode.';  // e.g. 422 for RU/BY barcodes
      }
    } catch (_) {
      scanStatus.textContent = navigator.onLine ? 'Could not reach the food database.' : 'You are offline — try again when connected.';
    } finally {
      busy = false;
    }
  }

  async function startScanner() {
    notFound.hidden = true;
    lastCode = null;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      scanStatus.textContent = 'Camera needs a secure (HTTPS) connection. Type the number below instead.';
      return;
    }
    scanStatus.textContent = 'Starting camera…';
    try {
      if ('BarcodeDetector' in window) {
        const detector = new BarcodeDetector({ formats: ['ean_13', 'ean_8', 'upc_a', 'upc_e'] });
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false });
        video.srcObject = stream;
        await video.play();
        scanStatus.textContent = 'Point the camera at a barcode.';
        const tick = async () => {
          if (!stream) return;
          try {
            const codes = await detector.detect(video);
            if (codes.length && !busy) { navigator.vibrate?.(15); await lookup(codes[0].rawValue); }
          } catch (_) {}
          if (stream) loopTimer = setTimeout(tick, 250);
        };
        tick();
      } else {
        await loadZXing();
        zxing = new ZXing.BrowserMultiFormatReader();
        scanStatus.textContent = 'Point the camera at a barcode.';
        await zxing.decodeFromConstraints({ video: { facingMode: 'environment' } }, video, (result) => {
          if (result && !busy) { navigator.vibrate?.(15); lookup(result.getText()); }
        });
      }
    } catch (err) {
      stopScanner();
      scanStatus.textContent = err && err.name === 'NotAllowedError'
        ? 'Camera access was denied. Type the barcode number below instead.'
        : 'Camera is not available. Type the barcode number below instead.';
    }
  }

  document.querySelectorAll('[data-scan-open]').forEach(b => b.addEventListener('click', startScanner));
  document.getElementById('scanSheet').addEventListener('sheet:closed', stopScanner);
  document.getElementById('manualBarcode').addEventListener('submit', (e) => {
    e.preventDefault();
    e.stopImmediatePropagation();  // not a server form: skip the global spinner
    const code = document.getElementById('manualCode').value.replace(/\D/g, '');
    if (code.length < 8) { scanStatus.textContent = 'A barcode has 8 to 14 digits.'; return; }
    lookup(code);
  });
  document.getElementById('scanCreate').addEventListener('click', () => {
    stopScanner();
    App.closeSheet('scanSheet', true);
    document.getElementById('c-barcode').value = lastCode || '';
    App.openSheet('createSheet');
  });

  /* ---------------------------------------------------------------- boot */
  const params = new URLSearchParams(location.search);
  if (selected && selected.portion) portion.value = Math.round(selected.portion);
  const meal = params.get('meal') && document.querySelector(
    `#logFoodForm input[name="meal_type"][value="${CSS.escape(params.get('meal'))}"]`);
  if (meal) meal.checked = true;
  refresh();
  if (selected) document.getElementById('portionBlock').scrollIntoView({ block: 'center' });
  if (params.get('scan') === '1') {  // opened from the + sheet
    App.openSheet('scanSheet');
    startScanner();
  }
})();
