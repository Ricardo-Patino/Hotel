// i18n-auto.js — runtime translator for any page
(() => {
  const SUPPORTED = ["es", "en", "fr"];
  const DICTS_BASE = "/assets/i18n/auto"; // carpeta donde el script de build deja los JSON
  const ATTRS = ["placeholder", "title", "alt", "aria-label"];
  const DISALLOWED = new Set(["SCRIPT","STYLE","NOSCRIPT","IFRAME","CODE","PRE"]);

  const normalize = (s) => s
    ?.replace(/\u00A0/g, " ")
    ?.replace(/\s+/g, " ")
    ?.trim() || "";

  // djb2 hash para clave estable
  const hash = (s) => {
    s = normalize(s);
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h) ^ s.charCodeAt(i);
    // clave corta legible
    return "k" + (h >>> 0).toString(16);
  };

  const pickLocale = () => {
    const fromStorage = localStorage.getItem("locale");
    if (fromStorage && SUPPORTED.includes(fromStorage)) return fromStorage;
    const nav = (navigator.language || "es").slice(0,2);
    return SUPPORTED.includes(nav) ? nav : "es";
  };

  async function loadDict(locale) {
    if (locale === "es") return null; // ES es la fuente, no traducimos
    try {
      const res = await fetch(`${DICTS_BASE}/${locale}.json`, { cache: "no-store" });
      if (!res.ok) throw new Error("dict not found");
      return await res.json();
    } catch {
      console.warn("[i18n] No dictionary for", locale);
      return null;
    }
  }

  // Recorremos nodos de texto visibles
  function walkTextNodes(root, cb) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const parent = node.parentElement;
        if (!parent || DISALLOWED.has(parent.tagName)) return NodeFilter.FILTER_REJECT;
        const txt = normalize(node.nodeValue);
        return txt ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    let n;
    while ((n = walker.nextNode())) cb(n);
  }

  function translatePage(dict) {
    if (!dict) return; // español, no tocamos
    // 1) Text nodes
    walkTextNodes(document.body, (node) => {
      const src = normalize(node.nodeValue);
      const key = hash(src);
      const t = dict[key];
      if (t) node.nodeValue = t;
    });

    // 2) Atributos comunes
    document.querySelectorAll("*").forEach(el => {
      ATTRS.forEach(a => {
        const val = el.getAttribute(a);
        const v = normalize(val || "");
        if (!v) return;
        const t = dict[hash(v)];
        if (t) el.setAttribute(a, t);
      });
    });

    // 3) <title> y meta description
    const titleEl = document.querySelector("head > title");
    if (titleEl) {
      const src = normalize(titleEl.textContent);
      const t = dict[hash(src)];
      if (t) titleEl.textContent = t;
    }
    const metaDesc = document.querySelector('meta[name="description"]');
    if (metaDesc?.content) {
      const src = normalize(metaDesc.content);
      const t = dict[hash(src)];
      if (t) metaDesc.setAttribute("content", t);
    }
  }

  function wireLanguageLinks() {
    document.querySelectorAll("[data-set-locale]").forEach(a => {
      a.addEventListener("click", async (e) => {
        e.preventDefault();
        const locale = a.getAttribute("data-set-locale");
        if (!SUPPORTED.includes(locale)) return;
        localStorage.setItem("locale", locale);
        // Recargamos para aplicar desde cero (más fiable en todos los componentes)
        location.reload();
      });
    });
  }

  async function init() {
    const locale = pickLocale();
    document.documentElement.setAttribute("lang", locale);
    wireLanguageLinks();
    const dict = await loadDict(locale);
    translatePage(dict);
  }

  // Opcional: evitar “flash” cambiando visibilidad
  document.documentElement.style.visibility = "hidden";
  window.addEventListener("DOMContentLoaded", async () => {
    await init();
    document.documentElement.style.visibility = "";
  });
})();
