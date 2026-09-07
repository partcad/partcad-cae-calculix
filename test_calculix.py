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
    # The three ways to get one, and the way out for a machine that has it
    # somewhere else. A message that only says "not found" sends the reader to a
    # search engine.
    assert "calculix-ccx" in message
    assert "conda" in message
    assert ccx.CCX_ENV in message


def test_arm_linux_is_told_that_gmsh_has_no_wheel_for_it(monkeypatch):
    """The one platform where the mesher cannot be installed at all.

    gmsh ships four wheels per release and none of them is linux aarch64, and it
    ships no sdist either, so `partcad.yaml` carries a marker telling pip not to
    try. What is left is a sandbox with no gmsh in it, and the reader has to be
    told that this is the platform rather than something they forgot to install.
    """
    monkeypatch.setattr(ccx.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(ccx.platform, "system", lambda: "Linux")
    monkeypatch.setitem(sys.modules, "gmsh", None)  # 'import gmsh' -> ImportError

    with pytest.raises(ccx.SolverMissing) as raised:
        ccx._gmsh()
    message = str(raised.value)
    assert "ARM" in message
    assert "x86_64" in message


def test_a_sandbox_without_gmsh_elsewhere_says_only_that(monkeypatch):
    """Everywhere else a missing gmsh is a broken sandbox, not a platform gap.

    Saying "no wheel for this platform" on a machine that has one would send the
    reader looking for a problem that is not there.
    """
    monkeypatch.setattr(ccx.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(ccx.platform, "system", lambda: "Linux")
    monkeypatch.setitem(sys.modules, "gmsh", None)

    with pytest.raises(ccx.SolverMissing) as raised:
        ccx._gmsh()
    assert "ARM" not in str(raised.value)


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
        if name in ("desc", "path", "extension"):
            continue
        assert isinstance(value, (int, float)), "%s: %r is not a number" % (name, value)


def test_the_package_declares_what_the_scripts_import():
    """A requirement that is imported and not declared is one the sandbox lacks."""
    import yaml

    here = os.path.dirname(__file__)
    config = yaml.safe_load(open(os.path.join(here, "partcad.yaml")))
    declared = " ".join(config["pythonRequirements"])
    source = "".join(
        open(os.path.join(here, name)).read() for name in ("calculix_common.py", "fea_calculix.py", "cfd_calculix.py")
    )
    for module in ("gmsh", "numpy", "trimesh"):
        if "import %s" % module in source:
            assert module in declared, "%s is imported but not declared" % module


def test_every_declared_file_type_has_its_script_on_disk():
    import yaml

    here = os.path.dirname(__file__)
    config = yaml.safe_load(open(os.path.join(here, "partcad.yaml")))
    for name, file_type in config["cae"].items():
        assert os.path.isfile(os.path.join(here, file_type["path"])), name
        # PartCAD has no default extension for a `cae:` type and will refuse one
        # that does not state it.
        assert file_type.get("extension"), name
