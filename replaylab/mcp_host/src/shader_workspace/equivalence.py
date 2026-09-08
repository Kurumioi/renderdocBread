"""Name-agnostic equivalence, Host-applied renames, review rounds and verdicts.

The organization review used to accept a proposal only when the compiled
executable block hashed identically to the baseline.  DXIL keeps resource,
cbuffer-field and entry names in its metadata, so exactly the renames that
make a decompiled shader readable were rejected.  This module adds:

* ``apply_renames`` / ``insert_function_comments`` – the Host edits the HLSL
  text itself from a rename map and a comment map, at the token level, so a
  rename can never touch arithmetic or control flow.
* ``rename_only_equivalent`` – proves that a proposal differs from the
  baseline by identifiers, comments and whitespace only (the *rename-only*
  equivalence level), with a located diff when it does not.
* ``ReviewRounds`` / ``max_rounds`` – bounded review rounds per workspace,
  configurable through ``REPLAYLAB_REVIEW_MAX_ROUNDS`` or
  ``%LOCALAPPDATA%\\ReplayLab\\config\\review.json``; after the last round the
  baseline (always compilable) is published with the analysis and flow graph.
* ``build_verdict`` – one verdict shape for every reviewer: accepted / level /
  reasons with a *direction* the model can act on / rounds left.
* ``mermaid_from_flow_graph`` – a fixed-style Mermaid rendering of the
  function-call graph so diagrams look the same whichever agent produced them.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..review_contract import (REVIEW_VERDICT_SCHEMA, DEFAULT_MAX_ROUNDS,
                               reason, build_verdict as _shared_verdict,
                               max_review_rounds)

ROUNDS_FILE = "organization-rounds.json"

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOKEN = re.compile(
    r"""
    (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<string>"(?:\\.|[^"\\\n])*")
  | (?P<pp>^[ \t]*\#[^\n]*)
  | (?P<number>(?:0[xX][0-9a-fA-F]+[uUlL]*|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fFhHuUlL]*))
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<punct><<=|>>=|<<|>>|<=|>=|==|!=|&&|\|\||\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|->|::|[-+*/%<>=!&|^~?:;,.(){}\[\]])
  | (?P<ws>\s+)
  | (?P<other>.)
    """,
    re.DOTALL | re.VERBOSE | re.MULTILINE,
)

# Identifiers the model may never rename: keywords, storage classes, object
# types, intrinsics/methods, semantics, register syntax and swizzles.
_KEYWORDS = frozenset(
    """
    if else for while do switch case default break continue return discard struct cbuffer tbuffer
    typedef register packoffset static const uniform in out inout precise groupshared row_major
    column_major nointerpolation linear centroid noperspective sample snorm unorm globallycoherent
    extern shared volatile void true false export interface class namespace template this
    unroll loop branch flatten numthreads earlydepthstencil maxvertexcount domain partitioning
    outputtopology patchconstantfunc outputcontrolpoints instance forcecase call allow_uav_condition
    fastopt point line triangle lineadj triangleadj vertices indices payload raypayload
    RWTexture1D RWTexture2D RWTexture3D RWTexture1DArray RWTexture2DArray RWBuffer RWByteAddressBuffer
    RWStructuredBuffer Texture1D Texture2D Texture3D TextureCube Texture1DArray Texture2DArray
    TextureCubeArray Texture2DMS Texture2DMSArray Buffer ByteAddressBuffer StructuredBuffer
    AppendStructuredBuffer ConsumeStructuredBuffer SamplerState SamplerComparisonState sampler
    ConstantBuffer TextureBuffer RaytracingAccelerationStructure InputPatch OutputPatch PointStream
    LineStream TriangleStream FeedbackTexture2D FeedbackTexture2DArray RasterizerOrderedTexture2D
    Sample SampleLevel SampleGrad SampleBias SampleCmp SampleCmpLevelZero SampleCmpLevel Load Load2
    Load3 Load4 Store Store2 Store3 Store4 Gather GatherRed GatherGreen GatherBlue GatherAlpha
    GatherCmp GatherCmpRed GetDimensions CalculateLevelOfDetail CalculateLevelOfDetailUnclamped
    Append Consume IncrementCounter DecrementCounter GetSamplePosition mips Operator
    abs acos all any asdouble asfloat asint asuint atan atan2 ceil clamp clip cos cosh countbits
    cross ddx ddx_coarse ddx_fine ddy ddy_coarse ddy_fine degrees determinant distance dot dst
    exp exp2 f16tof32 f32tof16 faceforward firstbithigh firstbitlow floor fma fmod frac frexp fwidth
    isfinite isinf isnan ldexp length lerp lit log log10 log2 mad max min modf msad4 mul noise
    normalize pow radians rcp reflect refract reversebits round rsqrt saturate sign sin sincos sinh
    smoothstep sqrt step tan tanh tex1D tex2D tex3D texCUBE tex2Dlod tex2Dproj tex2Dbias tex2Dgrad
    transpose trunc InterlockedAdd InterlockedAnd InterlockedCompareExchange InterlockedCompareStore
    InterlockedExchange InterlockedMax InterlockedMin InterlockedOr InterlockedXor
    GroupMemoryBarrier GroupMemoryBarrierWithGroupSync DeviceMemoryBarrier DeviceMemoryBarrierWithGroupSync
    AllMemoryBarrier AllMemoryBarrierWithGroupSync WaveIsFirstLane WaveGetLaneIndex WaveGetLaneCount
    WaveActiveAnyTrue WaveActiveAllTrue WaveActiveBallot WaveReadLaneAt WaveReadLaneFirst
    WaveActiveAllEqual WaveActiveCountBits WaveActiveSum WaveActiveProduct WaveActiveBitAnd
    WaveActiveBitOr WaveActiveBitXor WaveActiveMin WaveActiveMax WavePrefixCountBits WavePrefixSum
    WavePrefixProduct QuadReadAcrossX QuadReadAcrossY QuadReadAcrossDiagonal QuadReadLaneAt
    EvaluateAttributeAtSample EvaluateAttributeCentroid EvaluateAttributeSnapped
    GetRenderTargetSampleCount GetRenderTargetSamplePosition CheckAccessFullyMapped
    NonUniformResourceIndex TraceRay ReportHit IgnoreHit AcceptHitAndEndSearch CallShader
    DispatchMesh SetMeshOutputCounts printf errorf abort
    """.split()
)
_TYPE = re.compile(
    r"^(?:float|int|uint|bool|half|double|min16float|min10float|min16int|min12int|min16uint|"
    r"int16_t|uint16_t|int32_t|uint32_t|int64_t|uint64_t|float16_t|float32_t|float64_t|dword|"
    r"vector|matrix)(?:[1-4](?:x[1-4])?)?$"
)
_SEMANTIC = re.compile(
    r"^(?:SV_\w+|TEXCOORD\d*|POSITION\d*|POSITIONT|NORMAL\d*|COLOR\d*|TANGENT\d*|BINORMAL\d*|"
    r"BLENDINDICES\d*|BLENDWEIGHT\d*|PSIZE\d*|FOG|DEPTH|VFACE|VPOS|TESSFACTOR\d*)$"
)
_REGISTER = re.compile(r"^(?:[tsbuc]\d+|space\d+)$")
_SWIZZLE = re.compile(r"^(?:[xyzw]{1,4}|[rgba]{1,4})$")


class EquivalenceError(ValueError):
    """A rename request that cannot be applied safely."""


# ------------------------------------------------------------------ tokens
def tokenize(source: str) -> list[dict[str, Any]]:
    """Tokens with kind/text/line; comments and whitespace are kept for editing."""

    tokens: list[dict[str, Any]] = []
    line = 1
    for match in _TOKEN.finditer(source):
        kind = match.lastgroup or "other"
        text = match.group(0)
        tokens.append({"kind": kind, "text": text, "line": line, "start": match.start(), "end": match.end()})
        line += text.count("\n")
    return tokens


def code_tokens(source: str) -> list[dict[str, Any]]:
    """Tokens that matter for code identity (no comments, no whitespace)."""

    return [t for t in tokenize(source) if t["kind"] not in {"comment", "ws"}]


def is_reserved(identifier: str, *, as_target: bool = False) -> bool:
    """Keywords, types, intrinsics and semantics are never renamed.

    Register-looking names (``t0``, ``s1``, ``b2``, ``space0``) are legal
    variable names — decompilers use them for resources — so they may be
    renamed *from*; inside ``register(...)``/``packoffset(...)`` they are left
    alone by ``apply_renames``.  They are refused as *new* names.
    """

    return (
        identifier in _KEYWORDS
        or _TYPE.match(identifier) is not None
        or _SEMANTIC.match(identifier) is not None
        or (as_target and _REGISTER.match(identifier) is not None)
    )


def _is_member_access(tokens: Sequence[dict[str, Any]], index: int) -> bool:
    previous = index - 1
    while previous >= 0 and tokens[previous]["kind"] in {"ws", "comment"}:
        previous -= 1
    return previous >= 0 and tokens[previous]["text"] in {".", "->"}


# ----------------------------------------------------------------- renames
def validate_renames(source: str, renames: Mapping[str, Any]) -> dict[str, str]:
    """Return a clean ``old -> new`` map or raise ``EquivalenceError`` with every problem."""

    if not isinstance(renames, Mapping):
        raise EquivalenceError("renames must be an object mapping old identifier to new identifier")
    if len(renames) > 512:
        raise EquivalenceError("at most 512 renames per request")
    present = {t["text"] for t in tokenize(source) if t["kind"] == "ident"}
    problems: list[str] = []
    clean: dict[str, str] = {}
    targets: dict[str, str] = {}
    for old, new in renames.items():
        old_name = str(old or "").strip()
        new_name = str(new or "").strip()
        if _IDENTIFIER.fullmatch(old_name) is None or _IDENTIFIER.fullmatch(new_name) is None:
            problems.append(f"'{old_name}' -> '{new_name}': both sides must be plain identifiers")
            continue
        if old_name == new_name:
            continue
        if old_name not in present:
            problems.append(f"'{old_name}' does not occur in the baseline source")
            continue
        if is_reserved(old_name):
            problems.append(f"'{old_name}' is a keyword/type/intrinsic/semantic and cannot be renamed")
            continue
        if is_reserved(new_name, as_target=True):
            problems.append(f"'{new_name}' is a reserved HLSL word; pick another name for '{old_name}'")
            continue
        if new_name in present and new_name not in renames:
            problems.append(f"'{new_name}' already exists in the source; renaming '{old_name}' to it would collide")
            continue
        if new_name in targets:
            problems.append(f"'{targets[new_name]}' and '{old_name}' both map to '{new_name}'")
            continue
        targets[new_name] = old_name
        clean[old_name] = new_name
    if problems:
        raise EquivalenceError("; ".join(problems[:12]))
    return clean


def apply_renames(source: str, renames: Mapping[str, str]) -> str:
    """Token-level rename: identifiers only, never inside comments/strings/preprocessor lines."""

    if not renames:
        return source
    tokens = tokenize(source)
    protected = _binding_token_indexes(tokens)
    out: list[str] = []
    for index, token in enumerate(tokens):
        text = token["text"]
        if token["kind"] == "ident" and text in renames and index not in protected:
            # a member access ``.name`` is renamed too: struct/cbuffer fields are
            # legitimate targets, methods are excluded by is_reserved()
            out.append(renames[text])
        else:
            out.append(text)
    return "".join(out)


def _binding_token_indexes(tokens: Sequence[dict[str, Any]]) -> set[int]:
    """Indexes of tokens inside ``register(...)`` / ``packoffset(...)``."""

    protected: set[int] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token["kind"] == "ident" and token["text"] in {"register", "packoffset"}:
            look = index + 1
            while look < len(tokens) and tokens[look]["kind"] in {"ws", "comment"}:
                look += 1
            if look < len(tokens) and tokens[look]["text"] == "(":
                depth = 0
                cursor = look
                while cursor < len(tokens):
                    protected.add(cursor)
                    if tokens[cursor]["text"] == "(":
                        depth += 1
                    elif tokens[cursor]["text"] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    cursor += 1
                index = cursor
        index += 1
    return protected


_FUNCTION_HEADER = re.compile(
    r"^(?P<indent>[ \t]*)(?P<head>(?:\[[^\]\n]*\][ \t]*)*[A-Za-z_][\w<>,: ]*?\s+(?P<name>[A-Za-z_]\w*)\s*\()",
    re.MULTILINE,
)


def insert_function_comments(source: str, comments: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Put ``// text`` lines above each named function definition.

    Returns the new source and the names that were not found.  Existing
    Host-inserted comment blocks (marked ``// [ai]``) for the same function are
    replaced so repeated calls stay idempotent.
    """

    if not isinstance(comments, Mapping):
        raise EquivalenceError("comments must be an object mapping function name to text")
    missing: list[str] = []
    text = source
    for name, raw in comments.items():
        function = str(name or "").strip()
        body = " ".join(str(raw or "").split())
        if _IDENTIFIER.fullmatch(function) is None or not body:
            missing.append(function or "?")
            continue
        if len(body) > 600:
            body = body[:600]
        target = None
        for match in _FUNCTION_HEADER.finditer(text):
            if match.group("name") == function:
                # a definition, not a prototype: the header is followed by ')' ... '{'
                tail = text[match.end() :]
                close = tail.find(")")
                if close >= 0 and "{" in tail[close : close + 200].split(";")[0]:
                    target = match
                    break
        if target is None:
            missing.append(function)
            continue
        indent = target.group("indent")
        block_start = target.start()
        # drop a previous [ai] block directly above
        before = text[:block_start]
        marker = f"{indent}// [ai] "
        lines = before.split("\n")
        while len(lines) >= 2 and lines[-2].startswith(marker):
            lines.pop(-2)
        before = "\n".join(lines)
        comment = "".join(f"{indent}// [ai] {line}\n" for line in _wrap(body, 96))
        text = before + comment + text[block_start:]
    return text, missing


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


