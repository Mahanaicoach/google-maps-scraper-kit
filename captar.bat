@echo off
chcp 65001 >nul
rem Captação de lojas no Windows: dê dois cliques neste arquivo.
rem Precisa do Docker Desktop aberto ("Engine running").
cd /d "%~dp0"

docker info >nul 2>&1
if errorlevel 1 (
  echo.
  echo [ERRO] O Docker Desktop não está aberto. Abra o Docker Desktop, espere "Engine running" e tente de novo.
  pause
  exit /b 1
)

if not exist .env copy .env.example .env >nul

echo Iniciando o engine...
docker compose up -d
if errorlevel 1 (
  echo [ERRO] Não consegui iniciar o engine. Copie a mensagem acima e mande para o Claude.
  pause
  exit /b 1
)

echo.
set /p CIDADE=Cidade e UF (ex: Uberlandia, MG):
echo Nichos separados por virgula, ou Enter para todos os 9
set /p NICHOS=Nichos:
set /p DEPTH=Profundidade 1 a 10 (Enter = 3):
if "%DEPTH%"=="" set DEPTH=3

if "%NICHOS%"=="" (
  docker compose run --rm captador --cidade "%CIDADE%" --depth %DEPTH%
) else (
  docker compose run --rm captador --cidade "%CIDADE%" --nichos "%NICHOS%" --depth %DEPTH%
)

echo.
echo Pronto. Abrindo a pasta do relatório...
for /f "delims=" %%d in ('dir /b /ad /o-d captacao-* 2^>nul') do (
  start "" "%%d"
  goto :fim
)
:fim
pause
