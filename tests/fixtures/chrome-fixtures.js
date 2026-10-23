// Generator for the chrome-124-*-q85.jpg fixtures (change 11d): evaluated in headless
// zenika/alpine-chrome:124 (HeadlessChrome/124.0.6367.78) over the DevTools protocol
// (Runtime.evaluate with awaitPromise). The fixtures are the 'photo-0.85' and
// 'detailed-0.85' blobs, base64-decoded. The firefox-131-*-q85.jpg fixtures are the same
// generator run in Firefox 131 (Playwright 1.48.0 image, page.evaluate). Not part of the app or the test run.
(async () => {
  function rng(seed) { let s = seed >>> 0; return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; }; }
  function canvas() { const c = document.createElement('canvas'); c.width = c.height = 256; return c; }
  // Photo-like: a smooth gradient, mild grain and a soft shape.
  function photo() {
    const c = canvas(), x = c.getContext('2d');
    x.fillStyle = '#fff'; x.fillRect(0, 0, 256, 256);
    const g = x.createRadialGradient(100, 90, 10, 128, 128, 200);
    g.addColorStop(0, '#f4c49a'); g.addColorStop(0.5, '#a0623a'); g.addColorStop(1, '#283850');
    x.fillStyle = g; x.fillRect(0, 0, 256, 256);
    const r = rng(7), img = x.getImageData(0, 0, 256, 256), px = img.data;
    for (let i = 0; i < px.length; i += 4) { const n = (r() - 0.5) * 24; px[i] += n; px[i + 1] += n; px[i + 2] += n; }
    x.putImageData(img, 0, 0);
    x.fillStyle = 'rgba(30,30,40,0.6)'; x.beginPath(); x.arc(128, 150, 60, 0, Math.PI * 2); x.fill();
    return c;
  }
  // Detailed: a busy scene (many shapes and fine texture), a hard case for size.
  function detailed() {
    const c = canvas(), x = c.getContext('2d'), r = rng(42);
    x.fillStyle = '#fff'; x.fillRect(0, 0, 256, 256);
    for (let i = 0; i < 600; i++) {
      x.fillStyle = `hsl(${Math.floor(r() * 360)}, ${40 + Math.floor(r() * 50)}%, ${20 + Math.floor(r() * 60)}%)`;
      x.beginPath(); x.arc(r() * 256, r() * 256, 2 + r() * 14, 0, Math.PI * 2); x.fill();
    }
    const img = x.getImageData(0, 0, 256, 256), px = img.data;
    for (let i = 0; i < px.length; i += 4) { const n = (r() - 0.5) * 60; px[i] += n; px[i + 1] += n; px[i + 2] += n; }
    x.putImageData(img, 0, 0);
    return c;
  }
  // Worst case: pure per-pixel noise.
  function noise() {
    const c = canvas(), x = c.getContext('2d'), r = rng(99);
    const img = x.getImageData(0, 0, 256, 256), px = img.data;
    for (let i = 0; i < px.length; i += 4) { px[i] = r() * 255; px[i + 1] = r() * 255; px[i + 2] = r() * 255; px[i + 3] = 255; }
    x.putImageData(img, 0, 0);
    return c;
  }
  function blob(c, q) {
    return new Promise(res => c.toBlob(b => {
      const fr = new FileReader();
      fr.onload = () => res({ q, type: b.type, size: b.size, b64: fr.result.split(',')[1] });
      fr.readAsDataURL(b);
    }, 'image/jpeg', q));
  }
  const out = { ua: navigator.userAgent };
  const sources = { photo: photo(), detailed: detailed(), noise: noise() };
  for (const [name, c] of Object.entries(sources)) {
    for (const q of [0.85, 0.75, 0.65, 0.55]) out[name + '-' + q] = await blob(c, q);
  }
  return out;
})()
