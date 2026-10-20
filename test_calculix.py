#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""What can be checked about this package without a solver in the room.

`ccx` is a native executable and `gmsh` is a large wheel, so a contributor and a
CI runner alike may have neither. That rules out an end-to-end run, and it does
*not* rule out the parts most likely to be quietly wrong:

* the `.frd` reader, which is fixed-column ASCII parsing and the one thing here
  that a wrong column offset breaks silently rather than loudly;
* `von_mises`, which is arithmetic with a known closed form;
* `surface_triangles`, which has to find exactly the faces one element owns;
* `nodes_near`, which is what turns a port into a boundary condition;
* the deck writer's node-set wrapping, which CalculiX caps at 16 per line;
* and that a machine with no solver is told what to install rather than shown a
  traceback.

Run it with `pytest test_calculix.py`. It needs `numpy` and nothing else.
"""

import re
import os
import sys

import numpy as np
import pytest

import calculix_common as ccx

# --------------------------------------------------------------------------- #
# Finding the solver                                                          #
# --------------------------------------------------------------------------- #


def test_a_machine_with_no_solver_is_told_what_to_install(monkeypatch):
    monkeypatch.delenv(ccx.CCX_ENV, raising=False)
    monkeypatch.setattr(ccx.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ccx, "CCX_DIRECTORIES", ())

    with pytest.raises(ccx.SolverMissing) as raised:
        ccx.find_ccx()
    message = str(raised.value)
    # Both ways to have it, because a reader with a container runtime and a
    # reader without one need different answers and the message cannot tell
    # which it is talking to. Then the three ways to install a native solver,
    # and the way out for a machine that keeps one somewhere else. A message
    # that only says "not found" sends the reader to a search engine; one that
    # names a single remedy sends half of them to the wrong one.
    assert "dockerImage:" in message
    assert "calculix-ccx" in message
    assert "conda" in message
    assert ccx.CCX_ENV in message


@pytest.mark.parametrize(
    "system, machine",
    [("Linux", "aarch64"), ("Linux", "x86_64"), ("Darwin", "arm64"), ("Windows", "AMD64")],
)
def test_a_runtime_without_gmsh_names_both_ways_to_get_one(monkeypatch, system, machine):
    """A missing mesher has two remedies, and the message cannot tell which applies.

    64-bit ARM Linux is the case that shapes it: gmsh publishes no wheel there
    and no source distribution, so pip cannot supply it however the requirements
    are written, and the image or a distribution package is the answer.
    Everywhere else pip can, which is why the message names installing it too
    rather than telling every reader to start Docker.
    """
    monkeypatch.setattr(ccx.platform, "machine", lambda: machine)
    monkeypatch.setattr(ccx.platform, "system", lambda: system)
    monkeypatch.setitem(sys.modules, "gmsh", None)  # 'import gmsh' -> ImportError

    with pytest.raises(ccx.SolverMissing) as raised:
        ccx._gmsh()
    message = str(raised.value)
    # What is missing, where, and both ways out.
    assert "gmsh" in message
    assert machine in message and system in message
    assert "dockerImage:" in message
    assert "install" in message


def test_the_environment_variable_wins(monkeypatch, tmp_path):
    solver = tmp_path / "my-ccx"
    solver.write_text("#!/bin/sh\n")
    solver.chmod(0o755)
    monkeypatch.setenv(ccx.CCX_ENV, str(solver))
    # Even with something on PATH: a machine that names one has said which.
    monkeypatch.setattr(ccx.shutil, "which", lambda _name: "/usr/bin/ccx")
    assert ccx.find_ccx() == str(solver)


def test_an_environment_variable_pointing_at_nothing_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv(ccx.CCX_ENV, str(tmp_path / "absent"))
    with pytest.raises(ccx.SolverMissing, match="not an executable"):
        ccx.find_ccx()


# --------------------------------------------------------------------------- #
# The mesh                                                                    #
# --------------------------------------------------------------------------- #


def _one_tetrahedron():
    """A single first-order tetrahedron, which every face of is a surface face."""
    return ccx.Mesh(
        node_ids=np.array([1, 2, 3, 4], dtype=np.int64),
        coordinates=np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 10.0, 0.0], [0.0, 0.0, 10.0]]),
        elements=[(1, [1, 2, 3, 4])],
        element_type="C3D4",
        extent=10.0,
    )


def _two_tetrahedra():
    """Two tetrahedra sharing one face, so that face is interior."""
    return ccx.Mesh(
        node_ids=np.array([1, 2, 3, 4, 5], dtype=np.int64),
        coordinates=np.array(
            [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 10.0, 0.0], [0.0, 0.0, 10.0], [0.0, 0.0, -10.0]]
        ),
        elements=[(1, [1, 2, 3, 4]), (2, [1, 2, 3, 5])],
        element_type="C3D4",
        extent=20.0,
    )


def test_every_face_of_one_element_is_a_surface_face():
    assert len(_one_tetrahedron().surface_triangles()) == 4


def test_a_shared_face_is_interior_and_is_not_drawn():
    """Two tetrahedra have eight faces and six of them are on the outside.

    This is the whole of what `surface_triangles` is for: a volume mesh has no
    surface of its own, and drawing the interior would put a sheet through the
    middle of the result.
    """
    triangles = _two_tetrahedra().surface_triangles()
    assert len(triangles) == 6


def test_surface_triangles_index_the_node_array():
    """The triangles index `coordinates`, not CalculiX's node numbers.

    They have to, because the field they are coloured by is per node in that
    same order. Node ids here start at 1, so an off-by-one would show up as an
    index of 5 in a four-node mesh.
    """
    mesh = _one_tetrahedron()
    triangles = mesh.surface_triangles()
    assert triangles.min() >= 0
    assert triangles.max() < len(mesh.node_ids)


def test_a_second_order_element_is_read_by_its_corners():
    """A C3D10's mid-side nodes are not corners of any face.

    Its first four nodes are the corners; the six after them sit on the edges.
    A face built from all ten would be a face of nothing.
    """
    mesh = ccx.Mesh(
        node_ids=np.arange(1, 11, dtype=np.int64),
        coordinates=np.zeros((10, 3)),
        elements=[(1, list(range(1, 11)))],
        element_type="C3D10",
        extent=1.0,
    )
    triangles = mesh.surface_triangles()
    assert len(triangles) == 4
    # Only the corner nodes, which are indices 0..3 of the node array.
    assert set(triangles.flatten()) == {0, 1, 2, 3}


def test_nodes_near_selects_by_distance():
    """The port-to-node-set mapping, which is how a `fix:` becomes a boundary."""
    mesh = _one_tetrahedron()
    assert mesh.nodes_near([0.0, 0.0, 0.0], 1.0) == [1]
    # A radius that reaches the three nodes 10 mm away as well as the origin.
    assert sorted(mesh.nodes_near([0.0, 0.0, 0.0], 10.0)) == [1, 2, 3, 4]
    # Exactly on the radius counts: the comparison is inclusive.
    assert 2 in mesh.nodes_near([0.0, 0.0, 0.0], 10.0)


def test_a_port_clear_of_the_material_selects_nothing():
    """Which is what `port_node_sets` reports as an empty record."""
    assert _one_tetrahedron().nodes_near([500.0, 500.0, 500.0], 5.0) == []


def test_port_node_sets_separates_what_matched_from_what_did_not():
    mesh = _one_tetrahedron()
    boundary = [
        {"port": "near", "location": [[0.0, 0.0, 0.0], [0.0, 0.0, 1.0], 0.0], "fix": True},
        {"port": "far", "location": [[900.0, 0.0, 0.0], [0.0, 0.0, 1.0], 0.0], "fix": True},
    ]
    # radius_fraction 0.2 of a 10 mm extent is 2 mm: reaches the origin only.
    sets, empty = ccx.port_node_sets(mesh, boundary, 0.2)
    assert [name for name, _nodes, _record in sets] == ["PORT0"]
    assert [record["port"] for record in empty] == ["far"]


def test_two_calls_can_be_told_apart_by_their_prefix():
    """The bug this exists for answered `0.0000 mm` rather than failing.

    Every caller calls `port_node_sets` twice, once for what is held and once for
    what is loaded, and each call numbers from zero. Under one shared prefix both
    first sets are called `PORT0`; CalculiX merges them silently, so the encastre
    holds the loaded nodes too and the load pushes on nodes that cannot move. The
    deck is valid, `ccx` is happy, and the deflection comes back as zero -- which
    is the one wrong answer a reader might accept.
    """
    mesh = _one_tetrahedron()
    here = [{"port": "root", "location": [[0.0, 0.0, 0.0], [0.0, 0.0, 1.0], 0.0]}]

    held, _ = ccx.port_node_sets(mesh, here, 0.2, prefix="FIX")
    loaded, _ = ccx.port_node_sets(mesh, here, 0.2, prefix="LOAD")

    assert [name for name, _n, _r in held] == ["FIX0"]
    assert [name for name, _n, _r in loaded] == ["LOAD0"]
    # The point of the whole thing: no name is claimed by both.
    assert not {name for name, _n, _r in held} & {name for name, _n, _r in loaded}


class _FakeGmsh:
    """Just enough of the gmsh module for `_tetrahedra` to read one element."""

    def __init__(self, element_type, connectivity):
        outer = self

        class _Mesh:
            def getElements(self, dim):
                assert dim == 3
                return ([element_type], [[1]], [list(connectivity)])

        class _Model:
            mesh = _Mesh()

        self.model = _Model()
        del outer


def test_a_first_order_tetrahedron_is_passed_through_as_gmsh_wrote_it():
    """C3D4 and gmsh's TET4 agree on all four corners, so nothing is reordered."""
    keyword, elements = ccx._tetrahedra(_FakeGmsh(4, [11, 12, 13, 14]), 1)
    assert keyword == "C3D4"
    assert elements == [(1, [11, 12, 13, 14])]


