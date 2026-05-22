import json
import os
import sys

# Add project root to path so we can import app
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app

def generate_postman_collection():
    app = create_app()
    
    collection = {
        "info": {
            "name": "Siiqo API (Auto-generated)",
            "description": "Complete collection of Siiqo backend endpoints",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
        },
        "variable": [
            {
                "key": "base_url",
                "value": "https://devapi.siiqo.app",
                "type": "string"
            },
            {
                "key": "access_token",
                "value": "",
                "type": "string"
            }
        ],
        "item": [],
        "auth": {
            "type": "bearer",
            "bearer": [
                {
                    "key": "token",
                    "value": "{{access_token}}",
                    "type": "string"
                }
            ]
        }
    }

    folders = {}
    
    with app.app_context():
        for rule in app.url_map.iter_rules():
            if rule.endpoint == 'static':
                continue
                
            # Skip some internal routes if necessary
            path = str(rule)
            methods = [m for m in rule.methods if m not in ['HEAD', 'OPTIONS']]
            
            if not methods:
                continue
                
            # Determine folder based on URL prefix
            parts = path.strip('/').split('/')
            folder_name = "Root"
            if len(parts) > 1 and parts[0] == 'api':
                folder_name = parts[1].capitalize()
            elif len(parts) > 0:
                folder_name = parts[0].capitalize()
                
            if folder_name not in folders:
                folders[folder_name] = {
                    "name": folder_name,
                    "item": []
                }
                
            for method in methods:
                # Basic item structure
                item = {
                    "name": f"{method} {path}",
                    "request": {
                        "method": method,
                        "header": [
                            {
                                "key": "Content-Type",
                                "value": "application/json",
                                "type": "text"
                            }
                        ],
                        "url": {
                            "raw": "{{base_url}}" + path,
                            "host": ["{{base_url}}"],
                            "path": parts
                        }
                    },
                    "response": []
                }
                
                # Add body placeholders for POST/PUT/PATCH
                if method in ['POST', 'PUT', 'PATCH']:
                    item["request"]["body"] = {
                        "mode": "raw",
                        "raw": "{\n    \n}",
                        "options": {
                            "raw": {
                                "language": "json"
                            }
                        }
                    }
                    
                    # Add specific bodies for common auth endpoints to be helpful
                    if path == '/api/auth/register' and method == 'POST':
                        item["request"]["body"]["raw"] = json.dumps({
                            "email": "test@siiqo.com",
                            "password": "Password123!",
                            "first_name": "Test",
                            "last_name": "User"
                        }, indent=4)
                    elif path == '/api/auth/login' and method == 'POST':
                        item["request"]["body"]["raw"] = json.dumps({
                            "email": "test@siiqo.com",
                            "password": "Password123!"
                        }, indent=4)
                    elif path == '/api/auth/verify-email' and method == 'POST':
                        item["request"]["body"]["raw"] = json.dumps({
                            "email": "test@siiqo.com",
                            "otp": "123456"
                        }, indent=4)
                        
                folders[folder_name]["item"].append(item)

    # Sort folders alphabetically and add to collection
    for folder_name in sorted(folders.keys()):
        # Sort items inside folder by path
        folders[folder_name]["item"].sort(key=lambda x: x["name"])
        collection["item"].append(folders[folder_name])

    output_file = "Siiqo_Postman_Collection.json"
    with open(output_file, 'w') as f:
        json.dump(collection, f, indent=4)
        
    print(f"Successfully generated Postman collection at: {os.path.abspath(output_file)}")

if __name__ == '__main__':
    generate_postman_collection()
