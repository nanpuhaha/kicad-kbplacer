import builtins
import math
import re
from dataclasses import dataclass
from logging import Logger
from typing import Union

import pcbnew

from .board_modifier import KICAD_VERSION, BoardModifier, Point, Side


@dataclass
class DiodePosition:
    relative_position: Point
    orientation: float
    side: Side

@dataclass
class Spacing:
    x: float
    y: float
    
    @classmethod
    def load_from_kle(cls, kle: dict):
        x = kle['spacing']
        return cls(x=kle['spacing']['x'], y=kle['spacing']['y'])

ChocSpacing = Spacing(18, 17)
MXSpacing = Spacing(19.05, 19.05)
CfxSpacing = Spacing(17.05, 17.05)

def position_in_rotated_coordinates(
    point: pcbnew.wxPoint, angle: float
) -> pcbnew.wxPoint:
    """
    Map position in xy-Cartesian coordinate system to x'y'-Cartesian which has same origin
    but axes are rotated by angle

    :param point: A point to be mapped
    :param angle: Rotation angle (in degrees) of x'y'-Cartesian coordinates
    :type point: pcbnew.wxPoint
    :type angle: float
    :return: Result position in x'y'-Cartesian coordinates
    :rtype: pcbnew.wxPoint
    """
    x, y = point.x, point.y
    angle = math.radians(angle)
    xr = (x * math.cos(angle)) + (y * math.sin(angle))
    yr = (-x * math.sin(angle)) + (y * math.cos(angle))
    return pcbnew.wxPoint(xr, yr)


def position_in_cartesian_coordinates(
    point: pcbnew.wxPoint, angle: float
) -> pcbnew.wxPoint:
    """Performs inverse operation to position_in_rotated_coordinates i.e.
    map position in rotated (by angle) x'y'-Cartesian to xy-Cartesian

    :param point: A point to be mapped
    :param angle: Rotation angle (in degrees) of x'y'-Cartesian coordinates
    :type point: pcbnew.wxPoint
    :type angle: float
    :return: Result position in xy-Cartesian coordinates
    :rtype: pcbnew.wxPoint
    """
    xr, yr = point.x, point.y
    angle = math.radians(angle)
    x = (xr * math.cos(angle)) - (yr * math.sin(angle))
    y = (xr * math.sin(angle)) + (yr * math.cos(angle))
    return pcbnew.wxPoint(x, y)


