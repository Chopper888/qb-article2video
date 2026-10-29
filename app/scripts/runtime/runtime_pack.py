"""Offline Runtime Pack assembly with S4-R8c release-cache exclusions."""
import argparse
import csv
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
import uuid

REPO = Path(__file__).resolve().parents[2]
# APP2：A2V_RUNTIME_PROFILE=v5.7 时改用 src/shared/runtime-baseline-v5.7.json（引擎 v5.7 白名单、
# 档案覆盖的 FFmpeg、独立的 .cache/runtime-pack-v5.7 输出目录）。不设时行为与 v5 完全相同。
# APP3：v5.7 的 FFmpeg 改回 LGPL shared 版（静态版资产已被上游删除）；外部资产先从资产镜像取（见 fetch_asset）。
PROFILE = os.environ.get('A2V_RUNTIME_PROFILE', '').strip()
if PROFILE and not re.fullmatch(r'v\d+(\.\d+)*', PROFILE):
    raise RuntimeError('Unknown runtime profile: ' + PROFILE)
BASELINE = json.loads((REPO / ('src/shared/runtime-baseline-' + PROFILE + '.json' if PROFILE else 'src/shared/runtime-baseline.json')).read_text(encoding='utf-8'))
if PROFILE and BASELINE.get('version') != PROFILE:
    raise RuntimeError('Runtime profile file does not match its name')
SAMPLE_CONTRACT = json.loads((REPO / 'src/shared/v5-sample-contract.json').read_text(encoding='utf-8'))
PACK_NAME = 'runtime-pack-' + BASELINE['version']
PACK = REPO / '.cache' / PACK_NAME
ENGINE_FILE_COUNT = BASELINE.get('engineFileCount', 188)
# 动态链接（lgpl-shared，bin 里有 DLL；v5 与 APP3 起的 v5.7）还是静态（lgpl，单个 exe）
FFMPEG_LINKAGE = (BASELINE.get('ffmpeg') or {}).get('linkage', 'shared')
CACHE = REPO / '.cache/runtime-downloads'
LOCK_PATH = Path(__file__).with_name('assets-lock.json')
PARTS = ('engine', 'runtime', 'samples', 'python', 'node', 'ffmpeg', 'licenses', 'manifest')
FIXED_ENV = {'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1', 'NPM_CONFIG_UPDATE_NOTIFIER': 'false'}
BROWSER = 'runtime/node_modules/.remotion/chrome-headless-shell/win64/chrome-headless-shell-win64/chrome-headless-shell.exe'
COMPOSITOR = 'runtime/node_modules/@remotion/compositor-win32-x64-msvc'
FONT = 'runtime/fonts/LXGWWenKai-Regular.ttf'
LAUNCHERS = ('f2py.exe', 'idna.exe', 'normalizer.exe', 'numpy-config.exe')
WHITELIST_PATH = Path(__file__).with_name(BASELINE.get('whitelistFile', 'engine-whitelist-v5.txt'))
WHITELIST_SHA = BASELINE['whitelistSha256']
WEBPACK_CACHE = 'runtime/node_modules/.cache/webpack'
# APP3：组装时往 Pack 的引擎目录写 VERSION（"标签 短提交号"），APP 安装版据此显示引擎版本。
# 它不在引擎白名单里，是白名单之外唯一允许的引擎文件。
# APP4：档案文件名仍沿用 v5.7；写进 VERSION 的引擎版本取 engineVersion（v5.8）。
ENGINE_VERSION = BASELINE.get('engineVersion', BASELINE['version'])
ENGINE_VERSION_FILE = 'VERSION'
ASSEMBLY_CHECK_COUNT = 18
EXCLUDED_ENGINE_DOCUMENTS = frozenset({
    'pipeline/ASSEMBLY_LINE.md',
    'pipeline/LAYOUT_RULES.md',
    'pipeline/SCREENSHOT_DIRECTION.md',
    'pipeline/SCREENSHOT_HOOKS.md',
})
ENGINE_BYTECODE = frozenset(['pipeline/__pycache__/__init__.cpython-312.pyc'] + [
    f'pipeline/engine/__pycache__/{name}.cpython-312.pyc'
    for name in ('__init__', 'errors', 'ledger', 'paid', 'paths', 'pricing', 'spec')])
RECORD_EXCLUSIONS = {
    'charset_normalizer-3.4.9.dist-info/RECORD': ('../../bin/normalizer.exe',),
    'idna-3.18.dist-info/RECORD': ('../../bin/idna.exe',),
    'numpy-2.5.1.dist-info/RECORD': ('../../bin/f2py.exe', '../../bin/numpy-config.exe')}


def record_normalization_policy():
    return {'revision': 'S4-R8c-b',
            'reason': 'Remove stale records for the four excluded CLI launchers; retain all other metadata bytes.',
            'records': [{'path': 'python/Lib/site-packages/' + name, 'removedPaths': list(paths)}
                        for name, paths in RECORD_EXCLUSIONS.items()]}


def record_rows(path):
    rows = []
    for line in path.read_bytes().splitlines(keepends=True):
        parsed = list(csv.reader([line.decode('utf-8')], strict=True))
        if len(parsed) != 1 or len(parsed[0]) != 3:
            raise RuntimeError('Malformed RECORD row: ' + path.name)
        rows.append((parsed[0][0], line))
    return rows


def normalize_launcher_records(site_packages):
    # Validate all three before any writes. A second invocation must fail, not silently succeed.
    writes = []
    for relative, expected in RECORD_EXCLUSIONS.items():
        target = site_packages / relative
        assert_local(target)
        for entry in expected:
            if (site_packages / 'bin' / Path(entry).name).exists():
                raise RuntimeError('Excluded launcher still exists: ' + entry)
        rows = record_rows(target)
        for name, _ in rows:
            if name.startswith('../../bin/') and name.endswith('.exe') and name not in expected:
                raise RuntimeError('Unknown launcher RECORD row: ' + name)
        if any(sum(name == entry for name, _ in rows) != 1 for entry in expected):
            raise RuntimeError('Expected RECORD row missing or duplicated: ' + relative)
        writes.append((target, b''.join(line for name, line in rows if name not in expected)))
    for target, content in writes:
        target.write_bytes(content)
    return record_normalization_policy()


def exclude_engine_bytecode(files):
    selected, excluded = dict(files), []
    for name, path in sorted(files.items()):
        if path.suffix != '.pyc' or path.parent.name != '__pycache__':
            continue  # Standalone bytecode is not an authorized exclusion.
        if name not in ENGINE_BYTECODE:
            raise RuntimeError('Unexpected Engine bytecode: ' + name)
        source_name = str(Path(name).parent.parent / (path.name.split('.cpython-')[0] + '.py')).replace('\\', '/')
        source = files.get(source_name)
        if source is None or not source.is_file():
            raise RuntimeError('Bytecode has no whitelisted Python source: ' + name)
        excluded.append({'path': 'engine/' + name, 'source': 'engine/' + source_name,
                         'sourceSha256': sha(source), 'bytes': path.stat().st_size, 'sha256': sha(path)})
        del selected[name]
    return selected, excluded


def approved_engine_bytecode_exclusions(source, selected):
    if ENGINE_BYTECODE.intersection(selected):
        raise RuntimeError('Official Engine whitelist must not include approved source bytecode')
    excluded = []
    for name in sorted(ENGINE_BYTECODE):
        path = source / name
        source_name = str(Path(name).parent.parent / (path.name.split('.cpython-')[0] + '.py')).replace('\\', '/')
        source_path = source / source_name
        if not path.is_file() or not source_path.is_file():
            raise RuntimeError('Expected approved source bytecode and Python source: ' + name)
        excluded.append({'path': 'engine/' + name, 'source': 'engine/' + source_name,
                         'sourceSha256': sha(source_path), 'bytes': path.stat().st_size, 'sha256': sha(path)})
    return excluded


def release_exclusions(sources):
    raw, _ = expand_whitelist(sources['engine'], WHITELIST_PATH.read_text(encoding='utf-8-sig'))
    bytecode = approved_engine_bytecode_exclusions(sources['engine'], raw)
    cache = sources['runtime'] / 'node_modules/.cache/webpack'
    files = [{'path': 'runtime/' + p.relative_to(sources['runtime']).as_posix(),
              'bytes': p.stat().st_size, 'sha256': sha(p)}
             for p in sorted(cache.rglob('*')) if p.is_file()]
    return {'revision': 'S4-R8c-b', 'recordNormalization': record_normalization_policy(),
            'webpack': {'pattern': WEBPACK_CACHE + '/**',
            'decision': 'EXCLUDE_FROM_RELEASE', 'authority': 'Article2Video-v4.1-webpack-cache发布结论.md',
            'reason': 'Rebuilt offline in writable task _runtime; development cache is not portable.',
            'files': files}, 'engineBytecode': bytecode}


def copy_runtime(source, target):
    source = source.resolve(strict=True)
    cache_parent = source / 'node_modules/.cache'
    def ignore(directory, names):
        return ['webpack'] if Path(directory) == cache_parent and 'webpack' in names else []
    assert_local(target)
    shutil.copytree(source, target, ignore=ignore)


