"""
Core backend logic for Sollumz DDS Prep.
Contains discovery/classification (from Sollumz materials),
power-of-two rules, role-based format resolution, and csdevcompress execution.
"""

import os
import re
import platform
import subprocess
import bpy

# ==============================================================================
# BACKEND (CSDEVCOMPRESS)
# ==============================================================================

FORMAT_MAP = {
    "DXT1 (BC1)": "-bc1",
    "DXT5 (BC3)": "-bc3",
    "BC5": "-bc5",
    "BC7": "-bc7",
    "BC1": "-bc1",
    "BC3": "-bc3",
    "DXT1": "-bc1",
    "DXT5": "-bc3",
    "-bc1": "-bc1",
    "-bc3": "-bc3",
    "-bc5": "-bc5",
    "-bc7": "-bc7",
}

DISPLAY_FORMATS = [
    "DXT1 (BC1)",
    "DXT5 (BC3)",
    "BC5",
    "BC7",
]

FORMAT_TOOLTIPS = {
    "Auto (By Role)": "Automatically select BC3 for alpha, and BC1 otherwise (including normal maps)",
    "DXT1 (BC1)": "No alpha - best for opaque diffuse/color maps, normal maps, and masks",
    "DXT5 (BC3)": "Interpolated alpha - for diffuse textures with transparency",
    "BC5": "Two channel - optional high-quality normal/bump maps (BumpSampler)",
    "BC7": "High quality, full alpha - best for high-detail specular/normal/diffuse maps",
}

CREATE_NO_WINDOW = 0x08000000 if platform.system() == "Windows" else 0


def is_available(exe_path: str) -> bool:
    return bool(exe_path) and os.path.isfile(exe_path)


def build_command(exe_path, comp_format_display, input_file, output_file, mipmap_options, is_color_map):
    final_flag = FORMAT_MAP.get(comp_format_display)
    if not final_flag:
        fmt_u = str(comp_format_display).upper()
        if "BC3" in fmt_u or "DXT5" in fmt_u:
            final_flag = "-bc3"
        elif "BC5" in fmt_u or "ATI2" in fmt_u:
            final_flag = "-bc5"
        elif "BC7" in fmt_u:
            final_flag = "-bc7"
        else:
            final_flag = "-bc1"

    command = [exe_path, final_flag]

    if not mipmap_options.get("generate", True):
        command.append("-nomips")
    else:
        # Prevent csdevcompress from generating 1x1 or 2x2 mipmaps which can cause rendering issues
        command.extend(["-min-mip-size", "4"])
        # High quality mipmap downsampling filter
        command.extend(["-mipfilter", "mitchell-netravali"])

    if is_color_map:
        command.append("-color")
    else:
        command.append("-no-mip-gamma-correct")

    command.extend([input_file, output_file])
    return command


