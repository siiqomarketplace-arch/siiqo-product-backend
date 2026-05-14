# Quick Deployment Commands

## 🚀 First Time Deployment

### 1. Prepare Backend
```bash
cd "c:\Users\RABONY GLOBALS\Downloads\Siiqo prodcut\Siiqo backend"
prepare_deployment.bat
```

### 2. Commit and Push
```bash
git add .
git commit -m "Backend ready for production deployment"
git push origin main
```

### 3. Initialize EB (First Time Only)
```bash
eb init
```
- Select region (e.g., us-east-1)
- Select platform: Python 3.11
- Application name: siiqo-backend
- Setup SSH: Yes (recommended)

### 4. Create Environment (First Time Only)
```bash
eb create siiqo-backend-prod
```

### 5. Set Environment Variables
```bash
eb setenv SECRET_KEY="CHANGE_THIS_LONG_RANDOM_STRING" JWT_SECRET_KEY="CHANGE_THIS_ANOTHER_RANDOM_STRING" FLASK_ENV="production" DATABASE_URL="postgresql://username:password@host:5432/database" CORS_ORIGINS="https://siiqo.com,https://www.siiqo.com"
```

### 6. Deploy
```bash
eb deploy
```

### 7. Run Migrations
```bash
eb ssh
source /var/app/venv/*/bin/activate
cd /var/app/current
flask db upgrade
exit
```

### 8. Open Application
```bash
eb open
```

---

## 🔄 Subsequent Deployments

### Quick Deploy (After Code Changes)
```bash
cd "c:\Users\RABONY GLOBALS\Downloads\Siiqo prodcut\Siiqo backend"
git add .
git commit -m "Your commit message"
git push origin main
eb deploy
```

### Check Status
```bash
eb status
eb health
```

### View Logs
```bash
eb logs
eb logs --stream
```

---

## 🔧 Useful Commands

### Environment Management
```bash
eb list                    # List all environments
eb use siiqo-backend-prod  # Switch to environment
eb printenv                # Show environment variables
eb setenv KEY=value        # Set environment variable
```

### Application Management
```bash
eb open                    # Open app in browser
eb ssh                     # SSH into instance
eb restart                 # Restart application
eb terminate               # Terminate environment (CAREFUL!)
```

### Monitoring
```bash
eb health                  # Check health status
eb logs                    # View recent logs
eb logs --stream           # Stream logs in real-time
eb events                  # View recent events
```

### Database
```bash
eb ssh
source /var/app/venv/*/bin/activate
cd /var/app/current
flask db upgrade           # Run migrations
flask db current           # Check current migration
exit
```

---

## ⚠️ Important Notes

1. **Always test locally first** before deploying
2. **Commit all changes** before deploying
3. **Set environment variables** in EB, not in code
4. **Run migrations** after deploying database changes
5. **Monitor logs** after deployment for errors
6. **Use PostgreSQL** in production, not SQLite

---

## 🆘 Emergency Rollback

If deployment fails or causes issues:

```bash
# View deployment history
eb deploy --version

# Rollback to previous version
eb deploy --version <previous-version-number>
```

---

## 📞 Support

If you encounter issues:
1. Check logs: `eb logs`
2. Check health: `eb health`
3. SSH into instance: `eb ssh`
4. Review DEPLOYMENT_CHECKLIST.md
