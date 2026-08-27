@echo off
cd /d "%~dp0"
echo Starting Harness Dashboard...
echo Open your browser to http://localhost:8501 after startup
echo.
python -m streamlit run harness_dashboard.py
pause
