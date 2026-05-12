@echo off
set FLASK_ENV=development
set SECRET_KEY=test-secret
set JWT_SECRET_KEY=test-jwt-secret
cd /d "%~dp0"
venv\Scripts\python.exe -c "from app import create_app; app=create_app(); rules=[str(r) for r in app.url_map.iter_rules() if str(r).startswith('/api')]; print(chr(10).join(sorted(rules))); print(chr(10)+'TOTAL:',len(rules))"