def test_a_second_order_tetrahedron_is_reordered_for_calculix():
    """The bug this exists for rejected every element in every mesh.

    gmsh's TET10 and CalculiX's C3D10 agree on the four corners and on the first
    four mid-side nodes, and swap the last two: gmsh's 9th sits on edge 3-4 where
    CalculiX wants edge 2-4. Handed over unswapped, `ccx` answers `*ERROR in
    e_c3d: nonpositive jacobian determinant` for every element, the step never
    runs, and the `.frd` holds a mesh and no fields -- indistinguishable, from
    the outside, from an analysis that did not converge. `mesh_order: 2` is the
    default, so this was all of FEA.
    """
    gmsh_order = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    keyword, elements = ccx._tetrahedra(_FakeGmsh(11, gmsh_order), 2)
    assert keyword == "C3D10"
    (_id, connectivity) = elements[0]
    assert connectivity == [1, 2, 3, 4, 5, 6, 7, 8, 10, 9]
    # The corners and the first four mid-side nodes are untouched; only the last
    # two move, and they move by swapping rather than by rotating.
    assert connectivity[:8] == gmsh_order[:8]
    assert sorted(connectivity) == gmsh_order


def test_an_element_of_another_type_is_not_collected():
    """A mesh can hold more than tetrahedra; only the asked-for type is read."""
    _keyword, elements = ccx._tetrahedra(_FakeGmsh(4, [1, 2, 3, 4]), 2)
    assert elements == []


