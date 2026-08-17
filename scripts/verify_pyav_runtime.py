"""Verify Momento's PyAV wheel and its minimized native FFmpeg runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "build" / "pyav_runtime.json"
HASHED_DLL_RE = re.compile(r"-([0-9a-f]{8,64})(?=\.dll$)", re.IGNORECASE)
SYSTEM_DLLS = {
    "advapi32.dll", "avicap32.dll", "bcrypt.dll", "bcryptprimitives.dll", "cfgmgr32.dll",
    "combase.dll", "crypt32.dll", "d3d11.dll", "dxva2.dll", "gdi32.dll",
    "kernel32.dll", "mf.dll", "mfplat.dll", "mfreadwrite.dll", "mfuuid.dll",
    "msvcrt.dll", "ncrypt.dll", "ntdll.dll", "ole32.dll", "oleaut32.dll", "powrprof.dll",
    "secur32.dll", "shell32.dll", "shlwapi.dll", "user32.dll", "uuid.dll",
    "vcruntime140.dll", "vcruntime140_1.dll", "version.dll", "winmm.dll",
    "ws2_32.dll",
}


@dataclass(frozen=True)
class CheckResult:
    group: str
    code: str
    subject: str
    ok: bool
    detail: str = ""


@dataclass
class RuntimeReport:
    checks: list[CheckResult]

    def failures(self, *, group: str | None = None) -> list[CheckResult]:
        return [
            item for item in self.checks
            if not item.ok and (group is None or item.group == group)
        ]

    def print(self) -> None:
        for item in self.checks:
            state = "PASS" if item.ok else "FAIL"
            detail = f": {item.detail}" if item.detail else ""
            print(f"{state} [{item.group}/{item.code}] {item.subject}{detail}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_contract(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid PyAV runtime contract: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported PyAV runtime contract schema")
    required = {"runtime", "sources", "toolchain", "python_build_packages", "ffmpeg_configure", "capabilities", "native_runtime", "artifact"}
    missing = sorted(required - payload.keys())
    if missing:
        raise ValueError(f"PyAV runtime contract is missing: {', '.join(missing)}")
    return payload


def canonical_dll_name(name: str) -> str:
    return HASHED_DLL_RE.sub("", Path(name).name).lower()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rva_to_offset(data: bytes, sections: list[tuple[int, int, int, int]], rva: int) -> int:
    for virtual_address, virtual_size, raw_offset, raw_size in sections:
        span = max(virtual_size, raw_size)
        if virtual_address <= rva < virtual_address + span:
            return raw_offset + (rva - virtual_address)
    raise ValueError(f"PE RVA 0x{rva:x} is outside every section")


def pe_imports(path: Path) -> set[str]:
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("not a PE file")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise ValueError("invalid PE signature")
    coff = pe_offset + 4
    section_count = struct.unpack_from("<H", data, coff + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff + 16)[0]
    optional = coff + 20
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic == 0x20B:
        directory = optional + 112
    elif magic == 0x10B:
        directory = optional + 96
    else:
        raise ValueError(f"unsupported PE optional header 0x{magic:x}")
    sections_offset = optional + optional_size
    sections: list[tuple[int, int, int, int]] = []
    for index in range(section_count):
        entry = sections_offset + index * 40
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from("<IIII", data, entry + 8)
        sections.append((virtual_address, virtual_size, raw_offset, raw_size))

    imports: set[str] = set()
    for directory_index, descriptor_size, name_field in ((1, 20, 12), (13, 32, 4)):
        rva, size = struct.unpack_from("<II", data, directory + directory_index * 8)
        if not rva or not size:
            continue
        cursor = _rva_to_offset(data, sections, rva)
        limit = min(len(data), cursor + size)
        while cursor + descriptor_size <= limit:
            descriptor = data[cursor:cursor + descriptor_size]
            if not any(descriptor):
                break
            name_rva = struct.unpack_from("<I", descriptor, name_field)[0]
            if name_rva:
                name_offset = _rva_to_offset(data, sections, name_rva)
                end = data.find(b"\0", name_offset)
                if end < 0:
                    raise ValueError("unterminated PE import name")
                imports.add(data[name_offset:end].decode("ascii").lower())
            cursor += descriptor_size
    return imports


def _is_system_import(name: str) -> bool:
    lower = name.lower()
    return (
        lower in SYSTEM_DLLS
        or lower.startswith("api-ms-win-")
        or lower.startswith("ext-ms-win-")
        or re.fullmatch(r"python3(?:11|12)?\.dll", lower) is not None
    )


FUNCTIONAL_PROBE = r'''
import json
import os
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, os.environ['MOMENTO_PYAV_ROOT'])
checks = []
def run(code, subject, fn):
    try:
        fn()
    except Exception as exc:
        checks.append({"code": code, "subject": subject, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
    else:
        checks.append({"code": code, "subject": subject, "ok": True, "detail": ""})

import av
import numpy as np
contract = json.loads(Path(__import__('os').environ['MOMENTO_PYAV_CONTRACT']).read_text(encoding='utf-8'))

def versions():
    assert av.__version__ == contract['runtime']['pyav_version'], av.__version__
    assert av.ffmpeg_version_info == contract['runtime']['ffmpeg_version'], av.ffmpeg_version_info
    actual = {k: list(v) for k, v in av.library_versions.items()}
    assert actual == contract['runtime']['library_versions'], actual
run('versions', 'PyAV and FFmpeg versions', versions)

for mode, key in (('r', 'decoders'), ('w', 'encoders')):
    for name in contract['capabilities'][key]:
        run('codec', f'{name}:{mode}', lambda name=name, mode=mode: av.codec.Codec(name, mode))

for name in contract['capabilities']['formats']:
    run('format', name, lambda name=name: av.format.ContainerFormat(name))

for name in contract['capabilities']['filters']:
    run('filter', name, lambda name=name: av.filter.Filter(name))

for encoder, required in contract['capabilities']['encoder_options'].items():
    def options(encoder=encoder, required=required):
        actual = {item.name for item in av.codec.Codec(encoder, 'w').descriptor.options}
        assert set(required) <= actual, sorted(set(required) - actual)
    run('encoder-options', encoder, options)

def ndarray_conversion():
    frame = av.VideoFrame.from_ndarray(np.zeros((16, 16, 3), dtype=np.uint8), format='rgb24')
    assert frame.to_ndarray(format='rgb24').shape == (16, 16, 3)
run('ndarray', 'video frame ndarray conversion', ndarray_conversion)

def audio_resampling():
    frame = av.AudioFrame.from_ndarray(np.zeros((2, 1024), dtype=np.float32), format='fltp', layout='stereo')
    frame.sample_rate = 44100
    output = av.AudioResampler(format='fltp', layout='stereo', rate=48000).resample(frame)
    assert output and output[0].sample_rate == 48000
run('resample', 'audio resampling', audio_resampling)

def roundtrip(container_format, suffix):
    with tempfile.TemporaryDirectory(prefix='momento-pyav-probe-') as temp:
        path = Path(temp) / f'probe{suffix}'
        with av.open(str(path), 'w', format=container_format) as output:
            video = output.add_stream('libx264', rate=30)
            video.width = 32
            video.height = 32
            video.pix_fmt = 'yuv420p'
            video.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'crf': '28'}
            audio = output.add_stream('aac', rate=48000)
            audio.layout = 'stereo'
            for index in range(4):
                image = np.full((32, 32, 3), index * 40, dtype=np.uint8)
                frame = av.VideoFrame.from_ndarray(image, format='rgb24')
                frame.pts = index
                frame.time_base = Fraction(1, 30)
                for packet in video.encode(frame):
                    output.mux(packet)
            for index in range(8):
                samples = av.AudioFrame.from_ndarray(np.zeros((2, 1024), dtype=np.float32), format='fltp', layout='stereo')
                samples.sample_rate = 48000
                samples.pts = index * 1024
                samples.time_base = Fraction(1, 48000)
                for packet in audio.encode(samples):
                    output.mux(packet)
            for stream in (video, audio):
                for packet in stream.encode(None):
                    output.mux(packet)
        with av.open(str(path), 'r') as source:
            streams = {(item.type, item.codec_context.name) for item in source.streams}
            assert ('video', 'h264') in streams, streams
            assert ('audio', 'aac') in streams, streams
            counts = {'video': 0, 'audio': 0}
            for frame in source.decode():
                counts[frame.__class__.__name__.replace('Frame', '').lower()] += 1
            assert counts['video'] > 0 and counts['audio'] > 0, counts

run('roundtrip', 'Matroska H.264/AAC mux, demux, encode, and decode', lambda: roundtrip('matroska', '.mkv'))
run('roundtrip', 'MP4 H.264/AAC mux, demux, encode, and decode', lambda: roundtrip('mp4', '.mp4'))
print('MOMENTO_PYAV_RESULT=' + json.dumps(checks, sort_keys=True))
'''


def _functional_checks(runtime_root: Path, contract_path: Path) -> list[CheckResult]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(runtime_root)
    env["MOMENTO_PYAV_CONTRACT"] = str(contract_path.resolve())
    env["MOMENTO_PYAV_ROOT"] = str(runtime_root)
    libs = runtime_root / "av.libs"
    env["PATH"] = str(libs) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        [sys.executable, "-I", "-c", FUNCTIONAL_PROBE],
        cwd=runtime_root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
        check=False,
    )
    marker = "MOMENTO_PYAV_RESULT="
    line = next((item for item in reversed(result.stdout.splitlines()) if item.startswith(marker)), None)
    if line is None:
        return [CheckResult("functional", "probe", "isolated PyAV probe", False, result.stdout.strip())]
    payload = json.loads(line[len(marker):])
    checks = [CheckResult("functional", item["code"], item["subject"], item["ok"], item["detail"]) for item in payload]
    if result.returncode:
        checks.append(CheckResult("functional", "probe-exit", "isolated PyAV probe", False, f"exit {result.returncode}"))
    return checks


def _native_checks(runtime_root: Path, contract: dict[str, Any]) -> list[CheckResult]:
    checks: list[CheckResult] = []
    libs = runtime_root / "av.libs"
    dll_paths = sorted(libs.glob("*.dll")) if libs.is_dir() else []
    if not dll_paths:
        return [CheckResult("native", "dll-directory", "av.libs", False, "no bundled DLLs found")]

    actual = {canonical_dll_name(path.name): path for path in dll_paths}
    policy = contract["native_runtime"]
    allowed = {name.lower() for name in policy["allowed_dlls"]}
    forbidden_tokens = [token.lower() for token in policy["forbidden_tokens"]]
    for name in sorted(actual):
        token = next((item for item in forbidden_tokens if item in name), None)
        if token:
            checks.append(CheckResult("native", "forbidden-dll", f"lib{token}" if not token.startswith("lib") and token not in {"zlib"} else token, False, name))
        elif name not in allowed:
            checks.append(CheckResult("native", "unexpected-dll", name, False, "not in exact allowlist"))
        else:
            checks.append(CheckResult("native", "allowed-dll", name, True))

    for required in policy["required_dlls"]:
        checks.append(CheckResult("native", "required-dll", required, required.lower() in actual, "missing" if required.lower() not in actual else ""))

    license_root = libs / "licenses"
    for required in policy["required_license_files"]:
        present = (license_root / required).is_file()
        checks.append(CheckResult("native", "required-license", required, present, "missing" if not present else ""))

    available_names = {path.name.lower() for path in dll_paths}
    native_files = sorted((runtime_root / "av").rglob("*.pyd")) + dll_paths
    for path in native_files:
        try:
            imports = pe_imports(path)
        except (OSError, ValueError, struct.error) as exc:
            checks.append(CheckResult("native", "pe-imports", path.name, False, str(exc)))
            continue
        for imported in sorted(imports):
            if imported in available_names or _is_system_import(imported):
                continue
            checks.append(CheckResult("native", "unresolved-import", f"{path.name} -> {imported}", False))
    return checks


def verify_runtime(runtime: Path, *, contract_path: Path = DEFAULT_CONTRACT) -> RuntimeReport:
    runtime = runtime.resolve()
    contract = load_contract(contract_path)
    checks: list[CheckResult] = []
    temp: tempfile.TemporaryDirectory[str] | None = None
    try:
        if runtime.suffix.lower() == ".whl":
            expected = contract["artifact"]["sha256"].lower()
            actual = _sha256(runtime)
            hash_matches = bool(expected) and actual == expected
            checks.append(CheckResult("artifact", "wheel-hash", runtime.name, hash_matches, f"sha256={actual}"))
            if not hash_matches:
                return RuntimeReport(checks)
            temp = tempfile.TemporaryDirectory(prefix="momento-pyav-wheel-")
            with zipfile.ZipFile(runtime) as wheel:
                wheel.extractall(temp.name)
            runtime_root = Path(temp.name)
        else:
            runtime_root = runtime.parent if runtime.name.lower() == "av" else runtime
        checks.extend(_functional_checks(runtime_root, contract_path))
        checks.extend(_native_checks(runtime_root, contract))
    finally:
        if temp is not None:
            temp.cleanup()
    return RuntimeReport(checks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    args = parser.parse_args()
    report = verify_runtime(args.runtime, contract_path=args.contract)
    report.print()
    failures = report.failures()
    if failures:
        print(f"FAIL: {len(failures)} PyAV runtime contract check(s) failed")
        return 1
    print("PASS: PyAV runtime contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
