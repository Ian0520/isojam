"""Extract self-contained SVG previews from finalized Archify HTML.

Run from any directory after generating the three HTML files. Only Python's
standard library is required. This reads markup; it never executes JavaScript.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path


def static_styles(styles: list[str], svg: str) -> tuple[str, str]:
    """Resolve the light palette without requiring CSS-variable support."""
    css = re.sub(r"/\*[\s\S]*?\*/", "", "\n".join(styles))
    rules = list(re.finditer(r"([^{}]+)\{([^{}]*)\}", css))
    palette = {}
    for wanted in (":root", ':root,[data-theme="dark"]', '[data-theme="light"]'):
        for rule in rules:
            selector = re.sub(r"\s+", "", rule[1])
            if selector == wanted:
                palette.update(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", rule[2]))

    def resolve(text: str, overrides: dict | None = None) -> str:
        values = palette | (overrides or {})

        def substitute(match: re.Match) -> str:
            return values[match[1]].strip()

        def mix(match: re.Match) -> str:
            first, percent, second = match.groups()
            rgb = tuple(int(first[i : i + 2], 16) for i in (1, 3, 5))
            weight = float(percent) / 100
            if second == "transparent":
                return f"rgba({rgb[0]}, {rgb[1]}, {rgb[2]}, {weight:g})"
            other = tuple(int(second[i : i + 2], 16) for i in (1, 3, 5))
            result = [round(a * weight + b * (1 - weight)) for a, b in zip(rgb, other)]
            return "#" + "".join(f"{channel:02x}" for channel in result)

        text = re.sub(r"var\((--[\w-]+)\)", substitute, text)
        return re.sub(
            r"color-mix\(in srgb,\s*(#[\da-fA-F]{6})\s+([\d.]+)%,\s*(#[\da-fA-F]{6}|transparent)\)",
            mix,
            text,
        )

    classes = set()
    for value in re.findall(r'\bclass="([^"]+)"', svg):
        classes.update(value.split())
    semantic = []
    for rule in rules:
        for selector in rule[1].split(","):
            match = re.fullmatch(r"(?:svg\s+)?\.([\w-]+)", selector.strip())
            if match and match[1] in classes:
                semantic.append(selector.strip() + " {" + resolve(rule[2]) + "}")
        prefix = 'svg[data-erd-ui="cards"] '
        selector = rule[1].strip()
        if selector.startswith(prefix) and "--er-hue:" not in rule[2]:
            selector = selector.removeprefix(prefix)
            for hue in (
                "database",
                "frontend",
                "backend",
                "cloud",
                "messagebus",
                "security",
            ):
                overrides = {
                    "--er-hue": palette[f"--{hue}-stroke"],
                    "--er-wash": palette[f"--{hue}-fill"],
                }
                declaration = resolve(rule[2], overrides)
                # The unscoped version styles the legend; domain groups
                # override it with their own inherited hue.
                if hue == "database":
                    semantic.append(selector + " {" + declaration + "}")
                semantic.append(f".er-hue-{hue} {selector} {{" + declaration + "}")
    # The first style block contains the self-contained fonts and OFL notices.
    fonts = styles[0] if "@font-face" in styles[0] else ""
    clean_svg = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", svg)
    return fonts + "\n" + "\n".join(semantic), resolve(clean_svg)


def export_svg(html_path: Path) -> Path:
    html = html_path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"<svg\b[\s\S]*?</svg>", html))
    if len(matches) != 1:
        raise ValueError(f"Expected one diagram SVG in {html_path.name}")
    svg = matches[0].group()
    styles = re.findall(r"<style[^>]*>([\s\S]*?)</style>", html)
    if not styles:
        raise ValueError(f"Missing diagram styles in {html_path.name}")
    css, svg = static_styles(styles, svg)
    if "]]>" in css:
        raise ValueError("Unexpected CDATA delimiter in styles")

    opening, content = svg.split(">", 1)
    viewbox = re.search(r'\bviewBox="([^"]+)"', opening)
    if not viewbox:
        raise ValueError("Missing diagram viewBox")
    _, _, width, height = map(float, viewbox[1].split())
    # Theme and dimensions are fixed for GitHub's image renderer. The complete
    # diagram is preserved; viewer chrome and JavaScript are not copied.
    opening += (
        ' xmlns="http://www.w3.org/2000/svg" data-theme="light"'
        f' width="{width:g}" height="{height:g}"'
    )
    static_css = (
        f"svg {{ width: {width:g}px !important; height: {height:g}px !important; "
        "min-width: 0 !important; color: #111827; "
        "font-family: 'JetBrains Mono', Consolas, monospace; }\n"
        "* { animation: none !important; transition: none !important; }\n"
        ".c-grid { stroke: transparent; }\n"
        ".c-region { stroke-dasharray: none; }\n"
    )
    result = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + opening
        + ">\n<style><![CDATA[\n"
        + css
        + "\n"
        + static_css
        + "]]></style>\n"
        + f'<rect width="{width:g}" height="{height:g}" fill="#ffffff"/>\n'
        + content
        + "\n"
    )
    tree = ET.fromstring(result)
    ns = {"svg": "http://www.w3.org/2000/svg"}
    if tree.findall(".//svg:script", ns) or tree.findall(".//svg:foreignObject", ns):
        raise ValueError("An SVG preview must not contain executable HTML")
    out = html_path.with_suffix(".svg")
    out.write_text(result, encoding="utf-8")
    return out


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    for name in ("system-overview", "job-execution", "database-schema"):
        print(export_svg(root / f"{name}.html").name)
