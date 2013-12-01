"""
The TorrentMgr manages downloading and uploading for a torrent specified by a
metafile.  After being created, the TorrentMgr must be told to initialize,
whereupon it gets the metafile information and initializes itself to reflect
whether pieces of the torrent are already on disk (not implemented) and
initiates contact with the tracker.  After being told to start, it begins
to field requests for uploads (not implemented) and, if the torrent is not
fully downloaded, starts contacting peers to get needed pieces.

The TorrentMgr determines the strategy of whom to contact for which pieces
including end game strategy.  It also manages the amount of download and
upload traffic (not implemented).

This implementation of the TorrentMgr is simple in many ways.  Initially, it
opens a fixed number of connections with peers.  Upon receipt of a bitfield
or have message which includes a needed piece, it expresses interest to that
peer.  When that peer unchokes, it starts sequentially requesting blocks for
that piece.  If the peer chokes in the middle of the piece, the data received
so far is put aside and the rest of the piece is assigned to the next free,
unchoked peer which has the piece.  When a connected peer has multiple needed
pieces, the rarest piece across all peers is chosen to acquire.  When a peer
delivers a complete piece and has no other needed pieces, the TorrentMgr tells
it that it is no longer interested.  Then it opens a connection to an
additional peer.  Generally, the number of peers for whom the TorrentMgr is
interested stays at the fixed number although it will temporarily exceed that
number when a peer for whom there was no interest notifies the TorrentMgr
that it has obtained a needed piece.

Periodically, the TorrentMgr checks to over the peers that are interested and
requesting to try to rectify potential hung situations such as when a peer is
interested but unchoked for a long period of time or when it has an outstanding
request over a long period of time.

This TorrentMgr does not currently implement pipelined requests, an endgame
strategy or uploading.
"""

import hashlib
import logging
from bitstring import BitArray
from filemgr import FileMgr
from metainfo import Metainfo
from peerproxy import PeerProxy
from trackerproxy import TrackerProxy

from twisted.internet.defer import Deferred

logger = logging.getLogger('bt.torrentmgr')

_BLOCK_SIZE = 2**14
_TIMER_INTERVAL = 10
_MAX_RETRIES = 2


class TorrentMgrError(Exception):
    pass


