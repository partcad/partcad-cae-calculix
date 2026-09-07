# partcad-cae-calculix

The [CalculiX](https://www.calculix.de/) implementations of PartCAD's two engineering analyses: what
`pc cae fea` and `pc cae cfd` run by default.

This is a [PartCAD](https://partcad.org/) package, not a Python distribution. Nothing here is installed by
hand, and nothing here asks you to install a solver: PartCAD fetches the package as a dependency and runs both
analyses inside the container image they declare, which carries CalculiX and everything else they need. It is
reached from the public index as `//pub/feature/cae/calculix`, and the `caeFeaImplementation` /
`caeCfdImplementation` user configuration options point at `//pub/feature/cae/calculix:fea` and `:cfd` out of
the box. What it does need is a container runtime — see [What it needs](#what-it-needs).

## Status: `fea` works, `cfd` does not yet

Say this before anything else, because the two are not in the same state.

**`fea` is validated against a case with a known answer.** PartCAD's
[`examples/feature_cae`](https://github.com/partcad/partcad/tree/devel/examples/feature_cae) cantilever — 100 x
10 x 10 mm of steel, clamped at one end, 100 N at the other — comes back at **0.1655 mm**, inside the
0.16-0.18 mm the port-neighbourhood model predicts, and stable from 434 to 6468 elements. Peak von Mises
58.3 MPa against a bending figure of 60.0 MPa. Run with CalculiX 2.23 on macOS arm64 — a native solver, which
is what was to hand at the time; the image these analyses now declare carries Debian's build of `ccx` instead,
and nobody has re-run the cantilever through it to confirm the numbers land in the same place.

**`cfd` does not produce a usable answer, and it is now clear why.** Three things were wrong with it. Two are
fixed:

* **No outlet.** An incompressible flow is posed by *differences* in pressure, and `cfd:` could name only walls
  (`fix:`) and an inlet (`load:`). With nothing saying where the flow goes there is no downstream reference, and
  CalculiX answers with a field that never moves — peak speed of order 1e-16 m/s. `cfd:` now takes an `outlet:`,
  and a `cfd:` without one is refused with a sentence saying so rather than solved into a dead field.
* **No temperature anywhere.** An isothermal run still solves the energy equation, and nothing pinned it: the
  field drifted off its initial value and the run ended in `*ERROR in initialcfd: absolute temperature is
  nearly zero`, which reads as a mistake on the `*PHYSICAL CONSTANTS` card and is not one. The walls and the
  inlet are now held at the reference temperature.

The third is not, and it is the solver's:

```
 *ERROR in compdt; strongly decreasing time increment; the solution diverged
```

A deck built by hand for the same pipe — true end faces as inlet and outlet, the whole lateral surface as the
no-slip wall, 808 nodes rather than 178 — diverges identically, driven by pressure or by a prescribed inlet
velocity, at reference pressures from 1 Pa to 1e5 Pa. The first increment CalculiX computes for it is 8.1e-7 s,
an *acoustic* step for a flow moving at 0.02 m/s: `*CFD` demands `*SPECIFIC GAS CONSTANT` even under
`COMPRESSIBLE=NO` (leave it out and `initialcfd` refuses the deck), and the step derived from that speed of
sound collapses to 6.7e-9 s within one iteration.

That gas constant does control the increment, and it is not a way out. Lowering it buys more increments and
then breaks differently: at a tenth it ends in `con2phys: too many iterations` instead, at a hundredth it
marches far longer and still ends in `compdt`. A deck tuned that way would also be answering with
thermodynamics that were made up to move a number, which is not a result worth having.

So it is solver work, not tuning. **Do not read a number out of `cfd`.** It is shipped because the machinery
around it — the section, the command, the tab — is worth having in place, not because it answers.

One more thing is worth knowing even once a solver converges: a CFD boundary condition is a **surface**, and a
PartCAD port is a coordinate frame. The ball of nodes within `port_radius` of the pipe's inlet contains exactly
one node. Whatever fixes the solver will also have to find the *face* a port lies on.

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
the script, `container:` (or, for an implementation pip can provision, `pythonRequirements`) says where it
runs, `extension` says what it writes, and everything else is a parameter handed to the script:

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

`container:` is not repeated there and must not be: where the implementation runs is read from the package that
ships it, which `package:` names, so a re-tuned copy inherits the image along with the script.

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
bore as a part, not the casting. `fix:` names the walls (no slip), `load:` names where the flow is driven, and
`outlet:` names where it leaves:

```yaml
    cfd:
      fix:
        - pipe-wall     # no slip
      load:
        pipe-inlet: 5 mN
      outlet:
        - pipe-outlet   # held at the reference pressure
```

A force on a boundary divided by the area of that boundary is a pressure, and pressure is what an
incompressible solver is driven by. That is why `cfd:` takes a force rather than a velocity — it is the same
declaration the part already makes for FEA, in the same units, meaning the same physical thing. The inlet is
written as a *rise* above the reference and the outlet as the reference itself, because a difference is the
whole of what drives the flow: an absolute few hundred pascals under a field initialised at one atmosphere
says the fluid is being sucked backwards, and answers with a dead field.

`outlet:` is required. A part that names an inlet and no outlet is not asking a harder question, it is asking
one that has no answer.

## What it needs

**A container runtime, and nothing else.** Both analyses declare an image and run inside it:

    ghcr.io/partcad/partcad-container-calculix

It carries `ccx`, gmsh, numpy, trimesh and OpenCASCADE — the solver, the mesher, and the binding that rebuilds
PartCAD's geometry before the first line of these scripts runs. Nothing on the host is consulted, so there is
nothing to install, nothing to put on `PATH`, and no version of anything to match.

The image is built and published from
[`tools/containers/calculix/`](https://github.com/partcad/partcad/tree/devel/tools/containers/calculix) in the
`partcad` repository, for `linux/amd64` and `linux/arm64` alike, and it proves itself at build time: `verify.py`
there decodes a shape with OpenCASCADE, meshes it with gmsh and solves a deck with `ccx`, and a build where any
of that fails produces no image.

`container:` is declared on each of the two file types in `partcad.yaml`, pinned to a tag that is a hash of what
the image is built from rather than a version. The pin is immutable, so it keeps working when the image is next
edited, and the image and the code expecting it cannot drift apart silently.

### The cost, and what it rules out

An implementation that declares a `container:` has said a sandbox is not enough, and PartCAD takes it at its
word: there is no falling back to a machine that happens to have `ccx` installed natively. A machine with no
container runtime therefore cannot run these analyses at all.

That is the trade, and it is deliberate: it leaves exactly one thing that can stop them, and it is a thing
PartCAD can detect and say plainly. A missing container runtime is the one absence `pc test` passes over.
Everything else — a solver that will not converge, a mesh that will not build, an image that is not what it
claims — is this package failing, and is reported as a failure.

### Why not a Python sandbox

Because a Python sandbox cannot hold this pipeline, twice over:

* **`ccx` is a native executable.** pip has never heard of it, and PartCAD does not ship solvers.
* **gmsh publishes no wheel for 64-bit ARM Linux, and no source distribution** — four wheels per release
  (macOS x86_64, macOS arm64, manylinux x86_64, win_amd64), in every release from 4.12 through 4.15. On an ARM
  Linux runner or an ARM container there is nothing for pip to install and nothing to build from.

Debian builds both, for amd64 and arm64 alike, which is what the image is made of. A plugin that answered "not
on this machine" to a user who thought their machine was equipped would have failed; on 64-bit ARM Linux it
would have failed however carefully its `pythonRequirements` were written. So this package declares none —
there is nothing for PartCAD to install, and nothing that can fail to install.

### Running the scripts by hand

Outside PartCAD — a contributor debugging `calculix_common.py` at a prompt — there is no image, and what it
carries has to come from somewhere. `ccx` is looked up on `PATH` and then in the usual places, and
`PARTCAD_CCX` names it outright on a machine where it lives somewhere else:

```shell
apt install calculix-ccx                     # Debian, Ubuntu
brew install calculix-ccx                    # macOS
conda install -c conda-forge calculix        # anywhere conda is
```

That is a convenience for that case, not a second supported way to run the analyses. When one of them says the
solver or the mesher is missing, it says so as what it is: not the image, rather than not this machine.

## Tests

```shell
pytest test_calculix.py          # needs numpy and pyyaml; no solver, no gmsh
```

An end-to-end run needs the image, so this suite deliberately does not attempt one — which does *not* rule out
the parts most likely to be quietly wrong. What is covered is the `.frd` reader (fixed-column parsing,
including the case where two negative values fill their fields and touch, which is what rules out splitting the
line on whitespace), `von_mises` against its closed form, `surface_triangles` finding exactly the faces one
element owns, the port-to-node-set mapping, the deck writer's 16-per-line node sets, that a runtime without the
solver or the mesher says which one and what would explain it, and that both file types declare the image and
pin it to something immutable.

That suite has already earned itself: the `.frd` reader's column offsets were wrong on the first draft — every
field came back empty, which looks exactly like a solver that did not converge — and the offsets are now
written down in `calculix_common.py` beside the Fortran formats they come from.
