bl_info = {
    "name": "DDS Prep",
    "author": "CSDev",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > DDS Prep",
    "description": "Batch-converts source textures on Sollumz materials to "
                    "Sollumz-ready .dds (power-of-2, correct BC format per "
                    "map role, mipmaps) and relinks the Image datablocks.",
    "category": "Import-Export",
    "warning": "",
}

import os
import sys
import shutil
import tempfile
import concurrent.futures
import subprocess
import threading
import bpy
import bpy.utils.previews

if "core" in locals():
    import importlib
    importlib.reload(core)
else:
    from . import core

# Global preview collection for custom UI status badges
_preview_collections = {}

# ==============================================================================
# PREFERENCES
# ==============================================================================

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLED_CSDEVCOMPRESS = os.path.join(ADDON_DIR, "bin", "csdevcompress.exe")


class SollumzDDSPrepPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    csdevcompress_path: bpy.props.StringProperty(
        name="csdevcompress.exe path",
        description="Path to csdevcompress.exe. Leave as the bundled default unless you need a custom build",
        subtype='FILE_PATH',
        default=BUNDLED_CSDEVCOMPRESS,
    )

    max_workers: bpy.props.IntProperty(
        name="Max Parallel Conversions",
        description="How many parallel compression processes to run at once",
        default=8, min=1, max=32,
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        row = box.row(align=True)
        exe = get_compressor_path(context)
        if core.is_available(exe):
            row.label(text="Compression Engine: Ready", icon='CHECKMARK')
        else:
            row.alert = True
            row.label(text="Compression Engine: Missing (csdevcompress.exe)", icon='ERROR')

        col = box.column(align=True)
        col.prop(self, "csdevcompress_path", text="Executable")
        col.prop(self, "max_workers", text="Worker Threads")


def get_prefs(context=None):
    ctx = context or bpy.context
    addon = ctx.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def get_compressor_path(context=None) -> str:
    """Return a verified executable path, falling back automatically to the bundled binary."""
    try:
        prefs = get_prefs(context)
        if prefs:
            p = getattr(prefs, "csdevcompress_path", None)
            if p and core.is_available(p):
                return p
    except Exception:
        pass
    return BUNDLED_CSDEVCOMPRESS


# ==============================================================================
# PROPERTIES
# ==============================================================================

QUALITY_PRESETS = [
    ("4096", "4096 px", "Clamp longest edge to 4096px"),
    ("2048", "2048 px", "Clamp longest edge to 2048px"),
    ("1024", "1024 px", "Clamp longest edge to 1024px"),
    ("512", "512 px", "Clamp longest edge to 512px"),
    ("256", "256 px", "Clamp longest edge to 256px"),
    ("50%", "50% Scale", "Halve the original resolution"),
    ("25%", "25% Scale", "Quarter the original resolution"),
]

RESOLUTION_ITEMS = [("Global", "Use Global", "Use the global resolution rule")] + QUALITY_PRESETS

BASE_FORMAT_ITEMS = [
    ("Auto (By Role)", "Auto (By Role)", core.FORMAT_TOOLTIPS["Auto (By Role)"]),
] + [
    (name, name, core.FORMAT_TOOLTIPS.get(name, "")) for name in getattr(core, "DISPLAY_FORMATS", core.FORMAT_MAP.keys())
]

OVERRIDE_FORMAT_ITEMS = [("Global", "Use Global", "Use the global format rule")] + BASE_FORMAT_ITEMS


class SollumzDDSTextureItem(bpy.types.PropertyGroup):
    image_name: bpy.props.StringProperty()
    role: bpy.props.StringProperty()
    width: bpy.props.IntProperty()
    height: bpy.props.IntProperty()
    is_separator: bpy.props.BoolProperty(default=False)
    has_alpha: bpy.props.BoolProperty(default=False)

    use_override: bpy.props.BoolProperty(
        name="Override",
        description="Custom format/resolution override for this texture",
        default=False,
    )
    resolution: bpy.props.EnumProperty(name="Size", items=RESOLUTION_ITEMS, default="Global")
    format: bpy.props.EnumProperty(name="Format", items=OVERRIDE_FORMAT_ITEMS, default="Global")

    status: bpy.props.StringProperty(default="")


class SollumzDDSSettings(bpy.types.PropertyGroup):
    use_custom_output_dir: bpy.props.BoolProperty(
        name="Custom Output Folder",
        description="Export all .dds files into a specific folder instead of next to source images",
        default=False,
    )
    output_dir: bpy.props.StringProperty(
        name="Output Directory",
        subtype='DIR_PATH',
        description="Where converted .dds files are written. Source images are never overwritten",
    )
    relink_after_convert: bpy.props.BoolProperty(
        name="Relink Materials",
        description="Point each Image datablock at its new .dds and reload it automatically",
        default=True,
    )
    enforce_power_of_two: bpy.props.BoolProperty(
        name="Power of Two",
        description="Round non-PoT dimensions to the nearest power of two before compressing",
        default=True,
    )
    mip_generate: bpy.props.BoolProperty(
        name="Mipmaps",
        description="Generate Mitchell-Netravali mipmaps down to 4x4px",
        default=True,
    )

    use_quality_preset: bpy.props.BoolProperty(
        name="Resize / Clamp",
        description="Downscale or cap texture resolution globally",
        default=False,
    )
    quality_preset: bpy.props.EnumProperty(
        name="Max Size",
        items=QUALITY_PRESETS,
        default="2048",
    )
    global_format: bpy.props.EnumProperty(
        name="Compression",
        items=BASE_FORMAT_ITEMS,
        default="Auto (By Role)",
    )
    scan_target: bpy.props.EnumProperty(
        name="Scope",
        items=[
            ("SELECTED", "Selected", "Scan selected mesh objects"),
            ("SCENE", "Scene", "Scan all mesh objects and packed textures in the scene"),
            ("COLLECTION", "Collection", "Scan objects inside a specific collection"),
        ],
        default="SELECTED",
    )
    target_collection: bpy.props.PointerProperty(
        name="Collection",
        type=bpy.types.Collection,
    )

    search_query: bpy.props.StringProperty(
        name="Filter",
        description="Filter queue by texture name",
        default="",
    )
    role_filter: bpy.props.EnumProperty(
        name="Role",
        items=[
            ('ALL', "All Roles", "Show all texture types"),
            ('DiffuseSampler', "Diffuse", "Albedo / Base Color maps"),
            ('BumpSampler', "Normal", "Normal / Bump maps"),
            ('SpecSampler', "Specular", "Specular / ORM / Gloss maps"),
            ('DetailSampler', "Detail", "Detail maps"),
            ('TintPaletteSampler', "Tint", "Tint Palette maps"),
            ('ALPHA', "Alpha Only", "Show only textures with an alpha channel"),
        ],
        default='ALL',
    )

    bulk_resolution: bpy.props.EnumProperty(
        name="Batch Size",
        items=RESOLUTION_ITEMS,
        default="1024",
    )
    bulk_format: bpy.props.EnumProperty(
        name="Batch Format",
        items=OVERRIDE_FORMAT_ITEMS,
        default="Auto (By Role)",
    )

    # UI Drawer Toggles
    show_batch_tools: bpy.props.BoolProperty(
        name="Batch Override",
        description="Toggle bulk override controls for visible items",
        default=False,
    )
    show_settings_drawer: bpy.props.BoolProperty(
        name="Global Settings",
        description="Show compression and output configuration",
        default=True,
    )


# ==============================================================================
# UI & WORKFLOW HELPERS
# ==============================================================================

def _role_short_badge(role: str) -> tuple:
    """Return (short_tag, icon_name) for a Sollumz sampler role."""
    r = role.lower()
    if any(x in r for x in ("bump", "normal", "nrm")):
        return "NRM", 'NORMALS_VERTEX'
    if any(x in r for x in ("spec", "gloss", "orm", "rough")):
        return "SPC", 'NODE_MATERIAL'
    if "detail" in r:
        return "DTL", 'TEXTURE'
    if any(x in r for x in ("tint", "palette", "pal")):
        return "TNT", 'COLOR'
    return "DIF", 'MATERIAL'


def _short_format_label(fmt_name: str) -> str:
    if "BC1" in fmt_name:
        return "BC1"
    if "BC3" in fmt_name:
        return "BC3"
    if "BC5" in fmt_name:
        return "BC5"
    if "BC7" in fmt_name:
        return "BC7"
    return fmt_name


def _preview_item_target(settings, item) -> tuple:
    """Return (target_w, target_h, short_fmt) for live preview in the UIList."""
    if settings.use_quality_preset:
        qp = settings.quality_preset
        global_res = qp if qp.endswith('%') else int(qp)
    else:
        global_res = 0

    resolved = core.resolve(
        global_res=global_res,
        global_format=settings.global_format,
        role=item.role,
        has_alpha=item.has_alpha,
        override=item if item.use_override else None,
    )
    tw, th = core.resolve_target_dimensions(
        item.width, item.height, resolved["res"], settings.enforce_power_of_two
    )
    return tw, th, _short_format_label(resolved["format"])


def _populate_texture_list(scene, discovered_textures):
    """Populate scene.sollumz_dds_textures cleanly sorted (Opaque first, Alpha second)."""
    scene.sollumz_dds_textures.clear()
    scene.sollumz_dds_active_index = 0

    alpha_textures = []
    normal_textures = []

    for tex in discovered_textures:
        core.ensure_image_loaded(tex.image)
        if tex.image.depth in (32, 64):
            alpha_textures.append(tex)
        else:
            normal_textures.append(tex)

    def _add_item(tex, has_alpha):
        item = scene.sollumz_dds_textures.add()
        item.image_name = tex.image.name
        item.role = tex.role
        item.width, item.height = tex.image.size[0], tex.image.size[1]
        item.status = "" if (tex.image.filepath or tex.image.has_data) else "No source data"
        item.has_alpha = has_alpha
        item.is_separator = False

    for tex in normal_textures:
        _add_item(tex, False)
    for tex in alpha_textures:
        _add_item(tex, True)


def _passes_filter(item, search_query, role_filter):
    if item.is_separator:
        return False

    if search_query and search_query not in item.image_name.lower():
        return False

    if role_filter != 'ALL':
        if role_filter == 'ALPHA':
            return item.has_alpha
        r = item.role.lower()
        if role_filter == 'DiffuseSampler':
            return any(x in r for x in ('diffuse', 'color', 'albedo'))
        if role_filter == 'BumpSampler':
            return any(x in r for x in ('bump', 'normal', 'nrm'))
        if role_filter == 'SpecSampler':
            return any(x in r for x in ('spec', 'gloss', 'orm', 'rough'))
        if role_filter == 'DetailSampler':
            return 'detail' in r
        if role_filter == 'TintPaletteSampler':
            return any(x in r for x in ('tint', 'palette', 'pal'))
        return role_filter.lower() in r

    return True


def _tag_redraw_all(context):
    if not context or not context.screen:
        return
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def _prepare_input_file(image, target_w, target_h, temp_dir, output_file):
    if core.is_unsupported_format(image):
        raise RuntimeError(f"EXR/HDR floating-point files are not supported: {image.name}")

    try:
        src_path = os.path.abspath(bpy.path.abspath(image.filepath)) if image.filepath else ""
    except Exception:
        src_path = ""

    out_abs = os.path.abspath(output_file)
    is_same_file = bool(src_path) and os.path.normcase(src_path) == os.path.normcase(out_abs)
    is_packed = image.packed_file is not None
    has_valid_path = bool(src_path) and os.path.isfile(src_path)

    w, h = image.size[0], image.size[1]
    same_dims = (w, h) == (target_w, target_h) or target_w <= 0 or target_h <= 0
    is_dds = bool(src_path) and src_path.lower().endswith(".dds")

    # csdevcompress can only directly read standard 8-bit image formats from disk:
    direct_formats = {".png", ".tga", ".bmp", ".jpg", ".jpeg"}
    src_ext = os.path.splitext(src_path)[1].lower() if src_path else ""
    is_direct_supported = src_ext in direct_formats

    # If already a valid supported non-DDS file on disk and not overwriting in place, compressor can read it directly
    if not is_packed and has_valid_path and not is_same_file and same_dims and is_direct_supported and not is_dds:
        return src_path

    safe_stem = core.clean_filename_stem(image.name)
    out_path = os.path.join(temp_dir, f"{safe_stem}_{id(image)}.png")

    # Fast path: if same dimensions and not same file, save_render can export directly to PNG
    if same_dims and not is_same_file:
        scene = bpy.context.scene
        orig_color_mode = scene.render.image_settings.color_mode if scene else 'RGBA'
        orig_format = scene.render.image_settings.file_format if scene else 'PNG'
        try:
            if scene:
                scene.render.image_settings.file_format = 'PNG'
                scene.render.image_settings.color_mode = 'RGBA'
            image.save_render(filepath=out_path)
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                return out_path
        except Exception:
            pass
        finally:
            if scene:
                scene.render.image_settings.file_format = orig_format
                scene.render.image_settings.color_mode = orig_color_mode

    # Rescaling or fallback memory buffer extraction:
    new_w = target_w if target_w > 0 else w
    new_h = target_h if target_h > 0 else h
    temp_img = bpy.data.images.new(f"__dds_prep_tmp_{id(image)}", w, h, alpha=True, float_buffer=image.is_float)
    try:
        import array
        buf = array.array('f', [0.0] * (w * h * 4))
        image.pixels.foreach_get(buf)
        temp_img.pixels.foreach_set(buf)

        if (w, h) != (new_w, new_h) and new_w > 0 and new_h > 0:
            temp_img.scale(new_w, new_h)

        temp_img.filepath_raw = out_path
        temp_img.file_format = 'PNG'
        try:
            temp_img.save()
        except Exception:
            scene = bpy.context.scene
            orig_color_mode = scene.render.image_settings.color_mode if scene else 'RGBA'
            orig_format = scene.render.image_settings.file_format if scene else 'PNG'
            try:
                if scene:
                    scene.render.image_settings.file_format = 'PNG'
                    scene.render.image_settings.color_mode = 'RGBA'
                temp_img.save_render(filepath=out_path)
            finally:
                if scene:
                    scene.render.image_settings.file_format = orig_format
                    scene.render.image_settings.color_mode = orig_color_mode

        return out_path
    finally:
        bpy.data.images.remove(temp_img)


# ==============================================================================
# OPERATORS
# ==============================================================================

class SOLLUMZDDS_OT_scan(bpy.types.Operator):
    bl_idname = "sollumzdds.scan"
    bl_label = "Scan Textures"
    bl_description = "Discover Sollumz material textures from the target scope"

    @classmethod
    def poll(cls, context):
        return not SOLLUMZDDS_OT_convert._is_running

    def execute(self, context):
        scene = context.scene
        settings = scene.sollumz_dds_settings
        target = settings.scan_target

        if target == 'SELECTED':
            objects = context.selected_objects
            if not objects:
                self.report({'WARNING'}, "Select at least one object to scan.")
                return {'CANCELLED'}
        elif target == 'COLLECTION':
            if settings.target_collection:
                objects = settings.target_collection.all_objects
            else:
                self.report({'WARNING'}, "Choose a target collection first.")
                return {'CANCELLED'}
        else:
            objects = scene.objects

        found = core.discover(objects, include_unassigned_packed=(target == 'SCENE'))
        _populate_texture_list(scene, found)

        self.report({'INFO'}, f"Discovered {len(found)} texture(s).")
        return {'FINISHED'}


class SOLLUMZDDS_OT_bulk_override(bpy.types.Operator):
    bl_idname = "sollumzdds.bulk_override"
    bl_label = "Apply Batch Override"
    bl_description = "Apply custom format and size to all currently filtered textures"

    clear_overrides: bpy.props.BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        return not SOLLUMZDDS_OT_convert._is_running and len(context.scene.sollumz_dds_textures) > 0

    def execute(self, context):
        scene = context.scene
        settings = scene.sollumz_dds_settings

        search_query = settings.search_query.strip().lower()
        role_filter = settings.role_filter

        applied_count = 0
        for item in scene.sollumz_dds_textures:
            if not _passes_filter(item, search_query, role_filter):
                continue

            if self.clear_overrides:
                item.use_override = False
                item.resolution = "Global"
                item.format = "Global"
            else:
                item.use_override = True
                item.resolution = settings.bulk_resolution
                item.format = settings.bulk_format
            applied_count += 1

        action = "Reset overrides on" if self.clear_overrides else "Applied override to"
        self.report({'INFO'}, f"{action} {applied_count} texture(s).")
        return {'FINISHED'}


class SOLLUMZDDS_OT_clear_list(bpy.types.Operator):
    bl_idname = "sollumzdds.clear_list"
    bl_label = "Clear Queue"
    bl_description = "Clear all textures from the queue"

    @classmethod
    def poll(cls, context):
        return not SOLLUMZDDS_OT_convert._is_running and len(context.scene.sollumz_dds_textures) > 0

    def execute(self, context):
        context.scene.sollumz_dds_textures.clear()
        context.scene.sollumz_dds_active_index = 0
        return {'FINISHED'}


class SOLLUMZDDS_OT_open_output_dir(bpy.types.Operator):
    bl_idname = "sollumzdds.open_output_dir"
    bl_label = "Open Output Folder"
    bl_description = "Open the custom output folder in File Explorer"

    @classmethod
    def poll(cls, context):
        settings = context.scene.sollumz_dds_settings
        if settings.use_custom_output_dir and settings.output_dir:
            return os.path.isdir(bpy.path.abspath(settings.output_dir))
        return False

    def execute(self, context):
        path = bpy.path.abspath(context.scene.sollumz_dds_settings.output_dir)
        try:
            if hasattr(os, "startfile"):
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to open directory: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}


