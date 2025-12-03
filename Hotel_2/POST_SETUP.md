
# Post Setup (LITE)

Este paquete es liviano para poder descargarlo sin errores. Incluye:
- `app.py`, `templates/`, CSS/JS y fuentes.
- **No incluye** imágenes/videos/pdf (assets pesados).

## Cómo restaurar los assets pesados
1. Coloque su `Hotel.zip` (original) en la raíz de este proyecto (junto a `app.py`).
2. Con el entorno activado, ejecute:
   ```bash
   python populate_static_from_original.py
   ```
3. Esto copiará imágenes/videos/etc. a `static/` con la misma estructura usada por las plantillas.

> Alternativamente, puede copiar manualmente las carpetas de imágenes desde su proyecto original hacia `static/`.
