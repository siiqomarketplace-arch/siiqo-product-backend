import re

path = r'c:\Users\RABONY GLOBALS\Downloads\SQL files\realvlcj_siiqo (3).sql'
tables = set()
with open(path, 'r', encoding='utf-8', errors='ignore') as f:
    for line in f:
        match = re.search(r'INSERT INTO `(\w+)` \((.*?)\) VALUES', line)
        if match:
            table = match.group(1)
            cols = match.group(2)
            if table not in tables:
                print(f"Table: {table}")
                print(f"Columns: {cols}")
                tables.add(table)
