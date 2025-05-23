import os
from digiskr.config import Config
from digiskr import config
import logging
import threading
import time
import random
import socket
from functools import reduce
from operator import and_
from telnetlib import Telnet


def _modes(d): return dict([(v, k) for (k, v) in d.items()])


class TelnetCluster(object):
    sharedInstance = {}
    creationLock = threading.Lock()
    interval = 15

    supportedModes = _modes(config.MODES)

    @staticmethod
    def getSharedInstance(station: str):
        with TelnetCluster.creationLock:
            if TelnetCluster.sharedInstance.get(station) is None:
                TelnetCluster.sharedInstance[station] = TelnetCluster(station)
        return TelnetCluster.sharedInstance[station]

    @staticmethod
    def stop():
        [telnet.cancelTimer() for telnet in TelnetCluster.sharedInstance.values()]

    def __init__(self, station: str):
        self.spots = []
        self.spotLock = threading.Lock()
        self.uploader = Uploader(station)
        self.station = station
        self.timer = None

        # prepare logdir for uploader
        self.logdir = os.path.join(
            Config.logdir(), "spots", "telnet", station)
        os.makedirs(self.logdir, exist_ok=True)

    def scheduleNextUpload(self):
        if self.timer:
            return
        delay = TelnetCluster.interval + random.uniform(0, 15)
        logging.info(
            "scheduling next telnet upload in %3.2f seconds", delay)
        self.timer = threading.Timer(delay, self.upload)
        self.timer.setName("telnet.uploader-%s" % self.station)
        self.timer.start()

    def spotEquals(self, s1, s2):
        keys = ["callsign", "timestamp",
                "locator", "db", "freq", "mode", "msg"]

        return reduce(and_, map(lambda key: s1[key] == s2[key], keys))

    def spot(self, spot):
        if not spot["mode"] in TelnetCluster.supportedModes.keys():
            return
        with self.spotLock:
            if any(x for x in self.spots if self.spotEquals(spot, x)):
                # dupe
                pass
            else:
                self.spots.append(spot)
            self.scheduleNextUpload()

    def upload(self):
        try:
            with self.spotLock:
                self.timer = None
                spots = self.spots
                self.spots = []
            if spots:
                self.uploader.upload(spots)
                self.savelog(spots)
        except Exception:
            logging.exception("Failed to upload spots")

    def savelog(self, spots):
        spot_lines = []
        for s in spots:
            spot_lines.append("%s %s %s  %s %s %s %s\n" % (
                time.strftime("%H%M%S",  time.localtime(s["timestamp"])),
                ("%2.1f" % s["db"]).rjust(5, " "),
                ("%2.1f" % s["dt"]).rjust(5, " "),
                ("%2.6f" % s["freq"]).rjust(10, " "),
                TelnetCluster.supportedModes[s["mode"]],
                s["callsign"].ljust(6, " "),
                s["locator"]
            ))

        if "LOG_SPOTS" in Config.get() and Config.get()["LOG_SPOTS"]:
            file = os.path.join(self.logdir, "%s.log" %
                                time.strftime("%y%m%d", time.localtime()))
            with open(file, "a") as f:
                f.writelines(spot_lines)

    def cancelTimer(self):
        if self.timer:
            self.timer.cancel()
            self.timer.join()
        self.timer = None


