# Origin Studio

Quick origin & 3D-cursor tools for **Blender 4.2+** (including 5.x).

Sidebar: `3D Viewport ▸ N-panel ▸ Origin Studio`

> This add-on was later renamed to **[Origo](https://github.com/WilmaStudio/Origo)** — see the [v1.1.0 release](https://github.com/WilmaStudio/Origo/releases/tag/v1.1.0).

## Features

### Tab 1 — Object Mode
- **World / Local** coordinate-space choice for the origin location.
- Per-axis origin placement: **Top / Mid / Down** for each of X / Y / Z — instant, one undo step per click.
- Quick presets: **Ground** (bottom-center) and **Center**.
- Works on every selected Mesh / Curve / Surface / Text object.
- Objects never jump visually, and their children stay in place. Multi-user data is made single-user automatically.

### Tab 2 — Edit Mode
- Big **"Cursor → Selection"** button (median of the selected vertices / edges / faces).
- Bonus: cursor to the bounding-box center of the selection.

## Installation

### Extension ZIP (Blender 4.2+)
1. Download `OriginStudio-1.0.0.zip` from the [v1.0.0 release](https://github.com/WilmaStudio/Origo/releases/tag/v1.0.0) — do **not** unzip it.
2. In Blender: `Edit ▸ Preferences ▸ Get Extensions`.
3. Open the arrow menu next to the search box and pick **Install from Disk** (or drag & drop the ZIP onto the Blender window).
4. Press **N** in the 3D viewport and open the **Origin Studio** tab.

### Legacy single file (Blender 4.2 – 4.5)
1. Download [`legacy/origin_studio.py`](legacy/origin_studio.py).
2. In Blender: `Edit ▸ Preferences ▸ Add-ons ▸` arrow menu `▸ Install Legacy Add-on…`.
3. Enable **Origin Studio**.

> ⚠️ Legacy add-on installation was removed in Blender 5.x — use the extension ZIP there.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
