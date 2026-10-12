#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot_estacion.py - Bot de procesamiento de la estación meteorológica Davis.

Flujo (cada vez que se ejecuta, pensado para el Programador de tareas de Windows):
  1. Busca archivos mensuales de WeatherLink (AAAA-MM.wlk) en ORIGEN.
  2. Detecta cuáles son nuevos o cambiaron desde la última ejecución (estado.json).
  3. Copia el archivo crudo a SALIDA\\AAAA\\MM\\  (el original se deja en su sitio
     porque WeatherLink sigue escribiendo en él).
  4. Lee el binario (formato WDAT5.3: cabecera de 212 bytes + registros de 88 bytes).
  5. Convierte a unidades SI y aplica control de calidad (QC):
        - guiones del sensor (valores inválidos)
        - límites físicos
        - tasa de cambio excesiva entre horas consecutivas
        - valores congelados (stuck values)
  6. Exporta por variable y por Año/Mes/Día (con columnas hora y minuto).
  7. Registra todo en logs\\bot.log y los huecos en logs\\huecos_AAAA-MM.csv.

Uso:
    python bot_estacion.py                       (usa la configuración de abajo)
    python bot_estacion.py --origen D:\\datos --salida D:\\salida --formato ambos
    python bot_estacion.py --forzar              (reprocesa todo, ignora el estado)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import struct
import sys
import time
import traceback
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np
import pandas as pd

# =============================================================================
# CONFIGURACIÓN  (lo único que normalmente hay que editar)
# =============================================================================
ORIGEN = Path(r"C:\WeatherLink")          # carpeta donde WeatherLink guarda los .wlk
SALIDA = Path(r"C:\Estacion_Climatica")   # carpeta raíz de salida
FORMATO = "csv"                           # "csv", "parquet" o "ambos"

# Límites físicos (mín, máx) por columna. Ajustar si el sitio lo requiere.
LIMITES = {
    "temp_c": (-40.0, 60.0),
    "hr_pct": (0.0, 100.0),
    "presion_hpa": (500.0, 1100.0),
    "lluvia_mm": (0.0, 100.0),      # por intervalo de archivo (5 min)
    "viento_ms": (0.0, 75.0),
    "racha_ms": (0.0, 100.0),
    "dir_grados": (0.0, 360.0),
}

# Cambio máximo permitido en 60 minutos (valor absoluto).
SALTO_MAX_HORA = {
    "temp_c": 10.0,       # °C / h
    "hr_pct": 50.0,       # % / h
    "presion_hpa": 6.0,   # hPa / h
}

# Minutos con valor idéntico a partir de los cuales se considera "congelado".
CONGELADO_MIN = {
    "temp_c": 180,
    "hr_pct": 360,
    "presion_hpa": 180,
}

# Grupos de salida: carpeta -> columnas
GRUPOS = {
    "TEMPERATURA": ["temp_c"],
    "HUMEDAD": ["hr_pct"],
    "PRESION": ["presion_hpa"],
    "PRECIPITACION": ["lluvia_mm"],
    "VIENTO": ["viento_ms", "racha_ms", "dir_grados"],
}

# =============================================================================
# CONSTANTES DEL FORMATO WDAT5.3  (ingeniería inversa sobre archivos reales)
# =============================================================================
ID_ARCHIVO = b"WDAT5.3"
TAM_CABECERA = 212        # 16 bytes de id + total(4) + 32 entradas de 6 bytes (día: n, inicio)
TAM_REGISTRO = 88
SENTINELAS = (32767, -32768)   # valor que guarda WeatherLink cuando el sensor no dio dato
# type, intervalo, icon, more, hora(min desde 00:00), T, Thi, Tlo, Tint, barómetro,
# HR, HRint, lluvia, tasa lluvia, viento, racha, dir, dir racha, n muestras, solar, solar hi, UV, UV hi
REG = struct.Struct("<BBBBHhhhhhhhHhHHBBhhhBB")
RE_NOMBRE = re.compile(r"^(\d{4})-(\d{2})\.wlk$", re.IGNORECASE)

