# Pair Desk installer for Windows (PowerShell 5.1 or 7).
#
#   irm https://raw.githubusercontent.com/rennerdo30/pair-desk/main/install.ps1 | iex
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/rennerdo30/pair-desk/main/install.ps1))) update
#       (arguments: install, update or uninstall, then claude codex opencode antigravity muse, --dry-run)
#
# It needs Python 3.11+. It downloads Pair Desk (the main branch, or $env:PAIR_DESK_REF; or the
# .zip named by $env:PAIR_DESK_ARCHIVE) into a
# temporary folder and runs `desk.py install` from there, which shows exactly what it will do for
# supported agent tools and asks before each one. Non-Claude integrations get a copy in
# %LOCALAPPDATA%\Programs\AgentPairProgramming (or $env:PAIR_DESK_APP). The download is deleted
# afterwards. It never answers the questions for you.

& {
    $ErrorActionPreference = "Stop"
    $repo = "rennerdo30/pair-desk"
    $ref = if ($env:PAIR_DESK_REF) { $env:PAIR_DESK_REF } else { "main" }
    $probe = "import sys; sys.exit(sys.version_info < (3, 11))"

    function Test-Python([string[]]$cmd) {
        if (-not (Get-Command $cmd[0] -ErrorAction SilentlyContinue)) { return $false }
        $rest = @($cmd | Select-Object -Skip 1)
        try { & $cmd[0] @rest -I -S -c $probe 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) } catch { return $false }
    }

    $python = $null
    $candidates = @()
    if ($env:PAIR_DESK_PYTHON) { $candidates += ,@($env:PAIR_DESK_PYTHON) }
    $candidates += ,@("py", "-3")
    $candidates += ,@("python")
    $candidates += ,@("python3")
    foreach ($c in $candidates) { if (Test-Python $c) { $python = $c; break } }
    if (-not $python) {
        Write-Host "Pair Desk needs Python 3.11 or newer: install it from https://www.python.org/downloads/ (or winget install Python.Python.3.13), then run this again." -ForegroundColor Red
        return
    }
    $pyArgs = @($python | Select-Object -Skip 1)
    Write-Host ("Using " + (& $python[0] @pyArgs -c "import sys; print(sys.executable, sys.version.split()[0])"))

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("pair-desk-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $zip = Join-Path $tmp "src.zip"
        if ($env:PAIR_DESK_ARCHIVE -and (Test-Path $env:PAIR_DESK_ARCHIVE)) {
            # a local .zip (testing a release before it is pushed)
            Copy-Item $env:PAIR_DESK_ARCHIVE $zip
        } else {
            $url = if ($env:PAIR_DESK_ARCHIVE) { $env:PAIR_DESK_ARCHIVE } else { "https://github.com/$repo/archive/refs/heads/$ref.zip" }
            Write-Host "Downloading $url"
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        }
        Expand-Archive -Path $zip -DestinationPath $tmp
        $desk = Get-ChildItem -Path $tmp -Directory | ForEach-Object { Join-Path $_.FullName "desk.py" } | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $desk) { throw "The download did not contain desk.py." }
        $rest = @($args)
        $action = "install"
        if ($rest.Count -gt 0 -and @("install", "update", "uninstall") -contains $rest[0]) {
            $action = $rest[0]
            $rest = @($rest | Select-Object -Skip 1)
        }
        & $python[0] @pyArgs $desk $action @rest
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
} @args
