"""Which video encoder ffmpeg uses: x264 on the CPU (the default) or
NVIDIA's NVENC on the GPU (README: Videos → GPU (NVIDIA)).

    VIDEO_ENCODER=x264   the CPU, as always
    VIDEO_ENCODER=nvenc  the GPU (h264_nvenc; hevc_nvenc for an HDR
                         intermediate in the studio)
    VIDEO_ENCODER=auto   the GPU when a one-frame test encode works at
                         startup, else the CPU

The choice is made once (the test is cached) and logged. A job that fails
on the GPU is made again on the CPU (videos._encode), so a missing driver,
a busy encoder or a source NVDEC can't read never leaves a video failed.

What changes on the GPU, and what doesn't:
  * the same ladder (rungs, sizes, the source's frame rate up to 60, 4s
    keyframes aligned across rungs, per-rung bitrate ceilings, yuv420p
    8-bit High profile), encoded by h264_nvenc at constant quality
    (VIDEO_NVENC_CQ, 19 by default, about x264's CRF 18) with p7, two-pass
    lookahead and adaptive quantisation;
  * decoding and scaling stay on the GPU (NVDEC, scale_cuda) for an SDR
    source NVDEC reads, when this ffmpeg has scale_cuda; otherwise (HDR
    sources, which are tone-mapped on the CPU with zscale, other pixel
    formats, an ffmpeg without CUDA filters) the frames are filtered on the
    CPU and only the encode is on the GPU;
  * one GPU job at a time (`slot`): a consumer card has a few encoder
    sessions and the ladder uses one per rung."""
import re
import subprocess
import threading

from .config import VIDEO_ENCODER, VIDEO_NVENC_CQ, VIDEO_NVENC_PRESET

ENCODER = VIDEO_ENCODER
if ENCODER not in ("x264", "nvenc", "auto"):
    print(f"videos: VIDEO_ENCODER={ENCODER!r} isn't x264, nvenc or auto; using x264")
    ENCODER = "x264"
NVENC_CQ = VIDEO_NVENC_CQ
NVENC_PRESET = VIDEO_NVENC_PRESET

_lock = threading.Lock()
_choice: str | None = None
_filters: str | None = None
# One GPU encode at a time (the transcode worker and the studio's worker
# would otherwise open 12+ sessions at once).
slot = threading.Lock()

PROBE = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "nullsrc=s=256x256", "-t", "0.1",
         "-c:v", "h264_nvenc", "-f", "null", "-"]


def _probe() -> tuple[bool, str]:
    try:
        p = subprocess.run(PROBE, capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)
    if p.returncode == 0:
        return True, ""
    lines = [l.strip() for l in (p.stderr or "").splitlines() if l.strip()]
    # The line that says why ("Cannot load libcuda.so.1", "No capable devices found").
    why = next((l for l in lines if re.search(r"nvenc|cuda|device|driver", l, re.I) and "Error while" not in l), None)
    return False, (why or (lines[-1] if lines else f"ffmpeg exited {p.returncode}"))


def choice() -> str:
    """'nvenc' or 'x264', decided once."""
    global _choice
    with _lock:
        if _choice is not None:
            return _choice
        if ENCODER == "x264":
            _choice = "x264"
            print("videos: encoding with x264 (CPU)")
        elif ENCODER == "nvenc":
            _choice = "nvenc"
            ok, why = _probe()
            print("videos: encoding with NVENC (GPU)" + ("" if ok else
                  f"; the test encode failed ({why}), so each job will fall back to x264"))
        else:
            ok, why = _probe()
            _choice = "nvenc" if ok else "x264"
            print("videos: VIDEO_ENCODER=auto: " + ("NVENC works, encoding on the GPU" if ok else
                  f"no NVENC here ({why}), encoding with x264"))
        return _choice


def has_filter(name: str) -> bool:
    global _filters
    if _filters is None:
        try:
            _filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True,
                                      timeout=30).stdout or ""
        except (subprocess.SubprocessError, OSError):
            _filters = ""
    return bool(re.search(rf"\s{re.escape(name)}\s", _filters))


# What NVDEC (Turing/Ada) decodes: 4:2:0, H.264 8-bit only, HEVC/VP9/AV1
# 8 or 10-bit. Anything else is decoded on the CPU.
_NVDEC_FMTS = {"h264": {"yuv420p", "yuvj420p", "nv12"},
               "hevc": {"yuv420p", "yuvj420p", "nv12", "yuv420p10le", "p010le"},
               "vp9": {"yuv420p", "yuv420p10le"},
               "av1": {"yuv420p", "yuv420p10le"}}


def gpu_decodes(info: dict) -> bool:
    return (info.get("pix_fmt") or "") in _NVDEC_FMTS.get(info.get("vcodec") or "", set())


def gpu_ladder(info: dict) -> bool:
    """Decode and scale the ladder on the GPU too (no CPU filters needed)."""
    return gpu_decodes(info) and not info.get("hdr") and has_filter("scale_cuda")


def nvenc_ladder_args(fps: float, segment: int) -> list[str]:
    """h264_nvenc for the ladder: constant quality, capped per rung by the
    caller's -maxrate/-bufsize, an IDR every `segment` seconds on the same
    frames in every rung."""
    g = max(1, round(fps * segment))
    return ["-c:v", "h264_nvenc", "-preset", NVENC_PRESET, "-tune", "hq", "-profile:v", "high",
            "-rc", "vbr", "-cq", str(NVENC_CQ), "-b:v", "0",
            "-multipass", "fullres", "-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "32",
            "-bf", "3", "-b_ref_mode", "middle",
            "-g", str(g), "-keyint_min", str(g), "-strict_gop", "1", "-no-scenecut", "1", "-forced-idr", "1",
            "-force_key_frames", f"expr:gte(t,n_forced*{segment})"]


def nvenc_master_args(cq: int, hdr: bool) -> list[str]:
    """A near-lossless master (the studio's intermediate, a clip's cut):
    H.264 High, or HEVC Main10 when an HDR source stays HDR."""
    codec, profile = ("hevc_nvenc", "main10") if hdr else ("h264_nvenc", "high")
    args = ["-c:v", codec, "-preset", NVENC_PRESET, "-tune", "hq", "-profile:v", profile,
            "-rc", "vbr", "-cq", str(cq), "-b:v", "0",
            "-multipass", "fullres", "-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "32",
            "-bf", "3", "-b_ref_mode", "middle"]
    if hdr:
        args += ["-tag:v", "hvc1"]
    return args


def uses_gpu(cmd: list[str]) -> bool:
    return any(a in ("h264_nvenc", "hevc_nvenc", "av1_nvenc") for a in cmd)
