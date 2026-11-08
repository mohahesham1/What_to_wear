# Wardrobe AI - one-shot setup: installs Git + GitHub CLI (+ Ollama), then creates a
# private GitHub repo and pushes this folder to it.
#
# Run from this folder:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
# Or just double-click setup.bat
#
# Options:  -RepoName wardrobe-ai   -Public   -SkipOllama   -Model qwen3-vl:2b

param(
    [string]$RepoName = "wardrobe-ai",
    [switch]$Public,
    [switch]$SkipOllama,
    [string]$Model = "qwen3-vl:2b"
)

# "Continue" on purpose: in Windows PowerShell 5.1, "Stop" turns native stderr (git/gh progress)
# into fatal errors. Each step checks $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "`nERROR: $msg" -ForegroundColor Red; exit 1 }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Ensure-Tool($exe, $wingetId, $name) {
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        Write-Host "$name already installed."
        return
    }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail "winget not found. Install $name manually, then re-run this script."
    }
    Write-Host "Installing $name..."
    winget install --id $wingetId -e --source winget --accept-package-agreements --accept-source-agreements
    Refresh-Path
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) {
        Fail "$name installed but not on PATH yet. Close this window, open a new one, and re-run."
    }
}

# ------------------------------------------------------------------ 1. tools
Step "Checking tools"
Ensure-Tool git "Git.Git" "Git"
Ensure-Tool gh "GitHub.cli" "GitHub CLI"

# ------------------------------------------------------------------ 2. git identity
Step "Checking Git identity"
$gitName = git config --global user.name
$gitEmail = git config --global user.email
if (-not $gitName) {
    $gitName = Read-Host "Your name for Git commits"
    git config --global user.name "$gitName"
}
if (-not $gitEmail) {
    $gitEmail = Read-Host "Your email for Git commits (tip: use your GitHub noreply address)"
    git config --global user.email "$gitEmail"
}
Write-Host "Committing as $gitName <$gitEmail>"

# ------------------------------------------------------------------ 3. GitHub login
Step "Checking GitHub login"
gh auth status 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "A browser window will open. Sign in as the account that should own the repo."
    gh auth login --hostname github.com --git-protocol https --web
    if ($LASTEXITCODE -ne 0) { Fail "GitHub login failed." }
}
gh auth setup-git | Out-Null
$ghUser = gh api user --jq .login
Write-Host "Signed in to GitHub as $ghUser"

# ------------------------------------------------------------------ 4. safety checks
Step "Safety checks"
foreach ($f in @("server.py", "index.html", ".gitignore")) {
    if (-not (Test-Path $f)) { Fail "$f is missing from $PSScriptRoot" }
}
$ignore = Get-Content .gitignore
foreach ($rule in @(".env", "wardrobe/")) {
    if ($ignore -notcontains $rule) { Fail ".gitignore must contain '$rule' so secrets/photos are never uploaded." }
}

if (-not (Test-Path .git)) {
    git init -b main | Out-Null
}
git add -A
$staged = git diff --cached --name-only
$leaks = $staged | Where-Object { $_ -match '(^|/)\.env$' -or $_ -match '^wardrobe/' }
if ($leaks) {
    git reset -q
    Fail "These files must not be uploaded: $($leaks -join ', ')"
}
Write-Host "Files to upload:"
$staged | ForEach-Object { Write-Host "  $_" }

# ------------------------------------------------------------------ 5. commit + push
Step "Committing"
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -q -m "Wardrobe AI: upload clothes, score outfits, suggest purchases"
} else {
    Write-Host "Nothing new to commit."
}

Step "Creating GitHub repo $ghUser/$RepoName"
$visibility = if ($Public) { "--public" } else { "--private" }
gh repo view "$ghUser/$RepoName" 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Repo already exists - pushing to it."
    $remote = git remote 2>$null
    if ($remote -notcontains "origin") { git remote add origin "https://github.com/$ghUser/$RepoName.git" }
    git push -u origin main
} else {
    gh repo create $RepoName $visibility --source . --remote origin --push `
        --description "Local AI outfit helper: tag clothes with Ollama, score outfits, suggest purchases"
}
if ($LASTEXITCODE -ne 0) { Fail "Push failed." }
Write-Host "Done: https://github.com/$ghUser/$RepoName" -ForegroundColor Green

# ------------------------------------------------------------------ 6. Ollama (optional)
if (-not $SkipOllama) {
    Step "Setting up Ollama + $Model"
    Ensure-Tool ollama "Ollama.Ollama" "Ollama"
    ollama pull $Model
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Model pull failed. Make sure the Ollama app is running and up to date, then run: ollama pull $Model" -ForegroundColor Yellow
    }
}

Step "All set"
Write-Host "Start the app any time by double-clicking start.bat"
