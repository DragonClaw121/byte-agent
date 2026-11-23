# Instala (ou atualiza) o Bit no PC sem baixar nenhum .exe desconhecido e sem precisar de administrador.
# Abra o PowerShell e cole:
#     irm https://raw.githubusercontent.com/DragonClaw121/byte-agent/main/desktop/instalar.ps1 | iex
# O que ele faz:
#   1. Python 3.12 oficial (python.org, assinado pela Python Software Foundation), só para o seu usuário;
#   2. as bibliotecas mss (captura de tela) e pillow (imagens), do PyPI;
#   3. o app (bit_pc.py) em %LOCALAPPDATA%\BitPC e os atalhos "Bit no PC" no Menu Iniciar e na Área de Trabalho.
# Rodar de novo atualiza o app.

function Install-BitPC {
    $ErrorActionPreference = "Stop"
    $ProgressPreference = "SilentlyContinue"            # o download fica muito mais rápido sem a barra de progresso
    $origem = if ($env:BITPC_ORIGEM) { $env:BITPC_ORIGEM } else { "https://raw.githubusercontent.com/DragonClaw121/byte-agent/main/desktop" }
    $pasta = Join-Path $env:LOCALAPPDATA "BitPC"
    $pyDir = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312"
    $py = Join-Path $pyDir "python.exe"
    $pyw = Join-Path $pyDir "pythonw.exe"
    New-Item -ItemType Directory -Force -Path $pasta | Out-Null

    Write-Host "[Bit no PC] 1/3 Python 3.12 (oficial, só para o seu usuário)..." -ForegroundColor Cyan
    if (-not (Test-Path $py)) {
        $ver = "3.12.8"
        $inst = Join-Path $env:TEMP "python-$ver-amd64.exe"
        Invoke-WebRequest "https://www.python.org/ftp/python/$ver/python-$ver-amd64.exe" -OutFile $inst -UseBasicParsing
        $sig = Get-AuthenticodeSignature $inst
        if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Python Software Foundation") {
            throw "O instalador do Python baixado não tem a assinatura da Python Software Foundation. Instalação cancelada."
        }
        $p = Start-Process $inst -Wait -PassThru -ArgumentList @("/quiet", "InstallAllUsers=0", "Include_tcltk=1",
            "Include_launcher=0", "Include_test=0", "Include_doc=0", "PrependPath=0", "Shortcuts=0", "AssociateFiles=0")
        Remove-Item $inst -Force -ErrorAction SilentlyContinue
        if ($p.ExitCode -ne 0 -or -not (Test-Path $py)) { throw "O instalador do Python terminou com erro ($($p.ExitCode))." }
    } else {
        Write-Host "    já instalado"
    }

    Write-Host "[Bit no PC] 2/3 Bibliotecas (captura de tela e imagens)..." -ForegroundColor Cyan
    & $py -m pip install --disable-pip-version-check --no-warn-script-location -q "mss==9.0.2" "pillow==11.0.0"
    if ($LASTEXITCODE -ne 0) { throw "O pip não conseguiu instalar as bibliotecas (internet ou proxy da empresa?)." }

    Write-Host "[Bit no PC] 3/3 O app e os atalhos..." -ForegroundColor Cyan
    foreach ($arq in @("bit_pc.py", "bit.ico")) {
        $destino = Join-Path $pasta $arq
        if ($origem -match "^https://") {
            Invoke-WebRequest "$origem/$arq" -OutFile $destino -UseBasicParsing
        } else {
            Copy-Item (Join-Path $origem $arq) $destino -Force
        }
    }
    $app = Join-Path $pasta "bit_pc.py"
    $shell = New-Object -ComObject WScript.Shell
    $lugares = @((Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"), [Environment]::GetFolderPath("Desktop"))
    foreach ($lugar in $lugares) {
        if (-not $lugar) { continue }
        $lnk = $shell.CreateShortcut((Join-Path $lugar "Bit no PC.lnk"))
        $lnk.TargetPath = $pyw
        $lnk.Arguments = "`"$app`""
        $lnk.WorkingDirectory = $pasta
        $lnk.IconLocation = (Join-Path $pasta "bit.ico")
        $lnk.Description = "Bit no PC: conversa, olhar a tela e ajuda ao vivo"
        $lnk.Save()
    }
    Write-Host "[Bit no PC] Pronto! Atalho 'Bit no PC' no Menu Iniciar e na Área de Trabalho." -ForegroundColor Green
    Write-Host "            Para conectar: no painel do Bit, aba ⚡ Comandos -> 'Gerar link de conexão' (ou /pc no Telegram)."
    if (-not $env:BITPC_NAO_ABRIR) { Start-Process $pyw -ArgumentList "`"$app`"" -WorkingDirectory $pasta }
}

try {
    Install-BitPC
} catch {
    Write-Host "[Bit no PC] Não deu certo: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "            Se o PC é da empresa, a TI pode estar bloqueando downloads ou scripts."
}
