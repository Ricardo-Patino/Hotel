// Hotel2/static/assets/js/sac-widget.js
// Compatibilidad temporal:
// El controlador activo del widget vive en templates/partials/sac-widget.html.
// Este archivo queda deshabilitado para evitar doble inicialización del mismo chat.

(function () {
  if (window.__VG_SAC_WIDGET_V2__) {
    console.info('[SAC] sac-widget.js legado deshabilitado: controlador V2 ya cargado.');
    return;
  }

  console.warn('[SAC] sac-widget.js legado cargado sin V2 activo. No inicializa para evitar conflictos.');
})();