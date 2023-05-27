import base64
import logging
import os
import pcbnew
import pytest
import shutil
import svgpathtools

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Tuple, Union


Numeric = Union[int, float]
Box = Tuple[Numeric, Numeric, Numeric, Numeric]


KICAD_VERSION = int(pcbnew.Version().split(".")[0])
logger = logging.getLogger(__name__)


def pytest_collection_modifyitems(items):
    try:
        is_nightly = pcbnew.IsNightlyVersion()
    except AttributeError:
        is_nightly = False

    if is_nightly:
        for item in items:
            item.add_marker(
                pytest.mark.xfail(reason="Failures on nightly version ignored")
            )


def pytest_addoption(parser):
    parser.addoption(
        "--test-plugin-installation",
        action="store_true",
        help="Run tests using ~/.local/share/kicad/7.0/3rdparty/plugins instance instead of local one",
        default=False,
    )
    parser.addoption(
        "--save-results-as-reference",
        action="store_true",
        help="Save test results as expected results."
        "This option is for updating expected results and NOT for testing",
        default=False,
    )


@pytest.fixture(scope="session")
def workdir(request):
    if request.config.getoption("--test-plugin-installation"):
        home_directory = Path.home()
        return f"{home_directory}/.local/share/kicad/7.0/3rdparty/plugins"
    return Path(os.path.realpath(__file__)).parents[1]


@pytest.fixture(scope="session")
def package_name(request):
    if request.config.getoption("--test-plugin-installation"):
        return "com_github_adamws_kicad-kbplacer"
    return "kbplacer"


@pytest.fixture(autouse=True, scope="session")
def prepare_kicad_config(request):
    config_path = pcbnew.SETTINGS_MANAGER.GetUserSettingsPath()
    colors_path = f"{config_path}/colors"
    os.makedirs(colors_path, exist_ok=True)
    if not os.path.exists(f"{colors_path}/user.json"):
        shutil.copy("./colors/user.json", colors_path)


def get_references_dir(request):
    test_dir = Path(request.module.__file__).parent
    test_name, test_parameters = request.node.name.split("[")
    example_name, route_option, diode_option = test_parameters[:-1].split(";")
    kicad_dir = "kicad7" if KICAD_VERSION == 7 else "kicad6"
    return (
        test_dir
        / "data"
        / test_name
        / kicad_dir
        / example_name
        / f"{route_option}-{diode_option}"
    )


def get_footprints_dir(request):
    test_dir = Path(request.module.__file__).parent
    return test_dir / "data/footprints/tests.pretty"


def merge_bbox(left: Box, right: Box) -> Box:
    """
    Merge bounding boxes in format (xmin, xmax, ymin, ymax)
    """
    return tuple(f(l, r) for l, r, f in zip(left, right, [min, max, min, max]))


def shrink_svg(svg: ET.ElementTree, margin: int = 0) -> None:
    """
    Shrink the SVG canvas to the size of the drawing.
    """
    root = svg.getroot()
    paths = svgpathtools.document.flattened_paths(root)

    if len(paths) == 0:
        return
    bbox = paths[0].bbox()
    for x in paths:
        bbox = merge_bbox(bbox, x.bbox())
    bbox = list(bbox)
    bbox[0] -= margin
    bbox[1] += margin
    bbox[2] -= margin
    bbox[3] += margin

    root.set(
        "viewBox",
        f"{bbox[0]} {bbox[2]} {bbox[1] - bbox[0]} {bbox[3] - bbox[2]}",
    )

    root.set("width", f"{float(bbox[1] - bbox[0])}mm")
    root.set("height", f"{float(bbox[3] - bbox[2])}mm")


def remove_empty_groups(root):
    name = "{http://www.w3.org/2000/svg}g"
    for elem in root.findall(name):
        if len(elem) == 0:
            root.remove(elem)
    for child in root:
        remove_empty_groups(child)


def remove_tags(root, name):
    for elem in root.findall(name):
        root.remove(elem)