def expand_whitelist(root, text):
    """Expand relative file/glob patterns; reject traversal, reparse and empty matches."""
    root = root.resolve(strict=True)
    source_snapshot(root)  # Reject reparse points before glob can traverse them.
    files, counts = {}, {}
    for line in text.splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith('#'):
            continue
        parts = pattern.split('/')
        if (any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts)
                or any(c in pattern for c in ('\\', ':', '\x00'))
                or any(ord(c) < 32 for c in pattern)
                or ('**' in pattern and (not pattern.endswith('/**') or '**' in pattern[:-3]))):
            raise RuntimeError('Unsafe whitelist pattern: ' + pattern)
        if pattern in counts:
            raise RuntimeError('Duplicate whitelist pattern: ' + pattern)
        if pattern.endswith('/**'):
            directory = root / pattern[:-3]
            if any(c in pattern[:-3] for c in '*?[]') or not directory.is_dir():
                raise RuntimeError('Invalid recursive directory: ' + pattern)
            matches = sorted(p for p in directory.rglob('*') if p.is_file())
        else:
            matches = sorted(p for p in root.glob(pattern) if p.is_file())
        if not matches:
            raise RuntimeError('Whitelist matched zero files: ' + pattern)
        counts[pattern] = len(matches)
        for path in matches:
            if not path.resolve(strict=True).is_relative_to(root):
                raise RuntimeError('Whitelist escaped Engine root')
            files[path.relative_to(root).as_posix()] = path
    if not counts:
        raise RuntimeError('Empty whitelist')
    return files, counts


def engine_selection(source, lock):
    if sha(WHITELIST_PATH) != WHITELIST_SHA:
        raise RuntimeError('Authority whitelist hash mismatch')
    files, counts = expand_whitelist(source, WHITELIST_PATH.read_text(encoding='utf-8-sig'))
    if len(counts) != ENGINE_FILE_COUNT or len(files) != ENGINE_FILE_COUNT or any(value != 1 for value in counts.values()):
        raise RuntimeError('Official Engine whitelist must resolve to exactly %d explicit files' % ENGINE_FILE_COUNT)
    if EXCLUDED_ENGINE_DOCUMENTS.intersection(files):
        raise RuntimeError('Officially excluded Engine document entered the whitelist')
    if any(path.suffix == '.pyc' or '__pycache__' in path.parts for path in files.values()):
        raise RuntimeError('Engine bytecode entered the official whitelist')
    hashes = {name: sha(path) for name, path in sorted(files.items())}
    facts = {'path': WHITELIST_PATH.relative_to(REPO).as_posix(), 'sha256': WHITELIST_SHA,
             'patterns': list(counts), 'patternFileCounts': counts, 'fileCount': len(files),
             'bytes': sum(p.stat().st_size for p in files.values()), 'engineCommit': lock['engineCommit'],
             'filesSha256': hashes}
    return files, facts


def copy_engine(files, target):
    assert_local(target)
    target.mkdir()  # Never merge with an existing Engine payload.
    for relative, source in files.items():
        dest = target / relative
        assert_local(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)


def v5_sample_manifest(root, *, source=False):
    """Validate published samples; only the 29 approved S7 entries may be unlisted in source.

    Returns a complete manifest without writing to the source. The builder writes it only
    in its new Pack copy. Unknown/missing/changed files remain blocking.
    """
    manifest = read_json(root / 'manifest.json')
    entries = manifest['files']
    approved = SAMPLE_CONTRACT['s7Cache']
    if len(approved) != 29 or manifest['schema_version'] != 1 or manifest['total_files'] != len(entries):
        raise RuntimeError('v5 sample manifest identity/count mismatch')
    if manifest['total_size'] != sum(item['size'] for item in entries.values()):
        raise RuntimeError('v5 sample manifest total size mismatch')
    for relative in set(entries) | set(approved):
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or '\\' in relative or ':' in relative:
            raise RuntimeError('Unsafe sample manifest path: ' + relative)
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    expected = set(entries) | set(approved) | {'manifest.json'}
    if actual != expected or len(actual) - 1 != SAMPLE_CONTRACT['totalFiles']:
        raise RuntimeError('v5 sample file set mismatch')
    for relative, facts in entries.items():
        path = root / relative
        if path.stat().st_size != facts['size'] or sha(path) != facts['sha256']:
            raise RuntimeError('Sample file identity mismatch: ' + relative)
    completed = dict(entries)
    for relative, expected_sha in approved.items():
        path = root / relative
        if sha(path) != expected_sha:
            raise RuntimeError('v5 S7 cache identity mismatch: ' + relative)
        facts = {'size': path.stat().st_size, 'sha256': expected_sha}
        if not source and completed.get(relative) != facts:
            raise RuntimeError('v5 S7 cache missing from manifest: ' + relative)
        completed[relative] = facts
    return {**manifest, 'files': completed, 'total_files': len(completed),
            'total_size': sum(item['size'] for item in completed.values())}


def engine_version_text(lock):
    return f"{ENGINE_VERSION} {lock['engineCommit'][:7]}\n"


def write_engine_version(target, lock):
    path = target / ENGINE_VERSION_FILE
    assert_local(path)
    if path.exists():
        raise RuntimeError('Engine whitelist already contains VERSION')
    path.write_bytes(engine_version_text(lock).encode('ascii'))
    return path


def copy_samples(source, target, manifest):
    assert_local(target)
    shutil.copytree(source, target)
    write_json(target / 'manifest.json', manifest)
    # Like the other copied samples, the completed manifest has source-derived times.
    # This keeps directories.samples.metadataSha256 immutable across main/fresh builds.
    shutil.copystat(source / 'manifest.json', target / 'manifest.json')


def cached_asset(asset, wheel=False):
    path = CACHE / ('python-wheels' if wheel else asset.get('cacheDirectory', '')) / asset['filename']
    assert_local(path)
    if not path.is_file() or sha(path) != asset['sha256']:
        raise RuntimeError('Exact cached asset absent or changed: ' + asset['filename'])
    return path


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def development_path_rules(build_sources):
    sep = '\\'
    markers = {
        'windows-user-root': 'C:' + sep + 'Users' + sep,
        'claude-config': '.claude' + sep,
        'codex-config': '.codex' + sep,
        'development-resources': 'development-resources',
        'app-repository-path': str(REPO),
        **{f'build-source-{name}': value for name, value in build_sources.items()},
    }
    rules = {}
    for name, marker in markers.items():
        variants = set()
        for spelling in (marker, marker.replace('\\', '/'), marker.replace('\\', '\\\\')):
            variants.add(spelling.encode('utf-8').lower())
            variants.add(spelling.encode('utf-16-le').lower())
        rules[name] = tuple(sorted(variants))
    return rules


def development_path_needles(build_sources):
    return tuple(sorted({needle for variants in development_path_rules(build_sources).values()
                         for needle in variants}))


def classify_path_hygiene_finding(relative, markers):
    """Classify immutable third-party/sample provenance; return None for a release blocker."""
    markers = set(markers)
    if (relative.endswith('.dist-info/direct_url.json') and relative.startswith('python/Lib/site-packages/') and
            markers <= {'app-repository-path'}):
        return 'python-wheel-install-provenance'
    python_provenance = (
        relative == 'python/python312.dll' or
        relative == 'python/Lib/site-packages/numpy/__config__.py' or
        (relative.startswith('python/Lib/site-packages/numpy-') and relative.endswith('.dist-info/DELVEWHEEL'))
    )
    if python_provenance and markers == {'windows-user-root'}:
        return 'upstream-python-build-provenance'
    dependency_example = (
        relative.startswith('node/node_modules/npm/docs/') or
        relative == 'runtime/node_modules/@remotion/studio-server/dist/helpers/open-in-editor.js' or
        relative == 'runtime/node_modules/@types/node/fs.d.ts'
    )
    if dependency_example and markers == {'windows-user-root'}:
        return 'third-party-documentation-example'
    return None


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def assert_local(path):
    resolved = path.resolve()
    if not (resolved == REPO or REPO in resolved.parents):
        raise RuntimeError('Output escaped repository')
    for parent in (path, *path.parents):
        if parent == REPO.parent:
            break
        if parent.exists() and parent.is_junction():
            raise RuntimeError('Output junction is not allowed')
        if parent.is_symlink():
            raise RuntimeError('Output symlink is not allowed')


def source_snapshot(root):
    """No content import or source writes. Fingerprint names, sizes and mtimes."""
    root = root.resolve(strict=True)
    rows = []
    for base, dirs, files in os.walk(root):
        for name in sorted(dirs + files):
            item = Path(base) / name
            if item.is_symlink() or item.is_junction():
                raise RuntimeError('Source reparse point requires review: ' + str(item))
            relative = item.relative_to(root).as_posix()
            # CLN1W: Python bytecode is a build/test byproduct, not source. Approved
            # engine bytecode is still hashed by release_exclusions.
            if '__pycache__' in relative.split('/'):
                continue
            stat = item.stat()
            # Directory mtimes change whenever a child is added, including build
            # byproducts; file rows already reveal real content changes.
            is_file = item.is_file()
            rows.append((relative, stat.st_size if is_file else 0,
                         stat.st_mtime_ns if is_file else 0, is_file))
    rows.sort()
    return {'fileCount': sum(r[3] for r in rows), 'bytes': sum(r[1] for r in rows),
            'metadataSha256': hashlib.sha256(json.dumps(rows).encode()).hexdigest()}


def run(args, cwd, env, timeout=60, encoding='utf-8'):
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    out = result.stdout.decode(encoding, errors='replace').strip()
    err = result.stderr.decode(encoding, errors='replace').strip()
    if result.returncode:
        # Do not log the environment or potentially sensitive subprocess stderr.
        raise RuntimeError(f'{Path(args[0]).name} exit {result.returncode}')
    return out or err


