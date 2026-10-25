@echo off
chcp 65001
setlocal

:: 获取当前脚本所在根目录
set "ROOT=%~dp0"

pyinstaller ^
  --distpath "%ROOT%bin" ^
  --workpath "%ROOT%build" ^
  --specpath "%ROOT%build" ^
  --add-data "%ROOT%model_out;model_out" ^
  --add-data "%ROOT%dataset\job_dataset_with_pos.json;." ^
  --add-data "%ROOT%dataset\train.json;." ^
  --add-data "%ROOT%dataset\test.json;." ^
  --noconfirm ^
  --console ^
  run_model.py

echo.
echo 打包完成！输出路径：%ROOT%bin\run_model.exe
echo.
pause