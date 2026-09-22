#!/usr/bin/env python3
"""Generate QLC+ fixture profiles (.qxf) from extracted channel maps.

Input : fixture-profiles/channels.json  (produced by the extraction pass; one entry per
        manufacturer/model/mode with an ORDERED channel list)
Output: .qxf files in the QLC+ user fixture directory
        ~/Library/Application Support/QLC+/Fixtures/<Manufacturer>/

Deliberate scope: channel NAMES, ORDER and COUNT come from the source library, because that is
what decides whether DMX lands on the right attribute. Channel GROUPS are inferred from the name
(so QLC+'s fixture manager and the 2D/3D views behave sensibly); QLC+ presets and capability
ranges are NOT invented - a wrong preset silently changes how QLC+ treats a channel, and we have
no capability data from the source. Types already present in QLC+'s own library are skipped.

Usage: python3 generate_qxf.py [--force] [--out DIR]
"""
from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
CHANNELS = os.path.join(HERE, "channels.json")
DEFAULT_OUT = os.path.expanduser("~/Library/Application Support/QLC+/Fixtures")
QLC_LIB = "/Users/tom/Hermes/qlcplus/src/resources/fixtures"

GROUP_RULES = [
    (r"pan", "Pan"), (r"tilt", "Tilt"),
    (r"dimmer|intensity|master|^i$", "Intensity"),
    (r"red|green|blue|white|amber|uv|cto|ctb|colour|color|cyan|magenta|yellow", "Color"),
    (r"gobo|gobo ?(rot|wheel)|animation|prism", "Gobo"),
    (r"shutter|strobe|stop", "Shutter"),
    (r"zoom|focus|frost|iris|beam|blade|framing|lens", "Beam"),
    (r"speed|time|rate|fade", "Speed"),
    (r"effect|macro|fx", "Effect"),
    (r"reset|lamp|maintenance|control|function|dimmer ?curve", "Maintenance"),
]


def group_of(name: str) -> str:
    low = name.lower()
    for pattern, group in GROUP_RULES:
        if re.search(pattern, low):
            return group
    return "Effect"


def type_of(manufacturer: str, model: str, channels: list[dict]) -> str:
    low = f"{manufacturer} {model}".lower()
    if any(re.search(p, low) for p in (r"wash", r"profile", r"spot", r"beam", r"moving", r"mac ", r"sparx", r"p18", r"p12", r"a12", r"vl1\d", r"varyscan", r"motoryoke", r"solaframe", r"solawash", r"mov")):
        if "wash" in low:
            return "Moving Head"
        return "Moving Head"
    if "cycle|cyc|nano|spectra|led|batten|t5" and re.search(r"cycl|cyc|nano|spectra|led", low):
        return "Color Changer"
    if re.search(r"par|fresnel|pc |theatre|studio|compact|leonardo|bulb|niethammer|dimmer|mover|mag max", low):
        return "Dimmer"
    if "projector" in low or "ds20" in low:
        return "Other"
    return "Dimmer" if len(channels) <= 2 else "Color Changer"


def qlc_library_types() -> set[tuple[str, str]]:
    out = set()
    if not os.path.isdir(QLC_LIB):
        return out
    ln = lambda t: t.split("}", 1)[1] if "}" in t else t
    for root_dir, _, files in os.walk(QLC_LIB):
        for f in files:
            if not f.endswith(".qxf"):
                continue
            try:
                root = ET.parse(os.path.join(root_dir, f)).getroot()
            except Exception:
                continue
            man = mod = ""
            for c in root:
                if ln(c.tag) == "Manufacturer":
                    man = (c.text or "").strip()
                elif ln(c.tag) == "Model":
                    mod = (c.text or "").strip()
            if man and mod:
                out.add((man.lower(), mod.lower()))
    return out


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def main() -> int:
    force = "--force" in sys.argv
    out_dir = DEFAULT_OUT
    if "--out" in sys.argv:
        out_dir = sys.argv[sys.argv.index("--out") + 1]
    channels_path = sys.argv[sys.argv.index("--in") + 1] if "--in" in sys.argv else CHANNELS

    entries = json.load(open(channels_path))
    known = qlc_library_types()
    by_model: OrderedDict = OrderedDict()
    skipped, done = [], []

    for e in entries:
        man, mod, mode = e.get("manufacturer", "").strip(), e.get("model", "").strip(), e.get("mode", "")
        chans = e.get("channels") or []
        if not man or not mod:
            continue
        if not force and (man.lower(), mod.lower()) in known:
            skipped.append(f"{man} | {mod} (already in QLC+ library)")
            continue
        if not chans:
            skipped.append(f"{man} | {mod} (no channel list from any source)")
            continue
        by_model.setdefault((man, mod), []).append((mode or "Default", [c["name"] for c in chans]))

    for (man, mod), modes in by_model.items():
        # unique channel names, in first-seen order across modes
        names: list[str] = []
        for _, ch_names in modes:
            for n in ch_names:
                if n not in names:
                    names.append(n)
        if not names:
            continue
        declared = []
        used = {}
        for n in names:
            base = n or "Channel"
            uniq = base
            idx = 2
            while uniq in used:
                uniq = f"{base} #{idx}"
                idx += 1
            used[uniq] = True
            declared.append(uniq)
        lookup = {n: d for n, d in zip(names, declared)}
        ftype = type_of(man, mod, [{"name": n} for n in names])

        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<!DOCTYPE FixtureDefinition>",
                 '<FixtureDefinition xmlns="http://www.qlcplus.org/FixtureDefinition">',
                 " <Creator>", "  <Name>Q Light Controller Plus</Name>", "  <Version>5.2.2</Version>",
                 f"  <Author>Hermes - channel map from Capture/grandMA3 library</Author>", " </Creator>",
                 f" <Manufacturer>{esc(man)}</Manufacturer>", f" <Model>{esc(mod)}</Model>",
                 f" <Type>{ftype}</Type>"]
        for n, d in zip(names, declared):
            lines.append(f' <Channel Name="{esc(d)}"><Group Byte="0">{group_of(n)}</Group></Channel>')
        for mode_name, ch_names in modes:
            lines.append(f' <Mode Name="{esc(mode_name)}">')
            for i, n in enumerate(ch_names):
                lines.append(f'  <Channel Number="{i}">{esc(lookup[n])}</Channel>')
            lines.append(" </Mode>")
        lines.append("</FixtureDefinition>")

        safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{man}-{mod}")
        # QLC+'s user fixture loader reads files in that directory ONLY (no subdirectories, it
        # warns "Unrecognized fixture extension" on a folder), so profiles must be written flat.
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{safe}.qxf")
        open(path, "w").write("\n".join(lines) + "\n")
        done.append(f"{man} | {mod}: {len(declared)} channels, {len(modes)} mode(s), type {ftype} -> {path}")

    print(f"wrote {len(done)} profile(s) into {out_dir}")
    for d in done:
        print("  +", d)
    if skipped:
        print(f"\nskipped {len(skipped)}:")
        for s in skipped:
            print("  -", s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