def test_the_fluid_increment_cap_follows_the_run_that_was_asked_for():
    """`INCF` used to be a literal million, and `*NODE FILE` writes one block per
    increment: an observed run reached 5 GB of `.frd` in minutes and was still
    growing. The cap is now what the requested duration and time step imply.
    """
    import cfd_calculix

    cap = cfd_calculix._increment_cap
    # Twice the steps the request implies, so a solver taking smaller steps than
    # it was asked for still gets to the end.
    assert cap({"duration": 2.0, "time_step": 0.005}) == 800
    assert cap({}) == 200


@pytest.mark.parametrize(
    "request_",
    [
        {"duration": 1.0, "time_step": 0},
        {"duration": 0, "time_step": 0.01},
        {"duration": "soon", "time_step": "a bit"},
    ],
)
def test_a_nonsensical_run_still_produces_a_deck(request_):
    """A bad number is a finding for the solver to report, not a divide by zero
    in the code that writes the deck."""
    import cfd_calculix

    assert cfd_calculix._increment_cap(request_) == 10000


def test_the_cap_is_bounded_at_both_ends():
    import cfd_calculix

    cap = cfd_calculix._increment_cap
    assert cap({"duration": 0.001, "time_step": 0.01}) == 100
    assert cap({"duration": 1e9, "time_step": 1e-6}) == 1000000


# --------------------------------------------------------------------------- #
# The deck                                                                    #
# --------------------------------------------------------------------------- #


def test_the_deck_is_written_in_metres():
    """CalculiX has no units, so a deck is only consistent if it agrees with the
    material constants -- which are SI. PartCAD's geometry is in millimetres."""
    lines = ccx.deck_mesh(_one_tetrahedron())
    assert lines[0] == "*NODE, NSET=NALL"
    # The node at x = 10 mm is written as 0.01 m.
    assert "0.01" in lines[2]
    assert "*ELEMENT, TYPE=C3D4, ELSET=EALL" in lines


def test_a_port_cannot_be_both_an_inlet_and_an_outlet():
    """The same node written twice carries two degree-8 pressures in one step.

    Which one CalculiX keeps is not something the deck says, so the deck is not
    written at all: two ports selecting the same nodes is a model to fix, and
    the message names which two.
    """
    import cfd_calculix

    mesh = _one_tetrahedron()
    driven = [("INLET", [1, 2, 3], {"load": 1.0})]
    outlet = [("OUTLET", [3, 4], {})]

    with pytest.raises(ccx.SolverFailed, match="INLET.*OUTLET"):
        cfd_calculix._deck(mesh, [], driven, outlet, {}, 0.2)


def test_two_driven_ports_cannot_share_nodes_either():
    """Two inlets overlapping is the same fault as an inlet over an outlet.

    Both write a degree-8 pressure of their own, so a shared node carries two
    of them and CalculiX keeps whichever came last -- which makes the answer a
    function of the order the ports were listed in.
    """
    import cfd_calculix

    mesh = _one_tetrahedron()
    driven = [("INLET_A", [1, 2], {"load": 1.0}), ("INLET_B", [2, 3], {"load": 5.0})]
    outlet = [("OUTLET", [4], {})]

    with pytest.raises(ccx.SolverFailed, match="INLET_A.*INLET_B"):
        cfd_calculix._deck(mesh, [], driven, outlet, {}, 0.2)