def run(exe_path, comp_format_display, input_file, output_file, mipmap_options, is_color_map, active_procs=None, proc_lock=None, timeout=180):
    if not is_available(exe_path):
        raise RuntimeError(f"Compression engine not found: {exe_path}")

    command = build_command(exe_path, comp_format_display, input_file, output_file, mipmap_options, is_color_map)

    if not os.path.isfile(input_file):
        raise RuntimeError(f"Input file does not exist: {input_file}")

    proc = subprocess.Popen(
        command,
        creationflags=CREATE_NO_WINDOW,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if active_procs is not None:
        if proc_lock is not None:
            with proc_lock:
                active_procs.add(proc)
        else:
            active_procs.add(proc)

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError(f"Compression timed out after {timeout}s: {os.path.basename(input_file)}")
    finally:
        if active_procs is not None:
            if proc_lock is not None:
                with proc_lock:
                    active_procs.discard(proc)
            else:
                active_procs.discard(proc)

    if proc.returncode != 0:
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(
            f"csdevcompress exited {proc.returncode}: {detail or '(no output)'}\n"
            f"Command: {' '.join(command)}"
        )


# ==============================================================================
# CLASSIFY / DISCOVER
# ==============================================================================

_BLENDER_SUFFIX_RE = re.compile(r"\.\d{3}$")


def strip_blender_suffix(name: str) -> str:
    return _BLENDER_SUFFIX_RE.sub("", name)


def clean_filename_stem(name: str) -> str:
    """Strip Blender .001 suffixes, extensions, and invalid path characters."""
    base = strip_blender_suffix(name)
    base, ext = os.path.splitext(base)
    # In case of "texture.png.001" where strip_blender_suffix left "texture.png"
    if ext.lower() in (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff", ".dds", ".exr", ".webp"):
        base = strip_blender_suffix(base)
    else:
        base = base + ext
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
    return cleaned or "texture"


def guess_role_from_name(name: str) -> str:
    """Unified heuristic to classify texture sampler role from a name."""
    lower_name = strip_blender_suffix(name).lower()
    if any(x in lower_name for x in ("bump", "norm", "nrm", "_n.", "_n_")) or lower_name.endswith("_n"):
        return "BumpSampler"
    if any(x in lower_name for x in ("spec", "gloss", "orm", "rough", "metal", "_s.", "_s_")) or lower_name.endswith("_s"):
        return "SpecSampler"
    if "detail" in lower_name:
        return "DetailSampler"
    if any(x in lower_name for x in ("tint", "palette", "pal")):
        return "TintPaletteSampler"
    return "DiffuseSampler"


def is_color_role(role: str) -> bool:
    """Return True if the role represents a color/albedo texture."""
    r = strip_blender_suffix(role).lower()
    if any(x in r for x in ("bump", "norm", "spec", "gloss", "detail", "orm", "rough")):
        return False
    if any(x in r for x in ("diffuse", "color", "albedo", "tint", "palette")):
        return True
    clean_base = re.sub(r"\.[a-zA-Z0-9]+$", "", r)
    if clean_base.endswith(("_d", "_diff", "_alb", "_col", "_basecolor")):
        return True
    return False


def ensure_image_loaded(image) -> None:
    """Ensure Blender has initialized width, height, and depth for external images."""
    if image is None:
        return
    if image.size[0] == 0 and image.filepath:
        try:
            abs_path = bpy.path.abspath(image.filepath)
            if os.path.isfile(abs_path):
                image.reload()
        except Exception:
            pass


def _get_sollumz_texture_name(node, image):
    # In Sollumz, node.name or node.label is the actual sampler slot (e.g. 'DiffuseSampler')
    node_name = strip_blender_suffix(node.label or node.name).strip()
    if any(k in node_name.lower() for k in ("sampler", "diffuse", "bump", "normal", "spec", "detail", "tint")):
        return node_name

    # If node has explicit sampler tag in sollumz_texture_name
    raw_role = getattr(node, "sollumz_texture_name", "") or ""
    raw_role = strip_blender_suffix(raw_role).strip()
    if raw_role and any(k in raw_role.lower() for k in ("sampler", "diffuse", "bump", "normal", "spec", "detail", "tint")):
        return raw_role

    # Guess from image filename (e.g. 'Material.002_d.png' -> DiffuseSampler)
    if image:
        return guess_role_from_name(image.name)

    return raw_role or "DiffuseSampler" 


def _iter_image_nodes(node_tree, visited=None):
    """Yield ShaderNodeTexImage nodes recursively, including inside node groups."""
    if node_tree is None:
        return
    if visited is None:
        visited = set()
    if node_tree in visited:
        return
    visited.add(node_tree)

    for node in node_tree.nodes:
        if isinstance(node, bpy.types.ShaderNodeTexImage):
            yield node
        elif isinstance(node, bpy.types.ShaderNodeGroup) and node.node_tree:
            yield from _iter_image_nodes(node.node_tree, visited)


def _is_already_dds(image) -> bool:
    clean_name = strip_blender_suffix(image.name).lower()
    if clean_name.endswith(".dds"):
        return True
    if image.filepath and image.filepath.lower().endswith(".dds"):
        return True
    if getattr(image, "file_format", "") == "DDS":
        return True
    return False


def is_unsupported_format(image) -> bool:
    if image is None:
        return False
    clean_name = strip_blender_suffix(image.name).lower()
    if clean_name.endswith((".exr", ".hdr")):
        return True
    if image.filepath and image.filepath.lower().endswith((".exr", ".hdr")):
        return True
    if getattr(image, "file_format", "") in ("OPEN_EXR", "OPEN_EXR_MULTILAYER", "HDR"):
        return True
    return False


class DiscoveredTexture:
    __slots__ = ("image", "role")

    def __init__(self, image, role):
        self.image = image
        self.role = role


def discover(objects, include_unassigned_packed=False) -> list:
    results = []
    seen_images = set()

    for obj in objects:
        if obj.type != 'MESH':
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or mat.node_tree is None:
                continue
            for node in _iter_image_nodes(mat.node_tree):
                image = node.image
                if image is None or image.name in seen_images:
                    continue

                # Ignore disconnected floating image nodes unless explicitly tagged by Sollumz
                has_sollumz_tag = bool(getattr(node, "sollumz_texture_name", ""))
                is_linked = any(out.is_linked for out in node.outputs)
                if not has_sollumz_tag and not is_linked:
                    continue

                seen_images.add(image.name)

                if _is_already_dds(image) or is_unsupported_format(image):
                    continue

                ensure_image_loaded(image)
                sampler_name = _get_sollumz_texture_name(node, image)
                results.append(DiscoveredTexture(image=image, role=sampler_name))

    # Only scan unassigned packed images when scanning the entire scene
    if include_unassigned_packed:
        for image in bpy.data.images:
            if image.name in seen_images:
                continue
            if not image.packed_file:
                continue
            if _is_already_dds(image) or is_unsupported_format(image):
                continue

            ensure_image_loaded(image)
            seen_images.add(image.name)
            results.append(DiscoveredTexture(
                image=image,
                role=guess_role_from_name(image.name),
            ))

    return results


# ==============================================================================
# RULES
# ==============================================================================

# Minimum 4x4 required for DXT/BC 4x4 block compression and -min-mip-size 4
POWER_OF_TWO_SIZES = [4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192]


def nearest_power_of_two(value: int) -> int:
    if value <= POWER_OF_TWO_SIZES[0]:
        return POWER_OF_TWO_SIZES[0]
    return min(POWER_OF_TWO_SIZES, key=lambda s: abs(value - s))


def _round_to_block_multiple(value: float) -> int:
    """BC formats require dimensions to be multiples of 4 (min 4)."""
    return max(4, int(round(value / 4.0)) * 4)


def resolve_target_dimensions(width: int, height: int, target_max, enforce_pot: bool):
    """Compute output dimensions.

    ``target_max`` can be:
      - An int > 0 : clamp longest edge to that pixel count.
      - A str like ``"50%"`` or ``"25%"`` : scale both axes by that percentage.
      - 0 or falsy : keep original size (still honours *enforce_pot*).
    """
    if max(width, height) == 0:
        return width, height

    scale = 1.0

    if isinstance(target_max, str) and target_max.endswith('%'):
        pct = float(target_max.rstrip('%')) / 100.0
        scale = max(pct, 0.01)
    elif isinstance(target_max, (int, float)) and target_max > 0:
        if max(width, height) > target_max:
            scale = target_max / max(width, height)

    raw_w = width * scale
    raw_h = height * scale

    if enforce_pot:
        return nearest_power_of_two(round(raw_w)), nearest_power_of_two(round(raw_h))
    return _round_to_block_multiple(raw_w), _round_to_block_multiple(raw_h)


def resolve_role_default_format(role: str, has_alpha: bool) -> str:
    """Return the recommended BC format for a given Sollumz sampler role."""
    r = strip_blender_suffix(role).lower()
    if any(x in r for x in ("bump", "normal", "nrm")):
        return "DXT1 (BC1)"
    if has_alpha:
        return "DXT5 (BC3)"
    return "DXT1 (BC1)"


def resolve(global_res, global_format: str, role: str, has_alpha: bool, override) -> dict:
    """Return resolved resolution and format, considering role rules and overrides."""
    res = global_res

    if global_format == "Auto (By Role)":
        fmt = resolve_role_default_format(role, has_alpha)
    else:
        fmt = global_format
        if fmt == "DXT1 (BC1)" and has_alpha:
            fmt = "DXT5 (BC3)"

    if override is not None and getattr(override, "use_override", False):
        or_res = getattr(override, "resolution", "Global")
        if or_res != "Global":
            res = or_res if or_res.endswith('%') else int(or_res)
        or_fmt = getattr(override, "format", "Global")
        if or_fmt != "Global":
            fmt = resolve_role_default_format(role, has_alpha) if or_fmt == "Auto (By Role)" else or_fmt

    return {"res": res, "format": fmt}