"""Shrinkwrap self-updater (see the development guide, section 19).

Finds the newest vX.Y.Z tag of the GitHub repo and installs that release over
the live bundle with a stage-then-swap, so a failed update never leaves a
broken install. Pure Python with no Fusion imports, so it can be tested
outside Fusion.

Differences from the guide's reference design:
- The repo is public, so no login is needed. A Git Credential Manager token is
  used if one is already cached (raises GitHub's rate limit), but the user is
  never prompted and Git doesn't have to be installed.
- A downloaded bundle is checked before it goes live: it must contain the
  add-in files and its SHRINKWRAP_VERSION must match the tag.
"""

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

GITHUB_OWNER = 'Danecca-Ltd'
GITHUB_REPO = 'Shrinkwrap'
USER_AGENT = 'Shrinkwrap-Fusion-Addin'
BUNDLE_NAME = 'Shrinkwrap.bundle'
MAIN_FILE = 'Contents/Shrinkwrap.py'      # inside the bundle
PRESERVE = ('.env',)                      # user files carried over an update
TAG_PATTERN = re.compile(r'^v?\d+\.\d+\.\d+$')
VERSION_PATTERN = re.compile(r"^SHRINKWRAP_VERSION\s*=\s*['\"]([^'\"]+)['\"]", re.M)


class UpdateError(Exception):
    """An update problem worth showing to the user as-is."""


def parse_version(version):
    """'v1.12.3' or '1.12.3' -> (1, 12, 3), so versions compare correctly."""
    parts = []
    for part in version.lstrip('v').split('.'):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


# ----------------------------------------------------------------------------
# Optional GitHub token from Git Credential Manager (never interactive)
# ----------------------------------------------------------------------------