def _determine_output_path(context, image, resolution_str="ORIGINAL"):
    settings = context.scene.sollumz_dds_settings
    stem = core.clean_filename_stem(image.name)

    # 1. Custom output directory if enabled
    if settings.use_custom_output_dir and settings.output_dir:
        abs_custom = bpy.path.abspath(settings.output_dir)
        if os.path.isdir(abs_custom):
            return os.path.join(abs_custom, f"{stem}.dds")

    # 2. Existing image filepath directory
    if image.filepath:
        try:
            abs_img_dir = os.path.dirname(os.path.abspath(bpy.path.abspath(image.filepath)))
            if os.path.isdir(abs_img_dir):
                return os.path.join(abs_img_dir, f"{stem}.dds")
        except Exception:
            pass

    # 3. Blend file directory (saved in dds/ subfolder or same folder)
    if bpy.data.filepath:
        blend_dir = os.path.dirname(bpy.data.filepath)
        dds_dir = os.path.join(blend_dir, "dds")
        os.makedirs(dds_dir, exist_ok=True)
        return os.path.join(dds_dir, f"{stem}.dds")

    # 4. Fallback temp folder
    temp_dds_dir = os.path.join(tempfile.gettempdir(), "sollumz_dds_export")
    os.makedirs(temp_dds_dir, exist_ok=True)
    return os.path.join(temp_dds_dir, f"{stem}.dds")