def test_two_outlets_may_share_nodes():
    """Because both of them write the reference pressure, and nothing else.

    The check is about a node the deck constrains twice to two *different*
    values; two outlets constrain it twice to the same one, which the deck says
    unambiguously however CalculiX resolves it.
    """
    import cfd_calculix

    mesh = _one_tetrahedron()
    driven = [("INLET", [1], {"load": 1.0})]
    outlet = [("OUT_A", [2, 3], {}), ("OUT_B", [3, 4], {})]

    lines = cfd_calculix._deck(mesh, [], driven, outlet, {}, 0.2)
    assert any(line.startswith("OUT_A, 8, 8,") for line in lines)
    assert any(line.startswith("OUT_B, 8, 8,") for line in lines)


def test_ports_that_share_no_nodes_still_write_a_deck():
    """The check is about overlap, not about having two ports."""
    import cfd_calculix

    mesh = _one_tetrahedron()
    driven = [("INLET", [1, 2], {"load": 1.0})]
    outlet = [("OUTLET", [3, 4], {})]

    lines = cfd_calculix._deck(mesh, [], driven, outlet, {}, 0.2)
    assert any(line.startswith("INLET, 8, 8,") for line in lines)
    assert any(line.startswith("OUTLET, 8, 8,") for line in lines)


def test_a_node_set_wraps_at_sixteen_entries():
    """CalculiX's own limit. A 17th entry on one line is a parse error there."""
    lines = ccx.deck_nset("PORT0", list(range(1, 40)))
    assert lines[0] == "*NSET, NSET=PORT0"
    for line in lines[1:]:
        assert len(line.split(",")) <= 16
    # And nothing is lost in the wrapping.
    written = [int(value) for line in lines[1:] for value in line.split(",")]
    assert written == list(range(1, 40))


# --------------------------------------------------------------------------- #
# The results                                                                 #
# --------------------------------------------------------------------------- #


def test_von_mises_of_a_hydrostatic_stress_is_zero():
    """Equal pressure in all three directions distorts nothing."""
    assert ccx.von_mises([100.0, 100.0, 100.0, 0.0, 0.0, 0.0]) == pytest.approx(0.0)


def test_von_mises_of_uniaxial_tension_is_the_stress():
    assert ccx.von_mises([250.0, 0.0, 0.0, 0.0, 0.0, 0.0]) == pytest.approx(250.0)


def test_von_mises_of_pure_shear():
    """sqrt(3) times the shear stress, which is the textbook value."""
    assert ccx.von_mises([0.0, 0.0, 0.0, 100.0, 0.0, 0.0]) == pytest.approx(100.0 * 3**0.5)


def test_von_mises_tolerates_a_short_tensor():
    """A block that came back with fewer components than expected."""
    assert ccx.von_mises([250.0]) == pytest.approx(250.0)


def _frd_value(number):
    """One E12.5 field, the way CalculiX's Fortran writes it.

    Twelve columns exactly: a sign or a space, then `0.` and five digits, then
    the exponent. This is what makes the fixed-column read necessary -- a
    negative value fills the field, so two of them in a row touch.
    """
    if number == 0.0:
        return " 0.00000E+00"
    import math

    exponent = math.floor(math.log10(abs(number))) + 1
    digits = round(abs(number) / (10.0**exponent) * 1e5)
    if digits >= 100000:
        # The rounding carried: 0.99999995 rounds to 1.00000, which is not a
        # normalized mantissa. Fortran renormalizes rather than widening the
        # field, and so must this -- a 13-column field is not what CalculiX
        # writes, and a fixture that produced one would be testing the parser
        # against a format nothing emits.
        digits //= 10
        exponent += 1
    return "%s0.%05dE%+03d" % ("-" if number < 0 else " ", digits, exponent)


def _frd_node(node, values):
    """One `-1` record: (1X,'-1',I10,6E12.5)."""
    return " -1%10d%s\n" % (node, "".join(_frd_value(value) for value in values))


def _frd_block(name, components, rows):
    """One result block, header and all."""
    text = " -4  %-8s%5d%5d\n" % (name, components, 1)
    for index in range(components):
        text += " -5  %-8s%5d%5d%5d%5d\n" % ("C%d" % (index + 1), 1, 2, index + 1, 0)
    for node, values in rows:
        text += _frd_node(node, values)
    return text + " -3\n"


# One step of a `.frd` file, in the fixed columns CalculiX writes. The format is
# documented in the CalculiX manual: `-4` opens a block and names it, `-5` names
# its components, `-1` carries one node's values, `-3` closes it.
FRD = _frd_block("DISP", 3, [(1, [1e-4, 2e-4, 3e-4]), (2, [4e-4, 5e-4, 6e-4])]) + _frd_block(
    "STRESS", 6, [(1, [1e8, 0, 0, 0, 0, 0]), (2, [2e8, 0, 0, 0, 0, 0])]
)


