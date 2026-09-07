from __future__ import annotations

import copy
import io
import json
import re
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
try:
    from concurrent.futures.process import BrokenProcessPool
except Exception:  # pragma: no cover
    BrokenProcessPool = RuntimeError  # type: ignore
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from multiprocessing import get_context

import numpy as np
import requests
from PIL import Image, ImageGrab, ImageOps
from PySide6.QtCore import QThread, Signal

import difflib
import psutil
import win32gui
import win32process
import win32ui
import ctypes

import win32api

from ctypes import windll, wintypes
from dataclasses import dataclass

from multiscope import APP_FOOTER

# -----------------------------
# Frame similarity helpers
# -----------------------------

_FRAME_HASH_SIZE = 16  # 16x16 = 256-bit perceptual hash (fast, robust to minor noise)
_WINDOW_ENUM_INTERVAL_SECONDS = 1.0
_OCR_INFERENCE_SIZE = (1024, 512)
_OCR_EMPTY_COLOR_MASK_PIXELS = 0
_OCR_COMBINED_COLUMNS = 5
_OCR_COMBINED_GUTTER = 8


def _normalize_ocr_processing_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"combined", "combined_batch", "batch", "tiled"}:
        return "combined"
    return "separate"


def _chunk_combined_ocr_items(
    entries: List[Dict[str, Any]],
    metadata: List[Dict[str, Any]],
    max_images: int,
) -> List[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """Split OCR inputs and their routing metadata into aligned composites."""
    if len(entries) != len(metadata):
        raise ValueError("Combined OCR entries and metadata must have the same length")
    limit = max(1, int(max_images or 1))
    return [
        (entries[start : start + limit], metadata[start : start + limit])
        for start in range(0, len(entries), limit)
    ]


def compute_frame_hash(image: Image.Image, hash_size: int = _FRAME_HASH_SIZE) -> int:
    """
    Compute a compact perceptual hash (average-hash) for quickly comparing frames.

    Returns a Python int whose bits represent the hash (hash_size*hash_size bits).
    """
    if hash_size <= 0:
        raise ValueError("hash_size must be > 0")

    gray = image if image.mode == "L" else image.convert("L")
    small = gray.resize((hash_size, hash_size), Image.BILINEAR)
    arr = np.asarray(small, dtype=np.float32)
    avg = float(arr.mean()) if arr.size else 0.0
    bits = (arr > avg).astype(np.uint8).reshape(-1)

    packed = np.packbits(bits)
    return int.from_bytes(packed.tobytes(), byteorder="big", signed=False)


def frame_hash_diff_percent(hash_a: int, hash_b: int, hash_size: int = _FRAME_HASH_SIZE) -> float:
    """Return the percent of bits that differ between two frame hashes (0..100)."""
    bits = int(hash_size) * int(hash_size)
    if bits <= 0:
        return 100.0
    dist = (int(hash_a) ^ int(hash_b)).bit_count()
    return (float(dist) / float(bits)) * 100.0


def _format_step_duration(seconds: float) -> str:
    try:
        value = max(0.0, float(seconds))
    except Exception:
        value = 0.0
    if value < 1.0:
        return f"{value * 1000.0:.1f}ms"
    return f"{value:.3f}s"

# RapidOCR import with fallback search paths (helps when the frozen EXE missed the package)
RapidOCR = None  # type: ignore
ort = None  # type: ignore
_RAPIDOCR_IMPORT_ERROR = None
_ORT_IMPORT_ERROR = None
_OCR_DEVICE_SUMMARY: Optional[str] = None
_OCR_ENGINE_REF: Any = None
_OCR_DEVICE_ID: Optional[int] = None
_RAPIDOCR_NATIVE_LOCK = threading.RLock()


def _hresult_code(value: int) -> int:
    return value & 0xFFFFFFFF


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", wintypes.WCHAR * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", wintypes.UINT),
    ]


_DXGI_ERROR_NOT_FOUND = 0x887A0002
_DXGI_ADAPTER_FLAG_SOFTWARE = 0x2
_IID_IDXGIFactory1 = _GUID(
    0x770AAE78, 0xF26F, 0x4DBA, (0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87)
)


def _com_method(instance: ctypes.c_void_p, index: int, restype, argtypes):
    vtbl = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    func_type = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return func_type(vtbl[index])


def _release_com(instance: ctypes.c_void_p) -> None:
    if not instance:
        return
    try:
        release = _com_method(instance, 2, wintypes.ULONG, [])
        release(instance)
    except Exception:
        pass


def _dxgi_enum_adapters() -> List[Dict[str, Any]]:
    adapters: List[Dict[str, Any]] = []
    try:
        dxgi = ctypes.windll.dxgi
    except Exception:
        return adapters

    factory = ctypes.c_void_p()
    try:
        create_factory = dxgi.CreateDXGIFactory1
        create_factory.argtypes = [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
        create_factory.restype = wintypes.HRESULT
        hr = create_factory(ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory))
    except Exception:
        return adapters
    if hr != 0 or not factory:
        return adapters

    try:
        enum_adapters1 = _com_method(
            factory,
            12,
            wintypes.HRESULT,
            [wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)],
        )
        idx = 0
        while True:
            adapter = ctypes.c_void_p()
            hr = enum_adapters1(factory, idx, ctypes.byref(adapter))
            if _hresult_code(hr) == _DXGI_ERROR_NOT_FOUND:
                break
            if hr != 0 or not adapter:
                break
            try:
                get_desc1 = _com_method(
                    adapter, 10, wintypes.HRESULT, [ctypes.POINTER(_DXGI_ADAPTER_DESC1)]
                )
                desc = _DXGI_ADAPTER_DESC1()
                if get_desc1(adapter, ctypes.byref(desc)) == 0:
                    name = str(desc.Description).rstrip("\x00").strip()
                    if name and not (int(desc.Flags) & _DXGI_ADAPTER_FLAG_SOFTWARE):
                        adapters.append({"id": idx, "name": name})
            finally:
                _release_com(adapter)
            idx += 1
    finally:
        _release_com(factory)
    return adapters


def _enumerate_display_adapters_fallback() -> List[Dict[str, Any]]:
    if win32api is None:
        return []
    adapters: List[Dict[str, Any]] = []
    idx = 0
    while True:
        try:
            dev = win32api.EnumDisplayDevices(None, idx)
        except Exception:
            break
        name = (getattr(dev, "DeviceString", "") or "").strip()
        if name:
            adapters.append({"id": idx, "name": name})
        idx += 1
    return adapters


