/* Log Food screen: product picker (recent / favorites / my foods / search incl. Open Food Facts),
   portion stepper with live macros, barcode scanner, food photo recognition. Posts the unchanged form contract:
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
  const meta = (p) => App.t('{kcal} kcal · P {p} · F {f} · C {c}', { kcal: Math.round(p.kcal), p: fmt(p.p), f: fmt(p.f), c: fmt(p.c) })
    + ' · ' + (p.portion ? App.t('last {g} g', { g: Math.round(p.portion) }) : App.t('per 100 g'));

  /* Nutri-Score chip (.grade-* in app.css); a dashed outline marks a model prediction. */
  function paintGrade(el, p) {
    el.hidden = !p.grade;
    if (!p.grade) return;
    const predicted = p.grade_src === 'model';
    el.className = `grade-chip grade-${p.grade}${predicted ? ' grade-model' : ''}`;
    el.textContent = p.grade;
    el.setAttribute('role', 'img');
    const label = App.t(predicted ? 'Nutri-Score {g} (predicted by Kolos)' : 'Nutri-Score {g}', { g: p.grade.toUpperCase() });
    el.setAttribute('aria-label', label);
    el.title = label;
  }

  /* ---------------------------------------------------------------- rendering */
  function row(p) {
    const el = tpl.content.firstElementChild.cloneNode(true);
    el.dataset.id = p.id;
    el.querySelector('[data-name]').textContent = p.label;
    el.querySelector('[data-meta]').textContent = meta(p);
    paintGrade(el.querySelector('[data-grade]'), p);
    const pick = el.querySelector('[data-pick]');
    pick.setAttribute('aria-checked', String(selected && selected.id === p.id));
    if (selected && selected.id === p.id) {
      const check = el.querySelector('[data-check]');
      check.classList.add('bg-primary', 'border-primary', 'text-on-primary');
    }
    const star = el.querySelector('[data-star]');
    star.setAttribute('aria-pressed', String(!!p.favorite));
    star.setAttribute('aria-label', App.t(p.favorite ? 'Remove {name} from favorites' : 'Add {name} to favorites', { name: p.label }));
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
      App.t('Foods you log will show up here with your usual portion. Search or scan to get started.'));
    renderList(tabsScope.querySelector('[data-list="favorites"]'), lists.favorites,
      App.t('Tap the star next to a food to keep it here.'));
    renderList(tabsScope.querySelector('[data-list="mine"]'), lists.mine,
      App.t('Foods you create appear here. Use “Create food” for home recipes.'));
  }

  function renderSelected() {
    hiddenId.value = selected ? selected.id : '';
    selectedCard.hidden = !selected;
    if (selected) {
      paintGrade(document.getElementById('selectedGrade'), selected);
      document.getElementById('selectedName').textContent = selected.label;
      document.getElementById('selectedMeta').textContent =
        App.t('{kcal} kcal · P {p} · F {f} · C {c}', { kcal: Math.round(selected.kcal), p: fmt(selected.p), f: fmt(selected.f), c: fmt(selected.c) })
        + ' · ' + App.t('per 100 g');
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
        App.toast(err.message || App.t('Could not update favorites.'), 'error');
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
    renderList(box1, local, navigator.onLine ? App.t('No matches in your foods yet…') : App.t('No matches. You are offline, so online search is unavailable.'));
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
    status.textContent = App.t('Searching Open Food Facts…');
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
        none.textContent = App.t('Nothing found. Try another name, scan the barcode, or create the food.');
        results.appendChild(none);
      }
    } catch (_) {
      if (seq === remoteSeq) status.textContent = App.t('Online search is unavailable right now.');
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
      App.toast(App.t('Choose a food first.'), 'error');
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
    scanStatus.textContent = App.t('Looking up {code}…', { code });
    try {
      const res = await fetch(data.urls.barcode.replace('CODE', encodeURIComponent(code)), { headers: { Accept: 'application/json' } });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        stopScanner();
        App.closeSheet('scanSheet');
        select(body.product);
        App.toast(App.t('Found: {name}', { name: body.product.label }), 'success');
      } else if (res.status === 404) {
        lastCode = code;
        scanStatus.textContent = App.t('Barcode {code}', { code });
        notFound.hidden = false;
      } else {
        scanStatus.textContent = body.error || App.t('Could not look up this barcode.');  // e.g. 422 for RU/BY barcodes
      }
    } catch (_) {
      scanStatus.textContent = navigator.onLine ? App.t('Could not reach the food database.') : App.t('You are offline — try again when connected.');
    } finally {
      busy = false;
    }
  }

  async function startScanner() {
    notFound.hidden = true;
    lastCode = null;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      scanStatus.textContent = App.t('Camera needs a secure (HTTPS) connection. Type the number below instead.');
      return;
    }
    scanStatus.textContent = App.t('Starting camera…');
    try {
      if ('BarcodeDetector' in window) {
        const detector = new BarcodeDetector({ formats: ['ean_13', 'ean_8', 'upc_a', 'upc_e'] });
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false });
        video.srcObject = stream;
        await video.play();
        scanStatus.textContent = App.t('Point the camera at a barcode.');
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
        scanStatus.textContent = App.t('Point the camera at a barcode.');
        await zxing.decodeFromConstraints({ video: { facingMode: 'environment' } }, video, (result) => {
          if (result && !busy) { navigator.vibrate?.(15); lookup(result.getText()); }
        });
      }
    } catch (err) {
      stopScanner();
      scanStatus.textContent = err && err.name === 'NotAllowedError'
        ? App.t('Camera access was denied. Type the barcode number below instead.')
        : App.t('Camera is not available. Type the barcode number below instead.');
    }
  }

  document.querySelectorAll('[data-scan-open]').forEach(b => b.addEventListener('click', startScanner));
  document.getElementById('scanSheet').addEventListener('sheet:closed', stopScanner);
  document.getElementById('manualBarcode').addEventListener('submit', (e) => {
    e.preventDefault();
    e.stopImmediatePropagation();  // not a server form: skip the global spinner
    const code = document.getElementById('manualCode').value.replace(/\D/g, '');
    if (code.length < 8) { scanStatus.textContent = App.t('A barcode has 8 to 14 digits.'); return; }
    lookup(code);
  });
  document.getElementById('scanCreate').addEventListener('click', () => {
    stopScanner();
    App.closeSheet('scanSheet', true);
    document.getElementById('c-barcode').value = lastCode || '';
    App.openSheet('createSheet');
  });

  /* ---------------------------------------------------------------- food photo */
  const photoInput = document.getElementById('photoInput');
  const photoPreview = document.getElementById('photoPreview');
  const photoStatus = document.getElementById('photoStatus');
  const photoResults = document.getElementById('photoResults');
  const photoList = document.getElementById('photoList');
  const PHOTO_EDGE = 640;  // the model needs far less; keeps uploads small on mobile data

  /* Downscale on the device and re-encode as JPEG (this also drops EXIF location data). */
  async function shrink(file) {
    const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
    const k = Math.min(1, PHOTO_EDGE / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(bitmap.width * k);
    canvas.height = Math.round(bitmap.height * k);
    canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    if (bitmap.close) bitmap.close();
    return new Promise((resolve, reject) => canvas.toBlob(b => (b ? resolve(b) : reject(new Error())), 'image/jpeg', 0.85));
  }

  function candidateRow(c) {
    const item = document.createElement('div');
    item.setAttribute('role', 'listitem');
    const pct = Math.round(c.confidence * 100);
    const btn = document.createElement(c.product ? 'button' : 'div');
    btn.className = 'flex w-full items-center gap-3 px-4 min-h-[60px] py-2 text-left' + (c.product ? ' press' : '');
    if (c.product) btn.type = 'button';
    const grade = document.createElement('span');
    const text = document.createElement('span');
    text.className = 'min-w-0 flex-1';
    const name = document.createElement('span');
    name.className = 'block truncate text-[15px] font-medium';
    name.textContent = c.product ? c.product.label : c.label;
    const sub = document.createElement('span');
    sub.className = 'block text-[13px] text-fg-muted';
    sub.textContent = c.product ? meta(c.product) : App.t('Not in the food list yet. Try search.');
    text.append(name, sub);
    const conf = document.createElement('span');
    conf.className = 'flex w-16 shrink-0 flex-col items-end gap-1';
    conf.innerHTML = '<span class="text-[13px] font-semibold tabular-nums"></span>'
      + '<span class="h-1.5 w-full overflow-hidden rounded-full bg-surface-2"><span class="block h-full rounded-full bg-violet-500"></span></span>';
    conf.firstChild.textContent = `${pct}%`;
    conf.lastChild.firstChild.style.width = `${pct}%`;
    conf.setAttribute('aria-label', App.t('Confidence {n}%', { n: pct }));
    if (c.product) paintGrade(grade, c.product); else grade.hidden = true;
    btn.append(grade, text, conf);
    if (c.product) {
      btn.addEventListener('click', () => {
        App.closeSheet('photoSheet');
        select(c.product);
        App.toast(App.t('Selected: {name}', { name: c.product.label }), 'success');
      });
    }
    item.appendChild(btn);
    return item;
  }

  async function recognise(file) {
    photoResults.hidden = true;
    if (photoPreview.src) URL.revokeObjectURL(photoPreview.src);
    photoPreview.src = URL.createObjectURL(file);
    photoPreview.hidden = false;
    photoStatus.textContent = App.t('Recognising…');
    try {
      const body = new FormData();
      body.append('photo', await shrink(file), 'photo.jpg');
      const token = document.querySelector('meta[name="csrf-token"]')?.content || '';
      const res = await fetch(data.urls.photo, {
        method: 'POST', body, credentials: 'same-origin',
        headers: { 'X-CSRFToken': token, Accept: 'application/json' },
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(json.error || App.t('Could not recognise this photo.'));
      const candidates = (json.candidates || []).map(c => ({ ...c, product: c.product && remember(c.product) }));
      if (!candidates.length) { photoStatus.textContent = App.t('No food found on this photo. Try another angle.'); return; }
      photoStatus.textContent = '';
      photoList.replaceChildren(...candidates.map(candidateRow));
      photoResults.hidden = false;
    } catch (err) {
      photoStatus.textContent = navigator.onLine
        ? (err.message || App.t('Could not recognise this photo.'))
        : App.t('You are offline — try again when connected.');
    }
  }

  photoInput.addEventListener('change', () => {
    const file = photoInput.files && photoInput.files[0];
    if (file) recognise(file);
    photoInput.value = '';  // choosing the same photo again still fires change
  });
  document.getElementById('photoSheet').addEventListener('sheet:closed', () => {
    if (photoPreview.src) URL.revokeObjectURL(photoPreview.src);
    photoPreview.removeAttribute('src');
    photoPreview.hidden = true;
    photoResults.hidden = true;
    photoStatus.textContent = '';
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