class SOLLUMZDDS_OT_convert_single(bpy.types.Operator):
    bl_idname = "sollumzdds.convert_single"
    bl_label = "DDS Prep"
    bl_description = "DDS Prep"
    bl_options = {'REGISTER', 'UNDO'}

    material_name: bpy.props.StringProperty()
    node_name: bpy.props.StringProperty()

    format: bpy.props.EnumProperty(
        name="Format",
        items=[
            ("BC1", "DXT1 (BC1)", "No alpha - best for opaque diffuse, normal maps and masks"),
            ("BC3", "DXT5 (BC3)", "Interpolated alpha - for diffuse textures with transparency"),
            ("BC5", "BC5", "Two channel - optional high-quality normal/bump maps (BumpSampler)"),
            ("BC7", "BC7", "High quality, full alpha - best for high-detail specular/normal/diffuse maps"),
        ],
        default="BC3",
    )

    resolution: bpy.props.EnumProperty(
        name="Resolution",
        items=[
            ("ORIGINAL", "Original", "Keep original texture dimensions"),
            ("HALF", "50% (Half)", "Halve the texture dimensions"),
            ("QUARTER", "25% (Quarter)", "Quarter the texture dimensions"),
            ("2048", "2048", "Clamp longest edge to 2048px"),
            ("1024", "1024", "Clamp longest edge to 1024px"),
            ("512", "512", "Clamp longest edge to 512px"),
            ("256", "256", "Clamp longest edge to 256px"),
        ],
        default="ORIGINAL",
    )

    def invoke(self, context, event):
        mat = bpy.data.materials.get(self.material_name)
        if not mat or not mat.node_tree:
            if context.active_object and context.active_object.active_material:
                mat = context.active_object.active_material
                self.material_name = mat.name

        node = mat.node_tree.nodes.get(self.node_name) if mat and mat.node_tree else None
        node_str = (self.node_name or "").lower()

        # Smart format default based on sampler type & image properties
        if any(x in node_str for x in ("bump", "norm")):
            self.format = "BC1"
        elif any(x in node_str for x in ("spec", "gloss")):
            self.format = "BC3"
        else:
            has_alpha = False
            if node and node.image:
                core.ensure_image_loaded(node.image)
                has_alpha = (node.image.depth in (32, 64))
            self.format = "BC3" if has_alpha else "BC1"

        self.resolution = "ORIGINAL"
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        mat = bpy.data.materials.get(self.material_name)
        node = mat.node_tree.nodes.get(self.node_name) if mat and mat.node_tree else None
        img = node.image if node else None

        node_lower = (self.node_name or "").lower()
        if any(x in node_lower for x in ("bump", "norm")):
            role = "Normal"
        elif any(x in node_lower for x in ("spec", "gloss")):
            role = "Specular"
        elif any(x in node_lower for x in ("detail",)):
            role = "Detail"
        elif any(x in node_lower for x in ("tint", "palette")):
            role = "Tint"
        else:
            role = "Color"

        dims = f"{img.size[0]}x{img.size[1]}" if (img and img.size[0] > 0) else "Unknown"
        alpha_tag = " · RGBA" if (img and img.depth in (32, 64)) else " · RGB"

        # Header info label: DiffuseSampler — Color — 256x256 · RGBA
        row = layout.row()
        row.alignment = 'LEFT'
        row.label(text=f"{self.node_name} — {role} — {dims}{alpha_tag}")

        layout.separator()

        col = layout.column(align=True)
        col.prop(self, "format")
        col.prop(self, "resolution")

    def execute(self, context):
        exe_path = get_compressor_path(context)
        if not core.is_available(exe_path):
            self.report({'ERROR'}, "Compression engine (csdevcompress.exe) not found. Check Add-on Preferences.")
            return {'CANCELLED'}

        mat = bpy.data.materials.get(self.material_name)
        if not mat or not mat.node_tree:
            self.report({'ERROR'}, f"Material '{self.material_name}' not found.")
            return {'CANCELLED'}

        node = mat.node_tree.nodes.get(self.node_name)
        if not node or not isinstance(node, bpy.types.ShaderNodeTexImage) or not node.image:
            self.report({'ERROR'}, f"Texture node or image missing for '{self.node_name}'.")
            return {'CANCELLED'}

        image = node.image
        if core.is_unsupported_format(image):
            self.report({'ERROR'}, f"Image '{image.name}' is an EXR/HDR file. EXR format is not supported for DDS conversion.")
            return {'CANCELLED'}

        core.ensure_image_loaded(image)
        w, h = image.size[0], image.size[1]
        if w == 0 or h == 0:
            self.report({'ERROR'}, f"Image '{image.name}' has 0 dimensions or failed to load.")
            return {'CANCELLED'}

        orig_img_name = image.name
        orig_colorspace = image.colorspace_settings.name
        orig_alpha_mode = getattr(image, "alpha_mode", "STRAIGHT")
        orig_hd = getattr(image, "sz_is_hd", None)
        settings = context.scene.sollumz_dds_settings

        # Resolve target dimensions
        if self.resolution == "ORIGINAL":
            target_w, target_h = (
                core.nearest_power_of_two(w),
                core.nearest_power_of_two(h)
            ) if settings.enforce_power_of_two else (w, h)
        elif self.resolution == "HALF":
            target_w, target_h = core.resolve_target_dimensions(w, h, "50%", settings.enforce_power_of_two)
        elif self.resolution == "QUARTER":
            target_w, target_h = core.resolve_target_dimensions(w, h, "25%", settings.enforce_power_of_two)
        else:
            try:
                max_dim = int(self.resolution)
                target_w, target_h = core.resolve_target_dimensions(w, h, max_dim, settings.enforce_power_of_two)
            except ValueError:
                target_w, target_h = w, h

        output_file = _determine_output_path(context, image, self.resolution)
        out_dir = os.path.dirname(output_file)
        os.makedirs(out_dir, exist_ok=True)
        temp_dir = tempfile.mkdtemp(prefix="sollumz_dds_single_")

        try:
            input_file = _prepare_input_file(image, target_w, target_h, temp_dir, output_file)
            mip_options = {"generate": settings.mip_generate}
            node_lower = (self.node_name or "").lower()
            is_color = not any(x in node_lower for x in ("bump", "norm", "spec", "gloss", "orm", "rough"))

            comp_format = self.format
            if comp_format == "BC3":
                comp_format = "DXT5 (BC3)"
            elif comp_format == "BC1":
                comp_format = "DXT1 (BC1)"

            core.run(
                exe_path,
                comp_format,
                input_file,
                output_file,
                mip_options,
                is_color,
                timeout=120,
            )

            # Relink
            if image.packed_file:
                image.unpack(method='REMOVE')
            image.filepath = output_file
            image.name = os.path.basename(output_file)
            image.reload()

            if orig_colorspace:
                try:
                    image.colorspace_settings.name = orig_colorspace
                except Exception:
                    pass

            if orig_alpha_mode:
                try:
                    image.alpha_mode = orig_alpha_mode
                except Exception:
                    pass

            if orig_hd is not None and hasattr(image, "sz_is_hd"):
                try:
                    image.sz_is_hd = orig_hd
                except Exception:
                    pass

            # Update item status in queue if present
            for item in context.scene.sollumz_dds_textures:
                if item.image_name in (orig_img_name, image.name, os.path.basename(output_file)):
                    item.status = "OK"
                    item.image_name = image.name
                    item.width = image.size[0]
                    item.height = image.size[1]
                    if "BC3" in self.format or "BC7" in self.format:
                        item.has_alpha = True
                    elif "BC1" in self.format or "BC5" in self.format:
                        item.has_alpha = False

            _tag_redraw_all(context)
            for area in context.screen.areas:
                if area.type == 'PROPERTIES':
                    area.tag_redraw()

            self.report({'INFO'}, f"Successfully converted {self.node_name} to {self.format} ({target_w}x{target_h})")
            return {'FINISHED'}

        except Exception as e:
            self.report({'ERROR'}, f"DDS conversion failed: {e}")
            return {'CANCELLED'}
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class SOLLUMZDDS_OT_convert(bpy.types.Operator):
    bl_idname = "sollumzdds.convert"
    bl_label = "Convert to DDS"
    bl_description = "Compress queued textures into Sollumz-ready .dds files"

    _is_running = False
    _timer = None
    _executor = None
    _futures = None
    _active_procs = None
    _proc_lock = None
    _temp_dir = None
    _total = 0
    _done = 0
    _relink = True

    @classmethod
    def poll(cls, context):
        return not cls._is_running and len(context.scene.sollumz_dds_textures) > 0

    def _build_jobs(self, context):
        scene = context.scene
        settings = scene.sollumz_dds_settings

        if settings.use_quality_preset:
            qp = settings.quality_preset
            global_res = qp if qp.endswith('%') else int(qp)
        else:
            global_res = 0

        jobs = []
        for idx, item in enumerate(scene.sollumz_dds_textures):
            if item.is_separator:
                continue

            image = bpy.data.images.get(item.image_name)
            if image is None or (not image.filepath and not image.has_data):
                item.status = "Skipped: no source"
                continue

            if core.is_unsupported_format(image):
                item.status = "Skipped: EXR/HDR unsupported"
                continue

            core.ensure_image_loaded(image)
            if image.size[0] == 0 or image.size[1] == 0:
                item.status = "Skipped: 0 dimensions"
                continue

            has_alpha = image.depth in (32, 64)

            resolved = core.resolve(
                global_res=global_res,
                global_format=settings.global_format,
                role=item.role,
                has_alpha=has_alpha,
                override=item if item.use_override else None,
            )

            target_w, target_h = core.resolve_target_dimensions(
                image.size[0], image.size[1], resolved["res"], settings.enforce_power_of_two,
            )

            jobs.append({
                "item_index": idx,
                "image_name": image.name,
                "role": item.role,
                "orig_colorspace": image.colorspace_settings.name,
                "orig_alpha_mode": getattr(image, "alpha_mode", "STRAIGHT"),
                "orig_hd": getattr(image, "sz_is_hd", None),
                "target_w": target_w,
                "target_h": target_h,
                "format": resolved["format"],
            })
        return jobs

    def invoke(self, context, event):
        scene = context.scene
        settings = scene.sollumz_dds_settings
        prefs = get_prefs(context)
        exe_path = get_compressor_path(context)

        if not core.is_available(exe_path):
            self.report({'ERROR'}, "csdevcompress.exe not found. Configure it in Addon Preferences.")
            return {'CANCELLED'}

        if settings.use_custom_output_dir:
            out_dir = settings.output_dir
            if not out_dir or not os.path.isdir(bpy.path.abspath(out_dir)):
                self.report({'ERROR'}, "Select a valid Custom Output Folder first.")
                return {'CANCELLED'}
            out_dir = os.path.abspath(bpy.path.abspath(out_dir))
        else:
            out_dir = None

        jobs = self._build_jobs(context)
        if not jobs:
            self.report({'WARNING'}, "No valid textures to convert.")
            return {'CANCELLED'}

        self._temp_dir = tempfile.mkdtemp(prefix="sollumz_dds_prep_")
        mip_options = {"generate": settings.mip_generate}

        prepared = []
        used_output_paths = set()

        for job in jobs:
            image = bpy.data.images.get(job["image_name"])
            item = scene.sollumz_dds_textures[job["item_index"]]

            job_out_dir = out_dir
            if not job_out_dir:
                try:
                    raw_dir = os.path.dirname(os.path.abspath(bpy.path.abspath(image.filepath)))
                    if os.path.isdir(raw_dir):
                        job_out_dir = raw_dir
                except Exception:
                    pass

            job["pack_after"] = False
            if not job_out_dir:
                if image.packed_file:
                    job_out_dir = self._temp_dir
                    job["pack_after"] = True
                elif bpy.data.filepath:
                    job_out_dir = os.path.dirname(bpy.data.filepath)

            if not job_out_dir:
                self.report(
                    {'ERROR'},
                    f"No output path for '{image.name}'. Save your .blend file or enable Custom Output Folder.",
                )
                shutil.rmtree(self._temp_dir, ignore_errors=True)
                return {'CANCELLED'}

            stem = core.clean_filename_stem(image.name)
            candidate_path = os.path.join(job_out_dir, f"{stem}.dds")
            suffix_idx = 1
            while os.path.normcase(candidate_path) in used_output_paths:
                candidate_path = os.path.join(job_out_dir, f"{stem}_{suffix_idx}.dds")
                suffix_idx += 1
            used_output_paths.add(os.path.normcase(candidate_path))
            output_file = candidate_path

            item.status = "WAIT"
            try:
                input_file = _prepare_input_file(
                    image, job["target_w"], job["target_h"], self._temp_dir, output_file
                )
            except Exception as e:
                item.status = f"FAILED: {e}"
                continue

            prepared.append((job, input_file, output_file))

        if not prepared:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self.report({'ERROR'}, "Failed to prepare textures.")
            return {'CANCELLED'}

        SOLLUMZDDS_OT_convert._is_running = True
        self._active_procs = set()
        self._proc_lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=prefs.max_workers)
        self._futures = {}

        for job, input_file, output_file in prepared:
            future = self._executor.submit(
                core.run,
                exe_path,
                job["format"],
                input_file,
                output_file,
                mip_options,
                core.is_color_role(job["role"]),
                self._active_procs,
                self._proc_lock,
            )
            self._futures[future] = (job, output_file)

        self._total = len(self._futures)
        self._done = 0
        self._relink = settings.relink_after_convert

        wm = context.window_manager
        wm.progress_begin(0, self._total)
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        _tag_redraw_all(context)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            for future in list(self._futures):
                future.cancel()
            if self._proc_lock:
                with self._proc_lock:
                    procs = list(self._active_procs)
            else:
                procs = list(self._active_procs)
            for proc in procs:
                try:
                    proc.terminate()
                except Exception:
                    pass
            self.report({'WARNING'}, "Conversion cancelled.")
            self._finish(context, cancelled=True)
            return {'CANCELLED'}

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        done_futures, _ = concurrent.futures.wait(self._futures, timeout=0)
        if not done_futures:
            return {'RUNNING_MODAL'}

        textures_col = context.scene.sollumz_dds_textures

        for future in done_futures:
            job, output_file = self._futures.pop(future)
            idx = job["item_index"]
            item = textures_col[idx] if 0 <= idx < len(textures_col) else None
            image = bpy.data.images.get(job["image_name"])

            try:
                future.result()
                if item:
                    item.status = "OK"

                if self._relink and image is not None:
                    if image.packed_file:
                        image.unpack(method='REMOVE')
                    image.filepath = output_file
                    image.name = os.path.basename(output_file)
                    image.reload()

                    try:
                        if job.get("orig_colorspace"):
                            image.colorspace_settings.name = job["orig_colorspace"]
                    except Exception:
                        pass

                    try:
                        if job.get("orig_alpha_mode"):
                            image.alpha_mode = job["orig_alpha_mode"]
                    except Exception:
                        pass

                    if job.get("orig_hd") is not None and hasattr(image, "sz_is_hd"):
                        try:
                            image.sz_is_hd = job["orig_hd"]
                        except Exception:
                            pass

                    if job.get("pack_after"):
                        image.pack()

                    if item:
                        item.width = image.size[0]
                        item.height = image.size[1]
                        item.image_name = image.name
            except Exception as e:
                if item:
                    item.status = f"FAILED: {e}"
                print(f"[Sollumz DDS Prep] {job['image_name']} failed:\n{e}")
                self.report({'ERROR'}, f"{job['image_name']}: {e}")

            self._done += 1
            context.window_manager.progress_update(self._done)

        _tag_redraw_all(context)

        if not self._futures:
            return self._finish(context, cancelled=False)

        return {'RUNNING_MODAL'}

    def _finish(self, context, cancelled=False):
        SOLLUMZDDS_OT_convert._is_running = False
        wm = context.window_manager
        wm.progress_end()
        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None

        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None

        if self._temp_dir and os.path.isdir(self._temp_dir):
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None

        _tag_redraw_all(context)

        if cancelled:
            return {'CANCELLED'}

        settings = context.scene.sollumz_dds_settings
        failed = sum(1 for it in context.scene.sollumz_dds_textures if it.status.startswith("FAILED"))
        if failed:
            self.report(
                {'WARNING'},
                f"Converted {self._total - failed}/{self._total} ({failed} failed).",
            )
        else:
            dest = (
                bpy.path.abspath(settings.output_dir)
                if (settings.use_custom_output_dir and settings.output_dir)
                else "source folders"
            )
            self.report({'INFO'}, f"Converted {self._total} texture(s) to {dest}")
        return {'FINISHED'}


