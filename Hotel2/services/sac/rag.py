# services/sac/rag.py
from __future__ import annotations

import os
import json
import pickle
import pathlib
from typing import List, Dict, Any, Tuple, Optional

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# ---------------------------------------------------------------------------
# Modo debug (logs en consola si se activa SAC_RAG_DEBUG=1)
# ---------------------------------------------------------------------------
DEBUG_RAG = os.environ.get("SAC_RAG_DEBUG", "").lower() in {"1", "true", "yes"}

def _dbg(msg: str) -> None:
    if DEBUG_RAG:
        print(f"[SAC_RAG] {msg}")


# ---------------------------------------------------------------------------
# Stopwords en español para TfidfVectorizer
#   - sklearn solo acepta: None, "english" o una lista
# ---------------------------------------------------------------------------
SPANISH_STOP_WORDS = [
    "de", "la", "que", "el", "en", "y", "a", "los", "del", "se", "las", "por",
    "un", "para", "con", "no", "una", "su", "al", "lo", "como", "más", "pero",
    "sus", "le", "ya", "o", "porque", "cuando", "muy", "sin", "sobre", "también",
    "me", "hasta", "hay", "donde", "quien", "desde", "todo", "nos", "durante",
    "todos", "uno", "les", "ni", "contra", "otros", "ese", "eso", "ante", "ellos",
    "e", "esto", "mí", "antes", "algunos", "qué", "unos", "yo", "otro", "otras",
    "otra", "él", "tanto", "esa", "estos", "mucho", "quienes", "nada", "muchos",
    "cual", "poco", "ella", "estar", "estas", "algunas", "algo", "nosotros",
    "mi", "mis", "tú", "te", "ti", "tu", "tus", "ellas", "nosotras", "vosotros",
    "vosotras", "os", "mío", "mía", "míos", "mías", "tuyo", "tuya", "tuyos",
    "tuyas", "suyo", "suya", "suyos", "suyas"
]


# ---------------------------------------------------------------------------
# Directorio base de la KB
#   - Puedes sobreescribirlo con la variable de entorno SAC_KB_DIR
# ---------------------------------------------------------------------------
_BASE_DIR = os.environ.get("SAC_KB_DIR")
if _BASE_DIR:
    KB_DIR = pathlib.Path(_BASE_DIR).resolve()
else:
    # Por defecto: <raiz_proyecto>/var/sac_kb
    KB_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "var" / "sac_kb"

KB_DIR.mkdir(parents=True, exist_ok=True)
_INDEX_PATH = KB_DIR / "kb_index.pkl"


# ---------------------------------------------------------------------------
# Utilidades internas
# ---------------------------------------------------------------------------
def _load_binary(path: pathlib.Path) -> bytes:
    with path.open("rb") as f:
        return f.read()


def _load_text_from_file(path: pathlib.Path) -> str:
    """
    Carga texto desde distintos tipos de archivo.
    Implementación sencilla y defensiva; para PDF/DOCX intenta usar
    librerías si están instaladas, si no, hace un fallback básico.
    """
    ext = path.suffix.lower()
    _dbg(f"Intentando leer archivo KB: {path} (ext={ext})")

    # Textos planos / markdown / html
    if ext in {".txt", ".md", ".html", ".htm"}:
        try:
            txt = path.read_text(encoding="utf-8", errors="ignore")
            _dbg(f"Leído como texto plano, longitud={len(txt)}")
            return txt
        except Exception as e:
            _dbg(f"Error leyendo texto plano {path}: {e}")
            return path.read_text(errors="ignore")

    # DOCX (si existe python-docx)
    if ext == ".docx":
        try:
            import docx  # type: ignore
            doc = docx.Document(str(path))
            txt = "\n".join(p.text for p in doc.paragraphs)
            _dbg(f"Leído DOCX con python-docx, longitud={len(txt)}")
            return txt
        except Exception as e:
            _dbg(f"Error leyendo DOCX {path} con python-docx: {e}")

    # PDF (si existe PyPDF2)
    if ext == ".pdf":
        try:
            import PyPDF2  # type: ignore
            text_parts: List[str] = []
            with path.open("rb") as f:
                reader = PyPDF2.PdfReader(f)
                for i, page in enumerate(reader.pages):
                    page_text = page.extract_text() or ""
                    text_parts.append(page_text)
                    _dbg(f"Página {i} PDF {path}: longitud={len(page_text)}")
            txt = "\n".join(text_parts)
            _dbg(f"Leído PDF con PyPDF2, longitud total={len(txt)}")
            return txt
        except Exception as e:
            _dbg(f"Error leyendo PDF {path} con PyPDF2: {e}")
            # Fallback muy básico
            try:
                raw = path.read_bytes()
                txt = raw.decode("utf-8", errors="ignore")
                _dbg(f"Fallback lectura binaria->utf8, longitud={len(txt)}")
                return txt
            except Exception as e2:
                _dbg(f"Error en fallback lectura binaria {path}: {e2}")
                return ""

    # Fallback genérico
    try:
        txt = path.read_text(encoding="utf-8", errors="ignore")
        _dbg(f"Leído con fallback genérico, longitud={len(txt)}")
        return txt
    except Exception as e:
        _dbg(f"Error leyendo archivo {path} en fallback genérico: {e}")
        return ""