# pcb plotting based on https://github.com/kitspace/kitspace-v2/tree/master/processor/src/tasks/processKicadPCB
# and https://gitlab.com/kicad/code/kicad/-/blob/master/demos/python_scripts_examples/plot_board.py
def generate_render(tmpdir, request):
    project_name = "keyboard-before"
    pcb_path = f"{tmpdir}/{project_name}.kicad_pcb"
    board = pcbnew.LoadBoard(pcb_path)

    plot_layers = [
        "B_Cu",
        "F_Cu",
        "B_Silkscreen",
        "F_Silkscreen",
        "Edge_cuts",
        # on Kicad6 always printed in black, see: https://gitlab.com/kicad/code/kicad/-/issues/10293:
        "B_Mask",
        "F_Mask",
    ]
    plot_control = pcbnew.PLOT_CONTROLLER(board)
    plot_options = plot_control.GetPlotOptions()
    plot_options.SetOutputDirectory(tmpdir)
    plot_options.SetColorSettings(pcbnew.GetSettingsManager().GetColorSettings("user"))
    plot_options.SetPlotFrameRef(False)
    plot_options.SetSketchPadLineWidth(pcbnew.FromMM(0.35))
    plot_options.SetAutoScale(False)
    plot_options.SetMirror(False)
    plot_options.SetUseGerberAttributes(False)
    plot_options.SetScale(1)
    plot_options.SetUseAuxOrigin(True)
    plot_options.SetNegative(False)
    plot_options.SetPlotReference(True)
    plot_options.SetPlotValue(True)
    plot_options.SetPlotInvisibleText(False)
    if KICAD_VERSION == 7:
        plot_options.SetDrillMarksType(pcbnew.DRILL_MARKS_NO_DRILL_SHAPE)
        plot_options.SetSvgPrecision(aPrecision=1)
    else:
        plot_options.SetDrillMarksType(pcbnew.PCB_PLOT_PARAMS.NO_DRILL_SHAPE)
        plot_options.SetSvgPrecision(aPrecision=1, aUseInch=False)

    plot_plan = []
    start = pcbnew.PCBNEW_LAYER_ID_START
    end = pcbnew.PCBNEW_LAYER_ID_START + pcbnew.PCB_LAYER_ID_COUNT
    for i in range(start, end):
        name = pcbnew.LayerName(i).replace(".", "_")
        if name in plot_layers:
            plot_plan.append((name, i))

    for (layer_name, layer_id) in plot_plan:
        plot_control.SetLayer(layer_id)
        if KICAD_VERSION == 7:
            plot_control.OpenPlotfile(layer_name, pcbnew.PLOT_FORMAT_SVG)
        else:
            plot_control.OpenPlotfile(
                layer_name, pcbnew.PLOT_FORMAT_SVG, aSheetDesc=layer_name
            )
        plot_control.SetColorMode(True)
        plot_control.PlotLayer()
        plot_control.ClosePlot()

        filepath = os.path.join(tmpdir, f"{project_name}-{layer_name}.svg")
        tree = ET.parse(filepath)
        root = tree.getroot()
        # for some reason there is plenty empty groups in generated svg's (kicad bug?)
        # remove for clarity:
        remove_empty_groups(root)
        shrink_svg(tree, margin=1)
        tree.write(f"{tmpdir}/{layer_name}.svg")
        os.remove(f"{tmpdir}/{project_name}-{layer_name}.svg")

    if request.config.getoption("--save-results-as-reference"):
        references_dir = get_references_dir(request)
        references_dir.mkdir(parents=True, exist_ok=True)

        for layer_name, _ in plot_plan:
            filepath = os.path.join(tmpdir, f"{layer_name}.svg")
            shutil.copy(filepath, references_dir)

    new_tree = None
    new_root = None
    for i, (layer_name, _) in enumerate(plot_plan):
        filepath = os.path.join(tmpdir, f"{layer_name}.svg")
        tree = ET.parse(filepath)
        layer = tree.getroot()
        if i == 0:
            new_tree = tree
            new_root = layer
        else:
            for child in layer:
                new_root.append(child)

    # due to merging of multiple files we have multiple titles/descriptions,
    # remove all title/desc from root since we do not care about them:
    remove_tags(new_root, "{http://www.w3.org/2000/svg}title")
    remove_tags(new_root, "{http://www.w3.org/2000/svg}desc")

    shrink_svg(new_tree, margin=1)
    new_tree.write(f"{tmpdir}/render.svg")


def add_switch_footprint(board, request, ref_count) -> pcbnew.FOOTPRINT:
    library = get_footprints_dir(request)
    f = pcbnew.FootprintLoad(str(library), "SW_Cherry_MX_PCB_1.00u")
    f.SetReference(f"SW{ref_count}")
    board.Add(f)
    return f


def add_diode_footprint(board, request, ref_count) -> pcbnew.FOOTPRINT:
    library = get_footprints_dir(request)
    f = pcbnew.FootprintLoad(str(library), "D_SOD-323")
    f.SetReference(f"D{ref_count}")
    board.Add(f)
    return f


def get_track(board, start, end, layer):
    track = pcbnew.PCB_TRACK(board)
    track.SetWidth(pcbnew.FromMM(0.25))
    track.SetLayer(layer)
    if KICAD_VERSION == 7:
        track.SetStart(pcbnew.VECTOR2I(start.x, start.y))
        track.SetEnd(pcbnew.VECTOR2I(end.x, end.y))
    else:
        track.SetStart(start)
        track.SetEnd(end)
    return track


def add_track(board, start, end, layer):
    track = get_track(board, start, end, layer)
    board.Add(track)
    return track


def to_base64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def svg_to_base64_html(path):
    b64 = to_base64(path)
    return f'<div class="image"><img src="data:image/svg+xml;base64,{b64}"></div>'


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    pytest_html = item.config.pluginmanager.getplugin("html")
    outcome = yield
    report = outcome.get_result()
    extra = getattr(report, "extra", [])

    if report.when == "call" and not report.skipped:
        tmpdir = item.funcargs["tmpdir"]
        render_path = tmpdir / "render.svg"
        if render_path.isfile():
            render = svg_to_base64_html(render_path)
            extra.append(pytest_html.extras.html(render))
        report.extra = extra
