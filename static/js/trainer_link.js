/* Scan the trainer's QR code on the profile page and submit the code. */
(function () {
  const sheet = document.getElementById('trainerScanSheet');
  if (!sheet) return;
  const video = document.getElementById('trainerScanVideo');
  const status = document.getElementById('trainerScanStatus');
  const form = document.getElementById('trainerCodeForm');
  const input = document.getElementById('trainerCode');
  let stream = null, zxing = null, timer = null, done = false;

  function stop() {
    clearTimeout(timer); timer = null;
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

  function accept(text) {
    if (done || !text) return;
    done = true;
    navigator.vibrate?.(15);
    stop();
    input.value = text;          // the server extracts the code from a /join/<code> link too
    App.closeSheet('trainerScanSheet', true);
    form.submit();
  }

  async function start() {
    done = false;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      status.textContent = App.t('Camera needs a secure (HTTPS) connection. Type the code instead.');
      return;
    }
    status.textContent = App.t('Starting camera…');
    try {
      if ('BarcodeDetector' in window) {
        const detector = new BarcodeDetector({ formats: ['qr_code'] });
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false });
        video.srcObject = stream;
        await video.play();
        status.textContent = App.t('Point the camera at your trainer’s QR code.');
        const tick = async () => {
          if (!stream) return;
          try {
            const codes = await detector.detect(video);
            if (codes.length) return accept(codes[0].rawValue);
          } catch (_) {}
          timer = setTimeout(tick, 250);
        };
        tick();
      } else {
        await loadZXing();
        zxing = new ZXing.BrowserQRCodeReader();
        status.textContent = App.t('Point the camera at your trainer’s QR code.');
        await zxing.decodeFromConstraints({ video: { facingMode: 'environment' } }, video, (result) => {
          if (result) accept(result.getText());
        });
      }
    } catch (err) {
      stop();
      status.textContent = err && err.name === 'NotAllowedError'
        ? App.t('Camera access was denied. Type the code instead.')
        : App.t('Camera is not available. Type the code instead.');
    }
  }

  document.querySelectorAll('[data-trainer-scan-open]').forEach(b => b.addEventListener('click', start));
  sheet.addEventListener('sheet:closed', stop);
})();
