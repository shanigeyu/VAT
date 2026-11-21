"""Presentation preferences for the desktop UI. Stored project values stay canonical."""

THEMES = {"soft": "柔和纸白", "modern": "简约工作台", "classic": "经典工作台"}
ACCENTS = {"jade": ("松绿", "#19694f", "#e2f0e8", "#18342e"),
           "blue": ("靛蓝", "#3659a6", "#e7eefb", "#222d4c"),
           "violet": ("紫藤", "#76429c", "#f1e9f8", "#342642"),
           "amber": ("琥珀", "#945315", "#f8eddf", "#412e22")}
LANGUAGES = {"zh_CN": "简体中文", "zh_TW": "繁體中文"}


def mix(left, right, weight):
    parts = [round(int(left[i:i + 2], 16) * (1 - weight) + int(right[i:i + 2], 16) * weight)
             for i in (1, 3, 5)]
    return "#" + "".join(f"{value:02x}" for value in parts)


def palette(theme, accent):
    _, primary, tint, dark = ACCENTS.get(accent, ACCENTS["jade"])
    if theme == "soft":
        # 纸白底、低对比细线、松绿只做重点；卡片圆角由 widgets 读取 radius 绘制。
        return {
            "bg": "#F3F0E8", "surface": "#FBFAF6", "card": "#FFFFFF", "field": "#F7F3EA",
            "line": "#E5DFD1", "hairline": "#EDE8DB",
            "ink": "#23282B", "dim": "#6B7478", "faint": "#9AA2A1",
            "primary": mix(primary, "#ffffff", .05),
            "hover": mix(primary, "#000000", .16),
            "tint": mix(primary, "#ffffff", .86), "soft": "#EFEBE0",
            "rail": "#F0ECE2", "rail_text": "#56625F", "rail_title": "#23282B", "rail_dim": "#9AA29C",
            "active": mix(primary, "#ffffff", .84), "active_text": mix(primary, "#000000", .06),
            "shadow": "#E7E1D3", "chrome": "#22312D", "chrome_text": "#F2F0E9",
            "radius": 16, "radius_sm": 10, "rounded": True,
        }
    modern = theme != "classic"
    bg = "#f6f7f9" if modern else mix(dark, "#ffffff", .955)
    soft = "#eff1f4" if modern else mix(dark, "#ffffff", .91)
    return {
        "bg": bg, "surface": "#ffffff", "card": "#ffffff", "field": "#ffffff",
        "line": "#dce0e5" if modern else mix(dark, "#fffFFF", .82),
        "hairline": "#e8ebee" if modern else mix(dark, "#ffffff", .9),
        "ink": "#20252d" if modern else "#1c302b", "dim": "#576270" if modern else "#52665e",
        "faint": "#8b95a3" if modern else "#7d8f88",
        "primary": primary, "hover": mix(primary, "#000000", .18), "tint": tint, "soft": soft,
        "rail": "#ffffff" if modern else dark, "rail_text": "#46505e" if modern else "#e4eae7",
        "rail_title": "#222933" if modern else "#ffffff", "rail_dim": "#64707e" if modern else "#bfcfc8",
        "active": tint if modern else mix(dark, primary, .45),
        "active_text": primary if modern else "#ffffff",
        "shadow": "#e6e9ee" if modern else mix(dark, "#ffffff", .8),
        "chrome": "" if modern else dark, "chrome_text": "",
        "radius": 0, "radius_sm": 0, "rounded": False,
    }


current = palette("soft", "jade")
language = "zh_CN"


def apply_preferences(prefs):
    global current, language
    current = palette(prefs.get("theme"), prefs.get("accent"))
    language = prefs.get("language", "zh_CN")


def token(name, fallback=None):
    """Read a presentation token (radius, rounded, shadow…) with a fallback."""
    return current.get(name, fallback)


# Existing page helpers use the original semantic colours.
ROLES = {"#f5f6f2": "bg", "#ffffff": "surface", "#1c302b": "ink", "#52665e": "dim",
         "#d9e1d9": "line", "#18342e": "rail", "#19694f": "primary", "#d8eedc": "tint",
         "#eaf0e8": "soft", "#e5efe4": "tint", "#e6edef": "soft", "#e6eee3": "tint",
         "#eaf0e7": "soft", "#e8eee7": "soft", "#14573f": "hover", "#d9e5da": "tint",
         "#e2ede2": "tint", "#edf1eb": "soft", "#d8eddf": "tint", "#e5ece4": "soft"}


def color(value):
    return current[ROLES[value]] if value in ROLES else value
