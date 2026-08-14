import logging
logger = logging.getLogger(__name__)
import os
import uuid
import boto3
from werkzeug.utils import secure_filename
from flask import current_app

# Allowed extensions to prevent malicious uploads
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'avif'}
ALLOWED_DIGITAL_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'avif', 'pdf', 'zip', 'rar', '7z', 'mp3', 'mp4', 'mov', 'epub', 'docx', 'xlsx', 'txt', 'svg'}

def allowed_file(filename, is_digital=False):
    exts = ALLOWED_DIGITAL_EXTENSIONS if is_digital else ALLOWED_IMAGE_EXTENSIONS
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in exts

def save_uploaded_file(file_obj, subfolder="general", is_digital=False):
    """
    Saves an uploaded file to AWS S3 if credentials exist, otherwise falls back to local storage.
    Supports digital product assets (PDF, ZIP, MP3, MP4, etc.) up to 50MB when is_digital=True or subfolder='digital_products'.
    """
    if not file_obj or not file_obj.filename:
        return None

    if subfolder == "digital_products":
        is_digital = True

    # Check file size (50MB for digital products, 10MB for images)
    MAX_FILE_SIZE = (50 * 1024 * 1024) if is_digital else (10 * 1024 * 1024)
    file_obj.seek(0, 2)  # Seek to end
    file_size = file_obj.tell()
    file_obj.seek(0)  # Reset to beginning
    if file_size > MAX_FILE_SIZE:
        limit_mb = 50 if is_digital else 10
        raise ValueError(f"File is too large ({file_size // (1024*1024)}MB). Maximum allowed size is {limit_mb}MB.")
        
    if not allowed_file(file_obj.filename, is_digital=is_digital):
        if is_digital:
            raise ValueError("Invalid file format. Allowed: PDF, ZIP, MP3, MP4, EPUB, DOCX, XLSX, TXT, images.")
        else:
            raise ValueError("Invalid file type. Only image files (PNG, JPG, WEBP) are allowed.")
        
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