class Uploader(object):
    receiverDelimiter = [0x99, 0x92]
    senderDelimiter = [0x99, 0x93]

    def __init__(self, station: str):
        self.station = Config.get()["STATIONS"][station]
        self.station["name"] = station
        # logging.debug("Station: %s", self.station)
        self.sequence = 0
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def upload(self, spots):
        logging.warning("uploading %i spots               ", len(spots))
        #print(spots)
        #for packet in self.getPackets(spots):
        with Telnet('criede-fst-gw01',1234) as tn:
            tn.read_until(b"login: ",timeout=2)
            tn.write(b"dk0bt-0\n")
            logging.warning(tn.read_until(b">").decode('ascii'))
            for s in spots:
              #print(s)
              msg=b"dx " +bytes(str(s['freq']*1000),' utf-8')+b"  " +bytes(s['callsign'],'utf-8')+b" " +bytes(s['mode'],'utf-8')+b"\r\n"
              #logging.warning(msg)
              tn.write(msg)
              logging.warning(tn.read_until(b">").decode('ascii'))
            tn.close

    def getPackets(self, spots):
        encoded = [self.encodeSpot(spot) for spot in spots]
        # filter out any erroneous encodes
        encoded = [e for e in encoded if e is not None]

        def chunks(l, n):
            """Yield successive n-sized chunks from l."""
            for i in range(0, len(l), n):
                yield l[i: i + n]

        rHeader = self.getReceiverInformationHeader()
        rInfo = self.getReceiverInformation()
        sHeader = self.getSenderInformationHeader()

        packets = []
        # 50 seems to be a safe bet
        for chunk in chunks(encoded, 50):
            sInfo = self.getSenderInformation(chunk)
            length = 16 + len(rHeader) + len(sHeader) + len(rInfo) + len(sInfo)
            header = self.getHeader(length)
            packets.append(header + rHeader + sHeader + rInfo + sInfo)

        return packets

    def getHeader(self, length):
        self.sequence += 1
        return bytes(
            # protocol version
            [0x00, 0x0A]
            + list(length.to_bytes(2, "big"))
            + list(int(time.time()).to_bytes(4, "big"))
            + list(self.sequence.to_bytes(4, "big"))
            + list((id(self) & 0xFFFFFFFF).to_bytes(4, "big"))
        )

    def encodeString(self, s):
        return [len(s)] + list(s.encode("utf-8"))

    def encodeSpot(self, spot):
        try:
            return bytes(
                self.encodeString(spot["callsign"])
                # freq in Hz to telnet
                + list(int(spot["freq"]*1e6).to_bytes(4, "big"))
                + list(int(spot["db"]).to_bytes(1, "big", signed=True))
                + self.encodeString(spot["mode"])
                + self.encodeString(spot["locator"])
                # informationsource. 1 means "automatically extracted
                + [0x01]
                + list(spot["timestamp"].to_bytes(4, "big"))
            )
        except Exception:
            logging.exception("Error while encoding spot for telnet")
            return None
        

    def getReceiverInformationHeader(self):
        return bytes(
            # id, length
            [0x00, 0x03, 0x00, 0x2C]
            + Uploader.receiverDelimiter
            # number of fields
            + [0x00, 0x04, 0x00, 0x00]
            # receiverCallsign
            + [0x80, 0x02, 0xFF, 0xFF, 0x00, 0x00, 0x76, 0x8F]
            # receiverLocator
            + [0x80, 0x04, 0xFF, 0xFF, 0x00, 0x00, 0x76, 0x8F]
            # decodingSoftware
            + [0x80, 0x08, 0xFF, 0xFF, 0x00, 0x00, 0x76, 0x8F]
            # antennaInformation
            + [0x80, 0x09, 0xFF, 0xFF, 0x00, 0x00, 0x76, 0x8F]
            # padding
            + [0x00, 0x00]
        )

    def getReceiverInformation(self):
        callsign = self.station["callsign"]
        locator = self.station["grid"]
        antennaInformation = self.station["antenna"] if "antenna" in self.station else ""
        decodingSoftware = config.DECODING_SOFTWARE + " KiwiSDR"

        body = [b for s in [callsign, locator, decodingSoftware,
                            antennaInformation] for b in self.encodeString(s)]
        body = self.pad(body, 4)
        body = bytes(Uploader.receiverDelimiter +
                     list((len(body) + 4).to_bytes(2, "big")) + body)
        return body

    def getSenderInformationHeader(self):
        return bytes(
            # id, length
            [0x00, 0x02, 0x00, 0x3C]
            + Uploader.senderDelimiter
            # number of fields
            + [0x00, 0x07]
            # senderCallsign
            + [0x80, 0x01, 0xFF, 0xFF, 0x00, 0x00, 0x76, 0x8F]
            # frequency
            + [0x80, 0x05, 0x00, 0x04, 0x00, 0x00, 0x76, 0x8F]
            # sNR
            + [0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 0x76, 0x8F]
            # mode
            + [0x80, 0x0A, 0xFF, 0xFF, 0x00, 0x00, 0x76, 0x8F]
            # senderLocator
            + [0x80, 0x03, 0xFF, 0xFF, 0x00, 0x00, 0x76, 0x8F]
            # informationSource
            + [0x80, 0x0B, 0x00, 0x01, 0x00, 0x00, 0x76, 0x8F]
            # flowStartSeconds
            + [0x00, 0x96, 0x00, 0x04]
        )

    def getSenderInformation(self, chunk):
        sInfo = self.padBytes(b"".join(chunk), 4)
        sInfoLength = len(sInfo) + 4
        return bytes(Uploader.senderDelimiter) + sInfoLength.to_bytes(2, "big") + sInfo

    def pad(self, b, l):
        return b + [0x00 for _ in range(0, -1 * len(b) % l)]

    def padBytes(self, b, l):
        return b + bytes([0x00 for _ in range(0, -1 * len(b) % l)])
