"""Create larger-text SVG and HTML derivatives of finalized Archify diagrams.

Run from any directory after generating the four native HTML files. Only Python's
standard library is required. This reads markup; it never executes JavaScript.
"""

import html
import json
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


def enlarge_text(svg: str, name: str) -> str:
    """Increase actual text sizes, preserving the native diagram's structure."""
    tree = ET.fromstring(svg)
    for text in tree.iter("text"):
        size = float(text.get("font-size", "10"))
        classes = text.get("class", "").split()
        if name == "database-schema":
            size = 16 if "data-node-label" in text.attrib else max(size, 12)
            if "data-detail" in text.attrib:
                size = 10 if size == 9 else 13 if size == 11 else 12
        elif name == "job-execution":
            size = 16 if "data-node-label" in text.attrib else max(size, 9)
            if any(c.startswith("t-edge-") for c in classes) or size == 11:
                size = 13
        elif "data-node-label" in text.attrib:
            size = 20
        elif "data-boundary-label" in text.attrib:
            size = 14
        elif text.get("data-detail") == "fine":
            size = 12
        elif text.get("data-detail") == "context":
            size = 14
        else:
            size = max(size, 12)
        text.set("font-size", f"{size:g}")

    # Edge labels sit over a small opaque mask. Resize that mask with the text
    # so enlarging letters does not expose the line running behind them.
    for group in tree.iter("g"):
        text = next((e for e in group if e.tag == "text"), None)
        mask = next(
            (e for e in group if e.tag == "rect" and e.get("class") == "c-mask"),
            None,
        )
        if text is None or mask is None:
            continue
        size = float(text.get("font-size", "12"))
        width = max(
            float(mask.get("width", "0")),
            len("".join(text.itertext())) * size * 0.6 + 12,
        )
        center = float(text.get("x", "0"))
        mask.set("x", f"{center - width / 2:g}")
        mask.set("width", f"{width:g}")
        mask.set("y", f"{float(text.get('y', '0')) - size:g}")
        mask.set("height", f"{size + 5:g}")
    return ET.tostring(tree, encoding="unicode")


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
    svg = enlarge_text(svg, html_path.stem)
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
    out.write_text(result, encoding="utf-8", newline="\n")
    return out


def export_readable(svg_path: Path) -> Path:
    """Present the large-text derivative without altering checked Archify HTML."""
    source_path = next((svg_path.parent / "sources").glob(f"{svg_path.stem}.*.json"))
    spec = json.loads(source_path.read_text(encoding="utf-8"))
    title = html.escape(spec["meta"]["title"])
    svg = svg_path.read_text(encoding="utf-8").split("?>", 1)[1]
    # Inline dimensions override the fixed-size standalone SVG stylesheet.
    svg = svg.replace(
        "<svg ",
        '<svg style="width:100% !important;height:auto !important;display:block" ',
        1,
    )
    cards = []
    for card in spec.get("cards", []):
        items = "".join(f"<li>{html.escape(item)}</li>" for item in card["items"])
        cards.append(
            f"<section><h2>{html.escape(card['title'])}</h2><ul>{items}</ul></section>"
        )
    result = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} | Large text</title>
<style>
body{{margin:0;background:#f3f4f6;color:#111827;font:16px/1.6 system-ui,sans-serif}}
main{{max-width:1100px;margin:auto;padding:24px}}
h1{{font-size:28px;line-height:1.3}} h2{{font-size:19px}}
a{{color:#174b8b}} nav{{margin-bottom:24px}} nav a{{margin-right:20px}}
.diagram,section{{background:white;border:1px solid #d1d5db;border-radius:8px;padding:16px;margin:20px 0}}
li{{margin:8px 0}} @media(max-width:650px){{main{{padding:10px}}.diagram{{padding:4px}}}}
@media print{{body{{background:white}} main{{max-width:none}}nav{{display:none}}}}
</style></head><body><main>
<h1>{title}</h1>
<p>Large-text view. Scroll to read the full diagram; use browser zoom for details.</p>
<nav><a href="{svg_path.name}">Open SVG</a><a href="{svg_path.stem}.html">Interactive Archify reader</a>
<a href="sources/{source_path.name}">Editable source</a></nav>
<div class="diagram">{svg}</div>
{"".join(cards)}
</main></body></html>
"""
    out = svg_path.with_name(f"{svg_path.stem}-readable.html")
    out.write_text(result, encoding="utf-8", newline="\n")
    return out


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    for name in (
        "system-overview",
        "runtime-components",
        "job-execution",
        "database-schema",
    ):
        svg = export_svg(root / f"{name}.html")
        print(svg.name)
        print(export_readable(svg).name)
