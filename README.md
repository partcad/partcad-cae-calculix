# partcad-cae-calculix

The [CalculiX](https://www.calculix.de/) implementations of PartCAD's two engineering analyses: what
`pc cae fea` and `pc cae cfd` run by default.

This is a [PartCAD](https://partcad.org/) package, not a Python distribution. Nothing here is installed by
hand: PartCAD fetches the package as a dependency and installs the Python requirements below into the sandbox
it runs the implementations in. It is reached from the public index as `//pub/feature/cae/calculix`, and the
`caeFeaImplementation` / `caeCfdImplementation` user configuration options point at
`//pub/feature/cae/calculix:fea` and `:cfd` out of the box.

## Status: `fea` works, `cfd` does not yet

Say this before anything else, because the two are not in the same state.

**`fea` is validated against a case with a known answer.** PartCAD's
[`examples/feature_cae`](https://github.com/partcad/partcad/tree/devel/examples/feature_cae) cantilever — 100 x
10 x 10 mm of steel, clamped at one end, 100 N at the other — comes back at **0.1655 mm**, inside the
0.16-0.18 mm the port-neighbourhood model predicts, and stable from 434 to 6468 elements. Peak von Mises
58.3 MPa against a bending figure of 60.0 MPa. Run with CalculiX 2.23 on macOS arm64.

**`cfd` does not produce a usable answer.** The deck is accepted by CalculiX now, and it still does not
converge: observed runs settle at a dead field, peak speed of order 1e-16 m/s, and end with

```
 *ERROR in compdt; strongly decreasing time increment; the solution diverged
```

At least one cause is structural rather than a typo: `cfd:` can name walls (`fix:`) and an inlet (`load:`) and
has no way to name an **outlet**, so an incompressible problem is posed with no downstream pressure reference.
Do not read a number out of `cfd` yet. It is shipped because the machinery around it — the section, the
command, the tab — is worth having in place, not because it answers.

## Using it

Declare the boundary conditions on the part, in a section named after the analysis. They belong to the part
rather than to whoever analyses it — a bracket is bolted down at the same holes whichever solver is asked:

```yaml
parts:
  bracket:
    type: build123d
    path: bracket.py
    implements:
      m3-screw: {left: ..., right: ...}
      hook:
    fea:
      fix:
        - m3-screw          # every instance of this interface is held still
      load:
        hook: 5 kg          # every instance of this one carries this
```

Then:

```shell
pc cae fea :bracket                 # the model, and the findings
pc cae fea --json :bracket          # the findings as the JSON array they are
pc test -f fea :bracket             # the same analysis, as a check
```

`load` values are forces. Write a number and a unit — `n`, `nm`, `mn`, `kn` or `newton` for force, `mg`, `g`,
`kg`, `ton`, `tonne`, `lb` or `pound` for mass, matched case-insensitively, with or without a space and with
or without a plural `s` — or a bare number, which is a mass in kilograms. PartCAD weighs a mass into a force
before this package sees it, so everything here is newtons.

The result model is written to `<part>.<analysis>.glb` and shown in the PartCAD Viewer's FEA and CFD tabs,
turned and zoomed like any other 3D object. The findings are listed under it.

## What it is

Two file types in a `cae:` section, which is the same shape as an `export:` or a `render:` one — `path` names
the script, `pythonRequirements` describes its sandbox, `extension` says what it writes, and everything else
is a parameter handed to the script:

| File type | What it does | Writes |
| --- | --- | --- |
| `fea` | A linear static stress analysis of the part | a glTF coloured by von Mises stress |
| `cfd` | Incompressible flow through the part, read as the fluid volume | a glTF coloured by speed |

Both answer with **findings**: the JSON array of what the analysis has to say about the part. An empty one is
a pass, which is what `pc test`'s `fea` and `cfd` checks require.

Every parameter is documented in `partcad.yaml` beside its default — the mesh density, the material, the
thresholds that decide what counts as a finding. A package re-tunes any of them in a `cae:` section of its
own, and one object overrides them again in its own:

```yaml
cae:
  fea:
    # The implementation stays this package's; only the parameters change.
    package: //pub/feature/cae/calculix
    path: fea_calculix.py
    youngs_modulus: 6.9e+10        # aluminium
    yield_strength: 2.4e+8
    mesh_size: 0.02
```

### The pipeline

    the part  ->  gmsh  ->  a tetrahedral mesh
    a port    ->  the nodes within `port_radius` of it  ->  a CalculiX node set
    a deck    ->  ccx  ->  a .frd
    a field   ->  a colour per node  ->  a binary glTF

`calculix_common.py` is all of that except the deck and the field, which are the only two things the two
analyses actually differ in.

### A port is a neighbourhood, not a face

This is the one modelling decision worth arguing with. A PartCAD port is a coordinate frame: it says where a
bolt goes, not which surface it clamps. So a fixed port becomes the mesh nodes within `port_radius` (a
fraction of the part's largest dimension) of where the port is, and a loaded port spreads its force over the
same neighbourhood. Get that radius wrong and the answer is wrong in a way that looks plausible — too small
and the load is a point load with an artificial stress concentration under it, too large and a bolt hole
clamps half the bracket. It is a parameter of the file type for exactly that reason, and a port that reaches
no material at all is reported as a finding rather than passed over.

`pc render --with-ports` draws the ports on a projection of the part, which is the quickest way to see what
the solver was actually told.

### What `cfd:` reads the part as

The **fluid volume** — the space the fluid is in, not the wall around it. A duct is analysed by declaring the
bore as a part, not the casting. `fix:` names the walls (no slip) and `load:` names where the flow is driven:
a force on a boundary divided by the area of that boundary is a pressure, and pressure is what an
incompressible solver is driven by. That is why `cfd:` takes a force rather than a velocity — it is the same
declaration the part already makes for FEA, in the same units, meaning the same physical thing.

## What it needs

`gmsh`, `numpy` and `trimesh` are `pythonRequirements` and PartCAD installs them into the sandbox itself.

**`ccx` is not one of them.** It is a native executable, pip cannot install it, and PartCAD does not ship
solvers. Install it the way the platform does:

```shell
apt install calculix-ccx                     # Debian, Ubuntu
brew install calculix-ccx                    # macOS
conda install -c conda-forge calculix        # anywhere conda is
```

It is looked up on `PATH` and then in the usual places; `PARTCAD_CCX` names it outright on a machine where it
lives somewhere else. A machine with no solver is told so as a sentence saying what to install — that is a
finding about the machine, not about the part.

### Platforms: not 64-bit ARM Linux

`gmsh` publishes four wheels per release — macOS x86_64, macOS arm64, manylinux x86_64 and win_amd64 — and
**no linux aarch64 wheel and no source distribution**, in every release from 4.12 through 4.15. So on 64-bit
ARM Linux there is nothing for pip to install and nothing to build from, and no version of this package can
change that.

`pythonRequirements` therefore carries `; platform_machine != "aarch64"` on `gmsh`, which is not a preference
but the difference between two failures. Asked for it anyway, pip exits non-zero, PartCAD logs that as an
error, and a logged error makes `pc` exit non-zero even where the caller recovered — so `pc test` reports the
failure of a package that is perfectly well formed. Told not to try, pip skips it and exits clean, the sandbox
builds, and the analysis reports "not on this machine" through the same path a missing `ccx` uses: a warning,
and the check passes over the part.

Apple silicon is unaffected — macOS reports `arm64`, and gmsh publishes that wheel. This is 64-bit ARM
**Linux** alone, which in practice means an ARM CI runner or an ARM container.

## Tests

```shell
pytest test_calculix.py          # needs numpy and pyyaml; no solver, no gmsh
```

`ccx` is a native executable and `gmsh` is a large wheel, so a contributor may have neither — which rules out
an end-to-end run and does *not* rule out the parts most likely to be quietly wrong. What is covered is the
`.frd` reader (fixed-column parsing, including the case where two negative values fill their fields and touch,
which is what rules out splitting the line on whitespace), `von_mises` against its closed form,
`surface_triangles` finding exactly the faces one element owns, the port-to-node-set mapping, the deck
writer's 16-per-line node sets, and that a machine with no solver is told what to install.

That suite has already earned itself: the `.frd` reader's column offsets were wrong on the first draft — every
field came back empty, which looks exactly like a solver that did not converge — and the offsets are now
written down in `calculix_common.py` beside the Fortran formats they come from.

## Status

**Not yet validated end to end against a real solver.** The pipeline is written against CalculiX 2.20+ and
gmsh 4.x, and it has not been run: the machine it was written on has neither. What the tests above cover is
sound; what they cannot reach is whether `ccx` accepts the decks, whether the boundary conditions land where a
person would put them, and whether the numbers are right. Treat those with suspicion until somebody has
checked one against a case with a known answer — a cantilever beam under a tip load is the usual one, and its
closed form is in every strength-of-materials text.

Corrections welcome, and a checked case most of all.

## Licence

Apache License 2.0. See [LICENSE.txt](./LICENSE.txt).
