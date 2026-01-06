/* Hotel Villa Grace - Room Image Helper
   - Mantiene compatibilidad con:
     * img[data-room-img-url]
     * img[data-room-img]
   - NUEVO:
     * img[data-room-code]  -> RoomImage.hydrateByCode()
     * RoomImage.lookup(code)
*/
(function () {
  const DEFAULT_FALLBACK = "/static/assets/img/hotel/room-1.jpg";

  const _cache = {
    url: null,
    ts: 0,
    rooms: null,
    map: null,
  };

  function asStr(v) {
    return (v === undefined || v === null) ? "" : String(v).trim();
  }

  function normalize(url) {
    if (!url) return "";
    const s = String(url).trim();

    // ya es público
    if (s.startsWith("http://") || s.startsWith("https://") || s.startsWith("/")) return s;

    // si viene con "static/..." (relativo), lo convertimos a "/static/..."
    if (s.startsWith("static/")) return "/" + s;

    // si viene como "uploads/..." asumimos bajo /static
    if (s.startsWith("uploads/")) return "/static/" + s;

    // último intento: tratarlo como path estático relativo
    return "/static/" + s.replace(/^\/+/, "");
  }

  function resolve(roomOrUrl, fallback) {
    const fb = normalize(fallback || DEFAULT_FALLBACK);

    if (!roomOrUrl) return fb;

    // Si ya viene como URL string
    if (typeof roomOrUrl === "string") {
      const u = normalize(roomOrUrl);
      return u || fb;
    }

    // Si viene como objeto "room" (del endpoint /grr/habitaciones)
    const obj = roomOrUrl || {};
    const candidate =
      obj.img ||
      obj.Imagen_URL ||
      obj.imagen_url ||
      obj.image ||
      obj.image_url ||
      "";

    const u = normalize(candidate);
    return u || fb;
  }

  function set(imgEl, src, fallback) {
    if (!imgEl) return;

    const fb = normalize(fallback || imgEl.getAttribute("data-room-fallback") || imgEl.src || DEFAULT_FALLBACK);
    const finalSrc = normalize(src) || fb;

    // Evitar loop de error infinito
    imgEl.onerror = null;
    imgEl.onerror = () => {
      imgEl.onerror = null;
      imgEl.src = fb;
    };

    imgEl.src = finalSrc;
  }

  function hydrate(root) {
    const ctx = root || document;

    // 1) data-room-img-url: URL directa
    ctx.querySelectorAll("img[data-room-img-url]").forEach((img) => {
      const url = img.getAttribute("data-room-img-url") || "";
      const fb = img.getAttribute("data-room-fallback") || img.src || DEFAULT_FALLBACK;
      set(img, normalize(url), fb);
    });

    // 2) data-room-img: compat (algunas páginas lo usan)
    ctx.querySelectorAll("img[data-room-img]").forEach((img) => {
      const url = img.getAttribute("data-room-img") || "";
      const fb = img.getAttribute("data-room-fallback") || img.src || DEFAULT_FALLBACK;
      set(img, normalize(url), fb);
    });
  }

  async function fetchRooms(url) {
    const endpoint = url || "/grr/habitaciones";
    try {
      const r = await fetch(endpoint, { headers: { "Accept": "application/json" } });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j || !j.ok || !Array.isArray(j.rooms)) return [];
      return j.rooms;
    } catch {
      return [];
    }
  }

  function buildRoomMap(rooms) {
    const map = {};
    (rooms || []).forEach((room) => {
      const code =
        room.code ??
        room.Codigo_Habitacion ??
        room.roomCode ??
        room.id ??
        room.Codigo ??
        null;

      const key = asStr(code);
      if (key) map[key] = room;
    });
    return map;
  }

  async function getRoomsCached(url, ttlMs) {
    const endpoint = url || "/grr/habitaciones";
    const ttl = (ttlMs === undefined || ttlMs === null) ? 60000 : Number(ttlMs);

    const now = Date.now();
    if (_cache.rooms && _cache.map && _cache.url === endpoint && (now - _cache.ts) < ttl) {
      return { rooms: _cache.rooms, map: _cache.map };
    }

    const rooms = await fetchRooms(endpoint);
    const map = buildRoomMap(rooms);

    _cache.url = endpoint;
    _cache.ts = now;
    _cache.rooms = rooms;
    _cache.map = map;

    return { rooms, map };
  }

  async function lookup(code, options) {
    const roomCode = asStr(code);
    const url = (options && options.url) || "/grr/habitaciones";
    const ttlMs = (options && options.ttlMs) ?? 60000;
    const fallback = (options && options.fallback) || DEFAULT_FALLBACK;

    if (!roomCode) return normalize(fallback || DEFAULT_FALLBACK);

    const { map } = await getRoomsCached(url, ttlMs);
    const room = map[roomCode] || null;

    return resolve(room, fallback);
  }

  async function hydrateByCode(root, options) {
    const ctx = root || document;
    const imgs = Array.from(ctx.querySelectorAll("img[data-room-code]"));
    if (!imgs.length) return;

    const url = (options && options.url) || "/grr/habitaciones";
    const ttlMs = (options && options.ttlMs) ?? 60000;

    const { map } = await getRoomsCached(url, ttlMs);

    imgs.forEach((img) => {
      const code = asStr(img.getAttribute("data-room-code"));
      const fb = img.getAttribute("data-room-fallback") || img.src || DEFAULT_FALLBACK;
      const room = code ? (map[code] || null) : null;
      set(img, resolve(room, fb), fb);
    });
  }

  window.RoomImage = {
    normalize,
    resolve,
    set,
    hydrate,
    fetchRooms,
    lookup,
    hydrateByCode,
  };
})();
