import difflib
import logging
import pytest
import shutil
import subprocess
import xml.etree.ElementTree as ET

from xmldiff import actions, main
from .conftest import generate_render


logger = logging.getLogger(__name__)


def run_kbplacer_process(tmpdir, route, diode_position, workdir, package_name):
    layout_file = "{}/kle-internal.json".format(tmpdir)
    pcb_path = "{}/keyboard-before.kicad_pcb".format(tmpdir)

    kbplacer_args = [
        "python3",
        "-m",
        package_name,
        "-l",
        layout_file,
        "-b",
        pcb_path,
    ]
    if route:
        kbplacer_args.append("--route")
    if diode_position:
        kbplacer_args.append("--diode-position")
        kbplacer_args.append(diode_position)

    p = subprocess.Popen(
        kbplacer_args,
        cwd=workdir,
    )
    p.communicate()
    if p.returncode != 0:
        raise Exception("Switch placement failed")


def assert_layer(expected: ET.ElementTree, actual: ET.ElementTree):
    expected = ET.tostring(expected).decode()
    actual = ET.tostring(actual).decode()

    edit_script = main.diff_texts(
        expected, actual, diff_options={"F": 0, "ratio_mode": "accurate"}
    )

    if not all([type(node) == actions.MoveNode for node in edit_script]):
        logger.info("Difference found")
        diff = difflib.unified_diff(expected.splitlines(), actual.splitlines())
        for d in diff:
            logger.info(d)

        logger.info("Evaluating rules exceptions")
        differences = [node for node in edit_script if type(node) != actions.MoveNode]

        for node in list(differences):
            logger.info(f"Action node: {node}")
            if type(node) == actions.UpdateAttrib and node.name == "textLength":
                # let 'textLength' attributes to differ:
                logger.info("'textLength' attribute is allowed to be different")
                differences.remove(node)

        assert len(differences) == 0, "Not allowd differences found"


def assert_kicad_svg(expected, actual):
    logger.info("Analyzing SVG file")

    ns = {"svg": "http://www.w3.org/2000/svg"}
    expected_root = ET.parse(expected).getroot()
    expected_groups = expected_root.findall("svg:g", ns)

    actual_root = ET.parse(actual).getroot()
    actual_groups = actual_root.findall("svg:g", ns)

    assert len(expected_groups) == len(actual_groups)

    number_of_groups = len(expected_groups)

    for i in range(0, number_of_groups):
        assert_layer(expected_groups[i], actual_groups[i])


def __get_parameters():
    examples = ["2x2", "3x2-sizes", "2x3-rotations", "1x4-rotations-90-step"]
    route_options = {"NoTracks": False, "Tracks": True}
    diode_options = {"DefaultDiode": None, "DiodeOption1": "0,4.5,0,BACK"}
    test_params = []
    for example in examples:
        for route_option_name, route_option in route_options.items():
            for diode_option_name, diode_option in diode_options.items():
                test_id = f"{example};{route_option_name};{diode_option_name}"
                param = pytest.param(example, route_option, diode_option, id=test_id)
                test_params.append(param)
    return test_params


@pytest.mark.parametrize("example,route,diode_position", __get_parameters())
def test_with_examples(
    example, route, diode_position, tmpdir, request, workdir, package_name
):
    test_dir = request.fspath.dirname

    source_dir = "{}/../examples/{}".format(test_dir, example)
    shutil.copy("{}/keyboard-before.kicad_pcb".format(source_dir), tmpdir)
    shutil.copy("{}/kle-internal.json".format(source_dir), tmpdir)

    if route and diode_position != None:
        pytest.skip("Routing with non-default diode position not supported yet")

    run_kbplacer_process(tmpdir, route, diode_position, workdir, package_name)

    generate_render(tmpdir)

    # checking with reference currently suppored only for this subset of options:
    if route and diode_position == None:
        assert_kicad_svg(f"{source_dir}/pictures/render.svg", f"{tmpdir}/render.svg")
