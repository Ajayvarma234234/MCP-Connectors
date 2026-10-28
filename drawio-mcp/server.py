"""draw.io (diagrams.net) MCP connector (FastMCP, Streamable HTTP).

draw.io has no authenticated API, so this connector needs NO credentials. It builds and validates
diagram XML locally and returns (a) the XML, (b) a ready-to-save .drawio file body and (c) a link that
opens the diagram in the draw.io editor. Nothing is stored server-side, so it is safe for any user.

Set DRAWIO_BASE_URL to point links at a self-hosted draw.io instead of https://app.diagrams.net.
"""
import base64
import json
import os
import re
import zlib
from collections import defaultdict
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

from defusedxml import ElementTree as SafeET  # blocks entity-expansion / external-entity attacks
from defusedxml.common import DefusedXmlException
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

EDITOR_BASE = os.getenv("DRAWIO_BASE_URL", "https://app.diagrams.net").rstrip("/")
MAX_NODES = 300
MAX_XML_CHARS = 2_000_000

mcp = FastMCP(
    name="draw.io MCP Server",
    instructions=(
        "Create draw.io diagrams. Use create_diagram for flowcharts/architecture diagrams from nodes and edges "
        "(layout is automatic), or create_diagram_from_xml to validate and open hand-written draw.io XML. "
        "Every result includes an editor link and the .drawio file contents."
    ),
)

SHAPES = {
    "rectangle": "rounded=0;whiteSpace=wrap;html=1;",
    "rounded": "rounded=1;whiteSpace=wrap;html=1;",
    "ellipse": "ellipse;whiteSpace=wrap;html=1;",
    "diamond": "rhombus;whiteSpace=wrap;html=1;",
    "cylinder": "shape=cylinder3;whiteSpace=wrap;html=1;boundedLbl=1;backgroundOutline=1;size=15;",
    "cloud": "ellipse;shape=cloud;whiteSpace=wrap;html=1;",
    "actor": "shape=umlActor;verticalLabelPosition=bottom;verticalAlign=top;html=1;",
    "document": "shape=document;whiteSpace=wrap;html=1;boundedLbl=1;",
    "hexagon": "shape=hexagon;perimeter=hexagonPerimeter2;whiteSpace=wrap;html=1;",
    "parallelogram": "shape=parallelogram;perimeter=parallelogramPerimeter;whiteSpace=wrap;html=1;",
}
DEFAULT_SIZE = {"diamond": (140, 90), "actor": (40, 70), "ellipse": (130, 70), "cylinder": (90, 90)}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
H_GAP, V_GAP = 60, 70


# ----------------------------------------------------------------------------- helpers
def _wrap_model(cells_xml: str, page_w: int = 1600, page_h: int = 1200) -> str:
    return (f'<mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" '
            f'arrows="1" fold="1" page="1" pageScale="1" pageWidth="{page_w}" pageHeight="{page_h}" math="0" '
            f'shadow="0"><root><mxCell id="0"/><mxCell id="1" parent="0"/>{cells_xml}</root></mxGraphModel>')


def _drawio_file(model_xml: str, title: str) -> str:
    return (f'<mxfile host="app.diagrams.net"><diagram id="page-1" name={quoteattr(title[:80])}>'
            f'{model_xml}</diagram></mxfile>')


def _editor_url(xml: str) -> str:
    """Editor link in the format the official draw.io MCP uses:

        #create=<URI-encoded JSON {"type":"xml","compressed":true,"data":<base64>}>
        where data = base64(deflateRaw(encodeURIComponent(xml)))

    The whole hash value must be URI-encoded (draw.io's own docs require it).
    """
    packed = quote(xml, safe="~()*!.'").encode()  # == JavaScript encodeURIComponent
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)  # raw deflate: no zlib header/checksum
    raw = comp.compress(packed) + comp.flush()
    payload = json.dumps({"type": "xml", "compressed": True, "data": base64.b64encode(raw).decode()},
                         separators=(",", ":"))
    return f"{EDITOR_BASE}/#create={quote(payload, safe='')}"