# mm por "click" según el nibble alto del campo lluvia (documentación Davis).
MM_POR_CLICK = {0x0: 0.254, 0x1: 0.2, 0x2: 1.0, 0x3: 0.1, 0x6: 0.1}

log = logging.getLogger("bot_estacion")


# =============================================================================
# LECTURA DEL ARCHIVO .wlk
# =============================================================================
def leer_wlk(ruta: Path, anio: int, mes: int) -> pd.DataFrame:
    """Lee un .wlk mensual y devuelve un DataFrame (índice = fecha_hora) en unidades SI.

    Los valores inválidos (guiones) quedan como NaN y se anotan en las columnas
    `<col>_dash` (True cuando el sensor entregó guión).
    Lanza ValueError si el archivo no tiene el formato esperado o está incompleto.
    """
    datos = ruta.read_bytes()
    if len(datos) < TAM_CABECERA or datos[:7] != ID_ARCHIVO:
        raise ValueError(f"{ruta.name}: cabecera inválida (no es WDAT5.3)")
    total = struct.unpack_from("<i", datos, 16)[0]
    if total < 0 or len(datos) < TAM_CABECERA + total * TAM_REGISTRO:
        raise ValueError(f"{ruta.name}: archivo incompleto (¿WeatherLink está escribiendo?)")

    filas = []
    for dia in range(1, 32):
        n, inicio = struct.unpack_from("<hi", datos, 20 + dia * 6)
        if n <= 0:
            continue
        for i in range(n):
            off = TAM_CABECERA + (inicio + i) * TAM_REGISTRO
            if off + TAM_REGISTRO > len(datos):
                raise ValueError(f"{ruta.name}: registro fuera de rango (día {dia})")
            if datos[off] != 1:           # 1 = registro de archivo; 2 y 3 = resúmenes diarios
                continue
            v = REG.unpack_from(datos, off)
            filas.append((dia, v[1], v[4], v[5], v[9], v[10], v[12], v[13],
                          v[14], v[15], v[16], v[17]))

    if not filas:
        raise ValueError(f"{ruta.name}: sin registros de archivo")

    raw = pd.DataFrame(filas, columns=[
        "dia", "intervalo", "minuto", "t10", "bar", "hr10", "lluvia", "tasa",
        "viento10", "racha10", "dir", "dirmax"])
    raw["fecha_hora"] = [datetime(anio, mes, int(d)) + timedelta(minutes=int(m))
                         for d, m in zip(raw["dia"], raw["minuto"])]

    def dash(col):
        return raw[col].isin(SENTINELAS)

    out = pd.DataFrame(index=pd.DatetimeIndex(raw["fecha_hora"], name="fecha_hora"))
    out["intervalo_min"] = raw["intervalo"].values

    # Temperatura: décimas de °F -> °C
    out["temp_c"] = np.where(dash("t10"), np.nan, ((raw["t10"] / 10.0) - 32.0) * 5.0 / 9.0)
    out["temp_c_dash"] = dash("t10").values
    # Humedad relativa: décimas de %
    out["hr_pct"] = np.where(dash("hr10"), np.nan, raw["hr10"] / 10.0)
    out["hr_pct_dash"] = dash("hr10").values
    # Presión: milésimas de inHg -> hPa
    out["presion_hpa"] = np.where(dash("bar"), np.nan, raw["bar"] / 1000.0 * 33.8639)
    out["presion_hpa_dash"] = dash("bar").values
    # Viento: décimas de mph -> m/s ; dirección: 0-15 (x22.5°), 255 = guion
    v_dash = raw["viento10"].isin(SENTINELAS) | (raw["viento10"] >= 32767)
    r_dash = raw["racha10"].isin(SENTINELAS) | (raw["racha10"] >= 32767)
    d_dash = (raw["dir"] > 15)
    out["viento_ms"] = np.where(v_dash, np.nan, raw["viento10"] / 10.0 * 0.44704)
    out["viento_ms_dash"] = v_dash.values
    out["racha_ms"] = np.where(r_dash, np.nan, raw["racha10"] / 10.0 * 0.44704)
    out["racha_ms_dash"] = r_dash.values
    out["dir_grados"] = np.where(d_dash, np.nan, raw["dir"] * 22.5)
    out["dir_grados_dash"] = d_dash.values
    # Lluvia: clicks * mm/click (el nibble alto indica el tipo de pluviómetro)
    lluvia = raw["lluvia"].to_numpy()
    tipo = pd.Series((lluvia >> 12) & 0xF, index=raw.index)
    clicks = pd.Series(lluvia & 0x0FFF, index=raw.index)
    mm = tipo.map(MM_POR_CLICK)
    if mm.isna().any():
        log.warning("%s: tipo de pluviómetro desconocido en %d registros (lluvia queda NaN)",
                    ruta.name, int(mm.isna().sum()))
    out["lluvia_mm"] = (clicks * mm).values
    out["lluvia_mm_dash"] = mm.isna().values

    dup = out.index.duplicated(keep="last")
    if dup.any():
        log.warning("%s: %d registros con fecha repetida (se conserva el último)", ruta.name, int(dup.sum()))
        out = out[~dup]
    return out.sort_index()