def first_path_match(name, path):
    """Resolve the first PATH match without decoding console output.

    CLN1W: where.exe writes the OEM code page, so a path containing 【】 or ➡️
    was mangled to '??' and the priority check failed on a correct layout.
    """
    match = shutil.which(name, path=path)
    if not match:
        raise RuntimeError('Missing PATH entry: ' + name)
    return match


def child_environment(ffmpeg_bin=None, *, scratch=None):
    # Intentionally do not inherit API keys, proxy credentials or PYTHONPATH.
    allowed = ('SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT', 'NUMBER_OF_PROCESSORS')
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    prefixes = [PACK / 'python', PACK / 'node', ffmpeg_bin or PACK / 'ffmpeg/bin']
    env['PATH'] = os.pathsep.join(map(str, prefixes)) + os.pathsep + os.environ.get('PATH', '')
    env.update(FIXED_ENV)
    scratch = scratch if scratch is not None else CACHE / 'verification'
    scratch.mkdir(parents=True, exist_ok=True)
    env.update(TEMP=str(scratch), TMP=str(scratch), NPM_CONFIG_CACHE=str(scratch / 'npm-cache'))
    return env


def download(asset, wheel=False):
    if not asset.get('sha256'):
        raise RuntimeError('No approved asset hash; no fallback download')
    folder = CACHE / ('python-wheels' if wheel else asset.get('cacheDirectory', ''))
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / asset['filename']
    if not target.exists():
        partial = target.with_suffix(target.suffix + '.partial')
        request = urllib.request.Request(asset['url'], headers={'User-Agent': 'Article2Video-Runtime-Assembly'})
        with urllib.request.urlopen(request, timeout=60) as response, partial.open('xb') as stream:
            shutil.copyfileobj(response, stream)
        if sha(partial) != asset['sha256']:
            raise RuntimeError('Download hash mismatch; partial retained: ' + target.name)
        partial.rename(target)
    if sha(target) != asset['sha256']:
        raise RuntimeError('Cache hash mismatch: ' + target.name)
    return target


# APP3：资产镜像（默认是共享文件夹的 14-安装包/资产/，由打包脚本 -AssetMirror 传入）。
# 外部下载的资产先从镜像取，按锁定 SHA256 校验，一致才用；镜像里没有才去原地址下载，
# 下载后同样校验，并回填一份到镜像。镜像里的文件 SHA 不一致时拒绝（不使用、不覆盖、也不改去下载），交给人看。
MIRROR_MANIFEST = '资产清单.md'
ASSET_LICENSES = {
    'python': 'PSF-2.0', 'node': 'MIT（内含第三方组件，见包内 LICENSE）',
    'certifi': 'MPL-2.0', 'charset-normalizer': 'MIT', 'idna': 'BSD-3-Clause',
    'numpy': 'BSD-3-Clause（内含 OpenBLAS 等，见 wheel 内许可）', 'Pillow': 'MIT-CMU（HPND）',
    'requests': 'Apache-2.0', 'urllib3': 'MIT',
    'ffmpeg': 'LGPL-2.1-or-later', 'ofl': 'SIL Open Font License 1.1（霞鹜文楷的许可文本）'}


def profile_assets(lock):
    """当前档案需要的全部外部下载资产：[(名称, 资产, 是否 wheel)]。"""
    return [('python', lock['python'], False), ('node', lock['node'], False),
            *[(w['package'], w, True) for w in lock['wheels']],
            ('ffmpeg', lock['ffmpeg'], False), ('ofl', lock['ofl'], False)]


def cache_path(asset, wheel=False):
    return CACHE / ('python-wheels' if wheel else asset.get('cacheDirectory', '')) / asset['filename']


def _copy_verified(source, target, expected):
    """先写 .partial，校验一致再改名；目标已存在不覆盖。"""
    partial = target.with_name(target.name + '.partial')
    if partial.exists():
        partial.unlink()
    shutil.copyfile(source, partial)
    if sha(partial) != expected:
        partial.unlink()
        raise RuntimeError('Copied asset hash mismatch: ' + target.name)
    partial.rename(target)


def fetch_asset(asset, wheel=False, mirror=None, log=print):
    """把一个资产放进下载缓存。返回 (路径, 来源)：cache / mirror / download。"""
    if not asset.get('sha256'):
        raise RuntimeError('No approved asset hash; no fallback download')
    target = cache_path(asset, wheel)
    assert_local(target)
    if target.is_file():
        if sha(target) != asset['sha256']:
            raise RuntimeError('Cache hash mismatch: ' + target.name)
        return target, 'cache'
    mirrored = Path(mirror) / asset['filename'] if mirror else None
    if mirrored is not None and mirrored.is_file():
        if sha(mirrored) != asset['sha256']:
            raise RuntimeError('Mirror hash mismatch; refused: ' + str(mirrored))
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_verified(mirrored, target, asset['sha256'])
        log('镜像 ' + asset['filename'])
        return target, 'mirror'
    path = download(asset, wheel)
    log('下载 ' + asset['filename'])
    if mirrored is not None:
        backfill_mirror(path, mirrored, asset['sha256'], log)
    return path, 'download'


def backfill_mirror(source, mirrored, expected, log=print):
    """回填镜像：已有同名文件就不动（SHA 不一致时报出来，不覆盖）。失败只记录，不影响打包。"""
    try:
        if mirrored.exists():
            if sha(mirrored) != expected:
                log('镜像里已有同名文件但 SHA 不一致，未覆盖：' + mirrored.name)
            return False
        _copy_verified(source, mirrored, expected)
        log('已回填镜像 ' + mirrored.name)
        return True
    except OSError as error:
        log('回填镜像失败（不影响打包）：' + mirrored.name + '：' + str(error))
        return False


def fetch_assets(lock, mirror=None, log=print):
    return {asset['filename']: fetch_asset(asset, wheel, mirror, log)[1] for _, asset, wheel in profile_assets(lock)}


def mirror_manifest_text(lock, mirror):
    rows = ['# 打包资产清单', '',
            '由 `scripts/runtime/runtime_pack.py` 按当前档案（' + BASELINE['version'] + '）生成；SHA256 为锁定值。'
            '打包时先从这里取，校验一致才用；缺的会从原始来源下载并自动补进来。', '',
            '| 文件名 | 版本 | 许可 | SHA256 | 字节 | 原始来源 | 这里有没有 |', '|---|---|---|---|---|---|---|']
    for name, asset, _ in profile_assets(lock):
        path = Path(mirror) / asset['filename']
        state = '缺'
        if path.is_file():
            state = '有' if sha(path) == asset['sha256'] else '有，但 SHA 不一致'
        version = asset.get('version') or ('OFL 1.1' if name == 'ofl' else '')
        rows.append(f"| {asset['filename']} | {version} | {ASSET_LICENSES.get(name, '见包内许可')} | `{asset['sha256']}` | "
                    f"{asset.get('bytes', '')} | {asset['url']} | {state} |")
    return '\n'.join(rows) + '\n'


def write_mirror_manifest(lock, mirror):
    target = Path(mirror) / MIRROR_MANIFEST
    target.write_text(mirror_manifest_text(lock, mirror), encoding='utf-8')
    return target


def sync_mirror(lock, mirror, log=print):
    """补齐资产：当前档案需要、镜像里还没有的，都放进镜像（先进下载缓存并校验），再写资产清单。"""
    mirror = Path(mirror)
    if not mirror.is_dir():
        raise RuntimeError('Asset mirror directory not found: ' + str(mirror))
    added = []
    for _, asset, wheel in profile_assets(lock):
        mirrored = mirror / asset['filename']
        had = mirrored.exists()
        path, _ = fetch_asset(asset, wheel, mirror, log)  # 下载来的已经回填
        if not mirrored.exists():
            backfill_mirror(path, mirrored, asset['sha256'], log)
        if not mirrored.is_file() or sha(mirrored) != asset['sha256']:
            raise RuntimeError('Mirror missing or hash mismatch: ' + str(mirrored))
        if not had:
            added.append(asset['filename'])
    write_mirror_manifest(lock, mirror)
    return added


def extract(archive, target, strip=''):
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            name = entry.filename.replace('\\', '/')
            if strip:
                if not name.startswith(strip + '/'):
                    raise RuntimeError('Unexpected archive root')
                name = name[len(strip) + 1:]
            if not name:
                continue
            if ':' in name or name.startswith('/') or '..' in Path(name).parts or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError('Unsafe archive entry')
            output = target / name
            assert_local(output)
            if entry.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with z.open(entry) as source, output.open('xb') as dest:
                    shutil.copyfileobj(source, dest)


def engine_baseline(source, expected):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    if run(['git', '-C', source, 'rev-parse', 'HEAD'], REPO, env) != expected:
        raise RuntimeError('Engine commit mismatch')
    if run(['git', '-C', source, 'status', '--short'], REPO, env):
        raise RuntimeError('Engine source is dirty')


def archive_licenses():
    records = []
    for component in ('python', 'node', 'runtime', 'ffmpeg'):
        for path in sorted((PACK / component).rglob('*')):
            if path.is_file() and re.match(r'^(license|licence|copying|notice|ofl)([.\-_]|$)', path.name, re.I):
                relative = path.relative_to(PACK / component)
                target = PACK / 'licenses' / component / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                records.append({'component': component, 'file': target.relative_to(PACK).as_posix(), 'sha256': sha(target)})
    return records


