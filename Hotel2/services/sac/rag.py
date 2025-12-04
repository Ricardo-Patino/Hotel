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

    # Textos planos / markdown / html
    if ext in {".txt", ".md", ".html", ".htm"}:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return path.read_text(errors="ignore")

    # DOCX (si existe python-docx)
    if ext == ".docx":
        try:
            import docx  # type: ignore
            doc = docx.Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception:
            pass

    # PDF (si existe PyPDF2)
    if ext == ".pdf":
        try:
            import PyPDF2  # type: ignore
            text = []
            with path.open("rb") as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages:
                    text.append(page.extract_text() or "")
            return "\n".join(text)
        except Exception:
            pass

    # Fallback genérico
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
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

    return [c for c in chunks if c]


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
    with _INDEX_PATH.open("wb") as f:
        pickle.dump(obj, f)


def _load_index() -> Optional[Dict[str, Any]]:
    if not _INDEX_PATH.exists():
        return None
    try:
        with _INDEX_PATH.open("rb") as f:
            return pickle.load(f)
    except Exception:
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

    all_texts: List[str] = []
    meta: List[Dict[str, Any]] = []

    # Mapear Id → conteo de chunks
    chunks_por_doc: Dict[int, int] = {}

    for d in docs:
        doc_id = int(d["Id"])
        fname = d["FileName"]
        path = KB_DIR / fname

        if not path.exists():
            continue

        raw = _load_text_from_file(path)
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
        _save_index({"vectorizer": None, "matrix": None, "meta": []})
        # Actualizar Chunks=0
        with db.engine.begin() as conn:
            conn.execute(sql_text("UPDATE SAC_KB_Doc SET Chunks=0"))
        return docs, meta

    # Vectorizar
    vectorizer = TfidfVectorizer(stop_words="spanish")
    matrix = vectorizer.fit_transform(all_texts)

    index = {
        "vectorizer": vectorizer,
        "matrix": matrix,
        "meta": meta,
    }
    _save_index(index)

    # Actualizar conteo de chunks en la tabla
    with db.engine.begin() as conn:
        for d in docs:
            doc_id = int(d["Id"])
            cnt = int(chunks_por_doc.get(doc_id, 0))
            conn.execute(
                sql_text("UPDATE SAC_KB_Doc SET Chunks=:c WHERE Id=:id"),
                {"c": cnt, "id": doc_id},
            )

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
        return []

    index = _load_index()
    if not index:
        return []

    vectorizer: TfidfVectorizer = index.get("vectorizer")  # type: ignore
    matrix = index.get("matrix")
    meta: List[Dict[str, Any]] = index.get("meta", [])

    if not vectorizer or matrix is None or not meta:
        return []

    q_vec = vectorizer.transform([q])
    scores = cosine_similarity(q_vec, matrix)[0]

    # Ordenar indices por score desc
    ranked_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    hits: List[Dict[str, Any]] = []

    for i in ranked_idx[: max(1, topk)]:
        m = meta[i]
        hits.append(
            {
                "doc_id": m["doc_id"],
                "file_name": m["file_name"],
                "chunk_index": m["chunk_index"],
                "text": m["text"],
                "score": float(scores[i]),
            }
        )

    return hits


def answer_from_chunks(query: str, hits: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Construye una respuesta simple a partir de los mejores chunks.
    Para esta versión mínima:
      - Usa el chunk de mayor score como base de respuesta.
      - El confidence se calcula a partir del mejor score (capado).
    """
    if not hits:
        return {"ok": False, "answer": "", "confidence": 0.0, "sources": []}

    # Ordenar por score
    hits_sorted = sorted(hits, key=lambda h: h.get("score", 0.0), reverse=True)
    best = hits_sorted[0]
    best_score = float(best.get("score") or 0.0)

    # Texto base de respuesta (top 2–3 chunks concatenados, recortado)
    top_texts = [h["text"] for h in hits_sorted[:3] if h.get("text")]
    raw_answer = "\n\n".join(top_texts)
    # Recortar a algo razonable
    if len(raw_answer) > 1200:
        raw_answer = raw_answer[:1200].rsplit(" ", 1)[0] + "..."

    # Normalizar confidence en [0.1, 0.95] de forma heurística
    conf = max(0.1, min(0.95, best_score))

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
