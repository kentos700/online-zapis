$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

if (Test-Path "build") {
    Remove-Item -Recurse -Force "build"
}

if (Test-Path "dist") {
    Remove-Item -Recurse -Force "dist"
}

if (Test-Path "ClinicFlow.spec") {
    Remove-Item -Force "ClinicFlow.spec"
}

pyinstaller `
    --noconfirm `
    --clean `
    --onefile `
    --optimize 2 `
    --name "ClinicFlow" `
    --add-data "templates;templates" `
    --add-data "static;static" `
    --exclude-module IPython `
    --exclude-module matplotlib `
    --exclude-module matplotlib_inline `
    --exclude-module numpy `
    --exclude-module PIL `
    --exclude-module tkinter `
    --exclude-module jedi `
    --exclude-module parso `
    --exclude-module pygments `
    --exclude-module prompt_toolkit `
    --exclude-module traitlets `
    --exclude-module jupyter_client `
    --exclude-module jupyter_core `
    --exclude-module zmq `
    --exclude-module tornado `
    --exclude-module pyreadline3 `
    --exclude-module pexpect `
    --exclude-module stack_data `
    --exclude-module asttokens `
    --exclude-module pure_eval `
    --exclude-module psutil `
    launcher.py