# =============================================================================
# CONTROL DE CALIDAD
# =============================================================================
def _marcar(flags: pd.Series, mascara: pd.Series, codigo: str) -> None:
    """Agrega `codigo` a la bandera de las filas donde `mascara` es True."""
    m = mascara.fillna(False).astype(bool)
    if m.any():
        actual = flags.loc[m]
        flags.loc[m] = np.where(actual == "", codigo, actual + "|" + codigo)


def aplicar_qc(df: pd.DataFrame) -> pd.DataFrame:
    """Devuelve df con una columna `<col>_flag` por variable.

    Banderas: OK, GUION, LIMITE, SALTO, CONGELADO (pueden combinarse con '|').
    No se imputa ni se borra ningún valor: el dato original queda y la bandera lo marca.
    """
    out = df.copy()
    for col in LIMITES:
        flags = pd.Series("", index=out.index, dtype=object)
        s = out[col]

        # 1) guiones del sensor
        _marcar(flags, out[col + "_dash"], "GUION")

        # 2) límites físicos
        lo, hi = LIMITES[col]
        fuera = s.notna() & ((s < lo) | (s > hi))
        _marcar(flags, fuera, "LIMITE")

        # serie "confiable" para los demás chequeos
        confiable = s.where(~(out[col + "_dash"] | fuera))

        # 3) tasa de cambio entre horas consecutivas (contra el valor 60 min antes)
        if col in SALTO_MAX_HORA:
            previo = confiable.reindex(confiable.index - pd.Timedelta(minutes=60))
            previo.index = confiable.index
            salto = (confiable - previo).abs() > SALTO_MAX_HORA[col]
            _marcar(flags, salto, "SALTO")

        # 4) valores congelados
        if col in CONGELADO_MIN:
            n_min = max(2, int(CONGELADO_MIN[col] / max(1, int(out["intervalo_min"].median()))))
            id_racha = (confiable != confiable.shift()).cumsum()
            largo = confiable.groupby(id_racha).transform("size")
            _marcar(flags, confiable.notna() & (largo >= n_min), "CONGELADO")

        flags[flags == ""] = "OK"
        out[col + "_flag"] = flags
    return out


def detectar_huecos(df: pd.DataFrame) -> pd.DataFrame:
    """Lista los vacíos en la serie (diferencia entre registros > 1.5 x intervalo)."""
    paso = max(1, int(df["intervalo_min"].median()))
    d = df.index.to_series().diff().dt.total_seconds() / 60.0
    m = d > paso * 1.5
    return pd.DataFrame({
        "desde": df.index.to_series().shift()[m].values,
        "hasta": df.index[m],
        "minutos_sin_datos": (d[m] - paso).values,
        "registros_faltantes": ((d[m] / paso) - 1).round().astype(int).values,
    })


