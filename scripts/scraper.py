"""
Descarga y monitorea los listados del SAT publicados en los artículos 69 y 69-B del CFF.

- Descarga los CSV oficiales del SAT.
- Detecta encoding y fila de encabezado automáticamente (el SAT no es consistente).
- Guarda cada corrida en SQLite con snapshot histórico.
- Calcula diffs contra la corrida anterior (altas, bajas, cambios de estatus).
- Genera alertas si algún RFC de tu "watchlist" aparece o cambia de estatus.
- Exporta un JSON para alimentar la página de consulta (docs/data.json, GitHub Pages).

Uso:
    python scraper.py
"""

import csv
import io
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

# El Art. 69 no tiene un único CSV: el SAT lo publica en 6 archivos separados por
# categoría (vía el portal de datos abiertos del gobierno, más estable que el portal
# viejo del SAT). Se descargan los 6 y se combinan en una sola tabla, agregando la
# columna "Supuesto" para saber de cuál venía cada fila.
FUENTES_69_CATEGORIAS = {
    "entes_publicos_omisos": "https://repodatos.atdt.gob.mx/api_update/sat/contribuyentes_incumplidos/SAT_2_EntespublicosydeGobiernoomisos.csv",
    "sentencias": "https://www.datos.gob.mx/dataset/382fc296-5e90-4880-b0ca-4ed688f591ef/resource/4e0f0456-484c-4332-a63a-a6f2d3138dd5/download/sat_3_sentencias.csv",
    "no_localizados": "https://www.datos.gob.mx/dataset/382fc296-5e90-4880-b0ca-4ed688f591ef/resource/83fa79b9-357b-4ada-b0a4-950c97c50461/download/sat_4_nolocalizados.csv",
    "firmes": "https://www.datos.gob.mx/dataset/382fc296-5e90-4880-b0ca-4ed688f591ef/resource/29a7c943-1f77-42b2-95da-d3dc53549c94/download/sat_5_firmes.csv",
    "exigibles": "https://www.datos.gob.mx/dataset/382fc296-5e90-4880-b0ca-4ed688f591ef/resource/6301fffe-2388-489a-85e1-5c5ffcda4ce0/download/sat_6_exigibles.csv",
    "cancelados": "https://www.datos.gob.mx/dataset/382fc296-5e90-4880-b0ca-4ed688f591ef/resource/1b04d73d-faea-4056-bbab-81df9de5188f/download/sat_7_cancelados.csv",
}

FUENTES = {
    "69": {
        "categorias": FUENTES_69_CATEGORIAS,
        "tabla": "listado_69",
    },
    "69b": {
        "url": "http://omawww.sat.gob.mx/cifras_sat/Documents/Listado_Completo_69-B.csv",
        "tabla": "listado_69b",
    },
}

DB_PATH = Path(__file__).parent / "sat_listas.db"
WATCHLIST_PATH = Path(__file__).parent / "watchlist.csv"   # RFCs que te interesa monitorear
DOCS_DATA_PATH = Path(__file__).parent.parent / "docs" / "data.json"
DIFF_LOG_PATH = Path(__file__).parent / "diffs.jsonl"       # historial de cambios, uno por corrida

ENCODINGS_A_PROBAR = ["utf-8-sig", "utf-8", "windows-1250", "cp1252", "latin-1"]

# ---------------------------------------------------------------------------
# Descarga y parseo
# ---------------------------------------------------------------------------

def descargar_csv(url: str, verificar_ssl: bool = True) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SAT-monitor/1.0)"}
    resp = requests.get(url, headers=headers, timeout=60, verify=verificar_ssl)
    resp.raise_for_status()
    return resp.content


def decodificar(raw: bytes) -> str:
    """El SAT no publica siempre en UTF-8. Probamos encodings comunes en orden."""
    for enc in ENCODINGS_A_PROBAR:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # último recurso: forzar, reemplazando caracteres inválidos
    return raw.decode("latin-1", errors="replace")


def encontrar_fila_encabezado(lineas: list[str]) -> int:
    """
    El SAT suele meter 2-3 líneas de metadata/título antes del encabezado real.
    Buscamos la primera línea que contenga 'RFC' (mayúsculas, como columna).
    """
    for i, linea in enumerate(lineas[:15]):
        if "RFC" in linea.upper():
            return i
    return 0  # si no la encuentra, asume que no hay metadata