# ==============================================================================
# MODERN STUDIO UI
# ==============================================================================

class SOLLUMZDDS_UL_textures(bpy.types.UIList):
    def filter_items(self, context, data, propname):
        textures = getattr(data, propname)
        settings = context.scene.sollumz_dds_settings
        search_query = settings.search_query.strip().lower()
        role_filter = settings.role_filter

        flt_flags = [
            self.bitflag_filter_item if _passes_filter(item, search_query, role_filter) else 0
            for item in textures
        ]
        return flt_flags, []

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        settings = context.scene.sollumz_dds_settings
        image = bpy.data.images.get(item.image_name)
        role_tag, role_icon = _role_short_badge(item.role)
        tw, th, target_fmt = _preview_item_target(settings, item)

        row = layout.row(align=True)

        # Left column: Thumbnail + Texture Name
        left = row.split(factor=0.52, align=True)
        name_row = left.row(align=True)
        if image:
            name_row.label(text=item.image_name, icon_value=layout.icon(image))
        else:
            name_row.label(text=item.image_name, icon='IMAGE_DATA')

        # Right column: Role Badge + Live Output Preview + Status/Override Pin
        right = left.row(align=True)
        right.alignment = 'RIGHT'

        if item.status == "WAIT":
            right.label(text="Queued...", icon='SORTTIME')
        elif item.status.startswith("FAILED"):
            sub = right.row(align=True)
            pcoll = _preview_collections.get("main")
            if pcoll and "log_err" in pcoll:
                sub.label(text="Error", icon_value=pcoll["log_err"].icon_id)
            else:
                sub.alert = True
                sub.label(text="Error", icon='ERROR')
        else:
            # Show Alpha indicator icon if present
            if item.has_alpha:
                right.label(text="", icon='IMAGE_ALPHA')

            # Show compact [Role | Resolution · Format] summary
            dim_str = f"{max(tw, th)}p" if tw > 0 else "?"
            summary = f"{role_tag} · {dim_str} · {target_fmt}"
            right.label(text=summary)

            if item.status == "OK":
                pcoll = _preview_collections.get("main")
                if pcoll and "log_ok" in pcoll:
                    right.label(text="", icon_value=pcoll["log_ok"].icon_id)
                else:
                    right.label(text="", icon='CHECKMARK')

        # Override toggle pin button at far right
        pin_icon = 'PINNED' if item.use_override else 'UNPINNED'
        right.prop(item, "use_override", text="", icon=pin_icon, emboss=False)


