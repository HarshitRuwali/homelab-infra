#!/usr/bin/env python3
"""Run the desk_appliance btusb DKMS prepare script against fixture sources.

Standard library only (plus bash, tar and xz on the PATH); run from anywhere:

    python3 -I ansible/tests/desk-appliance-btusb-prepare-test.py

The script is templates/btusb-prepare.sh.j2, which is plain bash. Each case
builds a linux-source style tarball and runs the script in a fresh build
directory as DKMS would. Most cases name the tarball directly through
DESK_BTUSB_SOURCE_TARBALL; the lookup cases put a fake dpkg-query on PATH and
a merged-/usr module tree (lib -> usr/lib) in DESK_BTUSB_MODULES_DIR instead.
It checks:

  * a missing id is added at the top of quirks_table and nowhere else, and the
    rest of btusb.c is unchanged
  * an id already in quirks_table (any hex case) is not added twice, and when
    nothing is left to add the script exits 77 (DKMS: skipped)
  * an id that is only in the generic btusb_table still counts as missing
  * the split layout of 7.3 (btusb_main.c, btusb-y lines) is patched and its
    btusb-* Makefile lines reach the generated Kbuild
  * every way it can fail exits 77 (DKMS: skipped), never 1 or 2: no tarball,
    no list, no headers, a tarball without drivers/bluetooth, a corrupt
    tarball, a malformed id or flags, two quirks_table definitions, and a
    compile error (the compile goes through a fake kbuild directory), and an
    unexpected command failure (the ERR trap)
  * a duplicate id is added once, a backslash-continued btusb-* Makefile line
    is joined, and a comment that mentions quirks_table is not patched
  * the dpkg lookup finds the linux-source at the version of the package that
    owns the in-tree btusb.ko when dpkg records only the /usr/lib path, and
    declines (77) when the versions differ
"""
import io
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile

ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ANSIBLE / "roles" / "desk_appliance" / "templates" / "btusb-prepare.sh.j2"
errors = []
ROOT = tempfile.TemporaryDirectory(prefix="btusb-prepare-test.")

QUIRKS_OPEN = "static const struct usb_device_id quirks_table[] = {"
ADDED = "\t/* Added by desk_appliance (btusb-quirks.list) */"


def btusb_source(generic_ids=(), quirk_ids=()):
    def entries(ids, info):
        return "".join(
            f"\t{{ USB_DEVICE(0x{v}, 0x{p}), .driver_info = {info} }},\n" for v, p in ids
        )

    return (
        '#include "btmtk.h"\n\n'
        "static const struct usb_device_id btusb_table[] = {\n"
        "\t{ USB_DEVICE_INFO(0xe0, 0x01, 0x01) },\n"
        + entries(generic_ids, "0")
        + "\t{ }\t/* Terminating entry */\n};\n\n"
        + QUIRKS_OPEN
        + "\n\t/* CSR BlueCore devices */\n"
        "\t{ USB_DEVICE(0x0a12, 0x0001), .driver_info = BTUSB_CSR },\n"
        + entries(quirk_ids, "BTUSB_MEDIATEK")
        + "\t{ }\t/* Terminating entry */\n};\n\n"
        "static int btusb_probe(void) { return 0; }\n"
    )


SINGLE_MAKEFILE = (
    "obj-$(CONFIG_BT_HCIBTUSB)\t+= btusb.o\n"
    "obj-$(CONFIG_BT_MTK)\t\t+= btmtk.o\n"
    "hci_uart-y\t\t\t:= hci_ldisc.o\n"
)
SPLIT_MAKEFILE = (
    "obj-$(CONFIG_BT_HCIBTUSB)\t+= btusb.o\n"
    "btusb-y\t\t\t\t:= btusb_main.o\n"
    "btusb-$(CONFIG_BT_HCIBTUSB_QCOM)\t+= btusb_qcom.o\n"
)


