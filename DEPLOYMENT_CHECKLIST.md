# Siiqo Backend - Elastic Beanstalk Deployment Checklist

## ✅ Pre-Deployment Checklist

### 1. Environment Variables (CRITICAL)
- [ ] Set `SECRET_KEY` in AWS EB environment
- [ ] Set `JWT_SECRET_KEY` in AWS EB environment
- [ ] Set `DATABASE_URL` (PostgreSQL connection string)
- [ ] Set `FLASK_ENV=production`
- [ ] Set `CORS_ORIGINS` (include your frontend domains)
- [ ] Set email configuration (MAIL_SERVER, MAIL_USERNAME, MAIL_PASSWORD)
- [ ] Set `PAYSCROW_API_KEY` and `PAYSCROW_WEBHOOK_SECRET`
- [ ] Optional: Set Redis URL if using rate limiting
- [ ] Optional: Set AWS S3 credentials if using file uploads

### 2. Database
- [ ] PostgreSQL database created and accessible
- [ ] Database migrations are up to date
- [ ] Test database connection from EB environment

### 3. Code Quality
- [ ] All Python files have no syntax errors
- [ ] All routes are tested and working
- [ ] No hardcoded secrets in code
- [ ] `.env` file is NOT committed (check .gitignore)

### 4. Dependencies
- [ ] `requirements.txt` is up to date
- [ ] All dependencies are compatible with Python 3.11+
- [ ] `gunicorn` is included in requirements.txt

### 5. Configuration Files
- [ ] `Procfile` exists and is correct
- [ ] `.ebignore` excludes unnecessary files
- [ ] `application.py` is the entry point
- [ ] `.gitignore` excludes sensitive files

### 6. Git Repository
- [ ] All changes are committed
- [ ] Test files are excluded from deployment
- [ ] Repository is clean (no uncommitted changes)

## 🚀 Deployment Steps

### Step 1: Commit All Changes
```bash
cd "c:\Users\RABONY GLOBALS\Downloads\Siiqo prodcut\Siiqo backend"
git add .
git commit -m "Prepare backend for production deployment"
git push origin main
```

### Step 2: Initialize Elastic Beanstalk (First Time Only)
```bash
eb init -p python-3.11 siiqo-backend --region us-east-1
```

### Step 3: Create Environment (First Time Only)
```bash
eb create siiqo-backend-prod --database.engine postgres --database.username siiqoadmin
```

### Step 4: Set Environment Variables
```bash
eb setenv SECRET_KEY="your-secret-key-here" \
  JWT_SECRET_KEY="your-jwt-secret-here" \
  FLASK_ENV="production" \
  DATABASE_URL="postgresql://user:pass@host:5432/dbname" \
  CORS_ORIGINS="https://siiqo.com,https://www.siiqo.com" \
  MAIL_SERVER="mail.siiqo.com" \
  MAIL_PORT="465" \
  MAIL_USERNAME="support@siiqo.com" \
  MAIL_PASSWORD="your-mail-password" \
  MAIL_DEFAULT_SENDER="support@siiqo.com" \
  PAYSCROW_API_KEY="your-payscrow-key" \
  PAYSCROW_WEBHOOK_SECRET="your-webhook-secret"
```

### Step 5: Deploy
```bash
eb deploy
```

### Step 6: Run Database Migrations
```bash
eb ssh
cd /var/app/current
source /var/app/venv/*/bin/activate
flask db upgrade
exit
```

### Step 7: Verify Deployment
```bash
eb open
eb status
eb health
eb logs
```

## 🔧 Post-Deployment

### Monitor Application
- [ ] Check application logs: `eb logs`
- [ ] Verify health status: `eb health`
- [ ] Test API endpoints
- [ ] Verify database connectivity
- [ ] Test email sending
- [ ] Test payment integration

### Set Up Monitoring
- [ ] Configure CloudWatch alarms
- [ ] Set up error notifications
- [ ] Monitor application performance

## 🆘 Troubleshooting

### If deployment fails:
1. Check logs: `eb logs`
2. Verify environment variables: `eb printenv`
3. Check application health: `eb health`
4. SSH into instance: `eb ssh`

### Common Issues:
- **Database connection failed**: Check DATABASE_URL format
- **Module not found**: Verify requirements.txt
- **Permission denied**: Check IAM roles
- **502 Bad Gateway**: Check Procfile and gunicorn config

## 📝 Important Notes

1. **Never commit .env file** - Use EB environment variables
2. **Use PostgreSQL in production** - Not SQLite
3. **Enable HTTPS** - Configure SSL certificate in EB
4. **Set up backups** - Configure RDS automated backups
5. **Monitor costs** - Check AWS billing regularly

## 🔐 Security Reminders

- [ ] All secrets are in environment variables, not code
- [ ] CORS is properly configured
- [ ] Rate limiting is enabled
- [ ] Database has strong password
- [ ] SSL/TLS is enabled
- [ ] Security groups are properly configured
