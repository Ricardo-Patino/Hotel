// generate-locales.mjs — build-time generator (DeepL como ejemplo)
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import cheerio from "cheerio";
import dotenv from "dotenv";
import fetch from "node-fetch";

dotenv.config();
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// CONFIG
const SRC_DIR = path.resolve(__dirname, "..");         // raíz del proyecto
const PUBLIC_DIR = SRC_DIR;                             // donde están las .html
const OUT_DIR = path.resolve(SRC_DIR, "assets/i18n/auto"); // salida de diccionarios
const SUPPORTED = ["en","fr"];                          // lenguajes a generar
const DEEPL_API_KEY = process.env.DEEPL_API_KEY;        // .env
const DEEPL_URL = "https://api-free.deepl.com/v2/translate"; // usa el endpoint pago si aplica

if (!DEEPL_API_KEY) {
  console.error("Falta DEEPL_API_KEY en .env");
  process.exit(1);
}

// Normalización y hash (idénticos a i18n-auto.js)
const normalize = (s) => s?.replace(/\u00A0/g," ").replace(/\s+/g," ").trim() || "";
const hash = (s) => {
  s = normalize(s);
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h) ^ s.charCodeAt(i);
  return "k" + (h >>> 0).toString(16);
};

// Lee todas las .html recursivamente
function listHtml(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir)) {
    const p = path.join(dir, entry);
    const stat = fs.statSync(p);
    if (stat.isDirectory()) out.push(...listHtml(p));
    else if (stat.isFile() && p.endsWith(".html")) out.push(p);
  }
  return out;
}

// Extrae textos y atributos traducibles
function extractStringsFromHtml(html) {
  const $ = cheerio.load(html, { decodeEntities: false });
  const seen = new Map(); // key -> source
  const disallowed = new Set(["script","style","noscript","iframe","code","pre"]);

  // text nodes
  $("*").contents().each((_, node) => {
    if (node.type === "text") {
      const parent = node.parent;
      if (!parent || disallowed.has(parent.name)) return;
      const txt = normalize(node.data);
      if (!txt) return;
      seen.set(hash(txt), txt);
    }
  });

  // attributes
  const attrs = ["placeholder","title","alt","aria-label"];
  $("*").each((_, el) => {
    const $el = $(el);
    attrs.forEach(a => {
      const v = normalize($el.attr(a) || "");
      if (!v) return;
      seen.set(hash(v), v);
    });
  });

  // <title> y meta description
  const title = normalize($("head > title").text());
  if (title) seen.set(hash(title), title);
  const metaDesc = $('meta[name="description"]').attr("content");
  if (metaDesc) {
    const v = normalize(metaDesc);
    if (v) seen.set(hash(v), v);
  }

  return seen; // Map(key -> textES)
}

// Traduce en lotes (DeepL)
async function translateBatch(texts, target) {
  if (!texts.length) return [];
  const body = new URLSearchParams();
  texts.forEach(t => body.append("text", t));
  body.append("target_lang", target.toUpperCase());
  body.append("source_lang", "ES");
  const res = await fetch(DEEPL_URL, {
    method: "POST",
    headers: { "Authorization": `DeepL-Auth-Key ${DEEPL_API_KEY}`, "Content-Type": "application/x-www-form-urlencoded" },
    body
  });
  if (!res.ok) {
    const msg = await res.text();
    throw new Error(`DeepL error: ${res.status} ${msg}`);
  }
  const json = await res.json();
  return json.translations.map(t => t.text);
}

async function main() {
  // 1) escanear todas las .html y juntar frases únicas ES
  const files = listHtml(PUBLIC_DIR);
  const all = new Map(); // key -> source ES
  files.forEach(f => {
    const html = fs.readFileSync(f, "utf8");
    const m = extractStringsFromHtml(html);
    for (const [k, v] of m) all.set(k, v);
  });

  // 2) carga cache si existe (para no pagar de nuevo)
  const cachePath = path.join(OUT_DIR, "_cache.json");
  let cache = {};
  if (fs.existsSync(cachePath)) cache = JSON.parse(fs.readFileSync(cachePath, "utf8"));

  // 3) generar por idioma
  fs.mkdirSync(OUT_DIR, { recursive: true });

  for (const lang of SUPPORTED) {
    const dict = {};
    const missing = [];
    for (const [k, src] of all) {
      const cached = cache[src]?.[lang];
      if (cached) dict[k] = cached;
      else missing.push(src);
    }

    // traducir faltantes en lotes
    const BATCH = 40;
    for (let i = 0; i < missing.length; i += BATCH) {
      const chunk = missing.slice(i, i + BATCH);
      const translated = await translateBatch(chunk, lang);
      translated.forEach((t, idx) => {
        const src = chunk[idx];
        const k = hash(src);
        dict[k] = t;
        cache[src] = cache[src] || {};
        cache[src][lang] = t;
      });
      process.stdout.write(`Translated ${Math.min(i+BATCH, missing.length)}/${missing.length} -> ${lang}\r`);
    }

    // escribir diccionario
    fs.writeFileSync(path.join(OUT_DIR, `${lang}.json`), JSON.stringify(dict, null, 2), "utf8");
  }

  // guardar cache
  fs.writeFileSync(cachePath, JSON.stringify(cache, null, 2), "utf8");

  console.log("\nHecho. Diccionarios en:", OUT_DIR);
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
