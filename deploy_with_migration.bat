@echo off
echo ========================================
echo DEPLOYING BACKEND WITH AUTO-MIGRATION
echo ========================================
echo.

echo Step 1: Adding migration config...
git add .ebextensions/01_flask_migrate.config
echo.

echo Step 2: Committing...
git commit -m "Add automatic database migration on deployment"
echo.

echo Step 3: Pushing to GitHub...
git push origin main
echo.

echo ========================================
echo DONE! Migration will run automatically
echo ========================================
echo.
echo AWS Elastic Beanstalk will now:
echo 1. Detect the new commit
echo 2. Deploy the code
echo 3. Run "flask db upgrade" automatically
echo.
echo Wait 10-15 minutes for deployment to complete.
echo.
pause
