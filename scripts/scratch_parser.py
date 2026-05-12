import re

path = r'c:\Users\RABONY GLOBALS\Downloads\SQL files\realvlcj_siiqo (3).sql'
try:
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        data = f.read()
    tables = re.findall(r'CREATE TABLE `(.*?)`', data)
    print("Tables found in DB dump:")
    for t in tables:
        print(f" - {t}")
except Exception as e:
    print(f"Error: {e}")