def hash_manifest(manifest, lock, *, include_downloads=True, diagnostic_root=None):
    lines = []
    downloads = [lock['python'], lock['node'], *lock['wheels'], lock['ffmpeg'], *([lock['ofl']] if lock.get('ofl') else [])] if include_downloads else []
    for asset in downloads:
        path = CACHE / ('python-wheels' if asset in lock['wheels'] else asset.get('cacheDirectory', '')) / asset['filename']
        if path.is_file():
            lines.append(f'{sha(path)}  downloads/{path.relative_to(CACHE).as_posix()}')
    for relative in ['manifest/requirements.txt', 'manifest/runtime-pack.json', 'manifest/local-verification.json', FONT, BROWSER,
                     'python/python.exe', 'node/node.exe', 'runtime/manifest.json']:
        path = PACK / relative
        if diagnostic_root is not None and relative in DIAGNOSTIC_DOCUMENTS:
            path = diagnostic_root / relative
        if path.is_file():
            lines.append(f'{sha(path)}  pack/{relative}')
    target = (diagnostic_root if diagnostic_root is not None else PACK) / 'manifest/sha256sums.txt'
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def runtime_path_budget():
    entries = [p.relative_to(PACK / 'runtime').as_posix() for p in (PACK / 'runtime').rglob('*')]
    units = lambda text: len(text.encode('utf-16-le')) // 2
    longest = max(entries, key=lambda text: (units(text), text))
    if longest != BASELINE['longestRelativePath']:
        raise RuntimeError('Formal Runtime longest path differs from APP preflight baseline')
    return {'longestRelativePath': longest, 'relativeCharacters': units(longest),
            'maxTargetCharacters': BASELINE['maxTargetCharacters']}


def finish_manifest(manifest, lock, sources, snapshots):
    manifest['runtimePathBudget'] = runtime_path_budget()
    manifest['sourceUnchanged'] = {name: source_snapshot(path) == snapshots[name] for name, path in sources.items()}
    engine_baseline(sources['engine'], lock['engineCommit'])
    manifest['licenses'] = archive_licenses()
    write_json(PACK / 'licenses/provenance.json', {'records': manifest['licenses'],
               'font': lock['ofl'], 'fontSha256': lock['runtime']['fontSha256'],
               'ffmpeg': lock['ffmpeg'], 'releaseExclusions': manifest['releaseExclusions'],
               'legalReviewCompleted': False})
    write_json(PACK / 'licenses/ffmpeg/provenance.json', {'project': 'BtbN/FFmpeg-Builds',
               'asset': lock['ffmpeg'], 'legalInformation': 'https://ffmpeg.org/legal.html',
               'archivalComplete': True})
    manifest['directories'] = {part: source_snapshot(PACK / part) for part in PARTS if part != 'manifest'}
    write_json(PACK / 'manifest/runtime-pack.json', manifest)
    return verify(lock)


def build(args, lock):
    sources = {name: Path(getattr(args, name)).resolve(strict=True) for name in ('engine', 'runtime', 'samples')}
    if sources['samples'].name != SAMPLE_CONTRACT['sourceDirectory']:
        raise RuntimeError('v5 requires the formally delivered sample source')
    engine_baseline(sources['engine'], lock['engineCommit'])
    delivery = Path(args.delivery).resolve(strict=True)
    requirements = delivery / 'Article2Video-v4.1-requirements.txt'
    notes = delivery / 'Article2Video-v4.1-Python依赖说明.md'
    if sha(requirements) != lock['requirementsSha256'] or sha(notes) != lock['dependencyNotesSha256']:
        raise RuntimeError('Authority delivery hash mismatch')
    snapshots = {name: source_snapshot(path) for name, path in sources.items()}
    samples_manifest = v5_sample_manifest(sources['samples'], source=True)
    files, selection = engine_selection(sources['engine'], lock)
    # R7c is offline assembly of exact cached assets, never discovery or replacement.
    for asset in [lock['python'], lock['node'], *lock['wheels'], lock['ffmpeg'], lock['ofl']]:
        cached_asset(asset, asset in lock['wheels'])
    exclusions = release_exclusions(sources)
    if args.fresh:
        if PACK.exists():
            raise RuntimeError('Fresh target already exists; retained without cleanup')
        main_manifest = read_json(REPO / '.cache' / PACK_NAME / 'manifest/runtime-pack.json')
        main_result = read_json(REPO / '.cache' / PACK_NAME / 'manifest/local-verification.json')
        if (not main_manifest['localVerificationPassed'] or not main_result['passed']
                or any(c['status'] != 'PASS' for c in main_result['checks'])
                or main_manifest['assetLockSha256'] != sha(LOCK_PATH)
                or main_manifest['enginePublishWhitelist'] != selection
                or main_manifest['releaseExclusions'] != exclusions
                or main_manifest['sourceSnapshots'] != snapshots):
            raise RuntimeError('Main candidate must pass with identical inputs before fresh assembly')
    elif PACK.exists():
        if not args.rebuild:
            return verify(lock)
        previous = read_json(PACK / 'manifest/runtime-pack.json')
        if previous['assetLockSha256'] != sha(LOCK_PATH) or previous['sourceSnapshots'] != snapshots:
            raise RuntimeError('Rebuild inputs differ from the retained candidate')
        # Explicit --rebuild permits removal of this generated candidate only.
        assert_local(PACK)
        expected = REPO / '.cache' / PACK_NAME
        if PACK.resolve() != expected.resolve() or PACK != expected:
            raise RuntimeError('Rebuild target is not the fixed main Pack')
        source_snapshot(PACK)  # Reject nested reparse points before recursive removal.
        shutil.rmtree(PACK)
    PACK.mkdir()
    for part in PARTS:
        if part not in ('engine', 'runtime', 'samples'):
            (PACK / part).mkdir()
    manifest = {'schemaVersion': 1, 'article2videoVersion': BASELINE['version'],
                'engineCommit': lock['engineCommit'], 'generatedAt': now(),
                'buildSources': {k: str(v) for k, v in sources.items()},
                'sourceSnapshots': snapshots, 'limitations': [], 'assembly': {},
                'releaseExclusions': exclusions,
                **({'profile': {'name': PROFILE, 'baselineSha256': sha(REPO / ('src/shared/runtime-baseline-' + PROFILE + '.json')),
                                'ffmpegLinkage': FFMPEG_LINKAGE}} if PROFILE else {})}
    copy_engine(files, PACK / 'engine')
    write_engine_version(PACK / 'engine', lock)
    copy_runtime(sources['runtime'], PACK / 'runtime')
    copy_samples(sources['samples'], PACK / 'samples', samples_manifest)
    extract(cached_asset(lock['python']), PACK / 'python')
    (PACK / 'python/python312._pth').write_text('python312.zip\n.\nLib\\site-packages\n..\\engine\n', encoding='utf-8')
    env = child_environment()
    wheels = [cached_asset(w, True) for w in lock['wheels']]
    run([sys.executable, '-I', '-B', '-m', 'pip', '--isolated', 'install',
         '--disable-pip-version-check', '--no-index', '--no-deps', '--no-compile',
         '--target', PACK / 'python/Lib/site-packages', *wheels], CACHE, env, timeout=180)
    for name in LAUNCHERS:
        launcher = PACK / 'python/Lib/site-packages/bin' / name
        assert_local(launcher)
        launcher.unlink()  # The same four R7 exclusions; no other Python files removed.
    normalize_launcher_records(PACK / 'python/Lib/site-packages')
    extract(cached_asset(lock['node']), PACK / 'node', strip=lock['node']['filename'][:-4])
    extract(cached_asset(lock['ffmpeg']), PACK / 'ffmpeg', strip=lock['ffmpeg']['filename'][:-4])
    ofl_target = PACK / 'licenses/fonts/LXGW-WenKai/OFL.txt'
    ofl_target.parent.mkdir(parents=True)
    shutil.copy2(cached_asset(lock['ofl']), ofl_target)
    shutil.copy2(requirements, PACK / 'manifest/requirements.txt')
    manifest['assembly'] = {name: {'status': 'PASS'} for name in ('engine', 'runtime', 'samples', 'python', 'node', 'ffmpeg', 'fontLicense')}
    # Keep the validated R7c runtime baseline; the new release policy has its own revision.
    manifest.update(revision='S4-R7c', localVerificationPassed=False, assets=lock,
                    assetLockSha256=sha(LOCK_PATH), enginePublishWhitelist=selection,
                    launcherExclusions=['python/Lib/site-packages/bin/' + name for name in LAUNCHERS])
    return finish_manifest(manifest, lock, sources, snapshots)



# D5 frozen equality: exact JSON pointers, never file/directory exclusions.
DIAGNOSTIC_FIELDS = {'manifest/local-verification.json': {'/checks/0/detail/certifi': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/0/detail/engine': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/0/detail/executable': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/0/detail/path/0': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/0/detail/path/1': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/0/detail/path/2': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/0/detail/path/3': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/2/detail/node': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/2/detail/npm': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/2/detail/npx': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/2/detail/python': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/3/detail/path/ffmpeg': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/3/detail/path/ffprobe': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/8/detail/engine': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/8/detail/salign': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/8/detail/scene': 'DIAGNOSTIC_VOLATILE',
                                      '/checks/14/detail/scannedFiles': 'DIAGNOSTIC_VOLATILE',
                                      '/verifiedAt': 'DIAGNOSTIC_VOLATILE'},
 'manifest/path-hygiene.json': {'/scannedFiles': 'DIAGNOSTIC_VOLATILE'},
 'manifest/runtime-pack.json': {'/generatedAt': 'DIAGNOSTIC_VOLATILE',
                                '/lastVerifiedAt': 'DIAGNOSTIC_VOLATILE',
                                '/directories/engine/metadataSha256': 'DERIVED_FROM_VOLATILE',
                                '/directories/ffmpeg/metadataSha256': 'DERIVED_FROM_VOLATILE',
                                '/directories/licenses/metadataSha256': 'DERIVED_FROM_VOLATILE',
                                '/directories/node/metadataSha256': 'DERIVED_FROM_VOLATILE',
                                '/directories/python/metadataSha256': 'DERIVED_FROM_VOLATILE'}}
