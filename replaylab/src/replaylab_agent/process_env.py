"""Minimal, explicit environments for vendor Agent subprocesses."""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import AuthenticationError, ConfigurationError

if TYPE_CHECKING:
    from .profiles import Profile

SAFE_EXTERNAL_ENV = frozenset(
    {
        "APPDATA",
        "CODEX_HOME",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NODE_EXTRA_CA_CERTS",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)

# A model credential must never reuse an execution-, filesystem-, trust-, or ReplayLab-control
# variable. qrenderdoc injects the selected API-key value into AgentHost's environment; accepting
# one of these names could turn a secret into PATH/HOME/CODEX_HOME or propagate it to a supposedly
# credential-free MCP/vendor process.
REPLAYLAB_CONTROL_ENV = frozenset(
    {
        "RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY",
        "REPLAYLAB_AI_CLI",
        "REPLAYLAB_ALLOWED_TOOLS",
        "REPLAYLAB_INSTANCE_ID",
        "REPLAYLAB_MCP_HOST_ARGS",
        "REPLAYLAB_TOOL_AUDIT",
        "REPLAYLAB_TOOL_SCOPE",
    }
)
RESERVED_CREDENTIAL_ENV = SAFE_EXTERNAL_ENV | REPLAYLAB_CONTROL_ENV | frozenset(
    {
        "ALL_PROXY",
        "CURL_CA_BUNDLE",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NO_PROXY",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "REQUESTS_CA_BUNDLE",
        "VIRTUAL_ENV",
    }
)

# Only these audited catalog presets receive installation-layout fallbacks.  A
# custom ACP command remains explicit and is resolved only from its absolute
# path or PATH, exactly as before.
_KNOWN_VENDOR_COMMANDS = {
    "codex": "codex",
    "copilot-acp": "copilot",
    "gemini-acp": "gemini",
    "qwen-acp": "qwen",
    "kimi-acp": "kimi",
}
_NPM_SHIMS = {
    preset: f"{command}.cmd" for preset, command in _KNOWN_VENDOR_COMMANDS.items()
}
_CODEX_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_CODEX_BIN_ENTRIES = 64
_MIN_CODEX_EXE_BYTES = 64 * 1024
_MAX_CODEX_EXE_BYTES = 1024 * 1024 * 1024
_MAX_NPM_SHIM_BYTES = 1024 * 1024
_WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WINDOWS_TRANSIENT_DELETE_ERRORS = frozenset({5, 32, 33, 145})
_TRANSIENT_DELETE_ERRNOS = frozenset(
    {errno.EACCES, errno.EBUSY, errno.ENOTEMPTY, errno.EPERM}
)
# Antivirus scanners and git may briefly retain a pack-file handle after the
# vendor process exits. Keep this bounded: removal is a UI operation and must
# never wait indefinitely for an unrelated process.
_DELETE_RETRY_DELAYS = (0.05, 0.10, 0.20, 0.40, 0.80, 1.00, 1.00)

# Every ReplayLab machine protocol is UTF-8. Windows otherwise chooses the
# active ANSI code page for a Python process whose stdio is redirected to a
# pipe (for example CP936 on a Chinese installation). qrenderdoc, MCP and ACP
# correctly decode those pipes as UTF-8, so allowing the locale default here
# turns localized errors and non-ASCII paths into mojibake. These values are
# fixed by ReplayLab rather than inherited, and therefore cannot be repurposed
# as profile credentials or execution controls.
_UTF8_PROCESS_ENV = {
    "PYTHONIOENCODING": "utf-8:strict",
    "PYTHONUTF8": "1",
}


def external_agent_environment(
    profile: Profile,
    extra: Mapping[str, str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    result = {
        key: value for key, value in source.items() if key.upper() in SAFE_EXTERNAL_ENV
    }
    for name in profile.agent_env:
        value = source.get(name)
        if value is None:
            raise AuthenticationError(
                f"external Agent profile '{profile.id}' requires environment variable {name}"
            )
        result[name] = value
    if extra:
        result.update({str(key): str(value) for key, value in extra.items()})
    result.update(_UTF8_PROCESS_ENV)
    if profile.preset == "codex":
        # A subscription login and Codex configuration are both rooted at
        # CODEX_HOME.  Reusing the user's global home would also import global
        # AGENTS.md, MCP servers, Apps, hooks and memories into ReplayLab.  Keep
        # a durable, profile-scoped home so browser login remains persistent
        # while ReplayLab's Agent contract stays isolated.
        home = codex_profile_home(profile, environ=source)
        result["CODEX_HOME"] = str(home)
    return result


def codex_profile_home(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> Path:
    """Return ReplayLab's durable, profile-scoped Codex state root."""

    source = os.environ if environ is None else environ
    if profile.preset != "codex":
        raise ConfigurationError("a managed Codex home requires the Codex preset")
    # Profile.validate() constrains ids to a single traversal-free component.
    profile.validate()
    root = _codex_profiles_root(source)
    return (root / profile.id).resolve()


def _codex_profiles_root(source: Mapping[str, str]) -> Path:
    local = source.get("LOCALAPPDATA") or source.get("XDG_DATA_HOME")
    if local:
        data_root = Path(local).expanduser()
    else:
        data_root = Path.home() / ".local" / "share"
    if not data_root.is_absolute():
        raise ConfigurationError("ReplayLab Codex home root must be absolute")
    return data_root / "ReplayLab" / "codex"


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0) & _WINDOWS_REPARSE_ATTRIBUTE
    )


def _is_transient_delete_error(exc: OSError) -> bool:
    return (
        getattr(exc, "winerror", None) in _WINDOWS_TRANSIENT_DELETE_ERRORS
        or exc.errno in _TRANSIENT_DELETE_ERRNOS
    )


def _delete_error_detail(exc: OSError) -> str:
    """Return an ASCII-only Windows-safe detail for the qrenderdoc dialog."""

    winerror = getattr(exc, "winerror", None)
    code = f"Windows error {winerror}" if winerror is not None else f"errno {exc.errno}"
    filename = getattr(exc, "filename", None)
    return f"{code} at {filename}" if filename else code


def _retry_transient_delete(operation) -> None:
    """Retry a bounded filesystem mutation for Windows sharing/AV races."""

    for attempt in range(len(_DELETE_RETRY_DELAYS) + 1):
        try:
            operation()
            return
        except OSError as exc:
            if not _is_transient_delete_error(exc) or attempt == len(
                _DELETE_RETRY_DELAYS
            ):
                raise
            time.sleep(_DELETE_RETRY_DELAYS[attempt])


def _rmtree_with_windows_retries(target: Path) -> None:
    """Delete ``target`` without following links and tolerate short locks.

    Python 3.8+ treats Windows junctions as leaves in ``shutil.rmtree`` and on
    other supported platforms directory symlinks are unlinked rather than
    traversed. The callback deliberately refuses to chmod a link/reparse point,
    because doing so could mutate its external target.
    """

    lexical_root = Path(os.path.abspath(target))

    def make_writable_and_retry(function, path, exc_info) -> None:
        error = exc_info[1]
        if not isinstance(error, OSError):
            raise error
        failed = Path(os.path.abspath(path))
        try:
            failed.relative_to(lexical_root)
        except ValueError as exc:
            raise ConfigurationError(
                "managed Codex state deletion escaped its tombstone"
            ) from exc
        if _is_reparse_or_symlink(failed):
            # Never chmod through a link. A transient unlink/rmdir failure is
            # handled by the bounded outer retry instead.
            raise error
        try:
            details = failed.lstat()
            os.chmod(failed, details.st_mode | stat.S_IWRITE)
        except OSError:
            # Preserve the original, more useful deletion failure.
            raise error
        function(path)

    def remove_once() -> None:
        if not target.exists():
            return
        if _is_reparse_or_symlink(target):
            raise ConfigurationError(
                "refusing to remove managed Codex state through a link/reparse point"
            )
        shutil.rmtree(target, onerror=make_writable_and_retry)

    _retry_transient_delete(remove_once)


def remove_codex_profile_home(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> None:
    """Remove only ReplayLab's exact profile-scoped Codex state directory.

    This is intentionally stricter than ordinary cache cleanup because the
    directory can contain a vendor-managed subscription credential.  Refuse
    symlinks, Windows reparse points, path escapes and unexpected file targets
    instead of risking traversal into the user's global Codex state.
    """

    source = os.environ if environ is None else environ
    profile.validate()
    if profile.preset != "codex":
        raise ConfigurationError("managed Codex state can be removed only for a Codex profile")
    root = _codex_profiles_root(source)
    target = root / profile.id
    if target.parent != root or target.name != profile.id:
        raise ConfigurationError("managed Codex state target escaped its profile root")

    data_value = source.get("LOCALAPPDATA") or source.get("XDG_DATA_HOME")
    data_root = Path(data_value).expanduser() if data_value else Path.home() / ".local" / "share"
    resolved_data = data_root.resolve()
    resolved_root = root.resolve()
    try:
        resolved_root.relative_to(resolved_data)
    except ValueError as exc:
        raise ConfigurationError("managed Codex state root escaped the local data directory") from exc

    # Refuse any managed component that redirects traversal, even when it
    # happens to resolve back below the local data root today.
    for component in (data_root / "ReplayLab", root, target):
        if _is_reparse_or_symlink(component):
            raise ConfigurationError(
                f"refusing to remove managed Codex state through a link/reparse point: {component}"
            )
    if not target.exists():
        return
    if not target.is_dir():
        raise ConfigurationError("managed Codex state target is not a directory")
    resolved_target = target.resolve()
    if resolved_target.parent != resolved_root or resolved_target.name != profile.id:
        raise ConfigurationError("managed Codex state target escaped its profile root")
    # Detach the state directory first. This prevents a still-running vendor
    # helper from creating new files at the active CODEX_HOME while cleanup is
    # walking it, and makes each deletion attempt operate on a stable tree.
    # The tombstone is a random sibling below the already-validated root; it can
    # therefore never name the user's global ~/.codex directory.
    tombstone = root / f".delete-{profile.id}-{uuid.uuid4().hex}"
    if tombstone.parent != root or tombstone.exists():
        raise ConfigurationError("managed Codex state tombstone is invalid")
    try:
        _retry_transient_delete(lambda: target.rename(tombstone))
        _rmtree_with_windows_retries(tombstone)
    except (ConfigurationError, OSError) as exc:
        # Keep the profile and its state path consistent when a genuinely
        # long-lived process still owns a handle. rmtree may have removed cache
        # files before failing, but credentials that remain stay attached to the
        # still-visible profile instead of becoming an orphaned secret.
        restore_error: OSError | None = None
        if tombstone.exists() and not target.exists():
            try:
                _retry_transient_delete(lambda: tombstone.rename(target))
            except OSError as restore_exc:
                restore_error = restore_exc
        if restore_error is not None:
            original_detail = (
                str(exc)
                if isinstance(exc, ConfigurationError)
                else _delete_error_detail(exc)
            )
            raise ConfigurationError(
                "cannot remove managed Codex state and could not restore its "
                f"profile directory: {original_detail}; restore failed: "
                f"{_delete_error_detail(restore_error)}"
            ) from exc
        if isinstance(exc, ConfigurationError):
            raise
        raise ConfigurationError(
            "cannot remove managed Codex state after bounded retries; stop the current "
            f"AI task and retry ({_delete_error_detail(exc)})"
        ) from exc


def host_process_environment(
    extra: Mapping[str, str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a credential-free environment for ReplayLabMCP itself.

    In particular, model-provider API keys and unrelated Agent credentials are
    never inherited by the MCP host.  The caller explicitly adds only the
    qrenderdoc instance binding and the isolated live-edit registry path.
    """

    source = os.environ if environ is None else environ
    result = {
        key: value for key, value in source.items() if key.upper() in SAFE_EXTERNAL_ENV
    }
    if extra:
        result.update({str(key): str(value) for key, value in extra.items()})
    result.update(_UTF8_PROCESS_ENV)
    return result


def resolve_executable(
    command: str, *, environ: Mapping[str, str] | None = None
) -> str:
    """Resolve a command without ever consulting the task working directory."""

    source = os.environ if environ is None else environ
    expanded = Path(command).expanduser()
    if expanded.is_absolute():
        return str(expanded.resolve()) if expanded.is_file() else ""
    if any(separator in command for separator in ("/", "\\")):
        # Relative executable paths make an untrusted task cwd executable.
        return ""
    extensions = [""]
    if os.name == "nt" and not expanded.suffix:
        extensions.extend(
            item.lower() for item in source.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
        )
    for directory in source.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        directory_path = Path(directory).expanduser()
        if not directory_path.is_absolute():
            # Relative PATH entries (including '.') would consult the task cwd.
            continue
        for extension in extensions:
            candidate = directory_path / f"{command}{extension}"
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate.resolve())
    return ""


def _is_windows_platform() -> bool:
    return os.name == "nt"


def is_packaged_windows_executable(path: str) -> bool:
    """Identify WindowsApps aliases that are not reliable child-process CLIs."""

    if not _is_windows_platform():
        return False
    normalized = os.path.abspath(path).replace("/", "\\").casefold()
    return "\\program files\\windowsapps\\" in normalized or (
        "\\appdata\\local\\microsoft\\windowsapps\\" in normalized
    )


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_ATTRIBUTE
    )


def _safe_directory(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not _is_reparse(info)


def _safe_base(raw: str) -> Path | None:
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute() or not _safe_directory(path):
        return None
    try:
        return path.resolve(strict=True)
    except OSError:
        return None


def _is_contained_path(parent: Path, candidate: Path) -> bool:
    """Compare resolved paths with Windows case-folding and no prefix tricks."""

    try:
        common = os.path.commonpath((str(parent), str(candidate)))
    except (OSError, ValueError):
        return False
    return os.path.normcase(common) == os.path.normcase(str(parent))


def _safe_literal_directory(parent: Path, name: str) -> Path | None:
    candidate = parent / name
    if not _safe_directory(candidate):
        return None
    try:
        resolved = candidate.resolve(strict=True)
        if not _is_contained_path(parent, resolved):
            return None
        return resolved
    except (OSError, ValueError):
        return None


def _safe_regular_file(
    candidate: Path,
    *,
    minimum_size: int,
    maximum_size: int,
) -> os.stat_result | None:
    try:
        info = os.lstat(candidate)
    except OSError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or _is_reparse(info)
        or not minimum_size <= int(info.st_size) <= maximum_size
    ):
        return None
    return info


def _bundled_codex_executable(source: Mapping[str, str]) -> str:
    """Resolve one bounded official Codex desktop installation layout.

    The only enumeration is the direct child list of the literal
    ``OpenAI/Codex/bin`` directory, capped at 64 entries.  No recursive or
    drive-wide search is performed.
    """

    local = _safe_base(str(source.get("LOCALAPPDATA", "")))
    if local is None:
        return ""
    current = local
    for component in ("OpenAI", "Codex", "bin"):
        child = _safe_literal_directory(current, component)
        if child is None:
            return ""
        current = child
    bin_root = current
    candidates: list[tuple[int, str, str]] = []
    try:
        with os.scandir(bin_root) as entries:
            count = 0
            for entry in entries:
                count += 1
                if count > _MAX_CODEX_BIN_ENTRIES:
                    return ""
                if not _CODEX_VERSION_RE.fullmatch(entry.name):
                    continue
                try:
                    directory_info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISDIR(directory_info.st_mode) or _is_reparse(directory_info):
                    continue
                version_dir = Path(entry.path)
                executable = version_dir / "codex.exe"
                file_info = _safe_regular_file(
                    executable,
                    minimum_size=_MIN_CODEX_EXE_BYTES,
                    maximum_size=_MAX_CODEX_EXE_BYTES,
                )
                if file_info is None:
                    continue
                try:
                    resolved = executable.resolve(strict=True)
                    if not _is_contained_path(bin_root, resolved):
                        continue
                    with resolved.open("rb") as stream:
                        if stream.read(2) != b"MZ":
                            continue
                except (OSError, ValueError):
                    continue
                candidates.append(
                    (int(getattr(file_info, "st_mtime_ns", 0)), entry.name, str(resolved))
                )
    except OSError:
        return ""
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][2]


def _npm_vendor_shim(preset_id: str, source: Mapping[str, str]) -> str:
    shim_name = _NPM_SHIMS.get(preset_id)
    if not shim_name:
        return ""
    appdata = _safe_base(str(source.get("APPDATA", "")))
    if appdata is None:
        return ""
    npm = _safe_literal_directory(appdata, "npm")
    if npm is None:
        return ""
    candidate = npm / shim_name
    if (
        _safe_regular_file(candidate, minimum_size=1, maximum_size=_MAX_NPM_SHIM_BYTES)
        is None
    ):
        return ""
    try:
        resolved = candidate.resolve(strict=True)
        if not _is_contained_path(npm, resolved):
            return ""
        with resolved.open("rb") as stream:
            if b"\0" in stream.read(4096):
                return ""
        return str(resolved)
    except (OSError, ValueError):
        return ""


def resolve_known_vendor_executable(
    preset_id: str,
    command: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve an audited catalog CLI with tightly bounded Windows fallbacks."""

    source = os.environ if environ is None else environ
    resolved = resolve_executable(command, environ=source)
    if resolved and not is_packaged_windows_executable(resolved):
        return resolved
    expected = _KNOWN_VENDOR_COMMANDS.get(preset_id)
    if expected is None or command.casefold() != expected.casefold() or not _is_windows_platform():
        return ""
    if preset_id == "codex":
        resolved = _bundled_codex_executable(source)
        if resolved:
            return resolved
    return _npm_vendor_shim(preset_id, source)


def agent_command_argv(profile: Profile) -> list[str]:
    """Return audited catalog argv for known vendors, explicit argv otherwise."""

    expected = _KNOWN_VENDOR_COMMANDS.get(profile.preset)
    if expected is None:
        return profile.argv
    argv = list(profile.provider.command)
    if not argv or argv[0].casefold() != expected.casefold():
        raise ConfigurationError(f"catalog command mismatch for provider preset: {profile.preset}")
    return argv


def resolve_agent_executable(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> str:
    argv = agent_command_argv(profile)
    if not argv:
        return ""
    if profile.preset in _KNOWN_VENDOR_COMMANDS:
        return resolve_known_vendor_executable(
            profile.preset, argv[0], environ=environ
        )
    return resolve_executable(argv[0], environ=environ)


def resolved_agent_argv(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> list[str]:
    argv = agent_command_argv(profile)
    resolved = resolve_agent_executable(profile, environ=environ)
    if not resolved:
        raise ConfigurationError(
            f"external Agent executable is missing or not in an approved installation location: "
            f"{argv[0] if argv else '<empty>'}"
        )
    return [resolved, *argv[1:]]


__all__ = [
    "SAFE_EXTERNAL_ENV",
    "REPLAYLAB_CONTROL_ENV",
    "RESERVED_CREDENTIAL_ENV",
    "agent_command_argv",
    "codex_profile_home",
    "external_agent_environment",
    "remove_codex_profile_home",
    "host_process_environment",
    "is_packaged_windows_executable",
    "resolve_agent_executable",
    "resolve_executable",
    "resolve_known_vendor_executable",
    "resolved_agent_argv",
]