class KeyPlacer(BoardModifier):
    def __init__(
        self, logger: Logger, board: pcbnew.BOARD, layout, key_spacing: Spacing = MXSpacing
    ) -> None:
        super().__init__(logger, board)
        self.__layout = layout
        self.__key_distance = pcbnew.FromMM(key_distance)
        self.logger.debug(f"Set key 1U distance: {self.__key_distance}")
        self.__current_key = 1
        self.__current_diode = 1
        self.__reference_coordinate = pcbnew.wxPointMM(25, 25)

    def get_current_key(
        self, key_format: str, stabilizer_format: str
    ) -> tuple[pcbnew.FOOTPRINT, pcbnew.FOOTPRINT]:
        key = self.get_footprint(key_format.format(self.__current_key))

        # in case of perigoso/keyswitch-kicad-library, stabilizer holes are not part
        # of of switch footprint and needs to be handled separately,
        # check if there is stabilizer with id matching current key and return it
        # stabilizer will be None if not found
        stabilizer = self.board.FindFootprintByReference(
            stabilizer_format.format(self.__current_key)
        )
        self.__current_key += 1

        return key, stabilizer

    def get_current_diode(self, diode_format: str) -> pcbnew.FOOTPRINT:
        diode = self.get_footprint(diode_format.format(self.__current_diode))
        self.__current_diode += 1
        return diode

    def calculate_corner_position_of_switch_diode_route(
        self, diode_pad_position: pcbnew.wxPoint, switch_pad_position: pcbnew.wxPoint
    ) -> pcbnew.wxPoint:
        x_diff = diode_pad_position.x - switch_pad_position.x
        y_diff = diode_pad_position.y - switch_pad_position.y
        if builtins.abs(x_diff) < builtins.abs(y_diff):
            up_or_down = -1 if y_diff > 0 else 1
            return pcbnew.wxPoint(
                diode_pad_position.x - x_diff,
                diode_pad_position.y + (up_or_down * builtins.abs(x_diff)),
            )
        else:
            left_or_right = -1 if x_diff > 0 else 1
            return pcbnew.wxPoint(
                diode_pad_position.x + (left_or_right * builtins.abs(y_diff)),
                diode_pad_position.y - y_diff,
            )

    def route_switch_with_diode(
        self,
        switch: pcbnew.FOOTPRINT,
        diode: pcbnew.FOOTPRINT,
        angle: float,
        template_track_points=None,
    ) -> None:
        """Performs routing between switch and diode elements.
        Assumes col-to-row configuration where diode anode is pad number '2'.

        :param switch: Switch footprint to be routed.
        :param diode: Diode footprint to be routed.
        :param angle: Rotation angle (in degrees) of switch footprint (diode rotation is assumed to be the same)
        :param templateTrackPoints: List of positions (relative to diode pad position) of track corners connecting switch and diode.
                                    Does not support vias, will be routed on the layer of the diode.
                                    If None, use automatic routing algorithm.
        :type switch: FOOTPRINT
        :type diode: FOOTPRINT
        :type angle: float
        :type templateTrackPoints: List[pcbnew.wxPoint]
        """
        self.logger.info(f"Routing {switch.GetReference()} with {diode.GetReference()}")

        layer = pcbnew.B_Cu if self.get_side(diode) == Side.BACK else pcbnew.F_Cu
        switch_pad_position = switch.FindPadByNumber("2").GetPosition()
        diode_pad_position = diode.FindPadByNumber("2").GetPosition()
        if KICAD_VERSION == 7:
            switch_pad_position = pcbnew.wxPoint(
                switch_pad_position.x, switch_pad_position.y
            )
            diode_pad_position = pcbnew.wxPoint(
                diode_pad_position.x, diode_pad_position.y
            )

        self.logger.debug(
            f"switchPadPosition: {switch_pad_position}, diodePadPosition: {diode_pad_position}",
        )

        if template_track_points:
            if angle != 0:
                self.logger.info(f"Routing at {angle} degree angle")
            start = diode_pad_position
            for t in template_track_points:
                if angle != 0:
                    diode_pad_position_r = position_in_rotated_coordinates(
                        diode_pad_position, angle
                    )
                    end = t.__add__(diode_pad_position_r)
                    end = position_in_cartesian_coordinates(end, angle)
                else:
                    end = t.__add__(diode_pad_position)
                end = self.add_track_segment_by_points(start, end, layer)
                if end:
                    start = end
        else:
            if (
                switch_pad_position.x == diode_pad_position.x
                or switch_pad_position.y == diode_pad_position.y
            ):
                self.add_track_segment_by_points(
                    diode_pad_position, switch_pad_position, layer
                )
            else:
                # pads are not in single line, attempt routing with two segment track
                if angle != 0:
                    self.logger.info(f"Routing at {angle} degree angle")
                    switch_pad_position_r = position_in_rotated_coordinates(
                        switch_pad_position, angle
                    )
                    diode_pad_position_r = position_in_rotated_coordinates(
                        diode_pad_position, angle
                    )

                    self.logger.debug(
                        f"In rotated coordinates: switchPadPosition: {switch_pad_position_r},"
                        f" diodePadPosition: {diode_pad_position_r}",
                    )

                    corner = self.calculate_corner_position_of_switch_diode_route(
                        diode_pad_position_r, switch_pad_position_r
                    )
                    corner = position_in_cartesian_coordinates(corner, angle)
                else:
                    corner = self.calculate_corner_position_of_switch_diode_route(
                        diode_pad_position, switch_pad_position
                    )

                # first segment: at 45 degree angle
                # (might be in rotated coordinate system) towards switch pad
                self.add_track_segment_by_points(diode_pad_position, corner, layer)
                # second segment: up to switch pad
                self.add_track_segment_by_points(corner, switch_pad_position, layer)

    def get_default_diode_position(self) -> DiodePosition:
        return DiodePosition(Point(5.08, 3.03), 90.0, Side.BACK)

    def get_diode_position(
        self, key_format: str, diode_format: str, is_first_pair_used_as_template: bool
    ) -> DiodePosition:
        if is_first_pair_used_as_template:
            key1 = self.get_footprint(key_format.format(1))
            diode1 = self.get_footprint(diode_format.format(1))
            pos1 = self.get_position(key1)
            pos2 = self.get_position(diode1)
            return DiodePosition(
                Point(pcbnew.ToMM(pos2.x - pos1.x), pcbnew.ToMM(pos2.y - pos1.y)),
                diode1.GetOrientationDegrees(),
                self.get_side(diode1),
            )
        else:
            return self.get_default_diode_position()

    def remove_dangling_tracks(self) -> None:
        connectivity = self.get_connectivity()
        for track in self.board.GetTracks():
            if connectivity.TestTrackEndpointDangling(track):
                self.board.RemoveNative(track)

    def check_if_diode_routed(
        self, key_format: str, diode_format: str
    ) -> list[pcbnew.wxPoint]:
        switch = self.get_footprint(key_format.format(1))
        diode = self.get_footprint(diode_format.format(1))
        net1 = switch.FindPadByNumber("2").GetNetname()
        net2 = diode.FindPadByNumber("2").GetNetname()
        tracks = [t for t in self.board.GetTracks() if t.GetNetname() == net1 == net2]

        # convert tracks to list of vectors which will be used by `AddTrackSegmentByPoints`
        switch_pad_position = switch.FindPadByNumber("2").GetPosition()
        diode_pad_position = diode.FindPadByNumber("2").GetPosition()
        if KICAD_VERSION == 7:
            switch_pad_position = pcbnew.wxPoint(
                switch_pad_position.x, switch_pad_position.y
            )
            diode_pad_position = pcbnew.wxPoint(
                diode_pad_position.x, diode_pad_position.y
            )

        points_sorted = []
        search_point = diode_pad_position
        for i in range(0, len(tracks) + 1):
            for t in list(tracks):
                start = t.GetStart()
                end = t.GetEnd()
                if KICAD_VERSION == 7:
                    start = pcbnew.wxPoint(start.x, start.y)
                    end = pcbnew.wxPoint(end.x, end.y)
                found_start = start.__eq__(search_point)
                found_end = end.__eq__(search_point)
                if found_start or found_end:
                    points_sorted.append(search_point)
                    search_point = end if found_start else start
                    tracks.remove(t)
                    self.board.RemoveNative(t)
                    break
        if len(points_sorted) != 0:
            points_sorted.pop(0)
            points_sorted.append(switch_pad_position)

        reduced_points = [p.__sub__(diode_pad_position) for p in points_sorted]
        self.logger.info(f"Detected template switch-to-diode path: {reduced_points}")
        return reduced_points

    def run(
        self,
        key_format: str,
        stabilizer_format: str,
        diode_format: str,
        diode_position: Union[DiodePosition, None],
        route_tracks=False,
    ) -> None:
        self.logger.info(f"Diode position: {diode_position}")

        template_tracks = []
        if route_tracks:
            # check if first switch-diode pair is already routed, if yes,
            # then reuse its track shape for remaining pairs, otherwise try to use automatic 'router'
            template_tracks = self.check_if_diode_routed(key_format, diode_format)

        column_switch_pads = {}
        row_diode_pads = {}
        for key in self.__layout["keys"]:
            switch_footprint, stabilizer = self.get_current_key(
                key_format, stabilizer_format
            )

            width = key["width"]
            height = key["height"]
            position = (
                pcbnew.wxPoint(
                    (self.__key_distance * key["x"])
                    + (self.__key_distance * width // 2),
                    (self.__key_distance * key["y"])
                    + (self.__key_distance * height // 2),
                )
                + self.__reference_coordinate
            )
            self.set_position(switch_footprint, position)
            self.reset_rotation(switch_footprint)

            if stabilizer:
                self.set_position(stabilizer, position)
                self.reset_rotation(stabilizer)
                # recognize special case of ISO enter:
                width2 = key["width2"]
                height2 = key["height2"]
                if width == 1.25 and height == 2 and width2 == 1.5 and height2 == 1:
                    stabilizer.SetOrientationDegrees(90)

            diode_footprint = None
            if diode_position:
                diode_footprint = self.get_current_diode(diode_format)
                self.reset_rotation(diode_footprint)
                self.set_side(diode_footprint, diode_position.side)
                diode_footprint.SetOrientationDegrees(diode_position.orientation)
                self.set_relative_position_mm(
                    diode_footprint,
                    position,
                    diode_position.relative_position.to_list(),
                )

            angle = key["rotation_angle"]
            if angle != 0:
                rotation_reference = (
                    pcbnew.wxPoint(
                        (self.__key_distance * key["rotation_x"]),
                        (self.__key_distance * key["rotation_y"]),
                    )
                    + self.__reference_coordinate
                )
                self.rotate(switch_footprint, rotation_reference, angle)
                if stabilizer:
                    self.rotate(stabilizer, rotation_reference, angle)
                if diode_footprint:
                    self.rotate(diode_footprint, rotation_reference, angle)

            # append pad:
            pad = switch_footprint.FindPadByNumber("1")
            net_name = pad.GetNetname()
            match = re.match(r"^COL(\d+)$", net_name)
            if match:
                column_number = match.groups()[0]
                column_switch_pads.setdefault(column_number, []).append(pad)
            else:
                self.logger.warning("Switch pad without recognized net name found.")

            # append diode:
            if diode_footprint:
                pad = diode_footprint.FindPadByNumber("1")
                net_name = pad.GetNetname()
                match = re.match(r"^ROW(\d+)$", net_name)
                if match:
                    row_number = match.groups()[0]
                    row_diode_pads.setdefault(row_number, []).append(pad)
                else:
                    self.logger.warning("Switch pad without recognized net name found.")

            if route_tracks and diode_footprint:
                self.route_switch_with_diode(
                    switch_footprint, diode_footprint, angle, template_tracks
                )

        if route_tracks:
            # very naive routing approach, will fail in some scenarios:
            for column in column_switch_pads:
                pads = column_switch_pads[column]
                positions = [pad.GetPosition() for pad in pads]
                for pos1, pos2 in zip(positions, positions[1:]):
                    # connect two pads together
                    if pos1.x == pos2.x:
                        self.add_track_segment_by_points(pos1, pos2, layer=pcbnew.F_Cu)
                    else:
                        # two segment track
                        y_diff = builtins.abs(pos1.y - pos2.y)
                        x_diff = builtins.abs(pos1.x - pos2.x)
                        vector = [0, (y_diff - x_diff)]
                        if vector[1] <= 0:
                            self.logger.warning(
                                "Switch pad to far to route 2 segment track with 45 degree angles"
                            )
                        else:
                            last_position = self.add_track_segment(
                                pos1, vector, layer=pcbnew.F_Cu
                            )
                            if last_position:
                                self.add_track_segment_by_points(
                                    last_position, pos2, layer=pcbnew.F_Cu
                                )

            for row in row_diode_pads:
                pads = row_diode_pads[row]
                positions = [pad.GetPosition() for pad in pads]
                # we can assume that all diodes are on the same side:
                layer = (
                    pcbnew.B_Cu
                    if self.get_side(pads[0].GetParent()) == Side.BACK
                    else pcbnew.F_Cu
                )
                for pos1, pos2 in zip(positions, positions[1:]):
                    if pos1.y == pos2.y:
                        self.add_track_segment_by_points(pos1, pos2, layer)
                    else:
                        self.logger.warning(
                            "Automatic diode routing supported only when diodes aligned vertically"
                        )

            self.remove_dangling_tracks()
