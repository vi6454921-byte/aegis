@echo off
chcp 65001 >nul
title Бот Аегис
cd /d "%~dp0"
if not exist "app\__main__.py" goto notextracted

rem компоненты ставятся при первом запуске и заново — если после обновления изменился requirements.txt
if not exist ".venv\.installed" goto install
fc /b "requirements.txt" ".venv\.installed" >nul 2>&1
if not errorlevel 1 goto run

:install
rem ---- ищем Python и ставим компоненты в папку .venv
rem (вывод не прячем: установщик Python при первом вызове может что-то спросить)
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" || goto nopython

echo Устанавливаю нужные компоненты (первый запуск или обновление), это займёт 1-3 минуты...
echo.
if not exist ".venv\Scripts\python.exe" (
    %PY% -m venv .venv || goto failed
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || goto failed
copy /y "requirements.txt" ".venv\.installed" >nul || goto failed
cls

:run
set "WEB_HOST=127.0.0.1"
echo.
echo   Бот запускается. Пока это окно открыто, бот работает.
echo   Чтобы остановить бота, просто закройте окно.
echo.
".venv\Scripts\python.exe" -m app
echo.
echo Бот остановлен.
pause
exit /b 0

:nopython
echo.
echo Не найден Python 3.12 или новее.
echo Скачайте его с https://www.python.org/downloads/ и установите,
echo потом снова дважды щёлкните этот файл.
echo.
pause
exit /b 1

:failed
echo.
echo Не получилось установить компоненты. Проверьте интернет и снова запустите этот файл.
echo.
pause
exit /b 1

:notextracted
echo Сначала распакуйте архив: правой кнопкой по скачанному zip-файлу, затем «Извлечь все»,
echo и запустите start_windows из распакованной папки.
echo.
pause
exit /b 1