# =============================================================================
# EXPORTACIÓN
# =============================================================================
def _escribir(df: pd.DataFrame, destino: Path, formato: str) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    if formato in ("csv", "ambos"):
        tmp = destino.with_suffix(".csv.tmp")
        df.to_csv(tmp, index=False, encoding="utf-8", float_format="%.3f")
        os.replace(tmp, destino.with_suffix(".csv"))
    if formato in ("parquet", "ambos"):
        try:
            tmp = destino.with_suffix(".parquet.tmp")
            df.to_parquet(tmp, index=False)
            os.replace(tmp, destino.with_suffix(".parquet"))
        except ImportError:
            log.error("Parquet pedido pero falta 'pyarrow' (pip install pyarrow). Se omite.")


def exportar(df: pd.DataFrame, salida: Path, formato: str, desde: pd.Timestamp | None) -> int:
    """Escribe procesado/<VARIABLE>/AAAA/MM/DD/<VARIABLE>_AAAAMMDD.csv (un archivo por día).

    Solo reescribe los días >= `desde` (si se indica). Devuelve cuántos archivos escribió.
    """
    n = 0
    base = df.copy()
    base.insert(0, "fecha_hora", base.index.strftime("%Y-%m-%d %H:%M"))
    base.insert(1, "anio", base.index.year)
    base.insert(2, "mes", base.index.month)
    base.insert(3, "dia", base.index.day)
    base.insert(4, "hora", base.index.hour)
    base.insert(5, "minuto", base.index.minute)

    for fecha, dia_df in base.groupby(base.index.normalize()):
        if desde is not None and fecha < desde.normalize():
            continue
        for grupo, cols in GRUPOS.items():
            columnas = ["fecha_hora", "anio", "mes", "dia", "hora", "minuto"]
            for c in cols:
                columnas += [c, c + "_flag"]
            carpeta = salida / "procesado" / grupo / f"{fecha:%Y}" / f"{fecha:%m}" / f"{fecha:%d}"
            _escribir(dia_df[columnas], carpeta / f"{grupo}_{fecha:%Y%m%d}", formato)
            n += 1
    return n


def resumen_qc(df: pd.DataFrame, ruta: Path) -> None:
    """Guarda cuántas banderas de cada tipo hubo por variable."""
    filas = []
    for col in LIMITES:
        for bandera, cuenta in df[col + "_flag"].str.split("|").explode().value_counts().items():
            filas.append((col, bandera, int(cuenta)))
    ruta.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(filas, columns=["variable", "bandera", "conteo"]).to_csv(ruta, index=False)


# =============================================================================
# ESTADO, COPIA DEL ARCHIVO CRUDO Y BLOQUEO
# =============================================================================
def cargar_estado(ruta: Path) -> dict:
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("estado.json ilegible (%s); se reprocesa todo", e)
        return {}


def guardar_estado(ruta: Path, estado: dict) -> None:
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(estado, indent=2), encoding="utf-8")
    os.replace(tmp, ruta)


def copiar_crudo(origen: Path, salida: Path, anio: int, mes: int) -> Path:
    """Copia el .wlk a SALIDA\\AAAA\\MM\\ con reintentos (WeatherLink puede tenerlo abierto)."""
    destino = salida / f"{anio:04d}" / f"{mes:02d}" / origen.name
    destino.parent.mkdir(parents=True, exist_ok=True)
    for intento in range(1, 4):
        try:
            shutil.copy2(origen, destino)
            return destino
        except (PermissionError, OSError) as e:
            log.warning("Copia de %s falló (intento %d/3): %s", origen.name, intento, e)
            time.sleep(2)
    raise OSError(f"No se pudo copiar {origen}")


