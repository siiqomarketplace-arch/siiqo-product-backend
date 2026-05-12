import os
from app import create_app

# AWS Elastic Beanstalk expects the application callable to be named 'application'
config_name = os.environ.get('FLASK_ENV', 'production')
application = create_app(config_name)

if __name__ == '__main__':
    # Local fallback
    application.run(host='0.0.0.0', port=5000)
