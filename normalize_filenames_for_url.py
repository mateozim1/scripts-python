"""
normalize_filenames_for_url.py

- Normalizes file names
- Converts to URL-friendly slugs
- Preserves file extensions

Author: Matheus França
"""

import os
import re
import unicodedata

def slugify(text):
    # Remove acentos
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    
    # Converte para minúsculas
    text = text.lower()
    
    # Remove caracteres inválidos
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    
    # Substitui espaços e múltiplos hífens por um único hífen
    text = re.sub(r'[\s-]+', '-', text).strip('-')
    
    return text

def rename_files(directory):
    for filename in os.listdir(directory):
        old_path = os.path.join(directory, filename)

        if os.path.isfile(old_path):
            name, ext = os.path.splitext(filename)
            new_name = slugify(name) + ext.lower()
            new_path = os.path.join(directory, new_name)

            if old_path != new_path:
                os.rename(old_path, new_path)
                print(f'Renomeado: {filename} → {new_name}')

# 🔹 Informe o caminho da pasta
PASTA_ARQUIVOS = r'C:\caminho\para\sua\pasta'

rename_files(PASTA_ARQUIVOS)
