"""
normalize_filenames_for_url.py

- Normalizes file names
- Converts to URL-friendly slugs
- Preserves file extensions

** Comandos
- Renomear 
    python normalize_filenames_for_url.py --path "C:\meus\arquivos"
- Teste sem renomear 
    python normalize_filenames_for_url.py --path "C:\meus\arquivos" --dry-run
- Renomear aquivos em subpastas também
    python normalize_filenames_for_url.py --path "C:\meus\arquivos" -r
- Para não remover prefixo
    python normalize_filenames_for_url.py --path "C:\meus\arquivos" --prefix-regex ""
Author: Matheus França
"""

import os
import re
import unicodedata
import argparse
from pathlib import Path

def slugify(text: str) -> str:
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s-]+', '-', text).strip('-')
    return text

def remove_prefix(name: str, prefix_regex: str | None) -> str:
    if not prefix_regex:
        return name
    return re.sub(prefix_regex, '', name, flags=re.IGNORECASE)

def unique_path(path: Path) -> Path:
    """Se já existir, adiciona -1, -2, ... antes da extensão."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        candidate = path.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1

def iter_files(directory: Path, recursive: bool):
    if recursive:
        yield from (p for p in directory.rglob("*") if p.is_file())
    else:
        yield from (p for p in directory.iterdir() if p.is_file())

def rename_files(directory: Path, prefix_regex: str | None, dry_run: bool, recursive: bool):
    for old_path in iter_files(directory, recursive):
        name = old_path.stem
        ext = old_path.suffix.lower()

        # remove prefixo (ex: ^xxx-xx-x\s*)
        name = remove_prefix(name, prefix_regex)

        new_filename = slugify(name) + ext
        new_path = old_path.with_name(new_filename)

        if new_path == old_path:
            continue

        # evita colisão de nomes
        final_path = unique_path(new_path)

        if dry_run:
            print(f"[DRY-RUN] {old_path.name} -> {final_path.name}")
        else:
            old_path.rename(final_path)
            print(f"{old_path.name} -> {final_path.name}")

def main():
    parser = argparse.ArgumentParser(
        prog="normalize_filenames_for_url",
        description="Renomeia arquivos para um padrão URL-friendly (slug), com opção de remover prefixo via regex."
    )

    parser.add_argument(
        "--path", "-p",
        required=True,
        help="Caminho da pasta onde estão os arquivos."
    )
    parser.add_argument(
        "--prefix-regex",
        default=r"^xxx-xx-x\s*",
        help="Regex para remover do início do nome (default: ^xxx-xx-x\\s*). Use '' para desativar."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Não renomeia de verdade; só mostra o que faria."
    )
    parser.add_argument(
        "--recursive", "-r",
        action="store_true",
        help="Percorre subpastas também."
    )

    args = parser.parse_args()

    directory = Path(args.path).expanduser().resolve()
    if not directory.exists() or not directory.is_dir():
        raise SystemExit(f"Erro: caminho inválido: {directory}")

    prefix_regex = args.prefix_regex if args.prefix_regex.strip() else None

    rename_files(directory, prefix_regex, args.dry_run, args.recursive)

if __name__ == "__main__":
    main()
