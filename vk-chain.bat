@echo off
rem pythonw is the windowed interpreter, so there is no console behind the
rem app. `start` is deliberately not used here: quoting its empty window
rem title is what made Windows try to open "\\" instead.
cd /d "%~dp0"
pythonw.exe "%~dp0app.py"
