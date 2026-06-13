import logging
logger = logging.getLogger(__name__)
import os
import uuid
import boto3
from werkzeug.utils import secure_filename
from flask import current_app

# Allowed extensions to prevent malicious uploads
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'avif'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def save_uploaded_file(file_obj, subfolder="general"):
    """
    Saves an uploaded file to AWS S3 if credentials exist, otherwise falls back to local storage.
    """
    if not file_obj or not file_obj.filename:
        return None

    # Read content into memory to check size (max 10MB per file)
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
    file_obj.seek(0, 2)  # Seek to end
    file_size = file_obj.tell()
    file_obj.seek(0)  # Reset to beginning
    if file_size > MAX_FILE_SIZE:
        raise ValueError(f"File is too large ({file_size // (1024*1024)}MB). Maximum allowed size is 10MB.")
        
    if not allowed_file(file_obj.filename):
        raise ValueError("Invalid file type. Only image files are allowed.")
        
    ext = file_obj.filename.rsplit('.', 1)[1].lower()
    unique_filename = f"{uuid.uuid4().hex}.{ext}"
    s3_key = f"uploads/{subfolder}/{unique_filename}"
    
    aws_access_key = current_app.config.get('AWS_ACCESS_KEY_ID')
    aws_secret_key = current_app.config.get('AWS_SECRET_ACCESS_KEY')
    bucket_name = current_app.config.get('AWS_S3_BUCKET_NAME')
    region = current_app.config.get('AWS_REGION', 'us-east-1')
    
    if aws_access_key and aws_secret_key and bucket_name:
        try:
            s3_client = boto3.client(
                's3',
                aws_access_key_id=aws_access_key,
                aws_secret_access_key=aws_secret_key,
                region_name=region
            )
            content_type = file_obj.content_type or 'application/octet-stream'
            s3_client.upload_fileobj(
                file_obj,
                bucket_name,
                s3_key,
                ExtraArgs={"ContentType": content_type}
            )
            return f"https://{bucket_name}.s3.{region}.amazonaws.com/{s3_key}"
        except Exception as e:
            logger.info(f"[WARN] S3 upload failed, falling back to local storage: {e}")
            # Fall through to local storage below
            
    # Fallback to local storage if S3 is not configured
    upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', subfolder)
    os.makedirs(upload_dir, exist_ok=True)
    
    file_path = os.path.join(upload_dir, unique_filename)
    file_obj.save(file_path)
    
    return f"/static/uploads/{subfolder}/{unique_filename}"