@pytest.mark.parametrize(
    "number",
    [0.0, 1.0, -1.0, 1e-4, -1e-4, 2e8, -2e8, 9.99999e-9, 1e-99, -1.23456e77],
)
def test_the_fixture_writes_exactly_twelve_columns(number):
    """The fixture is only worth testing against if it is the real format.

    E12.5 is twelve columns whatever the value, and it is the *width* that the
    parser depends on -- so a fixture that quietly wrote thirteen would prove
    nothing about a real `.frd`.
    """
    field = _frd_value(number)
    assert len(field) == 12, repr(field)
    assert float(field) == pytest.approx(number, rel=1e-4, abs=1e-100)


def test_read_frd_reads_the_blocks_it_was_asked_for(tmp_path):
    path = tmp_path / "job.frd"
    path.write_text(FRD)
    fields = ccx.read_frd(str(path), {"DISP": 3, "STRESS": 6})

    assert fields["DISP"][1] == pytest.approx([1e-4, 2e-4, 3e-4])
    assert fields["DISP"][2] == pytest.approx([4e-4, 5e-4, 6e-4])
    assert fields["STRESS"][2][0] == pytest.approx(2e8)
    assert ccx.von_mises(fields["STRESS"][2]) == pytest.approx(2e8)


def test_read_frd_ignores_a_block_nobody_asked_about(tmp_path):
    path = tmp_path / "job.frd"
    path.write_text(FRD)
    fields = ccx.read_frd(str(path), {"DISP": 3})
    assert set(fields) == {"DISP"}


def test_read_frd_reads_values_that_run_together(tmp_path):
    """Two negative components fill their fields and touch, with no space.

    This is the case that rules out splitting the line on whitespace, and the
    reason the offsets in `calculix_common` are written down rather than
    guessed. `-0.10000E-03-0.20000E-03` is two numbers.
    """
    path = tmp_path / "job.frd"
    path.write_text(_frd_block("DISP", 3, [(7, [-1e-4, -2e-4, -3e-4])]))
    line = [row for row in path.read_text().splitlines() if row.startswith(" -1")][0]
    assert "E-03-0." in line, line  # they really do touch

    fields = ccx.read_frd(str(path), {"DISP": 3})
    assert fields["DISP"][7] == pytest.approx([-1e-4, -2e-4, -3e-4])


def test_read_frd_reads_a_node_number_that_fills_its_field(tmp_path):
    """I10 with no space in front of it, which a big model reaches."""
    path = tmp_path / "job.frd"
    path.write_text(_frd_block("DISP", 3, [(1234567890, [1.0, 2.0, 3.0])]))
    fields = ccx.read_frd(str(path), {"DISP": 3})
    assert fields["DISP"][1234567890] == pytest.approx([1.0, 2.0, 3.0])


def test_read_frd_keeps_the_last_step(tmp_path):
    """A transient run writes the block once per increment.

    What is wanted is the state it was marched to, so a later block replaces an
    earlier one rather than merging into it -- otherwise a node that stopped
    being reported would keep the value it had ten increments ago.
    """
    path = tmp_path / "job.frd"
    second = _frd_block("DISP", 3, [(1, [9e-4, 9e-4, 9e-4]), (2, [9e-4, 9e-4, 9e-4])])
    path.write_text(FRD + second)
    fields = ccx.read_frd(str(path), {"DISP": 3})
    assert fields["DISP"][1] == pytest.approx([9e-4, 9e-4, 9e-4])


def test_read_frd_of_a_file_with_nothing_in_it(tmp_path):
    """A run that produced a file and no results: reported as empty, not raised."""
    path = tmp_path / "job.frd"
    path.write_text("    1C\n 9999\n")
    assert ccx.read_frd(str(path), {"DISP": 3}) == {"DISP": {}}


# --------------------------------------------------------------------------- #
# The picture                                                                 #
# --------------------------------------------------------------------------- #


def test_colours_run_from_one_end_of_the_ramp_to_the_other():
    colours = ccx.colours_for([0.0, 0.5, 1.0])
    assert colours.shape == (3, 4)
    # Blue at the bottom, red at the top: the blue channel falls and the red
    # channel rises, which is what makes the picture readable at a glance.
    assert colours[0][2] > colours[-1][2]
    assert colours[-1][0] > colours[0][0]
    # Opaque throughout.
    assert list(colours[:, 3]) == [255, 255, 255]


def test_a_uniform_field_is_one_colour_rather_than_a_divide_by_zero():
    """An unloaded part, which is a legitimate answer and not a failure."""
    colours = ccx.colours_for([5.0, 5.0, 5.0])
    assert len({tuple(colour) for colour in colours}) == 1