class Bloqueo:
    """Evita que dos ejecuciones del bot corran a la vez (bloqueo caduca a los 30 min)."""

    def __init__(self, ruta: Path):
        self.ruta = ruta

    def __enter__(self):
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        if self.ruta.exists() and time.time() - self.ruta.stat().st_mtime > 1800:
            log.warning("Bloqueo antiguo eliminado")
            self.ruta.unlink()
        try:
            fd = os.open(self.ruta, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            raise RuntimeError("Otra ejecución del bot sigue en curso")
        return self

    def __exit__(self, *exc):
        try:
            self.ruta.unlink()
        except OSError:
            pass


# =============================================================================
# PROCESO PRINCIPAL
# =============================================================================
def procesar_archivo(ruta: Path, salida: Path, formato: str, estado: dict, forzar: bool) -> bool:
    """Procesa un .wlk. Devuelve True si hubo cambios que procesar."""
    m = RE_NOMBRE.match(ruta.name)
    anio, mes = int(m.group(1)), int(m.group(2))
    st = ruta.stat()
    clave = str(ruta.resolve())
    previo = estado.get(clave, {})

    if (not forzar and previo.get("tam") == st.st_size
            and previo.get("mtime") == int(st.st_mtime)):
        log.debug("Sin cambios: %s", ruta.name)
        return False

    log.info("Procesando %s (%d bytes)", ruta.name, st.st_size)
    copiar_crudo(ruta, salida, anio, mes)

    df = leer_wlk(ruta, anio, mes)
    df = aplicar_qc(df)

    huecos = detectar_huecos(df)
    ruta_h = salida / "logs" / f"huecos_{anio:04d}-{mes:02d}.csv"
    ruta_h.parent.mkdir(parents=True, exist_ok=True)
    huecos.to_csv(ruta_h, index=False)
    if len(huecos):
        log.warning("%s: %d huecos en los datos (ver %s)", ruta.name, len(huecos), ruta_h.name)

    resumen_qc(df, salida / "logs" / f"resumen_qc_{anio:04d}-{mes:02d}.csv")

    desde = None
    if not forzar and previo.get("ultimo_ts"):
        desde = pd.Timestamp(previo["ultimo_ts"])
    n = exportar(df, salida, formato, desde)

    estado[clave] = {
        "tam": st.st_size,
        "mtime": int(st.st_mtime),
        "ultimo_ts": df.index.max().isoformat(),
        "registros": int(len(df)),
        "procesado_en": datetime.now().isoformat(timespec="seconds"),
    }
    log.info("%s OK: %d registros, %d archivos de salida, último dato %s",
             ruta.name, len(df), n, df.index.max())
    return True


def configurar_logs(salida: Path) -> None:
    carpeta = salida / "logs"
    carpeta.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = RotatingFileHandler(carpeta / "bot.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(fh)
    log.addHandler(sh)


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot de procesamiento de estación Davis (.wlk)")
    ap.add_argument("--origen", type=Path, default=ORIGEN)
    ap.add_argument("--salida", type=Path, default=SALIDA)
    ap.add_argument("--formato", choices=["csv", "parquet", "ambos"], default=FORMATO)
    ap.add_argument("--forzar", action="store_true", help="reprocesar todo")
    args = ap.parse_args()

    configurar_logs(args.salida)
    log.info("=== Inicio del bot | origen=%s | salida=%s ===", args.origen, args.salida)
    errores = 0
    try:
        with Bloqueo(args.salida / "bot.lock"):
            if not args.origen.exists():
                log.error("La carpeta de origen no existe: %s", args.origen)
                return 1
            ruta_estado = args.salida / "estado.json"
            estado = cargar_estado(ruta_estado)
            salida_res = args.salida.resolve()
            archivos = sorted(p for p in args.origen.rglob("*.wlk")
                              if RE_NOMBRE.match(p.name) and salida_res not in p.resolve().parents)
            if not archivos:
                log.warning("No se encontraron archivos AAAA-MM.wlk en %s", args.origen)
            for ruta in archivos:
                try:
                    procesar_archivo(ruta, args.salida, args.formato, estado, args.forzar)
                    guardar_estado(ruta_estado, estado)
                except Exception as e:   # un archivo malo no debe tumbar al bot
                    errores += 1
                    log.error("Falló %s: %s", ruta.name, e)
                    log.debug(traceback.format_exc())
                    with open(args.salida / "logs" / "errores_detalle.log", "a", encoding="utf-8") as f:
                        f.write(f"\n--- {datetime.now()} {ruta}\n{traceback.format_exc()}")
    except RuntimeError as e:
        log.warning("%s; esta ejecución se omite", e)
        return 0
    except Exception as e:
        log.critical("Error inesperado: %s", e)
        log.critical(traceback.format_exc())
        return 2
    log.info("=== Fin del bot (%d errores) ===", errores)
    return 1 if errores else 0


if __name__ == "__main__":
    sys.exit(main())