class SOLLUMZDDS_PT_main(bpy.types.Panel):
    bl_label = "DDS Prep"
    bl_idname = "SOLLUMZDDS_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "DDS Prep"

    def draw_header(self, context):
        exe = get_compressor_path(context)
        if not core.is_available(exe):
            self.layout.label(text="", icon='ERROR')

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.sollumz_dds_settings
        prefs = get_prefs(context)
        exe = get_compressor_path(context)

        # ----------------------------------------------------------------------
        # 1. ENGINE MISSING WARNING BANNER (only shown if exe is missing)
        # ----------------------------------------------------------------------
        if not core.is_available(exe):
            warn_box = layout.box()
            warn_col = warn_box.column(align=True)
            warn_row = warn_col.row(align=True)
            warn_row.alert = True
            warn_row.label(text="csdevcompress.exe Not Found", icon='ERROR')
            warn_col.prop(prefs, "csdevcompress_path", text="Path")
            layout.separator(factor=0.5)

        # ----------------------------------------------------------------------
        # 2. SOURCE & DISCOVERY CARD
        # ----------------------------------------------------------------------
        src_box = layout.box()
        src_col = src_box.column(align=True)

        # Segmented Scope Pill Bar
        scope_row = src_col.row(align=True)
        scope_row.scale_y = 1.15
        scope_row.prop(settings, "scan_target", expand=True)

        if settings.scan_target == 'COLLECTION':
            src_col.separator(factor=0.5)
            src_col.prop(settings, "target_collection", text="", icon='OUTLINER_COLLECTION')

        src_col.separator(factor=0.8)
        act_row = src_col.row(align=True)
        act_row.scale_y = 1.25
        scan_labels = {
            'SELECTED': "Scan Selected Objects",
            'SCENE': "Scan Entire Scene",
            'COLLECTION': "Scan Collection",
        }
        act_row.operator(
            "sollumzdds.scan",
            text=scan_labels.get(settings.scan_target, "Scan Textures"),
            icon='VIEWZOOM',
        )
        if len(scene.sollumz_dds_textures) > 0:
            act_row.operator("sollumzdds.clear_list", text="", icon='TRASH')

        # ----------------------------------------------------------------------
        # 3. TEXTURE QUEUE & INSPECTOR
        # ----------------------------------------------------------------------
        textures = scene.sollumz_dds_textures
        total_count = len(textures)

        if total_count > 0:
            layout.separator(factor=0.4)
            q_box = layout.box()
            q_col = q_box.column(align=True)

            # Header Row: Stats + Batch Override Toggle
            alpha_count = sum(1 for it in textures if it.has_alpha)
            override_count = sum(1 for it in textures if it.use_override)
            ok_count = sum(1 for it in textures if it.status == "OK")

            hdr = q_col.row(align=True)
            stat_text = f"Queue: {total_count}"
            if alpha_count:
                stat_text += f"  ·  {alpha_count} RGBA"
            if override_count:
                stat_text += f"  ·  {override_count} Pinned"
            if ok_count:
                stat_text += f"  ·  {ok_count} Done"
            hdr.label(text=stat_text, icon='TEXTURE')

            hdr_right = hdr.row(align=True)
            hdr_right.alignment = 'RIGHT'
            hdr_right.prop(
                settings,
                "show_batch_tools",
                text="Batch Rule",
                icon='MODIFIER',
                toggle=True,
            )

            q_col.separator(factor=0.5)

            # Search & Role Filter Bar
            flt_row = q_col.row(align=True)
            flt_row.prop(settings, "search_query", text="", icon='VIEWZOOM')
            flt_row.prop(settings, "role_filter", text="")

            # Collapsible Batch Override Drawer
            if settings.show_batch_tools:
                q_col.separator(factor=0.5)
                b_box = q_col.box()
                b_col = b_box.column(align=True)
                b_col.label(text="Batch Override Visible Items:", icon='LIBRARY_DATA_OVERRIDE')
                b_row = b_col.row(align=True)
                b_row.prop(settings, "bulk_format", text="")
                b_row.prop(settings, "bulk_resolution", text="")
                b_act = b_col.row(align=True)
                op_apply = b_act.operator("sollumzdds.bulk_override", text="Apply to Visible", icon='CHECKMARK')
                op_apply.clear_overrides = False
                op_clear = b_act.operator("sollumzdds.bulk_override", text="Reset All", icon='LOOP_BACK')
                op_clear.clear_overrides = True

            q_col.separator(factor=0.4)

            # Main Texture UIList
            q_col.template_list(
                "SOLLUMZDDS_UL_textures", "",
                scene, "sollumz_dds_textures",
                scene, "sollumz_dds_active_index",
                rows=7,
            )

            # Active Item Inline Inspector Footer
            if 0 <= scene.sollumz_dds_active_index < total_count:
                active = textures[scene.sollumz_dds_active_index]
                insp = q_col.box()
                insp_col = insp.column(align=True)

                top_line = insp_col.row(align=True)
                _, r_icon = _role_short_badge(active.role)
                alpha_badge = " [RGBA]" if active.has_alpha else " [RGB]"
                top_line.label(
                    text=f"{active.role}{alpha_badge}  ({active.width}×{active.height})",
                    icon=r_icon,
                )
                top_line.prop(
                    active,
                    "use_override",
                    text="Custom Rule",
                    icon='PINNED' if active.use_override else 'UNPINNED',
                    toggle=True,
                )

                if active.use_override:
                    insp_col.separator(factor=0.5)
                    ov_row = insp_col.row(align=True)
                    ov_row.prop(active, "format", text="")
                    ov_row.prop(active, "resolution", text="")

                if active.status.startswith("FAILED"):
                    err_row = insp_col.row(align=True)
                    err_row.alert = True
                    err_row.label(text=active.status, icon='ERROR')

        else:
            # Empty State Card
            empty_box = layout.box()
            empty_col = empty_box.column(align=True)
            empty_row = empty_col.row()
            empty_row.alignment = 'CENTER'
            empty_row.enabled = False
            empty_row.label(text="Queue is empty — Scan scene objects above to discover textures.", icon='INFO')

        # ----------------------------------------------------------------------
        # 4. GLOBAL COMPRESSION & OUTPUT DRAWER
        # ----------------------------------------------------------------------
        layout.separator(factor=0.4)
        cfg_box = layout.box()
        cfg_col = cfg_box.column(align=True)

        drawer_hdr = cfg_col.row(align=True)
        drawer_icon = 'TRIA_DOWN' if settings.show_settings_drawer else 'TRIA_RIGHT'
        drawer_hdr.prop(
            settings,
            "show_settings_drawer",
            text="Compression & Output Rules",
            icon=drawer_icon,
            emboss=False,
        )

        if settings.show_settings_drawer:
            cfg_col.separator(factor=0.6)

            # Default Format Row
            fmt_split = cfg_col.split(factor=0.38, align=True)
            fmt_split.label(text="Default Format")
            fmt_split.prop(settings, "global_format", text="")

            cfg_col.separator(factor=0.4)

            # Downscale / Max Cap Row
            res_split = cfg_col.split(factor=0.38, align=True)
            res_split.prop(settings, "use_quality_preset", text="Max Resolution")
            res_sub = res_split.row(align=True)
            res_sub.enabled = settings.use_quality_preset
            res_sub.prop(settings, "quality_preset", text="")

            cfg_col.separator(factor=0.8)

            # 3-Pill Toggle Bar: Power of Two | Mipmaps | Relink
            pills = cfg_col.row(align=True)
            pills.prop(settings, "enforce_power_of_two", text="Power of 2", toggle=True, icon='GRID')
            pills.prop(settings, "mip_generate", text="Mipmaps", toggle=True, icon='RENDERLAYERS')
            pills.prop(settings, "relink_after_convert", text="Relink", toggle=True, icon='LINKED')

            cfg_col.separator(factor=0.8)

            # Output Destination Row
            out_row = cfg_col.row(align=True)
            out_row.prop(
                settings,
                "use_custom_output_dir",
                text="Custom Folder" if settings.use_custom_output_dir else "Output: Next to Source Files",
                toggle=True,
                icon='FOLDER_REDIRECT' if settings.use_custom_output_dir else 'FILE_FOLDER',
            )
            if settings.use_custom_output_dir:
                out_row.operator("sollumzdds.open_output_dir", text="", icon='EXTERNAL_DRIVE')
                cfg_col.separator(factor=0.4)
                cfg_col.prop(settings, "output_dir", text="")

        # ----------------------------------------------------------------------
        # 5. PRIMARY CONVERT CTA FOOTER
        # ----------------------------------------------------------------------
        layout.separator(factor=0.6)
        cta = layout.row(align=True)
        cta.scale_y = 1.5
        cta.enabled = total_count > 0 and not SOLLUMZDDS_OT_convert._is_running

        if SOLLUMZDDS_OT_convert._is_running:
            cta.operator("sollumzdds.convert", text="Compressing... (ESC to Cancel)", icon='SORTTIME')
        else:
            cta_text = f"Convert {total_count} Texture(s) to DDS" if total_count > 0 else "Convert to DDS"
            cta.operator("sollumzdds.convert", text=cta_text, icon='EXPORT')


