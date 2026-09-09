#!/usr/bin/env python3
#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Whether this image can actually run these analyses.

Run at build time, so that an image which cannot is never published. The base
image's own `verify.py` checks the contract PartCAD talks to a container
through; this checks the two things this image adds on top of it, and checks
them the way the analyses will use them.

That last part is the point. Neither addition is installed where this
interpreter would look by default: the mesher is a Debian package built for
Debian's interpreter, and the solver lives in a conda-forge prefix of its own.
So "the package installed" proves nothing, and what has to be proved is that
*this* interpreter can import the mesher and *this* PATH can find the solver.
"""

import shutil
import subprocess
import sys


def main() -> int:
    failures = []

    # gmsh, imported the way `calculix_common._gmsh()` imports it. A ctypes
    # wrapper is only importable if the shared library it opens is there too,
    # so this covers `libgmsh4` without naming it.
    try:
        import gmsh

        gmsh.initialize()
        try:
            gmsh.model.add("verify")
            box = gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)
            gmsh.model.occ.synchronize()
            gmsh.model.mesh.generate(3)
            nodes, _, _ = gmsh.model.mesh.getNodes()
            if len(nodes) == 0:
                failures.append("gmsh meshed a box into no nodes at all")
            else:
                print("gmsh %s meshed a box into %d nodes" % (gmsh.option.getString("General.Version"), len(nodes)))
            del box
        finally:
            gmsh.finalize()
    except Exception as e:
        failures.append(
            "gmsh could not mesh anything from this interpreter (%s). The module is copied out of "
            "Debian's dist-packages and named on PYTHONPATH; either the copy or libgmsh is missing." % e
        )

    # ccx, found the way `calculix_common.find_ccx()` finds it.
    found = shutil.which("ccx")
    if not found:
        failures.append("the CalculiX solver is not on PATH as 'ccx'")
    else:
        try:
            # No input file: it prints its banner and exits non-zero, which is
            # all this needs -- the question is whether it runs at all.
            result = subprocess.run([found], capture_output=True, text=True, timeout=60)
            banner = (result.stdout + result.stderr).strip().splitlines()
            print("ccx at %s says: %s" % (found, banner[0] if banner else "(nothing)"))
        except Exception as e:
            failures.append("the CalculiX solver at %s could not be run: %s" % (found, e))

    if failures:
        print("\nThis image cannot run these analyses:")
        for failure in failures:
            print("  * %s" % failure)
        return 1

    print("\nThe solver and the mesher are both usable from this interpreter.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