def _unique_by_name(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    unique: List[Dict[str, Any]] = []
    for item in items:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def get_ocr_available_devices() -> List[Tuple[int, str]]:
    adapters = _dxgi_enum_adapters()
    if not adapters:
        adapters = _unique_by_name(_enumerate_display_adapters_fallback())
    return [(int(d["id"]), str(d["name"])) for d in adapters]


def _get_gpu_name_for_device(device_id: int) -> Optional[str]:
    adapters = _dxgi_enum_adapters()
    if not adapters:
        adapters = _enumerate_display_adapters_fallback()
    for item in adapters:
        if int(item.get("id", -1)) == int(device_id):
            return str(item.get("name", "")).strip() or None
    return None


def _format_provider_summary(provider: str, device_id: Optional[int]) -> str:
    if provider in ("DmlExecutionProvider", "CUDAExecutionProvider"):
        base = "GPU"
    elif provider == "CPUExecutionProvider":
        return "CPU"
    else:
        return provider

    if device_id is None:
        device_id = 0
    name = _get_gpu_name_for_device(device_id)
    if name:
        return f"{name} ({base} {device_id})"
    return base


def _collect_provider_lists(obj: Any, max_depth: int = 3) -> List[List[str]]:
    providers_list: List[List[str]] = []
    seen: set[int] = set()

    def _walk(value: Any, depth: int) -> None:
        if value is None:
            return
        obj_id = id(value)
        if obj_id in seen:
            return
        seen.add(obj_id)

        get_providers = getattr(value, "get_providers", None)
        if callable(get_providers):
            try:
                providers = list(get_providers())
            except Exception:
                providers = []
            if providers:
                providers_list.append(providers)

        if depth >= max_depth:
            return

        if isinstance(value, dict):
            for v in value.values():
                _walk(v, depth + 1)
            return
        if isinstance(value, (list, tuple, set)):
            for v in value:
                _walk(v, depth + 1)
            return

        try:
            obj_dict = vars(value)
        except Exception:
            slots = getattr(value, "__slots__", None)
            if slots:
                for slot in slots:
                    try:
                        _walk(getattr(value, slot), depth + 1)
                    except Exception:
                        continue
            return
        for v in obj_dict.values():
            _walk(v, depth + 1)

    _walk(obj, 0)
    return providers_list


def _summarize_device_from_engine(
    engine: Any, fallback_providers: List[str], device_id: Optional[int]
) -> str:
    provider_lists = _collect_provider_lists(engine)
    if provider_lists:
        primaries: List[str] = []
        for providers in provider_lists:
            if providers:
                primaries.append(providers[0])
        if primaries:
            unique: List[str] = []
            for provider in primaries:
                if provider not in unique:
                    unique.append(provider)
            if len(unique) == 1:
                return _format_provider_summary(unique[0], device_id)
            readable = ", ".join(_format_provider_summary(p, device_id) for p in unique)
            return f"Mixed ({readable})"
    if fallback_providers:
        return f"Unknown (sessions not inspected; available: {', '.join(fallback_providers)})"
    return "Unknown (OCR not initialized)"


def get_ocr_device_summary() -> str:
    """
    Return a short human-readable summary of the device used for OCR.
    """
    if RapidOCR is None or ort is None:
        return "Unavailable (RapidOCR/ONNX Runtime not installed)"
    global _OCR_DEVICE_SUMMARY
    if _OCR_ENGINE_REF is not None:
        if not _OCR_DEVICE_SUMMARY or _OCR_DEVICE_SUMMARY.startswith("Unknown"):
            _OCR_DEVICE_SUMMARY = _summarize_device_from_engine(
                _OCR_ENGINE_REF, _get_ort_providers(), _OCR_DEVICE_ID
            )
        if _OCR_DEVICE_SUMMARY:
            return _OCR_DEVICE_SUMMARY
    if _OCR_DEVICE_SUMMARY:
        return _OCR_DEVICE_SUMMARY
    return "Unknown (OCR engine not initialized)"


def _add_site_packages_paths() -> None:
    """Add common site-packages locations to sys.path (best-effort)."""
    import sys
    import sysconfig
    from pathlib import Path

    candidates = []

    # Explicit site-packages locations (purelib/platlib) and base_prefix/Lib/site-packages
    for key in ("purelib", "platlib"):
        try:
            p = sysconfig.get_path(key)
            if p:
                candidates.append(Path(p))
        except Exception:
            pass
    try:
        candidates.append(Path(sys.base_prefix) / "Lib" / "site-packages")
    except Exception:
        pass
    # Explicit Windows install path under LOCALAPPDATA\Programs\Python\PythonXY\Lib\site-packages
    try:
        import os
        la = os.environ.get("LOCALAPPDATA", "")
        if la:
            pyver = f"Python{sys.version_info.major}{sys.version_info.minor}"
            candidates.append(Path(la) / "Programs" / "Python" / pyver / "Lib" / "site-packages")
    except Exception:
        pass

    seen = set()
    for base in candidates:
        if not base:
            continue
        if base in seen:
            continue
        seen.add(base)
        if str(base) not in sys.path:
            sys.path.insert(0, str(base))


def _try_import_rapidocr():
    """Try to import rapidocr from standard site-packages locations only."""
    import importlib

    _add_site_packages_paths()
    try:
        module = importlib.import_module("rapidocr")  # type: ignore
    except Exception:
        return None
    return getattr(module, "RapidOCR", None)


def _try_import_onnxruntime():
    """Try to import onnxruntime from standard site-packages locations only."""
    import importlib

    _add_site_packages_paths()
    try:
        return importlib.import_module("onnxruntime")  # type: ignore
    except Exception:
        return None


try:
    from rapidocr import RapidOCR  # type: ignore
except Exception as _rapid_err:  # pragma: no cover - environment dependent
    RapidOCR = _try_import_rapidocr()  # type: ignore
    _RAPIDOCR_IMPORT_ERROR = _rapid_err if RapidOCR is None else None
else:
    _RAPIDOCR_IMPORT_ERROR = None

try:
    import onnxruntime as ort  # type: ignore
except Exception as _ort_err:  # pragma: no cover - environment dependent
    ort = _try_import_onnxruntime()  # type: ignore
    _ORT_IMPORT_ERROR = _ort_err if ort is None else None
else:
    _ORT_IMPORT_ERROR = None


def _get_ort_providers() -> List[str]:
    if ort is None:
        return []
    try:
        return ort.get_available_providers()  # type: ignore[attr-defined]
    except Exception:
        return []


def _ensure_rapidocr_dml_options_are_plain_dict() -> None:
    """Keep RapidOCR 3.x DirectML options compatible with ONNX Runtime."""
    try:
        from rapidocr.inference_engine.onnxruntime.provider_config import (  # type: ignore
            ProviderConfig,
        )
    except Exception:
        return

    current = ProviderConfig.dml_ep_cfg
    if getattr(current, "_jaram_plain_dict", False):
        return

    def _dml_ep_cfg(self):  # type: ignore[no-untyped-def]
        # RapidOCR 3.9 returns an OmegaConf DictConfig when dml_ep_cfg is
        # explicitly configured. ONNX Runtime requires a real dict and
        # otherwise silently retries the sessions on CPU.
        options = current(self)
        return dict(options or {})

    _dml_ep_cfg._jaram_plain_dict = True  # type: ignore[attr-defined]
    ProviderConfig.dml_ep_cfg = _dml_ep_cfg


def _init_rapidocr_engine(
    require_dml: bool = True,
    device_id: Optional[int] = None,
    force_cpu: bool = False,
):
    global _OCR_DEVICE_SUMMARY, _OCR_ENGINE_REF, _OCR_DEVICE_ID
    if RapidOCR is None:
        raise RuntimeError("rapidocr is not available.")
    if ort is None:
        raise RuntimeError("onnxruntime is not available.")

    # DirectML-backed ONNX Runtime has crashed in native code when different
    # app threads entered RapidOCR sessions at the same time.
    with _RAPIDOCR_NATIVE_LOCK:
        if force_cpu:
            require_dml = False
        _OCR_DEVICE_ID = None if force_cpu else device_id
        providers = _get_ort_providers()
        dml_available = "DmlExecutionProvider" in providers
        if require_dml and not dml_available:
            _OCR_DEVICE_SUMMARY = "Unavailable (DirectML provider not available)"
            raise RuntimeError("DirectML provider is not available (install onnxruntime-directml).")

        use_dml = dml_available and not force_cpu
        rapidocr_params: Dict[str, Any] = {
            # Empty OCR results are expected while polling frames, so keep
            # RapidOCR's per-call INFO/WARNING output out of the terminal.
            "Global.log_level": "critical",
            "Global.use_cls": False,
            "EngineConfig.onnxruntime.use_dml": use_dml,
            "EngineConfig.onnxruntime.use_cuda": False,
        }
        if use_dml and device_id is not None:
            rapidocr_params["EngineConfig.onnxruntime.dml_ep_cfg"] = {
                # ONNX Runtime provider option values are strings, including
                # numeric options such as the DirectML adapter index.
                "device_id": str(int(device_id))
            }

        if use_dml:
            _ensure_rapidocr_dml_options_are_plain_dict()
        engine = RapidOCR(params=rapidocr_params)
        if force_cpu:
            _OCR_DEVICE_SUMMARY = "CPU"
        else:
            _OCR_DEVICE_SUMMARY = _summarize_device_from_engine(engine, providers, device_id)
        _OCR_ENGINE_REF = engine
        return engine


def _rapidocr_text_only(engine, img_np: np.ndarray) -> str:
    """Run RapidOCR and return text only (one line per detection)."""
    with _RAPIDOCR_NATIVE_LOCK:
        ocr_result = engine(img_np)

    texts = getattr(ocr_result, "txts", None)
    if not texts:
        return ""
    return "\n".join(str(text) for text in texts if text)


_POOL_ENGINE = None


@dataclass
class PreparedOCRImage:
    image: Image.Image
    has_color_filters: bool = False
    mask_pixels: int = 0
    total_pixels: int = 0


def _init_pool_reader(device_id: Optional[int] = None, force_cpu: bool = False) -> None:
    """Initializer for OCR process workers so RapidOCR is created once per process."""
    global _POOL_ENGINE
    if _POOL_ENGINE is None:
        _POOL_ENGINE = _init_rapidocr_engine(
            require_dml=not force_cpu,
            device_id=device_id,
            force_cpu=force_cpu,
        )


def _image_payload(img: Image.Image) -> Tuple[str, Tuple[int, int], bytes]:
    mode = img.mode if img.mode in ("L", "RGB", "RGBA") else "RGB"
    work = img if img.mode == mode else img.convert(mode)
    return mode, work.size, work.tobytes()


def _image_from_payload(payload: Tuple[str, Tuple[int, int], bytes]) -> Image.Image:
    mode, size, data = payload
    return Image.frombytes(str(mode), tuple(size), data)


def _ocr_input_array(image: Image.Image) -> np.ndarray:
    img = image.convert("RGB")
    target_w, target_h = _OCR_INFERENCE_SIZE
    if img.size != _OCR_INFERENCE_SIZE:
        w, h = img.size
        if w <= 0 or h <= 0:
            img = Image.new("RGB", _OCR_INFERENCE_SIZE, "white")
        else:
            scale = min(float(target_w) / float(w), float(target_h) / float(h))
            new_size = (
                max(1, min(target_w, int(round(w * scale)))),
                max(1, min(target_h, int(round(h * scale)))),
            )
            resized = img.resize(new_size, Image.BILINEAR)
            canvas = Image.new("RGB", _OCR_INFERENCE_SIZE, "white")
            canvas.paste(resized, ((target_w - new_size[0]) // 2, (target_h - new_size[1]) // 2))
            img = canvas
    return np.asarray(img, dtype=np.uint8)


def _pool_read_text(img_payload: Tuple[str, Tuple[int, int], bytes]) -> str:
    """Read text from a preprocessed image inside a process worker."""
    global _POOL_ENGINE
    if _POOL_ENGINE is None:
        _init_pool_reader()
    img = _image_from_payload(img_payload)
    return _rapidocr_text_only(_POOL_ENGINE, _ocr_input_array(img))


def _tile_ocr_input_arrays(
    images: List[np.ndarray],
    *,
    columns: int = _OCR_COMBINED_COLUMNS,
    gutter: int = _OCR_COMBINED_GUTTER,
) -> Tuple[np.ndarray, List[Tuple[int, int, int, int]]]:
    """Tile normalized OCR inputs and return each tile's bounds."""
    if not images:
        raise ValueError("At least one OCR image is required")

    tile_w, tile_h = _OCR_INFERENCE_SIZE
    columns = max(1, min(int(columns or 1), len(images)))
    rows = (len(images) + columns - 1) // columns
    gutter = max(0, int(gutter or 0))
    width = columns * tile_w + max(0, columns - 1) * gutter
    height = rows * tile_h + max(0, rows - 1) * gutter
    tiled = np.full((height, width, 3), 255, dtype=np.uint8)
    bounds: List[Tuple[int, int, int, int]] = []

    for index, image in enumerate(images):
        row, column = divmod(index, columns)
        x0 = column * (tile_w + gutter)
        y0 = row * (tile_h + gutter)
        x1 = x0 + tile_w
        y1 = y0 + tile_h
        array = np.asarray(image, dtype=np.uint8)
        if array.shape != (tile_h, tile_w, 3):
            raise ValueError(
                f"Combined OCR input {index} has shape {array.shape}; "
                f"expected {(tile_h, tile_w, 3)}"
            )
        tiled[y0:y1, x0:x1] = array
        bounds.append((x0, y0, x1, y1))
    return tiled, bounds


def _rapidocr_texts_by_tile(
    engine: Any,
    images: List[np.ndarray],
    *,
    columns: int = _OCR_COMBINED_COLUMNS,
    gutter: int = _OCR_COMBINED_GUTTER,
) -> List[str]:
    """Run RapidOCR once and route detected text back to its source tile."""
    if not images:
        return []
    if len(images) == 1:
        return [_rapidocr_text_only(engine, images[0])]

    tiled, bounds = _tile_ocr_input_arrays(images, columns=columns, gutter=gutter)
    with _RAPIDOCR_NATIVE_LOCK:
        ocr_result = engine(tiled)

    texts = list(getattr(ocr_result, "txts", None) or ())
    if not texts:
        return [""] * len(images)
    boxes = getattr(ocr_result, "boxes", None)
    if boxes is None or len(boxes) != len(texts):
        raise RuntimeError("RapidOCR combined output did not include one box per text detection")

    routed: List[List[str]] = [[] for _ in images]
    for text, box in zip(texts, boxes):
        if not text:
            continue
        points = np.asarray(box, dtype=np.float64).reshape(-1, 2)
        if points.size == 0 or not np.isfinite(points).all():
            raise RuntimeError("RapidOCR combined output included an invalid text box")
        center_x = float(points[:, 0].mean())
        center_y = float(points[:, 1].mean())

        tile_index = None
        for index, (x0, y0, x1, y1) in enumerate(bounds):
            if x0 <= center_x < x1 and y0 <= center_y < y1:
                tile_index = index
                break
        if tile_index is None:
            raise RuntimeError("RapidOCR combined text box fell outside every source tile")
        routed[tile_index].append(str(text))

    return ["\n".join(lines) for lines in routed]


def _pool_read_texts_combined(
    img_payloads: List[Tuple[str, Tuple[int, int], bytes]],
) -> List[str]:
    """Read multiple images in one tiled RapidOCR detector call."""
    global _POOL_ENGINE
    if _POOL_ENGINE is None:
        _init_pool_reader()
    inputs = [_ocr_input_array(_image_from_payload(payload)) for payload in img_payloads]
    return _rapidocr_texts_by_tile(_POOL_ENGINE, inputs)


def _ocr_text_task(img_payload: Tuple[str, Tuple[int, int], bytes]) -> Dict[str, Any]:
    try:
        return {"text": _pool_read_text(img_payload)}
    except Exception as e:
        return {"error": f"{e.__class__.__name__}: {e}", "text": ""}


def _ocr_pool_warmup_task() -> Dict[str, Any]:
    try:
        global _POOL_ENGINE
        if _POOL_ENGINE is None:
            _init_pool_reader()
        return {
            "ok": True,
            "providers": _get_ort_providers(),
            "device": get_ocr_device_summary(),
        }
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


def _prepare_filter_ocr_image_with_stats(
    image: Image.Image,
    filter_specs: List[Dict[str, Any]],
) -> PreparedOCRImage:
    colors = [_filter_color_from_spec(spec) for spec in (filter_specs or []) if bool((spec or {}).get("enabled", True))]
    if not colors:
        return preprocess_for_ocr_with_stats(image, [])

    try:
        w, h = image.size
        work = image.convert("RGB")
        if max(w, h) > 800:
            scale = 800.0 / float(max(w, h))
            work = work.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)
        return preprocess_for_ocr_with_stats(work, colors)
    except Exception as e:
        raise RuntimeError(f"OCR preprocessing failed: {e}") from e


def _prepare_filter_ocr_image(image: Image.Image, filter_specs: List[Dict[str, Any]]) -> Image.Image:
    return _prepare_filter_ocr_image_with_stats(image, filter_specs).image


def _prepared_has_empty_color_mask(prepared: PreparedOCRImage) -> bool:
    return bool(prepared.has_color_filters and prepared.mask_pixels <= _OCR_EMPTY_COLOR_MASK_PIXELS)


def _ocr_pool_task(
    preprocessed_payload: Tuple[str, Tuple[int, int], bytes],
    raw_payload: Tuple[str, Tuple[int, int], bytes],
    filters: List[Dict[str, Any]],
    use_preprocess: bool,
) -> Dict[str, Any]:
    """
    Run OCR inside a process worker and return all verified filter matches.
    """
    try:
        broad_payload = preprocessed_payload if use_preprocess else raw_payload
        text = _pool_read_text(broad_payload)
        ranked = _rank_filter_candidates(text, filters)
        if not ranked:
            return {"matches": [], "text": text}

        raw_img = _image_from_payload(raw_payload).convert("RGB")
        matches: List[Dict[str, Any]] = []
        for score, spec in ranked:
            if use_preprocess:
                verify_prepared = _prepare_filter_ocr_image_with_stats(raw_img, [spec])
                if _prepared_has_empty_color_mask(verify_prepared):
                    continue
                verify_text = _pool_read_text(_image_payload(verify_prepared.image))
            else:
                verify_text = text
            verify_score = _score_text_against_target(verify_text, str(spec.get("target_text", "") or ""))
            if verify_score >= DEFAULT_OCR_MATCH_THRESHOLD:
                matches.append(
                    {
                        "id": str(spec.get("id") or ""),
                        "name": str(spec.get("name") or ""),
                        "behavior": str(spec.get("behavior") or ""),
                        "cooldown_group": str(spec.get("cooldown_group") or ""),
                        "score": float(score),
                        "verify_score": float(verify_score),
                    }
                )

        return {"matches": matches, "text": text}
    except Exception as e:
        return {"matches": [], "error": f"{e.__class__.__name__}: {e}"}


def _ocr_combined_pool_task(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run each entry's broad OCR read as one tiled call, then verify matches normally."""
    if not entries:
        return {"results": []}

    try:
        broad_payloads = [
            entry["preprocessed_payload"]
            if bool(entry.get("use_preprocess", True))
            else entry["raw_payload"]
            for entry in entries
        ]
        broad_texts = _pool_read_texts_combined(broad_payloads)
        if len(broad_texts) != len(entries):
            raise RuntimeError("Combined OCR returned an unexpected number of tile results")
    except Exception as e:
        fallback_results = [
            _ocr_pool_task(
                entry["preprocessed_payload"],
                entry["raw_payload"],
                list(entry.get("filters") or []),
                bool(entry.get("use_preprocess", True)),
            )
            for entry in entries
        ]
        return {
            "results": fallback_results,
            "fallback_error": f"{e.__class__.__name__}: {e}",
        }

    results: List[Dict[str, Any]] = []
    for entry, text in zip(entries, broad_texts):
        try:
            filters = list(entry.get("filters") or [])
            use_preprocess = bool(entry.get("use_preprocess", True))
            ranked = _rank_filter_candidates(text, filters)
            if not ranked:
                results.append({"matches": [], "text": text})
                continue

            raw_img = _image_from_payload(entry["raw_payload"]).convert("RGB")
            matches: List[Dict[str, Any]] = []
            for score, spec in ranked:
                if use_preprocess:
                    verify_prepared = _prepare_filter_ocr_image_with_stats(raw_img, [spec])
                    if _prepared_has_empty_color_mask(verify_prepared):
                        continue
                    # Keep candidate verification separate to preserve the existing
                    # full-size verification behavior and its accuracy.
                    verify_text = _pool_read_text(_image_payload(verify_prepared.image))
                else:
                    verify_text = text
                verify_score = _score_text_against_target(
                    verify_text,
                    str(spec.get("target_text", "") or ""),
                )
                if verify_score >= DEFAULT_OCR_MATCH_THRESHOLD:
                    matches.append(
                        {
                            "id": str(spec.get("id") or ""),
                            "name": str(spec.get("name") or ""),
                            "behavior": str(spec.get("behavior") or ""),
                            "cooldown_group": str(spec.get("cooldown_group") or ""),
                            "score": float(score),
                            "verify_score": float(verify_score),
                        }
                    )
            results.append({"matches": matches, "text": text})
        except Exception as e:
            results.append({"matches": [], "error": f"{e.__class__.__name__}: {e}", "text": text})

    return {"results": results}


# -----------------------------
# Data structures & helpers
# -----------------------------


@dataclass
class RobloxWindow:
    hwnd: int
    pid: int
    title: str


@dataclass
class ColorFilter:
    name: str
    r: int
    g: int
    b: int
    tol: int
    enabled: bool = True


MERCHANT_LINES = {
    "jester": "[Merchant]: Jester has arrived on the island!!",
    "mari": "[Merchant]: Mari has arrived on the island...",
    "rin": "[Merchant]: Rin has arrived on the island!!",
}
START_PUZZLE_RE = re.compile(r"start\W*puzzle", re.IGNORECASE)
DEFAULT_OCR_MATCH_THRESHOLD = 0.80
DEFAULT_OCR_MATCH_LOOKAHEAD = 4
MERCHANT_FILTER_IDS = {
    "merchant_jester": "jester",
    "merchant_mari": "mari",
    "merchant_rin": "rin",
}
DEFAULT_OCR_FILTERS: List[Dict[str, Any]] = [
    {
        "id": "merchant_jester",
        "name": "Jester",
        "r": 145,
        "g": 67,
        "b": 255,
        "tol": 60,
        "enabled": True,
        "solo_ocr": False,
        "target_text": MERCHANT_LINES["jester"],
        "cooldown_seconds": 900,
        "use_shared_area": True,
        "shared_area_id": "chat",
        "roi": {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
        "webhook_url": "",
        "webhook_message": "",
        "send_screenshot": True,
        "behavior": "merchant",
        "cooldown_group": "merchant_filters",
    },
    {
        "id": "merchant_mari",
        "name": "Mari",
        "r": 255,
        "g": 255,
        "b": 255,
        "tol": 60,
        "enabled": True,
        "solo_ocr": False,
        "target_text": MERCHANT_LINES["mari"],
        "cooldown_seconds": 900,
        "use_shared_area": True,
        "shared_area_id": "chat",
        "roi": {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
        "webhook_url": "",
        "webhook_message": "",
        "send_screenshot": True,
        "behavior": "merchant",
        "cooldown_group": "merchant_filters",
    },
    {
        "id": "merchant_rin",
        "name": "Rin",
        "r": 255,
        "g": 138,
        "b": 68,
        "tol": 60,
        "enabled": True,
        "solo_ocr": False,
        "target_text": MERCHANT_LINES["rin"],
        "cooldown_seconds": 900,
        "use_shared_area": True,
        "shared_area_id": "chat",
        "roi": {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
        "webhook_url": "",
        "webhook_message": "",
        "send_screenshot": True,
        "behavior": "merchant",
        "cooldown_group": "merchant_filters",
    },
    {
        "id": "verification_check",
        "name": "Verification Check",
        "r": 255,
        "g": 255,
        "b": 255,
        "tol": 60,
        "enabled": True,
        "solo_ocr": False,
        "target_text": "Start Puzzle",
        "cooldown_seconds": 600,
        "use_shared_area": False,
        "shared_area_id": "",
        "roi": {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
        "webhook_url": "",
        "webhook_message": "",
        "send_screenshot": False,
        "behavior": "verification_cap",
        "cooldown_group": "verification_check",
    },
]


def get_default_ocr_filters() -> List[Dict[str, Any]]:
    return copy.deepcopy(DEFAULT_OCR_FILTERS)


def _normalize_text(s: str) -> str:
    return " ".join(s.lower().split())


def _fuzzy_ratio(line: str, target: str) -> float:
    l = _normalize_text(line)
    t = _normalize_text(target)
    if not l or not t:
        return 0.0
    return difflib.SequenceMatcher(None, l, t).ratio()


def _fuzzy_match(line: str, target: str, threshold: float = 0.7) -> bool:
    return _fuzzy_ratio(line, target) >= threshold


def _normalize_filter_name(raw: str) -> str:
    name = str(raw or "").strip()
    lower = name.lower()
    if lower == "white_text":
        return "Mari"
    if lower == "purple_text":
        return "Jester"
    if lower == "orange_text":
        return "Rin"
    if lower in ("verification", "verification_check", "verification check"):
        return "Verification Check"
    return name


def _slugify_filter_id(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")
    return slug or "filter"


def _known_filter_id(name: str) -> str:
    lower = _normalize_filter_name(name).strip().lower()
    if lower == "jester":
        return "merchant_jester"
    if lower == "mari":
        return "merchant_mari"
    if lower == "rin":
        return "merchant_rin"
    if lower == "verification check":
        return "verification_check"
    return ""


def _filter_behavior(filter_id: str, name: str, raw_behavior: Any = None) -> str:
    behavior = str(raw_behavior or "").strip().lower()
    if behavior in ("merchant", "verification_cap", "webhook"):
        return behavior
    if filter_id in MERCHANT_FILTER_IDS:
        return "merchant"
    if filter_id == "verification_check" or _normalize_filter_name(name).strip().lower() == "verification check":
        return "verification_cap"
    return "webhook"


def _cooldown_group_for_filter(filter_id: str, behavior: str, raw_group: Any = None) -> str:
    group = str(raw_group or "").strip()
    if group:
        return group
    if behavior == "merchant":
        return "merchant_filters"
    return filter_id or behavior or "filter"


def _empty_roi_dict() -> Dict[str, float]:
    return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}


def _normalize_shared_area_spec(raw: Dict[str, Any], *, fallback_index: int = 0) -> Dict[str, Any]:
    base = raw or {}
    name = str(base.get("name", "") or "").strip() or f"Shared Area {fallback_index + 1}"
    area_id = str(base.get("id") or base.get("area_id") or "").strip()
    if not area_id or area_id.lower() == "chat":
        area_id = f"shared_{fallback_index}_{_slugify_filter_id(name)}"

    roi_cfg = base.get("roi") if isinstance(base.get("roi"), dict) else {}
    return {
        "id": area_id,
        "name": name,
        "roi": {
            "x": float((roi_cfg or {}).get("x", 0.0) or 0.0),
            "y": float((roi_cfg or {}).get("y", 0.0) or 0.0),
            "w": float((roi_cfg or {}).get("w", 0.0) or 0.0),
            "h": float((roi_cfg or {}).get("h", 0.0) or 0.0),
        },
    }


def _merge_shared_areas(areas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, raw in enumerate(areas or []):
        if not isinstance(raw, dict):
            continue
        spec = _normalize_shared_area_spec(raw, fallback_index=idx)
        area_id = str(spec.get("id") or "").strip()
        if not area_id or area_id in seen_ids or area_id.lower() == "chat":
            continue
        seen_ids.add(area_id)
        normalized.append(spec)
    return normalized


def _shared_areas_from_cfg(ocr_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_areas = ocr_cfg.get("shared_areas")
    if isinstance(raw_areas, list):
        return _merge_shared_areas(raw_areas)
    return []


def _filter_shared_area_id(spec: Dict[str, Any]) -> str:
    base = spec or {}
    use_shared_area = bool(base.get("use_shared_area", base.get("use_chat_area", False)))
    if not use_shared_area:
        return ""
    area_id = str(base.get("shared_area_id") or "").strip()
    if not area_id and bool(base.get("use_chat_area", False)):
        area_id = "chat"
    return area_id or "chat"


def _filter_uses_chat_area(spec: Dict[str, Any]) -> bool:
    return _filter_shared_area_id(spec) == "chat"


def _filter_solo_ocr(spec: Dict[str, Any]) -> bool:
    base = spec or {}
    for key in ("solo_ocr", "isolate_color_filter", "isolated_ocr"):
        if key in base:
            return bool(base.get(key))
    return False


def _normalize_filter_user_ids(raw: Any) -> Optional[List[str]]:
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple, set)):
        return None
    cleaned: List[str] = []
    seen: set[str] = set()
    for uid in raw:
        uid_s = str(uid or "").strip()
        if not uid_s or uid_s in seen:
            continue
        seen.add(uid_s)
        cleaned.append(uid_s)
    return cleaned


def _normalize_user_filter_mode(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in {"blacklist", "blocklist", "exclude", "denylist", "deny"}:
        return "blacklist"
    return "whitelist"


def _normalize_filter_spec(raw: Dict[str, Any], *, fallback_index: int = 0) -> Dict[str, Any]:
    base = raw or {}
    name = _normalize_filter_name(str(base.get("name", "")).strip())
    filter_id = str(base.get("id") or base.get("filter_id") or "").strip()
    if not filter_id:
        filter_id = _known_filter_id(name)
    if not filter_id:
        filter_id = f"custom_{fallback_index}_{_slugify_filter_id(name)}"

    default_map = {str(item.get("id") or ""): item for item in DEFAULT_OCR_FILTERS}
    default_spec = default_map.get(filter_id, {})
    behavior = _filter_behavior(filter_id, name, base.get("behavior", default_spec.get("behavior")))
    use_shared_area = bool(
        base.get(
            "use_shared_area",
            base.get(
                "use_chat_area",
                default_spec.get("use_shared_area", default_spec.get("use_chat_area", behavior == "merchant")),
            ),
        )
    )
    shared_area_id = str(
        base.get("shared_area_id", default_spec.get("shared_area_id", "chat" if use_shared_area else "")) or ""
    ).strip()
    if not shared_area_id and bool(base.get("use_chat_area", False)):
        shared_area_id = "chat"
    if behavior == "merchant":
        use_shared_area = True
        shared_area_id = "chat"
    if not use_shared_area:
        shared_area_id = ""

    roi_cfg = base.get("roi") if isinstance(base.get("roi"), dict) else default_spec.get("roi", _empty_roi_dict())
    roi_dict = {
        "x": float((roi_cfg or {}).get("x", 0.0) or 0.0),
        "y": float((roi_cfg or {}).get("y", 0.0) or 0.0),
        "w": float((roi_cfg or {}).get("w", 0.0) or 0.0),
        "h": float((roi_cfg or {}).get("h", 0.0) or 0.0),
    }
    solo_ocr = _filter_solo_ocr(base)
    if not any(key in base for key in ("solo_ocr", "isolate_color_filter", "isolated_ocr")):
        solo_ocr = _filter_solo_ocr(default_spec)

    return {
        "id": filter_id,
        "name": name or str(default_spec.get("name") or filter_id),
        "r": int(base.get("r", default_spec.get("r", 255)) or 0),
        "g": int(base.get("g", default_spec.get("g", 255)) or 0),
        "b": int(base.get("b", default_spec.get("b", 255)) or 0),
        "tol": int(base.get("tol", default_spec.get("tol", 60)) or 0),
        "enabled": bool(base.get("enabled", default_spec.get("enabled", True))),
        "solo_ocr": solo_ocr,
        "target_text": str(base.get("target_text", default_spec.get("target_text", name)) or "").strip(),
        "cooldown_seconds": float(base.get("cooldown_seconds", default_spec.get("cooldown_seconds", 600)) or 0.0),
        "use_shared_area": bool(use_shared_area),
        "shared_area_id": shared_area_id,
        "roi": roi_dict,
        "webhook_url": str(base.get("webhook_url", "") or "").strip(),
        "webhook_message": str(base.get("webhook_message", "") or ""),
        "send_screenshot": bool(base.get("send_screenshot", default_spec.get("send_screenshot", behavior == "merchant"))),
        "repeat_alert_sound": bool(base.get("repeat_alert_sound", default_spec.get("repeat_alert_sound", False))),
        "user_ids": _normalize_filter_user_ids(base.get("user_ids", default_spec.get("user_ids"))),
        "user_filter_mode": _normalize_user_filter_mode(base.get("user_filter_mode", default_spec.get("user_filter_mode", "whitelist"))),
        "behavior": behavior,
        "cooldown_group": _cooldown_group_for_filter(
            filter_id,
            behavior,
            base.get("cooldown_group", default_spec.get("cooldown_group")),
        ),
    }


def _merge_filters_with_defaults(filters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, raw in enumerate(filters or []):
        if not isinstance(raw, dict):
            continue
        spec = _normalize_filter_spec(raw, fallback_index=idx)
        filter_id = str(spec.get("id") or "").strip()
        if not filter_id or filter_id in seen_ids:
            continue
        seen_ids.add(filter_id)
        normalized.append(spec)

    by_id = {str(spec.get("id") or ""): spec for spec in normalized}
    out: List[Dict[str, Any]] = []
    for default_spec in get_default_ocr_filters():
        filter_id = str(default_spec.get("id") or "").strip()
        out.append(by_id.pop(filter_id, _normalize_filter_spec(default_spec)))
    out.extend(spec for spec in normalized if str(spec.get("id") or "").strip() in by_id)
    return out


def _migrate_legacy_filters(
    raw_filters: List[Dict[str, Any]],
    *,
    legacy_verification_roi: Optional[Dict[str, Any]] = None,
    legacy_cooldown_seconds: float = 600.0,
) -> List[Dict[str, Any]]:
    migrated: List[Dict[str, Any]] = []
    legacy_roi = legacy_verification_roi if isinstance(legacy_verification_roi, dict) else _empty_roi_dict()
    for idx, raw in enumerate(raw_filters or []):
        if not isinstance(raw, dict):
            continue
        name = _normalize_filter_name(str(raw.get("name", "")).strip())
        filter_id = _known_filter_id(name)
        default_spec = next((item for item in DEFAULT_OCR_FILTERS if str(item.get("id") or "") == filter_id), {})
        behavior = _filter_behavior(filter_id, name)
        use_shared_area = behavior == "merchant"
        migrated.append(
            {
                "id": filter_id or "",
                "name": name,
                "r": int(raw.get("r", default_spec.get("r", 255)) or 0),
                "g": int(raw.get("g", default_spec.get("g", 255)) or 0),
                "b": int(raw.get("b", default_spec.get("b", 255)) or 0),
                "tol": int(raw.get("tol", default_spec.get("tol", 60)) or 0),
                "enabled": bool(raw.get("enabled", True)),
                "solo_ocr": False,
                "target_text": str(default_spec.get("target_text") or name or "").strip(),
                "cooldown_seconds": float(legacy_cooldown_seconds or default_spec.get("cooldown_seconds", 600) or 600),
                "use_shared_area": bool(use_shared_area),
                "shared_area_id": "chat" if use_shared_area else "",
                "roi": copy.deepcopy(legacy_roi if filter_id == "verification_check" else _empty_roi_dict()),
                "webhook_url": "",
                "webhook_message": "",
                "send_screenshot": bool(behavior == "merchant"),
                "repeat_alert_sound": False,
                "user_ids": None,
                "user_filter_mode": "whitelist",
                "behavior": behavior,
                "cooldown_group": _cooldown_group_for_filter(filter_id, behavior),
            }
        )
    return _merge_filters_with_defaults(migrated)


def _filters_from_cfg(ocr_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_filters = ocr_cfg.get("filters")
    if isinstance(raw_filters, list) and raw_filters:
        return _merge_filters_with_defaults(raw_filters)

    legacy_filters = ocr_cfg.get("color_filters")
    if isinstance(legacy_filters, list) and legacy_filters:
        try:
            legacy_cooldown = float(ocr_cfg.get("cooldown_seconds", 600) or 600)
        except Exception:
            legacy_cooldown = 600.0
        return _migrate_legacy_filters(
            legacy_filters,
            legacy_verification_roi=ocr_cfg.get("verification_roi"),
            legacy_cooldown_seconds=legacy_cooldown,
        )

    return get_default_ocr_filters()


def _filter_color_from_spec(spec: Dict[str, Any], *, enabled: Optional[bool] = None) -> ColorFilter:
    return ColorFilter(
        str(spec.get("name", "") or "").strip(),
        int(spec.get("r", 0) or 0),
        int(spec.get("g", 0) or 0),
        int(spec.get("b", 0) or 0),
        int(spec.get("tol", 0) or 0),
        bool(spec.get("enabled", True) if enabled is None else enabled),
    )


def _compact_text(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").lower())


def _score_text_against_target(text: str, target: str) -> float:
    raw_text = str(text or "")
    raw_target = str(target or "")
    if not raw_text.strip() or not raw_target.strip():
        return 0.0

    best = _fuzzy_ratio(raw_text, raw_target)
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    target_lines = [ln.strip() for ln in raw_target.splitlines() if ln.strip()]
    compact_target = _compact_text(raw_target)
    max_lookahead = max(
        1,
        min(
            DEFAULT_OCR_MATCH_LOOKAHEAD,
            max(1, len(target_lines)) + 1,
            len(lines) if lines else 1,
        ),
    )

    if not lines:
        compact_text = _compact_text(raw_text)
        if compact_target and compact_target in compact_text:
            return 1.0
        return best

    for start in range(len(lines)):
        for lookahead in range(1, min(max_lookahead, len(lines) - start) + 1):
            block = " ".join(lines[start : start + lookahead])
            score = _fuzzy_ratio(block, raw_target)
            if compact_target and compact_target in _compact_text(block):
                score = max(score, 1.0)
            if score > best:
                best = score
    return best


def _rank_filter_candidates(text: str, filters: List[Dict[str, Any]]) -> List[Tuple[float, Dict[str, Any]]]:
    ranked: List[Tuple[float, Dict[str, Any]]] = []
    for spec in filters or []:
        target_text = str(spec.get("target_text", "") or "").strip()
        if not target_text:
            continue
        score = _score_text_against_target(text, target_text)
        if score >= DEFAULT_OCR_MATCH_THRESHOLD:
            ranked.append((score, spec))
    ranked.sort(key=lambda item: (-item[0], str(item[1].get("name", "")).lower()))
    return ranked


def detect_merchant_type(text: str) -> Optional[str]:
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    max_lookahead = 3
    for i in range(len(lines)):
        # Merchant lines may wrap across chat lines, so check a window of nearby lines.
        for lookahead in range(1, min(max_lookahead, len(lines) - i) + 1):
            block = " ".join(lines[i : i + lookahead])
            block_lower = block.lower()
            if "merchant" not in block_lower:
                continue
            if "jester" in block_lower and "arrived" in block_lower and "island" in block_lower:
                return "jester"
            if "mari" in block_lower and "arrived" in block_lower and "island" in block_lower:
                return "mari"
            if "rin" in block_lower and "arrived" in block_lower and "island" in block_lower:
                return "rin"
            scores = {
                "jester": _fuzzy_ratio(block, MERCHANT_LINES["jester"]),
                "mari": _fuzzy_ratio(block, MERCHANT_LINES["mari"]),
                "rin": _fuzzy_ratio(block, MERCHANT_LINES["rin"]),
            }
            best_name, best_score = max(scores.items(), key=lambda kv: kv[1])
            if best_score >= 0.7:
                mari_score = scores["mari"]
                rin_score = scores["rin"]
                # Rare OCR ambiguity: Mari vs Rin can be very close when the name is noisy.
                # Prefer punctuation cues to avoid false Mari hits on Rin lines.
                if mari_score >= 0.7 and rin_score >= 0.7 and abs(mari_score - rin_score) <= 0.02:
                    has_ellipsis = "..." in block
                    has_exclaim = "!" in block
                    if has_exclaim and not has_ellipsis:
                        return "rin"
                    if has_ellipsis:
                        return "mari"
                return best_name
    return None


def contains_jester_message(text: str) -> bool:
    return detect_merchant_type(text) == "jester"


def enum_roblox_windows(process_names: Optional[List[str]] = None) -> List[RobloxWindow]:
    if process_names is None:
        process_names = ["RobloxPlayerBeta.exe"]

    wanted = set(n.lower() for n in process_names)
    pid_to_name: Dict[int, str] = {}

    def get_proc_name(pid: int) -> Optional[str]:
        if pid in pid_to_name:
            return pid_to_name[pid]
        try:
            p = psutil.Process(pid)
            name = p.name().lower()
            pid_to_name[pid] = name
            return name
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    windows: List[RobloxWindow] = []

    def callback(hwnd, _extra):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return
            name = get_proc_name(pid)
            if name in wanted:
                windows.append(RobloxWindow(hwnd=hwnd, pid=pid, title=title))
        except Exception:
            return

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        return windows
    return windows


def crop_image_with_roi(image: Image.Image, roi: Tuple[float, float, float, float]) -> Image.Image:
    iw, ih = image.size
    rx, ry, rw, rh = roi
    x0 = max(0, min(iw, int(rx * iw)))
    y0 = max(0, min(ih, int(ry * ih)))
    x1 = max(0, min(iw, int((rx + rw) * iw)))
    y1 = max(0, min(ih, int((ry + rh) * ih)))
    if x1 <= x0 or y1 <= y0:
        return image.copy()
    return image.crop((x0, y0, x1, y1))


def capture_window_printwindow(hwnd: int) -> Optional[Image.Image]:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None

    mfc_dc = None
    save_dc = None
    save_bitmap = None
    old_bitmap = None
    try:
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        save_bitmap = win32ui.CreateBitmap()
        save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        old_bitmap = save_dc.SelectObject(save_bitmap)
        result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0x00000002)
        if result != 1:
            return None
        bmpinfo = save_bitmap.GetInfo()
        bmpstr = save_bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB",
            (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
            bmpstr,
            "raw",
            "BGRX",
            0,
            1,
        )
        return img
    except Exception:
        return None
    finally:
        try:
            if save_dc is not None and old_bitmap is not None:
                save_dc.SelectObject(old_bitmap)
        except Exception:
            pass
        try:
            if save_bitmap is not None:
                win32gui.DeleteObject(save_bitmap.GetHandle())
        except Exception:
            pass
        try:
            if save_dc is not None:
                save_dc.DeleteDC()
        except Exception:
            pass
        try:
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
        except Exception:
            pass
        try:
            win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def capture_window_fallback(hwnd: int) -> Optional[Image.Image]:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    try:
        return ImageGrab.grab(bbox=(left, top, right, bottom))
    except Exception:
        return None


def capture_window_image(hwnd: int, roi: Optional[Tuple[float, float, float, float]] = None) -> Optional[Image.Image]:
    img = capture_window_printwindow(hwnd)
    if img is None:
        img = capture_window_fallback(hwnd)
    if img is None:
        return None
    if roi is not None:
        return crop_image_with_roi(img, roi)
    return img


def preprocess_for_ocr_with_stats(image: Image.Image, color_filters: List[ColorFilter]) -> PreparedOCRImage:
    def _finalize_gray(gray_img: Image.Image) -> Image.Image:
        """Apply contrast, scale, and invert to prepare for OCR."""
        if gray_img.mode != "L":
            gray_img = gray_img.convert("L")
        gray_img = ImageOps.autocontrast(gray_img)
        w0, h0 = gray_img.size
        if w0 == 0 or h0 == 0:
            return ImageOps.invert(gray_img)
        scale = 3 if w0 < 400 else 2
        resized = gray_img.resize((w0 * scale, h0 * scale), Image.LANCZOS)
        return ImageOps.invert(resized)

    if not color_filters:
        return PreparedOCRImage(image=_finalize_gray(image.convert("L")))

    rgb = image.convert("RGB")
    arr = np.asarray(rgb, dtype=np.uint8)

    h, w = arr.shape[0], arr.shape[1]
    if h == 0 or w == 0:
        return PreparedOCRImage(image=_finalize_gray(rgb.convert("L")), has_color_filters=True)

    enabled_filters = [cf for cf in color_filters if cf.enabled]
    if not enabled_filters:
        return PreparedOCRImage(image=_finalize_gray(rgb.convert("L")))

    keep_mask = np.zeros((h, w), dtype=bool)
    r_channel = arr[:, :, 0].astype(np.int16)
    g_channel = arr[:, :, 1].astype(np.int16)
    b_channel = arr[:, :, 2].astype(np.int16)

    for cf in enabled_filters:
        r0 = max(0, min(255, int(cf.r)))
        g0 = max(0, min(255, int(cf.g)))
        b0 = max(0, min(255, int(cf.b)))
        tol = max(0, int(cf.tol))

        dr = np.abs(r_channel - r0)
        dg = np.abs(g_channel - g0)
        db = np.abs(b_channel - b0)

        keep_mask |= (dr <= tol) & (dg <= tol) & (db <= tol)

    mask_arr = np.zeros_like(arr, dtype=np.uint8)
    mask_arr[keep_mask] = [255, 255, 255]
    text_only = Image.fromarray(mask_arr, mode="RGB")
    gray = text_only.convert("L")
    return PreparedOCRImage(
        image=_finalize_gray(gray),
        has_color_filters=True,
        mask_pixels=int(np.count_nonzero(keep_mask)),
        total_pixels=int(h * w),
    )


def preprocess_for_ocr(image: Image.Image, color_filters: List[ColorFilter]) -> Image.Image:
    return preprocess_for_ocr_with_stats(image, color_filters).image

def _active_color_filters_from_specs(filter_specs: List[Dict[str, Any]]) -> List[ColorFilter]:
    colors: List[ColorFilter] = []
    for spec in filter_specs or []:
        try:
            colors.append(_filter_color_from_spec(spec))
        except Exception:
            continue
    return colors


def _roi_from_cfg(roi_cfg: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    try:
        rx = float(roi_cfg.get("x", 0.0))
        ry = float(roi_cfg.get("y", 0.0))
        rw = float(roi_cfg.get("w", 0.0))
        rh = float(roi_cfg.get("h", 0.0))
    except Exception:
        return None
    if rw <= 0 or rh <= 0:
        return None
    return (rx, ry, rw, rh)


def _image_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _parse_device_id(raw: Any) -> Tuple[Optional[int], bool]:
    if raw is None:
        return None, False
    if isinstance(raw, str):
        val = raw.strip().lower()
        if val in ("", "auto", "none"):
            return None, False
        if val in ("cpu", "force_cpu"):
            return None, True
    try:
        device_id = int(raw)
    except Exception:
        return None, False
    if device_id < 0:
        return None, True
    return device_id, False


_ON_DEMAND_OCR_LOCK = threading.Lock()
_ON_DEMAND_OCR_POOL: Optional[ProcessPoolExecutor] = None
_ON_DEMAND_OCR_KEY: Optional[Tuple[Optional[int], bool]] = None


def _is_ort_device_lost_error(exc: BaseException) -> bool:
    text = f"{exc.__class__.__name__}: {exc}".lower()
    return any(
        token in text
        for token in (
            "887a0005",
            "887a0006",
            "device instance has been suspended",
            "device removed",
            "deviceremoved",
            "dml executionprovider",
        )
    )


def _reset_on_demand_ocr_reader() -> None:
    global _ON_DEMAND_OCR_POOL, _ON_DEMAND_OCR_KEY
    if _ON_DEMAND_OCR_POOL is not None:
        try:
            _ON_DEMAND_OCR_POOL.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            try:
                _ON_DEMAND_OCR_POOL.shutdown(wait=False)
            except Exception:
                pass
        except Exception:
            pass
    _ON_DEMAND_OCR_POOL = None
    _ON_DEMAND_OCR_KEY = None


def _on_demand_ocr_pool(device_id: Optional[int], force_cpu: bool) -> ProcessPoolExecutor:
    global _ON_DEMAND_OCR_POOL, _ON_DEMAND_OCR_KEY
    key = (device_id, bool(force_cpu))
    if _ON_DEMAND_OCR_POOL is None or _ON_DEMAND_OCR_KEY != key:
        _reset_on_demand_ocr_reader()
        _ON_DEMAND_OCR_POOL = ProcessPoolExecutor(
            max_workers=1,
            mp_context=get_context("spawn"),
            initializer=_init_pool_reader,
            initargs=(device_id, bool(force_cpu)),
        )
        _ON_DEMAND_OCR_KEY = key
    return _ON_DEMAND_OCR_POOL


def read_window_ocr_text_once(
    hwnd: int,
    *,
    roi: Optional[Dict[str, Any]] = None,
    ocr_settings: Optional[Dict[str, Any]] = None,
    filter_ids: Optional[List[str]] = None,
    color_filters: Optional[List[Dict[str, Any]]] = None,
    ocr_pool: Optional[ProcessPoolExecutor] = None,
) -> str:
    """
    Capture one window/ROI and run OCR using the same RapidOCR/preprocess stack as OCRWorker.

    This is used by Auto Actions OCR conditionals, which need an immediate read at the
    exact step where the condition is evaluated instead of waiting for the background
    OCR batch loop.
    """
    cfg = ocr_settings or {}
    if RapidOCR is None:
        raise RuntimeError(f"rapidocr is not available: {_RAPIDOCR_IMPORT_ERROR}")
    if ort is None:
        raise RuntimeError(f"onnxruntime is not available: {_ORT_IMPORT_ERROR}")

    roi_tuple = _roi_from_cfg(roi or {}) if isinstance(roi, dict) else None
    if roi_tuple is None and isinstance(cfg.get("roi"), dict):
        roi_tuple = _roi_from_cfg(cfg.get("roi") or {})

    raw_img = capture_window_image(int(hwnd), roi_tuple)
    if raw_img is None:
        raise RuntimeError("window capture failed")

    filters: List[Dict[str, Any]] = []
    if isinstance(color_filters, list) and color_filters:
        for idx, raw in enumerate(color_filters):
            if not isinstance(raw, dict) or not bool(raw.get("enabled", True)):
                continue
            try:
                filters.append(
                    {
                        "id": f"auto_action_condition_{idx}",
                        "name": str(raw.get("name") or f"Condition Color {idx + 1}"),
                        "r": int(raw.get("r", 255) or 0),
                        "g": int(raw.get("g", 255) or 0),
                        "b": int(raw.get("b", 255) or 0),
                        "tol": int(raw.get("tol", raw.get("tolerance", 40)) or 0),
                        "enabled": True,
                    }
                )
            except Exception:
                continue

    wanted = {str(fid or "").strip() for fid in (filter_ids or []) if str(fid or "").strip()}
    if wanted and not filters:
        for spec in _filters_from_cfg(cfg):
            if str(spec.get("id") or "").strip() in wanted and bool(spec.get("enabled", True)):
                filters.append(spec)

    use_preprocess = bool(cfg.get("use_preprocess", True))
    try:
        prepared = _prepare_filter_ocr_image_with_stats(raw_img, filters) if use_preprocess else PreparedOCRImage(raw_img)
        if _prepared_has_empty_color_mask(prepared):
            return ""
        img_for_ocr = prepared.image
    except Exception:
        img_for_ocr = raw_img

    device_id, force_cpu = _parse_device_id(cfg.get("device_id"))
    payload = _image_payload(img_for_ocr)

    if ocr_pool is not None:
        result = ocr_pool.submit(_ocr_text_task, payload).result()
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(str(result.get("error")))
        return str((result or {}).get("text") or "") if isinstance(result, dict) else ""

    with _ON_DEMAND_OCR_LOCK:
        pool = _on_demand_ocr_pool(device_id, force_cpu)
        try:
            result = pool.submit(_ocr_text_task, payload).result()
            if isinstance(result, dict) and result.get("error"):
                raise RuntimeError(str(result.get("error")))
            return str((result or {}).get("text") or "") if isinstance(result, dict) else ""
        except Exception as e:
            if not _is_ort_device_lost_error(e):
                raise
            _reset_on_demand_ocr_reader()
            retry_pool = _on_demand_ocr_pool(device_id, force_cpu)
            result = retry_pool.submit(_ocr_text_task, payload).result()
            if isinstance(result, dict) and result.get("error"):
                raise RuntimeError(str(result.get("error")))
            return str((result or {}).get("text") or "") if isinstance(result, dict) else ""


class OCRWorker(QThread):
    """
    Background OCR loop that mirrors roblox_multi_ocr.py but integrates with
    the existing MultiScope merchant webhook and JARAM process metadata.
    """

    log_signal = Signal(str)
    status_signal = Signal(str)
    merchant_signal = Signal(str, str)  # (user_id, merchant)
    verification_cap_signal = Signal(str)  # user_id to mark CAP
    filter_alert_signal = Signal(str)  # filter name
    filter_match_signal = Signal(int, str, str)  # pid, filter_id, filter_name

    def __init__(
        self,
        *,
        ocr_settings: Optional[Dict[str, Any]],
        ms_settings: Optional[Dict[str, Any]],
        context_provider: Optional[Callable[[int], Dict[str, Any]]] = None,
    ):
        super().__init__()
        self._ocr_cfg = ocr_settings or {}
        self._ms_cfg = ms_settings or {}
        self._context_provider = context_provider

        self._cooldowns: Dict[int, Dict[str, float]] = {}
        self._capture_rr_index = 0
        self._stop_event = threading.Event()
        self._last_log: Optional[str] = None
        self._ocr_pool: Optional[ProcessPoolExecutor] = None
        self._mp_ctx = get_context("spawn")
        self._frame_hash_size = _FRAME_HASH_SIZE
        self._frame_diff_tolerance = 0.0
        self._last_frame_hash_by_key: Dict[str, int] = {}
        self._verification_next_check_by_pid: Dict[int, float] = {}
        self._verification_check_interval = 2.0
        self._window_snapshot_lock = threading.Lock()
        self._window_snapshot_ready = threading.Event()
        self._window_snapshot: List[RobloxWindow] = []
        self._window_snapshot_at = 0.0
        self._window_snapshot_elapsed = 0.0
        self._window_snapshot_error = ""
        self._window_enumerator_thread: Optional[threading.Thread] = None
        self._window_enum_interval_seconds = _WINDOW_ENUM_INTERVAL_SECONDS

        self._send_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ocr-send")

        self._apply_cfg(self._ocr_cfg, self._ms_cfg)

    def stop(self) -> None:
        self._stop_event.set()

    def update_settings(self, ocr_settings: Dict[str, Any], ms_settings: Optional[Dict[str, Any]] = None) -> None:
        self._apply_cfg(ocr_settings or {}, ms_settings or self._ms_cfg)

    # -------------------- window enumeration cache --------------------
    def _start_window_enumerator(self) -> None:
        thread = getattr(self, "_window_enumerator_thread", None)
        if thread is not None and thread.is_alive():
            return
        with self._window_snapshot_lock:
            self._window_snapshot = []
            self._window_snapshot_at = 0.0
            self._window_snapshot_elapsed = 0.0
            self._window_snapshot_error = ""
        self._window_snapshot_ready.clear()
        thread = threading.Thread(
            target=self._window_enumerator_loop,
            name="ocr-window-enumerator",
            daemon=True,
        )
        self._window_enumerator_thread = thread
        thread.start()

    def _stop_window_enumerator(self) -> None:
        thread = getattr(self, "_window_enumerator_thread", None)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._window_enumerator_thread = None

    def _window_enumerator_loop(self) -> None:
        while not self._stop_event.is_set():
            started = time.perf_counter()
            windows: List[RobloxWindow] = []
            error_text = ""
            try:
                windows = enum_roblox_windows()
            except Exception as e:
                error_text = repr(e)

            elapsed = time.perf_counter() - started
            with self._window_snapshot_lock:
                self._window_snapshot = list(windows or [])
                self._window_snapshot_at = time.perf_counter()
                self._window_snapshot_elapsed = elapsed
                self._window_snapshot_error = error_text
            self._window_snapshot_ready.set()

            try:
                interval = max(0.05, float(getattr(self, "_window_enum_interval_seconds", _WINDOW_ENUM_INTERVAL_SECONDS)))
            except Exception:
                interval = _WINDOW_ENUM_INTERVAL_SECONDS
            self._stop_event.wait(timeout=max(0.0, interval - (time.perf_counter() - started)))

    def _get_window_snapshot(self) -> Tuple[List[RobloxWindow], Optional[float], Optional[float], bool, str]:
        if not self._window_snapshot_ready.is_set():
            try:
                interval = max(0.05, float(getattr(self, "_window_enum_interval_seconds", _WINDOW_ENUM_INTERVAL_SECONDS)))
            except Exception:
                interval = _WINDOW_ENUM_INTERVAL_SECONDS
            self._window_snapshot_ready.wait(timeout=min(0.25, interval))

        with self._window_snapshot_lock:
            windows = list(self._window_snapshot or [])
            snapshot_at = float(self._window_snapshot_at or 0.0)
            enum_elapsed = float(self._window_snapshot_elapsed or 0.0)
            error_text = str(self._window_snapshot_error or "")

        if snapshot_at <= 0.0:
            return windows, None, None, False, error_text
        return windows, max(0.0, time.perf_counter() - snapshot_at), enum_elapsed, True, error_text

    # -------------------------- core loop --------------------------
    def run(self) -> None:
        global _OCR_DEVICE_SUMMARY
        if RapidOCR is None or ort is None:
            missing: List[str] = []
            if RapidOCR is None:
                missing.append(f"rapidocr ({_RAPIDOCR_IMPORT_ERROR})")
            if ort is None:
                missing.append(f"onnxruntime ({_ORT_IMPORT_ERROR})")
            self._log(f"[OCR] RapidOCR/ONNX Runtime not available: {', '.join(missing)}")
            return

        self._stop_event.clear()
        loop_idx = 0
        providers = _get_ort_providers()
        if providers:
            self._log(f"[ORT] Available providers: {providers}")
        else:
            self._log("[ORT] No ONNX Runtime providers detected.")
        if not self._force_cpu and "DmlExecutionProvider" not in providers:
            _OCR_DEVICE_SUMMARY = "Unavailable (DirectML provider not available)"
            self._log("[OCR] Failed to initialize RapidOCR (DirectML): DirectML provider is not available.")
            return
        try:
            self._ocr_pool = ProcessPoolExecutor(
                max_workers=1,
                mp_context=self._mp_ctx,
                initializer=_init_pool_reader,
                initargs=(self._device_id, self._force_cpu),
            )
            warmup = self._ocr_pool.submit(_ocr_pool_warmup_task)
            warmup_result = warmup.result()
            if not isinstance(warmup_result, dict) or not warmup_result.get("ok"):
                err = warmup_result.get("error") if isinstance(warmup_result, dict) else "unknown error"
                self._log(f"[OCR] Failed to initialize OCR process: {err}")
                self._shutdown_ocr_pool()
                return
            _OCR_DEVICE_SUMMARY = str(warmup_result.get("device") or "")
            if self._force_cpu:
                self._log("[ORT] OCR initialized on CPU in the OCR process.")
            elif "DmlExecutionProvider" in providers:
                self._log("[ORT] DirectML detected. OCR initialized in one OCR process.")
        except Exception as e:
            self._log(f"[OCR] Failed to start OCR process pool: {e}")
            return

        try:
            self.status_signal.emit("running")
            self._log("OCR worker started.")
            self._log(f"[OCR] Processing mode: {self._processing_mode}.")
            self._start_window_enumerator()

            while not self._stop_event.is_set():
                loop_idx += 1
                loop_started = time.perf_counter()
                try:
                    step_started = time.perf_counter()
                    windows, snapshot_age, enum_elapsed, snapshot_ready, snapshot_error = self._get_window_snapshot()
                    snapshot_load_elapsed = time.perf_counter() - step_started
                    if snapshot_ready:
                        self._log(
                            f"[Loop {loop_idx}] Loaded cached window snapshot: {len(windows)} Roblox window(s) "
                            f"in {_format_step_duration(snapshot_load_elapsed)} "
                            f"(age {_format_step_duration(snapshot_age or 0.0)}, "
                            f"enum {_format_step_duration(enum_elapsed or 0.0)})."
                        )
                    else:
                        self._log(
                            f"[Loop {loop_idx}] Window snapshot pending after "
                            f"{_format_step_duration(snapshot_load_elapsed)}."
                        )
                    if snapshot_error:
                        self._log(f"[OCR] Last window enumeration error: {snapshot_error}")
                    if not windows:
                        if snapshot_ready:
                            self._log("No Roblox windows found.")
                            sleep_reason = "no windows"
                        else:
                            self._log(f"[Loop {loop_idx}] No cached windows available yet.")
                            sleep_reason = "snapshot pending"
                        sleep_for = max(0.0, getattr(self, "_batch_delay_seconds", 1.0))
                        active_elapsed = time.perf_counter() - loop_started
                        self._log(
                            f"[Loop {loop_idx}] Sleeping {sleep_for:.2f}s ({sleep_reason}; "
                            f"active {_format_step_duration(active_elapsed)})."
                        )
                        self._stop_event.wait(timeout=sleep_for)
                        continue

                    step_started = time.perf_counter()
                    work_list = self._select_windows(windows)
                    select_elapsed = time.perf_counter() - step_started
                    self._log(
                        f"[Loop {loop_idx}] Selected {len(work_list)} window(s) for capture "
                        f"in {_format_step_duration(select_elapsed)}."
                    )
                    if work_list:
                        step_started = time.perf_counter()
                        try:
                            self._run_verification_checks(work_list)
                        except Exception as e:
                            self._log(f"[Verification] check error: {e}")
                        verification_elapsed = time.perf_counter() - step_started
                        self._log(
                            f"[Loop {loop_idx}] Verification checks finished "
                            f"in {_format_step_duration(verification_elapsed)}."
                        )

                        configured_batch_limit = max(
                            1,
                            int(getattr(self, "_max_captures_per_second", 1) or 1),
                        )
                        processed_count = 0
                        skipped_similar = 0
                        captured_count = 0
                        future_map = {}
                        processing_mode = _normalize_ocr_processing_mode(
                            getattr(self, "_processing_mode", "separate")
                        )
                        combined_mode = processing_mode == "combined"
                        remaining_slots = len(work_list) if combined_mode else configured_batch_limit
                        combined_entries: List[Dict[str, Any]] = []
                        combined_meta: List[Dict[str, Any]] = []

                        pending_groups: List[Dict[str, Any]] = []
                        step_started = time.perf_counter()
                        for win in work_list:
                            groups = self._build_capture_groups(int(getattr(win, "pid", 0) or 0))
                            if groups:
                                pending_groups.append({"win": win, "groups": list(groups)})
                        build_groups_elapsed = time.perf_counter() - step_started
                        total_groups = sum(len(item.get("groups") or []) for item in pending_groups)
                        self._log(
                            f"[Loop {loop_idx}] Built {total_groups} capture group(s) "
                            f"in {_format_step_duration(build_groups_elapsed)}."
                        )

                        capture_elapsed = 0.0
                        payload_elapsed = 0.0
                        preprocess_elapsed = 0.0
                        frame_compare_elapsed = 0.0
                        dispatch_elapsed = 0.0
                        skipped_empty_mask = 0
                        dispatched_count = 0
                        combined_batch_count = 0

                        for entry in pending_groups:
                            if self._stop_event.is_set() or remaining_slots <= 0:
                                break

                            win = entry.get("win")
                            groups = list(entry.get("groups") or [])
                            if win is None or not groups:
                                continue

                            step_started = time.perf_counter()
                            full_img = capture_window_image(win.hwnd)
                            capture_elapsed += time.perf_counter() - step_started
                            remaining_slots -= 1
                            if full_img is None:
                                continue

                            captured_count += 1
                            roi_cache: Dict[str, Image.Image] = {}
                            raw_payload_cache: Dict[str, Tuple[str, Tuple[int, int], bytes]] = {}

                            for group in groups:
                                if self._stop_event.is_set():
                                    break

                                capture_key = str(group.get("capture_key") or group.get("cache_key") or "")
                                raw_img = roi_cache.get(capture_key)
                                if raw_img is None:
                                    raw_img = crop_image_with_roi(full_img, group["roi"])
                                    roi_cache[capture_key] = raw_img

                                try:
                                    step_started = time.perf_counter()
                                    prepared = (
                                        _prepare_filter_ocr_image_with_stats(raw_img, group["filters"])
                                        if self._use_preprocess
                                        else PreparedOCRImage(raw_img)
                                    )
                                    preprocess_elapsed += time.perf_counter() - step_started
                                except Exception as e:
                                    preprocess_elapsed += time.perf_counter() - step_started
                                    self._log(f"[OCR] Preprocess failed for PID {win.pid}: {e}")
                                    self._log(traceback.format_exc())
                                    prepared = PreparedOCRImage(raw_img)

                                if _prepared_has_empty_color_mask(prepared):
                                    skipped_empty_mask += 1
                                    continue

                                prep_img = prepared.image
                                step_started = time.perf_counter()
                                skip, _diff_pct = self._skip_ocr_for_similar_frame(
                                    self._frame_cache_key(int(win.pid), str(group["cache_key"])),
                                    prep_img,
                                )
                                frame_compare_elapsed += time.perf_counter() - step_started
                                if skip:
                                    skipped_similar += 1
                                    continue

                                if not self._ocr_pool:
                                    self._log("[OCR] Process pool is unavailable; skipping OCR task.")
                                    continue

                                try:
                                    step_started = time.perf_counter()
                                    raw_payload = raw_payload_cache.get(capture_key)
                                    if raw_payload is None:
                                        raw_payload = _image_payload(raw_img)
                                        raw_payload_cache[capture_key] = raw_payload
                                    prep_payload = _image_payload(prep_img)
                                    payload_elapsed += time.perf_counter() - step_started

                                    task_meta = {
                                        "win": win,
                                        "raw_img": raw_img,
                                        "group": group,
                                    }
                                    if combined_mode:
                                        combined_entries.append(
                                            {
                                                "preprocessed_payload": prep_payload,
                                                "raw_payload": raw_payload,
                                                "filters": group["filters"],
                                                "use_preprocess": self._use_preprocess,
                                            }
                                        )
                                        combined_meta.append(task_meta)
                                    else:
                                        step_started = time.perf_counter()
                                        fut = self._ocr_pool.submit(
                                            _ocr_pool_task,
                                            prep_payload,
                                            raw_payload,
                                            group["filters"],
                                            self._use_preprocess,
                                        )
                                        dispatch_elapsed += time.perf_counter() - step_started
                                        future_map[fut] = task_meta
                                        dispatched_count += 1
                                except Exception as e:
                                    dispatch_elapsed += time.perf_counter() - step_started
                                    self._log(f"[OCR] Failed to dispatch process task for PID {win.pid}: {e}")
                                    if isinstance(e, BrokenProcessPool):
                                        self._restart_ocr_pool()

                        if combined_mode and combined_entries and self._ocr_pool:
                            combined_batches = _chunk_combined_ocr_items(
                                combined_entries,
                                combined_meta,
                                configured_batch_limit,
                            )
                            for batch_index, (batch_entries, batch_meta) in enumerate(
                                combined_batches,
                                start=1,
                            ):
                                step_started = time.perf_counter()
                                try:
                                    fut = self._ocr_pool.submit(_ocr_combined_pool_task, batch_entries)
                                    future_map[fut] = {"batch_meta": batch_meta}
                                    dispatched_count += len(batch_entries)
                                    combined_batch_count += 1
                                except Exception as e:
                                    self._log(
                                        f"[OCR] Failed to dispatch combined OCR batch "
                                        f"{batch_index}/{len(combined_batches)}: {e}"
                                    )
                                    if isinstance(e, BrokenProcessPool):
                                        self._restart_ocr_pool()
                                finally:
                                    dispatch_elapsed += time.perf_counter() - step_started

                        self._log(
                            f"[Loop {loop_idx}] Captured {captured_count} window image(s) "
                            f"in {_format_step_duration(capture_elapsed)}."
                        )
                        self._log(
                            f"[Loop {loop_idx}] Prepared OCR tasks in "
                            f"{_format_step_duration(payload_elapsed + preprocess_elapsed + frame_compare_elapsed + dispatch_elapsed)} "
                            f"(payload {_format_step_duration(payload_elapsed)}, "
                            f"preprocess {_format_step_duration(preprocess_elapsed)}, "
                            f"frame compare {_format_step_duration(frame_compare_elapsed)}, "
                            f"empty masks {skipped_empty_mask}, "
                            f"dispatch {dispatched_count} image task(s)"
                            f"{f' in {combined_batch_count} combined batch(es)' if combined_mode else ''} "
                            f"in {_format_step_duration(dispatch_elapsed)})."
                        )

                        pending = set(future_map.keys())
                        ocr_wait_elapsed = 0.0
                        result_handle_elapsed = 0.0
                        while pending:
                            if self._stop_event.is_set():
                                for fut in list(pending):
                                    try:
                                        fut.cancel()
                                    except Exception:
                                        pass
                                break

                            step_started = time.perf_counter()
                            done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                            ocr_wait_elapsed += time.perf_counter() - step_started
                            if not done:
                                continue

                            for fut in done:
                                step_started = time.perf_counter()
                                try:
                                    meta = future_map.get(fut) or {}
                                    if self._stop_event.is_set():
                                        break
                                    try:
                                        future_result = fut.result()
                                    except Exception as e:
                                        win = meta.get("win")
                                        pid_label = f" for PID {win.pid}" if win is not None else ""
                                        self._log(f"[OCR] Worker process error{pid_label}: {e}")
                                        if isinstance(e, BrokenProcessPool):
                                            self._restart_ocr_pool()
                                        continue

                                    batch_meta = meta.get("batch_meta")
                                    if isinstance(batch_meta, list):
                                        if not isinstance(future_result, dict):
                                            continue
                                        fallback_error = str(future_result.get("fallback_error") or "").strip()
                                        if fallback_error:
                                            self._log(
                                                f"[OCR] Combined batch fell back to separate reads: {fallback_error}"
                                            )
                                        batch_results = future_result.get("results")
                                        if not isinstance(batch_results, list):
                                            self._log("[OCR] Combined OCR worker returned no result list.")
                                            continue
                                        if len(batch_results) != len(batch_meta):
                                            self._log(
                                                "[OCR] Combined OCR worker returned "
                                                f"{len(batch_results)} result(s) for {len(batch_meta)} task(s)."
                                            )
                                        result_items = list(zip(batch_meta, batch_results))
                                    else:
                                        result_items = [(meta, future_result)]

                                    for item_meta, result in result_items:
                                        win = item_meta.get("win") if isinstance(item_meta, dict) else None
                                        raw_img = item_meta.get("raw_img") if isinstance(item_meta, dict) else None
                                        group = (item_meta.get("group") or {}) if isinstance(item_meta, dict) else {}
                                        if win is None or raw_img is None or not isinstance(result, dict):
                                            continue
                                        if result.get("error"):
                                            self._log(f"[OCR] Worker reported error for PID {win.pid}: {result['error']}")
                                            continue

                                        if getattr(self, "_log_ocr_text", False):
                                            text = str(result.get("text") or "").strip()
                                            if text:
                                                self._log(
                                                    f"[OCR TEXT] PID {win.pid} "
                                                    f"[{group.get('label') or 'group'}]:\n{text}"
                                                )

                                        matches = result.get("matches")
                                        if not isinstance(matches, list):
                                            legacy_match = result.get("match")
                                            matches = [legacy_match] if isinstance(legacy_match, dict) else []
                                        for match in matches:
                                            if not isinstance(match, dict):
                                                continue
                                            spec = self._filter_spec_by_id(str(match.get("id") or ""))
                                            if spec:
                                                if self._handle_filter_match(spec, win.pid, raw_img):
                                                    self._set_filter_cooldown(win.pid, spec)
                                        processed_count += 1
                                finally:
                                    result_handle_elapsed += time.perf_counter() - step_started

                        self._log(
                            f"[Loop {loop_idx}] Completed OCR for {processed_count} image(s) "
                            f"(skipped {skipped_similar} similar) in "
                            f"{_format_step_duration(ocr_wait_elapsed + result_handle_elapsed)}."
                        )
                        self._log(
                            f"[Loop {loop_idx}] OCR wait/result handling took "
                            f"{_format_step_duration(ocr_wait_elapsed + result_handle_elapsed)} "
                            f"(wait {_format_step_duration(ocr_wait_elapsed)}, "
                            f"handle {_format_step_duration(result_handle_elapsed)})."
                        )
                    else:
                        self._log(f"[Loop {loop_idx}] No windows eligible for capture this cycle.")

                    elapsed = time.perf_counter() - loop_started
                    self._log(f"[Loop {loop_idx}] Active loop time {_format_step_duration(elapsed)}.")
                    target_delay = max(0.0, getattr(self, "_batch_delay_seconds", 1.0))
                    if elapsed < target_delay:
                        sleep_for = max(0.0, target_delay - elapsed)
                        self._log(
                            f"[Loop {loop_idx}] Sleeping {sleep_for:.2f}s to throttle loop "
                            f"(target {target_delay:.2f}s)."
                        )
                        self._stop_event.wait(timeout=sleep_for)
                except Exception as e:
                    self._log(f"[OCR] Loop error: {e}")
                    self._log(traceback.format_exc())
                    self._stop_event.wait(timeout=1.0)

            self._log("OCR worker stopped.")
            self.status_signal.emit("stopped")
        finally:
            self._stop_event.set()
            self._stop_window_enumerator()
            self._shutdown_ocr_pool()
            self._shutdown_send_pool()

    # ---------------------- detection helpers ---------------------
    def _preprocess_image(self, image: Image.Image, filter_specs: List[Dict[str, Any]]) -> Image.Image:
        if not self._use_preprocess:
            return image

        return _prepare_filter_ocr_image(image, filter_specs)

    def _handle_detection(self, merchant: str, pid: int, raw_img: Image.Image) -> None:
        ctx = self._context_provider(pid) if self._context_provider else {}
        try:
            uid = str(ctx.get("user_id") or "").strip()
            if uid and merchant:
                self.merchant_signal.emit(uid, str(merchant))
            elif merchant:
                self._log(
                    f"[OCR->FoundStats] {merchant.upper()} detected in PID {pid}, but PID could not be mapped to user_id."
                )
        except Exception:
            pass
        username = ctx.get("username") or f"PID {pid}"
        owner_raw = str(ctx.get("owner") or "").strip()
        owner_known = bool(owner_raw)
        owner = owner_raw or username
        server_label = str(ctx.get("server_label") or "").strip() or "Unknown"
        ps_link = str(ctx.get("ps_link") or "").strip()

        self._log(f"[DETECT] {merchant.upper()} detected in PID {pid} ({username}).")
        self._send_webhook(merchant, pid, username, owner, owner_known, server_label, ps_link, raw_img)

    def _send_webhook(
        self,
        merchant: str,
        pid: int,
        username: str,
        owner: str,
        owner_known: bool,
        server_label: str,
        ps_link: str,
        raw_img: Image.Image,
    ) -> None:
        url = (self._ms_cfg or {}).get("merchant_webhook", "").strip()
        if not url:
            self._log("[Webhook] Merchant webhook is not configured.")
            return
        if bool(getattr(self, "_skip_webhook_unknown_context", False)):
            server_unknown = (not server_label) or server_label.strip().lower() == "unknown"
            owner_unknown = (not owner_known) or (str(owner).strip().lower() == "unknown")
            ps_unknown = not bool(ps_link)
            if server_unknown or owner_unknown or ps_unknown:
                self._log("[Webhook] Skipping OCR webhook; owner or private server unknown.")
                return

        ping_map = {
            "jester": (self._ms_cfg or {}).get("jester_ping", ""),
            "mari": (self._ms_cfg or {}).get("mari_ping", ""),
            "rin": (self._ms_cfg or {}).get("rin_ping", ""),
        }

        ts = datetime.now(timezone.utc)
        ts_epoch = int(ts.timestamp())
        ts_full = f"<t:{ts_epoch}:D> - <t:{ts_epoch}:T>"
        ts_rel = f"<t:{ts_epoch}:R>"

        emojis = {"jester": "\U0001f0cf", "mari": "\U0001f6cd", "rin": "\U0001f98a"}
        colors = {"jester": 0xA352FF, "mari": 0xFF82AB, "rin": 0xFF9F1C}
        title = f"{emojis.get(merchant, '\U0001f4e3')} {merchant.title()} Has Arrived!"

        desc = (
            f"**Owner:** `{owner}`\n"
            f"**Detected by:** `{username}`\n"
            f"**Detected At:** {ts_full} ({ts_rel})\n"
            f"**Private Server:** " + (f"[Private Server Link]({ps_link})" if ps_link else "`N/A`")
        )

        embed = {
            "title": title,
            "description": desc,
            "color": colors.get(merchant, 0x7289DA),
            "timestamp": ts.isoformat(),
            "footer": {"text": f"{APP_FOOTER} - {server_label}"},
            "image": {"url": "attachment://chat.png"},
        }

        payload = {
            "content": ping_map.get(merchant, ""),
            "embeds": [embed],
        }

        files = {
            "chat.png": ("chat.png", _image_bytes(raw_img), "image/png"),
        }

        def _send() -> None:
            try:
                requests.post(url, data={"payload_json": json.dumps(payload)}, files=files, timeout=10)
            except Exception as e:
                self._log(f"[Webhook] Error posting detection: {e}")

        self._send_pool.submit(_send)

    @staticmethod
    def _is_truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("1", "true", "yes", "y", "on"):
                return True
            if v in ("0", "false", "no", "n", "off", ""):
                return False
        return bool(value)

    def _format_filter_message(self, spec: Dict[str, Any], pid: int, ctx: Dict[str, Any]) -> str:
        template = str(spec.get("webhook_message", "") or "").strip()
        if not template:
            template = "{filter} detected in {username} (PID {pid})"

        values = {
            "filter": str(spec.get("name", "") or "").strip(),
            "pid": int(pid),
            "user_id": str((ctx or {}).get("user_id") or "").strip(),
            "username": str((ctx or {}).get("username") or f"PID {pid}"),
            "owner": str((ctx or {}).get("owner") or "").strip(),
            "server_label": str((ctx or {}).get("server_label") or "").strip(),
            "ps_link": str((ctx or {}).get("ps_link") or "").strip(),
        }
        discord_ping = str(
            (ctx or {}).get("discord_ping")
            or (ctx or {}).get("discord_user_ping")
            or (ctx or {}).get("discord_mention")
            or ""
        ).strip()
        values["discord_ping"] = discord_ping
        values["discord_user_ping"] = discord_ping
        values["discord_mention"] = discord_ping

        class _SafeFormatDict(dict):
            def __missing__(self, key):
                return "{" + str(key) + "}"

        try:
            return template.format_map(_SafeFormatDict(values))
        except Exception:
            return template

    def _send_custom_filter_webhook(
        self,
        spec: Dict[str, Any],
        pid: int,
        ctx: Dict[str, Any],
        raw_img: Optional[Image.Image] = None,
    ) -> None:
        url = str(spec.get("webhook_url", "") or "").strip()
        if not url:
            self._log(f"[Webhook] Filter '{spec.get('name', '')}' detected in PID {pid}, but no webhook URL is configured.")
            return

        payload = {"content": self._format_filter_message(spec, pid, ctx)}
        send_screenshot = bool(spec.get("send_screenshot", False))

        def _send() -> None:
            try:
                if send_screenshot and raw_img is not None:
                    files = {
                        "chat.png": ("chat.png", _image_bytes(raw_img), "image/png"),
                    }
                    requests.post(url, data={"payload_json": json.dumps(payload)}, files=files, timeout=10)
                else:
                    requests.post(url, json=payload, timeout=10)
            except Exception as e:
                self._log(f"[Webhook] Error posting filter detection: {e}")

        self._send_pool.submit(_send)

    def _emit_filter_alert(self, spec: Dict[str, Any], filter_name: str) -> None:
        if not bool((spec or {}).get("repeat_alert_sound", False)):
            return
        try:
            self.filter_alert_signal.emit(str(filter_name or str(spec.get("name") or "Filter")))
        except Exception:
            pass

    def _handle_filter_match(self, spec: Dict[str, Any], pid: int, raw_img: Image.Image) -> bool:
        behavior = str(spec.get("behavior", "") or "").strip().lower()
        filter_name = str(spec.get("name", "") or "").strip() or str(spec.get("id", "") or "Filter")
        filter_id = str(spec.get("id", "") or "").strip()
        ctx = self._context_provider(pid) if self._context_provider else {}

        if behavior == "merchant":
            self._emit_filter_match_signal(pid, filter_id, filter_name)
            merchant = MERCHANT_FILTER_IDS.get(str(spec.get("id") or "").strip(), filter_name.lower())
            self._handle_detection(merchant, pid, raw_img)
            self._emit_filter_alert(spec, filter_name)
            return True

        if behavior == "verification_cap":
            uid = str((ctx or {}).get("user_id") or "").strip()
            username = str((ctx or {}).get("username") or uid or f"PID {pid}")
            if not uid:
                self._log(f"[Verification] {filter_name} matched in PID {pid} ({username}), but the PID is not mapped to a user.")
                return False
            if self._is_truthy((ctx or {}).get("is_cap")):
                return False
            if self._is_truthy((ctx or {}).get("has_user_log")):
                return False
            self._log(f"[Verification] {filter_name} matched in PID {pid} ({username}); marking CAP.")
            try:
                self.verification_cap_signal.emit(uid)
            except Exception:
                pass
            self._emit_filter_match_signal(pid, filter_id, filter_name)
            if str(spec.get("webhook_url", "") or "").strip():
                self._send_custom_filter_webhook(spec, pid, ctx, raw_img)
            self._emit_filter_alert(spec, filter_name)
            return True

        username = str((ctx or {}).get("username") or f"PID {pid}")
        self._log(f"[DETECT] {filter_name} detected in PID {pid} ({username}).")
        self._emit_filter_match_signal(pid, filter_id, filter_name)
        self._send_custom_filter_webhook(spec, pid, ctx, raw_img)
        self._emit_filter_alert(spec, filter_name)
        return True

    # ------------------------- scheduling -------------------------
    def _select_windows(self, windows) -> List[Any]:
        total = len(windows)
        if total == 0:
            return []

        if _normalize_ocr_processing_mode(getattr(self, "_processing_mode", "separate")) == "combined":
            limit = total
        else:
            limit = min(self._max_captures_per_second, total)
        start_idx = self._capture_rr_index % total
        idx = start_idx
        seen = 0
        work_list: List[Any] = []

        while seen < total and len(work_list) < limit:
            win = windows[idx]
            pid = int(getattr(win, "pid", 0) or 0)
            if self._pid_has_eligible_filters(pid) or self._pid_has_eligible_verification_filters(pid):
                work_list.append(win)
            idx = (idx + 1) % total
            seen += 1

        self._capture_rr_index = idx
        return work_list

    def _filter_spec_by_id(self, filter_id: str) -> Optional[Dict[str, Any]]:
        fid = str(filter_id or "").strip()
        for spec in self._filters or []:
            if str(spec.get("id") or "").strip() == fid:
                return spec
        return None

    def _shared_area_roi(self, area_id: str) -> Optional[Tuple[float, float, float, float]]:
        wanted = str(area_id or "").strip()
        if not wanted:
            return None
        if wanted == "chat":
            return self._roi
        area = (getattr(self, "_shared_area_map", None) or {}).get(wanted) or {}
        return _roi_from_cfg(area.get("roi") or {})

    def _effective_filter_roi(self, spec: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
        area_id = _filter_shared_area_id(spec)
        if area_id:
            return self._shared_area_roi(area_id)
        return _roi_from_cfg(spec.get("roi") or {})

    def _pid_context(self, pid: int) -> Dict[str, Any]:
        if not self._context_provider:
            return {}
        try:
            return self._context_provider(int(pid or 0)) or {}
        except Exception:
            return {}

    def _filter_targets_pid(self, pid: int, spec: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> bool:
        ctx = ctx or self._pid_context(pid)
        uid = str((ctx or {}).get("user_id") or "").strip()
        if bool(getattr(self, "_only_mapped_pids", False)) and not uid:
            return False

        allowed_user_ids = _normalize_filter_user_ids((spec or {}).get("user_ids", None))
        if allowed_user_ids is None:
            return True
        user_set = set(allowed_user_ids)
        if _normalize_user_filter_mode((spec or {}).get("user_filter_mode", "whitelist")) == "blacklist":
            if not user_set:
                return True
            return not (bool(uid) and uid in user_set)
        if not user_set:
            return False
        return bool(uid) and uid in user_set

    def _pid_has_eligible_filters(self, pid: int) -> bool:
        return bool(self._build_capture_groups(pid))

    def _verification_filter_specs(
        self,
        pid: int,
        ctx: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        ctx = ctx or self._pid_context(pid)
        specs: List[Dict[str, Any]] = []
        for spec in self._filters or []:
            if not bool(spec.get("enabled", True)):
                continue
            if str(spec.get("behavior", "") or "").strip().lower() != "verification_cap":
                continue
            if not self._filter_targets_pid(pid, spec, ctx):
                continue
            if self._effective_filter_roi(spec) is None:
                continue
            specs.append(spec)
        specs.sort(key=lambda item: str(item.get("name", "")).lower())
        return specs

    def _pid_has_eligible_verification_filters(self, pid: int) -> bool:
        return bool(self._verification_filter_specs(pid))

    def _build_capture_groups(self, pid: int) -> List[Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        ctx = self._pid_context(pid)
        for spec in self._filters or []:
            if not bool(spec.get("enabled", True)):
                continue
            if str(spec.get("behavior", "") or "").strip().lower() == "verification_cap":
                continue
            if not self._filter_targets_pid(pid, spec, ctx):
                continue
            if self._filter_on_cooldown(pid, spec):
                continue
            roi = self._effective_filter_roi(spec)
            if roi is None:
                continue
            capture_key = self._roi_cache_key(roi, use_chat_area=_filter_uses_chat_area(spec))
            cache_key = capture_key
            if _filter_solo_ocr(spec):
                filter_key = str(spec.get("id") or spec.get("name") or "filter").strip() or "filter"
                cache_key = f"{cache_key}|solo:{filter_key}"
            group = groups.setdefault(
                cache_key,
                {
                    "roi": roi,
                    "filters": [],
                    "cache_key": cache_key,
                    "capture_key": capture_key,
                    "solo_ocr": bool(_filter_solo_ocr(spec)),
                },
            )
            group["filters"].append(spec)

        out: List[Dict[str, Any]] = []
        for group in groups.values():
            names = [str(spec.get("name", "") or "").strip() for spec in group.get("filters", [])]
            group["label"] = ", ".join(name for name in names if name) or "filters"
            out.append(group)
        out.sort(
            key=lambda item: (
                str(item.get("capture_key") or item.get("cache_key") or ""),
                1 if bool(item.get("solo_ocr", False)) else 0,
                str(item.get("label", "")).lower(),
            )
        )
        return out

    def _filter_group_key(self, spec: Dict[str, Any]) -> str:
        return str(spec.get("cooldown_group") or spec.get("id") or spec.get("name") or "filter").strip()

    def _filter_on_cooldown(self, pid: int, spec: Dict[str, Any]) -> bool:
        pid_key = int(pid or 0)
        group_key = self._filter_group_key(spec)
        next_allowed = (self._cooldowns.get(pid_key) or {}).get(group_key)
        return bool(next_allowed and time.time() < next_allowed)

    def _set_filter_cooldown(self, pid: int, spec: Dict[str, Any]) -> None:
        pid_key = int(pid or 0)
        group_key = self._filter_group_key(spec)
        try:
            cooldown = max(0.0, float(spec.get("cooldown_seconds", 600) or 0.0))
        except Exception:
            cooldown = 600.0
        self._cooldowns.setdefault(pid_key, {})[group_key] = time.time() + cooldown

    @staticmethod
    def _roi_cache_key(roi: Tuple[float, float, float, float], *, use_chat_area: bool = False) -> str:
        rx, ry, rw, rh = roi
        prefix = "chat" if use_chat_area else "roi"
        return f"{prefix}:{rx:.5f}:{ry:.5f}:{rw:.5f}:{rh:.5f}"

    @staticmethod
    def _frame_cache_key(pid: int, group_key: str) -> str:
        return f"{int(pid or 0)}:{group_key}"

    def _skip_ocr_for_similar_frame(self, cache_key: str, img_for_ocr: Image.Image) -> Tuple[bool, Optional[float]]:
        """
        Compare the current OCR frame to the last frame for this capture group.
        Returns (skip, diff_percent). The internal last-frame cache is updated
        regardless so consecutive frames are compared.
        """
        tol = getattr(self, "_frame_diff_tolerance", None)
        if tol is None or float(tol) < 0:
            return False, None

        try:
            current_hash = compute_frame_hash(img_for_ocr, hash_size=self._frame_hash_size)
        except Exception:
            return False, None

        last_hash = self._last_frame_hash_by_key.get(str(cache_key))
        self._last_frame_hash_by_key[str(cache_key)] = current_hash
        if last_hash is None:
            return False, None

        diff_pct = frame_hash_diff_percent(last_hash, current_hash, hash_size=self._frame_hash_size)
        return (diff_pct <= float(tol)), diff_pct

    # ------------------------ configuration -----------------------
    def _apply_cfg(self, ocr_settings: Dict[str, Any], ms_settings: Dict[str, Any]) -> None:
        self._ocr_cfg = ocr_settings or {}
        self._ms_cfg = ms_settings or {}

        prev_filters = getattr(self, "_filters", None)
        prev_roi = getattr(self, "_roi", None)
        prev_shared_areas = getattr(self, "_shared_areas", None)
        prev_use_preprocess = getattr(self, "_use_preprocess", None)
        prev_device_id = getattr(self, "_device_id", None)
        prev_force_cpu = getattr(self, "_force_cpu", None)
        prev_verification_interval = getattr(self, "_verification_check_interval", None)

        self._filters = _filters_from_cfg(self._ocr_cfg)
        self._roi = _roi_from_cfg(self._ocr_cfg.get("roi") or {})
        self._shared_areas = _shared_areas_from_cfg(self._ocr_cfg)
        self._shared_area_map = {str(item.get("id") or "").strip(): item for item in self._shared_areas}
        self._only_mapped_pids = bool(self._ocr_cfg.get("only_mapped_pids", False))
        self._workers = max(1, int(self._ocr_cfg.get("workers", 1) or 1))
        self._max_captures_per_second = max(1, int(self._ocr_cfg.get("max_captures_per_second", 20) or 1))
        self._processing_mode = _normalize_ocr_processing_mode(
            self._ocr_cfg.get("processing_mode", "separate")
        )
        try:
            self._batch_delay_seconds = max(0.0, float(self._ocr_cfg.get("batch_delay_seconds", 1.0)))
        except Exception:
            self._batch_delay_seconds = 1.0
        try:
            self._window_enum_interval_seconds = max(
                0.05,
                min(5.0, float(self._ocr_cfg.get("window_enum_interval_seconds", _WINDOW_ENUM_INTERVAL_SECONDS))),
            )
        except Exception:
            self._window_enum_interval_seconds = _WINDOW_ENUM_INTERVAL_SECONDS
        try:
            self._verification_check_interval = max(
                0.5,
                float(self._ocr_cfg.get("verification_check_interval", 2.0)),
            )
        except Exception:
            self._verification_check_interval = 2.0
        self._use_preprocess = bool(self._ocr_cfg.get("use_preprocess", True))
        self._device_id, self._force_cpu = _parse_device_id(self._ocr_cfg.get("device_id"))
        self._log_ocr_text = bool(self._ocr_cfg.get("log_ocr_text", False))
        self._log_loop = bool(self._ocr_cfg.get("log_loop", True))
        skip_flag = None
        if isinstance(self._ms_cfg, dict) and "skip_webhook_unknown_context" in self._ms_cfg:
            skip_flag = bool(self._ms_cfg.get("skip_webhook_unknown_context", False))
        if skip_flag is None:
            skip_flag = bool(self._ocr_cfg.get("skip_webhook_unknown_context", False))
        self._skip_webhook_unknown_context = bool(skip_flag)

        try:
            tol = float(self._ocr_cfg.get("frame_diff_tolerance", 2.0))
        except Exception:
            tol = 2.0
        self._frame_diff_tolerance = max(0.0, min(100.0, tol))

        if (
            prev_filters != self._filters
            or prev_roi != self._roi
            or prev_shared_areas != self._shared_areas
            or prev_use_preprocess != self._use_preprocess
        ):
            try:
                self._last_frame_hash_by_key.clear()
            except Exception:
                pass
        if (
            prev_filters != self._filters
            or prev_shared_areas != self._shared_areas
            or prev_verification_interval != self._verification_check_interval
        ):
            try:
                self._verification_next_check_by_pid.clear()
            except Exception:
                pass
        if (
            (prev_device_id != self._device_id or prev_force_cpu != self._force_cpu)
            and getattr(self, "_ocr_pool", None) is not None
        ):
            try:
                self._log("[OCR] Device change detected; restart OCR to apply.")
            except Exception:
                pass

    def _shutdown_ocr_pool(self) -> None:
        if self._ocr_pool:
            try:
                self._ocr_pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self._ocr_pool.shutdown(wait=False)
            except Exception:
                pass
            self._ocr_pool = None

    def _restart_ocr_pool(self) -> bool:
        if self._stop_event.is_set():
            return False
        self._log("[OCR] process pool broke; restarting OCR pool.")
        self._shutdown_ocr_pool()
        try:
            self._ocr_pool = ProcessPoolExecutor(
                max_workers=1,
                mp_context=self._mp_ctx,
                initializer=_init_pool_reader,
                initargs=(self._device_id, self._force_cpu),
            )
            return True
        except Exception as e:
            self._ocr_pool = None
            self._log(f"[OCR] Failed to restart OCR process pool: {e}")
            return False

    def _shutdown_send_pool(self) -> None:
        pool = getattr(self, "_send_pool", None)
        if pool is None:
            return
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            try:
                pool.shutdown(wait=False)
            except Exception:
                pass
        except Exception:
            pass
        try:
            self._send_pool = None
        except Exception:
            pass

    def _run_text_ocr_in_pool(self, image: Image.Image) -> str:
        pool = getattr(self, "_ocr_pool", None)
        if pool is None:
            raise RuntimeError("OCR process pool is not running")
        try:
            result = pool.submit(_ocr_text_task, _image_payload(image)).result()
        except Exception as e:
            if isinstance(e, BrokenProcessPool):
                self._restart_ocr_pool()
            raise
        if not isinstance(result, dict):
            return ""
        if result.get("error"):
            raise RuntimeError(str(result.get("error")))
        return str(result.get("text") or "")

    def _emit_filter_match_signal(self, pid: int, filter_id: str, filter_name: str) -> None:
        try:
            self.filter_match_signal.emit(int(pid), str(filter_id or "").strip(), str(filter_name or "").strip())
        except Exception:
            pass

    @staticmethod
    def _contains_start_puzzle_text(text: str) -> bool:
        raw = str(text or "")
        if not raw:
            return False
        if START_PUZZLE_RE.search(raw):
            return True
        squashed = re.sub(r"[^a-z0-9]+", "", raw.lower())
        return "startpuzzle" in squashed

    def _run_verification_checks(self, windows: List[Any]) -> None:
        if self._ocr_pool is None:
            return

        now = time.time()
        for win in windows:
            if self._stop_event.is_set():
                return

            pid = int(getattr(win, "pid", 0) or 0)
            if pid <= 0:
                continue

            next_allowed = float(self._verification_next_check_by_pid.get(pid, 0.0) or 0.0)
            if now < next_allowed:
                continue

            ctx = self._pid_context(pid)
            uid = str((ctx or {}).get("user_id") or "").strip()
            if not uid:
                self._verification_next_check_by_pid[pid] = now + self._verification_check_interval
                continue

            if self._is_truthy((ctx or {}).get("is_cap")):
                self._verification_next_check_by_pid[pid] = now + 30.0
                continue

            if self._is_truthy((ctx or {}).get("has_user_log")):
                self._verification_next_check_by_pid[pid] = now + self._verification_check_interval
                continue

            verification_specs = self._verification_filter_specs(pid, ctx)
            if not verification_specs:
                continue

            full_img = capture_window_image(win.hwnd)
            if full_img is None:
                self._verification_next_check_by_pid[pid] = now + self._verification_check_interval
                continue

            matched = False
            for spec in verification_specs:
                if self._stop_event.is_set():
                    return

                roi = self._effective_filter_roi(spec)
                if roi is None:
                    continue

                img = crop_image_with_roi(full_img, roi)

                try:
                    prepared = (
                        _prepare_filter_ocr_image_with_stats(img, [spec])
                        if self._use_preprocess
                        else PreparedOCRImage(img)
                    )
                    if _prepared_has_empty_color_mask(prepared):
                        continue
                    text = self._run_text_ocr_in_pool(prepared.image)
                except Exception as e:
                    self._log(f"[Verification] OCR error for PID {pid}: {e}")
                    continue

                target_text = str(spec.get("target_text", "") or "").strip()
                score = _score_text_against_target(text, target_text) if target_text else 0.0
                if self._contains_start_puzzle_text(text):
                    score = max(score, 1.0)

                if score < DEFAULT_OCR_MATCH_THRESHOLD:
                    continue

                if self._handle_filter_match(spec, pid, img):
                    self._verification_next_check_by_pid[pid] = now + 30.0
                    matched = True
                    break

            if not matched:
                self._verification_next_check_by_pid[pid] = now + self._verification_check_interval

    # ---------------------------- misc ----------------------------
    def _log(self, msg: str) -> None:
        clean = msg.strip()
        if not clean:
            return
        if not getattr(self, "_log_loop", True):
            if clean.startswith("[Loop "):
                return
            if clean == "No Roblox windows found.":
                return
        if clean == self._last_log:
            return
        self._last_log = clean
        self.log_signal.emit(clean)

    def __del__(self) -> None:  # pragma: no cover - destructor safety
        try:
            self._shutdown_send_pool()
        except Exception:
            pass
        try:
            self._shutdown_ocr_pool()
        except Exception:
            pass
