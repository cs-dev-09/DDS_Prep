# DDS Prep for Blender

A Blender add-on that turns your textures into game-ready DDS files and relinks them in your Sollumz materials. Built for GTA V modding. Windows only.

Give it `.png`, `.jpg` or `.tga` sources and it writes `.dds` files with the right block compression, power-of-two dimensions and mipmaps. Then it points the images in your Sollumz materials at the new files. The compressor (`csdevcompress.exe`) is bundled in the `bin` folder, so there's nothing extra to download.

Works with Blender 4.4, 5.0 and 5.2+.

## What it does

- Converts textures in batches on background threads. You get a progress bar, and ESC cancels.
- Picks a compression format from each texture's Sollumz sampler role (see below).
- Rounds sizes to a power of two, and can cap the max resolution (4096, 2048, 1024, 512, 256, 50% or 25%).
- Builds mipmaps with the Mitchell-Netravali filter, stopping at 4x4.
- Relinks the converted textures in your materials without touching color space, alpha mode, the Embedded toggle or the HD flag (`sz_is_hd`).
- Handles textures packed inside the `.blend`, so you don't have to extract them first.
- Adds a small status badge next to each texture in the Sollumz Texture Parameters panel.

Your source images are never overwritten.


## Defaults

| Setting | Default | Notes |
|:---|:---|:---|
| Compression | Auto (By Role) | Diffuse and Normal maps default to BC1; transparent images keep their alpha (BC3). |
| Mipmaps | On | Full chain, Mitchell-Netravali filter. |
| Minimum mip size | 4x4 | 1x1 and 2x2 mips can show up as black pixels in game, so it stops at 4x4. |
| Mip gamma correction | Automatic | On for diffuse. Off for normal and specular, so normals and roughness don't get warped. |
| Power of two | On | Something like 1023x511 gets rounded to the nearest valid size. Range is 4x4 up to 8192x8192, aspect ratio kept. |
| Relink materials | On | Updates the image datablocks to the new .dds files while preserving all node settings (Embedded toggle, HD flag, colorspace). |
| Output location | Next to the source files | Or pick a custom folder. |
| Worker threads | 8 | Change it in the add-on preferences, anywhere from 1 to 32. |

## Installation

1. Download the `sollumz_dds_prep` zip.
2. In Blender, go to Edit > Preferences > Add-ons > Install from Disk. (On Blender 4.2 and later it's under the dropdown arrow in the top right of the Add-ons tab.)
3. Pick the zip and enable DDS Prep.

The panel shows up in the 3D Viewport sidebar (press N) under the DDS Prep tab.

## Using it

### Batch conversion

Open the sidebar (N) and go to the DDS Prep tab.

1. Choose a scope: selected objects, the entire scene, or one collection.
2. Click the scan button to list the textures it finds.
3. Look through the list. You can search by name or filter by role (Diffuse, Normal, Specular and so on). Set overrides on single textures, or use Batch Rule for many at once.
4. Adjust the rules if you want to. Auto format, power of two, mipmaps and relinking are all fine as they are for most GTA V assets. Turn on Max Resolution if you need to shrink everything, and use Custom Folder if you don't want the DDS files next to your sources.
5. Click Convert Texture(s) to DDS. Progress shows in Blender's status bar.

### Single texture

Open Properties > Material > Texture Parameters. Each texture slot (`DiffuseSampler`, `BumpSampler`, `SpecSampler` and so on) gets a badge:

- Green: already a `.dds`.
- Red: not a `.dds` yet.

Click the badge, choose a format (BC1, BC3, BC5 or BC7) and a resolution, then hit OK.

## Repository layout

```
sollumz_dds_prep/
├── bin/
│   └── csdevcompress.exe    # bundled compressor built from nvidia-texture-tools
├── icons/
│   ├── log_ok.png
│   └── log_err.png
├── __init__.py              # registration, UI, properties, modal operator
├── core.py                  # texture classification, PoT rules, subprocess runner
├── .gitignore
└── README.md
```

## Compatibility

- Blender 4.4, 5.0, 5.2+
- Sollumz (GTA V / RAGE)
- Windows, since the bundled compressor is a Windows executable

## Credits & Acknowledgements

The compression backend (`csdevcompress.exe`) is built upon the open-source [nvidia-texture-tools](https://github.com/castano/nvidia-texture-tools) by **Ignacio Castaño** (MIT License).


