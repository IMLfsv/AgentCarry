"""Build and verify a standalone Windows x64 ZIP. Run in a clean build venv."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile
from desktop_service import VERSION

SOURCE_FILES = [
    'app.py', 'desktop.py', 'desktop_service.py', 'self_test.py', 'build_release.py', 'filesystem.py',
    'porter.py', 'migrate.py', 'transfer.py', 'publication.py', 'native_install.py',
    'antigravity_trial.py', 'demo.py', 'README.md', 'QUICKSTART.txt', 'ACCEPTANCE.md',
    'VALIDATION.txt', 'RELEASE_NOTES.md', 'LICENSE', 'THIRD_PARTY_NOTICES.txt', 'licenses/Tcl.txt', '.gitignore', 'requirements-build.txt', 'requirements.txt',
    'adapters/__init__.py', 'adapters/antigravity.py', 'adapters/antigravity_codec.py',
    'examples/selection.example.json', '.github/workflows/build.yml',
    'tests/test_porter.py', 'tests/test_migrate.py', 'tests/test_native_install.py',
    'tests/test_antigravity_trial.py', 'tests/test_desktop_service.py',
]


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def build(out):
    if sys.platform != 'win32' or platform.architecture()[0] != '64bit':
        raise RuntimeError('Build the Windows x64 release on 64-bit Windows.')
    out, source = out.absolute(), Path(__file__).resolve().parent
    if out.exists():
        raise RuntimeError('Use a new build output directory; no existing output is overwritten.')
    for relative in SOURCE_FILES:
        if not (source / relative).is_file():
            raise RuntimeError('Missing release source: ' + relative)
    out.mkdir(parents=True)
    env = dict(os.environ)
    env['PYINSTALLER_CONFIG_DIR'] = str(out / 'pyinstaller-cache')
    command = [sys.executable, '-m', 'PyInstaller', '--onedir', '--windowed', '--noupx',
               '--name', 'AgentCarry', '--distpath', str(out / 'dist'),
               '--workpath', str(out / 'build'), '--specpath', str(out / 'spec'),
               str(source / 'app.py')]
    subprocess.run(command, cwd=source, env=env, check=True)
    product = out / 'dist/AgentCarry'
    for name in ('QUICKSTART.txt', 'README.md', 'ACCEPTANCE.md', 'VALIDATION.txt', 'LICENSE', 'THIRD_PARTY_NOTICES.txt'):
        shutil.copy2(source / name, product / name)
    licenses = product / 'licenses'
    licenses.mkdir()
    base = Path(sys.base_prefix)
    import PyInstaller
    import importlib.metadata
    distribution = importlib.metadata.distribution('pyinstaller')
    pyi_license = next(distribution.locate_file(p) for p in distribution.files if str(p).endswith('licenses/COPYING.txt'))
    tcl_license = base / 'tcl/tcl8.6/license.terms'
    if not tcl_license.is_file():
        tcl_license = source / 'licenses/Tcl.txt'
    for original, name in [(base / 'LICENSE.txt', 'Python.txt'),
                           (tcl_license, 'Tcl.txt'),
                           (base / 'tcl/tk8.6/license.terms', 'Tk.txt'),
                           (pyi_license, 'PyInstaller.txt')]:
        if not original.is_file():
            raise RuntimeError('Missing runtime redistribution license: ' + str(original))
        shutil.copy2(original, licenses / name)
    zstd_dist = importlib.metadata.distribution('zstandard')
    zstd_licenses = [p for p in zstd_dist.files if '/licenses/' in str(p).replace('\\', '/')]
    if not zstd_licenses:
        raise RuntimeError('Missing Zstandard redistribution licenses')
    for entry in zstd_licenses:
        shutil.copy2(zstd_dist.locate_file(entry), licenses / ('zstandard-' + Path(entry).name))
    product_hashes = {p.relative_to(product).as_posix(): sha(p) for p in sorted(product.rglob('*')) if p.is_file()}
    (product / 'FILE_HASHES.json').write_text(json.dumps(product_hashes, indent=2) + '\n', encoding='utf-8')
    binary_zip = out / f'AgentCarry-{VERSION}-windows-x64.zip'
    with zipfile.ZipFile(binary_zip, 'x', compression=zipfile.ZIP_DEFLATED) as z:
        for file in sorted(product.rglob('*')):
            if file.is_file():
                z.write(file, 'AgentCarry/' + file.relative_to(product).as_posix())
    # Test the ZIP users receive after relocating it, without Python on PATH,
    # and with a fresh child-only user profile. No global environment is changed.
    sandbox = out / 'download-test' / '另一位用户'
    sandbox.mkdir(parents=True)
    with zipfile.ZipFile(binary_zip) as z:
        assert z.testzip() is None
        z.extractall(sandbox)
    executable = sandbox / 'AgentCarry/AgentCarry.exe'
    profile = sandbox / 'fresh-profile'
    for part in ('AppData/Local', 'AppData/Roaming', 'temp'):
        (profile / part).mkdir(parents=True, exist_ok=True)
    windows = Path(os.environ['SystemRoot'])
    child_env = {key: value for key, value in os.environ.items() if key.upper() in {
        'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE'}}
    child_env.update({'USERPROFILE': str(profile), 'APPDATA': str(profile / 'AppData/Roaming'),
                      'LOCALAPPDATA': str(profile / 'AppData/Local'), 'TEMP': str(profile / 'temp'),
                      'TMP': str(profile / 'temp'), 'PATH': str(windows / 'System32') + ';' + str(windows)})
    results = {}
    for flag, name in [('--self-test', 'migration'), ('--smoke-gui', 'gui')]:
        report = sandbox / (name + '-report.json')
        subprocess.run([str(executable), flag, '--report', str(report)], cwd=sandbox, env=child_env,
                       check=True, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
        results[name] = json.loads(report.read_text(encoding='utf-8'))
    if not results['migration'].get('passed') or not results['gui'].get('gui_initialized'):
        raise RuntimeError('Packaged application self-test failed.')
    source_zip = out / f'AgentCarry-{VERSION}-source.zip'
    with zipfile.ZipFile(source_zip, 'x', compression=zipfile.ZIP_DEFLATED) as z:
        for rel in SOURCE_FILES:
            z.write(source / rel, 'AgentCarry/' + rel)
    report = {'version': VERSION, 'platform': 'windows-x64', 'python_bundled': platform.python_version(),
              'pyinstaller': PyInstaller.__version__, 'verification': results,
              'environment': {'relocated': True, 'unicode_path': True, 'python_on_path': False,
                              'fresh_child_profile': True, 'different_physical_computer_tested': False},
              'artifacts': [{'file': p.name, 'bytes': p.stat().st_size, 'sha256': sha(p)}
                            for p in (binary_zip, source_zip)],
              'native_live_scope': 'Only the previously validated short text fixture on App 2.32.2.',
              'code_signed': False}
    (out / 'release-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (out / 'SHA256SUMS.txt').write_text(''.join(f'{item["sha256"]}  {item["file"]}\n' for item in report['artifacts']), encoding='ascii')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    build(parser.parse_args().out)
