"""Render a synthetic transfer dashboard without downloading anything.

Usage: .venv/Scripts/python.exe support/preview_terminal.py [--png]
"""
import argparse
import os
from pathlib import Path
import re
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.terminal import MultiBarManager


def preview_frame():
    with patch('core.terminal.shutil.get_terminal_size', return_value=os.terminal_size((120, 32))):
        display = MultiBarManager(max_slots=3, is_tty=False)
        video = display.assign_slot('video', 'yt-dlp', 'Travel video', extractor='youtube')
        display.update_slot(video, phase='processing', status_message='Merging audio and video',
                            downloaded=285 * 1024**2, total=285 * 1024**2, percent=100)
        album = display.assign_slot('album', 'gallery-dl', 'Summer album', extractor='erome')
        display.update_slot(album, phase='downloading', status_message='Downloading', total_files=23,
                            completed_files=7, file_name='clip_008.mp4', file_downloaded_bytes=1100 * 1024,
                            speed=1.59 * 1024**2)
        gallery = display.assign_slot('gallery', 'gallery-dl', 'Gallery 107374', extractor='nhentai')
        display.update_slot(gallery, phase='downloading', status_message='Downloading', total_files=48,
                            completed_files=0, file_name='page_001.png', file_downloaded_bytes=384 * 1024,
                            file_total_bytes=1024**2, speed=194.6 * 1024)
        display.set_pending_items([('telegram-dl', 'https://t.me/example/123', 'Waiting for an available slot')])
        display.set_queue_counts(remaining=4, completed=2, failed=0)
        display.render(force=True)
        return display._last_frame


def write_png(lines, path):
    from PIL import Image, ImageDraw, ImageFont
    font_path = Path('C:/Windows/Fonts/consola.ttf')
    font = ImageFont.truetype(str(font_path) if font_path.exists() else 'DejaVuSansMono.ttf', 20)
    cell = font.getlength('M')
    row = 29
    palette = {90: '#8b97a3', 97: '#e4e9ed', 96: '#67d5d8', 92: '#8ad19d', 93: '#e5c477', 91: '#ef8b8b',
               30: '#182026', 31: '#ef8b8b', 32: '#8ad19d', 33: '#e5c477', 34: '#87b9eb',
               35: '#c5a0e6', 36: '#67d5d8', 37: '#d5dde5'}
    image = Image.new('RGB', (int(120 * cell + 40), row * len(lines) + 40), '#171d23')
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        x, color = 20.0, '#d5dde5'
        for token in re.split(r'(\x1b\[[0-9;]*m)', line):
            if token.startswith('\x1b['):
                for code in token[2:-1].split(';'):
                    value = int(code or 0)
                    if value == 0:
                        color = '#d5dde5'
                    elif value in palette:
                        color = palette[value]
            else:
                draw.text((x, 18 + index * row), token, font=font, fill=color)
                x += font.getlength(token)
    image.save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--png', action='store_true', help='Also save a rendered preview image in scratch/')
    options = parser.parse_args()
    lines = preview_frame()
    plain = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', '\n'.join(lines))
    print(plain)
    folder = ROOT / 'scratch'
    folder.mkdir(exist_ok=True)
    (folder / 'terminal-preview.txt').write_text(plain + '\n', encoding='utf-8')
    if options.png:
        write_png(lines, folder / 'terminal-preview.png')


if __name__ == '__main__':
    main()
