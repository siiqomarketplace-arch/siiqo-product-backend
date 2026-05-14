@echo off
echo ========================================
echo CLEANING BACKEND COMMIT
echo ========================================
echo.

echo Step 1: Reset to 2 commits ago...
git reset --soft HEAD~2
echo.

echo Step 2: Unstage all files...
git reset
echo.

echo Step 3: Stage ONLY the migration file...
git add migrations/versions/ea362e02b64f_update.py
echo.

echo Step 4: Check what's staged...
git status
echo.

echo Step 5: Create clean commit...
git commit -m "Add database migration for timezone and schema updates"
echo.

echo Step 6: Verify the commit...
git log -1 --stat
echo.

echo ========================================
echo DONE! Now you can run: git push origin main
echo ========================================
pause
