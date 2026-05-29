import os

email_dir = r"c:\Users\RABONY GLOBALS\Downloads\Siiqo prodcut\Siiqo backend\app\templates\emails"

old_social_block = """            <div style="margin-bottom: 20px; font-size: 14px;">
                <a href="https://www.linkedin.com/company/siiqo4smes" target="_blank" style="text-decoration: none; margin: 0 10px; font-weight: 600;">LinkedIn</a> | 
                <a href="https://www.facebook.com/Siiqo4SMEs" target="_blank" style="text-decoration: none; margin: 0 10px; font-weight: 600;">Facebook</a> | 
                <a href="https://x.com/Siiqo4SMEs" target="_blank" style="text-decoration: none; margin: 0 10px; font-weight: 600;">X (Twitter)</a> | 
                <a href="https://www.youtube.com/@Siiqo4SMEs" target="_blank" style="text-decoration: none; margin: 0 10px; font-weight: 600;">YouTube</a>
            </div>"""

new_social_block = """            <div style="margin-bottom: 20px; font-size: 14px;">
                <a href="https://www.linkedin.com/company/siiqo4smes" target="_blank" style="text-decoration: none; margin: 0 10px;">
                    <img src="https://img.icons8.com/color/48/000000/linkedin.png" alt="LinkedIn" width="24" height="24" style="border: none; display: inline-block; vertical-align: middle;">
                </a>
                <a href="https://www.facebook.com/Siiqo4SMEs" target="_blank" style="text-decoration: none; margin: 0 10px;">
                    <img src="https://img.icons8.com/color/48/000000/facebook-new.png" alt="Facebook" width="24" height="24" style="border: none; display: inline-block; vertical-align: middle;">
                </a>
                <a href="https://x.com/Siiqo4SMEs" target="_blank" style="text-decoration: none; margin: 0 10px;">
                    <img src="https://img.icons8.com/color/48/000000/twitterx--v1.png" alt="X (Twitter)" width="24" height="24" style="border: none; display: inline-block; vertical-align: middle;">
                </a>
                <a href="https://www.youtube.com/@Siiqo4SMEs" target="_blank" style="text-decoration: none; margin: 0 10px;">
                    <img src="https://img.icons8.com/color/48/000000/youtube-play.png" alt="YouTube" width="24" height="24" style="border: none; display: inline-block; vertical-align: middle;">
                </a>
            </div>"""

def update_templates():
    for root, dirs, files in os.walk(email_dir):
        for file in files:
            if file.endswith(".html"):
                path = os.path.join(root, file)
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()

                if old_social_block in content:
                    content = content.replace(old_social_block, new_social_block)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                    print(f"Updated {file}")

if __name__ == "__main__":
    update_templates()
