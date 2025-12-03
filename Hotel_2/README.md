# HotelFlask

Proyecto Flask generado automáticamente a partir de `Hotel.zip`.

## Cómo ejecutar

```bash
cd HotelFlask
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
python app.py
```

Luego abra http://localhost:5000 en su navegador.
- Si su proyecto original tenía `index.html`, la ruta raíz (`/`) ya está creada.
- Todas las páginas `.html` adicionales también están disponibles como `/nombre.html`.
  Ejemplo: si existe `about.html`, podrá entrar a `http://localhost:5000/about.html`.

## Estructura

- `app.py` — aplicación Flask con rutas auto-generadas.
- `templates/` — HTMLs convertidos en plantillas Jinja2.
- `static/` — assets (CSS, JS, imágenes, fuentes, etc.). Las rutas a assets se reescribieron con `url_for('static', filename=...)` cuando fue posible.

> Nota: si algún asset no cargara, verifique las rutas dentro del HTML. Puede ajustar manualmente los enlaces o mover archivos dentro de `static/`.