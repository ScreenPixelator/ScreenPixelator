"""Builds ScreenPixelator-Setup.exe: a small Windows program (setup.c) with the
program files packed at its end. Needs: pip install ziglang

    python setup-exe/build.py
"""
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FILES = ["install.ps1", "screen_pixelator.py", "pixelator_settings.pyw", "yolo11n-seg.onnx", "pixelator.ico"]
STUB = os.path.join(HERE, "stub.exe")
OUT = os.path.join(ROOT, "ScreenPixelator-Setup.exe")

subprocess.run([sys.executable, "-m", "ziglang", "cc", "-target", "x86_64-windows-gnu", "-O2", "-s",
                "-o", STUB, os.path.join(HERE, "setup.c"), os.path.join(HERE, "setup.rc")],
               cwd=HERE, check=True)
with open(STUB, "rb") as f:
    exe = f.read()
with open(OUT, "wb") as f:
    f.write(exe)
    offset = f.tell()
    for name in FILES:
        with open(os.path.join(ROOT, name), "rb") as src:
            data = src.read()
        nb = name.encode("utf-8")
        f.write(struct.pack("<I", len(nb)) + nb + struct.pack("<Q", len(data)) + data)
    f.write(b"SPXSETUP" + struct.pack("<Q", offset))
print("built", OUT, os.path.getsize(OUT), "bytes")
