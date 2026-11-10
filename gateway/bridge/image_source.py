"""Read existing SGA image files without altering source contents or metadata."""
import hashlib
import os
import re
import stat
from pathlib import Path

MAX_IMAGE_BYTES = 6 * 1024 * 1024
PATTERN = re.compile(r'^(?P<article>\d+)(?:_(?P<slot>\d+))?\.(jpe?g|png|webp|bmp)$', re.I)
EXTENSION_ORDER = {'.jpg': 0, '.jpeg': 0, '.png': 1, '.webp': 2, '.bmp': 3}


class ImageSourceChanged(ValueError):
    code = 'SGA_IMAGE_CHANGED_DURING_READ'


class ProductImageSource:
    def __init__(self, directory):
        self.directory = Path(directory).resolve(strict=True)
        if not self.directory.is_dir():
            raise ValueError('Image source is not a directory')

    def snapshot(self):
        groups = {}
        for path in self.directory.iterdir():
            match = PATTERN.fullmatch(path.name)
            if not match:
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError('Image source must contain regular files, not links')
            info = path.stat()
            if not 0 < info.st_size <= MAX_IMAGE_BYTES or len(path.name) > 255 or int(match['article']) <= 0:
                raise ValueError('Invalid image identity or source size')
            groups.setdefault(int(match['article']), []).append({
                'path': path, 'key': path.name.lower(), 'slot': int(match['slot'] or 1),
                'explicit': match['slot'] is not None, 'size': info.st_size, 'mtime': info.st_mtime_ns,
            })
        rows = []
        for article, images in sorted(groups.items()):
            explicit_main = [r for r in images if r['explicit'] and r['slot'] == 1]
            def priority(row):
                return (0 if row['slot'] == 1 else 1, row['slot'],
                        EXTENSION_ORDER[row['path'].suffix.lower()], row['key'])
            main = min(explicit_main or images, key=priority)
            others = sorted([r for r in images if r is not main], key=priority)
            for sequence, row in enumerate([main] + others):
                rows.append({**row, 'article_id': article, 'primary': row is main, 'sequence': sequence * 10})
        if len({r['key'] for r in rows}) != len(rows):
            raise ValueError('Case-insensitive image filename collision')
        return rows

    def read(self, row):
        descriptor = os.open(row['path'], os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_size != row['size']
                    or before.st_mtime_ns != row['mtime']):
                raise ImageSourceChanged()
            raw = stream.read(MAX_IMAGE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
                or len(raw) != before.st_size):
            raise ImageSourceChanged()
        item = {key: row[key] for key in ('article_id', 'key', 'primary', 'sequence')}
        item['checksum'] = hashlib.sha256(raw).hexdigest()
        return item, raw