# ------------------------------------------------------------- equivalence
def rename_only_equivalent(
    baseline: str, proposal: str, renames: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Is *proposal* the baseline plus renames, comments and whitespace?

    Returns ``{"equal": bool, "differences": [...], "level": "rename-only"}``;
    each difference names the baseline/proposal lines and the tokens around
    the first divergence so the model knows exactly what to revert.
    """

    mapping = {str(k): str(v) for k, v in (renames or {}).items()}
    left = code_tokens(apply_renames(baseline, mapping)) if mapping else code_tokens(baseline)
    right = code_tokens(proposal)
    differences: list[dict[str, Any]] = []
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index]["text"] == right[index]["text"]:
        index += 1
    if index == len(left) == len(right):
        return {"equal": True, "level": "rename-only", "differences": []}
    # report up to 5 divergent spots by resynchronising on line boundaries
    cursor_left, cursor_right = index, index
    for _ in range(5):
        if cursor_left >= len(left) and cursor_right >= len(right):
            break
        differences.append(
            {
                "baselineLine": left[cursor_left]["line"] if cursor_left < len(left) else None,
                "proposalLine": right[cursor_right]["line"] if cursor_right < len(right) else None,
                "baseline": _context(left, cursor_left),
                "proposal": _context(right, cursor_right),
            }
        )
        line_left = left[cursor_left]["line"] if cursor_left < len(left) else None
        line_right = right[cursor_right]["line"] if cursor_right < len(right) else None
        while cursor_left < len(left) and left[cursor_left]["line"] == line_left:
            cursor_left += 1
        while cursor_right < len(right) and right[cursor_right]["line"] == line_right:
            cursor_right += 1
        while (
            cursor_left < len(left)
            and cursor_right < len(right)
            and left[cursor_left]["text"] == right[cursor_right]["text"]
        ):
            cursor_left += 1
            cursor_right += 1
    return {"equal": False, "level": "rename-only", "differences": differences}


def _context(tokens: Sequence[dict[str, Any]], index: int, span: int = 8) -> str:
    if index >= len(tokens):
        return "<end of file>"
    line = tokens[index]["line"]
    parts = [t["text"] for t in tokens if t["line"] == line]
    text = " ".join(parts)
    return text if len(text) <= 160 else text[:157] + "..."


# ----------------------------------------------------------------- rounds
def max_rounds(kind: str = "shader-organization") -> int:
    """Use the same bounded review settings as the other Host domains."""
    return max_review_rounds(kind)


# Format/template mistakes (wrong JSON fields, missing stages) are the AI's own
# output errors, not a judgement on the organization.  They get their own,
# larger counter so three schema slips cannot burn the whole content budget.
TEMPLATE_ERROR_MULTIPLIER = 3
MIN_TEMPLATE_ERRORS = 6
# User-granted extensions are bounded so a consenting click cannot become an
# unbounded loop: at most this many grants and this many extra rounds in total.
MAX_BUDGET_GRANTS = 3
MAX_EXTRA_ROUNDS = 9


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ReviewRounds:
    """Per-workspace round counter persisted beside the other AI artifacts.

    Two counters live here.  ``rounds`` is the content-review budget (base
    limit from settings plus any user-granted extension).  ``templateErrors``
    counts rejected template/schema submissions separately.  Both are stored in
    ``ai/organization-rounds.json`` under the review kind.
    """

    def __init__(self, root: Path, kind: str = "shader-organization") -> None:
        self.path = Path(root) / "ai" / ROUNDS_FILE
        self.kind = kind
        self.base_limit = max_rounds(kind)
        self.state = self._load()

    @property
    def limit(self) -> int:
        entry = self.state.get(self.kind) if isinstance(self.state.get(self.kind), dict) else {}
        extra = entry.get("extraRounds", 0)
        extra = int(extra) if isinstance(extra, int) and not isinstance(extra, bool) and extra > 0 else 0
        return self.base_limit + min(extra, MAX_EXTRA_ROUNDS)

    @property
    def template_limit(self) -> int:
        return max(MIN_TEMPLATE_ERRORS, TEMPLATE_ERROR_MULTIPLIER * self.base_limit)

    @property
    def template_errors(self) -> int:
        self.state = self._load()
        entry = self.state.get(self.kind) if isinstance(self.state.get(self.kind), dict) else {}
        value = entry.get("templateErrors", 0)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    @property
    def user_soft_publish_allowed(self) -> bool:
        self.state = self._load()
        entry = self.state.get(self.kind) if isinstance(self.state.get(self.kind), dict) else {}
        return entry.get("userSoftPublishAllowed") is True

    @property
    def grants(self) -> list[dict[str, Any]]:
        self.state = self._load()
        entry = self.state.get(self.kind) if isinstance(self.state.get(self.kind), dict) else {}
        value = entry.get("grants")
        return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def _write_state_locked(self) -> None:
        descriptor, name = tempfile.mkstemp(prefix="organization-rounds-", suffix=".tmp", dir=self.path.parent)
        tmp = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.state, stream, ensure_ascii=False, indent=2)
            self._check_paths()
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def record_template_error(self, reasons: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
        """Count one rejected template submission; returns (errors, templateLimit)."""

        with self._locked():
            self.state = self._load()
            entry = self.state.get(self.kind, {})
            entry.setdefault("rounds", 0)
            errors = entry.get("templateErrors", 0)
            errors = (int(errors) if isinstance(errors, int) and not isinstance(errors, bool) else 0) + 1
            entry.update(templateErrors=errors, lastTemplateReasons=[dict(r) for r in reasons][:12])
            self.state[self.kind] = entry
            self._write_state_locked()
            return errors, self.template_limit

    def template_exhausted(self) -> bool:
        return self.template_errors >= self.template_limit

    def grant(self, *, extra_rounds: int, soft_publish_allowed: bool, note: str) -> dict[str, Any]:
        """Apply one user decision: more rounds and/or permission to publish the baseline as-is."""

        if isinstance(extra_rounds, bool) or not isinstance(extra_rounds, int) or not 0 <= extra_rounds <= 3:
            raise EquivalenceError("extra_rounds must be an integer between 0 and 3")
        with self._locked():
            self.state = self._load()
            entry = self.state.get(self.kind, {})
            entry.setdefault("rounds", 0)
            grants = [dict(item) for item in entry.get("grants", []) if isinstance(item, dict)]
            if len(grants) >= MAX_BUDGET_GRANTS:
                raise EquivalenceError(
                    "this shader already received %d user budget grants; further organization needs a human review"
                    % MAX_BUDGET_GRANTS
                )
            current_extra = entry.get("extraRounds", 0)
            current_extra = int(current_extra) if isinstance(current_extra, int) and not isinstance(current_extra, bool) else 0
            new_extra = min(MAX_EXTRA_ROUNDS, current_extra + extra_rounds)
            grants.append({
                "at": _now_iso(), "extraRounds": new_extra - current_extra,
                "softPublishAllowed": bool(soft_publish_allowed), "note": str(note or "")[:400],
            })
            entry.update(extraRounds=new_extra, grants=grants, templateErrors=0)
            if soft_publish_allowed:
                entry["userSoftPublishAllowed"] = True
            self.state[self.kind] = entry
            self._write_state_locked()
            return {
                "rounds": int(entry.get("rounds", 0) or 0), "maxRounds": self.limit,
                "roundsLeft": max(0, self.limit - int(entry.get("rounds", 0) or 0)),
                "userSoftPublishAllowed": entry.get("userSoftPublishAllowed") is True,
                "grants": grants,
            }

    def _load(self) -> dict[str, Any]:
        self._check_paths()
        try:
            if self.path.stat().st_size > 262144:
                raise EquivalenceError("organization review state is oversized; refusing to reset its budget")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, ValueError) as error:
            raise EquivalenceError("organization review state is unreadable or invalid; refusing to reset its budget") from error
        if not isinstance(payload, dict):
            raise EquivalenceError("organization review state must be an object; refusing to reset its budget")
        for entry in payload.values():
            if (not isinstance(entry, dict) or type(entry.get("rounds")) is not int
                    or entry["rounds"] < 0):
                raise EquivalenceError("organization review rounds must be a nonnegative integer; refusing to reset its budget")
        return payload

    def _check_paths(self) -> None:
        for path in (self.path.parent.parent, self.path.parent, self.path, self.path.with_suffix(".lock")):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1)):
                raise EquivalenceError("organization review state and lock must not be redirected")

    @contextmanager
    def _locked(self):
        self._check_paths()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Keep a stable lock inode: removing it after use could let another
        # process lock a different file while the first holder still runs.
        with self.path.with_suffix(".lock").open("a+b") as stream:
            self._check_paths()
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise EquivalenceError("organization review state is busy; no budget was reset") from error
                    time.sleep(0.02)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @property
    def used(self) -> int:
        self.state = self._load()
        value = self.state.get(self.kind, {}).get("rounds") if isinstance(self.state.get(self.kind), dict) else 0
        return int(value or 0)

    def record(self, *, accepted: bool, reasons: Sequence[Mapping[str, Any]]) -> int:
        with self._locked():
            self.state = self._load()
            entry = self.state.get(self.kind, {})
            used = entry.get("rounds", 0)
            if used >= self.limit:
                raise EquivalenceError("organization review budget is exhausted; stop automatic retries")
            rounds = used + 1
            entry.update(rounds=0 if accepted else rounds, lastAccepted=accepted,
                         lastReasons=[dict(r) for r in reasons][:12])
            if accepted:
                # A fresh success closes the chapter: extensions and consent
                # were for this organization only.
                entry.pop("extraRounds", None)
                entry.pop("userSoftPublishAllowed", None)
                entry["templateErrors"] = 0
            self.state[self.kind] = entry
            self._write_state_locked()
            return rounds

    def exhausted(self, rounds_used: int) -> bool:
        return rounds_used >= self.limit


# ---------------------------------------------------------------- verdict
def build_verdict(
    *,
    accepted: bool,
    level: str,
    round_used: int,
    round_limit: int,
    reasons: Iterable[Mapping[str, Any]] = (),
    soft_accepted: bool = False,
    next_step: str = "",
    workspace_path: str = "",
) -> dict[str, Any]:
    reasons_list = [dict(r) for r in reasons]
    rounds_left = max(0, round_limit - round_used) if not accepted else round_limit
    if not next_step:
        if accepted:
            next_step = "register_active_project_workspace to publish; complete authorized editable delivery with checked apply and start_shader_workspace_live_edit"
        elif soft_accepted:
            next_step = (
                "the review budget is used up: the validated baseline HLSL, safe Host summary and fixed "
                "flow graph are published; proposal code and analysis stay in the organization session for a human"
            )
        else:
            next_step = (
                f"fix exactly the locations listed under reasons and call review again ({rounds_left} round(s) left); "
                "use the returned templates and preserve the validated baseline"
            )
    exhausted = not accepted and not soft_accepted and round_used >= round_limit
    if exhausted:
        next_step = (
            "The automatic review budget is exhausted. Do not retry. Ask the user the question in "
            "verdict.userDecision with its options; when they answer, call "
            "decide_shader_organization_review_budget with their choice, then continue."
        )
    verdict = _shared_verdict(accepted=accepted, level=level, round_used=round_used,
                              round_limit=round_limit, reasons=reasons_list,
                              soft_accepted=soft_accepted, next_step=next_step)
    if exhausted:
        verdict["userDecision"] = review_budget_user_decision(workspace_path, reasons_list)
    return verdict


def review_budget_user_decision(workspace_path: str, reasons: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """The one question the AI must put to the user when the budget is spent."""

    blocking = [str(r.get("message", "")) for r in reasons if r.get("severity", "blocker") == "blocker"][:3]
    return {
        "required": True,
        "kind": "shader-organization-review-budget",
        "question": (
            "这个着色器的自动整理评审预算已用完，可读整理还没有通过"
            + ("（未通过项：" + "；".join(blocking) + "）" if blocking else "")
            + "。要怎么处理？"
        ),
        "options": [
            {"id": "extend", "label": "再给 3 轮自动整理", "description": "AI 继续按评审意见修改并重新评审，最多再 3 轮。"},
            {"id": "publish-baseline", "label": "按已验证的基线原样入库",
             "description": "不再整理；把编译并通过回放 ABI 校验的 HLSL 原样注册到工程库，分析只保留 Host 的安全摘要。"},
            {"id": "stop", "label": "先不处理", "description": "保留当前工作区，什么都不改。"},
        ],
        "tool": "decide_shader_organization_review_budget",
        "toolArguments": {
            "workspace_path": str(workspace_path or "<this workspace>"),
            "decision": "<extend | publish-baseline | stop>",
            "user_confirmation": "<the user's own words>",
        },
        "policy": "Only the user may answer; never pick an option on their behalf.",
    }


def differences_to_reasons(differences: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    reasons_list: list[dict[str, str]] = []
    for item in differences:
        b_line = item.get("baselineLine")
        p_line = item.get("proposalLine")
        reasons_list.append(
            reason(
                "code-changed",
                f"proposal line {p_line} differs from baseline line {b_line}: "
                f"baseline `{item.get('baseline')}` vs proposal `{item.get('proposal')}`",
                location=f"proposal.hlsl:{p_line}",
                severity="warning",
                direction=(
                    "keep the baseline statement byte-for-byte (only identifiers, comments and whitespace "
                    "may change); if this is a rename, declare it through rename_shader_workspace_symbols"
                ),
            )
        )
    return reasons_list


# ---------------------------------------------------------------- mermaid
_MERMAID_STYLE = (
    "  classDef entry fill:#2b6cb0,stroke:#1a365d,color:#ffffff,font-weight:bold;\n"
    "  classDef fn fill:#edf2f7,stroke:#4a5568,color:#1a202c;\n"
)


def mermaid_from_flow_graph(graph: Mapping[str, Any]) -> str:
    """Deterministic ``flowchart TD`` from the Host's function-call graph."""

    nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
    edges = graph.get("edges") if isinstance(graph, Mapping) else None
    if not isinstance(nodes, list):
        nodes = []
    if not isinstance(edges, list):
        edges = []
    lines = ["flowchart TD"]
    ids: dict[str, str] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            continue
        node_id = f"n{index}"
        ids[str(node.get("id", ""))] = node_id
        name = str(node.get("label") or node.get("name") or f"fn{index}")
        description = " ".join(str(node.get("description") or "").split())
        if len(description) > 90:
            description = description[:87] + "..."
        label = _mermaid_escape(name if not description or description == name else f"{name} — {description}")
        lines.append(f'  {node_id}["{label}"]')
        lines.append(f"  class {node_id} {'entry' if node.get('kind') == 'entry-point' else 'fn'};")
    for edge in edges:
        if not isinstance(edge, Mapping):
            continue
        source = ids.get(str(edge.get("source", "")))
        target = ids.get(str(edge.get("target", "")))
        if source and target:
            values = ", ".join(edge.get("values", []))
            lines.append(f'  {source} -->|"{_mermaid_escape(values)}"| {target}' if values else f"  {source} --> {target}")
    if len(lines) == 1:
        entry = str(graph.get("entryPoint") or "main") if isinstance(graph, Mapping) else "main"
        lines.append(f'  n0["{_mermaid_escape(entry)}"]')
        lines.append("  class n0 entry;")
    return "\n".join(lines) + "\n" + _MERMAID_STYLE


def _mermaid_escape(text: str) -> str:
    return text.replace("\\", "/").replace('"', "'").replace("<", "‹").replace(">", "›").replace("\n", " ")


# -------------------------------------------------------------- flow spec
FLOW_SPEC_SCHEMA = "replaylab-shader-flow-spec/v1"
_FLOW_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_FLOW_TEXT = re.compile(r'^[^"<>`\n\r\t]{1,72}$')
_DECL_RESOURCE = re.compile(
    r"^\s*(?P<type>(?:RW)?Texture\w*|(?:RW)?(?:Structured|ByteAddress|Append|Consume)?Buffer\w*|"
    r"SamplerState|SamplerComparisonState|ConstantBuffer|TextureBuffer)\s*(?:<[^>\n]*>)?\s+(?P<name>[A-Za-z_]\w*)"
    r"(?:\s*\[[^\]]*\])?\s*:\s*register",
    re.MULTILINE,
)
_DECL_CBUFFER = re.compile(r"^\s*cbuffer\s+(?P<name>[A-Za-z_]\w*)", re.MULTILINE)
_OUTPUT_SEMANTIC = re.compile(r":\s*(SV_Target\d*|SV_Depth\w*|SV_Position|SV_Coverage|SV_StencilRef)\b")
MAX_FLOW_STAGES = 40


def declared_symbols(source: str) -> dict[str, list[str]]:
    """Resources, cbuffers and output semantics declared in the HLSL (for templates and validation)."""

    source = _source_without_comments(source)
    resources = []
    for match in _DECL_RESOURCE.finditer(source):
        resources.append(match.group("name"))
    cbuffers = [m.group("name") for m in _DECL_CBUFFER.finditer(source)]
    outputs: list[str] = []
    for match in _OUTPUT_SEMANTIC.finditer(source):
        if match.group(1) not in outputs:
            outputs.append(match.group(1))
    return {"resources": resources, "cbuffers": cbuffers, "outputs": outputs}


def _source_without_comments(source: str) -> str:
    return re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"', lambda m: " " * len(m.group()), source, flags=re.DOTALL)


def flow_value_bindings(source: str) -> dict[str, list[str]]:
    """Bound explanatory input/value names to declarations, never model lists."""
    source = _source_without_comments(source)
    declarations = re.compile(r"\b[A-Za-z_]\w*(?:\s*<[^;{}\n]+>)?\s+([A-Za-z_]\w*)\s*(?:\[[^\]\n]*\]\s*)?(?=[:;,=)])")
    values = set(declarations.findall(source))
    fields = set()
    for block in re.finditer(r"\bcbuffer\s+(\w+)[^{]*\{([^{}]*)\}", source, re.DOTALL):
        fields.update(block.group(1) + "." + name for name in declarations.findall(block.group(2)))
    # Explicit struct-member accesses are permitted only when they actually
    # occur in source. A model cannot invent CB.field by naming only CB.
    fields.update(re.findall(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b", source))
    fields.update(".".join(field.split(".")[:2]) for field in list(fields) if field.count(".") > 1)
    return {"inputs": sorted(values), "fields": sorted(fields)}


def flow_spec_template(source: str, functions: Sequence[str], entry_point: str) -> dict[str, Any]:
    """A skeleton the agent fills in: fixed shape, its own stage labels/edges."""

    symbols = declared_symbols(source)
    bindings = flow_value_bindings(source)
    from .flow_graph import computation_ranges, external_value_hints
    ranges = computation_ranges(source)
    by_function = {item["function"]: item for item in ranges}
    stages: list[dict[str, Any]] = []
    # The template is one editable example, not an automatically prescribed
    # node per function/resource. Large helper libraries would swamp the UI.
    ordered = [entry_point] if entry_point in functions or not functions else list(functions[:1])
    for index, function in enumerate(ordered[:MAX_FLOW_STAGES]):
        stages.append(
            {
                "id": f"stage_{index + 1}",
                "label": f"<what {function} computes, <= 72 chars>",
                "function": function,
                "sourceRange": by_function.get(function, {}).get("sourceRange", {"startLine": 1, "endLine": 1}),
                "inferred": True,
                "reads": [],
                "writes": [],
                "next": [f"stage_{index + 2}"] if index + 1 < len(ordered[:MAX_FLOW_STAGES]) else [],
            }
        )
    if not stages:
        stages.append({"id": "stage_1", "label": "<main computation>", "function": entry_point, "reads": [], "writes": [], "next": []})
    return {
        "schema": FLOW_SPEC_SCHEMA,
        "inputs": [],
        "resources": (symbols["resources"] + symbols["cbuffers"])[:32],
        "outputs": symbols["outputs"],
        "stages": stages,
        "rules": (
            "ids: lowercase [a-z0-9_]; labels <= 72 chars, no quotes/angle brackets; every 'next' must be a "
            "stage id later in this list; 'function' and sourceRange identify actual executable calculations, not declarations. "
            "Use a few cohesive stages, not each temporary or resource. reads/writes may name source-local intermediate values; "
            "Function.return denotes a real helper return/call. A shared value must actually occur in each stage. "
            "Use inout/out struct fields naturally; Host connects them only through a unique actual caller argument chain. "
            "At most 40 stages. Input/field lists below are bounded hints, not an exhaustive allowlist: actual local fields "
            "in a chosen source range remain valid and all claims are checked against the full proposal. "
            "Labels describe inferred meaning, not physical evidence or semantic proof."
        ),
        "allowedInputs": external_value_hints(source, entry_point)[:32],
        "allowedFields": bindings["fields"][:32],
        "computationRanges": ranges,
    }


def validate_flow_spec(
    spec: Any, *, functions: Iterable[str], resources: Iterable[str], outputs: Iterable[str],
    inputs: Iterable[str] = (), fields: Iterable[str] = (), source: str | None = None
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Return (clean spec, reasons). Reasons carry location + direction for the agent."""

    reasons: list[dict[str, str]] = []
    if not isinstance(spec, Mapping):
        return {}, [reason("flow-spec-shape", "flow spec must be a JSON object", location="flow_spec", direction="start from the template in the begin result")]
    allowed_keys = {"schema", "inputs", "resources", "outputs", "stages", "rules", "allowedInputs", "allowedFields", "computationRanges"}
    if spec.get("schema") != FLOW_SPEC_SCHEMA or set(spec) - allowed_keys:
        reasons.append(reason("flow-spec-schema", "Use the exact flowSpecTemplate schema and fields.", location="flow_spec", direction="Remove unknown fields and copy the schema from flowSpecTemplate.", target="flowSpecTemplate.schema", example=FLOW_SPEC_SCHEMA))
    known_functions = set(functions)
    known_resources = set(resources)
    known_outputs = set(outputs)
    known_inputs = set(inputs)
    known_fields = set(fields)
    source_records, parameter_records, effect_memo = None, None, {}
    if source is not None:
        from .flow_graph import _function_records, _unique_records
        source_records = _function_records(source)
        parameter_records = _unique_records(source_records)
    clean: dict[str, Any] = {"schema": FLOW_SPEC_SCHEMA, "inputs": [], "resources": [], "outputs": [], "stages": []}

    def texts(key: str, limit: int) -> list[str]:
        raw = spec.get(key, [])
        if not isinstance(raw, list):
            reasons.append(reason("flow-spec-shape", f"'{key}' must be a list of strings", location=f"flow_spec.{key}", direction="use a JSON array"))
            return []
        if len(raw) > limit:
            reasons.append(reason("flow-spec-size", f"'{key}' exceeds {limit} entries", location="flow_spec." + key, direction="Remove or group minor entries; nothing is silently truncated.", target=str(limit)))
        out: list[str] = []
        for item in raw[:limit]:
            if not isinstance(item, str):
                reasons.append(reason("flow-spec-type", "Names must be strings.", location="flow_spec." + key, direction="Use a declared name as a JSON string.", example='"MainPS"'))
                continue
            text = item.strip()
            if not text:
                reasons.append(reason("flow-spec-text", "Names cannot be empty.", location="flow_spec." + key, direction="Remove the empty entry or use an actual source name.", example='"MainPS"'))
                continue
            if text and _FLOW_TEXT.match(text) and text not in out:
                out.append(text)
            elif text:
                reasons.append(reason("flow-spec-text", f"'{text[:40]}' in '{key}' has forbidden characters or is too long", location=f"flow_spec.{key}", direction="plain text, <= 72 chars, no quotes or angle brackets"))
        return out

    clean["inputs"] = [t for t in texts("inputs", 32) if not t.startswith("<")]
    clean["resources"] = [t for t in texts("resources", 64) if not t.startswith("<")]
    clean["outputs"] = [t for t in texts("outputs", 16) if not t.startswith("<")]
    for key, known in (("inputs", known_inputs), ("resources", known_resources), ("outputs", known_outputs)):
        for item in clean[key]:
            if item not in known:
                reasons.append(reason("flow-spec-" + key, f"'{item}' is not a source-declared {key} name.", location="flow_spec." + key,
                                      direction="Use only names supplied by the source-bound template; do not authorize a name by listing it yourself.",
                                      target=", ".join(sorted(known)[:16]), example=next(iter(sorted(known)), "[]")))
    raw_stages = spec.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        reasons.append(reason("flow-spec-shape", "'stages' must be a non-empty list", location="flow_spec.stages", direction="one stage per meaningful computation step, in execution order"))
        return clean, reasons
    if len(raw_stages) > MAX_FLOW_STAGES:
        reasons.append(reason("flow-spec-size", f"{len(raw_stages)} stages exceed the limit of {MAX_FLOW_STAGES}", location="flow_spec.stages", direction="merge minor steps"))
        raw_stages = raw_stages[:MAX_FLOW_STAGES]
    ids: list[str] = []
    for index, raw in enumerate(raw_stages):
        where = f"flow_spec.stages[{index}]"
        if not isinstance(raw, Mapping):
            reasons.append(reason("flow-spec-shape", "stage must be an object", location=where, direction="{id,label,function,reads,writes,next}"))
            continue
        if set(raw) - {"id", "label", "function", "sourceRange", "inferred", "reads", "writes", "next"} or raw.get("inferred", True) is not True:
            reasons.append(reason("flow-spec-stage-schema", "Stage fields are fixed and explanatory labels must be inferred.", location=where, direction="Use id,label,function,inferred,reads,writes,next; inferred must be true.", example='"inferred": true'))
        if any(not isinstance(raw.get(key), str) for key in ("id", "label", "function")):
            reasons.append(reason("flow-spec-stage-type", "Stage id, label and function must be strings.", location=where, direction="Use the string fields from flowSpecTemplate.", example='"function": "MainPS"'))
        stage_id = str(raw.get("id") or "").strip()
        label = " ".join(str(raw.get("label") or "").split())
        function = str(raw.get("function") or "").strip()
        if not _FLOW_ID.match(stage_id) or stage_id in ids:
            reasons.append(reason("flow-spec-id", f"stage id '{stage_id[:40]}' is missing, invalid or duplicated", location=where, direction="lowercase letters, digits, underscores; unique"))
            continue
        if not label or label.startswith("<") or not _FLOW_TEXT.match(label):
            reasons.append(reason("flow-spec-text", f"stage '{stage_id}' needs a plain label (<= 72 chars, no quotes/angle brackets)", location=where + ".label", direction="describe what this step computes"))
            continue
        if function not in known_functions:
            reasons.append(reason("flow-spec-function", f"stage '{stage_id}' names unknown function '{function[:40]}'", location=where + ".function", direction="use one of: " + ", ".join(sorted(known_functions)[:12])))
            continue
        for key, limit in (("reads", 16), ("writes", 8), ("next", 8)):
            raw_list = raw.get(key, [])
            if not isinstance(raw_list, list) or len(raw_list) > limit or any(not isinstance(x, str) or not x.strip() for x in raw_list):
                reasons.append(reason("flow-spec-stage-list", f"{key} must be an array of at most {limit} non-empty names.", location=where + "." + key, direction="Use a bounded JSON array of source names or stage IDs.", example='["MainPS"]'))
        reads = [str(x).strip() for x in (raw.get("reads") or []) if str(x).strip()][:16] if isinstance(raw.get("reads"), list) else []
        writes = [str(x).strip() for x in (raw.get("writes") or []) if str(x).strip()][:8] if isinstance(raw.get("writes"), list) else []
        for name in reads + writes:
            if not _FLOW_TEXT.match(name):
                reasons.append(reason("flow-spec-text", f"'{name[:40]}' in stage '{stage_id}' has forbidden characters", location=where, direction="plain identifiers"))
        for name in writes:
            if name not in known_outputs and not (source is not None and (name in known_inputs | known_fields or name == function + ".return")):
                reasons.append(reason("flow-spec-output", f"stage '{stage_id}' writes unknown output '{name}'", location=where + ".writes", direction="use a declared output: " + ", ".join(sorted(known_outputs))))
        ids.append(stage_id)
        stage = {"id": stage_id, "label": label, "function": function, "inferred": True, "reads": reads, "writes": writes, "next": [str(x).strip() for x in (raw.get("next") or []) if str(x).strip()][:8] if isinstance(raw.get("next"), list) else []}
        if source is not None:
            from .flow_graph import bind_computation_range, ShaderFlowGraphError
            try:
                bound = bind_computation_range(source, function, raw.get("sourceRange"), records=source_records)
                stage["sourceRange"] = bound["sourceRange"]
                reasons.extend(_stage_value_issues(stage, bound["tokens"], known_outputs, known_functions, where, source,
                                                  parameter_records, effect_memo))
            except ShaderFlowGraphError as error:
                reasons.append(reason("flow-spec-source-range", str(error), location=where + ".sourceRange",
                                      direction="Use a complete calculation span inside this function body; read the current proposal after edits.",
                                      target="Actual executable statements, excluding parameters, resources and uninitialized declarations.",
                                      example='"sourceRange": {"startLine": 20, "endLine": 45}'))
        clean["stages"].append(stage)
    for stage in clean["stages"]:
        for target in stage["next"]:
            if target not in ids:
                reasons.append(reason("flow-spec-edge", f"stage '{stage['id']}' points to unknown stage '{target[:40]}'", location=f"flow_spec.stages[{stage['id']}].next", direction="next must list existing stage ids"))
            elif source is not None and ids.index(target) <= ids.index(stage["id"]):
                reasons.append(reason("flow-spec-order", "Computation graph edges must follow top-down stage order.", location=f"flow_spec.stages[{stage['id']}].next",
                                      direction="Order data preparation before its consumers. Represent loop internals within one computation stage.",
                                      target="An acyclic overview of the algorithm's data flow.", example='"next": ["output"]'))
    known_reads = known_inputs | known_resources
    for stage in clean["stages"]:
        for name in stage["reads"]:
            base = name.split(".", 1)[0]
            if name not in known_reads and name not in known_fields and not (source is not None and name.endswith(".return") and name[:-7] in known_functions):
                reasons.append(reason("flow-spec-read", f"stage '{stage['id']}' reads '{name}' which is not declared in source", location=f"flow_spec.stages[{stage['id']}].reads", direction="Use a source-declared input/resource/field; self-declared names grant no evidence.", target=", ".join(sorted(known_reads)[:12]), example=next(iter(sorted(known_reads)), "[]")))
    return clean, reasons


def _stage_value_issues(stage, tokens, outputs, functions, where, source, records, effect_memo):
    """Check actual lexical occurrences and writes, without inferring math meaning."""
    issues = []
    text = " ".join(tokens)
    from .flow_graph import called_parameter_effects
    called_reads, called_writes = called_parameter_effects(source, tokens, records=records, memo=effect_memo)
    for kind in ("reads", "writes"):
        for name in stage[kind]:
            if name.endswith(".return"):
                function = name[:-7]
                returned = tokens if kind == "writes" else (records.get(function) or {}).get("bodyTokens", [])
                has_value = any(word == "return" and index + 1 < len(returned) and returned[index + 1] != ";" for index, word in enumerate(returned))
                present = has_value and ((function == stage["function"]) if kind == "writes" else function in functions and re.search(r"\b" + re.escape(function) + r"\s*\(", text))
            elif name in outputs:
                present = kind == "writes" and ("return" in tokens or "=" in tokens)
            else:
                pattern = r"(?<![\w])" + r"\s*\.\s*".join(re.escape(part) for part in name.split(".")) + r"(?![\w])"
                if kind == "writes":
                    pattern += r"(?:\s*\[[^;=]*?\]|\s*\.\s*\w+)*\s*(?:[+*/%&|^-]\s*)?=(?!=)"
                present = re.search(pattern, text) or name in (called_reads if kind == "reads" else called_writes)
            if not present:
                issues.append(reason("flow-spec-value-binding", "The claimed %s value '%s' is not supported by this calculation range." % (kind, name),
                                     location=where + "." + kind,
                                     direction="Use exact values computed/read in this span; Function.return must correspond to a real return or call.",
                                     target="Source-bound algorithm edges; listing a name is not evidence.", example='"writes": ["sampledColor"]'))
    return issues


def mermaid_from_flow_spec(spec: Mapping[str, Any]) -> str:
    """Fixed-layout data-flow diagram: Inputs | Resources -> Stages -> Outputs."""

    lines = ["flowchart TD"]
    ids: dict[str, str] = {}
    def node(kind: str, name: str) -> str:
        key = f"{kind}:{name}"
        if key not in ids:
            ids[key] = f"{kind}{len(ids)}"
        return ids[key]
    inputs = list(spec.get("inputs") or [])
    resources = list(spec.get("resources") or [])
    outputs = list(spec.get("outputs") or [])
    stages = list(spec.get("stages") or [])
    if inputs:
        lines.append("  subgraph IN[Inputs]")
        for name in inputs:
            lines.append(f'    {node("in", name)}(["{_mermaid_escape(name)}"])')
        lines.append("  end")
    if resources:
        lines.append("  subgraph RES[Resources]")
        for name in resources:
            lines.append(f'    {node("res", name)}[("{_mermaid_escape(name)}")]')
        lines.append("  end")
    lines.append("  subgraph FLOW[Computation]")
    for stage in stages:
        label = stage["label"] + (f" · {stage['function']}()" if stage.get("function") else "")
        lines.append(f'    {node("st", stage["id"])}["{_mermaid_escape(label)}"]')
    lines.append("  end")
    if outputs:
        lines.append("  subgraph OUT[Outputs]")
        for name in outputs:
            lines.append(f'    {node("out", name)}[["{_mermaid_escape(name)}"]]')
        lines.append("  end")
    for stage in stages:
        target = node("st", stage["id"])
        for name in stage.get("reads", []):
            base = name.split(".", 1)[0]
            if name in inputs:
                lines.append(f'  {node("in", name)} --> {target}')
            elif name in resources or base in resources:
                lines.append(f'  {node("res", name if name in resources else base)} -. "{_mermaid_escape(name)}" .-> {target}')
        for name in stage.get("writes", []):
            if name in outputs:
                lines.append(f'  {target} ==> {node("out", name)}')
        for nxt in stage.get("next", []):
            lines.append(f'  {target} --> {node("st", nxt)}')
    lines.append("  classDef stage fill:#edf2f7,stroke:#4a5568,color:#1a202c;")
    lines.append("  classDef io fill:#ebf8ff,stroke:#2b6cb0,color:#1a365d;")
    lines.append("  classDef res fill:#fffaf0,stroke:#c05621,color:#7b341e;")
    for key, ident in ids.items():
        kind = key.split(":", 1)[0]
        lines.append(f"  class {ident} {'stage' if kind == 'st' else ('res' if kind == 'res' else 'io')};")
    return "\n".join(lines) + "\n"


ANALYSIS_TEMPLATE = (
    "# Shader analysis\n\n"
    "## Overview\n(one paragraph: what this shader is for, which pass/draw uses it)\n\n"
    "## Inputs and resources\n(stage inputs, cbuffers with field meanings, textures/samplers/buffers by register)\n\n"
    "## Computation\n(numbered steps in execution order; name the functions from the flow graph)\n\n"
    "## Outputs\n(render targets / UAVs written, semantics, blend/depth assumptions)\n\n"
    "## Observed facts\n(bullet list; each fact names the tool result it came from)\n\n"
    "## Inferences\n(bullet list; each with a confidence: high / medium / low)\n"
)


__all__ = [
    "FLOW_SPEC_SCHEMA",
    "MAX_FLOW_STAGES",
    "declared_symbols",
    "flow_spec_template",
    "mermaid_from_flow_spec",
    "validate_flow_spec",
    "ANALYSIS_TEMPLATE",
    "DEFAULT_MAX_ROUNDS",
    "EquivalenceError",
    "REVIEW_VERDICT_SCHEMA",
    "ReviewRounds",
    "apply_renames",
    "build_verdict",
    "code_tokens",
    "differences_to_reasons",
    "insert_function_comments",
    "is_reserved",
    "max_rounds",
    "mermaid_from_flow_graph",
    "reason",
    "rename_only_equivalent",
    "tokenize",
    "validate_renames",
]
