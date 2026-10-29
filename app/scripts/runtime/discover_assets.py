"""Explicit R7b discovery. Builds never refresh pinned assets automatically."""
import json
import re
import shutil
import urllib.request
from pathlib import Path

from runtime_pack import CACHE, LOCK_PATH, assert_local, now, read_json, sha, write_json

API = 'https://api.github.com/repos/BtbN/FFmpeg-Builds/releases'
ASSET = re.compile(r'^ffmpeg-(n9\.0(?:\.\d+)?-\d+-g[0-9a-f]+)-win64-lgpl-shared-9\.0\.zip$')
OFL_URL = 'https://raw.githubusercontent.com/lxgw/LxgwWenKai/main/OFL.txt'


def request(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Article2Video-R7b'}), timeout=90)


def choose(releases):
    for release in sorted(releases, key=lambda r: r.get('published_at') or '', reverse=True):
        if release['draft'] or release['tag_name'] == 'latest':
            continue  # A moving alias is a discovery hint, never a final lock.
        for asset in release['assets']:
            match = ASSET.fullmatch(asset['name'])
            prefix = f"https://github.com/BtbN/FFmpeg-Builds/releases/download/{release['tag_name']}/"
            if match and asset['browser_download_url'] == prefix + asset['name']:
                return {'version': match[1], 'filename': asset['name'], 'url': asset['browser_download_url'],
                        'releaseTag': release['tag_name'], 'releasePublishedAt': release['published_at'],
                        'assetId': asset['id'], 'bytes': asset['size'], 'cacheDirectory': 'ffmpeg',
                        'licenseVariant': 'LGPL shared', 'repository': 'https://github.com/BtbN/FFmpeg-Builds'}
    raise RuntimeError('No immutable 9.0/win64/LGPL/shared ZIP found; no fallback')


def acquire(asset):
    target = CACHE / asset['cacheDirectory'] / asset['filename']
    assert_local(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise RuntimeError('Unpinned download already exists; retain and review, do not overwrite')
    partial = target.with_suffix(target.suffix + '.partial')
    with request(asset['url']) as response, partial.open('xb') as dest:
        shutil.copyfileobj(response, dest)
    if asset.get('bytes') and partial.stat().st_size != asset['bytes']:
        raise RuntimeError('Download size mismatch; partial retained')
    asset.update(sha256=sha(partial), bytes=partial.stat().st_size, lockedAt=now())
    partial.rename(target)
    return target


def main():
    lock = read_json(LOCK_PATH)
    if not lock['ffmpeg'].get('blocked') and lock['ffmpeg'].get('sha256') and lock.get('ofl'):
        print('Assets already pinned; discovery skipped')
        return
    if lock['ffmpeg'].get('blocked'):
        releases = []
        for page in range(1, 11):
            with request(f'{API}?per_page=100&page={page}') as response:
                rows = json.load(response)
            releases.extend(rows)
            if len(rows) < 100:
                break
        else:
            raise RuntimeError('Release pagination limit reached; do not select from incomplete list')
        asset = choose(releases)
        print('Selected', asset['releaseTag'], asset['filename'], flush=True)
        acquire(asset)
        lock['ffmpeg'] = asset
        write_json(LOCK_PATH, lock)
        print('FFmpeg SHA256', asset['sha256'], flush=True)
    if not lock.get('ofl'):
        asset = {'filename': 'OFL.txt', 'url': OFL_URL, 'cacheDirectory': 'fonts/LXGW-WenKai',
                 'repository': 'https://github.com/lxgw/LxgwWenKai', 'license': 'SIL Open Font License 1.1'}
        path = acquire(asset)
        content = path.read_text(encoding='utf-8')
        if 'SIL OPEN FONT LICENSE' not in content or 'Version 1.1' not in content:
            raise RuntimeError('Official OFL content does not match; retain and stop')
        lock['ofl'] = asset
        write_json(LOCK_PATH, lock)
        print('OFL SHA256', asset['sha256'], flush=True)


if __name__ == '__main__':
    main()
