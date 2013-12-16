BitTorrent
==========

A BitTorrent client (not using Twisted) suitable for entry into the BitTorrent games.

This version of the BitTorrent client is optimized to download the torrent specified on the command line and exit when finished.

Invocation
----------

python client.py

Requirements
------------

The following libraries are required:

bencode
bitstring
requests


Unit Tests
----------

py.test is used for unit testing.  From the test directory run:

PYTHONPATH=..:${PYTHONPATH} py.test