def test_colours_are_clamped_to_the_range_they_were_given():
    """`low=0` is passed for stress and speed, neither of which goes negative.

    A value outside the range must land on the end of the ramp rather than
    indexing off it.
    """
    colours = ccx.colours_for([-10.0, 0.0, 100.0, 500.0], low=0.0, high=100.0)
    assert tuple(colours[0]) == tuple(colours[1])
    assert tuple(colours[2]) == tuple(colours[3])


def test_field_per_node_follows_the_node_order():
    mesh = _one_tetrahedron()
    field = ccx.field_per_node(mesh, {1: 10.0, 3: 30.0})
    # Node 2 and node 4 said nothing, so they take the default.
    assert list(field) == [10.0, 0.0, 30.0, 0.0]


def test_failed_is_the_shape_partcads_wrapper_expects():
    result = ccx.failed(ccx.SolverMissing("no ccx here"))
    assert result["success"] is False
    assert "no ccx here" in result["exception"]


# --------------------------------------------------------------------------- #
# The two entry points                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("module_name", ["fea_calculix", "cfd_calculix"])
def test_each_analysis_defines_the_entry_point_partcad_calls(module_name):
    """`process(path, request)` is the contract with `wrapper_export.py`."""
    import importlib

    module = importlib.import_module(module_name)
    assert callable(module.process)
    assert module.process.__code__.co_varnames[:2] == ("path", "request")


@pytest.mark.parametrize("module_name", ["fea_calculix", "cfd_calculix"])
def test_a_missing_shape_is_reported_rather_than_raised(module_name, tmp_path):
    """PartCAD's wrapper wants a dict back, not an exception."""
    import importlib

    module = importlib.import_module(module_name)
    result = module.process(str(tmp_path / "out.glb"), {})
    assert result["success"] is False
    assert "shape" in result["exception"].lower()


@pytest.mark.parametrize("module_name", ["fea_calculix", "cfd_calculix"])
def test_the_declared_parameters_are_all_numbers(module_name):
    """YAML 1.1 reads `2.5e8` as a string; `2.5e+8` as a number.

    Every parameter is coerced on the way in, so a string still works -- but a
    configuration should mean what it looks like it means, and this is what
    catches an exponent that lost its sign in an edit.
    """
    import yaml

    config = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "partcad.yaml")))
    analysis = module_name.split("_")[0]
    for name, value in config["cae"][analysis].items():
        # PartCAD's own keys, which describe the file type rather than parametrise
        # the analysis: they never reach the script (see `IMPLEMENTATION_KEYS` in
        # `partcad/output.py`) and none of them is a number.
        if name in (
            "desc",
            "path",
            "extension",
            "package",
            "dockerImage",
            "container",
            "pythonRequirements",
            "pythonVersion",
            "decode",
            # What the implementation reads of a part's section (see
            # `ANALYSIS_KEYS` in `partcad/output.py`), checked below.
            "reads",
        ):
            continue
        assert isinstance(value, (int, float)), "%s: %r is not a number" % (name, value)


# What both scripts import beyond the standard library. Declared in
# `partcad.yaml` as `pythonRequirements` *and* carried by the image, which is
# the contract rather than a duplication: the image says where these run best,
# the requirements say how they run at all.
IMPORTS = ("gmsh", "numpy", "trimesh")


def test_every_file_type_names_the_image_it_runs_best_in():
    """`dockerImage` is a preference, and the pin has to be one that cannot move.

    `ccx` is a native executable and gmsh publishes no wheel for 64-bit ARM
    Linux, so the image is what lets this package work on a machine with
    nothing installed. It is not what lets it work at all -- that is the
    requirements below -- which is why a machine using conda or venv ignores
    this line entirely.
    """
    import yaml

    config = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "partcad.yaml")))
    for name, file_type in config["cae"].items():
        image = file_type.get("dockerImage")
        assert image, "%s names no image" % name
        # An immutable tag. A moving one would let the image and the code
        # expecting it drift apart with nothing to point at afterwards.
        assert ":" in image.rsplit("/", 1)[-1], "%s pins no tag: %s" % (name, image)
        assert not image.endswith((":latest", ":main", ":devel")), "%s pins a moving tag: %s" % (name, image)
        # No architecture. PartCAD appends one and falls back to the bare name,
        # so writing it here would pin the package to one machine.
        assert not image.endswith(("-amd64", "-arm64")), "%s pins one architecture: %s" % (name, image)


def test_the_pin_is_the_tag_this_repository_builds():
    """Two files name it, and a pin nobody published fails at the first analysis."""
    import subprocess

    import yaml

    here = os.path.dirname(__file__)
    built = subprocess.run([os.path.join(here, "image-tag.sh")], capture_output=True, text=True, check=True)
    config = yaml.safe_load(open(os.path.join(here, "partcad.yaml")))
    for name, file_type in config["cae"].items():
        assert file_type["dockerImage"].rsplit(":", 1)[1] == built.stdout.strip(), (
            "%s pins a tag this repository does not build; run ./image-tag.sh" % name
        )