# ==============================================================================
# SOLLUMZ MATERIAL PROPERTIES PANEL HOOK
# ==============================================================================

_orig_sollumz_txtparams_draw = None
_sollumz_hook_active = False


def _hooked_sollumz_txtparams_draw(self, context):
    try:
        layout = self.layout
        aobj = context.active_object
        if aobj is None:
            return

        mat = aobj.active_material
        if mat is None:
            return

        collect_fn = None
        if _orig_sollumz_txtparams_draw is not None:
            collect_fn = _orig_sollumz_txtparams_draw.__globals__.get("collect_parameter_nodes")

        if collect_fn is not None:
            nodes = collect_fn(
                mat,
                lambda n: isinstance(n, bpy.types.ShaderNodeTexImage) and getattr(n, "is_sollumz", False),
            )
        else:
            nodes = [
                n
                for n in (mat.node_tree.nodes if mat.node_tree else [])
                if isinstance(n, bpy.types.ShaderNodeTexImage) and getattr(n, "is_sollumz", False)
            ]

        for n in nodes:
            SPLIT_FACTOR = 0.2
            split = layout.split(factor=SPLIT_FACTOR)
            split.alignment = "RIGHT"
            split.label(text=n.name)
            row = split.row(align=True)
            row.template_ID(n, "image", open="image.open")

            if n.image is not None:
                # 1. Exact vanilla Sollumz HD toggle (compact, preserves original dimensions)
                if hasattr(n.image, "sz_is_hd"):
                    subrow = row.row(align=True)
                    subrow.alignment = "RIGHT"
                    subrow.scale_x = 0.65
                    subrow.enabled = getattr(n.texture_properties, "embedded", True)
                    subrow.prop(n.image, "sz_is_hd", toggle=True, text="HD")

                # 2. Compact DDS Convert button (icon only so HD and image name never get squished)
                is_dds = (
                    (bool(n.image.filepath) and n.image.filepath.lower().endswith(".dds"))
                    or (bool(n.image.name) and n.image.name.lower().endswith(".dds"))
                    or (getattr(n.image, "file_format", "") == "DDS")
                )
                dds_row = row.row(align=True)
                dds_row.alignment = "RIGHT"
                dds_row.scale_x = 0.75
                pcoll = _preview_collections.get("main")
                has_custom_icons = pcoll is not None and "log_ok" in pcoll and "log_err" in pcoll
                if has_custom_icons:
                    dds_op = dds_row.operator(
                        "sollumzdds.convert_single",
                        text=" ",
                        icon_value=pcoll["log_ok"].icon_id if is_dds else pcoll["log_err"].icon_id,
                    )
                else:
                    if not is_dds:
                        dds_row.alert = True
                    dds_op = dds_row.operator(
                        "sollumzdds.convert_single",
                        text=" ",
                        icon="CHECKMARK" if is_dds else "VIEW_PAN",
                    )
                dds_op.material_name = mat.name
                dds_op.node_name = n.name

            if n.image is not None:
                split = layout.split(factor=SPLIT_FACTOR)
                _ = split.row()
                row = split.row()
                row.enabled = bool(n.image.filepath)
                if hasattr(n, "texture_properties") and hasattr(n.texture_properties, "embedded"):
                    row.prop(n.texture_properties, "embedded")
                if hasattr(n.image, "colorspace_settings"):
                    row.prop(n.image.colorspace_settings, "name", text="Color Space")

    except Exception:
        if _orig_sollumz_txtparams_draw is not None:
            _orig_sollumz_txtparams_draw(self, context)


