@echo off
chcp 936 >nul
cd /d "%~dp0"
title 图包素材审批工具

rem ---- 找一个真的能用的 Python（跳过 Microsoft Store 的假 python3 占位符）----
set "PYEXE="
for %%C in (python py python3) do (
  if not defined PYEXE (
    %%C -c "import flask, PIL" >nul 2>nul && set "PYEXE=%%C"
  )
)
if not defined PYEXE (
  for %%C in (python py) do (
    if not defined PYEXE (
      %%C --version >nul 2>nul && set "PYEXE=%%C"
    )
  )
)
if not defined PYEXE (
  echo.
  echo   [错误] 没找到可用的 Python。
  echo   请安装 Python 3.10 以上版本，安装时勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

rem ---- 缺依赖就装 ----
%PYEXE% -c "import flask, PIL" >nul 2>nul
if errorlevel 1 (
  echo   缺少依赖，正在安装 flask 和 pillow ...
  %PYEXE% -m pip install flask pillow
  %PYEXE% -c "import flask, PIL" >nul 2>nul
  if errorlevel 1 (
    echo.
    echo   [错误] 依赖安装失败，请手动执行： %PYEXE% -m pip install flask pillow
    echo.
    pause
    exit /b 1
  )
)

echo.
echo   启动中（用 %PYEXE%）...  这个窗口要一直开着，关掉窗口就等于关掉工具。
echo.

set /a RESTARTS=0
:loop
%PYEXE% app.py %*
set CODE=%ERRORLEVEL%
if "%CODE%"=="0" goto :done
if "%CODE%"=="130" goto :done
set /a RESTARTS+=1
if %RESTARTS% GEQ 20 goto :toomany
echo.
echo   [警告] 工具进程意外退出了（第 %RESTARTS% 次，退出码 %CODE%），3 秒后自动重启 ...
echo           想彻底停下来就按 Ctrl+C。
echo.
timeout /t 3 /nobreak >nul
goto :loop

:toomany
echo.
echo   [错误] 连续多次异常退出，已停止自动重启。请把上面的报错截图给开发者。
echo.
pause
exit /b 1

:done
echo.
echo   工具已退出。
pause