def test_the_workflow_and_the_dockerfile_name_one_base_image():
    """The build reads the base image as a control, so it has to be the same one.

    Before it decides a content tag is unpublished, the workflow inspects the
    base image to prove the registry answered at all -- which only distinguishes
    "absent" from "unreachable" if that is the image this actually builds
    `FROM`. Two files naming it is two chances for one to move; the Dockerfile
    is the one that decides.
    """
    import re

    here = os.path.dirname(__file__)
    dockerfile = open(os.path.join(here, "Dockerfile")).read()
    workflow = open(os.path.join(here, ".github", "workflows", "image.yml")).read()

    built_from = re.search(r"^FROM\s+(\S+?):", dockerfile, re.MULTILINE)
    assert built_from, "the Dockerfile names no base image"
    inspected = re.search(r"^\s*BASE_IMAGE:\s*(\S+)\s*$", workflow, re.MULTILINE)
    assert inspected, "the workflow names no BASE_IMAGE"

    assert built_from.group(1) == inspected.group(1), (
        "the Dockerfile builds FROM %s but the workflow probes %s" % (built_from.group(1), inspected.group(1))
    )


def test_the_scripts_declare_everything_they_import():
    """An import nothing installs is one that only works inside the image.

    Which is the failure this contract exists to prevent: a package that runs
    for whoever has Docker and fails for everyone else, without saying so.
    """
    here = os.path.dirname(__file__)
    import yaml

    config = yaml.safe_load(open(os.path.join(here, "partcad.yaml")))
    declared = " ".join(config["pythonRequirements"])

    source = "".join(
        open(os.path.join(here, name)).read() for name in ("calculix_common.py", "fea_calculix.py", "cfd_calculix.py")
    )
    imported = set(re.findall(r"^\s*import (\w+)", source, re.MULTILINE))
    imported |= set(re.findall(r"^\s*from (\w+) import", source, re.MULTILINE))
    stdlib = set(sys.stdlib_module_names) | {"calculix_common"}

    for module in sorted(imported - stdlib):
        assert module in IMPORTS, "%s is imported but is not one of this package's dependencies" % module
        assert module in declared, "%s is imported but not declared in pythonRequirements" % module


def test_every_declared_file_type_has_its_script_on_disk():
    import yaml

    here = os.path.dirname(__file__)
    config = yaml.safe_load(open(os.path.join(here, "partcad.yaml")))
    for name, file_type in config["cae"].items():
        assert os.path.isfile(os.path.join(here, file_type["path"])), name
        # PartCAD has no default extension for a `cae:` type and will refuse one
        # that does not state it.
        assert file_type.get("extension"), name


# --------------------------------------------------------------------------- #
# A load with a direction, and the part's own gravity                         #
# --------------------------------------------------------------------------- #


def _step(loaded_record, request):
    """The step of a deck holding one fixed port and one loaded port of two nodes each."""
    import fea_calculix

    lines = fea_calculix._deck(
        _two_tetrahedra(), [("FIX0", [1, 2], {"fix": True})], [("LOAD0", [3, 4], loaded_record)], request
    )
    return lines[lines.index("*STEP") :]


def test_a_load_stated_as_a_magnitude_is_applied_straight_down():
    """What every part that states no direction has always been analysed under."""
    step = _step({"load": 10.0}, {"self_weight": False})
    assert step[step.index("*CLOAD") + 1 :][:1] == ["LOAD0, 3, -5"]
    assert "*DLOAD" not in step


def test_a_load_with_a_direction_is_applied_along_it_in_the_parts_frame():
    """Each component spread over the port's nodes, as the magnitude always was; a component of nought is left out."""
    step = _step({"load": 5.0, "force": [3.0, 0.0, -4.0]}, {"self_weight": False})
    cload = step.index("*CLOAD")
    assert step[cload + 1 : cload + 3] == ["LOAD0, 1, 1.5", "LOAD0, 3, -2"]
    assert not any(line.startswith("LOAD0, 2,") for line in step)