def descargar_y_combinar_69(categorias: dict) -> list[dict]:
    """
    Descarga las 6 categorías del Art. 69 y las combina, marcando la fuente de cada fila.

    Nota: datos.gob.mx tiene la cadena de certificados SSL mal configurada del lado
    del servidor (problema conocido en varios sitios .gob.mx), así que estas descargas
    se hacen sin verificar el certificado. El riesgo es bajo porque son datos públicos
    de solo lectura, pero si prefieres no desactivar la verificación, puedes intentar
    correr el script con `pip install certifi --upgrade` primero, o cambiar
    verificar_ssl=True aquí y ver si tu entorno ya trae el certificado correcto.
    """
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    todas = []
    for nombre_categoria, url in categorias.items():
        try:
            raw = descargar_csv(url, verificar_ssl=False)
        except requests.RequestException as e:
            print(f"  ADVERTENCIA: no se pudo descargar categoría '{nombre_categoria}': {e}", file=sys.stderr)
            continue
        filas = parsear_csv(raw)
        for fila in filas:
            fila["Supuesto_Art69"] = nombre_categoria
        print(f"  {nombre_categoria}: {len(filas)} registros")
        todas.extend(filas)
    return todas


def parsear_csv(raw: bytes) -> list[dict]:
    texto = decodificar(raw)
    lineas = texto.splitlines()
    idx_header = encontrar_fila_encabezado(lineas)

    reader = csv.DictReader(io.StringIO("\n".join(lineas[idx_header:])))
    filas = []
    for row in reader:
        # normaliza claves: quita espacios, mayúsculas para 'RFC' consistente
        limpio = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        if limpio.get("RFC") or limpio.get("Rfc"):
            filas.append(limpio)
    return filas


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection):
    for cfg in FUENTES.values():
        tabla = cfg["tabla"]
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {tabla} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rfc TEXT NOT NULL,
                datos_json TEXT NOT NULL,
                fecha_corrida TEXT NOT NULL
            )
        """)
        conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{tabla}_rfc ON {tabla}(rfc)
        """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS corridas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            fuente TEXT NOT NULL,
            total_registros INTEGER NOT NULL
        )
    """)
    conn.commit()


def rfc_de_fila(fila: dict) -> str:
    return fila.get("RFC") or fila.get("Rfc") or ""


def obtener_ultima_corrida(conn: sqlite3.Connection, tabla: str) -> dict:
    """Devuelve {rfc: fila_dict} de la corrida más reciente guardada, o {} si no hay historial."""
    cur = conn.execute(f"SELECT MAX(fecha_corrida) FROM {tabla}")
    ultima_fecha = cur.fetchone()[0]
    if not ultima_fecha:
        return {}
    cur = conn.execute(
        f"SELECT rfc, datos_json FROM {tabla} WHERE fecha_corrida = ?", (ultima_fecha,)
    )
    return {rfc: json.loads(datos) for rfc, datos in cur.fetchall()}


def guardar_corrida(conn: sqlite3.Connection, tabla: str, filas: list[dict], fecha: str):
    for fila in filas:
        rfc = rfc_de_fila(fila)
        conn.execute(
            f"INSERT INTO {tabla} (rfc, datos_json, fecha_corrida) VALUES (?, ?, ?)",
            (rfc, json.dumps(fila, ensure_ascii=False), fecha),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Diff entre corridas
# ---------------------------------------------------------------------------

def calcular_diff(anterior: dict, actual: dict) -> dict:
    rfcs_anterior = set(anterior.keys())
    rfcs_actual = set(actual.keys())

    nuevos = sorted(rfcs_actual - rfcs_anterior)
    removidos = sorted(rfcs_anterior - rfcs_actual)
    en_ambos = rfcs_actual & rfcs_anterior

    campo_situacion = None
    if en_ambos:
        muestra = actual[next(iter(en_ambos))]
        for posible in ("Situación del contribuyente", "Situación", "SITUACION"):
            if posible in muestra:
                campo_situacion = posible
                break

    cambios_estatus = []
    if campo_situacion:
        for rfc in en_ambos:
            v_ant = anterior[rfc].get(campo_situacion)
            v_act = actual[rfc].get(campo_situacion)
            if v_ant != v_act:
                cambios_estatus.append({
                    "rfc": rfc, "antes": v_ant, "ahora": v_act,
                })

    return {
        "nuevos": [{"rfc": r, "datos": actual[r]} for r in nuevos],
        "removidos": [{"rfc": r, "datos": anterior[r]} for r in removidos],
        "cambios_estatus": cambios_estatus,
    }


# ---------------------------------------------------------------------------
# Watchlist / alertas
# ---------------------------------------------------------------------------

def cargar_watchlist() -> set[str]:
    if not WATCHLIST_PATH.exists():
        return set()
    with open(WATCHLIST_PATH, encoding="utf-8") as f:
        return {line.strip().upper() for line in f if line.strip() and not line.startswith("#")}


def filtrar_diff_por_watchlist(diff: dict, watchlist: set[str]) -> dict:
    if not watchlist:
        return diff
    return {
        "nuevos": [x for x in diff["nuevos"] if x["rfc"].upper() in watchlist],
        "removidos": [x for x in diff["removidos"] if x["rfc"].upper() in watchlist],
        "cambios_estatus": [x for x in diff["cambios_estatus"] if x["rfc"].upper() in watchlist],
    }


def hay_cambios(diff: dict) -> bool:
    return bool(diff["nuevos"] or diff["removidos"] or diff["cambios_estatus"])


def enviar_alerta(fuente: str, diff_watchlist: dict):
    """
    Placeholder de envío de alerta. Por defecto solo imprime a stdout
    (útil para ver el log en GitHub Actions). Conecta aquí tu servicio
    de correo (Resend, SendGrid, smtplib, etc.) cuando quieras notificaciones reales.
    """
    print(f"\n🚨 ALERTA — cambios en watchlist para {fuente.upper()}:")
    print(json.dumps(diff_watchlist, indent=2, ensure_ascii=False))
    # Ejemplo de integración real (comentado):
    #
    # import smtplib
    # from email.mime.text import MIMEText
    # msg = MIMEText(json.dumps(diff_watchlist, indent=2, ensure_ascii=False))
    # msg["Subject"] = f"[SAT {fuente.upper()}] Cambios detectados en tu watchlist"
    # msg["From"] = "alertas@tudominio.com"
    # msg["To"] = "tu_correo@tudominio.com"
    # with smtplib.SMTP_SSL("smtp.tu_proveedor.com", 465) as s:
    #     s.login("usuario", "password")
    #     s.send_message(msg)


# ---------------------------------------------------------------------------
# Export para la página de consulta (GitHub Pages)
# ---------------------------------------------------------------------------

def exportar_json_para_web(conn: sqlite3.Connection):
    export = {"generado": datetime.now(timezone.utc).isoformat(), "fuentes": {}}
    for clave, cfg in FUENTES.items():
        tabla = cfg["tabla"]
        actual = obtener_ultima_corrida(conn, tabla)
        export["fuentes"][clave] = list(actual.values())

    DOCS_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DOCS_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False)
    print(f"Exportado JSON de consulta a {DOCS_DATA_PATH}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def procesar_fuente(conn: sqlite3.Connection, clave: str, cfg: dict, watchlist: set[str], fecha: str):
    print(f"\n--- Procesando {clave.upper()} ---")

    if "categorias" in cfg:
        filas = descargar_y_combinar_69(cfg["categorias"])
    else:
        try:
            raw = descargar_csv(cfg["url"])
        except requests.RequestException as e:
            print(f"ERROR descargando {clave}: {e}", file=sys.stderr)
            return
        filas = parsear_csv(raw)

    print(f"{len(filas)} registros parseados de {clave.upper()}")
    if not filas:
        print("ADVERTENCIA: 0 filas parseadas, revisa el formato del CSV manualmente.", file=sys.stderr)
        return

    tabla = cfg["tabla"]
    anterior = obtener_ultima_corrida(conn, tabla)
    actual = {rfc_de_fila(f): f for f in filas}

    diff = calcular_diff(anterior, actual)
    with open(DIFF_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"fuente": clave, "fecha": fecha, "diff": diff}, ensure_ascii=False) + "\n")

    print(f"Nuevos: {len(diff['nuevos'])} | Removidos: {len(diff['removidos'])} | "
          f"Cambios de estatus: {len(diff['cambios_estatus'])}")

    if watchlist:
        diff_watchlist = filtrar_diff_por_watchlist(diff, watchlist)
        if hay_cambios(diff_watchlist):
            enviar_alerta(clave, diff_watchlist)
    # Si no hay watchlist configurada, no se envían alertas (evita ruido con
    # todo el listado en la primera corrida). Configura scripts/watchlist.csv
    # con tus RFCs para activar las alertas dirigidas.

    guardar_corrida(conn, tabla, filas, fecha)
    conn.execute(
        "INSERT INTO corridas (fecha, fuente, total_registros) VALUES (?, ?, ?)",
        (fecha, clave, len(filas)),
    )
    conn.commit()


def main():
    fecha = datetime.now(timezone.utc).isoformat()
    watchlist = cargar_watchlist()
    if watchlist:
        print(f"Watchlist cargada: {len(watchlist)} RFC(s)")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    for clave, cfg in FUENTES.items():
        procesar_fuente(conn, clave, cfg, watchlist, fecha)

    exportar_json_para_web(conn)
    conn.close()
    print("\nListo.")


if __name__ == "__main__":
    main()
