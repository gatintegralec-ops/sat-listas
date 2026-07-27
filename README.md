# Monitor de listados SAT — Art. 69 y 69-B CFF

Descarga automáticamente los listados oficiales del SAT (contribuyentes incumplidos del
Art. 69 y EFOS del Art. 69-B), guarda historial, detecta cambios entre publicaciones y
expone una página web de consulta por RFC. Corre solo, sin servidor, usando GitHub Actions.

## Qué hace

1. `scripts/scraper.py` descarga los CSV oficiales del SAT, los limpia (el SAT usa encodings
   inconsistentes y mete filas de metadata antes del encabezado real) y los guarda en SQLite
   (`scripts/sat_listas.db`), con un snapshot completo por cada corrida.
2. Compara la corrida actual contra la anterior y genera un log de RFCs nuevos, removidos y
   con cambio de estatus (`scripts/diffs.jsonl`).
3. Si algún RFC de tu `scripts/watchlist.csv` aparece en ese diff, dispara una alerta
   (por defecto solo imprime en el log de GitHub Actions — conecta tu propio correo, ver abajo).
4. Exporta `docs/data.json`, que alimenta `docs/index.html`, una página estática de consulta
   por RFC (lista para publicarse gratis con GitHub Pages).
5. `.github/workflows/actualizar.yml` corre todo esto automáticamente todos los días y
   hace commit de los cambios al repo.

## Instalación (10 minutos)

### 1. Sube este proyecto a un repositorio de GitHub
```bash
cd sat_listas_repo
git init
git add .
git commit -m "Setup inicial"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/TU_REPO.git
git push -u origin main
```

### 2. Activa GitHub Pages
En tu repo: **Settings → Pages → Source → Deploy from branch → main → /docs**.
Tu página de consulta quedará en `https://TU_USUARIO.github.io/TU_REPO/`.

### 3. Prueba el workflow manualmente
En la pestaña **Actions** de tu repo, selecciona "Actualizar listados SAT 69 y 69-B" →
**Run workflow**. Esto corre el scraper por primera vez y genera `docs/data.json`.
El cron programado (`0 13 * * *`) lo seguirá corriendo solo todos los días sin que hagas nada.

### 4. (Opcional) Define tu watchlist
Edita `scripts/watchlist.csv` y agrega los RFCs de tus proveedores/clientes que quieres
monitorear, uno por línea. Cuando alguno aparezca o cambie de estatus, saldrá resaltado
en el log de la corrida de GitHub Actions.

### 5. (Opcional) Alertas por correo reales
Por defecto las alertas solo se imprimen en el log de GitHub Actions. Para recibirlas por
correo, edita la función `enviar_alerta()` en `scripts/scraper.py` — hay un ejemplo comentado
con `smtplib`. Guarda las credenciales como **Secrets** del repo
(Settings → Secrets and variables → Actions), nunca en el código directamente.

## Correrlo localmente (para probar antes de subir a GitHub)

```bash
cd scripts
pip install -r requirements.txt
python scraper.py
```

Esto crea `sat_listas.db`, `diffs.jsonl` y `../docs/data.json`. Abre `docs/index.html` en tu
navegador (o corre `python -m http.server` desde `docs/`) para probar el buscador con datos reales.

## Notas importantes

- **Fuente de datos**: CSV oficiales publicados por el SAT en su portal de Cifras SAT.
  Si el SAT cambia la URL o el formato del archivo, el scraper puede necesitar ajustes —
  está escrito para ser tolerante a encoding y metadata variable, pero no es infalible.
- **Historial completo**: cada corrida se guarda como snapshot, nunca se sobreescribe, así que
  siempre puedes reconstruir "quién estaba en la lista en qué fecha".
- **Esto no es asesoría legal**: es una herramienta de consulta informativa. Para decisiones
  de materialidad fiscal o defensa ante el SAT, consulta a tu contador o abogado fiscalista.