def _hook_sollumz_panel():
    global _orig_sollumz_txtparams_draw, _sollumz_hook_active
    panel_cls = getattr(bpy.types, "SOLLUMZ_PT_TXTPARAMS_PANEL", None)
    if panel_cls is None:
        return False
    if _sollumz_hook_active:
        return True

    _orig_sollumz_txtparams_draw = panel_cls.draw
    panel_cls.draw = _hooked_sollumz_txtparams_draw
    _sollumz_hook_active = True
    return True


def _unhook_sollumz_panel():
    global _orig_sollumz_txtparams_draw, _sollumz_hook_active
    panel_cls = getattr(bpy.types, "SOLLUMZ_PT_TXTPARAMS_PANEL", None)
    if panel_cls and _orig_sollumz_txtparams_draw is not None:
        panel_cls.draw = _orig_sollumz_txtparams_draw
    _orig_sollumz_txtparams_draw = None
    _sollumz_hook_active = False


def _sollumz_hook_timer():
    if _hook_sollumz_panel():
        return None
    return 2.0


# ==============================================================================
# REGISTRATION
# ==============================================================================

CLASSES = (
    SollumzDDSPrepPreferences,
    SollumzDDSTextureItem,
    SollumzDDSSettings,
    SOLLUMZDDS_OT_scan,
    SOLLUMZDDS_OT_convert,
    SOLLUMZDDS_OT_convert_single,
    SOLLUMZDDS_OT_bulk_override,
    SOLLUMZDDS_OT_clear_list,
    SOLLUMZDDS_OT_open_output_dir,
    SOLLUMZDDS_UL_textures,
    SOLLUMZDDS_PT_main,
)


