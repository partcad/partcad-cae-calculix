#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""`pc cae cfd`, by CalculiX: incompressible flow through one part.

The part is read as the **fluid volume** -- the space the fluid is in, not the
wall around it. That is the one thing worth understanding before reading a
result out of this: a duct is analysed by declaring the bore as a part, not the
casting.

The boundary conditions are the same two keys `fea:` takes, and they mean the
corresponding things:

* `fix:` names the **walls**: no slip, the fluid is held still against them.
* `load:` names where the flow is **driven**, as a force. A force on a boundary
  divided by the area of that boundary is a pressure, and pressure is what an
  incompressible solver is driven by -- so a `load:` of 30 N on an inlet whose
  neighbourhood covers 300 mm2 is a 100 kPa inlet. This is why `cfd:` takes a
  force and not a velocity: it is the same declaration the part already makes
  for FEA, in the same units, meaning the same physical thing.

CalculiX solves incompressible flow transiently and is marched to a steady state
rather than asked for one, which is what `duration` and `time_step` are.
"""

import math
import os
import sys
import tempfile

# See the note beside the same two lines in `fea_calculix.py`:
# `runpy.run_path()` leaves the script's directory off `sys.path`, so the
# sibling import below cannot resolve without it.
sys.path.append(os.path.dirname(__file__))
import calculix_common as ccx  # noqa: E402

ERROR = "error"
WARNING = "warning"
INFO = "info"


def process(path, request):
    try:
        return _analyse(path, request)
    except (ccx.SolverMissing, ccx.SolverFailed) as e:
        return ccx.failed(e)


def _analyse(path, request):
    import numpy as np

    findings = []
    shape = request.get("wrapped")
    if shape is None:
        raise ccx.SolverFailed("No shape was handed to the analysis")

    # First order and nothing else: CalculiX's CFD solver takes linear elements.
    mesh = ccx.mesh_shape(shape, request.get("mesh_size", 0.05), 1)
    mesh.element_type = "F3D4"

    boundary = request.get("boundary") or []
    walls = [record for record in boundary if record.get("fix")]
    driven = [record for record in boundary if record.get("load")]
    open_ends = [record for record in boundary if record.get("outlet")]

    if not walls:
        raise ccx.SolverFailed(
            "'cfd:' names no wall, so the fluid has nothing to flow along: name the wall interfaces under 'fix:'"
        )
    if not driven:
        raise ccx.SolverFailed(
            "'cfd:' names nothing to drive the flow: name an inlet under 'load:' with the force behind it"
        )
    if not open_ends:
        # An incompressible flow is posed by differences in pressure. Driven at
        # one end and closed everywhere else, the problem has no downstream
        # reference: what is pushed in has nowhere to go, and CalculiX answers
        # with a field that never moves or with `compdt: the solution diverged`
        # -- neither of which says what is actually wrong.
        raise ccx.SolverFailed(
            "'cfd:' names no outlet, so the flow has nowhere to go and the problem has no "
            "downstream pressure reference: name where it leaves under 'outlet:'"
        )

    radius = request.get("port_radius", 0.05)
    wall_sets, wall_empty = ccx.port_node_sets(mesh, walls, radius, prefix="WALL")
    driven_sets, driven_empty = ccx.port_node_sets(mesh, driven, radius, prefix="IN")
    outlet_sets, outlet_empty = ccx.port_node_sets(mesh, open_ends, radius, prefix="OUT")
    for record in wall_empty + driven_empty + outlet_empty:
        findings.append(
            {
                "severity": WARNING,
                "message": (
                    "The port is not near any material, so the condition on it did nothing. "
                    "Check where the port is placed, or raise 'port_radius'."
                ),
                "where": record.get("port") or record.get("interface_label") or "a port",
            }
        )
    if not wall_sets or not driven_sets or not outlet_sets:
        raise ccx.SolverFailed(
            "None of the walls, none of the driven ports or none of the outlets is near any material. "
            "Check where 'implements:' places them, or raise 'port_radius'."
        )

    with tempfile.TemporaryDirectory() as work:
        deck = _deck(mesh, wall_sets, driven_sets, outlet_sets, request, radius)
        results = ccx.run_ccx(deck, work)
        # A CFD step is not named like a structural one: CalculiX writes `V3DF`
        # for velocity and `PS3DF` for static pressure, where a static step
        # writes `DISP` and `STRESS`. Asked for the structural names, `read_frd`
        # matched nothing at all and every converged run still looked empty.
        fields = ccx.read_frd(results, {"V3DF": 3, "PS3DF": 1, "SA3DF": 1})

        velocities = {
            node: math.sqrt(values[0] ** 2 + values[1] ** 2 + values[2] ** 2) for node, values in fields["V3DF"].items()
        }
        if not velocities:
            raise ccx.SolverFailed(
                "CalculiX produced no velocities: the flow did not converge. "
                "A smaller 'time_step' or a longer 'duration' is the usual remedy."
            )
        # Static pressure is `PS3DF`; `SA3DF` is asked for as well because some
        # builds write the same field under it, and an empty dictionary costs
        # nothing.
        pressures = fields["PS3DF"] or fields["SA3DF"]

        speed_field = ccx.field_per_node(mesh, velocities)
        ccx.write_glb(path, mesh.coordinates, mesh.surface_triangles(), speed_field, low=0.0)

    peak_speed = float(np.max(speed_field))
    values = [entry[0] for entry in pressures.values()] if pressures else []
    pressure_drop = (max(values) - min(values)) if values else 0.0

    findings.extend(_verdict(peak_speed, pressure_drop, request))
    return {
        "success": True,
        "findings": findings,
        "warnings": [
            "Peak speed %.3g m/s, pressure drop %.3g Pa, over %d elements"
            % (peak_speed, pressure_drop, len(mesh.elements))
        ],
    }


def _verdict(peak_speed, pressure_drop, request):
    findings = []

    limit = request.get("max_velocity")
    if limit is not None and peak_speed > float(limit):
        findings.append(
            {
                "severity": WARNING,
                "message": (
                    "The flow reaches %.3g m/s, past the %.3g m/s this geometry is meant to hold."
                    % (peak_speed, float(limit))
                ),
                "where": "peak velocity",
            }
        )

    limit = request.get("max_pressure_drop")
    if limit is not None and pressure_drop > float(limit):
        findings.append(
            {
                "severity": WARNING,
                "message": (
                    "The pressure drop across the part is %.3g Pa, past the %.3g Pa allowed."
                    % (pressure_drop, float(limit))
                ),
                "where": "pressure drop",
            }
        )
    return findings


def _increment_cap(request):
    """How many fluid increments the requested run can possibly need.

    `duration / time_step`, doubled so that a solver taking smaller steps than
    it was asked for still finishes, and floored so that a nonsensical request
    still produces a deck. It is a cap and not a target: a steady-state run
    stops when it converges.
    """
    try:
        step = float(request.get("time_step", 0.01))
        duration = float(request.get("duration", 1.0))
    except (TypeError, ValueError):
        return 10000
    if step <= 0 or duration <= 0:
        return 10000
    return max(100, min(1000000, int(2 * duration / step)))


def _deck(mesh, wall_sets, driven_sets, outlet_sets, request, radius_fraction):
    """The CalculiX CFD deck: the mesh, the fluid, the boundaries, the step."""
    lines = ccx.deck_mesh(mesh)
    for name, nodes, _record in wall_sets + driven_sets + outlet_sets:
        lines.extend(ccx.deck_nset(name, nodes))

    # Every card below except the first two is here because CalculiX asked for
    # it by name and refused the deck without it. An isothermal incompressible
    # run uses almost none of them, which is why the first draft left them out
    # and why `*CFD` was rejected before the solve began:
    #
    #   *ERROR reading *CFD: please define initial conditions for the temperature
    #   *ERROR in initialcfd: specific gas constant for material FLUID is close
    #          to zero; maybe it has not been defined
    #   *ERROR in inicialcfd: initial pressure must be strictly positive
    #   *ERROR in materialdata_cond: fluid conductivity is lacking
    #
    # `*FLUID CONSTANTS` does not cover conductivity, whatever the comment that
    # used to sit here said: its three numbers are specific heat, dynamic
    # viscosity and temperature. Conductivity has a card of its own.
    temperature = 293.0
    pressure_reference = 1.0e5
    lines.extend(
        [
            "*MATERIAL, NAME=FLUID",
            "*FLUID CONSTANTS",
            # Specific heat, dynamic viscosity, temperature.
            "1005., %.9g, %.9g" % (float(request.get("viscosity", 1.82e-5)), temperature),
            "*DENSITY",
            "%.9g" % float(request.get("density", 1.204)),
            "*CONDUCTIVITY",
            "0.0257",
            "*SPECIFIC GAS CONSTANT",
            "287.",
            "*SOLID SECTION, ELSET=EALL, MATERIAL=FLUID",
            "*PHYSICAL CONSTANTS, ABSOLUTE ZERO=0.",
            "*INITIAL CONDITIONS, TYPE=FLUID VELOCITY",
            "NALL, 1, 0.",
            "NALL, 2, 0.",
            "NALL, 3, 0.",
            "*INITIAL CONDITIONS, TYPE=TEMPERATURE",
            "NALL, %.9g" % temperature,
            # Strictly positive, or `inicialcfd` refuses it. It is a reference
            # level for an incompressible run, not a physical claim.
            "*INITIAL CONDITIONS, TYPE=PRESSURE",
            "NALL, %.9g" % pressure_reference,
            # Bounded, and bounded by the run that was asked for. `INCF` caps the
            # fluid increments, and an unqualified `*NODE FILE` writes a result
            # block per increment -- so the million that used to be here is a
            # million blocks, and one observed run reached 5 GB of `.frd` in
            # minutes and was still growing. The number of steps the requested
            # duration and time step imply, with room to spare, is the honest cap.
            "*STEP, INCF=%d" % _increment_cap(request),
            "*CFD, STEADY STATE, COMPRESSIBLE=NO",
            "%.9g, %.9g" % (float(request.get("time_step", 0.01)), float(request.get("duration", 1.0))),
        ]
    )

    # No slip: the fluid is held still at every wall node.
    for name, _nodes, _record in wall_sets:
        lines.append("*BOUNDARY")
        lines.append("%s, 1, 3, 0." % name)

    # And held at the reference temperature, here and at the inlet. An
    # isothermal run still solves the energy equation -- CalculiX's `*CFD` has
    # no way to be told not to -- and an equation with no Dirichlet condition
    # anywhere is unconstrained: the field drifts off its initial value over a
    # few increments and the run ends in
    #
    #   *ERROR in initialcfd: absolute temperature is nearly zero; maybe
    #          absolute zero was wrongly defined or not defined at all
    #
    # which reads as a mistake on the `*PHYSICAL CONSTANTS` card and is not one.
    # Degree of freedom 11 is the temperature of a CFD node.
    for name, _nodes, _record in wall_sets + driven_sets:
        lines.append("*BOUNDARY")
        lines.append("%s, 11, 11, %.9g" % (name, temperature))

    # The driven boundaries, as a static pressure. The area a port's
    # neighbourhood stands for is the disc of the search radius, which is the
    # only area a coordinate frame implies; a port that means something else
    # should say so with its own `port_radius`.
    #
    # Both ends are written against `pressure_reference`, and that is the whole
    # of what an incompressible run is driven by: a *difference*. Written as an
    # absolute pressure instead -- which is what this did -- an inlet of a few
    # hundred pascals sits under a field initialised at one atmosphere, so the
    # deck says the fluid is being sucked backwards out of the inlet as hard as
    # the atmosphere can push, and the answer is a dead field.
    # One node, one pressure. Written into the deck twice it carries two
    # degree-8 constraints in one step, and CalculiX's rule for that is to keep
    # the later one -- so the model that gets solved would be decided by the
    # order the ports happen to be listed in, which is not something the model
    # says. Two ports that overlap is a model somebody has to fix, so say which
    # ones rather than solving an arbitrary one of the readings.
    #
    # Every pair that could write a *different* pressure, which is two of the
    # three kinds of pair: a driven port against another driven port carries a
    # rise each, and a driven port against an outlet carries a rise and the
    # reference. Two outlets are not a pair -- both write the reference, so the
    # deck says the same thing whichever one CalculiX keeps.
    pressure_sets = [(name, nodes) for name, nodes, _record in driven_sets]
    outlet_pairs = [(name, nodes) for name, nodes, _record in outlet_sets]
    for index, (name, nodes) in enumerate(pressure_sets):
        for other_name, other_nodes in pressure_sets[index + 1 :] + outlet_pairs:
            shared = set(nodes) & set(other_nodes)
            if shared:
                raise ccx.SolverFailed(
                    "ports '%s' and '%s' select %d of the same mesh nodes, so the deck would constrain "
                    "those nodes to two pressures at once. Move the ports apart, or narrow them with a "
                    "smaller radius." % (name, other_name, len(shared))
                )

    area = math.pi * (mesh.extent * float(radius_fraction) / ccx.MM_PER_M) ** 2
    for name, _nodes, record in driven_sets:
        rise = float(record["load"]) / max(area, 1e-12)
        lines.append("*BOUNDARY")
        # Degree of freedom 8 is the static pressure of a CFD node.
        lines.append("%s, 8, 8, %.9g" % (name, pressure_reference + rise))

    # Where it leaves: the reference itself, which is what makes the inlet's
    # rise a rise rather than a number.
    for name, _nodes, _record in outlet_sets:
        lines.append("*BOUNDARY")
        lines.append("%s, 8, 8, %.9g" % (name, pressure_reference))

    lines.extend(
        [
            "*NODE FILE",
            "VF, PSF",
            "*END STEP",
        ]
    )
    return lines


if __name__ == "__partcad_export__":
    pass
