# Bot de procesamiento - Estación Davis (WeatherLink)

Lee los archivos mensuales `AAAA-MM.wlk` que genera WeatherLink, aplica control de calidad
y los exporta por variable y por Año/Mes/Día/Hora. Está pensado para correr **en el
computador del laboratorio**, sin intervención, con el Programador de tareas de Windows.

## 1. Instalación (computador del laboratorio)

1. Instalar Python 3.9 o superior (64 bits) desde https://www.python.org (marcar **Add python.exe to PATH**).
2. Copiar esta carpeta, por ejemplo a `C:\estacion_bot\`.
3. Abrir `cmd` en esa carpeta e instalar dependencias:
   ```
   pip install -r requirements.txt
   ```
4. Abrir `bot_estacion.py` y revisar la sección **CONFIGURACIÓN**:
   - `ORIGEN`: carpeta donde WeatherLink guarda los `.wlk` (por defecto `C:\WeatherLink`; el programa
     busca dentro de sus subcarpetas, incluida la de la estación).
   - `SALIDA`: carpeta de resultados (por defecto `C:\Estacion_Climatica`).
   - `FORMATO`: `csv`, `parquet` o `ambos`.

## 2. Prueba manual

```
python bot_estacion.py
```
Debe terminar con `0 errores`. Una segunda ejecución inmediata no debe reprocesar nada
(solo procesa archivos nuevos o modificados).

## 3. Automatización

Doble clic en `instalar_tarea.bat`. Crea la tarea `BotEstacionClimatica`, que corre cada hora
al minuto 5. Se puede revisar en *Programador de tareas*.
Requisitos para que funcione sin intervención humana:
- WeatherLink abierto con **Auto Download** activo (el bot solo lee lo que WeatherLink ya descargó).
- El computador encendido y sin suspensión/hibernación.
- Para que corra aunque nadie haya iniciado sesión: en el Programador de tareas, propiedades de la
  tarea > "Ejecutar tanto si el usuario inició sesión como si no".

## 4. Qué produce

```
C:\Estacion_Climatica\
  AAAA\MM\2026-09.wlk                      copia cruda del archivo (nombre de 8 caracteres)
  procesado\
    TEMPERATURA\AAAA\MM\DD\TEMPERATURA_AAAAMMDD.csv
    HUMEDAD\...        PRESION\...        PRECIPITACION\...        VIENTO\...
  logs\bot.log                              registro de cada ejecución (rota a 1 MB)
  logs\errores_detalle.log                  traza completa de los errores
  logs\huecos_AAAA-MM.csv                   vacíos en la serie
  logs\resumen_qc_AAAA-MM.csv               conteo de banderas por variable
  estado.json                               qué se procesó y hasta qué fecha
```
Cada CSV trae `fecha_hora, anio, mes, dia, hora, minuto`, el valor y su bandera (`<variable>_flag`).
Unidades: °C, %, hPa, mm por intervalo (5 min), m/s, grados.

## 5. Control de calidad

| Bandera | Significado |
|---|---|
| `OK` | Dato sin problemas |
| `GUION` | El sensor no entregó dato (valor -32768 en el archivo). El valor queda vacío (NaN) |
| `LIMITE` | Fuera de límites físicos (`LIMITES` en la configuración) |
| `SALTO` | Cambió más de lo permitido respecto al valor de 60 min antes (`SALTO_MAX_HORA`) |
| `CONGELADO` | Mismo valor durante más de `CONGELADO_MIN` minutos |

Las banderas pueden combinarse con `|`. No se borra ni se inventa ningún dato: solo se marca.
Nota: la dirección del viento aparece como `GUION` cuando no hay viento (la consola no la registra
con calma). Es normal, la guía de WeatherLink lo explica en la página 16.

## 6. Robustez

- Un archivo dañado o incompleto se registra en el log y no detiene el procesamiento de los demás.
- Un archivo de bloqueo evita dos ejecuciones simultáneas.
- Copia con reintentos si WeatherLink tiene el archivo abierto.
- Se reprocesa solo desde el último día ya exportado. `--forzar` reprocesa todo.

## 7. Limitaciones conocidas

- El formato `.wlk` (WDAT5.3) no es público; se dedujo de archivos reales y se validó con ellos.
  Si WeatherLink se actualiza a otra versión del formato, el bot lo reportará en el log (cabecera inválida).
- La conversión de lluvia asume el pluviómetro estándar de Davis (tipo indicado en el propio registro).
  Conviene comparar el total mensual con el que muestra WeatherLink.
- El QC de salto y de congelado usa registros del mismo archivo mensual, así que el primer dato de
  cada mes no se compara con el último del mes anterior.