def make_tarball(path, files):
    with tarfile.open(path, "w:xz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"linux-source-9.9/drivers/bluetooth/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


FAKE_DPKG_QUERY = r"""#!/bin/bash
# Answers the four dpkg-query calls btusb-prepare.sh makes, from FAKE_* vars.
case "$1" in
  -S)
    if [ "$2" = "$FAKE_OWNED" ]; then echo "linux-image-9.9.0-test: $2"; exit 0; fi
    echo "dpkg-query: no path found matching pattern $2" >&2; exit 1 ;;
  -W)
    case "$4" in
      linux-image-9.9.0-test) printf '%s' "$FAKE_IMAGE_VERSION" ;;
      'linux-source-*') printf 'linux-source-9.9\t%s\tii \n' "$FAKE_SOURCE_VERSION" ;;
      *) exit 1 ;;
    esac ;;
  -L)
    if [ "$2" = linux-source-9.9 ]; then echo /usr/src; echo "$FAKE_TARBALL"; exit 0; fi
    exit 1 ;;
esac
"""


# `make -C <this> M=<build> modules` stands in for the kernel's kbuild: it
# records the call and fails when FAKE_MAKE_FAIL is set.
FAKE_KBUILD_MAKEFILE = """modules:
\t@test -z "$(FAKE_MAKE_FAIL)" || { echo "fake compile error" >&2; exit 2; }
\t@echo "$(KERNELRELEASE)" > "$(M)/btusb.ko"
"""


def run(files, quirks, *, tarball=True, write_list=True, lookup=None,
        headers=True, make_fails=False, raw_tarball=None, failing_mktemp=False):
    """Run the script in a fresh build dir; return (rc, output, build dir).

    lookup, when given, is (image version, source version): the tarball is
    then found through the fake dpkg-query instead of being named directly.
    """
    tmp = pathlib.Path(tempfile.mkdtemp(dir=ROOT.name))
    build = tmp / "build"
    build.mkdir()
    tar_path = tmp / "linux-source-9.9.tar.xz"
    if raw_tarball is not None:
        tar_path.write_bytes(raw_tarball)
    elif tarball:
        make_tarball(tar_path, files)
    if write_list:
        (build / "btusb-quirks.list").write_text(
            "# a comment line\n\n" + "".join(f"{q}\n" for q in quirks)
        )
    kbuild = tmp / "kbuild"
    kbuild.mkdir()
    (kbuild / "Makefile").write_text(FAKE_KBUILD_MAKEFILE)
    if headers:
        (kbuild / "include").mkdir()
    env = dict(
        os.environ,
        DESK_BTUSB_SOURCE_TARBALL=str(tar_path),
        DESK_BTUSB_KERNEL_BUILD_DIR=str(kbuild),
    )
    env.pop("FAKE_MAKE_FAIL", None)
    if make_fails:
        env["FAKE_MAKE_FAIL"] = "1"
    if failing_mktemp:
        shim = tmp / "shim"
        shim.mkdir()
        (shim / "mktemp").write_text("#!/bin/sh\nexit 1\n")
        (shim / "mktemp").chmod(0o755)
        env["PATH"] = f"{shim}:{env['PATH']}"
    if lookup:
        del env["DESK_BTUSB_SOURCE_TARBALL"]
        bt = tmp / "usr/lib/modules/9.9.0-test/kernel/drivers/bluetooth"
        bt.mkdir(parents=True)
        (bt / "btusb.ko.xz").write_bytes(b"")
        (tmp / "lib").symlink_to("usr/lib")
        fake_bin = tmp / "bin"
        fake_bin.mkdir()
        (fake_bin / "dpkg-query").write_text(FAKE_DPKG_QUERY)
        (fake_bin / "dpkg-query").chmod(0o755)
        env.update(
            PATH=f"{fake_bin}:{env['PATH']}",
            DESK_BTUSB_MODULES_DIR=str(tmp / "lib/modules"),
            FAKE_OWNED=str(bt / "btusb.ko.xz"),
            FAKE_IMAGE_VERSION=lookup[0],
            FAKE_SOURCE_VERSION=lookup[1],
            FAKE_TARBALL=str(tar_path),
        )
    proc = subprocess.run(
        ["bash", str(SCRIPT), "9.9.0-test"],
        cwd=build,
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr, build


def check(name, cond, detail=""):
    if not cond:
        errors.append(f"{name}: {detail}")


def quirks_region(text):
    start = text.index(QUIRKS_OPEN)
    return text[start : text.index("\n};", start)]


MT7902 = "0489 e156 BTUSB_MEDIATEK | BTUSB_WIDEBAND_SPEECH"
MT7902_LINE = "\t{ USB_DEVICE(0x0489, 0xe156), .driver_info = BTUSB_MEDIATEK | BTUSB_WIDEBAND_SPEECH },"

# The script is executed as is, so it must not need Jinja rendering.
text = SCRIPT.read_text()
check("plain bash", "{{" not in text and "{%" not in text, "template syntax in the script")

# 1. Single-file layout, id missing: added at the top of quirks_table only.
original = btusb_source()
rc, out, build = run(
    {"btusb.c": original, "btmtk.h": "/* h */\n", "Makefile": SINGLE_MAKEFILE}, [MT7902]
)
check("single: exit", rc == 0, f"rc={rc} {out}")
if rc == 0:
    patched = (build / "btusb.c").read_text()
    check(
        "single: placement",
        f"{QUIRKS_OPEN}\n{ADDED}\n{MT7902_LINE}\n" in patched,
        "entry not directly after the quirks_table opening line",
    )
    check(
        "single: nothing else changed",
        patched.replace(f"{ADDED}\n{MT7902_LINE}\n", "") == original,
        "btusb.c differs beyond the added lines",
    )
    check("single: header copied", (build / "btmtk.h").is_file())
    check("single: upstream Makefile not copied", not (build / "Makefile").exists())
    kbuild = (build / "Kbuild").read_text().splitlines()
    check("single: Kbuild", kbuild[1:] == ["obj-m := btusb.o"], f"{kbuild}")
    built = build / "btusb.ko"
    check(
        "single: compiled for the kernel",
        built.is_file() and built.read_text().strip() == "9.9.0-test",
        "make was not run with M=<build dir> and KERNELRELEASE",
    )

# 2. Already in quirks_table, upper-case hex: nothing to add, skipped.
rc, out, build = run(
    {"btusb.c": btusb_source(quirk_ids=[("0489", "E156")]), "Makefile": SINGLE_MAKEFILE},
    [MT7902],
)
check("present: exit 77", rc == 77, f"rc={rc} {out}")
check("present: reason", "already" in out, out)

# 3. Only in the generic btusb_table: still missing from quirks_table.
rc, out, build = run(
    {"btusb.c": btusb_source(generic_ids=[("0489", "e156")]), "Makefile": SINGLE_MAKEFILE},
    [MT7902],
)
check("generic only: exit", rc == 0, f"rc={rc} {out}")
if rc == 0:
    region = quirks_region((build / "btusb.c").read_text())
    check("generic only: added to quirks", MT7902_LINE in region, region)

# 4. Two ids, one present and one missing: only the missing one is added.
rc, out, build = run(
    {"btusb.c": btusb_source(quirk_ids=[("13d3", "3579")]), "Makefile": SINGLE_MAKEFILE},
    ["13d3 3579 BTUSB_MEDIATEK", MT7902],
)
check("mixed: exit", rc == 0, f"rc={rc} {out}")
if rc == 0:
    region = quirks_region((build / "btusb.c").read_text())
    check("mixed: missing added", MT7902_LINE in region, region)
    check("mixed: present not doubled", region.count("0x13d3, 0x3579") == 1, region)

# 5. Split layout (7.3): btusb_main.c patched, btusb-* lines in Kbuild.
rc, out, build = run(
    {
        "btusb_main.c": btusb_source(),
        "btusb_qcom.c": "/* qcom */\n",
        "btusb_qcom.h": "/* qcom */\n",
        "Makefile": SPLIT_MAKEFILE,
    },
    [MT7902],
)
check("split: exit", rc == 0, f"rc={rc} {out}")
if rc == 0:
    check("split: patched", MT7902_LINE in (build / "btusb_main.c").read_text())
    kbuild = (build / "Kbuild").read_text().splitlines()
    check(
        "split: Kbuild",
        kbuild[1:]
        == [
            "obj-m := btusb.o",
            "btusb-y\t\t\t\t:= btusb_main.o",
            "btusb-$(CONFIG_BT_HCIBTUSB_QCOM)\t+= btusb_qcom.o",
        ],
        f"{kbuild}",
    )

# 6. Every reason to decline exits 77, so DKMS never reports a failure.
single = {"btusb.c": btusb_source(), "Makefile": SINGLE_MAKEFILE}
for name, kwargs, quirks in [
    ("no tarball", {"tarball": False}, [MT7902]),
    ("no list", {"write_list": False}, [MT7902]),
    ("bad id", {}, ["489 e156 BTUSB_MEDIATEK"]),
    ("bad flags", {}, ["0489 e156 BTUSB_MEDIATEK|rm -rf /"]),
    ("empty list", {}, []),
    ("no headers", {"headers": False}, [MT7902]),
    ("compile error", {"make_fails": True}, [MT7902]),
    ("corrupt tarball", {"raw_tarball": b"\xfd7zXZ\x00 not really xz"}, [MT7902]),
    # No explicit check covers this one; the ERR trap turns it into 77.
    ("unexpected failure", {"failing_mktemp": True}, [MT7902]),
]:
    rc, out, _ = run(single, quirks, **kwargs)
    check(f"{name}: exit 77", rc == 77, f"rc={rc} {out}")

# 6b. A tarball without drivers/bluetooth, and two quirks_table definitions.
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:xz") as tar:
    data = b"obj-y += x.o\n"
    info = tarfile.TarInfo("linux-source-9.9/sound/Makefile")
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))
rc, out, _ = run({}, [MT7902], raw_tarball=buf.getvalue())
check("no drivers/bluetooth: exit 77", rc == 77, f"rc={rc} {out}")
check("no drivers/bluetooth: reason", "drivers/bluetooth" in out, out)
twice = btusb_source() + "\n" + QUIRKS_OPEN + "\n\t{ }\n};\n"
rc, out, _ = run({"btusb.c": twice, "Makefile": SINGLE_MAKEFILE}, [MT7902])
check("two tables: exit 77", rc == 77, f"rc={rc} {out}")

