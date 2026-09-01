@echo off
REM peek launcher - so any shell can call `C:\peek\peek.cmd <url>` without python on the line.
python "%~dp0peek.py" %*