def test_a_load_is_shared_by_the_surface_each_node_stands_for():
    """A third of every surface triangle near the port, to each of its corners.

    Not a share per node: that loads a node inside the material like one on
    the surface, and a row of nodes a mesher packed along a curve as hard as a
    whole patch -- which on a worm gear's tooth read as a third more peak stress
    from nothing but where the nodes fell.
    """
    tetrahedron = _one_tetrahedron()
    # Three faces of 50 mm^2 meet at node 1; each other node has two of them and
    # the slanted face, 50 sqrt(3).
    slanted = 50.0 * 3**0.5
    total = 150.0 + slanted
    shares = tetrahedron.surface_shares([2.5, 2.5, 2.5], 100.0)
    assert shares[1] == pytest.approx(50.0 / total)
    for node in (2, 3, 4):
        assert shares[node] == pytest.approx((100.0 + slanted) / 3.0 / total)
    # Within a millimetre of the bottom face's centroid, that face alone.
    bottom = [10.0 / 3, 10.0 / 3, 0.0]
    assert tetrahedron.surface_shares(bottom, 1.0) == pytest.approx({1: 1 / 3, 2: 1 / 3, 3: 1 / 3})
    # And nowhere near the surface, nothing.
    assert tetrahedron.surface_shares([50.0, 50.0, 50.0], 1.0) == {}

    import fea_calculix

    lines = fea_calculix._deck(
        tetrahedron,
        [("FIX0", [4], {"fix": True})],
        [("LOAD0", [1, 2, 3], {"load": 9.0, "location": [bottom, [0, 0, 1], 0]})],
        {"self_weight": False, "port_radius": 0.1},
    )
    cload = lines.index("*CLOAD")
    assert lines[cload + 1 : cload + 4] == ["1, 3, -3", "2, 3, -3", "3, 3, -3"]


def test_on_second_order_elements_a_load_is_on_the_mid_side_nodes():
    """A six-node triangle's consistent load under a uniform traction: a third to each middle, none to a corner.

    On the corners it would stand on a few nodes, as a point load does, and the
    stress under each would be the mesh's rather than the part's.
    """
    corners = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 10.0, 0.0], [0.0, 0.0, 10.0]])
    # CalculiX's C3D10 order: the middles of 1-2, 2-3, 3-1, 1-4, 2-4, 3-4.
    edges = [(0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3)]
    middles = np.array([(corners[a] + corners[b]) / 2 for a, b in edges])
    tetrahedron = ccx.Mesh(
        node_ids=np.arange(1, 11, dtype=np.int64),
        coordinates=np.vstack([corners, middles]),
        elements=[(1, list(range(1, 11)))],
        element_type="C3D10",
        extent=10.0,
    )
    shares = tetrahedron.surface_shares([10.0 / 3, 10.0 / 3, 0.0], 1.0)
    assert shares == pytest.approx({5: 1 / 3, 6: 1 / 3, 7: 1 / 3})


def test_the_part_is_weighed_along_minus_z_unless_it_states_its_own_gravity():
    assert "EALL, GRAV, 9.81, 0, 0, -1" in _step({"load": 1.0}, {})
    # Upside down, and accelerating a little sideways: weighed along what it said.
    step = _step({"load": 1.0}, {"gravity": [3.0, 0.0, 4.0]})
    assert "EALL, GRAV, 5, 0.6, 0, 0.8" in step
    # Which replaces 'self_weight' either way: stated weightless, it weighs nothing.
    assert "*DLOAD" not in _step({"load": 1.0}, {"gravity": [0.0, 0.0, 0.0], "self_weight": True})
    assert "EALL, GRAV, 9.81, 0, 1, 0" in _step({"load": 1.0}, {"gravity": [0, 9.81, 0], "self_weight": False})


def test_the_fea_file_type_says_it_reads_a_direction_and_a_gravity():
    """What PartCAD checks before handing either over (`partcad.cae.READS`)."""
    import yaml

    config = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "partcad.yaml")))
    assert sorted(config["cae"]["fea"]["reads"]) == ["force", "gravity"]
    # The flow solver reads neither, and says nothing, so a direction is refused for it.
    assert "reads" not in config["cae"]["cfd"]


def test_second_order_elements_are_meshed_with_straight_edges(monkeypatch):
    """Mid-side nodes moved out onto a tight curve fold an element inside out, and CalculiX writes no stresses.

    Which is what a worm gear's teeth did: "nonpositive jacobian" in a few
    dozen elements, and no answer at all. The mesher is stood in for, so this
    needs no gmsh: what is checked is what it is asked for.
    """
    asked = {}

    class Gmsh:
        class option:
            @staticmethod
            def setNumber(name, value):
                asked[name] = value

        class model:
            @staticmethod
            def add(name):
                pass

            class occ:
                @staticmethod
                def importShapes(path):
                    pass

                @staticmethod
                def synchronize():
                    pass

            class mesh:
                @staticmethod
                def generate(dimension):
                    asked["generated"] = dimension

                @staticmethod
                def getNodes():
                    return [1, 2, 3, 4], [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1], None

        @staticmethod
        def initialize():
            pass

        @staticmethod
        def finalize():
            pass

    monkeypatch.setattr(ccx, "_gmsh", lambda: Gmsh)
    monkeypatch.setattr(ccx, "_write_step", lambda shape, path: None)
    monkeypatch.setattr(ccx, "_extent", lambda gmsh: 10.0)
    monkeypatch.setattr(ccx, "_tetrahedra", lambda gmsh, order: ("C3D10", [(1, [1, 2, 3, 4])]))

    ccx.mesh_shape(object(), 0.05, 2)

    assert asked["Mesh.ElementOrder"] == 2
    assert asked["Mesh.SecondOrderLinear"] == 1
    assert asked["generated"] == 3