# 6c. A comment mentioning the table is not patched; duplicates added once;
# a continued Makefile line is joined.
commented = "/* see quirks_table[] = { below */\n" + btusb_source()
rc, out, build = run(
    {"btusb.c": commented, "Makefile": SINGLE_MAKEFILE}, [MT7902, MT7902]
)
check("comment and duplicate: exit", rc == 0, f"rc={rc} {out}")
if rc == 0:
    patched = (build / "btusb.c").read_text()
    check("comment not patched", patched.startswith("/* see quirks_table[] = { below */\n#include"), patched[:120])
    check("duplicate added once", patched.count("0x0489, 0xe156") == 1, patched)
continued = "obj-$(CONFIG_BT_HCIBTUSB)\t+= btusb.o\nbtusb-y := btusb_main.o \\\n\tbtusb_extra.o\n"
rc, out, build = run({"btusb_main.c": btusb_source(), "Makefile": continued}, [MT7902])
check("continued line: exit", rc == 0, f"rc={rc} {out}")
if rc == 0:
    kbuild = (build / "Kbuild").read_text().splitlines()
    check("continued line: joined", "btusb-y := btusb_main.o \tbtusb_extra.o" in kbuild, f"{kbuild}")

# 7. The dpkg lookup, on a merged /usr where dpkg knows only /usr/lib/modules.
rc, out, build = run(single, [MT7902], lookup=("9.9.0-1", "9.9.0-1"))
check("lookup: exit", rc == 0, f"rc={rc} {out}")
if rc == 0:
    check("lookup: patched", MT7902_LINE in (build / "btusb.c").read_text())
rc, out, _ = run(single, [MT7902], lookup=("9.9.0-1", "9.9.0-2"))
check("lookup mismatch: exit 77", rc == 77, f"rc={rc} {out}")
check("lookup mismatch: reason", "no installed linux-source" in out, out)

ROOT.cleanup()
if errors:
    print("\n".join(f"FAIL: {e}" for e in errors))
    sys.exit(1)
print("btusb-prepare.sh behaves as documented")