def find_git_executable():
    git = shutil.which('git')
    if git:
        return git
    if sys.platform == 'win32':
        candidates = [
            os.path.join(os.environ.get('ProgramFiles', ''), 'Git', 'cmd', 'git.exe'),
            os.path.join(os.environ.get('ProgramFiles(x86)', ''), 'Git', 'cmd', 'git.exe'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Git', 'cmd', 'git.exe'),
        ]
    elif sys.platform == 'darwin':
        candidates = ['/usr/bin/git', '/usr/local/bin/git', '/opt/homebrew/bin/git']
    else:
        candidates = []
    return next((p for p in candidates if p and os.path.isfile(p)), None)


def cached_github_token():
    """A GitHub token if Git Credential Manager already has one, else None.

    Never opens a login window: GCM_INTERACTIVE=never / GIT_TERMINAL_PROMPT=0.
    """
    git = find_git_executable()
    if not git:
        return None
    env = os.environ.copy()
    env['GCM_INTERACTIVE'] = 'never'
    env['GIT_TERMINAL_PROMPT'] = '0'
    try:
        proc = subprocess.run(
            [git, 'credential', 'fill'], input='protocol=https\nhost=github.com\n\n',
            capture_output=True, text=True, timeout=15, env=env,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for line in proc.stdout.splitlines():
            if line.startswith('password='):
                return line.split('=', 1)[1] or None
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------
# HTTPS with the Windows expired-anchor workaround (guide section 18)
# ----------------------------------------------------------------------------

_ssl_ctx_cache = {}


def _ssl_context(root_only=False):
    """Fully-verifying SSL context; root_only builds it from the Windows ROOT store only."""
    if root_only in _ssl_ctx_cache:
        return _ssl_ctx_cache[root_only]
    ctx = None
    if root_only and hasattr(ssl, 'enum_certificates'):
        try:
            candidate = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            candidate.check_hostname = True
            candidate.verify_mode = ssl.CERT_REQUIRED
            loaded = 0
            for cert_bytes, encoding, _trust in ssl.enum_certificates('ROOT'):
                if encoding != 'x509_asn':
                    continue
                try:
                    candidate.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(cert_bytes))
                    loaded += 1
                except Exception:
                    continue
            if loaded:
                ctx = candidate
        except Exception:
            ctx = None
    if ctx is None:
        ctx = ssl.create_default_context()
    _ssl_ctx_cache[root_only] = ctx
    return ctx


def _is_expired_anchor_error(exc):
    inner = getattr(exc, 'reason', None) or exc
    text = str(exc).lower()
    verify_message = (getattr(inner, 'verify_message', '') or '').lower()
    if ('certificate_verify_failed' not in text
            and getattr(inner, 'reason', '') != 'CERTIFICATE_VERIFY_FAILED'):
        return False
    return 'certificate has expired' in text or 'expired' in verify_message


def _urlopen(request, timeout):
    default_ctx = _ssl_context()
    try:
        return urllib.request.urlopen(request, context=default_ctx, timeout=timeout)
    except (urllib.error.URLError, ssl.SSLError) as e:
        if not _is_expired_anchor_error(e):
            raise
        fallback = _ssl_context(root_only=True)
        if fallback is default_ctx:
            raise
        return urllib.request.urlopen(request, context=fallback, timeout=timeout)


def _github(url, token, timeout):
    """Open a GitHub API URL; a rejected token falls back to anonymous access."""
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': USER_AGENT}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        return _urlopen(urllib.request.Request(url, headers=headers), timeout)
    except urllib.error.HTTPError as e:
        if token and e.code in (401, 403):
            return _github(url, None, timeout)
        if e.code == 403 and e.headers.get('X-RateLimit-Remaining') == '0':
            raise UpdateError('GitHub\'s hourly limit for update checks from this network '
                              'has been reached. Try again in an hour.') from e
        raise UpdateError(f'GitHub returned an error ({e.code} {e.reason}).') from e
    except (urllib.error.URLError, ssl.SSLError, OSError) as e:
        raise UpdateError(f'Could not reach GitHub: {getattr(e, "reason", e)}') from e


# ----------------------------------------------------------------------------
# Checking and installing
# ----------------------------------------------------------------------------

def latest_release(token=None, timeout=10):
    """(version, tag) of the newest vX.Y.Z tag on GitHub, e.g. ('1.3.0', 'v1.3.0')."""
    url = f'https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/tags?per_page=100'
    with _github(url, token, timeout) as response:
        tags = json.loads(response.read().decode('utf-8'))
    names = [t.get('name', '') for t in tags if TAG_PATTERN.match(t.get('name', ''))]
    if not names:
        raise UpdateError('No released versions were found on GitHub.')
    tag = max(names, key=parse_version)
    return tag.lstrip('v'), tag


def is_newer(remote, local):
    return parse_version(remote) > parse_version(local)


def addins_install_path():
    """Fusion's ApplicationPlugins folder for this platform."""
    if sys.platform == 'win32':
        base = os.environ.get('APPDATA', '')
    elif sys.platform == 'darwin':
        base = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support')
    else:
        raise UpdateError(f'Unsupported platform: {sys.platform}')
    return os.path.join(base, 'Autodesk', 'ApplicationPlugins')


def _safe_extract(zf, member, dest):
    """Extract one member, refusing paths that would land outside dest."""
    target = os.path.realpath(os.path.join(dest, member.filename))
    if not target.startswith(os.path.realpath(dest) + os.sep):
        raise UpdateError(f'Unsafe path in the update archive: {member.filename}')
    zf.extract(member, dest)


def _bundle_version(bundle_dir):
    try:
        with open(os.path.join(bundle_dir, *MAIN_FILE.split('/')), encoding='utf-8') as f:
            match = VERSION_PATTERN.search(f.read())
        return match.group(1) if match else None
    except OSError:
        return None


def download_and_install(tag, token=None, addins_dir=None, log=lambda msg: None):
    """Download release `tag` and install it with a stage-then-swap.

    Returns the installed bundle path. Raises UpdateError (or OSError) on
    failure; the live bundle is then left as it was.
    """
    addins_dir = addins_dir or addins_install_path()
    temp_dir = tempfile.mkdtemp(prefix='shrinkwrap_update_')
    try:
        # 1. Download the release archive.
        url = f'https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/zipball/{tag}'
        zip_path = os.path.join(temp_dir, 'update.zip')
        log(f'Update: downloading {tag}')
        with _github(url, token, 60) as response, open(zip_path, 'wb') as f:
            shutil.copyfileobj(response, f)

        # 2. Extract just the .bundle folder. GitHub archives have a single
        #    root folder like "Danecca-Ltd-Shrinkwrap-abc1234/".
        extract_dir = os.path.join(temp_dir, 'extracted')
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            if not names:
                raise UpdateError('The downloaded update is empty.')
            root = names[0].split('/')[0]
            bundles = sorted({n.split('/')[1] for n in names
                              if n.count('/') >= 2 and n.split('/')[1].endswith('.bundle')})
            if not bundles:
                raise UpdateError('The downloaded update has no .bundle folder.')
            bundle_name = BUNDLE_NAME if BUNDLE_NAME in bundles else bundles[0]
            prefix = f'{root}/{bundle_name}/'
            for member in zf.infolist():
                if member.filename.startswith(prefix):
                    _safe_extract(zf, member, extract_dir)
        new_bundle = os.path.join(extract_dir, root, bundle_name)

        # 3. Check it before it goes anywhere near the live install.
        if not os.path.isfile(os.path.join(new_bundle, 'PackageContents.xml')):
            raise UpdateError('The downloaded update is incomplete (no PackageContents.xml).')
        found = _bundle_version(new_bundle)
        if found != tag.lstrip('v'):
            raise UpdateError(f'The downloaded update says it is version {found}, '
                              f'not {tag.lstrip("v")}; not installing it.')

        # 4. Carry over user files (none today; kept for parity with the guide).
        install_path = os.path.join(addins_dir, bundle_name)
        for name in PRESERVE:
            src = os.path.join(install_path, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(new_bundle, name))

        # 5. Stage-then-swap: the slow copy goes to a sibling folder; the live
        #    install only changes in two quick renames, rolled back on failure.
        os.makedirs(addins_dir, exist_ok=True)
        staging = install_path + '.new'
        backup = install_path + '.old'
        for leftover in (staging, backup):
            if os.path.isdir(leftover):
                shutil.rmtree(leftover, ignore_errors=True)
        log(f'Update: staging {bundle_name}')
        shutil.copytree(new_bundle, staging)

        had_live = os.path.isdir(install_path)
        try:
            if had_live:
                os.replace(install_path, backup)
        except OSError as e:
            shutil.rmtree(staging, ignore_errors=True)
            raise UpdateError(f'Could not replace the installed add-in ({e}). '
                              'Close Fusion and try again.') from e
        try:
            os.replace(staging, install_path)
        except OSError as e:
            if had_live and os.path.isdir(backup) and not os.path.isdir(install_path):
                os.replace(backup, install_path)
            raise UpdateError(f'Could not install the update ({e}); the previous version '
                              'was restored.') from e
        shutil.rmtree(backup, ignore_errors=True)
        log(f'Update: installed {tag} to {install_path}')
        return install_path
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