DERIVED_CHECKSUMS = frozenset({
    'pack/manifest/local-verification.json', 'pack/manifest/runtime-pack.json'})
DIAGNOSTIC_DOCUMENTS = (*DIAGNOSTIC_FIELDS, 'manifest/sha256sums.txt')


def content_inventory(root):
    """Complete read-only inventory; enumeration failures propagate."""
    result = {}
    def fail(error):
        raise error
    for base, dirs, files in os.walk(root, onerror=fail):
        for name in dirs + files:
            path = Path(base) / name
            if path.is_symlink() or path.is_junction():
                raise RuntimeError('Pack reparse point: ' + str(path))
        for name in sorted(files):
            path = Path(base) / name
            before = path.stat()
            digest = sha(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError('Pack changed while hashing: ' + str(path))
            result[path.relative_to(root).as_posix()] = {'bytes': before.st_size, 'sha256': digest}
    return result


def diagnostic_documents(root):
    return {name: (root / name).read_bytes().decode('utf-8') for name in DIAGNOSTIC_DOCUMENTS}


def exact_content_diff(left, right):
    return {'missing': sorted(left.keys() - right.keys()), 'extra': sorted(right.keys() - left.keys()),
            'changed': sorted(name for name in left.keys() & right.keys()
                              if any(left[name][k] != right[name][k] for k in ('bytes', 'sha256')))}


def pack_postcheck(before, after=None, error=None):
    if error is not None:
        return {'status': 'ERROR', 'error': str(error), 'missing': None, 'extra': None, 'changed': None}
    if before is None:
        return {'status': 'NOT_CHECKED', 'missing': None, 'extra': None, 'changed': None}
    if after is None:
        return {'status': 'ERROR', 'error': 'No after inventory',
                'missing': None, 'extra': None, 'changed': None}
    diff = exact_content_diff(before, after)
    return {'status': 'CHANGED' if any(diff.values()) else 'UNCHANGED',
            **{k: len(v) for k, v in diff.items()}, 'paths': diff}


def frozen_equality(left, right, left_documents, right_documents):
    """All payload bytes and all non-allowlisted fields must match."""
    diff = exact_content_diff(left, right)
    violations, allowed = [], []
    for name in DIAGNOSTIC_DOCUMENTS:
        for inventory, documents in ((left, left_documents), (right, right_documents)):
            if name not in inventory or name not in documents:
                violations.append({'file': name, 'reason': 'Missing diagnostic document'})
                continue
            data = documents[name].encode('utf-8')
            if len(data) != inventory[name]['bytes'] or hashlib.sha256(data).hexdigest() != inventory[name]['sha256']:
                violations.append({'file': name, 'reason': 'Diagnostic bytes do not match inventory'})
    def compare_json(file, a, b, pointer=''):
        if type(a) is not type(b):
            violations.append({'file': file, 'path': pointer, 'reason': 'Type change'})
        elif isinstance(a, dict):
            for key in sorted(a.keys() | b.keys()):
                child = pointer + '/' + key.replace('~', '~0').replace('/', '~1')
                if key not in a or key not in b:
                    violations.append({'file': file, 'path': child, 'reason': 'Unknown/missing field'})
                else:
                    compare_json(file, a[key], b[key], child)
        elif isinstance(a, list):
            if len(a) != len(b):
                violations.append({'file': file, 'path': pointer, 'reason': 'List shape change'})
            else:
                for index, (x, y) in enumerate(zip(a, b)):
                    compare_json(file, x, y, pointer + '/' + str(index))
        elif a != b:
            kind = DIAGNOSTIC_FIELDS[file].get(pointer)
            valid = kind is not None
            if pointer.endswith('scannedFiles'):
                valid = valid and type(a) is int and a >= 0 and b >= 0
            else:
                valid = valid and isinstance(a, str)
            if pointer.endswith('metadataSha256'):
                valid = valid and bool(re.fullmatch('[0-9a-f]{64}', a)) and bool(re.fullmatch('[0-9a-f]{64}', b))
            if valid:
                allowed.append({'file': file, 'path': pointer, 'classification': kind})
            else:
                violations.append({'file': file, 'path': pointer, 'reason': 'Immutable or unknown field change'})
    for name in DIAGNOSTIC_FIELDS:
        if name not in left_documents or name not in right_documents:
            continue
        compare_json(name, json.loads(left_documents[name].lstrip('\ufeff')),
                     json.loads(right_documents[name].lstrip('\ufeff')))
    def checksums(text):
        values = {}
        for line in text.splitlines():
            digest, name = line.split('  ', 1)
            if name in values or not re.fullmatch('[0-9a-f]{64}', digest):
                raise RuntimeError('Malformed or duplicate checksum entry')
            values[name] = digest
        return values
    name = 'manifest/sha256sums.txt'
    if name in left_documents and name in right_documents:
        a, b = checksums(left_documents[name]), checksums(right_documents[name])
        # Derived values are admitted only with a valid dependency chain on BOTH sides.
        for values, documents in ((a, left_documents), (b, right_documents)):
            for entry in DERIVED_CHECKSUMS:
                target = entry.removeprefix('pack/')
                if entry not in values or target not in documents or values[entry] != hashlib.sha256(documents[target].encode('utf-8')).hexdigest():
                    violations.append({'file': name, 'path': entry, 'reason': 'Invalid derived checksum'})
        for entry in sorted(a.keys() | b.keys()):
            if entry not in a or entry not in b:
                violations.append({'file': name, 'path': entry, 'reason': 'Unknown/missing checksum entry'})
            elif a[entry] != b[entry]:
                if entry in DERIVED_CHECKSUMS:
                    allowed.append({'file': name, 'path': entry, 'classification': 'DERIVED_FROM_VOLATILE'})
                else:
                    violations.append({'file': name, 'path': entry, 'reason': 'Immutable checksum change'})
    payload = [name for name in diff['changed'] if name not in DIAGNOSTIC_DOCUMENTS]
    effective = len(diff['missing']) + len(diff['extra']) + len(payload) + len(violations)
    return {'passed': effective == 0, 'effective_diff': effective, 'missing': diff['missing'], 'extra': diff['extra'],
            'raw_changed': diff['changed'], 'payload_changed': payload,
            'diagnostic_violations': violations, 'allowed_diagnostic_differences': allowed}


def audit_release_sources(manifest):
    expected = release_exclusions({k: Path(v) for k, v in manifest['buildSources'].items()})
    if manifest.get('releaseExclusions') != expected:
        raise RuntimeError('Release exclusion provenance mismatch')
    return expected


def audit_source_snapshots(manifest, lock):
    if not all(manifest['sourceUnchanged'].values()):
        raise RuntimeError('Source metadata changed during assembly')
    for name, source in manifest['buildSources'].items():
        if source_snapshot(Path(source)) != manifest['sourceSnapshots'][name]:
            raise RuntimeError('Source metadata changed: ' + name)
    engine_baseline(manifest['buildSources']['engine'], lock['engineCommit'])
    return {'metadataSnapshotsMatch': True, 'engineCommitAndCleanMatch': True}


def frozen_release_exclusions(manifest):
    expected = manifest['releaseExclusions']
    webpack = expected['webpack']
    if (expected['revision'] != 'S4-R8c-b' or webpack['pattern'] != WEBPACK_CACHE + '/**'
            or webpack['decision'] != 'EXCLUDE_FROM_RELEASE'
            or webpack['authority'] != 'Article2Video-v4.1-webpack-cache发布结论.md'
            or expected['recordNormalization'] != record_normalization_policy()):
        raise RuntimeError('Frozen release exclusion policy mismatch')
    # Presence is checked even for empty directories; no blanket cache exclusion.
    for path in PACK.rglob('*'):
        rel = path.relative_to(PACK).as_posix()
        if '.git' in path.parts or (rel.startswith('engine/') and
                ('__pycache__' in path.parts or path.suffix.lower() == '.pyc')):
            raise RuntimeError('Forbidden frozen release entry: ' + rel)
    if (PACK / WEBPACK_CACHE).exists():
        raise RuntimeError('Webpack cache must not ship')
    if any((PACK / 'engine' / name).exists() for name in EXCLUDED_ENGINE_DOCUMENTS):
        raise RuntimeError('Excluded Engine document entered the Pack')
    return expected



def private_python_import_paths(initial, after_import, root):
    """Validate both phases; preserve post-import order/duplicates as immutable relative facts."""
    root = root.resolve()
    if not all(Path(p).resolve().is_relative_to(root) for p in [*initial, *after_import]):
        raise RuntimeError('External sys.path')
    return [Path(p).resolve().relative_to(root).as_posix() for p in after_import]


def verify(lock, *, frozen_baseline=None, diagnostic_root=None):
    if frozen_baseline is not None:
        if diagnostic_root is None:
            raise RuntimeError('Frozen verification requires an external diagnostic sink')
        diagnostic_root = external_evidence_root(diagnostic_root)
    elif diagnostic_root is not None:
        raise RuntimeError('Assembly audit retains its own diagnostic contract')
    sink = diagnostic_root if diagnostic_root is not None else PACK
    (sink / 'manifest').mkdir(parents=True, exist_ok=True)
    probe_cwd = sink / 'scratch' if diagnostic_root is not None else CACHE
    scratch = probe_cwd if diagnostic_root is not None else CACHE / 'verification'
    manifest = read_json(PACK / 'manifest/runtime-pack.json')
    checks = []
    env = child_environment(scratch=scratch)
    def check(name, action):
        try:
            detail = action()
            checks.append({'name': name, 'status': 'PASS', 'detail': detail})
        except Exception as error:
            checks.append({'name': name, 'status': 'FAIL', 'reason': str(error)})
        print(name, checks[-1]['status'], flush=True)
    def require(condition, message):
        if not condition:
            raise RuntimeError(message)
    def python_check():
        executable = PACK / 'python/python.exe'
        script = """import sys,json,importlib.metadata
initial_path = list(sys.path)
import certifi,requests,charset_normalizer,idna,urllib3,numpy,PIL.Image,pipeline.engine
import numpy._core._multiarray_umath, PIL._imaging, charset_normalizer.md
print(json.dumps({'version':sys.version.split()[0],'executable':sys.executable,'path':initial_path,'postImportPath':sys.path,'isolated':sys.flags.isolated,'no_user_site':sys.flags.no_user_site,'dont_write_bytecode':sys.dont_write_bytecode,'certifi':certifi.where(),'engine':pipeline.engine.__file__,'packages':{n:importlib.metadata.version(n) for n in ['certifi','charset-normalizer','idna','numpy','Pillow','requests','urllib3']}}))"""
        value = json.loads(run([executable, '-B', '-c', script], probe_cwd, env))
        require(value['version'] == '3.12.10', 'Python version mismatch')
        require(Path(value['executable']).resolve() == executable.resolve(), 'Non-private interpreter')
        value['postImportPathRelative'] = private_python_import_paths(value['path'], value.pop('postImportPath'), PACK)
        require(value['isolated'] and value['no_user_site'] and value['dont_write_bytecode'], 'Python isolation flags missing')
        require(Path(value['certifi']).is_file() and Path(value['certifi']).resolve().is_relative_to(PACK.resolve()), 'Non-private cacert')
        require(Path(value['engine']).resolve().is_relative_to((PACK / 'engine').resolve()), 'Non-private engine import')
        require(value['packages'] == {w['package']: w['version'] for w in lock['wheels']}, 'Package version mismatch')
        require(all((PACK / 'python' / dll).is_file() for dll in ('vcruntime140.dll', 'vcruntime140_1.dll')), 'VC DLL missing')
        require(not any((PACK / 'python/Lib/site-packages/bin' / name).exists() for name in LAUNCHERS), 'Excluded launcher still distributed')
        return value
    check('private-python', python_check)
    def node_check():
        version = run([PACK / 'node/node.exe', '--version'], probe_cwd, env)
        require(version == 'v24.19.0', 'Node version mismatch')
        cmd = Path(os.environ['SystemRoot']) / 'System32/cmd.exe'
        versions = {name: run([cmd, '/d', '/c', str(PACK / 'node' / (name + '.cmd')), '--version'], probe_cwd, env) for name in ('npm', 'npx')}
        require(all(v == '11.17.0' for v in versions.values()), 'npm/npx version mismatch')
        return {'node': version, **versions}
    check('private-node-npm-npx', node_check)
    def path_check(names):
        result = {}
        for name, folder in names.items():
            first = first_path_match(name, env['PATH'])
            require(Path(first).resolve().is_relative_to((PACK / folder).resolve()), 'Non-private first PATH match: ' + name)
            result[name] = first
        return result
    check('python-node-path-priority', lambda: path_check({'python': 'python', 'node': 'node', 'npm': 'node', 'npx': 'node'}))
    def ffmpeg_check():
        require(not lock['ffmpeg'].get('blocked'), 'Approved FFmpeg asset unavailable; AAC/scale not executed')
        ffmpeg, probe = PACK / 'ffmpeg/bin/ffmpeg.exe', PACK / 'ffmpeg/bin/ffprobe.exe'
        version = run([ffmpeg, '-version'], probe_cwd, env)
        probe_version = run([probe, '-version'], probe_cwd, env)
        require(lock['ffmpeg']['version'] in version, 'FFmpeg build mismatch')
        require(re.search(r'ffmpeg version n?9\.0(?:\.|-)', version), 'Not FFmpeg 9.0 series')
        require(lock['ffmpeg']['version'] in probe_version, 'ffprobe build mismatch')
        require(lock['ffmpeg'].get('releaseTag') not in (None, 'latest') and '-latest-' not in lock['ffmpeg']['filename'], 'Moving alias is not a lock')
        shared = '--enable-shared' in version
        require('--enable-gpl' not in version and '--enable-nonfree' not in version and shared == (FFMPEG_LINKAGE == 'shared'), 'Wrong license/build variant')
        if FFMPEG_LINKAGE == 'shared':
            require(bool(list((PACK / 'ffmpeg/bin').glob('*.dll'))), 'Shared DLLs missing')
        else:
            require(not list((PACK / 'ffmpeg/bin').glob('*.dll')), 'Static FFmpeg must not ship DLLs')
        encoders = run([ffmpeg, '-encoders'], probe_cwd, env)
        filters = run([ffmpeg, '-filters'], probe_cwd, env)
        aac = re.search(r'^\s*A\S*\s+aac\s+.*$', encoders, re.M)
        scale = re.search(r'^\s*\S+\s+scale\s+.*$', filters, re.M)
        require(aac, 'AAC encoder missing')
        require(scale, 'scale filter missing')
        media = scratch / 'aac-scale-test.mkv'
        run([ffmpeg, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=64x64:r=5', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
             '-t', '0.4', '-vf', 'scale=32:32', '-c:v', 'ffv1', '-c:a', 'aac', media], probe_cwd, env)
        facts = json.loads(run([probe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', media], probe_cwd, env))
        require(any(s['codec_name'] == 'aac' for s in facts['streams']), 'AAC output missing')
        require(any(s.get('width') == 32 and s.get('height') == 32 for s in facts['streams']), 'Scale output mismatch')
        require(float(facts['format']['duration']) > 0, 'Invalid media duration')
        return {'version': version.splitlines()[0], 'probe': probe_version.splitlines()[0], 'media': facts,
                'configuration': next(line for line in version.splitlines() if line.startswith('configuration:')),
                'sharedDlls': sorted(p.name for p in (PACK / 'ffmpeg/bin').glob('*.dll')),
                'aacEncoder': aac[0].strip(), 'scaleFilter': scale[0].strip(),
                'path': path_check({'ffmpeg': 'ffmpeg', 'ffprobe': 'ffmpeg'})}
    check('private-ffmpeg-aac-scale', ffmpeg_check)
    def engine_check():
        for name in ('pipeline/engine', 'pipeline/render_mux.py', 'tools/prepare_sample.py', 'R57-render-test'):
            require((PACK / 'engine' / name).exists(), 'Engine missing ' + name)
        require(not list((PACK / 'engine').rglob('.git')), 'Engine contains .git')
        for name in ('package.json', 'package-lock.json', 'src/index-r125.ts'):
            require((PACK / 'engine/R57-render-test' / name).is_file(), 'Render source missing ' + name)
        script = "import runpy; runpy.run_path(" + repr(str(PACK / 'engine/tools/prepare_sample.py')) + ", run_name='runtime_pack_import_check'); print('prepare import PASS')"
        prepare = run([PACK / 'python/python.exe', '-B', '-c', script], probe_cwd, env)
        return {'entry': 'engine/pipeline/engine', 'prepareImport': prepare, 'fullRenderExecuted': False}
    check('engine-layout', engine_check)
    def engine_whitelist_check():
        require(manifest['assembly']['engine']['status'] == 'PASS', 'Engine assembly incomplete')
        expected = manifest['enginePublishWhitelist'] if frozen_baseline is not None else engine_selection(Path(manifest['buildSources']['engine']), lock)[1]
        _, actual = engine_selection(PACK / 'engine', lock)
        if frozen_baseline is not None:
            require(len(actual['filesSha256']) == ENGINE_FILE_COUNT, 'Frozen Engine must contain %d files' % ENGINE_FILE_COUNT)
        require(actual == expected == manifest['enginePublishWhitelist'], 'Whitelist counts, content or provenance mismatch')
        all_files = {p.relative_to(PACK / 'engine').as_posix() for p in (PACK / 'engine').rglob('*') if p.is_file()}
        all_files.discard(ENGINE_VERSION_FILE)  # 由 engine-version-file 单独核对
        require(all_files == set(expected['filesSha256']), 'Engine contains files outside whitelist')
        require(not EXCLUDED_ENGINE_DOCUMENTS.intersection(all_files),
                'Officially excluded Engine document entered the Pack')
        allowed_dirs = {parent.as_posix() for name in all_files for parent in Path(name).parents if parent != Path('.')}
        actual_dirs = {p.relative_to(PACK / 'engine').as_posix() for p in (PACK / 'engine').rglob('*') if p.is_dir()}
        require(actual_dirs == allowed_dirs, 'Unexpected Engine directory')
        return {key: value for key, value in actual.items() if key != 'filesSha256'}
    check('engine-publish-whitelist', engine_whitelist_check)
    def engine_version_check():
        # APP3：VERSION 存在，内容与这次打包用的引擎（档案版本 + 锁定提交）一致
        # APP4：版本号取档案的 engineVersion；档案文件名仍是 v5.7
        path = PACK / 'engine' / ENGINE_VERSION_FILE
        require(path.is_file(), 'Engine VERSION missing')
        text = path.read_bytes().decode('ascii')
        require(text == engine_version_text(lock), 'Engine VERSION does not match the packaged engine')
        require(BASELINE.get('engineVersion', BASELINE['version']) == text.split()[0],
                'Engine version differs from baseline engineVersion')
        require(manifest['engineCommit'] == lock['engineCommit'] == BASELINE['engineCommit'], 'Engine commit differs from baseline')
        return {'path': 'engine/' + ENGINE_VERSION_FILE, 'content': text.strip()}
    check('engine-version-file', engine_version_check)
    def exclusions_check():
        expected = frozen_release_exclusions(manifest) if frozen_baseline is not None else audit_release_sources(manifest)
        require(read_json(PACK / 'licenses/provenance.json').get('releaseExclusions') == expected,
                'License provenance missing release exclusions')
        require(not (PACK / WEBPACK_CACHE).exists(), 'Webpack cache must not ship')
        require(not any(p.parent.name == '__pycache__' for p in (PACK / 'engine').rglob('*.pyc')),
                'Engine release bytecode must not ship')
        for entry in expected['engineBytecode']:
            require((PACK / entry['source']).is_file() and sha(PACK / entry['source']) == entry['sourceSha256'],
                    'Excluded bytecode source missing or changed')
        return expected
    check('release-cache-exclusions', exclusions_check)
    def records_check():
        require(manifest['releaseExclusions'].get('recordNormalization') == record_normalization_policy(),
                'RECORD normalization policy mismatch')
        records = {}
        for relative, removed in RECORD_EXCLUSIONS.items():
            record = PACK / 'python/Lib/site-packages' / relative
            names = [name for name, _ in record_rows(record)]
            require(not any(name in removed or (name.startswith('../../bin/') and name.endswith('.exe'))
                            for name in names), 'Excluded or unknown launcher RECORD row remains')
            require(all(not (PACK / 'python/Lib/site-packages/bin' / Path(name).name).exists()
                        for name in removed), 'Launcher returned after normalization')
            records[relative] = sha(record)
        return records
    check('python-release-records', records_check)
    def scene_import_check():
        script = "import json,pipeline.engine,pipeline.scene_build as s; print(json.dumps({'engine':pipeline.engine.__file__,'scene':s.__file__,'salign':s.salign.__file__}))"
        value = json.loads(run([PACK / 'python/python.exe', '-B', '-c', script], probe_cwd, env))
        require(Path(value['salign']).resolve() == (PACK / 'engine/R148/r148_subtitle_align.py').resolve(), 'S9 borrowed non-pack R148')
        require(all(Path(p).resolve().is_relative_to((PACK / 'engine').resolve()) for p in value.values()), 'Non-private Engine import')
        require(bool(list((PACK / 'engine/R168').glob('*.md'))), 'prepare_sample article absent')
        return value
    check('engine-scene-private-import', scene_import_check)
    def runtime_check():
        original = read_json(PACK / 'runtime/manifest.json')
        versions = {}
        for name, expected in original['remotion_packages'].items():
            actual = read_json(PACK / 'runtime/node_modules' / name / 'package.json')['version']
            require(actual == expected == '4.0.505', 'Remotion version mismatch: ' + name)
            versions[name] = actual
        for relative in (BROWSER, COMPOSITOR + '/remotion.exe', COMPOSITOR + '/ffmpeg.exe', COMPOSITOR + '/ffprobe.exe',
                         'runtime/node_modules/@esbuild/win32-x64/esbuild.exe', FONT):
            require((PACK / relative).is_file(), 'Runtime missing ' + relative)
        require(sha(PACK / FONT) == lock['runtime']['fontSha256'] == original['fonts']['fonts/LXGWWenKai-Regular.ttf']['sha256'], 'Font digest mismatch')
        browser_version = run([PACK / BROWSER, '--version'], probe_cwd, env)
        require('149.0.7790.0' in browser_version, 'Browser version mismatch')
        return {'remotion': versions, 'browser': browser_version, 'fontSha256': sha(PACK / FONT), 'compositorAndEsbuild': True}
    check('runtime-browser-font', runtime_check)
    def sample_check():
        # prepare_sample.py REL_R168 / THEORY_IMPORT identify the theory sample.
        sample_manifest = read_json(PACK / 'samples/manifest.json')
        files = sample_manifest['files']
        require(len(files) == sample_manifest['total_files'], 'Sample manifest count mismatch')
        for relative, facts in files.items():
            path = PACK / 'samples' / relative
            require(path.is_file() and path.stat().st_size == facts['size'], 'Sample file missing or truncated: ' + relative)
        theory_files = ['narration.txt', 'narration_meta.json', 'narration_tts.txt', 'sentences.json',
                        'query_response.json', 'audio/r168_doubao_full.wav', 'manifest.json']
        for relative in theory_files:
            name = 'R168/' + relative
            require(name in files and sha(PACK / 'samples' / name) == files[name]['sha256'], 'Theory sample mismatch: ' + name)
        v5_sample_manifest(PACK / 'samples')
        return {'sample': '示例篇/示例篇-截图', 'sourceRelativePath': 'R168', 'manifestFileCount': len(files),
                'theoryKeyFilesHashed': len(theory_files), 's7CacheFilesHashed': len(SAMPLE_CONTRACT['s7Cache'])}
    check('sample-presence', sample_check)
    def license_check():
        require(not manifest['limitations'] and manifest['assembly']['ffmpeg']['status'] == 'PASS', 'License archival incomplete')
        ofl = PACK / 'licenses/fonts/LXGW-WenKai/OFL.txt'
        require(ofl.is_file() and sha(ofl) == lock['ofl']['sha256'], 'OFL archive/hash mismatch')
        content = ofl.read_text(encoding='utf-8')
        require('SIL OPEN FONT LICENSE' in content and 'Version 1.1' in content, 'OFL content mismatch')
        provenance = read_json(PACK / 'licenses/provenance.json')
        require(provenance['font'] == lock['ofl'], 'OFL provenance mismatch')
        require(any(r['component'] == 'ffmpeg' for r in manifest['licenses']), 'FFmpeg license files missing')
        return {'oflSha256': sha(ofl), 'oflUrl': lock['ofl']['url'], 'legalReviewCompleted': False}
    check('license-completeness', license_check)
    def sources_check():
        if frozen_baseline is None:
            return audit_source_snapshots(manifest, lock)
        result = frozen_equality(frozen_baseline['files'], content_inventory(PACK),
                                 frozen_baseline['documents'], diagnostic_documents(PACK))
        require(result['passed'], 'Frozen inventory mismatch: ' + json.dumps(result))
        return result
    check('source-read-only' if frozen_baseline is None else 'frozen-pack-inventory', sources_check)
    def assets_check():
        if frozen_baseline is not None:
            require(manifest['assets'] == lock and sha(LOCK_PATH) == manifest['assetLockSha256'], 'Frozen asset identity mismatch')
            return {'assetIdentityMatches': True, 'payloadHashesCheckedByFrozenInventory': True}
        names = []
        for asset in [lock['python'], lock['node'], *lock['wheels'], lock['ffmpeg'], lock['ofl']]:
            path = CACHE / ('python-wheels' if asset in lock['wheels'] else asset.get('cacheDirectory', '')) / asset['filename']
            require(path.is_file() and sha(path) == asset['sha256'], 'Cached asset mismatch: ' + asset['filename'])
            names.append(path.name)
        require(sha(LOCK_PATH) == manifest['assetLockSha256'], 'Asset lock changed after assembly')
        return names
    check('asset-integrity', assets_check)
    def pollution_check():
        # Literal search rules are diagnostics, not executable paths or fallback configuration.
        rules = development_path_rules(manifest['buildSources'])
        pattern = re.compile(b'|'.join(re.escape(n) for n in development_path_needles(manifest['buildSources'])))
        raw_findings, allowed_findings, unallowed_findings, scanned = [], [], [], 0
        diagnostic = {'manifest/runtime-pack.json', 'manifest/local-verification.json', 'manifest/path-hygiene.json'}
        for root in (PACK, Path(__file__).parent):
            for path in sorted(root.rglob('*')):
                if not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                if root == PACK and relative in diagnostic:
                    continue  # Explicit build-source / observed local execution evidence only.
                if root != PACK and path.name in ('runtime_pack.py', 'README.md', 'test_release_hygiene.py'):
                    continue  # Search rules and explanatory documentation, no operational paths.
                scanned += 1
                tail, matched_markers = b'', set()
                with path.open('rb') as stream:
                    while chunk := stream.read(1024 * 1024):
                        block = tail + chunk.lower()
                        if pattern.search(block):
                            for name, variants in rules.items():
                                if any(needle in block for needle in variants):
                                    matched_markers.add(name)
                        tail = block[-1024:]
                if matched_markers:
                    payload_relative = relative if root == PACK else 'scripts/' + relative
                    classification = (classify_path_hygiene_finding(relative, matched_markers)
                                      if root == PACK else None)
                    finding = {'file': payload_relative, 'markers': sorted(matched_markers),
                               'classification': classification or 'unallowed'}
                    raw_findings.append(finding)
                    (allowed_findings if classification else unallowed_findings).append(finding)
        write_json(sink / 'manifest/path-hygiene.json', {
            'scannedFiles': scanned,
            'rawFindings': raw_findings,
            'classifiedAllowedFindings': allowed_findings,
            'unallowedFindings': unallowed_findings,
            'findings': [finding['file'] for finding in unallowed_findings],
            'diagnosticExclusions': sorted(diagnostic),
            'rulesAndDocsExcluded': ['runtime_pack.py', 'README.md', 'test_release_hygiene.py'],
        })
        require(not unallowed_findings,
                'Unallowed development-machine prefix found; review manifest/path-hygiene.json (no automatic rewrite)')
        return {'scannedFiles': scanned, 'rawHits': len(raw_findings),
                'classifiedAllowedHits': len(allowed_findings), 'unallowedHits': 0}
    check('path-hygiene', pollution_check)
    def path_budget_check():
        require(manifest['article2videoVersion'] == BASELINE['version'] and
                manifest['engineCommit'] == BASELINE['engineCommit'], 'APP Engine baseline differs')
        require(manifest['runtimePathBudget'] == runtime_path_budget(), 'Runtime path budget differs')
        return manifest['runtimePathBudget']
    check('runtime-path-budget', path_budget_check)
    check('assembly-completeness', lambda: require(all(v['status'] == 'PASS' for v in manifest['assembly'].values()), 'Assembly has blocked components'))
    result = {'schemaVersion': 1, 'verifiedAt': now(), 'checks': checks, 'passed': all(c['status'] == 'PASS' for c in checks),
              'fixedEnvironment': FIXED_ENV, 'pathPrefixes': ['python', 'node', 'ffmpeg/bin'],
              'sandboxExecuted': False, 'realRenderExecuted': False, 'paidApiCalls': 0}
    write_json(sink / 'manifest/local-verification.json', result)
    manifest['localVerificationPassed'] = result['passed']
    manifest['lastVerifiedAt'] = result['verifiedAt']
    write_json(sink / 'manifest/runtime-pack.json', manifest)
    hash_manifest(manifest, lock, include_downloads=frozen_baseline is None, diagnostic_root=diagnostic_root)
    return 0 if result['passed'] else 2


def external_evidence_root(path):
    """Only ignored release evidence is writable; neither Pack can be a sink."""
    path = Path(path).absolute()
    assert_local(path)
    path = path.resolve()
    root = REPO / '.cache/r11-release'
    if not path.is_relative_to(root) or any(path.is_relative_to(p) for p in frozen_pack_roots().values()):
        raise RuntimeError('Diagnostic sink must be outside Packs in .cache/r11-release')
    return path


def frozen_pack_roots():
    return {'main': REPO / '.cache' / PACK_NAME, 'fresh': REPO / '.cache' / (PACK_NAME + '-fresh')}


def verify_frozen_release(lock, baseline_path, evidence_root):
    """Release entry: independent frozen input, both Packs, exact finally postchecks."""
    global PACK
    baseline_path = Path(baseline_path)
    assert_local(baseline_path)
    baseline = read_json(baseline_path)
    if (baseline.get('schemaVersion') != 1 or baseline.get('engineCommit') != BASELINE['engineCommit']
            or set(baseline.get('packs', {})) != {'main', 'fresh'}):
        raise RuntimeError('Frozen baseline identity/schema mismatch')
    roots = frozen_pack_roots()
    for root in roots.values():
        assert_local(root)
    evidence = external_evidence_root(evidence_root) / str(uuid.uuid4())
    external_evidence_root(evidence)
    evidence.mkdir(parents=True, exist_ok=False)
    original_pack = PACK
    before, documents = {}, {}
    result = {'passed': False, 'mode': 'frozen', 'baselineSha256': sha(baseline_path),
              'baselinePath': str(baseline_path), 'evidenceRoot': str(evidence),
              'packs': {}, 'pack_postcheck': {}, 'realRenderExecuted': False, 'paidApiCalls': 0}
    try:
        # Reject drift before executing any probe from either Pack.
        for label, root in roots.items():
            before[label] = content_inventory(root)
            documents[label] = diagnostic_documents(root)
            frozen = baseline['packs'][label]
            identity = frozen_equality(frozen['files'], before[label], frozen['documents'], documents[label])
            result['packs'][label] = {'identity': identity}
            write_json(evidence / (label + '-before.json'), before[label])
            if not identity['passed']:
                raise RuntimeError(label + ' frozen inventory differs')
        result['equality'] = frozen_equality(before['main'], before['fresh'], documents['main'], documents['fresh'])
        if not result['equality']['passed']:
            raise RuntimeError('Main/fresh frozen equality failed')
        for label, root in roots.items():
            PACK = root
            sink = evidence / label
            code = verify(lock, frozen_baseline=baseline['packs'][label], diagnostic_root=sink)
            checks = read_json(sink / 'manifest/local-verification.json')
            result['packs'][label]['verification'] = checks
            if code != 0 or not checks['passed'] or len(checks['checks']) != ASSEMBLY_CHECK_COUNT:
                raise RuntimeError(label + ' frozen %d-check verification failed' % ASSEMBLY_CHECK_COUNT)
        result['passed'] = True
    except Exception as error:
        result['error'] = str(error)
    finally:
        PACK = original_pack
        for label, root in roots.items():
            try:
                if label not in before:
                    state = pack_postcheck(None)
                else:
                    after = content_inventory(root)
                    write_json(evidence / (label + '-after.json'), after)
                    state = pack_postcheck(before[label], after)
            except Exception as error:
                state = pack_postcheck(before.get(label), error=error)
            result['pack_postcheck'][label] = state
            if state['status'] != 'UNCHANGED':
                result['passed'] = False
        write_json(evidence / 'summary.json', result)
        print(json.dumps({'passed': result['passed'], 'evidenceRoot': str(evidence),
                          'pack_postcheck': result['pack_postcheck'], 'error': result.get('error')}, ensure_ascii=False), flush=True)
    return 0 if result['passed'] else 2


def main():
    global PACK
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    builder = sub.add_parser('build')
    for arg in ('engine', 'runtime', 'samples', 'delivery'):
        builder.add_argument('--' + arg, required=True)
    builder.add_argument('--fresh', action='store_true')
    builder.add_argument('--rebuild', action='store_true')
    verifier = sub.add_parser('verify')
    verifier.add_argument('--baseline', type=Path, default=REPO / '.cache/r11-release/frozen-baseline.json')
    verifier.add_argument('--evidence-root', type=Path, default=REPO / '.cache/r11-release/frozen-verification')
    assembly = sub.add_parser('verify-assembly', help='Assembly/source audit; updates Pack diagnostics')
    assembly.add_argument('--fresh', action='store_true')
    sub.add_parser('environment-template')
    sub.add_parser('fetch-profile-assets', help='Download only the assets the profile overrides (exact SHA, HTTPS)')
    fetcher = sub.add_parser('fetch-assets', help='APP3: all external assets into the download cache, mirror first')
    fetcher.add_argument('--mirror', default='')
    syncer = sub.add_parser('sync-mirror', help='APP3: fill the asset mirror with every asset this profile needs')
    syncer.add_argument('--mirror', required=True)
    lister = sub.add_parser('mirror-manifest', help='APP3: write 资产清单.md into the mirror (no download)')
    lister.add_argument('--mirror', required=True)
    args = parser.parse_args()
    if getattr(args, 'fresh', False) and getattr(args, 'rebuild', False):
        raise RuntimeError('--fresh and --rebuild are mutually exclusive')
    if getattr(args, 'fresh', False):
        PACK = REPO / '.cache' / (PACK_NAME + '-fresh')
    assert_local(PACK)
    assert_local(CACHE)
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError('Build tool must be Python 3.12; final checks use pack Python')
    lock = read_json(LOCK_PATH)
    if PROFILE:
        # 档案覆盖：引擎 commit 和 FFmpeg 资产；其他工具链资产沿用 assets-lock.json
        lock['engineCommit'] = BASELINE['engineCommit']
        if BASELINE.get('ffmpeg'):
            lock['ffmpeg'] = {k: v for k, v in BASELINE['ffmpeg'].items() if k not in ('linkage', 'shaSource')}
    if args.command == 'fetch-profile-assets':
        if not PROFILE or not BASELINE.get('ffmpeg'):
            raise RuntimeError('No profile asset to fetch')
        CACHE.mkdir(parents=True, exist_ok=True)
        print(download(lock['ffmpeg']).name)
        return 0
    if args.command in ('fetch-assets', 'sync-mirror', 'mirror-manifest'):
        mirror = Path(args.mirror) if args.mirror else None
        if mirror is not None and not mirror.is_dir():
            raise RuntimeError('Asset mirror directory not found: ' + str(mirror))
        CACHE.mkdir(parents=True, exist_ok=True)
        if args.command == 'fetch-assets':
            sources = fetch_assets(lock, mirror)
            print(json.dumps(sources, ensure_ascii=False))
        elif args.command == 'sync-mirror':
            print(json.dumps({'added': sync_mirror(lock, mirror)}, ensure_ascii=False))
        else:
            print(write_mirror_manifest(lock, mirror))
        return 0
    if args.command == 'verify':
        return verify_frozen_release(lock, args.baseline, args.evidence_root)
    CACHE.mkdir(parents=True, exist_ok=True)
    if args.command == 'environment-template':
        # No inherited environment serialization. Resolve these relative paths at launch time.
        print(json.dumps({'pathPrefixesRelativeToPack': ['python', 'node', 'ffmpeg/bin'], 'appendOriginalPath': True, 'variables': FIXED_ENV}))
        return 0
    return build(args, lock) if args.command == 'build' else verify(lock)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        sys.exit(1)
