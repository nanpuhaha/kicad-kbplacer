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

    def toList(self):
        return [self.x, self.y]


class BoardModifier():
    def __init__(self, logger, board):
        self.logger = logger
        self.board = board

    def GetConnectivity(self):
        self.board.BuildConnectivity()
        return self.board.GetConnectivity()

    def GetFootprint(self, reference):
        self.logger.info("Searching for {} footprint".format(reference))
        footprint = self.board.FindFootprintByReference(reference)
        if footprint == None:
            self.logger.error("Footprint not found")
            raise Exception("Cannot find footprint {}".format(reference))
        return footprint

    def SetPosition(self, footprint, position: wxPoint):
        self.logger.info("Setting {} footprint position: {}".format(footprint.GetReference(), position))
        if KICAD_VERSION == 7:
            footprint.SetPosition(VECTOR2I(position))
        else:
            footprint.SetPosition(position)

    def SetPositionByPoints(self, footprint, x: int, y: int):
        self.SetPosition(footprint, wxPoint(x, y))

    def GetPosition(self, footprint):
        position = footprint.GetPosition()
        self.logger.info("Getting {} footprint position: {}".format(footprint.GetReference(), position))
        return position

    def SetRelativePositionMM(self, footprint, referencePoint, direction):
        position = wxPoint(referencePoint.x + FromMM(direction[0]), referencePoint.y + FromMM(direction[1]))
        self.SetPosition(footprint, position)

    def TestTrackCollision(self, track):
        collideList = []
        trackShape = track.GetEffectiveShape()
        trackStart = track.GetStart()
        trackEnd = track.GetEnd()
        trackNetCode = track.GetNetCode()
        # connectivity needs to be last, otherwise it will update track net name before we want it to:
        connectivity = self.GetConnectivity()
        for f in self.board.GetFootprints():
            reference = f.GetReference()
            hull = f.GetBoundingHull()
            hitTestResult = hull.Collide(trackShape)
            if hitTestResult:
                for p in f.Pads():
                    padName = p.GetName()
                    padShape = p.GetEffectiveShape()

                    # track has non default netlist set so we can skip collision detection for pad of same netlist:
                    if trackNetCode != 0 and trackNetCode == p.GetNetCode():
                        self.logger.debug("Track collision ignored, pad {}:{} on same netlist: {}/{}".format(reference, padName, track.GetNetname(), p.GetNetname()))
                        continue

                    # if track starts or ends in pad than assume this collision is expected, with the execption of case where track
                    # already has netlist set and it is different than pad's netlist
                    if p.HitTest(trackStart) or p.HitTest(trackEnd):
                        if trackNetCode != 0 and trackNetCode != p.GetNetCode() and p.IsOnLayer(track.GetLayer()):
                            self.logger.debug("Track collide with pad {}:{}".format(reference, padName))
                            collideList.append(p)
                        else:
                            self.logger.debug("Track collision ignored, track starts or ends in pad {}:{}".format(reference, padName))
                    else:
                        hitTestResult = padShape.Collide(trackShape, FromMM(DEFAULT_CLEARANCE_MM))
                        onSameLayer = p.IsOnLayer(track.GetLayer())
                        if hitTestResult and onSameLayer:
                            self.logger.debug("Track collide with pad {}:{}".format(reference, padName))
                            collideList.append(p)
        # track ids to clear at the end:
        tracksToClear = []
        for t in self.board.GetTracks():
            # check collision if not itself, on same layer and with different netlist (unless 'trackNetCode' is default '0' netlist):
            if t.m_Uuid.__ne__(track.m_Uuid) and t.IsOnLayer(track.GetLayer()) and (trackNetCode != t.GetNetCode() or trackNetCode == 0):
                if trackStart == t.GetStart() or trackStart == t.GetEnd() or trackEnd == t.GetStart() or trackEnd == t.GetEnd():
                    self.logger.debug("Track collision ignored, track starts or ends at the end of {} track".format(t.m_Uuid.AsString()))
                    # ignoring one track means that we can ignore all other connected to it:
                    tracksToClear += [x.m_Uuid for x in connectivity.GetConnectedTracks(t)]
                    # check if connection to this track clears pad collision:
                    connectedPadsIds = [x.m_Uuid for x in connectivity.GetConnectedPads(t)]
                    for collision in list(collideList):
                        if collision.m_Uuid in connectedPadsIds:
                            self.logger.debug("Pad collision removed due to connection with track which leads to that pad")
                            collideList.remove(collision)
                else:
                    hitTestResult = t.GetEffectiveShape().Collide(trackShape, FromMM(DEFAULT_CLEARANCE_MM))
                    if hitTestResult:
                        self.logger.debug("Track collide with another track: {}".format(t.m_Uuid.AsString()))
                        collideList.append(t)
        for collision in list(collideList):
            if collision.m_Uuid in tracksToClear:
                self.logger.debug("Track collision with {} removed due to connection with track which leads to it".format(collision.m_Uuid.AsString()))
                collideList.remove(collision)
        return len(collideList) != 0

    def AddTrackToBoard(self, track):
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
        if not self.TestTrackCollision(track):
            layerName = self.board.GetLayerName(track.GetLayer())
            start = track.GetStart()
            stop = track.GetEnd()
            self.logger.info("Adding track segment ({}): [{}, {}]".format(layerName, start, stop))
            self.board.Add(track)
            return stop
        else:
            self.logger.warning("Could not add track segment due to detected collision")
            return None

    def AddTrackSegmentByPoints(self, start, end, layer=B_Cu):
        track = PCB_TRACK(self.board)
        track.SetWidth(FromMM(0.25))
        track.SetLayer(layer)
        if KICAD_VERSION == 7:
            track.SetStart(VECTOR2I(start))
            track.SetEnd(VECTOR2I(end))
        else:
            track.SetStart(start)
            track.SetEnd(end)
        return self.AddTrackToBoard(track)

    def AddTrackSegment(self, start, vector, layer=B_Cu):
        end = wxPoint(start.x + vector[0], start.y + vector[1])
        return self.AddTrackSegmentByPoints(start, end, layer)

    def Rotate(self, footprint, rotationReference, angle):
        self.logger.info("Rotating {} footprint: rotationReference: {}, rotationAngle: {}".format(footprint.GetReference(), rotationReference, angle))
        if KICAD_VERSION == 7:
            footprint.Rotate(VECTOR2I(rotationReference), EDA_ANGLE(angle * -1, DEGREES_T))
        else:
            footprint.Rotate(rotationReference, angle * -10)

    def SetSide(self, footprint, side: Side):
        if side ^ self.GetSide(footprint):
            footprint.Flip(footprint.GetPosition(), False)

    def GetSide(self, footprint):
        return Side(footprint.IsFlipped())

