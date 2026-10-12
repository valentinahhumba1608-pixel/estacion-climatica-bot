@echo off
REM Crea una tarea programada de Windows que ejecuta el bot cada hora (minuto :05).
REM Ejecutar con doble clic (si falla por permisos: clic derecho > Ejecutar como administrador).
setlocal
set "PY="
for /f "delims=" %%i in ('where python 2^>nul') do (
  if not defined PY set "PY=%%i"
)
if not defined PY (
  echo No se encontro Python en el PATH.
  echo Instalalo desde python.org marcando "Add python.exe to PATH" y vuelve a intentar.
  pause
  exit /b 1
)
echo Python: %PY%
schtasks /Create /TN "BotEstacionClimatica" /SC HOURLY /MO 1 /ST 00:05 /TR "\"%PY%\" \"%~dp0bot_estacion.py\"" /F
if errorlevel 1 (
  echo No se pudo crear la tarea.
) else (
  echo Tarea "BotEstacionClimatica" creada: corre cada hora al minuto 5.
)
pause
