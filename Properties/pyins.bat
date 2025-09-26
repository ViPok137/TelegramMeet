@echo off
set "URL=https://www.python.org/ftp/python/3.13.7/python-3.13.7-amd64.exe"
set "InstallerName=python-3.13.7-amd64.exe"
set "LogFile=python_install.log"

echo Downloading Python 3.13.7...
powershell -Command "Invoke-WebRequest -Uri '%URL%' -OutFile '%InstallerName%'"
if errorlevel 1 (
    echo Error downloading the file. Please check your internet connection.
    pause
    exit /b 1
)

echo Starting silent installation to default location...
start /wait "" "%InstallerName%" /quiet PrependPath=1 Include_test=0 Include_launcher=1 /log "%LogFile%"

echo Installation complete.

echo Deleting temporary installation files...
if exist "%InstallerName%" (
    del "%InstallerName%"
)
if exist "%LogFile%" (
    del "%LogFile%"
)
pip install python-telegram-bot==13.15
pip install pillow
echo Installing required Python packages...
echo Installation complete.
echo Operation finished.