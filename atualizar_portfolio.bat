@echo off
REM ============================================================
REM  Fermento - publica esta pasta no GitHub
REM  O que faz: confere, mostra o que vai subir, pede OK e envia.
REM  O que NAO faz: nao copia arquivos de outras pastas. Quem
REM  prepara o conteudo sanitizado e o Cronos.
REM  Pode rodar quantas vezes quiser.
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

git --version >nul 2>&1 || (echo [ERRO] Git nao encontrado. Instale em https://git-scm.com e rode de novo. & pause & exit /b 1)

if not exist ".git" (
  git init >nul
  git branch -M main
  echo Repositorio local criado.
)

git remote get-url origin >nul 2>&1
if errorlevel 1 (
  echo.
  echo Cole o endereco do repositorio criado no GitHub
  echo  exemplo: https://github.com/SEU_USUARIO/fermento.git
  set /p URL=Endereco: 
  call git remote add origin %%URL%%
)

git add -A

REM --- Trava de seguranca: nada de segredo, banco, log ou planilha ---
git diff --cached --name-only | findstr /I /R "\.env$ \.db$ \.log$ \.xlsx$ \.csv$ \.jsonl$ tokens" >nul
if not errorlevel 1 (
  echo.
  echo [BLOQUEADO] Arquivo proibido na lista abaixo. Nada foi enviado.
  git diff --cached --name-only | findstr /I /R "\.env$ \.db$ \.log$ \.xlsx$ \.csv$ \.jsonl$ tokens"
  git reset >nul
  pause & exit /b 1
)

echo.
echo ===== O que vai subir =====
git diff --cached --stat
echo.
set /p OK=Confirma publicar no GitHub? (S/N): 
if /I not "%OK%"=="S" (git reset >nul & echo Cancelado. & pause & exit /b 0)

git commit -m "Atualizacao do portfolio Fermento - %date%"
git push -u origin main
echo.
echo Pronto. Confira no navegador o seu repositorio.
pause