class TorrentMgr(object):
    class _States(object):
        (Uninitialized, Initialized, Started) = range(3)

    def __init__(self, filename, port, peer_id, reactor):
        self._filename = filename
        self._port = port
        self._peer_id = peer_id
        self._reactor = reactor
        self._state = self._States.Uninitialized
        self._completionDeferred = None

    def initialize(self):
        """
        initialize() returns a deferred which fires when initialization
        of the TorrentMgr is complete or an error has occurred.
        """
        if not self._state == self._States.Uninitialized:
            error = "TorrentMgr can't be reinitialized"
            logger.debug(error)
            d = Deferred()
            d.errback(TorrentMgrError(error))
            return d

        try:
            self._metainfo = Metainfo(self._filename)
        except ValueError as err:
            logger.error(err)
            d = Deferred()
            d.errback(TorrentMgrError(err.message))
            return d
        except IOError as err:
            logger.error(err)
            d = Deferred()
            d.errback(TorrentMgrError(err.strerror))
            return d

        # _peers is a list of peers that the TorrentMgr is trying
        # to communicate with
        self._peers = []

        # _bitfields is a dictionary mapping peers to a bitfield of the pieces
        # each has
        self._bitfields = {}

        # _have is the bitfield for this torrent. It is initialized to reflect
        # which pieces are already available on disk.
        self._filemgr = FileMgr(self._metainfo)
        self._have = self._filemgr.have()

        # _needed is a dictionary of pieces which are still needed.
        # The value for each piece is a tuple of the number of peers which
        # have the piece and a list of those peers.
        self._needed = {piece: (0, []) for piece
                        in list(self._have.findall('0b0'))}

        # _interested is a dictionary of peers to whom interest has been
        # expressed.  The value for each peer is a tuple of the piece that
        # has been reserved for the peer, the number of bytes of the piece that
        # have already been received, the sha1 hash of the bytes received so
        # far and the value of the tick at the time interest was expressed.
        self._interested = {}

        # _requesting is a dictionary of peers to whom a block request has been
        # made.  The value for each peer is a tuple of the piece that is being
        # requested, the number of bytes that have already been received, the
        # shal2 hash of the bytes received so far, the value of the tick at
        # the time the request was made and the number of retries that have
        # been attempted
        self._requesting = {}

        # _partial is a list which tracks pieces that were interrupted while
        # being downloaded.  Each entry is a tuple containing the index of the
        # piece, the number of bytes received so far and the sha1 hash of those
        # bytes.
        self._partial = []

        self._tracker_proxy = TrackerProxy(self._metainfo, self._port,
                                           self._peer_id)

        def success(result):
            self._state = self._States.Initialized

        def failure(failure):
            logger.critical("Could not connect to tracker at {}"
                            .format(self._metainfo.announce))
            message = failure.value.message
            logger.debug("    Tracker Error: {}".format(message))
            raise TorrentMgrError(message)

        return self._tracker_proxy.start().addCallbacks(success, failure)

    def start(self):
        if not self._state == self._States.Initialized:
            raise TorrentMgrError("TorrentMgr must be initialized to be "
                                  "started")

        self._reactor.callLater(_TIMER_INTERVAL, self.timer_event)
        self._tick = 1

        logger.info("Starting to serve torrent {}".format(self._filename))
        print "Starting to serve torrent {}".format(self._filename)

        self._state = self._States.Started

        self._connect_to_peers(20)

        self._completionDeferred = Deferred()
        return self._completionDeferred

    def percent(self):
        if not self._state == self._States.Uninitialized:
            return 100 * (1 - (len(self._needed) /
                               float(self._metainfo.num_pieces)))
        else:
            raise TorrentMgrError("Can't get percent on uninitialized "
                                  "TorrentMgr")

    def info_hash(self):
        if not self._state == self._States.Uninitialized:
            return self._metainfo.info_hash
        else:
            raise TorrentMgrError("Can't get hash on uninitialized "
                                  "TorrentMgr")

    def name(self):
        return self._metainfo.name

    def _connect_to_peers(self, n):
        # Get addresses of n peers from the tracker and try to establish
        # a connection with each

        def handle_addrs(addrs):
            for addr in addrs:
                peer = PeerProxy(self, self._peer_id,
                                 (addr['ip'], addr['port']), self._reactor,
                                 info_hash=self._metainfo.info_hash)
                self._peers.append(peer)
                self._bitfields[peer] = BitArray(self._metainfo.num_pieces)
        self._tracker_proxy.get_peers(n).addCallback(handle_addrs)

    def _remove_peer(self, peer):
        # Clean up references to the peer in various data structures
        self._peers.remove(peer)

        pieces = list(self._bitfields[peer].findall('0b1'))
        for piece in pieces:
            if piece in self._needed:
                occurences, peers = self._needed[piece]
                if peer in peers:
                    peers.remove(peer)
                    self._needed[piece] = (occurences-1, peers)

        del self._bitfields[peer]

        if peer in self._interested:
            del self._interested[peer]
        elif peer in self._requesting:
            # If the peer is in the middle of downloading a piece, save
            # the state in the partial list
            index, offset, sha1, _, _ = self._requesting[peer]
            self._partial.append((index, offset, sha1))
            del self._requesting[peer]

    def _rarest(self):
        # Returns a list of tuples which includes a piece index sorted by
        # the number of peers which have the piece in ascending order
        return sorted([(occurences, peers, index)
                       for (index, (occurences, peers)) in self._needed.items()
                       if occurences != 0])

    def _show_interest(self, peer):
        if not peer.is_interested():
            logger.debug("Expressing interest in peer {}"
                         .format(str(peer.addr())))
            peer.interested()

        if not peer.is_peer_choked():
            self._request(peer)

    def _check_interest(self, peer):
        # If the peer is not already interested or requesting, identify a piece
        # for it to download and show interest to the peer.
        if not peer in self._interested and not peer in self._requesting:
            # Compute the set of needed pieces which the peer has that are not
            # already designated for another peer
            needed = self._have.copy()
            needed.invert()
            of_interest = list((needed & self._bitfields[peer]).findall('0b1'))
            dont_consider = [i for i, _, _, _ in self._interested.values()]
            dont_consider.extend([i for i, _, _, _, _
                                  in self._requesting.values()])

            # When there are potential pieces for the peer to download, give
            # preference to a piece that has already been partially
            # downloaded followed by the rarest available piece
            if len(of_interest) > 0:
                for index, offset, sha1 in self._partial:
                    if index in of_interest:
                        self._partial.remove((index, offset, sha1))
                        self._interested[peer] = (index, offset, sha1,
                                                  self._tick)
                        self._show_interest(peer)
                        return

                for _, _, index in self._rarest():
                    if index in of_interest and not index in dont_consider:
                        self._interested[peer] = (index, 0, hashlib.sha1(),
                                                  self._tick)
                        self._show_interest(peer)
                        return

            # If there is no further piece for a peer which was previously
            # interested to download, make it not interested and connect to
            # another peer
            if not peer in self._interested and peer.is_interested():
                logger.debug("Expressing lack of interest in peer {}"
                             .format(str(peer.addr())))
                peer.not_interested()
                self._connect_to_peers(1)

    def _request(self, peer):
        if peer in self._interested:
            index, offset, sha1, _ = self._interested[peer]
            del self._interested[peer]
            self._requesting[peer] = (index, offset, sha1, self._tick, 0)

        index, received_bytes, _, _, _ = self._requesting[peer]

        bytes_to_request = self._bytes_to_request(index, received_bytes)
        logger.debug("Requesting pc: {} off: {} len: {} from {}"
                     .format(index, received_bytes, bytes_to_request,
                             str(peer.addr())))
        peer.request(index, received_bytes, bytes_to_request)

    def _is_last_piece(self, index):
        return index == self._metainfo.num_pieces-1

    def _length_of_last_piece(self):
        return (self._metainfo.total_length -
               (self._metainfo.num_pieces-1)*self._metainfo.piece_length)

    def _length_of_piece(self, index):
        if self._is_last_piece(index):
            return self._length_of_last_piece()
        else:
            return self._metainfo.piece_length

    def _in_last_block(self, index, offset):
        if self._is_last_piece(index):
            piece_length = self._length_of_last_piece()
        else:
            piece_length = self._metainfo.piece_length

        return piece_length-offset < _BLOCK_SIZE

    def _bytes_to_request(self, index, offset):
        if not self._in_last_block(index, offset):
            return _BLOCK_SIZE
        else:
            return self._length_of_piece(index) - offset

    # PeerProxy callbacks

    def get_bitfield(self):
        return self._have

    def peer_unconnected(self, peer):
        logger.info("Peer {} is unconnected".format(str(peer.addr())))
        self._remove_peer(peer)
        self._connect_to_peers(1)

    def peer_bitfield(self, peer, bitfield):
        # Validate the bitfield
        length = len(bitfield)
        if (length < self._metainfo.num_pieces or
            (length > self._metainfo.num_pieces and
             bitfield[self._metainfo.num_pieces:length].any(1))):
            logger.debug("Invalid bitfield from peer {}"
                         .format(str(peer.addr())))
            peer.drop_connection()
            self._remove_peer(peer)
            self._connect_to_peers(1)
            return

        # Set the peer's bitfield and updated needed to reflect which pieces
        # the peer has
        logger.debug("Peer at {} sent bitfield".format(str(peer.addr())))
        self._bitfields[peer] = bitfield[0:self._metainfo.num_pieces]
        pieces = list(self._bitfields[peer].findall('0b1'))
        for piece in pieces:
            if piece in self._needed:
                occurences, peers = self._needed[piece]
                if not peer in peers:
                    peers.append(peer)
                    self._needed[piece] = (occurences+1, peers)

        # Check whether there may be interest obtaining a piece from this peer
        self._check_interest(peer)

    def peer_has(self, peer, index):
        # Update the peer's bitfield and needed to reflect the availability
        # of the piece
        logger.debug("Peer at {} has piece {}".format(str(peer.addr()), index))
        if index < self._metainfo.num_pieces:
            self._bitfields[peer][index] = 1
        else:
            raise IndexError

        if index in self._needed:
            occurences, peers = self._needed[index]
            if not peer in peers:
                peers.append(peer)
                self._needed[index] = (occurences+1, peers)

            # Check whether there may be interest obtaining a piece from this
            # peer
            self._check_interest(peer)

    def peer_choked(self, peer):
        logger.debug("Peer {} choked".format(str(peer.addr())))
        if peer in self._interested:
            del self._interested[peer]
        elif peer in self._requesting:
            # When choked in the middle of obtaining a piece, save the
            # progress in the partial list
            index, offset, sha1, _, _ = self._requesting[peer]
            self._partial.append((index, offset, sha1))
            del self._requesting[peer]

    def peer_unchoked(self, peer):
        logger.debug("Peer {} unchoked".format(str(peer.addr())))
        if peer in self._interested:
            self._request(peer)

    def peer_sent_block(self, peer, index, begin, buf):
        if not peer in self._requesting:
            # If a peer is very slow in responding, a block could come after
            # it has timed out.  Just ignore the data at this point and
            # ignore the slow peer
            logger.debug("Received block from peer {} which has timed out"
                         .format(str(peer.addr())))
            return

        piece, received_bytes, sha1, _, _ = self._requesting[peer]
        if piece == index and begin == received_bytes:
            # When the next expected block is received, update the hash value
            # and write the block to file
            sha1.update(buf)
            self._filemgr.write_block(index, begin, buf)
            self._requesting[peer] = (piece, received_bytes + len(buf),
                                      sha1, self._tick, 0)

            if received_bytes + len(buf) < self._length_of_piece(index):
                # Request the next block in the piece
                self._request(peer)
            else:
                # On receipt of the last block in the piece, verify the hash
                # and update the records to reflect receipt of the piece
                if sha1.digest() == self._metainfo.piece_hash(index):
                    logger.info("Successfully received piece {} from {}"
                                .format(index, str(peer.addr())))
                    del self._needed[index]
                    print "{0}: Downloaded {1:1.4f}%".format(self._filename,
                                                             self.percent())
                    self._have[index] = 1
                else:
                    logger.info("Unsuccessfully received piece {} from {}"
                                .format(index, str(peer.addr())))
                del self._requesting[peer]

                if self._needed != {}:
                    # Try to find another piece for this peer to get
                    self._check_interest(peer)
                else:
                    logger.info("Successfully downloaded entire torrent {}"
                                .format(self._filename))
                    self._completionDeferred.callback(self._filename)

    def peer_interested(self, peer):
        pass

    def peer_not_interested(self, peer):
        pass

    def peer_request(self, peer, index, begin, length):
        pass

    def peer_canceled(self, peer, index, begin, length):
        pass

    # Reactor callback

    def timer_event(self):
        self._reactor.callLater(_TIMER_INTERVAL, self.timer_event)
        self._tick += 1

        # For any peers that have been interested but unchoked for an
        # excessive period of time, stop being interested, free up assigned
        # piece and connect to another peer
        for peer, (_, _, _, tick) in self._interested.items():
            if tick + 4 == self._tick:
                logger.debug("Timed out on interest for peer {}"
                             .format(str(peer.addr())))
                peer.not_interested()
                del self._interested[peer]
                self._connect_to_peers(1)

        # For any peer that has an outstanding request for an excessive period
        # of time, resend the request message in case it got lost or is being
        # ignored
        for peer, (index, offset, sha1, tick, retries) \
                in self._requesting.items():
            if tick + 5 == self._tick:
                logger.debug("Timed out on request for peer {}"
                             .format(str(peer.addr())))
                if retries < _MAX_RETRIES:
                    self._requesting[peer] = (index, offset, sha1,
                                              self._tick, retries+1)
                    self._request(peer)
                else:
                    logger.debug("Giving up on peer {}"
                                 .format(str(peer.addr())))
                    self._partial.append((index, offset, sha1))
                    del self._requesting[peer]
                    peer.not_interested()
                    self._connect_to_peers(1)
