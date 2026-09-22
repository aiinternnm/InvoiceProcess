@echo off
rem Run the invoice pipeline. Options you can pass:
rem   --dry-run            validate + extract but write nothing
rem   --mock-extract       skip the AI model, inject a sample invoice (still needs Drive)
rem   --test-model         ping the Qwen/LM Studio endpoint and exit
rem   --skip-model-check   proceed even if the model is unreachable
rem   --folder <link>      override the Drive folder for this run
cd /d "%~dp0"
python main.py %*
if errorlevel 1 pause