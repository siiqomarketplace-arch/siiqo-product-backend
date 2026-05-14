# Siiqo Backend - Production Deployment Guide

## 📋 Overview

This backend is ready for deployment to AWS Elastic Beanstalk. All necessary configuration files are in place.

## ✅ What's Been Prepared

### Configuration Files
- ✅ **Procfile** - Gunicorn configuration for production
- ✅ **.ebignore** - Excludes unnecessary files from deployment
- ✅ **.gitignore** - Prevents sensitive files from being committed
- ✅ **application.py** - Entry point for Elastic Beanstalk
- ✅ **requirements.txt** - All Python dependencies
- ✅ **.env.example** - Template for environment variables

### Documentation
- ✅ **DEPLOYMENT_CHECKLIST.md** - Complete deployment checklist
- ✅ **DEPLOY_COMMANDS.md** - Quick reference for all commands
- ✅ **README_DEPLOYMENT.md** - This file

### Scripts
- ✅ **prepare_deployment.bat** - Automated pre-deployment checks

## 🚀 Quick Start (3 Steps)

### Step 1: Run Preparation Script
```bash
cd "c:\Users\RABONY GLOBALS\Downloads\Siiqo prodcut\Siiqo backend"
prepare_deployment.bat
```

### Step 2: Commit and Push
```bash
git add .
git commit -m "Backend ready for production"
git push origin main
```

### Step 3: Deploy to Elastic Beanstalk
```bash
# First time only
eb init -p python-3.11 siiqo-backend --region us-east-1
eb create siiqo-backend-prod

# Set environment variables (IMPORTANT!)
eb setenv SECRET_KEY="your-secret-key" JWT_SECRET_KEY="your-jwt-key" FLASK_ENV="production" DATABASE_URL="postgresql://user:pass@host:5432/db"

# Deploy
eb deploy

# Run migrations
eb ssh
source /var/app/venv/*/bin/activate
cd /var/app/current
flask db upgrade
exit
```

## ⚙️ Environment Variables (CRITICAL)

You MUST set these in AWS Elastic Beanstalk:

### Required
- `SECRET_KEY` - Flask secret key (generate a long random string)
- `JWT_SECRET_KEY` - JWT secret key (generate another long random string)
- `FLASK_ENV` - Set to "production"
- `DATABASE_URL` - PostgreSQL connection string
- `CORS_ORIGINS` - Your frontend domains (comma-separated)

### Email Configuration
- `MAIL_SERVER` - Your email server
- `MAIL_PORT` - Email port (usually 465 or 587)
- `MAIL_USERNAME` - Email username
- `MAIL_PASSWORD` - Email password
- `MAIL_DEFAULT_SENDER` - Default sender email

### Payment Integration
- `PAYSCROW_API_KEY` - PayScrow API key
- `PAYSCROW_WEBHOOK_SECRET` - PayScrow webhook secret

### Optional
- `REDIS_URL` - Redis connection string (for rate limiting)
- `AWS_ACCESS_KEY_ID` - AWS access key (for S3 uploads)
- `AWS_SECRET_ACCESS_KEY` - AWS secret key
- `AWS_S3_BUCKET_NAME` - S3 bucket name
- `AWS_REGION` - AWS region

## 🔐 Security Checklist

Before deploying, ensure:
- [ ] `.env` file is NOT committed (check .gitignore)
- [ ] All secrets are in environment variables, not code
- [ ] Database has a strong password
- [ ] CORS is properly configured
- [ ] SSL/TLS will be enabled in production
- [ ] Rate limiting is configured

## 📊 Post-Deployment Verification

After deployment, verify:
1. Application is running: `eb open`
2. Health is good: `eb health`
3. No errors in logs: `eb logs`
4. Database is connected
5. API endpoints are responding
6. Email sending works
7. Payment integration works

## 🔄 Updating After Deployment

When you make code changes:
```bash
git add .
git commit -m "Your changes"
git push origin main
eb deploy
```

If you change database models:
```bash
eb ssh
source /var/app/venv/*/bin/activate
cd /var/app/current
flask db upgrade
exit
```

## 📁 Project Structure

```
Siiqo backend/
├── app/                    # Application code
│   ├── models/            # Database models
│   ├── routes/            # API routes
│   └── ...
├── migrations/            # Database migrations
├── application.py         # Entry point (EB expects this)
├── requirements.txt       # Python dependencies
├── Procfile              # Gunicorn configuration
├── .ebignore             # EB deployment exclusions
├── .gitignore            # Git exclusions
├── .env.example          # Environment variables template
└── DEPLOYMENT_*.md       # Deployment documentation
```

## 🆘 Troubleshooting

### Deployment Fails
1. Check logs: `eb logs`
2. Verify environment variables: `eb printenv`
3. Check syntax: Run `prepare_deployment.bat`

### Application Not Starting
1. Check Procfile is correct
2. Verify gunicorn is in requirements.txt
3. Check application.py has no errors

### Database Connection Issues
1. Verify DATABASE_URL format
2. Check database security groups
3. Ensure database is accessible from EB

### 502 Bad Gateway
1. Check gunicorn is running: `eb ssh` then `ps aux | grep gunicorn`
2. Check application logs: `eb logs`
3. Verify Procfile configuration

## 📞 Need Help?

1. Review **DEPLOYMENT_CHECKLIST.md** for detailed steps
2. Check **DEPLOY_COMMANDS.md** for command reference
3. Run `prepare_deployment.bat` to check for issues
4. Check AWS Elastic Beanstalk documentation

## ⚠️ Important Reminders

1. **Never commit .env file** - Use EB environment variables
2. **Use PostgreSQL in production** - Not SQLite
3. **Run migrations after deployment** - If database changed
4. **Monitor logs after deployment** - Check for errors
5. **Test thoroughly before deploying** - Avoid production issues

---

## 🎉 You're Ready!

Your backend is now prepared for deployment. Follow the Quick Start guide above to deploy to AWS Elastic Beanstalk.

Good luck! 🚀