def _split_into_chunks(text: str, max_chars: int = 800, overlap: int = 150) -> List[str]:
    """
    Divide un texto en chunks de tamaño aprox. max_chars con solapamiento.
    Sencillo pero suficiente para un RAG inicial.
    """
    text = (text or "").strip()
    if not text:
        return []

    chunks: List[str] = []
    start = 0
    n = len(text)

    while start < n:
        end = start + max_chars
        fragment = text[start:end]
        # Intentar cortar en fin de frase
        last_dot = fragment.rfind(".")
        if last_dot > 200:  # evitar cortar demasiado pronto
            fragment = fragment[: last_dot + 1]
            end = start + last_dot + 1

        chunks.append(fragment.strip())
        # Nuevo inicio con solapamiento
        start = max(end - overlap, start + 1)

    chunks = [c for c in chunks if c]
    _dbg(f"_split_into_chunks → {len(chunks)} chunks")
    return chunks


# ---------------------------------------------------------------------------
# Estructura del índice
#   - Guardamos en disco:
#       {
#         "vectorizer": TfidfVectorizer,
#         "matrix": scipy.sparse,
#         "meta": [ {doc_id, file_name, chunk_index}, ... ]
#       }
# ---------------------------------------------------------------------------
def _save_index(obj: Dict[str, Any]) -> None:
    _dbg(f"Guardando índice en {_INDEX_PATH}")
    with _INDEX_PATH.open("wb") as f:
        pickle.dump(obj, f)


