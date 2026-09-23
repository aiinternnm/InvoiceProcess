@echo off
rem Run the invoice pipeline. Options you can pass:
rem   --dry-run            validate + extract but write nothing
rem   --step                pause after each file and ask whether to continue
rem   --limit N             process at most the first N files, then stop (0 = all)
rem   --mock-extract        skip the AI model, inject a sample invoice (still needs Drive)
rem   --test-model         ping the Qwen/LM Studio endpoint and exit
rem   --skip-model-check   proceed even if the model is unreachable
rem   --folder <link>      override the Drive folder for this run
cd /d "%~dp0"
python main.py %*
if errorlevel 1 pause