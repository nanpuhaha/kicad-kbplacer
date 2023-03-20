from dataclasses import dataclass
from enum import Flag

from pcbnew import *

KICAD_VERSION = int(Version().split(".")[0])
DEFAULT_CLEARANCE_MM = 0.25


class Side(Flag):
    FRONT = False
    BACK = True


@dataclass
class Point:
    x: float
    y: float

    def to_list(self):
        return [self.x, self.y]


class BoardModifier:
    def __init__(self, logger, board):
        self.logger = logger
        self.board = board

    def get_connectivity(self):
        self.board.BuildConnectivity()
        return self.board.GetConnectivity()

    def get_footprint(self, reference):
        self.logger.info(f"Searching for {reference} footprint")
        footprint = self.board.FindFootprintByReference(reference)
        if footprint is None:
            self.logger.error("Footprint not found")
            msg = f"Cannot find footprint {reference}"
            raise Exception(msg)
        return footprint

    def set_position(self, footprint, position: wxPoint):
        self.logger.info(
            "Setting {} footprint position: {}".format(
                footprint.GetReference(), position
            )
        )
        if KICAD_VERSION == 7:
            footprint.SetPosition(VECTOR2I(position.x, position.y))
        else:
            footprint.SetPosition(position)

    def set_position_by_points(self, footprint, x: int, y: int):
        self.set_position(footprint, wxPoint(x, y))

    def get_position(self, footprint):
        position = footprint.GetPosition()
        self.logger.info(
            "Getting {} footprint position: {}".format(
                footprint.GetReference(), position
            )
        )
        if KICAD_VERSION == 7:
            return wxPoint(position.x, position.y)
        return position

    def set_relative_position_mm(self, footprint, reference_point, direction):
        position = wxPoint(
            reference_point.x + FromMM(direction[0]),
            reference_point.y + FromMM(direction[1]),
        )
        self.set_position(footprint, position)

    def test_track_collision(self, track):
        collide_list = []
        track_shape = track.GetEffectiveShape()
        track_start = track.GetStart()
        track_end = track.GetEnd()
        track_net_code = track.GetNetCode()
        # connectivity needs to be last, otherwise it will update track net name before we want it to:
        connectivity = self.get_connectivity()
        for f in self.board.GetFootprints():
            reference = f.GetReference()
            hull = f.GetBoundingHull()
            hit_test_result = hull.Collide(track_shape)
            if hit_test_result:
                for p in f.Pads():
                    pad_name = p.GetName()
                    pad_shape = p.GetEffectiveShape()

                    # track has non default netlist set so we can skip collision detection for pad of same netlist:
                    if track_net_code != 0 and track_net_code == p.GetNetCode():
                        self.logger.debug(
                            "Track collision ignored, pad {}:{} on same netlist: {}/{}".format(
                                reference, pad_name, track.GetNetname(), p.GetNetname()
                            )
                        )
                        continue

                    # if track starts or ends in pad than assume this collision is expected, with the execption of case where track
                    # already has netlist set and it is different than pad's netlist
                    if p.HitTest(track_start) or p.HitTest(track_end):
                        if (
                            track_net_code != 0
                            and track_net_code != p.GetNetCode()
                            and p.IsOnLayer(track.GetLayer())
                        ):
                            self.logger.debug(
                                "Track collide with pad {}:{}".format(
                                    reference, pad_name
                                )
                            )
                            collide_list.append(p)
                        else:
                            self.logger.debug(
                                "Track collision ignored, track starts or ends in pad {}:{}".format(
                                    reference, pad_name
                                )
                            )
                    else:
                        hit_test_result = pad_shape.Collide(
                            track_shape, FromMM(DEFAULT_CLEARANCE_MM)
                        )
                        on_same_layer = p.IsOnLayer(track.GetLayer())
                        if hit_test_result and on_same_layer:
                            self.logger.debug(
                                "Track collide with pad {}:{}".format(
                                    reference, pad_name
                                )
                            )
                            collide_list.append(p)
        # track ids to clear at the end:
        tracks_to_clear = []
        for t in self.board.GetTracks():
            # check collision if not itself, on same layer and with different netlist (unless 'trackNetCode' is default '0' netlist):
            if (
                t.m_Uuid.__ne__(track.m_Uuid)
                and t.IsOnLayer(track.GetLayer())
                and (track_net_code != t.GetNetCode() or track_net_code == 0)
            ):
                if (
                    track_start == t.GetStart()
                    or track_start == t.GetEnd()
                    or track_end == t.GetStart()
                    or track_end == t.GetEnd()
                ):
                    self.logger.debug(
                        "Track collision ignored, track starts or ends at the end of {} track".format(
                            t.m_Uuid.AsString()
                        )
                    )
                    # ignoring one track means that we can ignore all other connected to it:
                    tracks_to_clear += [
                        x.m_Uuid for x in connectivity.GetConnectedTracks(t)
                    ]
                    # check if connection to this track clears pad collision:
                    connected_pads_ids = [
                        x.m_Uuid for x in connectivity.GetConnectedPads(t)
                    ]
                    for collision in list(collide_list):
                        if collision.m_Uuid in connected_pads_ids:
                            self.logger.debug(
                                "Pad collision removed due to connection with track which leads to that pad"
                            )
                            collide_list.remove(collision)
                else:
                    hit_test_result = t.GetEffectiveShape().Collide(
                        track_shape, FromMM(DEFAULT_CLEARANCE_MM)
                    )
                    if hit_test_result:
                        self.logger.debug(
                            "Track collide with another track: {}".format(
                                t.m_Uuid.AsString()
                            )
                        )
                        collide_list.append(t)
        for collision in list(collide_list):
            if collision.m_Uuid in tracks_to_clear:
                self.logger.debug(
                    "Track collision with {} removed due to connection with track which leads to it".format(
                        collision.m_Uuid.AsString()
                    )
                )
                collide_list.remove(collision)
        return len(collide_list) != 0

    def add_track_to_board(self, track):
        """Add track to the board if track passes collision check.
        If track has no set netlist, it would get netlist of a pad
        or other track, on which it started or ended.
        Collision with element of the same netlist will be ignored
        unless it is default '0' netlist.
        This exception about '0' netlist is important because it helps
        to detect collisions with holes.

        :param track: A track to be added to board
        :return: End position of added track or None if failed to add.
        """
        if not self.test_track_collision(track):
            layer_name = self.board.GetLayerName(track.GetLayer())
            start = track.GetStart()
            stop = track.GetEnd()
            self.logger.info(
                f"Adding track segment ({layer_name}): [{start}, {stop}]",
            )
            self.board.Add(track)
            return stop
        else:
            self.logger.warning("Could not add track segment due to detected collision")
            return None

    def add_track_segment_by_points(self, start, end, layer=B_Cu):
        track = PCB_TRACK(self.board)
        track.SetWidth(FromMM(0.25))
        track.SetLayer(layer)
        if KICAD_VERSION == 7:
            track.SetStart(VECTOR2I(start.x, start.y))
            track.SetEnd(VECTOR2I(end.x, end.y))
        else:
            track.SetStart(start)
            track.SetEnd(end)
        return self.add_track_to_board(track)

    def add_track_segment(self, start, vector, layer=B_Cu):
        end = wxPoint(start.x + vector[0], start.y + vector[1])
        return self.add_track_segment_by_points(start, end, layer)

    def reset_rotation(self, footprint):
        footprint.SetOrientationDegrees(0)

    def rotate(self, footprint, rotation_reference, angle):
        self.logger.info(
            "Rotating {} footprint: rotationReference: {}, rotationAngle: {}".format(
                footprint.GetReference(), rotation_reference, angle
            )
        )
        if KICAD_VERSION == 7:
            footprint.Rotate(
                VECTOR2I(rotation_reference.x, rotation_reference.y),
                EDA_ANGLE(angle * -1, DEGREES_T),
            )
        else:
            footprint.Rotate(rotation_reference, angle * -10)

    def set_side(self, footprint, side: Side):
        if side ^ self.get_side(footprint):
            footprint.Flip(footprint.GetPosition(), False)

    def get_side(self, footprint):
        return Side(footprint.IsFlipped())