def _load_index() -> Optional[Dict[str, Any]]:
    if not _INDEX_PATH.exists():
        _dbg("No existe índice en disco aún.")
        return None
    try:
        with _INDEX_PATH.open("rb") as f:
            idx = pickle.load(f)
            _dbg(
                f"Índice cargado: "
                f"vectorizer={'ok' if idx.get('vectorizer') else 'None'}, "
                f"matrix={'ok' if idx.get('matrix') is not None else 'None'}, "
                f"meta_registros={len(idx.get('meta', []))}"
            )
            return idx
    except Exception as e:
        _dbg(f"Error cargando índice: {e}")
        return None


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def rebuild_index(db, docs: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Reconstruye completamente el índice de la KB en base a las filas de
    SAC_KB_Doc activas.

    Parámetros:
      - db: objeto extensions.db (para actualizar Chunks).
      - docs: lista de diccionarios con al menos {Id, FileName}.

    Retorna:
      (docs_con_chunks, lista_chunks)
      donde lista_chunks es una lista de dicts con metadatos de los chunks.
    """
    from sqlalchemy import text as sql_text  # import local para evitar ciclos

    _dbg(f"rebuild_index: KB_DIR={KB_DIR}, num_docs={len(docs)}")

    all_texts: List[str] = []
    meta: List[Dict[str, Any]] = []

    # Mapear Id → conteo de chunks
    chunks_por_doc: Dict[int, int] = {}

    for d in docs:
        doc_id = int(d["Id"])
        fname = d["FileName"]
        path = KB_DIR / fname

        _dbg(f"Procesando doc_id={doc_id}, file={fname}, path={path}")
        if not path.exists():
            _dbg(f"  Archivo no encontrado en KB_DIR, se omite.")
            chunks_por_doc[doc_id] = 0
            continue

        raw = _load_text_from_file(path)
        if not raw.strip():
            _dbg(f"  Documento vacío tras extracción de texto, se omite.")
            chunks_por_doc[doc_id] = 0
            continue

        chunks = _split_into_chunks(raw)
        chunks_por_doc[doc_id] = len(chunks)

        for idx, ch in enumerate(chunks):
            all_texts.append(ch)
            meta.append(
                {
                    "doc_id": doc_id,
                    "file_name": fname,
                    "chunk_index": idx,
                    "text": ch,
                }
            )

    if not all_texts:
        # No hay documentos o todos vacíos → limpiar índice
        _dbg("No hay texto para indexar. Limpiando índice y poniendo Chunks=0.")
        _save_index({"vectorizer": None, "matrix": None, "meta": []})
        with db.engine.begin() as conn:
            conn.execute(sql_text("UPDATE SAC_KB_Doc SET Chunks=0"))
        return docs, meta

    _dbg(f"Total de chunks a vectorizar: {len(all_texts)}")

    # Vectorizar (corregido: usamos una lista de stopwords en español)
    vectorizer = TfidfVectorizer(stop_words=SPANISH_STOP_WORDS)
    matrix = vectorizer.fit_transform(all_texts)
    _dbg("Vectorización completada.")

    index = {
        "vectorizer": vectorizer,
        "matrix": matrix,
        "meta": meta,
    }
    _save_index(index)

    # Actualizar conteo de chunks en la tabla
    from sqlalchemy import text as sql_text  # asegurar import en este bloque también
    with db.engine.begin() as conn:
        for d in docs:
            doc_id = int(d["Id"])
            cnt = int(chunks_por_doc.get(doc_id, 0))
            conn.execute(
                sql_text("UPDATE SAC_KB_Doc SET Chunks=:c WHERE Id=:id"),
                {"c": cnt, "id": doc_id},
            )
            _dbg(f"  SAC_KB_Doc(Id={doc_id}) → Chunks={cnt}")

    return docs, meta


def search(query: str, topk: int = 5) -> List[Dict[str, Any]]:
    """
    Realiza búsqueda semántica aproximada sobre los chunks indexados.
    Retorna una lista de hits ordenados por score desc:
      {
        "doc_id": int,
        "file_name": str,
        "chunk_index": int,
        "text": str,
        "score": float
      }
    """
    q = (query or "").strip()
    if not q:
        _dbg("search: consulta vacía.")
        return []

    _dbg(f"search: query='{q}', topk={topk}")

    index = _load_index()
    if not index:
        _dbg("search: índice no presente.")
        return []

    vectorizer: TfidfVectorizer = index.get("vectorizer")  # type: ignore
    matrix = index.get("matrix")
    meta: List[Dict[str, Any]] = index.get("meta", [])

    if not vectorizer or matrix is None or not meta:
        _dbg("search: índice incompleto (vectorizer/matrix/meta faltan).")
        return []

    q_vec = vectorizer.transform([q])
    scores = cosine_similarity(q_vec, matrix)[0]

    # Ordenar indices por score desc
    ranked_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    hits: List[Dict[str, Any]] = []

    for i in ranked_idx[: max(1, topk)]:
        m = meta[i]
        hit = {
            "doc_id": m["doc_id"],
            "file_name": m["file_name"],
            "chunk_index": m["chunk_index"],
            "text": m["text"],
            "score": float(scores[i]),
        }
        hits.append(hit)

    if hits:
        _dbg(
            "search: mejores scores = "
            + ", ".join(f"{h['score']:.4f}" for h in hits[:5])
        )
    else:
        _dbg("search: sin resultados.")

    return hits


def answer_from_chunks(query: str, hits: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Construye una respuesta simple a partir de los mejores chunks.
    Para esta versión mínima:
      - Usa el chunk de mayor score como base de respuesta.
      - El confidence se calcula a partir del mejor score con una heurística
        más generosa, para no filtrar respuestas válidas del hotel.
    """
    if not hits:
        _dbg("answer_from_chunks: sin hits.")
        return {"ok": False, "answer": "", "confidence": 0.0, "sources": []}

    # Ordenar por score
    hits_sorted = sorted(hits, key=lambda h: h.get("score", 0.0), reverse=True)
    best = hits_sorted[0]
    best_score = float(best.get("score") or 0.0)
    _dbg(f"answer_from_chunks: best_score={best_score:.6f}")

    # Texto base de respuesta (top 2–3 chunks concatenados, recortado)
    top_texts = [h["text"] for h in hits_sorted[:3] if h.get("text")]
    raw_answer = "\n\n".join(top_texts)
    # Recortar a algo razonable
    if len(raw_answer) > 1200:
        raw_answer = raw_answer[:1200].rsplit(" ", 1)[0] + "..."

    # Heurística de confianza basada en el score de coseno
    # (valores típicos en TF-IDF suelen ser bajos; ampliamos el rango)
    if best_score < 0.02:
        conf = 0.15  # prácticamente ruido
    elif best_score < 0.05:
        conf = 0.35  # match débil pero algo relevante
    elif best_score < 0.10:
        conf = 0.55  # match moderado
    elif best_score < 0.20:
        conf = 0.75  # buen match
    else:
        conf = 0.90  # match muy claro

    _dbg(f"answer_from_chunks: confianza heurística={conf:.2f}")

    sources = [
        {
            "doc_id": h["doc_id"],
            "file_name": h["file_name"],
            "chunk_index": h["chunk_index"],
            "score": float(h.get("score") or 0.0),
        }
        for h in hits_sorted[:10]
    ]

    # Mensaje amigable
    answer = (
        "Según la información registrada en la base de conocimiento del hotel:\n\n"
        f"{raw_answer}"
    )

    return {
        "ok": True,
        "answer": answer,
        "confidence": conf,
        "sources": sources,
    }