def register():
    # 1. Custom preview icons (console log badges)
    try:
        pcoll = bpy.utils.previews.new()
        icons_dir = os.path.join(ADDON_DIR, "icons")
        ok_path = os.path.join(icons_dir, "log_ok.png")
        err_path = os.path.join(icons_dir, "log_err.png")
        if os.path.exists(ok_path):
            pcoll.load("log_ok", ok_path, "IMAGE")
        if os.path.exists(err_path):
            pcoll.load("log_err", err_path, "IMAGE")
        _preview_collections["main"] = pcoll
    except Exception as e:
        print(f"[DDS Prep] Failed to load preview icons: {e}")

    for cls in CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.sollumz_dds_settings = bpy.props.PointerProperty(type=SollumzDDSSettings)
    bpy.types.Scene.sollumz_dds_textures = bpy.props.CollectionProperty(type=SollumzDDSTextureItem)
    bpy.types.Scene.sollumz_dds_active_index = bpy.props.IntProperty(default=0)

    if not _hook_sollumz_panel():
        try:
            bpy.app.timers.register(_sollumz_hook_timer, first_interval=0.5)
        except Exception:
            pass


def unregister():
    # 1. Clean up custom preview icons
    global _preview_collections
    for pcoll in list(_preview_collections.values()):
        try:
            bpy.utils.previews.remove(pcoll)
        except Exception:
            pass
    _preview_collections.clear()

    try:
        if bpy.app.timers.is_registered(_sollumz_hook_timer):
            bpy.app.timers.unregister(_sollumz_hook_timer)
    except Exception:
        pass

    _unhook_sollumz_panel()

    del bpy.types.Scene.sollumz_dds_active_index
    del bpy.types.Scene.sollumz_dds_textures
    del bpy.types.Scene.sollumz_dds_settings

    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()