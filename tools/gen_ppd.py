#!/usr/bin/env python3
"""Generate ppd/Phomemo-T02.ppd — edit the SIZES list to add your own label formats."""

from pathlib import Path

W = 136.2  # 384 dots @ 203 dpi, in PostScript points
DEFAULT = "Receipt48"
SIZES = [  # (keyword, label, height in mm)
    ("w48h30", "48 x 30 mm", 30),
    ("w48h40", "48 x 40 mm", 40),
    ("w48h50", "48 x 50 mm", 50),
    ("w48h75", "48 x 75 mm", 75),
    ("w48h100", "48 x 100 mm", 100),
    ("w48h150", "48 x 150 mm", 150),
    ("w48h200", "48 x 200 mm", 200),
    ("Receipt48", "Ticket 48 mm (coupe auto)", 297),
]


def pt(mm: float) -> str:
    return f"{mm * 72 / 25.4:.2f}"


def build() -> str:
    L: list[str] = []
    a = L.append
    a('*PPD-Adobe: "4.3"')
    a("*% Phomemo T02 - macOS BLE bridge. macOS renders the page, t02bridge (LaunchAgent)")
    a("*% converts it and sends it over Bluetooth. Raster goes to socket://127.0.0.1:9100.")
    for line in (
        '*FormatVersion: "4.3"', '*FileVersion: "1.0.0"', "*LanguageVersion: English",
        "*LanguageEncoding: ISOLatin1", '*PCFileName: "PHOMT02.PPD"', '*Manufacturer: "Phomemo"',
        '*Product: "(T02)"', '*ModelName: "Phomemo T02"', '*ShortNickName: "Phomemo T02"',
        '*NickName: "Phomemo T02 (BLE bridge)"', '*PSVersion: "(3010.000) 0"', '*LanguageLevel: "3"',
        "*ColorDevice: False", "*DefaultColorSpace: Gray", "*FileSystem: False", '*Throughput: "1"',
        "*LandscapeOrientation: Plus90", "*TTRasterizer: Type42", "*cupsVersion: 2.2",
        "*cupsModelNumber: 0", "*cupsManualCopies: True", "*cupsSNMPSupplies: False",
        '*% "-" = pass-through: CUPS stops at raster and ships it to the backend untouched.',
        '*cupsFilter: "application/vnd.cups-raster 0 -"', "",
    ):
        a(line)

    a("*OpenUI *ColorModel/Color Mode: PickOne")
    a("*OrderDependency: 10 AnySetup *ColorModel")
    a("*DefaultColorModel: Gray")
    a('*ColorModel Gray/Grayscale: "<</cupsColorOrder 0/cupsColorSpace 18/cupsBitsPerColor 8/cupsCompression 0>>setpagedevice"')
    a("*CloseUI: *ColorModel")
    a("")
    a("*OpenUI *Resolution/Resolution: PickOne")
    a("*OrderDependency: 10 AnySetup *Resolution")
    a("*DefaultResolution: 203dpi")
    a('*Resolution 203dpi/203 dpi: "<</HWResolution[203 203]>>setpagedevice"')
    a("*CloseUI: *Resolution")
    a("")

    for kw in ("PageSize", "PageRegion"):
        a(f"*OpenUI *{kw}/Media Size: PickOne")
        a(f"*OrderDependency: 10 AnySetup *{kw}")
        a(f"*Default{kw}: {DEFAULT}")
        for key, name, h in SIZES:
            a(f'*{kw} {key}/{name}: "<</PageSize[{W} {pt(h)}]/ImagingBBox null>>setpagedevice"')
        a(f"*CloseUI: *{kw}")
        a("")
    a(f"*DefaultImageableArea: {DEFAULT}")
    for key, name, h in SIZES:
        a(f'*ImageableArea {key}/{name}: "0 0 {W} {pt(h)}"')
    a("")
    a(f"*DefaultPaperDimension: {DEFAULT}")
    for key, name, h in SIZES:
        a(f'*PaperDimension {key}/{name}: "{W} {pt(h)}"')
    a("")

    a("*VariablePaperSize: True")
    a(f'*MaxMediaWidth: "{W}"')
    a('*MaxMediaHeight: "14400"')
    a("*HWMargins: 0 0 0 0")
    a('*CustomPageSize True: "pop pop pop <</PageSize[5 -2 roll]/ImagingBBox null>>setpagedevice"')
    a(f"*ParamCustomPageSize Width: 1 points 36 {W}")
    a("*ParamCustomPageSize Height: 2 points 36 14400")
    a("*ParamCustomPageSize WidthOffset: 3 points 0 0")
    a("*ParamCustomPageSize HeightOffset: 4 points 0 0")
    a("*ParamCustomPageSize Orientation: 5 int 0 0")
    a("")

    def ui(kw, label, default, choices, slot):
        a(f"*OpenUI *{kw}/{label}: PickOne")
        a(f"*OrderDependency: 20 AnySetup *{kw}")
        a(f"*Default{kw}: {default}")
        for k, lab, v in choices:
            a(f'*{kw} {k}/{lab}: "<</cupsInteger{slot} {v}>>setpagedevice"')
        a(f"*CloseUI: *{kw}")
        a("")

    a("*% Job options travel in the raster header (cupsInteger0-3) - see t02bridge/pipeline.py")
    ui("Density", "Densité", "Normal",
       [("Light", "Clair", 1), ("Normal", "Normal", 2), ("Dark", "Foncé", 3), ("Darkest", "Très foncé", 4)], 0)
    ui("Dither", "Tramage", "Auto",
       [("Auto", "Auto (texte net, photo tramée)", 0), ("Floyd", "Photo - Floyd-Steinberg", 1),
        ("Threshold", "Texte - seuil net", 2), ("Atkinson", "Photo - Atkinson", 3)], 1)
    ui("FeedAfter", "Avance papier en fin de job", "None",
       [("None", "Aucune", 0), ("Feed5", "+5 mm", 40), ("Feed10", "+10 mm", 80), ("Feed15", "+15 mm", 120)], 2)
    ui("TrimBlank", "Couper le blanc en bas", "True",
       [("True", "Oui (économise le papier)", 1), ("False", "Non (taille exacte)", 2)], 3)
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "ppd" / "Phomemo-T02.ppd"
    out.write_text(build(), encoding="latin-1")
    print(f"wrote {out}")
