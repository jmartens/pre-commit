from __future__ import annotations

import contextlib
import functools
import os
import sys
from collections.abc import Generator
from collections.abc import Sequence

import pre_commit.constants as C
from pre_commit import lang_base
from pre_commit.envcontext import envcontext
from pre_commit.envcontext import PatchesT
from pre_commit.envcontext import UNSET
from pre_commit.envcontext import Var
from pre_commit.parse_shebang import find_executable
from pre_commit.prefix import Prefix
from pre_commit.util import CalledProcessError
from pre_commit.util import cmd_output
from pre_commit.util import cmd_output_b
from pre_commit.util import win_exe

try:
    import uv  # Attempt to import uv for handling virtualenvs and installation
except ImportError:
    uv = None  # If uv is not present, fall back to normal method

ENVIRONMENT_DIR = 'py_env'
run_hook = lang_base.basic_run_hook


@functools.cache
def _version_info(exe: str) -> str:
    prog = 'import sys;print(".".join(str(p) for p in sys.version_info))'
    try:
        return cmd_output(exe, '-S', '-c', prog)[1].strip()
    except CalledProcessError:
        return f'<<error retrieving version from {exe}>>'


def _read_pyvenv_cfg(filename: str) -> dict[str, str]:
    ret = {}
    with open(filename, encoding='UTF-8') as f:
        for line in f:
            try:
                k, v = line.split('=')
            except ValueError:  # blank line / comment / etc.
                continue
            else:
                ret[k.strip()] = v.strip()
    return ret


def bin_dir(venv: str) -> str:
    """On windows there's a different directory for the virtualenv"""
    bin_part = 'Scripts' if sys.platform == 'win32' else 'bin'
    return os.path.join(venv, bin_part)


def get_env_patch(venv: str) -> PatchesT:
    return (
        ('PIP_DISABLE_PIP_VERSION_CHECK', '1'),
        ('PYTHONHOME', UNSET),
        ('VIRTUAL_ENV', venv),
        ('PATH', (bin_dir(venv), os.pathsep, Var('PATH'))),
    )


def _find_by_py_launcher(
        version: str,
) -> str | None:  # pragma: no cover (windows only)
    if version.startswith('python'):
        num = version.removeprefix('python')
        cmd = ('py', f'-{num}', '-c', 'import sys; print(sys.executable)')
        env = dict(os.environ, PYTHONIOENCODING='UTF-8')
        try:
            return cmd_output(*cmd, env=env)[1].strip()
        except CalledProcessError:
            pass
    return None


def _find_by_sys_executable() -> str | None:
    def _norm(path: str) -> str | None:
        _, exe = os.path.split(path.lower())
        exe, _, _ = exe.partition('.exe')
        if exe not in {'python', 'pythonw'} and find_executable(exe):
            return exe
        return None

    for path in (sys.executable, os.path.realpath(sys.executable)):
        exe = _norm(path)
        if exe:
            return exe
    return None


@functools.lru_cache(maxsize=1)
def get_default_version() -> str:  # pragma: no cover (platform dependent)
    exe = _find_by_sys_executable()
    if exe:
        return exe

    exe = f'python{sys.version_info[0]}.{sys.version_info[1]}'
    if find_executable(exe):
        return exe

    if _find_by_py_launcher(exe):
        return exe

    return C.DEFAULT


def _sys_executable_matches(version: str) -> bool:
    if version == 'python':
        return True
    elif not version.startswith('python'):
        return False

    try:
        info = tuple(int(p) for p in version.removeprefix('python').split('.'))
    except ValueError:
        return False

    return sys.version_info[:len(info)] == info


def norm_version(version: str) -> str | None:
    if version == C.DEFAULT:
        return None
    elif _sys_executable_matches(version):
        return None

    if sys.platform == 'win32':
        version_exec = _find_by_py_launcher(version)
        if version_exec:
            return version_exec

        version_exec = find_executable(version)
        if version_exec and version_exec != version:
            return version_exec

    return os.path.expanduser(version)


@contextlib.contextmanager
def in_env(prefix: Prefix, version: str) -> Generator[None]:
    envdir = lang_base.environment_dir(prefix, ENVIRONMENT_DIR, version)
    with envcontext(get_env_patch(envdir)):
        yield


def health_check(prefix: Prefix, version: str) -> str | None:
    envdir = lang_base.environment_dir(prefix, ENVIRONMENT_DIR, version)
    pyvenv_cfg = os.path.join(envdir, 'pyvenv.cfg')

    if not os.path.exists(pyvenv_cfg):
        return 'pyvenv.cfg does not exist (old virtualenv?)'

    exe_name = win_exe('python')
    py_exe = prefix.path(bin_dir(envdir), exe_name)
    cfg = _read_pyvenv_cfg(pyvenv_cfg)

    if 'version_info' not in cfg:
        return "created virtualenv's pyvenv.cfg is missing `version_info`"

    virtualenv_version = _version_info.__wrapped__(py_exe)
    if virtualenv_version != cfg['version_info']:
        return (
            f'virtualenv python version did not match created version:\n'
            f'- actual version: {virtualenv_version}\n'
            f'- expected version: {cfg["version_info"]}\n'
        )

    if 'base-executable' not in cfg:
        return None

    base_exe_version = _version_info(cfg['base-executable'])
    if base_exe_version != cfg['version_info']:
        return (
            f'base executable python version does not match created version:\n'
            f'- base-executable version: {base_exe_version}\n'
            f'- expected version: {cfg["version_info"]}\n'
        )
    else:
        return None


def install_environment(
        prefix: Prefix,
        version: str,
        additional_dependencies: Sequence[str],
) -> None:
    envdir = lang_base.environment_dir(prefix, ENVIRONMENT_DIR, version)
    python = norm_version(version)
    install_cmd = [sys.executable, '-mpip', 'install', '.', *additional_dependencies]

    if uv:
        create_venv_with_uv(envdir, python)
        install_with_uv(install_cmd)
    else:
        run_sync_install(envdir, python, install_cmd, prefix, version)


def create_venv_with_uv(envdir: str, python: str | None) -> None:
    # Use uv to create the virtual environment
    uv.venv.create(envdir, python=python)


def install_with_uv(install_cmd: list) -> None:
    # Use uv to install the dependencies
    uv.pip.sync(install_cmd)


def run_sync_install(envdir: str, python: str, install_cmd: list, prefix: Prefix, version: str) -> None:
    # Fallback to normal installation method (using virtualenv and pip)
    venv_cmd = [sys.executable, '-mvirtualenv', envdir]
    if python is not None:
        venv_cmd.extend(('-p', python))
    cmd_output_b(*venv_cmd, cwd='/')
    
    # Install dependencies
    with in_env(prefix, version):
        lang_base.setup_cmd(prefix, install_cmd)
