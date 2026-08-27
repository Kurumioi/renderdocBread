"""Format-aware routing for hash-pinned external shader tools.

The route catalog never supplies a command, executable, environment variable,
or argument.  It can only select an operation that has already passed the
strict :mod:`shader_workspace.tools` registry validation.  This separation is
important when an AI agent chooses a route: the agent chooses an ID and formats,
not a process invocation.

High-level output reconstructed from bytecode is deliberately labelled as such.
Neither a successful route nor a round-trip smoke test proves semantic or replay
ABI equivalence; the RenderDoc replay-reflection ABI gate remains authoritative.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..symbols.dxbc import fingerprint_bytes
from .catalog import normalize_stage
from .errors import (
    ShaderWorkspaceError,
    ToolConfigurationError,
    ToolExecutionError,
    ToolchainRoutingError,
)
from .io import atomic_write_bytes, atomic_write_json, read_json_object
from .tools import ProcessResult, ToolOperation, ToolRegistry, run_operation

TOOLCHAIN_SCHEMA = "renderdoc-ultra-shader-toolchains/v1"
TOOLCHAIN_VERSION = 1
SMOKE_SCHEMA = "renderdoc-ultra-shader-round-trip-smoke/v1"
RECOVERY_PLAN_SCHEMA = "renderdoc-ultra-shader-recovery-plan/v1"
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_ROUTES = 1024
MAX_CAVEATS = 16
MAX_CAVEAT_CHARS = 2048
MAX_SHADER_BYTES = 512 * 1024 * 1024
MAX_ROUTE_HOPS = 4
ROUTE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

FORMATS = frozenset(
    {
        "dxbc",
        "dxbc-asm",
        "dxil",
        "dxil-asm",
        "spirv",
        "spirv-asm",
        "opengl-spirv",
        "opengl-spirv-asm",
        "hlsl",
        "glsl",
        "essl",
        "msl",
        "wgsl",
        "slang",
    }
)
FORMAT_ALIASES = {
    "spir-v": "spirv",
    "spv": "spirv",
    "spir-v-asm": "spirv-asm",
    "spvasm": "spirv-asm",
    "vulkanspirv": "spirv",
    "vulkan-spirv": "spirv",
    "vulkanspirvasm": "spirv-asm",
    "vulkan-spirv-asm": "spirv-asm",
    "openglspirv": "opengl-spirv",
    "opengl-spv": "opengl-spirv",
    "openglspirvasm": "opengl-spirv-asm",
    "opengl-spv-asm": "opengl-spirv-asm",
    "dxbc-disassembly": "dxbc-asm",
    "dxil-disassembly": "dxil-asm",
}
TEXT_FORMATS = frozenset(
    {
        "dxbc-asm",
        "dxil-asm",
        "spirv-asm",
        "opengl-spirv-asm",
        "hlsl",
        "glsl",
        "essl",
        "msl",
        "wgsl",
        "slang",
    }
)
BINARY_FORMATS = FORMATS.difference(TEXT_FORMATS)
_NATIVE_EDITABLE_FALLBACKS = {
    "dxbc": ("dxbc-asm",),
    "dxil": ("dxil-asm",),
}
STAGES = frozenset(
    {
        "*",
        "vertex",
        "hull",
        "domain",
        "geometry",
        "fragment",
        "compute",
        "task",
        "mesh",
        "raygen",
        "intersection",
        "anyhit",
        "closesthit",
        "miss",
        "callable",
        "library",
    }
)
FIDELITIES = frozenset(
    {
        "native-compile",
        "ir-disassembly",
        "ir-assembly",
        "reconstructed-source",
        "custom-transform",
    }
)
OPERATIONS = frozenset({"decompile", "compile", "transform"})
_FORMAT_SUFFIX = {
    "dxbc": ".dxbc",
    "dxbc-asm": ".dxbc.txt",
    "dxil": ".dxil",
    "dxil-asm": ".ll",
    "spirv": ".spv",
    "spirv-asm": ".spvasm",
    "opengl-spirv": ".opengl.spv",
    "opengl-spirv-asm": ".opengl.spvasm",
    "hlsl": ".hlsl",
    "glsl": ".glsl",
    "essl": ".essl",
    "msl": ".metal",
    "wgsl": ".wgsl",
    "slang": ".slang",
}


def normalize_format(value: Any) -> str:
    result = str(value or "").strip().lower().replace("_", "-")
    result = FORMAT_ALIASES.get(result, result)
    if result not in FORMATS:
        raise ToolchainRoutingError("unsupported shader format: %s" % (value,))
    return result


def _normalize_target_api(value: Any) -> str:
    api = str(value or "").strip().casefold().replace(" ", "")
    if api.startswith("graphicsapi."):
        api = api[len("graphicsapi.") :]
    api = {
        "direct3d11": "d3d11",
        "direct3d12": "d3d12",
        "vk": "vulkan",
        "gl": "opengl",
    }.get(api, api)
    if api and api not in {"d3d11", "d3d12", "vulkan", "opengl"}:
        raise ToolchainRoutingError("unsupported target API for auto-route: %s" % value)
    return api


def _require_format_for_api(api: str, shader_format: str) -> None:
    formats_by_api = {
        "d3d11": frozenset({"dxbc"}),
        "d3d12": frozenset({"dxbc", "dxil"}),
        "vulkan": frozenset({"spirv"}),
        # RenderDoc deliberately distinguishes Vulkan SPIR-V from OpenGL
        # extension-flavoured SPIR-V even though the physical word stream has
        # the same magic. Capture encoding, not the module header, is the
        # authority for this distinction.
        "opengl": frozenset({"opengl-spirv"}),
    }
    if api and shader_format not in formats_by_api[api]:
        raise ToolchainRoutingError(
            "target API %s does not accept captured container %s"
            % (api, shader_format)
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _bounded_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ToolConfigurationError("%s must be a string" % field)
    result = value.strip()
    if (not result and not allow_empty) or len(result) > MAX_CAVEAT_CHARS:
        raise ToolConfigurationError(
            "%s must contain %s..%d characters"
            % (field, 0 if allow_empty else 1, MAX_CAVEAT_CHARS)
        )
    if "\x00" in result:
        raise ToolConfigurationError("%s must not contain NUL" % field)
    return result


@dataclass(frozen=True)
class FormatDetection:
    format: str | None
    confidence: str
    evidence: tuple[str, ...]
    is_binary: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "isBinary": self.is_binary,
        }


def _detect_text_format(text: str) -> FormatDetection:
    sample = text[:1024 * 1024]
    if re.search(r"\bOpCapability\b", sample) and re.search(
        r"\bOpMemoryModel\b", sample
    ):
        return FormatDetection(
            "spirv-asm", "strong", ("OpCapability and OpMemoryModel",), False
        )
    if "target triple = \"dxil-ms-dx\"" in sample or re.search(
        r"\bcall\s+[^\n]*@dx\.op\.", sample
    ):
        return FormatDetection(
            "dxil-asm", "strong", ("DXIL LLVM target/dx.op intrinsic",), False
        )
    if re.search(
        r"(?m)^\s*(?:ps|vs|gs|hs|ds|cs)_(?:4_[01]|5_[01])\s*$", sample
    ) and re.search(
        r"(?m)^\s*dcl_", sample
    ):
        return FormatDetection(
            "dxbc-asm", "strong", ("DXBC profile and dcl_ opcode",), False
        )

    wgsl_score = sum(
        marker in sample
        for marker in ("@vertex", "@fragment", "@compute", "@group(", "var<uniform>")
    )
    msl_score = sum(
        marker in sample
        for marker in ("using namespace metal", "[[stage_in]]", "texture2d<", "device ")
    )
    hlsl_score = sum(
        bool(re.search(pattern, sample))
        for pattern in (
            r"\bSV_[A-Za-z0-9_]+\b",
            r"\bcbuffer\s+[A-Za-z_]",
            r"\b(?:RW)?Texture(?:1D|2D|3D|Cube)(?:Array)?\b",
            r"\bregister\s*\(\s*[btsu]\d+",
            r"\b(?:float|half|uint|int)[1-4]x[1-4]\b",
        )
    )
    glsl_score = sum(
        bool(re.search(pattern, sample))
        for pattern in (
            r"(?m)^\s*#version\s+\d+",
            r"\bgl_(?:Position|FragCoord|FragColor|VertexID|InstanceID)\b",
            r"\blayout\s*\([^)]*(?:location|binding|set)\s*=",
            r"\b(?:sampler|image)(?:1D|2D|3D|Cube)(?:Array)?\b",
        )
    )
    scores = {"wgsl": wgsl_score, "msl": msl_score, "hlsl": hlsl_score, "glsl": glsl_score}
    best = max(scores.values())
    winners = sorted(name for name, score in scores.items() if score == best and score > 0)
    uniquely_strong_single_marker = (
        len(winners) == 1
        and (
            (winners[0] == "hlsl" and bool(re.search(r"\bSV_[A-Za-z0-9_]+\b", sample)))
            or (winners[0] == "glsl" and bool(re.search(r"(?m)^\s*#version\s+\d+", sample)))
        )
    )
    if (best >= 2 or uniquely_strong_single_marker) and len(winners) == 1:
        selected = winners[0]
        if selected == "glsl" and re.search(r"(?m)^\s*#version\s+\d+\s+es\b", sample):
            selected = "essl"
        return FormatDetection(
            selected,
            "strong",
            ("language marker score %d" % best,),
            False,
        )
    if best == 1 and len(winners) == 1 and winners[0] in {"wgsl", "msl"}:
        return FormatDetection(
            winners[0], "weak", ("one language-specific marker",), False
        )
    return FormatDetection(
        None,
        "unknown",
        ("UTF-8 text is absent or language markers are ambiguous",),
        False,
    )


def detect_shader_format(data: bytes, hint: str | None = None) -> FormatDetection:
    """Detect well-known shader containers/IR and conservative source markers.

    A caller-supplied hint is only accepted when strong binary/text evidence does
    not contradict it.  Source languages overlap heavily, so ambiguous text is
    returned as unknown instead of guessed.
    """

    if not isinstance(data, bytes):
        raise TypeError("shader data must be bytes")
    detected: FormatDetection
    container = fingerprint_bytes(data)
    if (
        container is not None
        and container.byte_size == len(data)
        and any(chunk.fourcc in {"SHDR", "SHEX", "DXIL"} for chunk in container.chunks)
    ):
        detected = FormatDetection(
            container.kind.lower(),
            "exact",
            ("well-formed DXBC container and executable chunk",),
            True,
        )
    elif len(data) >= 20 and len(data) % 4 == 0:
        magic, version, unused_generator, bound, schema = struct.unpack_from(
            "<5I", data, 0
        )
        version_shape_valid = (
            version & 0xFF0000FF == 0 and ((version >> 16) & 0xFF) > 0
        )
        header_valid = version_shape_valid and bound > 0 and schema == 0
        if magic == 0x07230203:
            if header_valid:
                detected = FormatDetection(
                    "spirv",
                    "strong",
                    (
                        "SPIR-V little-endian magic, aligned words, and plausible header",
                        "full module validation is separate",
                    ),
                    True,
                )
            else:
                detected = FormatDetection(
                    None, "unknown", ("SPIR-V magic has a malformed header",), True
                )
        elif magic == 0x03022307:
            swapped = struct.unpack_from(">5I", data, 0)
            swapped_version = swapped[1]
            swapped_header_valid = (
                swapped_version & 0xFF0000FF == 0
                and ((swapped_version >> 16) & 0xFF) > 0
                and swapped[3] > 0
                and swapped[4] == 0
            )
            detected = (
                FormatDetection(
                    "spirv",
                    "strong",
                    (
                        "byte-swapped SPIR-V magic and plausible header",
                        "consumer compatibility and full validation are separate",
                    ),
                    True,
                )
                if swapped_header_valid
                else FormatDetection(
                    None, "unknown", ("byte-swapped SPIR-V magic has a malformed header",), True
                )
            )
        else:
            detected = FormatDetection(None, "unknown", ("unrecognized binary/text",), None)
    else:
        detected = FormatDetection(None, "unknown", ("unrecognized binary/text",), None)

    if detected.format is None:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
        else:
            if "\x00" not in text:
                detected = _detect_text_format(text)

    if hint is None:
        return detected
    declared = normalize_format(hint)
    declared_semantic_spirv = {
        ("spirv", "opengl-spirv"),
        ("spirv-asm", "opengl-spirv-asm"),
    }
    if (
        detected.format is not None
        and detected.format != declared
        and (detected.format, declared) not in declared_semantic_spirv
    ):
        raise ToolchainRoutingError(
            "shader format hint %s contradicts detected %s evidence"
            % (declared, detected.format)
        )
    if detected.format is not None:
        if (detected.format, declared) in declared_semantic_spirv:
            return FormatDetection(
                declared,
                "declared-semantic-environment",
                detected.evidence
                + (
                    "capture encoding distinguishes OpenGL SPIR-V from the physically identical generic word stream",
                ),
                detected.is_binary,
            )
        return FormatDetection(
            detected.format,
            detected.confidence,
            detected.evidence + ("caller hint agrees",),
            detected.is_binary,
        )
    return FormatDetection(
        declared,
        "declared",
        detected.evidence + ("caller hint used because content was ambiguous",),
        declared in BINARY_FORMATS,
    )


def _read_shader_file(path: str | Path) -> tuple[Path, bytes]:
    source_path = Path(path).expanduser()
    if source_path.is_symlink() or not source_path.is_file():
        raise ToolchainRoutingError("shader path must be a regular non-symlink file")
    shader_path = source_path.resolve()
    size = shader_path.stat().st_size
    if size <= 0 or size > MAX_SHADER_BYTES:
        raise ToolchainRoutingError(
            "shader file must contain 1..%d bytes" % MAX_SHADER_BYTES
        )
    data = shader_path.read_bytes()
    if len(data) != size:
        raise ToolchainRoutingError("shader file changed while it was being read")
    return shader_path, data


def detect_shader_file(path: str | Path, hint: str | None = None) -> FormatDetection:
    unused_path, data = _read_shader_file(path)
    return detect_shader_format(data, hint)


@dataclass(frozen=True)
class RouteCapability:
    route_id: str
    tool_id: str
    operation: str
    tool_config_digest: str
    input_format: str
    output_format: str
    stages: tuple[str, ...]
    fidelity: str
    priority: int
    implementation: str
    source_url: str
    license_name: str
    caveats: tuple[str, ...]

    def supports_stage(self, stage: str) -> bool:
        return "*" in self.stages or stage in self.stages

    @property
    def is_reconstructed(self) -> bool:
        return self.fidelity == "reconstructed-source"

    def as_dict(self) -> dict[str, Any]:
        return {
            "routeId": self.route_id,
            "toolId": self.tool_id,
            "operation": self.operation,
            "toolConfigDigest": self.tool_config_digest,
            "inputFormat": self.input_format,
            "outputFormat": self.output_format,
            "stages": list(self.stages),
            "fidelity": self.fidelity,
            "priority": self.priority,
            "implementation": self.implementation,
            "sourceUrl": self.source_url,
            "license": self.license_name,
            "caveats": list(self.caveats),
            "requiresReplayAbiValidation": self.fidelity
            in {"native-compile", "reconstructed-source", "custom-transform"},
        }


@dataclass(frozen=True)
class ValidatorCapability:
    validator_id: str
    tool_id: str
    tool_config_digest: str
    input_format: str
    stages: tuple[str, ...]
    priority: int
    implementation: str
    source_url: str
    license_name: str
    caveats: tuple[str, ...]

    def supports_stage(self, stage: str) -> bool:
        return "*" in self.stages or stage in self.stages

    def as_dict(self) -> dict[str, Any]:
        return {
            "validatorId": self.validator_id,
            "toolId": self.tool_id,
            "operation": "validate",
            "toolConfigDigest": self.tool_config_digest,
            "inputFormat": self.input_format,
            "stages": list(self.stages),
            "priority": self.priority,
            "implementation": self.implementation,
            "sourceUrl": self.source_url,
            "license": self.license_name,
            "caveats": list(self.caveats),
        }


def _parse_stages(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > len(STAGES):
        raise ToolConfigurationError("%s must be a non-empty bounded array" % field)
    stages: set[str] = set()
    for raw_stage in value:
        stage = "*" if raw_stage == "*" else normalize_stage(raw_stage)
        if stage not in STAGES:
            raise ToolConfigurationError("%s has unsupported stage %s" % (field, raw_stage))
        stages.add(stage)
    if "*" in stages and len(stages) != 1:
        raise ToolConfigurationError("%s wildcard stage must be used alone" % field)
    return tuple(sorted(stages))


def _parse_validator(index: int, value: Any, registry: ToolRegistry) -> ValidatorCapability:
    field = "validators[%d]" % index
    if not isinstance(value, Mapping):
        raise ToolConfigurationError("%s must be an object" % field)
    allowed = {
        "validatorId",
        "toolId",
        "toolConfigDigest",
        "inputFormat",
        "stages",
        "priority",
        "implementation",
        "sourceUrl",
        "license",
        "caveats",
    }
    unknown = set(value).difference(allowed)
    if unknown:
        raise ToolConfigurationError(
            "%s has unknown fields: %s" % (field, ", ".join(sorted(unknown)))
        )
    validator_id = str(value.get("validatorId", ""))
    if not ROUTE_ID_RE.fullmatch(validator_id):
        raise ToolConfigurationError("%s.validatorId is invalid" % field)
    tool_id = str(value.get("toolId", ""))
    operation = registry.operation(tool_id, "validate")
    digest = str(value.get("toolConfigDigest", "")).lower()
    if not SHA256_RE.fullmatch(digest) or digest != operation.config_digest:
        raise ToolConfigurationError(
            "%s.toolConfigDigest does not match the pinned validator" % field
        )
    input_format = normalize_format(value.get("inputFormat"))
    stages = _parse_stages(value.get("stages"), field + ".stages")
    priority = value.get("priority", 100)
    if isinstance(priority, bool):
        raise ToolConfigurationError("%s.priority must be an integer" % field)
    try:
        priority = int(priority)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ToolConfigurationError("%s.priority must be an integer" % field) from exc
    if priority < 0 or priority > 10000:
        raise ToolConfigurationError("%s.priority must be between 0 and 10000" % field)
    implementation = _bounded_text(value.get("implementation", "custom"), field + ".implementation")
    source_url = _bounded_text(value.get("sourceUrl", ""), field + ".sourceUrl", allow_empty=True)
    if source_url and not source_url.startswith("https://"):
        raise ToolConfigurationError("%s.sourceUrl must use https" % field)
    license_name = _bounded_text(value.get("license", ""), field + ".license", allow_empty=True)
    caveats_value = value.get("caveats", [])
    if not isinstance(caveats_value, list) or len(caveats_value) > MAX_CAVEATS:
        raise ToolConfigurationError("%s.caveats must be a bounded array" % field)
    caveats = tuple(
        _bounded_text(item, "%s.caveats[%d]" % (field, item_index))
        for item_index, item in enumerate(caveats_value)
    )
    return ValidatorCapability(
        validator_id=validator_id,
        tool_id=tool_id,
        tool_config_digest=digest,
        input_format=input_format,
        stages=stages,
        priority=priority,
        implementation=implementation,
        source_url=source_url,
        license_name=license_name,
        caveats=caveats,
    )


def _parse_route(index: int, value: Any, registry: ToolRegistry) -> RouteCapability:
    field = "routes[%d]" % index
    if not isinstance(value, Mapping):
        raise ToolConfigurationError("%s must be an object" % field)
    allowed = {
        "routeId",
        "toolId",
        "operation",
        "toolConfigDigest",
        "inputFormat",
        "outputFormat",
        "stages",
        "fidelity",
        "priority",
        "implementation",
        "sourceUrl",
        "license",
        "caveats",
    }
    unknown = set(value).difference(allowed)
    if unknown:
        raise ToolConfigurationError(
            "%s has unknown fields: %s" % (field, ", ".join(sorted(unknown)))
        )
    route_id = str(value.get("routeId", ""))
    if not ROUTE_ID_RE.fullmatch(route_id):
        raise ToolConfigurationError("%s.routeId is invalid" % field)
    tool_id = str(value.get("toolId", ""))
    operation_name = str(value.get("operation", ""))
    if operation_name not in OPERATIONS:
        raise ToolConfigurationError(
            "%s.operation must be decompile, compile, or transform" % field
        )
    operation = registry.operation(tool_id, operation_name)
    digest = str(value.get("toolConfigDigest", "")).lower()
    if not SHA256_RE.fullmatch(digest) or digest != operation.config_digest:
        raise ToolConfigurationError(
            "%s.toolConfigDigest does not match the pinned operation" % field
        )
    input_format = normalize_format(value.get("inputFormat"))
    output_format = normalize_format(value.get("outputFormat"))
    if input_format == output_format:
        raise ToolConfigurationError("%s must change format" % field)
    stages_value = value.get("stages")
    if not isinstance(stages_value, list) or not stages_value or len(stages_value) > len(STAGES):
        raise ToolConfigurationError("%s.stages must be a non-empty bounded array" % field)
    stages: set[str] = set()
    for raw_stage in stages_value:
        stage = "*" if raw_stage == "*" else normalize_stage(raw_stage)
        if stage not in STAGES:
            raise ToolConfigurationError("%s has unsupported stage %s" % (field, raw_stage))
        stages.add(stage)
    if "*" in stages and len(stages) != 1:
        raise ToolConfigurationError("%s wildcard stage must be used alone" % field)
    fidelity = str(value.get("fidelity", ""))
    if fidelity not in FIDELITIES:
        raise ToolConfigurationError("%s.fidelity is unsupported" % field)
    if operation_name == "decompile" and input_format not in BINARY_FORMATS:
        raise ToolConfigurationError("%s.decompile input must be a binary format" % field)
    if operation_name == "compile" and output_format not in BINARY_FORMATS:
        raise ToolConfigurationError("%s.compile output must be a binary format" % field)
    if input_format == "hlsl" and operation_name == "compile" and "target_profile" not in operation.placeholders:
        raise ToolConfigurationError(
            "%s HLSL compiler must consume the exact {target_profile} placeholder" % field
        )
    if operation_name == "transform" and fidelity != "custom-transform":
        raise ToolConfigurationError("%s.transform must be labelled custom-transform" % field)
    if fidelity == "reconstructed-source" and output_format not in TEXT_FORMATS:
        raise ToolConfigurationError("%s reconstructed output must be text" % field)
    priority_raw = value.get("priority", 100)
    if isinstance(priority_raw, bool):
        raise ToolConfigurationError("%s.priority must be an integer" % field)
    try:
        priority = int(priority_raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ToolConfigurationError("%s.priority must be an integer" % field) from exc
    if priority < 0 or priority > 10000:
        raise ToolConfigurationError("%s.priority must be between 0 and 10000" % field)
    implementation = _bounded_text(value.get("implementation", "custom"), field + ".implementation")
    source_url = _bounded_text(value.get("sourceUrl", ""), field + ".sourceUrl", allow_empty=True)
    if source_url and not source_url.startswith("https://"):
        raise ToolConfigurationError("%s.sourceUrl must use https" % field)
    license_name = _bounded_text(value.get("license", ""), field + ".license", allow_empty=True)
    caveats_value = value.get("caveats", [])
    if not isinstance(caveats_value, list) or len(caveats_value) > MAX_CAVEATS:
        raise ToolConfigurationError("%s.caveats must be a bounded array" % field)
    caveats = tuple(
        _bounded_text(item, "%s.caveats[%d]" % (field, caveat_index))
        for caveat_index, item in enumerate(caveats_value)
    )
    if fidelity == "reconstructed-source" and not caveats:
        raise ToolConfigurationError(
            "%s reconstructed-source route must state at least one caveat" % field
        )
    return RouteCapability(
        route_id=route_id,
        tool_id=tool_id,
        operation=operation_name,
        tool_config_digest=digest,
        input_format=input_format,
        output_format=output_format,
        stages=tuple(sorted(stages)),
        fidelity=fidelity,
        priority=priority,
        implementation=implementation,
        source_url=source_url,
        license_name=license_name,
        caveats=caveats,
    )


_KNOWN_MATRIX = (
    ("dxbc", "dxbc-asm", "FXC/RenderDoc can expose assembly, not original HLSL."),
    ("dxbc", "hlsl", "No Microsoft compiler provides a general DXBC-to-original-HLSL recovery guarantee."),
    ("hlsl", "dxbc", "FXC compiles Shader Model 2 through 5.1 HLSL to DXBC."),
    ("dxil", "dxil-asm", "DXC disassembles DXIL to an LLVM/DXIL representation, not recovered HLSL."),
    ("dxil", "hlsl", "DXC does not provide a general DXIL-to-original-HLSL decompiler."),
    ("hlsl", "dxil", "DXC compiles Shader Model 6+ HLSL to DXIL."),
    ("spirv", "spirv-asm", "SPIRV-Tools provides the normative-style binary disassembly path."),
    ("spirv-asm", "spirv", "SPIRV-Tools assembles text but validation is a separate step."),
    ("spirv", "hlsl", "SPIRV-Cross reconstructs usable HLSL; names/source structure can be lost."),
    ("spirv", "glsl", "SPIRV-Cross reconstructs GLSL; target semantics/options must be selected deliberately."),
    ("hlsl", "spirv", "DXC supports HLSL-to-SPIR-V for Vulkan; binding/environment options affect ABI."),
    ("glsl", "spirv", "glslang is the Khronos reference GLSL front end and SPIR-V generator."),
    (
        "opengl-spirv",
        "opengl-spirv-asm",
        "OpenGL SPIR-V has the same physical magic as Vulkan SPIR-V but RenderDoc keeps the semantic environment distinct.",
    ),
    (
        "opengl-spirv-asm",
        "opengl-spirv",
        "SPIRV-Tools can assemble the text, but OpenGL-environment validation is separate.",
    ),
    (
        "opengl-spirv",
        "glsl",
        "SPIRV-Cross reconstructs OpenGL GLSL only when the capture encoding identifies OpenGL semantics.",
    ),
    (
        "glsl",
        "opengl-spirv",
        "glslang -G compiles GLSL under OpenGL SPIR-V semantics; this is not Vulkan SPIR-V.",
    ),
)


class ToolchainCatalog:
    """Immutable capability routes bound to one immutable tool-registry snapshot."""

    def __init__(
        self,
        tool_config_path: str | Path,
        route_config_path: str | Path,
        *,
        registry: ToolRegistry | None = None,
    ):
        requested_registry_path = Path(tool_config_path).expanduser().resolve()
        if registry is not None and registry.path != requested_registry_path:
            raise ToolConfigurationError(
                "supplied tool registry path does not match tool_config_path"
            )
        self.registry = registry or ToolRegistry(requested_registry_path)
        self.path = Path(route_config_path).expanduser().resolve()
        try:
            value = read_json_object(self.path, MAX_CONFIG_BYTES, "shader toolchain config")
        except ValueError as exc:
            raise ToolConfigurationError(str(exc)) from exc
        unknown_top = set(value).difference(
            {"schema", "schemaVersion", "toolRegistrySha256", "routes", "validators"}
        )
        if unknown_top:
            raise ToolConfigurationError(
                "unknown shader toolchain config fields: %s"
                % ", ".join(sorted(unknown_top))
            )
        if value.get("schema") != TOOLCHAIN_SCHEMA or value.get("schemaVersion") != TOOLCHAIN_VERSION:
            raise ToolConfigurationError("unsupported shader toolchain schema/version")
        expected_registry_hash = str(value.get("toolRegistrySha256", "")).lower()
        actual_registry_hash = _sha256_file(self.registry.path)
        if not SHA256_RE.fullmatch(expected_registry_hash) or expected_registry_hash != actual_registry_hash:
            raise ToolConfigurationError(
                "toolRegistrySha256 does not match the exact shader tool registry bytes"
            )
        route_values = value.get("routes")
        if not isinstance(route_values, list) or len(route_values) > MAX_ROUTES:
            raise ToolConfigurationError(
                "shader toolchain routes must contain 0..%d records" % MAX_ROUTES
            )
        routes = [_parse_route(index, item, self.registry) for index, item in enumerate(route_values)]
        ids = [item.route_id for item in routes]
        if len(set(ids)) != len(ids):
            raise ToolConfigurationError("shader toolchain routeId values must be unique")
        validator_values = value.get("validators", [])
        if not isinstance(validator_values, list) or len(validator_values) > MAX_ROUTES:
            raise ToolConfigurationError("shader toolchain validators must be a bounded array")
        validators = [
            _parse_validator(index, item, self.registry)
            for index, item in enumerate(validator_values)
        ]
        validator_ids = [item.validator_id for item in validators]
        if len(set(validator_ids)) != len(validator_ids):
            raise ToolConfigurationError("shader toolchain validatorId values must be unique")
        if not routes and not validators:
            raise ToolConfigurationError(
                "shader toolchain config must contain at least one route or validator"
            )
        self.tool_registry_sha256 = actual_registry_hash
        self._routes = tuple(sorted(routes, key=lambda item: item.route_id))
        self._validators = tuple(sorted(validators, key=lambda item: item.validator_id))

    def routes(self) -> tuple[RouteCapability, ...]:
        return self._routes

    def validators(self) -> tuple[ValidatorCapability, ...]:
        return self._validators

    @staticmethod
    def _is_direct_native_ir_round_trip(
        original: str,
        editable: str,
        forward: Sequence[RouteCapability],
        reverse: Sequence[RouteCapability],
    ) -> bool:
        """Accept only one-hop disassembly plus one-hop assembly fallback.

        This prevents a reconstructed-source or custom-transform chain from
        being relabelled as native IR merely because it happens to terminate at
        an assembly text format.
        """

        return bool(
            len(forward) == 1
            and len(reverse) == 1
            and forward[0].input_format == original
            and forward[0].output_format == editable
            and forward[0].fidelity == "ir-disassembly"
            and reverse[0].input_format == editable
            and reverse[0].output_format == original
            and reverse[0].fidelity == "ir-assembly"
        )

    def matching_validators(
        self, input_format: str, stage: str, *, require_integrity: bool = True
    ) -> tuple[ValidatorCapability, ...]:
        shader_format = normalize_format(input_format)
        normalized_stage = normalize_stage(stage)
        integrity = self._integrity() if require_integrity else {}
        result = [
            item
            for item in self._validators
            if item.input_format == shader_format
            and item.supports_stage(normalized_stage)
            and (
                not require_integrity
                or integrity[(item.tool_id, "validate")]["available"]
            )
        ]
        return tuple(sorted(result, key=lambda item: (item.priority, item.validator_id)))

    def _integrity(self) -> dict[tuple[str, str], dict[str, Any]]:
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for operation in self.registry.operations():
            key = (operation.tool_id, operation.operation)
            result[key] = self.registry.operation_integrity(*key)
        return result

    def matching_routes(
        self,
        input_format: str,
        output_format: str,
        stage: str,
        *,
        allow_reconstructed: bool = True,
        require_integrity: bool = True,
    ) -> tuple[RouteCapability, ...]:
        source = normalize_format(input_format)
        target = normalize_format(output_format)
        normalized_stage = normalize_stage(stage)
        if normalized_stage not in STAGES or normalized_stage == "*":
            raise ToolchainRoutingError("a concrete supported shader stage is required")
        integrity = self._integrity() if require_integrity else {}
        result = []
        for route in self._routes:
            if route.input_format != source or route.output_format != target:
                continue
            if not route.supports_stage(normalized_stage):
                continue
            if not allow_reconstructed and route.is_reconstructed:
                continue
            if require_integrity and not integrity[(route.tool_id, route.operation)]["available"]:
                continue
            result.append(route)
        return tuple(sorted(result, key=lambda item: (item.priority, item.route_id)))

    def find_path(
        self,
        input_format: str,
        output_format: str,
        stage: str,
        *,
        allow_reconstructed: bool = True,
        max_hops: int = MAX_ROUTE_HOPS,
        require_unique: bool = True,
    ) -> tuple[RouteCapability, ...]:
        source = normalize_format(input_format)
        target = normalize_format(output_format)
        normalized_stage = normalize_stage(stage)
        if normalized_stage not in STAGES or normalized_stage == "*":
            raise ToolchainRoutingError("a concrete supported shader stage is required")
        if not isinstance(allow_reconstructed, bool):
            raise ToolchainRoutingError("allow_reconstructed must be a boolean")
        if (
            isinstance(max_hops, bool)
            or not isinstance(max_hops, int)
            or max_hops < 1
            or max_hops > MAX_ROUTE_HOPS
        ):
            raise ToolchainRoutingError("max_hops must be between 1 and %d" % MAX_ROUTE_HOPS)
        if source == target:
            return ()
        integrity = self._integrity()
        candidates = [
            route
            for route in self._routes
            if route.supports_stage(normalized_stage)
            and (allow_reconstructed or not route.is_reconstructed)
            and integrity[(route.tool_id, route.operation)]["available"]
        ]
        paths: list[tuple[RouteCapability, ...]] = []

        def visit(current: str, path: tuple[RouteCapability, ...], seen: frozenset[str]) -> None:
            if len(path) >= max_hops:
                return
            for route in candidates:
                if route.input_format != current or route.output_format in seen:
                    continue
                next_path = path + (route,)
                if route.output_format == target:
                    paths.append(next_path)
                else:
                    visit(route.output_format, next_path, seen | {route.output_format})

        visit(source, (), frozenset({source}))
        if not paths:
            raise ToolchainRoutingError(
                "no integrity-valid %s -> %s route supports stage %s%s"
                % (
                    source,
                    target,
                    normalized_stage,
                    " without reconstructed source" if not allow_reconstructed else "",
                )
            )

        def score(path: tuple[RouteCapability, ...]) -> tuple[Any, ...]:
            reconstructed = sum(item.is_reconstructed for item in path)
            custom = sum(item.fidelity == "custom-transform" for item in path)
            return (
                reconstructed,
                custom,
                sum(item.priority for item in path),
                len(path),
            )
        best_score = min(score(path) for path in paths)
        best = sorted(
            (path for path in paths if score(path) == best_score),
            key=lambda path: tuple(item.route_id for item in path),
        )
        if require_unique and len(best) != 1:
            raise ToolchainRoutingError(
                "auto-route is ambiguous for %s -> %s at stage %s: %s; "
                "adjust trusted priorities or choose an explicit route ID"
                % (
                    source,
                    target,
                    normalized_stage,
                    "; ".join(
                        "+".join(item.route_id for item in path) for path in best
                    ),
                )
            )
        return best[0]

    def candidate_paths(
        self,
        input_format: str,
        output_format: str,
        stage: str,
        *,
        allow_reconstructed: bool = True,
        max_hops: int = MAX_ROUTE_HOPS,
        require_integrity: bool = True,
        limit: int = 16,
    ) -> tuple[tuple[RouteCapability, ...], ...]:
        """Return bounded deterministic route candidates, best first.

        Unlike :meth:`find_path`, this method does not treat an equal-score tie
        as an error.  Recovery orchestration needs the complete, fixed set of
        registered alternatives so program logic (or an AI choosing only from
        those IDs) can retry another implementation without ever supplying a
        command line or executable path.
        """

        source = normalize_format(input_format)
        target = normalize_format(output_format)
        normalized_stage = normalize_stage(stage)
        if normalized_stage not in STAGES or normalized_stage == "*":
            raise ToolchainRoutingError("a concrete supported shader stage is required")
        if not isinstance(allow_reconstructed, bool):
            raise ToolchainRoutingError("allow_reconstructed must be a boolean")
        if (
            isinstance(max_hops, bool)
            or not isinstance(max_hops, int)
            or max_hops < 1
            or max_hops > MAX_ROUTE_HOPS
        ):
            raise ToolchainRoutingError("max_hops must be between 1 and %d" % MAX_ROUTE_HOPS)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 128:
            raise ToolchainRoutingError("candidate path limit must be between 1 and 128")
        if source == target:
            return ((),)

        integrity = self._integrity() if require_integrity else {}
        routes = [
            route
            for route in self._routes
            if route.supports_stage(normalized_stage)
            and (allow_reconstructed or not route.is_reconstructed)
            and (
                not require_integrity
                or integrity[(route.tool_id, route.operation)]["available"]
            )
        ]
        discovered: list[tuple[RouteCapability, ...]] = []

        def visit(
            current: str,
            path: tuple[RouteCapability, ...],
            seen: frozenset[str],
        ) -> None:
            if len(path) >= max_hops:
                return
            for route in routes:
                if route.input_format != current or route.output_format in seen:
                    continue
                candidate = path + (route,)
                if route.output_format == target:
                    discovered.append(candidate)
                else:
                    visit(
                        route.output_format,
                        candidate,
                        seen | {route.output_format},
                    )

        visit(source, (), frozenset({source}))

        def score(path: tuple[RouteCapability, ...]) -> tuple[Any, ...]:
            return (
                sum(item.is_reconstructed for item in path),
                sum(item.fidelity == "custom-transform" for item in path),
                sum(item.priority for item in path),
                len(path),
                tuple(item.route_id for item in path),
            )

        ordered = sorted(discovered, key=score)
        return tuple(ordered[:limit])

    def plan_recovery(
        self,
        input_format: str,
        stage: str,
        *,
        editable_format: str | None = None,
        capture_encoding: str | None = None,
        target_api: str | None = None,
        allow_reconstructed: bool = True,
    ) -> dict[str, Any]:
        """Plan a recoverable workspace even when decompilation is unavailable.

        Program logic owns format detection, route enumeration, integrity and
        the allowed next actions.  AI is permitted to choose among those
        actions and, only when a fixed reverse compiler route exists, author a
        provisional source candidate.  The result never authorizes replay
        apply and never claims original-source or semantic recovery.
        """

        original = normalize_format(input_format)
        normalized_stage = normalize_stage(stage)
        if original not in BINARY_FORMATS:
            raise ToolchainRoutingError(
                "automatic recovery planning requires a captured binary shader format"
            )
        if not isinstance(allow_reconstructed, bool):
            raise ToolchainRoutingError("allow_reconstructed must be a boolean")
        if capture_encoding is not None:
            encoding = normalize_format(capture_encoding)
            if encoding != original:
                raise ToolchainRoutingError(
                    "capture encoding %s contradicts detected container %s"
                    % (encoding, original)
                )
        api = _normalize_target_api(target_api)
        _require_format_for_api(api, original)
        if original in {"spirv", "opengl-spirv"} and not api:
            raise ToolchainRoutingError(
                "SPIR-V target API/environment cannot be derived from the module; "
                "capture metadata must identify the API"
            )
        if editable_format is None:
            editable = "hlsl" if original in {"dxbc", "dxil"} else "glsl"
        else:
            editable = normalize_format(editable_format)
        if editable not in TEXT_FORMATS:
            raise ToolchainRoutingError("recovery editable format must be textual")

        decompile_paths = self.candidate_paths(
            original,
            editable,
            normalized_stage,
            allow_reconstructed=allow_reconstructed,
            require_integrity=True,
        )
        compile_paths = self.candidate_paths(
            editable,
            original,
            normalized_stage,
            allow_reconstructed=allow_reconstructed,
            require_integrity=True,
        )
        configured_decompile_paths = self.candidate_paths(
            original,
            editable,
            normalized_stage,
            allow_reconstructed=allow_reconstructed,
            require_integrity=False,
        )
        configured_compile_paths = self.candidate_paths(
            editable,
            original,
            normalized_stage,
            allow_reconstructed=allow_reconstructed,
            require_integrity=False,
        )

        if decompile_paths and compile_paths:
            mode = "round-trip"
        elif compile_paths:
            mode = "compile-only-recovery"
        else:
            mode = "evidence-only"

        def path_ids(path: Sequence[RouteCapability]) -> list[str]:
            return [item.route_id for item in path]

        primary_decompile = decompile_paths[0] if decompile_paths else ()
        primary_compile = compile_paths[0] if compile_paths else ()
        alternate_route_pairs = [
            {
                "decompileCandidateIndex": decompile_index,
                "compileCandidateIndex": compile_index,
                "decompileRouteIds": path_ids(decompile_path),
                "compileRouteIds": path_ids(compile_path),
            }
            for decompile_index, decompile_path in enumerate(decompile_paths)
            for compile_index, compile_path in enumerate(compile_paths)
            if (decompile_index, compile_index) != (0, 0)
        ]
        diagnostics = [
            {
                "phase": "decompile",
                "category": (
                    "route-ready"
                    if decompile_paths
                    else (
                        "tool-integrity-unavailable"
                        if configured_decompile_paths
                        else "route-unavailable"
                    )
                ),
                "readyCandidateCount": len(decompile_paths),
                "configuredCandidateCount": len(configured_decompile_paths),
            },
            {
                "phase": "compile",
                "category": (
                    "route-ready"
                    if compile_paths
                    else (
                        "tool-integrity-unavailable"
                        if configured_compile_paths
                        else "route-unavailable"
                    )
                ),
                "readyCandidateCount": len(compile_paths),
                "configuredCandidateCount": len(configured_compile_paths),
            },
        ]
        allowed_actions: list[dict[str, Any]] = [
            {
                "id": "inspect-native-ir",
                "owner": "program",
                "requiresCompileRoute": False,
                "producesExecutableSource": False,
            }
        ]
        if decompile_paths:
            executable_decompile = bool(compile_paths)
            allowed_actions.append(
                {
                    "id": "run-registered-decompiler",
                    "owner": "program",
                    "routeIds": path_ids(primary_decompile),
                    "requiresCompileRoute": executable_decompile,
                    "producesExecutableSource": executable_decompile,
                    "producesExplanatoryIr": not executable_decompile,
                }
            )
            if len(decompile_paths) > 1 or alternate_route_pairs:
                allowed_actions.append(
                    {
                        "id": "retry-registered-decompiler",
                        "owner": "program-or-ai-selection",
                        "candidateRoutePaths": [
                            path_ids(path) for path in decompile_paths[1:]
                        ],
                        # A recovered source is executable-editable only after
                        # the untouched baseline also passes one fixed reverse
                        # compiler path.  Keep the finite Cartesian alternatives
                        # explicit so recovery can retry the same decompiler with
                        # another compiler instead of incorrectly consuming the
                        # whole decompile candidate after one compile-probe failure.
                        "candidateRoutePairs": alternate_route_pairs,
                        "requiresCompileRoute": executable_decompile,
                        "producesExecutableSource": executable_decompile,
                        "producesExplanatoryIr": not executable_decompile,
                    }
                )
        if compile_paths:
            allowed_actions.extend(
                [
                    {
                        "id": "seed-reviewed-asset-source",
                        "owner": "program-or-ai-selection",
                        "requiresExactIdentityAndFreshAbiValidation": True,
                        "producesExecutableSource": True,
                    },
                    {
                        "id": "construct-provisional-source",
                        "owner": "ai",
                        "requiresExplicitHashBoundPromotion": True,
                        "producesExecutableSource": True,
                    },
                ]
            )
        else:
            allowed_actions.append(
                {
                    "id": "install-or-register-format-provider",
                    "owner": "user-or-program-owner",
                    "producesExecutableSource": False,
                }
            )

        chosen = list(primary_decompile) + list(primary_compile)
        if not api and any(
            "target_api"
            in self.registry.operation(item.tool_id, item.operation).placeholders
            for item in chosen
        ):
            raise ToolchainRoutingError(
                "the selected recovery route requires the physical target API; it "
                "will not be inferred from shader bytes"
            )
        return {
            "schema": RECOVERY_PLAN_SCHEMA,
            "schemaVersion": 1,
            "mode": mode,
            "inputFormat": original,
            "captureEncoding": original,
            "targetApi": api or None,
            "stage": normalized_stage,
            "editableFormat": editable,
            "decompileRouteIds": path_ids(primary_decompile),
            "decompilerToolIds": [item.tool_id for item in primary_decompile],
            "compileRouteIds": path_ids(primary_compile),
            "compilerToolIds": [item.tool_id for item in primary_compile],
            "capabilities": [item.as_dict() for item in chosen],
            "decompileCandidates": [path_ids(path) for path in decompile_paths],
            "compileCandidates": [path_ids(path) for path in compile_paths],
            "configuredButUnavailable": {
                "decompile": [path_ids(path) for path in configured_decompile_paths]
                if not decompile_paths
                else [],
                "compile": [path_ids(path) for path in configured_compile_paths]
                if not compile_paths
                else [],
            },
            "diagnostics": diagnostics,
            "allowedActions": allowed_actions,
            "aiPolicy": {
                "mayClassifyDiagnostics": True,
                "mayChooseOnlyListedActions": True,
                "mayAuthorProvisionalSource": bool(compile_paths),
                "maySupplyCommandOrExecutable": False,
                "mayAuthorizeApply": False,
            },
            "containsReconstructedSource": any(
                item.is_reconstructed for item in primary_decompile
            ),
            "lane": "candidate" if mode == "round-trip" else "evidence",
            "directReplayApplyAuthorized": False,
            "semanticEquivalenceVerified": False,
            "replayAbiValidationStillRequired": True,
            "guarantee": (
                "This plan binds available program routes and recovery choices only; "
                "it does not recover original source or prove semantic equivalence."
            ),
        }

    def plan_automatic_recovery(
        self,
        input_format: str,
        stage: str,
        *,
        capture_encoding: str | None = None,
        target_api: str | None = None,
        allow_reconstructed: bool = True,
    ) -> dict[str, Any]:
        """Prefer source, then select a complete native-IR round trip.

        A default request is a request for the best *usable* editable lane, not
        an assertion that high-level source exists.  HLSL/GLSL remains first;
        when it is only compile-only or evidence-only, a fixed same-family
        assembly lane wins if and only if both directions are hash-verified.
        Explicit ``editable_format`` requests continue to use
        :meth:`plan_recovery` and never switch languages behind the caller.
        """

        original = normalize_format(input_format)
        preferred = self.plan_recovery(
            original,
            stage,
            editable_format=None,
            capture_encoding=capture_encoding,
            target_api=target_api,
            allow_reconstructed=allow_reconstructed,
        )
        preferred_strict_error = ""
        if preferred.get("mode") == "round-trip":
            try:
                strict_preferred = self.select_round_trip(
                    original,
                    stage,
                    editable_format=str(preferred.get("editableFormat", "")),
                    capture_encoding=capture_encoding,
                    target_api=target_api,
                    allow_reconstructed=allow_reconstructed,
                )
            except ToolchainRoutingError as exc:
                preferred_strict_error = str(exc)
            else:
                if (
                    strict_preferred.get("decompileRouteIds")
                    == preferred.get("decompileRouteIds")
                    and strict_preferred.get("compileRouteIds")
                    == preferred.get("compileRouteIds")
                ):
                    return preferred
                preferred_strict_error = (
                    "strict preferred routes differ from recovery primary routes"
                )
        for editable in _NATIVE_EDITABLE_FALLBACKS.get(original, ()):
            try:
                strict = self.select_round_trip(
                    original,
                    stage,
                    editable_format=editable,
                    capture_encoding=capture_encoding,
                    target_api=target_api,
                    allow_reconstructed=False,
                )
                forward_count = len(strict.get("decompileRouteIds", []))
                capabilities = strict.get("capabilities", [])
                routes_by_id = {route.route_id: route for route in self._routes}
                forward = tuple(
                    routes_by_id[route_id]
                    for route_id in strict["decompileRouteIds"]
                )
                reverse = tuple(
                    routes_by_id[route_id]
                    for route_id in strict["compileRouteIds"]
                )
                if (
                    len(capabilities) != forward_count + len(reverse)
                    or not self._is_direct_native_ir_round_trip(
                        original, editable, forward, reverse
                    )
                ):
                    continue
                alternate = self.plan_recovery(
                    original,
                    stage,
                    editable_format=editable,
                    capture_encoding=capture_encoding,
                    target_api=target_api,
                    allow_reconstructed=False,
                )
            except ToolchainRoutingError:
                continue
            if (
                alternate.get("mode") != "round-trip"
                or alternate.get("decompileRouteIds")
                != strict.get("decompileRouteIds")
                or alternate.get("compileRouteIds")
                != strict.get("compileRouteIds")
            ):
                continue
            alternate["diagnostics"] = [
                {
                    "phase": "editable-selection",
                    "category": "automatic-native-ir-round-trip-fallback",
                    "preferredEditableFormat": preferred.get("editableFormat"),
                    "preferredMode": preferred.get("mode"),
                    "selectedEditableFormat": alternate.get("editableFormat"),
                    "preferredDiagnostics": list(
                        preferred.get("diagnostics", [])
                    ),
                    "preferredStrictError": preferred_strict_error,
                    "reason": (
                        "the preferred high-level lane had no complete round trip; "
                        "the selected native IR lane has fixed decompile and compile routes"
                    ),
                },
                *list(alternate.get("diagnostics", [])),
            ]
            return alternate
        if preferred_strict_error:
            raise ToolchainRoutingError(
                "preferred automatic shader routes are ambiguous or inconsistent, "
                "and no unique direct native-IR round trip is available: %s"
                % preferred_strict_error
            )
        return preferred

    def select_round_trip(
        self,
        input_format: str,
        stage: str,
        *,
        editable_format: str | None = None,
        capture_encoding: str | None = None,
        target_api: str | None = None,
        allow_reconstructed: bool = True,
    ) -> dict[str, Any]:
        """Fail-closed automatic decompiler/compiler selection.

        The selection is deterministic only when exactly one best route exists
        in each direction.  Tool IDs remain available through ``matching_routes``
        for an explicit advanced workflow, but an AI intent path cannot silently
        pick between equally ranked implementations.
        """

        original = normalize_format(input_format)
        normalized_stage = normalize_stage(stage)
        if not isinstance(allow_reconstructed, bool):
            raise ToolchainRoutingError("allow_reconstructed must be a boolean")
        if original not in BINARY_FORMATS:
            raise ToolchainRoutingError(
                "automatic round-trip planning requires a captured binary shader format"
            )
        if capture_encoding is not None:
            encoding = normalize_format(capture_encoding)
            if encoding != original:
                raise ToolchainRoutingError(
                    "capture encoding %s contradicts detected container %s"
                    % (encoding, original)
                )
        api = _normalize_target_api(target_api)
        _require_format_for_api(api, original)
        if original in {"spirv", "opengl-spirv"} and not api:
            raise ToolchainRoutingError(
                "SPIR-V target API/environment cannot be derived from the module; "
                "capture metadata must identify the API"
            )
        explicit_editable = bool(str(editable_format or "").strip())
        if explicit_editable:
            editable_candidates = (normalize_format(editable_format),)
        elif original in {"dxbc", "dxil"}:
            editable_candidates = (
                "hlsl",
                *_NATIVE_EDITABLE_FALLBACKS.get(original, ()),
            )
        elif original in {"spirv", "opengl-spirv"}:
            editable_candidates = (
                "glsl",
                *_NATIVE_EDITABLE_FALLBACKS.get(original, ()),
            )
        else:
            raise ToolchainRoutingError(
                "no safe default editable language exists for %s" % original
            )
        failures = []
        forward: tuple[RouteCapability, ...] | None = None
        reverse: tuple[RouteCapability, ...] | None = None
        editable = ""
        for candidate in editable_candidates:
            if candidate not in TEXT_FORMATS:
                raise ToolchainRoutingError(
                    "auto-route editable format must be textual"
                )
            try:
                candidate_forward = self.find_path(
                    original,
                    candidate,
                    normalized_stage,
                    allow_reconstructed=allow_reconstructed,
                    require_unique=True,
                )
                candidate_reverse = self.find_path(
                    candidate,
                    original,
                    normalized_stage,
                    allow_reconstructed=allow_reconstructed,
                    require_unique=True,
                )
            except ToolchainRoutingError as exc:
                if explicit_editable:
                    raise
                failures.append("%s: %s" % (candidate, exc))
                continue
            if (
                not explicit_editable
                and candidate in _NATIVE_EDITABLE_FALLBACKS.get(original, ())
                and not self._is_direct_native_ir_round_trip(
                    original,
                    candidate,
                    candidate_forward,
                    candidate_reverse,
                )
            ):
                failures.append(
                    "%s: automatic native-IR fallback requires one direct "
                    "ir-disassembly and one direct ir-assembly route" % candidate
                )
                continue
            editable = candidate
            forward = candidate_forward
            reverse = candidate_reverse
            break
        if forward is None or reverse is None:
            raise ToolchainRoutingError(
                "no complete automatic shader round trip is available (%s)"
                % "; ".join(failures)
            )
        if not api and any(
            "target_api"
            in self.registry.operation(item.tool_id, item.operation).placeholders
            for item in forward + reverse
        ):
            raise ToolchainRoutingError(
                "the selected route requires the physical target API; it will not "
                "be inferred from the shader container"
            )
        capabilities = [item.as_dict() for item in forward + reverse]
        return {
            "inputFormat": original,
            "captureEncoding": original,
            "targetApi": api or None,
            "stage": normalized_stage,
            "editableFormat": editable,
            "decompileRouteIds": [item.route_id for item in forward],
            "decompilerToolIds": [item.tool_id for item in forward],
            "compileRouteIds": [item.route_id for item in reverse],
            "compilerToolIds": [item.tool_id for item in reverse],
            "capabilities": capabilities,
            "why": [
                "container/encoding, stage, target API, and requested editable language all match",
                "every selected executable and fixed file passed a fresh SHA-256 integrity probe",
                "each direction had exactly one best trusted route; ambiguity fails closed",
                (
                    "the preferred high-level lane was unavailable, so a complete native-IR lane was selected"
                    if not explicit_editable
                    and editable in _NATIVE_EDITABLE_FALLBACKS.get(original, ())
                    else "the preferred or explicitly requested editable lane was selected"
                ),
            ],
            "containsReconstructedSource": any(item.is_reconstructed for item in forward),
            "lane": "candidate",
            "directReplayApplyAuthorized": False,
            "semanticEquivalenceVerified": False,
            "replayAbiValidationStillRequired": True,
            "candidateEligibleAfterRoundTripOnly": True,
            "guarantee": (
                "Selection and tool availability only. No route can guarantee recovery of "
                "the original high-level source from arbitrary compiled shader bytes."
            ),
        }

    def select_round_trip_for_file(
        self,
        shader_path: str | Path,
        stage: str,
        *,
        editable_format: str | None = None,
        capture_encoding: str | None = None,
        target_api: str | None = None,
        allow_reconstructed: bool = True,
    ) -> dict[str, Any]:
        unused_shader, data = _read_shader_file(shader_path)
        detection = detect_shader_format(data, capture_encoding)
        if detection.format is None:
            raise ToolchainRoutingError("captured shader format could not be detected")
        selection = self.select_round_trip(
            detection.format,
            stage,
            editable_format=editable_format,
            capture_encoding=capture_encoding or detection.format,
            target_api=target_api,
            allow_reconstructed=allow_reconstructed,
        )
        profile = None
        if detection.format in {"dxbc", "dxil"}:
            from .profiles import extract_target_profile

            profile = extract_target_profile(data, stage).as_dict()
        if any(
            "target_profile"
            in self.registry.operation(item["toolId"], item["operation"]).placeholders
            for item in selection["capabilities"]
        ) and profile is None:
            raise ToolchainRoutingError(
                "the automatic route needs a compiler target profile that cannot be "
                "derived from this shader format"
            )
        selection["inputDetection"] = detection.as_dict()
        selection["targetProfile"] = profile
        return selection

    def plan_recovery_for_file(
        self,
        shader_path: str | Path,
        stage: str,
        *,
        editable_format: str | None = None,
        capture_encoding: str | None = None,
        target_api: str | None = None,
        allow_reconstructed: bool = True,
    ) -> dict[str, Any]:
        """Detect one file and return a non-mutating recovery plan."""

        source, data = _read_shader_file(shader_path)
        detection = detect_shader_format(data, capture_encoding)
        if detection.format is None:
            raise ToolchainRoutingError("captured shader format could not be detected")
        if editable_format is None or not str(editable_format).strip():
            plan = self.plan_automatic_recovery(
                detection.format,
                stage,
                capture_encoding=capture_encoding or detection.format,
                target_api=target_api,
                allow_reconstructed=allow_reconstructed,
            )
        else:
            plan = self.plan_recovery(
                detection.format,
                stage,
                editable_format=editable_format,
                capture_encoding=capture_encoding or detection.format,
                target_api=target_api,
                allow_reconstructed=allow_reconstructed,
            )
        profile = None
        if detection.format in {"dxbc", "dxil"}:
            from .profiles import extract_target_profile

            profile = extract_target_profile(data, stage).as_dict()
        plan.update(
            {
                "inputPath": str(source),
                "inputDetection": detection.as_dict(),
                "inputRawSha256": hashlib.sha256(data).hexdigest(),
                "targetProfile": profile,
            }
        )
        return plan

    def diagnostic_matrix(self, stage: str) -> dict[str, Any]:
        normalized_stage = normalize_stage(stage)
        if normalized_stage not in STAGES or normalized_stage == "*":
            raise ToolchainRoutingError("a concrete supported shader stage is required")
        integrity = self._integrity()
        rows = []
        known_pairs = {(source, target) for source, target, unused in _KNOWN_MATRIX}
        all_pairs = list(_KNOWN_MATRIX)
        for route in self._routes:
            pair = (route.input_format, route.output_format)
            if pair not in known_pairs:
                all_pairs.append((pair[0], pair[1], "Custom configured format edge."))
                known_pairs.add(pair)
        for source, target, boundary in all_pairs:
            configured = [
                route
                for route in self._routes
                if route.input_format == source
                and route.output_format == target
                and route.supports_stage(normalized_stage)
            ]
            ready = [
                route
                for route in configured
                if integrity[(route.tool_id, route.operation)]["available"]
            ]
            reverse_ready = [
                route
                for route in self._routes
                if route.input_format == target
                and route.output_format == source
                and route.supports_stage(normalized_stage)
                and integrity[(route.tool_id, route.operation)]["available"]
            ]
            round_trip_ready = bool(ready and reverse_ready)
            rows.append(
                {
                    "inputFormat": source,
                    "outputFormat": target,
                    "stage": normalized_stage,
                    "status": "ready" if ready else ("integrity-failed" if configured else "missing"),
                    "configuredRouteIds": [item.route_id for item in configured],
                    "readyRouteIds": [item.route_id for item in ready],
                    "roundTripReady": round_trip_ready,
                    "reverseRouteIds": [item.route_id for item in reverse_ready],
                    "readyValidatorIds": [
                        item.validator_id
                        for item in self.matching_validators(target, normalized_stage)
                    ],
                    "containsReconstructedSource": any(item.is_reconstructed for item in ready),
                    "boundary": boundary,
                    "lane": (
                        "executable-editable"
                        if round_trip_ready
                        and target
                        in {
                            "dxbc-asm",
                            "dxil-asm",
                            "spirv-asm",
                            "opengl-spirv-asm",
                        }
                        else (
                            "explanation"
                            if target
                            in {
                                "dxbc-asm",
                                "dxil-asm",
                                "spirv-asm",
                                "opengl-spirv-asm",
                            }
                            else "candidate"
                        )
                    ),
                    "directReplayApplyAuthorized": False,
                    "replayAbiValidationStillRequired": target
                    in {"dxbc", "dxil", "spirv", "opengl-spirv"},
                }
            )
        return {
            "schema": "renderdoc-ultra-shader-toolchain-diagnostics/v1",
            "schemaVersion": 1,
            "toolRegistryPath": str(self.registry.path),
            "toolRegistrySha256": self.tool_registry_sha256,
            "routeCatalogPath": str(self.path),
            "stage": normalized_stage,
            "operations": [
                integrity[key] for key in sorted(integrity, key=lambda item: (item[0], item[1]))
            ],
            "matrix": rows,
            "guarantee": (
                "Availability and format routing only. Reconstructed source is not the original "
                "source, and successful compilation does not prove replay ABI compatibility."
            ),
        }

    @staticmethod
    def _require_process_success(result: ProcessResult) -> None:
        if result.launch_error:
            raise ToolExecutionError("external tool launch/integrity failure: %s" % result.launch_error)
        if result.timed_out:
            raise ToolExecutionError("external tool timed out")
        if result.return_code != 0:
            raise ToolExecutionError("external tool exited with code %s" % result.return_code)

    def _validate_file(
        self,
        shader_path: Path,
        shader_format: str,
        stage: str,
        run_root: Path,
        baseline_path: Path,
        target_profile: str,
        target_api: str,
        start_index: int,
    ) -> tuple[list[dict[str, Any]], int]:
        records: list[dict[str, Any]] = []
        validators = self.matching_validators(shader_format, stage)
        baseline_hash = _sha256_file(baseline_path)
        for validator in validators:
            job_dir = run_root / "jobs" / (
                "%03d-validate-%s" % (start_index, validator.validator_id)
            )
            job_dir.mkdir(parents=True, exist_ok=False)
            staged_input = job_dir / ("input" + _FORMAT_SUFFIX[shader_format])
            atomic_write_bytes(staged_input, shader_path.read_bytes())
            operation = self.registry.operation(validator.tool_id, "validate")
            result = run_operation(
                operation,
                {
                    "input": str(staged_input),
                    "output": str(job_dir / "unused-output"),
                    "baseline": str(baseline_path),
                    "job_dir": str(job_dir),
                    "workspace_dir": str(run_root),
                    "stage": stage,
                    "entry_point": "main",
                    "revision": str(start_index),
                    "capture_id": "round-trip-smoke",
                    "raw_sha256": baseline_hash,
                    "target_profile": target_profile,
                    "target_api": target_api,
                },
                job_dir,
            )
            self._require_process_success(result)
            records.append(
                {
                    "validator": validator.as_dict(),
                    "jobPath": str(job_dir),
                    "inputSha256": _sha256_file(staged_input),
                    "stdoutPath": str(result.stdout_path),
                    "stderrPath": str(result.stderr_path),
                    "passed": True,
                }
            )
            start_index += 1
        return records, start_index

    def _execute_path(
        self,
        initial_path: Path,
        initial_format: str,
        path: Sequence[RouteCapability],
        run_root: Path,
        stage: str,
        entry_point: str,
        baseline_path: Path,
        target_profile: str,
        target_api: str,
        job_index: int,
    ) -> tuple[Path, str, list[dict[str, Any]], int]:
        current_path = initial_path
        current_format = initial_format
        jobs: list[dict[str, Any]] = []
        baseline_hash = _sha256_file(baseline_path)
        for route in path:
            if route.input_format != current_format:
                raise ToolchainRoutingError("route path is not contiguous")
            integrity = self.registry.operation_integrity(route.tool_id, route.operation)
            if not integrity["available"]:
                raise ToolExecutionError(
                    "tool integrity changed before smoke execution: %s" % route.route_id
                )
            operation = self.registry.operation(route.tool_id, route.operation)
            job_dir = run_root / "jobs" / ("%03d-%s" % (job_index, route.route_id))
            job_dir.mkdir(parents=True, exist_ok=False)
            staged_input = job_dir / ("input" + _FORMAT_SUFFIX[current_format])
            atomic_write_bytes(staged_input, current_path.read_bytes())
            output_path = job_dir / ("output" + _FORMAT_SUFFIX[route.output_format])
            values = {
                "input": str(staged_input),
                "output": str(output_path),
                "baseline": str(baseline_path),
                "job_dir": str(job_dir),
                "workspace_dir": str(run_root),
                "stage": stage,
                "entry_point": entry_point,
                "revision": str(job_index),
                "capture_id": "round-trip-smoke",
                "raw_sha256": baseline_hash,
                "target_profile": target_profile,
                "target_api": target_api,
            }
            result = run_operation(operation, values, job_dir)
            self._require_process_success(result)
            if output_path.is_symlink() or not output_path.is_file():
                raise ToolExecutionError("route %s did not create its declared output" % route.route_id)
            size = output_path.stat().st_size
            if size <= 0 or size > MAX_SHADER_BYTES:
                raise ToolExecutionError(
                    "route %s output must contain 1..%d bytes" % (route.route_id, MAX_SHADER_BYTES)
                )
            # SPIR-V binary/assembly does not encode whether the consumer is
            # Vulkan or OpenGL. Preserve the route's capture-derived semantic
            # encoding while still rejecting any physically contradictory
            # output.
            detected = detect_shader_file(output_path, route.output_format)
            if detected.format != route.output_format:
                raise ToolExecutionError(
                    "route %s declared %s but output detected as %s"
                    % (route.route_id, route.output_format, detected.format or "unknown")
                )
            jobs.append(
                {
                    "route": route.as_dict(),
                    "jobPath": str(job_dir),
                    "inputSha256": _sha256_file(staged_input),
                    "outputSha256": _sha256_file(output_path),
                    "outputDetection": detected.as_dict(),
                    "stdoutPath": str(result.stdout_path),
                    "stderrPath": str(result.stderr_path),
                }
            )
            current_path = output_path
            current_format = route.output_format
            job_index += 1
        return current_path, current_format, jobs, job_index

    def round_trip_smoke(
        self,
        input_path: str | Path,
        editable_format: str,
        output_directory: str | Path,
        stage: str,
        entry_point: str = "main",
        *,
        input_format_hint: str | None = None,
        allow_reconstructed: bool = True,
        target_profile: str | None = None,
        target_api: str | None = None,
    ) -> dict[str, Any]:
        """Run binary -> editable -> binary and record format-level evidence.

        ``formatSmokePassed`` only means both fixed processes succeeded and each
        output matched its declared container/text format.  It explicitly does
        not mean the reconstructed source or final shader is semantically or ABI
        equivalent to the capture.
        """

        source, source_bytes = _read_shader_file(input_path)
        if not isinstance(entry_point, str) or not entry_point.strip():
            raise ToolchainRoutingError("entry_point must be a non-empty string")
        if len(entry_point) > 512 or "\x00" in entry_point:
            raise ToolchainRoutingError("entry_point is too large or contains NUL")
        detection = detect_shader_format(source_bytes, input_format_hint)
        if detection.format is None:
            raise ToolchainRoutingError("input shader format is unknown; supply a non-conflicting hint")
        original_format = detection.format
        editable = normalize_format(editable_format)
        normalized_stage = normalize_stage(stage)
        target_api_value = _normalize_target_api(target_api)
        _require_format_for_api(target_api_value, original_format)
        if original_format in {"spirv", "opengl-spirv"} and not target_api_value:
            raise ToolchainRoutingError(
                "SPIR-V target API/environment cannot be derived from the module; "
                "capture metadata must identify the API"
            )
        from .profiles import extract_target_profile, validate_target_profile

        target_profile_details = None
        if original_format in {"dxbc", "dxil"}:
            parsed_profile = extract_target_profile(source_bytes, normalized_stage)
            target_profile_details = parsed_profile.as_dict()
            target_profile_value = parsed_profile.profile
            if target_profile is not None:
                asserted_profile = validate_target_profile(
                    target_profile, normalized_stage
                )
                if asserted_profile != target_profile_value:
                    raise ToolchainRoutingError(
                        "caller target profile contradicts the captured executable token"
                    )
        else:
            target_profile_value = ""
            if target_profile is not None:
                raise ToolchainRoutingError(
                    "target profiles are read-only facts derived from captured "
                    "DXBC/DXIL executable tokens; this input has no such token"
                )
        if editable not in TEXT_FORMATS:
            raise ToolchainRoutingError("editable_format must be a textual shader representation")
        forward = self.find_path(
            original_format,
            editable,
            normalized_stage,
            allow_reconstructed=allow_reconstructed,
        )
        reverse = self.find_path(
            editable,
            original_format,
            normalized_stage,
            allow_reconstructed=allow_reconstructed,
        )
        if not target_profile_value and any(
            "target_profile"
            in self.registry.operation(item.tool_id, item.operation).placeholders
            for item in forward + reverse
        ):
            raise ToolchainRoutingError(
                "the selected route requires an exact target profile, but it cannot "
                "be derived from this captured executable token"
            )
        if not target_api_value and any(
            "target_api"
            in self.registry.operation(item.tool_id, item.operation).placeholders
            for item in forward + reverse
        ):
            raise ToolchainRoutingError(
                "the selected route requires the physical target API, but it was "
                "not supplied by capture metadata"
            )
        destination = Path(output_directory).expanduser().resolve()
        if destination.exists():
            raise ToolchainRoutingError("round-trip output directory already exists")
        destination.mkdir(parents=True)
        (destination / "jobs").mkdir()
        original_copy = destination / ("original" + _FORMAT_SUFFIX[original_format])
        atomic_write_bytes(original_copy, source_bytes)
        report: dict[str, Any] = {
            "schema": SMOKE_SCHEMA,
            "schemaVersion": 1,
            "inputPath": str(source),
            "inputDetection": detection.as_dict(),
            "originalSha256": _sha256_file(original_copy),
            "stage": normalized_stage,
            "entryPoint": str(entry_point),
            "targetProfile": target_profile_value or None,
            "targetProfileDetails": target_profile_details,
            "targetApi": target_api_value or None,
            "editableFormat": editable,
            "forwardRouteIds": [item.route_id for item in forward],
            "reverseRouteIds": [item.route_id for item in reverse],
            "jobs": [],
            "formatSmokePassed": False,
            "byteIdentical": False,
            "semanticEquivalenceVerified": False,
            "replayAbiValidated": False,
            "externalValidationConfigured": False,
            "externalValidationPassed": False,
            "validatorJobs": [],
            "assurance": "process-and-format-only",
            "error": None,
        }
        try:
            middle, middle_format, jobs, next_index = self._execute_path(
                original_copy,
                original_format,
                forward,
                destination,
                normalized_stage,
                str(entry_point),
                original_copy,
                target_profile_value,
                target_api_value,
                1,
            )
            final, final_format, reverse_jobs, unused_index = self._execute_path(
                middle,
                middle_format,
                reverse,
                destination,
                normalized_stage,
                str(entry_point),
                original_copy,
                target_profile_value,
                target_api_value,
                next_index,
            )
            jobs.extend(reverse_jobs)
            final_copy = destination / ("round-trip" + _FORMAT_SUFFIX[final_format])
            atomic_write_bytes(final_copy, final.read_bytes())
            validator_jobs, unused_index = self._validate_file(
                final_copy,
                final_format,
                normalized_stage,
                destination,
                original_copy,
                target_profile_value,
                target_api_value,
                unused_index,
            )
            report.update(
                {
                    "jobs": jobs,
                    "finalPath": str(final_copy),
                    "finalFormat": final_format,
                    "finalSha256": _sha256_file(final_copy),
                    "formatSmokePassed": final_format == original_format,
                    "byteIdentical": original_copy.read_bytes() == final_copy.read_bytes(),
                    "externalValidationConfigured": bool(validator_jobs),
                    "externalValidationPassed": bool(validator_jobs),
                    "validatorJobs": validator_jobs,
                }
            )
        except ShaderWorkspaceError as exc:
            report["error"] = "%s: %s" % (type(exc).__name__, exc)
        atomic_write_json(destination / "smoke-report.json", report)
        return report


def route_record(
    registry: ToolRegistry,
    *,
    route_id: str,
    tool_id: str,
    operation: str,
    input_format: str,
    output_format: str,
    stages: Iterable[str],
    fidelity: str,
    priority: int,
    implementation: str,
    source_url: str,
    license_name: str = "",
    caveats: Iterable[str] = (),
) -> dict[str, Any]:
    """Build one digest-bound route record for trusted config generators."""

    operation_record: ToolOperation = registry.operation(tool_id, operation)
    return {
        "routeId": route_id,
        "toolId": tool_id,
        "operation": operation,
        "toolConfigDigest": operation_record.config_digest,
        "inputFormat": input_format,
        "outputFormat": output_format,
        "stages": list(stages),
        "fidelity": fidelity,
        "priority": priority,
        "implementation": implementation,
        "sourceUrl": source_url,
        "license": license_name,
        "caveats": list(caveats),
    }


def validator_record(
    registry: ToolRegistry,
    *,
    validator_id: str,
    tool_id: str,
    input_format: str,
    stages: Iterable[str],
    priority: int,
    implementation: str,
    source_url: str,
    license_name: str = "",
    caveats: Iterable[str] = (),
) -> dict[str, Any]:
    operation = registry.operation(tool_id, "validate")
    return {
        "validatorId": validator_id,
        "toolId": tool_id,
        "toolConfigDigest": operation.config_digest,
        "inputFormat": input_format,
        "stages": list(stages),
        "priority": priority,
        "implementation": implementation,
        "sourceUrl": source_url,
        "license": license_name,
        "caveats": list(caveats),
    }


def toolchain_document(
    tool_config_path: str | Path,
    routes: Iterable[Mapping[str, Any]],
    validators: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Create a route document bound to the exact bytes of a tool registry."""

    path = Path(tool_config_path).expanduser().resolve()
    return {
        "schema": TOOLCHAIN_SCHEMA,
        "schemaVersion": TOOLCHAIN_VERSION,
        "toolRegistrySha256": _sha256_file(path),
        "routes": [dict(item) for item in routes],
        "validators": [dict(item) for item in validators],
    }
