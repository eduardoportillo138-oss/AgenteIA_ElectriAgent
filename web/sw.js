// Service worker de ElectriAgent: guarda la interfaz para abrirla sin conexión.
// Los datos (/api) siempre se piden al servidor: un análisis nunca sale de caché.
const CACHE = "electriagent-v1";
const INTERFAZ = ["./", "index.html", "estilos.css", "app.js", "manifest.webmanifest",
                  "icono-192.png", "icono-512.png", "favicon.png", "apple-touch-icon.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(INTERFAZ)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  // Red primero; si no hay conexión, la copia guardada.
  e.respondWith(
    fetch(e.request)
      .then((r) => { const copia = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copia)); return r; })
      .catch(() => caches.match(e.request))
  );
});
