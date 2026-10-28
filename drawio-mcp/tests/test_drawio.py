"""draw.io connector: generated diagrams are valid, input is sanitised, editor links round-trip."""
import asyncio
import base64
import importlib.util
import json
import sys
import zlib
from pathlib import Path
from urllib.parse import unquote

from defusedxml import ElementTree as ET
from fastmcp import Client

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("drawio_server", ROOT / "server.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def decode_url(url: str) -> str:
    """Reverse the official '#create=' link: URI-decode -> JSON -> base64 -> raw inflate -> URI-decode."""
    assert "#create=" in url and "#R" not in url, "editor_url must use the #create= format"
    frag = url.split("#create=", 1)[1]
    assert not any(ch in frag for ch in '"{}<>+/ '), "hash must be fully URI-encoded (found raw JSON/base64 characters)"
    payload = json.loads(unquote(frag))
    assert payload["type"] == "xml" and payload["compressed"] is True
    return unquote(zlib.decompress(base64.b64decode(payload["data"]), -15).decode())


def validate_model(xml: str):
    root = ET.fromstring(xml)
    cells = list(root.iter("mxCell"))
    ids = [c.get("id") for c in cells]
    check(len(ids) == len(set(ids)), "duplicate cell ids in generated XML")
    for c in cells:
        for a in ("source", "target", "parent"):
            if c.get(a):
                check(c.get(a) in ids, f"dangling {a}={c.get(a)}")
    return cells


async def call(c, tool, args):
    return json.loads((await c.call_tool(tool, args)).content[0].text)


async def expect_error(c, tool, args, needle, label):
    try:
        await c.call_tool(tool, args)
        check(False, f"{label}: expected an error, call succeeded")
    except Exception as exc:
        check(needle in str(exc), f"{label}: wrong error: {str(exc)[:200]}")


async def main():
    async with Client(mod.mcp) as c:
        # ---- happy path with layering
        r = await call(c, "create_diagram", {
            "title": "Order flow",
            "nodes": [{"id": "web", "label": "Web <App> & \"UI\"", "shape": "rounded", "fill": "#dae8fc", "stroke": "#6c8ebf"},
                      {"id": "api", "label": "API"}, {"id": "db", "label": "Orders DB", "shape": "cylinder"},
                      {"id": "q", "label": "Queue", "shape": "hexagon"}],
            "edges": [{"source": "web", "target": "api", "label": "HTTPS"}, {"source": "api", "target": "db"},
                      {"source": "api", "target": "q", "dashed": True}]})
        cells = validate_model(r["xml"])
        vertices = [x for x in cells if x.get("vertex")]
        check(len(vertices) == 4 and len([x for x in cells if x.get("edge")]) == 3, "wrong cell counts")
        geo = {x.get("id"): x.find("mxGeometry") for x in vertices}
        y = {k: float(g.get("y")) for k, g in geo.items()}
        check(y["n_web"] < y["n_api"] < y["n_db"], f"TB layering wrong: {y}")
        check(y["n_db"] == y["n_q"], "siblings should share a layer")
        label = next(x for x in vertices if x.get("id") == "n_web").get("value")
        check(label == 'Web <App> & "UI"', f"label did not survive escaping round-trip: {label!r}")
        check(decode_url(r["editor_url"]) == r["xml"], "editor_url does not round-trip to the XML")
        check(r["suggested_filename"] == "Order_flow.drawio", f"filename {r['suggested_filename']}")
        file_root = ET.fromstring(r["drawio_file"])
        check(file_root.tag == "mxfile", ".drawio file is not an <mxfile>")
        check(file_root.find("diagram/mxGraphModel") is not None, ".drawio file is missing its <mxGraphModel>")

        # ---- LR direction: x increases with layer
        r = await call(c, "create_diagram", {"direction": "LR", "nodes": [{"id": "a"}, {"id": "b"}],
                                             "edges": [{"source": "a", "target": "b"}]})
        gx = {x.get("id"): float(x.find("mxGeometry").get("x")) for x in validate_model(r["xml"]) if x.get("vertex")}
        check(gx["n_a"] < gx["n_b"], f"LR layering wrong: {gx}")

        # ---- cycles and self loops must not hang or crash
        r = await asyncio.wait_for(call(c, "create_diagram", {
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}, {"source": "c", "target": "a"},
                      {"source": "a", "target": "a"}]}), timeout=10)
        validate_model(r["xml"])

        # ---- large diagram
        big = {"nodes": [{"id": f"n{i}"} for i in range(250)],
               "edges": [{"source": f"n{i}", "target": f"n{i + 1}"} for i in range(249)]}
        validate_model((await call(c, "create_diagram", big))["xml"])

        # ---- rejected input
        await expect_error(c, "create_diagram", {"nodes": [{"id": "a", "shape": "blob"}]}, "Unknown shape", "bad shape")
        await expect_error(c, "create_diagram", {"nodes": [{"id": "a", "fill": 'red;" onload="x'}]}, "hex color", "style injection")
        await expect_error(c, "create_diagram", {"nodes": [{"id": "a"}, {"id": "a"}]}, "Duplicate node id", "dup ids")
        await expect_error(c, "create_diagram", {"nodes": [{"id": "a"}], "edges": [{"source": "a", "target": "zzz"}]},
                           "does not exist", "dangling edge")
        await expect_error(c, "create_diagram", {"nodes": []}, "at least one node", "empty")
        await expect_error(c, "create_diagram", {"nodes": [{"id": "a"}], "direction": "UP"}, "direction", "bad direction")
        await expect_error(c, "create_diagram", {"nodes": [{"id": f"n{i}"} for i in range(301)]}, "Too many nodes", "node cap")

        # ---- from_xml
        good = ('<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
                '<mxCell id="2" value="A" vertex="1" parent="1"><mxGeometry x="0" y="0" width="80" height="40" as="geometry"/></mxCell>'
                '<mxCell id="3" value="B" vertex="1" parent="1"><mxGeometry x="200" y="0" width="80" height="40" as="geometry"/></mxCell>'
                '<mxCell id="4" edge="1" source="2" target="3" parent="1"><mxGeometry relative="1" as="geometry"/></mxCell>'
                '</root></mxGraphModel>')
        r = await call(c, "create_diagram_from_xml", {"xml": good})
        check(len(validate_model(r["xml"])) == 5 and decode_url(r["editor_url"]) == r["xml"], "from_xml (model) failed")
        r = await call(c, "create_diagram_from_xml", {"xml": f'<mxfile><diagram name="p">{good}</diagram></mxfile>'})
        validate_model(r["xml"])
        bare = ('<mxCell id="2" value="A" vertex="1" parent="1"><mxGeometry width="80" height="40" as="geometry"/></mxCell>'
                '<mxCell id="3" value="B" vertex="1" parent="1"><mxGeometry x="200" width="80" height="40" as="geometry"/></mxCell>')
        r = await call(c, "create_diagram_from_xml", {"xml": bare})
        check(r["warnings"] and len(validate_model(r["xml"])) == 4, f"bare cells not wrapped: {r['warnings']}")

        bomb = ('<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                '<mxGraphModel><root><mxCell id="0" value="&b;"/></root></mxGraphModel>')
        await expect_error(c, "create_diagram_from_xml", {"xml": bomb}, "forbidden constructs", "entity bomb")
        xxe = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
               '<mxGraphModel><root><mxCell id="0" value="&x;"/></root></mxGraphModel>')
        await expect_error(c, "create_diagram_from_xml", {"xml": xxe}, "forbidden constructs", "XXE")
        await expect_error(c, "create_diagram_from_xml", {"xml": "<mxGraphModel><root>"}, "not well-formed", "malformed")
        await expect_error(c, "create_diagram_from_xml", {"xml": '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
                           '<mxCell id="1" parent="0"/></root></mxGraphModel>'}, "Duplicate cell ids", "dup cell ids")
        await expect_error(c, "create_diagram_from_xml", {"xml": '<mxGraphModel><root><mxCell id="0"/>'
                           '<mxCell id="2" edge="1" source="9" parent="0"/></root></mxGraphModel>'}, "does not exist", "dangling ref")
        await expect_error(c, "create_diagram_from_xml", {"xml": "<mxfile><diagram>eJxLzs8tSM0tyMnMSwUABy4DUw==</diagram></mxfile>"},
                           "compressed or empty", "compressed diagram")

        shapes = await call(c, "list_shapes", {})
        check("cylinder" in shapes["shapes"], "list_shapes")


if __name__ == "__main__":
    asyncio.run(main())
    print("drawio: " + ("PASS" if not failures else "FAIL\n  " + "\n  ".join(failures)))
    sys.exit(1 if failures else 0)
