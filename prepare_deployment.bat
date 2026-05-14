@echo off
echo ========================================
echo Siiqo Backend - Deployment Preparation
echo ========================================
echo.

echo [1/5] Checking Python syntax...
python -m py_compile application.py
if %errorlevel% neq 0 (
    echo ERROR: application.py has syntax errors!
    pause
    exit /b 1
)
echo ✓ application.py syntax OK

echo.
echo [2/5] Checking if .env is in .gitignore...
findstr /C:".env" .gitignore >nul
if %errorlevel% neq 0 (
    echo WARNING: .env might not be in .gitignore!
    echo Please verify .gitignore excludes .env
    pause
)
echo ✓ .gitignore configured

echo.
echo [3/5] Checking required files...
if not exist "Procfile" (
    echo ERROR: Procfile not found!
    pause
    exit /b 1
)
echo ✓ Procfile exists

if not exist "requirements.txt" (
    echo ERROR: requirements.txt not found!
    pause
    exit /b 1
)
echo ✓ requirements.txt exists

if not exist "application.py" (
    echo ERROR: application.py not found!
    pause
    exit /b 1
)
echo ✓ application.py exists

if not exist ".ebignore" (
    echo ERROR: .ebignore not found!
    pause
    exit /b 1
)
echo ✓ .ebignore exists

echo.
echo [4/5] Checking git status...
git status --short
if %errorlevel% neq 0 (
    echo ERROR: Not a git repository!
    pause
    exit /b 1
)

echo.
echo [5/5] Checking for uncommitted changes...
git diff-index --quiet HEAD --
if %errorlevel% neq 0 (
    echo WARNING: You have uncommitted changes!
    echo Please commit all changes before deploying.
    git status --short
    echo.
    echo Do you want to continue anyway? (y/n)
    set /p continue=
    if /i not "%continue%"=="y" (
        echo Deployment preparation cancelled.
        pause
        exit /b 1
    )
)

echo.
echo ========================================
echo ✓ Deployment preparation complete!
echo ========================================
echo.
echo Next steps:
echo 1. Review DEPLOYMENT_CHECKLIST.md
echo 2. Set environment variables in AWS EB
echo 3. Run: git add . && git commit -m "Deploy" && git push
echo 4. Run: eb deploy
echo.
pause