def _result(model_xml: str, title: str, warnings: list[str] | None = None) -> dict[str, Any]:
    return {"editor_url": _editor_url(model_xml), "xml": model_xml, "drawio_file": _drawio_file(model_xml, title),
            "suggested_filename": re.sub(r"[^A-Za-z0-9._-]+", "_", title)[:60].strip("_") + ".drawio",
            "warnings": warnings or []}


def _layers(ids: list[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """Longest-path layering. Cycles are tolerated by capping relaxation passes."""
    layer = {i: 0 for i in ids}
    for _ in range(len(ids)):
        changed = False
        for s, t in edges:
            if s != t and layer[t] < layer[s] + 1 and layer[s] + 1 < len(ids):
                layer[t] = layer[s] + 1
                changed = True
        if not changed:
            break
    return layer


# ----------------------------------------------------------------------------- tools
@mcp.tool
def create_diagram(nodes: list[dict], edges: list[dict] | None = None, direction: str = "TB",
                   title: str = "Diagram") -> dict:
    """Build a diagram with automatic layered layout.

    nodes: [{"id": "api", "label": "API Gateway", "shape": "rounded", "fill": "#dae8fc", "stroke": "#6c8ebf",
             "width": 140, "height": 60}]  (only id is required; label defaults to id)
      shape: rectangle | rounded | ellipse | diamond | cylinder | cloud | actor | document | hexagon | parallelogram
    edges: [{"source": "api", "target": "db", "label": "SQL", "dashed": false}]
    direction: TB (top-to-bottom) or LR (left-to-right).
    Returns the editor link, the XML, and the .drawio file contents.
    """
    edges = edges or []
    if direction not in ("TB", "LR"):
        raise ToolError("direction must be 'TB' or 'LR'")
    if not nodes:
        raise ToolError("Provide at least one node.")
    if len(nodes) > MAX_NODES:
        raise ToolError(f"Too many nodes ({len(nodes)}); the limit is {MAX_NODES}.")

    seen: set[str] = set()
    for n in nodes:
        nid = str(n.get("id", "")).strip()
        if not nid:
            raise ToolError("Every node needs a non-empty 'id'.")
        if nid in seen:
            raise ToolError(f"Duplicate node id: {nid}")
        seen.add(nid)
        if n.get("shape", "rectangle") not in SHAPES:
            raise ToolError(f"Unknown shape '{n['shape']}' on node {nid}. Use one of: {', '.join(SHAPES)}")
        for key in ("fill", "stroke"):
            if n.get(key) and not HEX.match(str(n[key])):
                raise ToolError(f"Node {nid}: '{key}' must be a hex color like #dae8fc")

    pairs: list[tuple[str, str]] = []
    for e in edges:
        s, t = str(e.get("source", "")), str(e.get("target", ""))
        if s not in seen or t not in seen:
            raise ToolError(f"Edge {s!r} -> {t!r} refers to a node id that does not exist.")
        pairs.append((s, t))

    ids = [str(n["id"]).strip() for n in nodes]
    layer = _layers(ids, pairs)
    by_layer: dict[int, list[dict]] = defaultdict(list)
    for n in nodes:
        by_layer[layer[str(n["id"]).strip()]].append(n)

    def size(n: dict) -> tuple[int, int]:
        dw, dh = DEFAULT_SIZE.get(n.get("shape", "rectangle"), (140, 60))
        return int(n.get("width", dw)), int(n.get("height", dh))

    # lay out layer by layer; each layer is centred on the widest one
    extents = {li: sum((size(n)[0] if direction == "TB" else size(n)[1]) + (H_GAP if direction == "TB" else V_GAP)
                       for n in ns) for li, ns in by_layer.items()}
    widest = max(extents.values())
    pos: dict[str, tuple[int, int]] = {}
    cursor_main = 40
    for li in sorted(by_layer):
        ns = by_layer[li]
        cursor_cross = 40 + (widest - extents[li]) // 2
        layer_main = max((size(n)[1] if direction == "TB" else size(n)[0]) for n in ns)
        for n in ns:
            w, h = size(n)
            pos[str(n["id"]).strip()] = (cursor_cross, cursor_main) if direction == "TB" else (cursor_main, cursor_cross)
            cursor_cross += (w + H_GAP) if direction == "TB" else (h + V_GAP)
        cursor_main += layer_main + (V_GAP if direction == "TB" else H_GAP + 20)

    cells: list[str] = []
    for n in nodes:
        nid = str(n["id"]).strip()
        shape = n.get("shape", "rectangle")
        style = SHAPES[shape]
        if n.get("fill"):
            style += f"fillColor={n['fill']};"
        if n.get("stroke"):
            style += f"strokeColor={n['stroke']};"
        w, h = size(n)
        x, y = pos[nid]
        label = escape(str(n.get("label", nid)), {'"': "&quot;"})
        cells.append(f'<mxCell id={quoteattr("n_" + nid)} value="{label}" style="{style}" vertex="1" parent="1">'
                     f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
    for i, e in enumerate(edges):
        style = "edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;html=1;endArrow=block;endFill=1;"
        if e.get("dashed"):
            style += "dashed=1;"
        label = escape(str(e.get("label", "")), {'"': "&quot;"})
        cells.append(f'<mxCell id="e_{i}" value="{label}" style="{style}" edge="1" parent="1" '
                     f'source={quoteattr("n_" + str(e["source"]))} target={quoteattr("n_" + str(e["target"]))}>'
                     f'<mxGeometry relative="1" as="geometry"/></mxCell>')

    total_w = max(1600, widest + 200)
    return _result(_wrap_model("".join(cells), total_w, max(1200, cursor_main + 200)), title)


@mcp.tool
def create_diagram_from_xml(xml: str, title: str = "Diagram") -> dict:
    """Validate hand-written draw.io XML and return an editor link plus the .drawio file contents.

    Accepts a full <mxfile>, a bare <mxGraphModel>, or just the <mxCell> elements. Checks that the XML is
    well-formed and safe, cell ids are unique, and every edge's source/target exists.
    """
    if len(xml) > MAX_XML_CHARS:
        raise ToolError("XML is too large.")
    text = xml.strip()
    try:
        try:
            root = SafeET.fromstring(text)
        except DefusedXmlException:
            raise
        except Exception:
            root = SafeET.fromstring(f"<x>{text}</x>")  # several sibling <mxCell> elements, no single root
    except DefusedXmlException:
        raise ToolError("XML contains forbidden constructs (entities / DTD).")
    except Exception as exc:
        raise ToolError(f"XML is not well-formed: {exc}")

    warnings: list[str] = []
    if root.tag == "mxfile":
        diagram = root.find("diagram")
        if diagram is None:
            raise ToolError("<mxfile> has no <diagram>.")
        inner = diagram.find("mxGraphModel")
        if inner is None:
            raise ToolError("This <diagram> is compressed or empty; provide uncompressed <mxGraphModel> XML.")
        model = inner
    elif root.tag == "mxGraphModel":
        model = root
    else:
        model = None

    cells = list(model.iter("mxCell")) if model is not None else list(root.iter("mxCell"))
    if not cells:
        raise ToolError("No <mxCell> elements found.")
    ids = [c.get("id") for c in cells]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ToolError(f"Duplicate cell ids: {sorted(map(str, dupes))[:5]}")
    idset = set(ids)
    if model is None:
        idset |= {"0", "1"}  # bare cells: the root cells are added below, so references to them are valid
    for c in cells:
        for attr in ("source", "target", "parent"):
            ref = c.get(attr)
            if ref and ref not in idset:
                raise ToolError(f"Cell {c.get('id')!r} has {attr}={ref!r}, which does not exist.")

    if model is not None:
        model_xml = SafeET.tostring(model, encoding="unicode")
    else:
        body = "".join(SafeET.tostring(c, encoding="unicode") for c in cells if c.get("id") not in ("0", "1"))
        if "0" not in ids:
            warnings.append("Added the required root cells (ids 0 and 1); new cells need parent=\"1\".")
        model_xml = _wrap_model(body)
    return _result(model_xml, title, warnings)


@mcp.tool
def list_shapes() -> dict:
    """List the shape names accepted by create_diagram."""
    return {"shapes": list(SHAPES), "directions": ["TB", "LR"]}


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "server": "drawio"})


if __name__ == "__main__":
    mcp.run(transport="http", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")),
            stateless_http=True)
